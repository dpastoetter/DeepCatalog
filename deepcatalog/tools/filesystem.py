"""Filesystem helpers and ADK tools for reading/filing documents."""

from __future__ import annotations

import os
import platform
import re
import shutil
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any

from deepcatalog.config import ensure_data_dirs
from deepcatalog.env_permissions import harden_app_owned_file, open_private_file
from deepcatalog.media_validate import MediaValidationError, validate_scan_file
from deepcatalog.media_worker import MediaWorkerError, extract_pdf_page_texts_isolated
from deepcatalog.ollama_setup import host_desktop_env
from deepcatalog.settings import get_folder_for_category, get_source_dir

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
PDF_SUFFIXES = {".pdf"}
SUPPORTED_SUFFIXES = IMAGE_SUFFIXES | PDF_SUFFIXES
UPLOAD_CHUNK_BYTES = 1024 * 1024  # 1 MiB stream chunks


def path_is_within(path: Path, root: Path) -> bool:
    """True when ``path`` resolves under ``root`` (defense-in-depth helper)."""
    try:
        resolved = path.resolve()
        base = root.resolve()
        return resolved == base or resolved.is_relative_to(base)
    except (OSError, ValueError):
        return False


def _posix_nofollow_supported() -> bool:
    return os.name == "posix" and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")


def _normalize_archive_year(year: str | None) -> str:
    """Return a single path segment: ``YYYY`` or ``unknown`` (never ``..`` / separators)."""
    year_part = (year or "unknown").strip()
    # Reject traversal / multi-segment values before any YYYY extraction.
    if (
        not year_part
        or year_part in {".", ".."}
        or "/" in year_part
        or "\\" in year_part
        or Path(year_part).name != year_part
    ):
        return "unknown"
    if year_part != "unknown" and not re.fullmatch(r"\d{4}", year_part):
        match = re.fullmatch(r"(\d{4})(?:-\d{2}-\d{2})?", year_part)
        year_part = match.group(1) if match else "unknown"
    if year_part != "unknown" and not re.fullmatch(r"\d{4}", year_part):
        return "unknown"
    return year_part


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except OSError:
        return False


def _reject_symlink_path(path: Path, *, label: str) -> dict[str, Any] | None:
    """Return an error dict when ``path`` exists as a symlink."""
    try:
        if path.is_symlink():
            return {
                "status": "error",
                "error": (
                    f"{label} is a symlink — refusing to follow archive destination links ({path})"
                ),
                "code": "symlink_escape",
            }
    except OSError as exc:
        return {
            "status": "error",
            "error": f"could not inspect {label}: {exc}",
            "code": "path_escape",
        }
    return None


def _assert_resolved_under(path: Path, root: Path, *, label: str) -> Path | dict[str, Any]:
    """Resolve ``path`` and require it stays under ``root`` via ``is_relative_to``."""
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError as exc:
        return {
            "status": "error",
            "error": f"could not resolve {label}: {exc}",
            "code": "path_escape",
        }
    if not (resolved == base or resolved.is_relative_to(base)):
        return {
            "status": "error",
            "error": f"{label} resolves outside the category archive root ({base})",
            "code": "path_escape",
        }
    return resolved


def _open_dir_nofollow(path: Path) -> int:
    """Open a directory FD without following a final-component symlink."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    return os.open(path, flags)


def _mkdir_child_nofollow(parent_fd: int, name: str, *, mode: int = 0o700) -> None:
    try:
        os.mkdir(name, mode, dir_fd=parent_fd)
    except FileExistsError:
        pass


def _ensure_archive_year_dir(category_root: Path, year_part: str) -> Path | dict[str, Any]:
    """
    Ensure ``category_root / year_part`` is a real directory under ``category_root``.

    Rejects category or year paths that are symlinks (including links that point
    elsewhere inside the same tree) so destination writes never follow an
    attacker-controlled link. On POSIX, creates/opens with ``O_NOFOLLOW``.
    """
    year_part = _normalize_archive_year(year_part)
    try:
        category = category_root.expanduser()
    except OSError as exc:
        return {
            "status": "error",
            "error": f"invalid category folder: {exc}",
            "code": "path_escape",
        }

    err = _reject_symlink_path(category, label="category folder")
    if err:
        return err

    try:
        if not category.exists():
            category.parent.mkdir(parents=True, exist_ok=True)
            if _posix_nofollow_supported() and category.parent.is_dir():
                parent_fd = _open_dir_nofollow(category.parent.resolve())
                try:
                    _mkdir_child_nofollow(parent_fd, category.name)
                finally:
                    os.close(parent_fd)
            else:
                category.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {
            "status": "error",
            "error": f"could not create category folder: {exc}",
            "code": "path_escape",
        }

    err = _reject_symlink_path(category, label="category folder")
    if err:
        return err
    if not category.is_dir():
        return {
            "status": "error",
            "error": f"category folder is not a directory: {category}",
            "code": "path_escape",
        }

    try:
        root_resolved = category.resolve()
    except OSError as exc:
        return {
            "status": "error",
            "error": f"could not resolve category folder: {exc}",
            "code": "path_escape",
        }

    dest_dir = category / year_part
    err = _reject_symlink_path(dest_dir, label="year directory")
    if err:
        return err

    try:
        if _posix_nofollow_supported():
            cat_fd = _open_dir_nofollow(root_resolved)
            try:
                _mkdir_child_nofollow(cat_fd, year_part)
                # Re-check: open year with O_NOFOLLOW (fails if it is a symlink).
                year_fd = os.open(
                    year_part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=cat_fd,
                )
                os.close(year_fd)
            finally:
                os.close(cat_fd)
        else:
            dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # ELOOP / equivalent when a symlink appeared between check and open.
        return {
            "status": "error",
            "error": f"could not create archive year directory securely: {exc}",
            "code": "symlink_escape",
        }

    err = _reject_symlink_path(dest_dir, label="year directory")
    if err:
        return err

    resolved_year = _assert_resolved_under(dest_dir, root_resolved, label="year directory")
    if isinstance(resolved_year, dict):
        return resolved_year
    if not dest_dir.is_dir() or dest_dir.is_symlink():
        return {
            "status": "error",
            "error": f"year directory is not a real directory: {dest_dir}",
            "code": "symlink_escape",
        }
    return dest_dir


def _unique_archive_destination(dest_dir: Path, safe_name: str) -> Path | dict[str, Any]:
    """Pick ``dest_dir/safe_name``, skipping existing files and destination symlinks."""
    stem = Path(safe_name).stem
    suffix = Path(safe_name).suffix
    for n in range(1, 10_000):
        name = safe_name if n == 1 else f"{stem}_{n}{suffix}"
        candidate = dest_dir / name
        if not _lexists(candidate):
            return candidate
        try:
            if candidate.is_symlink():
                # Never write through or replace via an existing dest symlink name
                # on the first attempt — keep scanning for a free name.
                continue
        except OSError:
            continue
    return {
        "status": "error",
        "error": "could not allocate a unique archive filename",
        "code": "path_escape",
    }


def _place_file_in_archive_dir(
    src: Path,
    dest_dir: Path,
    dest_name: str,
    *,
    delete_source: bool,
) -> Path | dict[str, Any]:
    """
    Copy ``src`` into ``dest_dir/dest_name`` without following a dest symlink.

    Uses a ``.part`` sibling then ``os.replace``. On POSIX, creation uses
    ``O_NOFOLLOW`` relative to the year directory FD when available.
    """
    dest = dest_dir / dest_name
    if Path(dest_name).name != dest_name or dest_name in {".", ".."}:
        return {
            "status": "error",
            "error": "archive destination escapes year directory",
            "code": "path_escape",
        }
    if not path_is_within(dest, dest_dir):
        return {
            "status": "error",
            "error": "archive destination escapes year directory",
            "code": "path_escape",
        }
    try:
        if dest.is_symlink():
            return {
                "status": "error",
                "error": f"archive destination is a symlink: {dest}",
                "code": "symlink_escape",
            }
    except OSError as exc:
        return {
            "status": "error",
            "error": f"could not inspect archive destination: {exc}",
            "code": "path_escape",
        }

    partial_name = f".{dest_name}.{uuid.uuid4().hex}.part"
    partial = dest_dir / partial_name

    try:
        if _posix_nofollow_supported():
            dir_fd = _open_dir_nofollow(dest_dir.resolve())
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                fd = os.open(partial_name, flags, 0o600, dir_fd=dir_fd)
                try:
                    with os.fdopen(fd, "wb") as out, src.open("rb") as inp:
                        shutil.copyfileobj(inp, out, UPLOAD_CHUNK_BYTES)
                        out.flush()
                        os.fsync(out.fileno())
                except Exception:
                    try:
                        os.unlink(partial_name, dir_fd=dir_fd)
                    except OSError:
                        pass
                    raise
                try:
                    st = os.lstat(dest_name, dir_fd=dir_fd)
                    if stat.S_ISLNK(st.st_mode):
                        os.unlink(partial_name, dir_fd=dir_fd)
                        return {
                            "status": "error",
                            "error": f"archive destination is a symlink: {dest}",
                            "code": "symlink_escape",
                        }
                except FileNotFoundError:
                    pass
                os.replace(partial_name, dest_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            finally:
                os.close(dir_fd)
        else:
            with open_private_file(partial, binary=True) as out, src.open("rb") as inp:
                shutil.copyfileobj(inp, out, UPLOAD_CHUNK_BYTES)
                out.flush()
                os.fsync(out.fileno())
            if dest.is_symlink():
                partial.unlink(missing_ok=True)
                return {
                    "status": "error",
                    "error": f"archive destination is a symlink: {dest}",
                    "code": "symlink_escape",
                }
            os.replace(str(partial), str(dest))
    except OSError as exc:
        partial.unlink(missing_ok=True)
        return {
            "status": "error",
            "error": f"could not file document: {exc}",
            "code": "io_error",
        }

    if delete_source:
        try:
            src.unlink(missing_ok=True)
        except OSError as exc:
            return {
                "status": "error",
                "error": f"archived but could not remove inbox source: {exc}",
                "code": "io_error",
                "archive_path": str(dest.resolve()),
            }

    final = _assert_resolved_under(dest, dest_dir, label="archive file")
    if isinstance(final, dict):
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        return final
    return dest


def confined_inbox_file(user_path: str) -> Path:
    """
    Map a caller-supplied path to a file inside the (flat) inbox.

    Only the basename is used, so absolute paths and ``..`` segments cannot
    escape the inbox. Lexical prefix checks reject paths that are not already
    under the inbox without resolving the user string as a filesystem path.
    """
    ensure_data_dirs()
    inbox = get_source_dir().resolve()
    name = Path(str(user_path or "")).name
    if not name or name in {".", ".."}:
        raise ValueError("only files inside the configured inbox can be processed")
    raw = Path(str(user_path)).expanduser()
    if raw.is_absolute():
        try:
            raw.relative_to(inbox)
        except ValueError as exc:
            raise ValueError("only files inside the configured inbox can be processed") from exc
    candidate = (inbox / name).resolve()
    if not candidate.is_relative_to(inbox):
        raise ValueError("only files inside the configured inbox can be processed")
    return candidate


def require_inbox_source(path: str | Path) -> Path | dict[str, Any]:
    """
    Resolve ``path`` and require it to be a file inside the configured inbox.

    Returns the resolved ``Path`` on success, or an error dict suitable for
    ADK tool responses. Used by read/file tools so agents cannot touch
    arbitrary home-directory PDFs even if a debug UI is exposed.
    """
    ensure_data_dirs()
    inbox = get_source_dir().resolve()
    try:
        file_path = Path(path).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        return {
            "status": "error",
            "error": f"invalid path: {exc}",
            "code": "invalid_path",
        }

    if not path_is_within(file_path, inbox):
        return {
            "status": "error",
            "error": (f"source path escapes inbox ({inbox}): refusing to operate on {file_path}"),
            "code": "outside_inbox",
        }
    if not file_path.exists() or not file_path.is_file():
        return {
            "status": "error",
            "error": f"file not found: {path}",
            "code": "missing",
        }
    return file_path


def _safe_stem(value: str) -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", value.strip(), flags=re.UNICODE)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    return cleaned[:120] or "document"


def list_inbox() -> dict[str, Any]:
    """List supported scan files waiting in the configured source folder."""
    ensure_data_dirs()
    inbox = get_source_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    entries = []
    for p in inbox.iterdir():
        if not p.is_file() or p.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        stat = p.stat()
        entries.append(
            {
                "name": p.name,
                "path": str(p.resolve()),
                "suffix": p.suffix.lower(),
                "size_bytes": stat.st_size,
                "mtime": stat.st_mtime,
            }
        )
    files = sorted(entries, key=lambda item: str(item["name"]))
    return {
        "status": "success",
        "count": len(files),
        "files": files,
        "source_dir": str(inbox),
    }


def clear_inbox() -> dict[str, Any]:
    """Delete all supported scan files from the configured source folder."""
    ensure_data_dirs()
    inbox = get_source_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    removed: list[str] = []
    for path in sorted(inbox.iterdir()):
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            path.unlink()
            removed.append(path.name)
    return {
        "status": "success",
        "removed_count": len(removed),
        "removed": removed,
        "source_dir": str(inbox),
    }


def _which_host(name: str, env: dict[str, str]) -> str | None:
    """Resolve a host binary using the cleaned desktop PATH."""
    path = env.get("PATH")
    found = shutil.which(name, path=path)
    if found:
        return found
    for candidate in (f"/usr/bin/{name}", f"/bin/{name}"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _run_host_cmd(
    argv: list[str],
    *,
    env: dict[str, str],
    timeout: float = 4.0,
) -> bool:
    """
    Run a host desktop helper and treat a quick non-zero exit as failure.

    ``xdg-open`` / ``gio`` normally return promptly after handing off. A crash
    from polluted AppImage libs also returns quickly — do not report success
    in that case. Long-lived helpers are accepted after ``timeout``.
    """
    try:
        completed = subprocess.run(  # noqa: S603
            argv,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        # Still running after handoff — treat as success.
        return True
    except OSError:
        return False
    return completed.returncode == 0


def _reveal_via_file_manager1(file_path: Path, env: dict[str, str]) -> bool:
    """Ask the session file manager to select ``file_path`` (FreeDesktop)."""
    uri = file_path.as_uri()
    gdbus = _which_host("gdbus", env)
    if gdbus:
        return _run_host_cmd(
            [
                gdbus,
                "call",
                "--session",
                "--dest",
                "org.freedesktop.FileManager1",
                "--object-path",
                "/org/freedesktop/FileManager1",
                "--method",
                "org.freedesktop.FileManager1.ShowItems",
                f"['{uri}']",
                "",
            ],
            env=env,
        )
    dbus_send = _which_host("dbus-send", env)
    if dbus_send:
        return _run_host_cmd(
            [
                dbus_send,
                "--session",
                "--dest=org.freedesktop.FileManager1",
                "--type=method_call",
                "/org/freedesktop/FileManager1",
                "org.freedesktop.FileManager1.ShowItems",
                f"array:string:{uri}",
                "string:",
            ],
            env=env,
        )
    return False


def _open_path_with_host(path: Path, env: dict[str, str]) -> bool:
    """Open a file or directory with the host default handler."""
    target = str(path)
    for name, extra in (
        ("gio", ["open", target]),
        ("xdg-open", [target]),
    ):
        exe = _which_host(name, env)
        if not exe:
            continue
        if _run_host_cmd([exe, *extra], env=env):
            return True
    return False


def reveal_in_explorer(path: str) -> dict[str, Any]:
    """
    Reveal a local file in the system file manager (or open its folder).

    On Linux this prefers the FreeDesktop FileManager1 D-Bus API, then
    ``gio`` / ``xdg-open`` on the parent folder. Host tools are launched with
    ``host_desktop_env`` so AppImage / Ollama library paths cannot crash them.
    """
    file_path = Path(path).expanduser().resolve()
    if not file_path.exists():
        return {"status": "error", "error": f"file not found: {path}"}

    env = host_desktop_env()
    system = platform.system()
    try:
        if system == "Darwin":
            if not _run_host_cmd(["open", "-R", str(file_path)], env=env):
                return {"status": "error", "error": "could not open the file manager"}
        elif system == "Windows":
            subprocess.Popen(  # noqa: S603
                ["explorer", "/select,", str(file_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
        else:
            if _reveal_via_file_manager1(file_path, env):
                return {
                    "status": "success",
                    "path": str(file_path),
                    "opened": "file-manager1",
                }
            # Select helpers (may exist but crash under a bad env — verify exit).
            for cmd in (
                ["nautilus", "--select", str(file_path)],
                ["dolphin", "--select", str(file_path)],
                ["nemo", str(file_path)],
            ):
                exe = _which_host(cmd[0], env)
                if not exe:
                    continue
                if _run_host_cmd([exe, *cmd[1:]], env=env, timeout=2.0):
                    return {
                        "status": "success",
                        "path": str(file_path),
                        "opened": cmd[0],
                    }
            if _open_path_with_host(file_path.parent, env):
                return {
                    "status": "success",
                    "path": str(file_path),
                    "opened": "folder",
                }
            return {
                "status": "error",
                "error": (
                    "Could not open a file manager (tried FileManager1, nautilus, "
                    "dolphin, nemo, gio, xdg-open)"
                ),
            }
    except OSError:
        return {"status": "error", "error": "could not open the file manager"}

    return {
        "status": "success",
        "path": str(file_path),
        "opened": "explorer",
    }


def open_with_os(path: str) -> dict[str, Any]:
    """
    Open a local file with the desktop default application (gio / xdg-open).

    Uses ``host_desktop_env`` so AppImage-bundled WebKit / Ollama libs do not
    break the host PDF viewer or image app.
    """
    file_path = Path(path).expanduser().resolve()
    if not file_path.exists() or not file_path.is_file():
        return {"status": "error", "error": f"file not found: {path}"}

    env = host_desktop_env()
    system = platform.system()
    try:
        if system == "Darwin":
            if not _run_host_cmd(["open", str(file_path)], env=env):
                return {"status": "error", "error": "could not open the file"}
        elif system == "Windows":
            os.startfile(str(file_path))  # type: ignore[attr-defined]
        else:
            if not _open_path_with_host(file_path, env):
                return {
                    "status": "error",
                    "error": "Could not open the file (tried gio and xdg-open)",
                }
    except OSError:
        return {"status": "error", "error": "could not open the file"}

    return {
        "status": "success",
        "path": str(file_path),
        "opened": "os",
    }


def read_document(path: str) -> dict[str, Any]:
    """
    Read a local PDF or image for classification/extraction.

    The source must resolve inside the configured inbox (``get_source_dir()``).
    For PDFs, returns extracted text when available. Scanned PDFs may have
    empty text; agents should still classify from filename/context and any
    available text. Images return metadata and note that vision OCR is needed.
    """
    confined = require_inbox_source(path)
    if isinstance(confined, dict):
        return confined
    file_path = confined

    suffix = file_path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        return {
            "status": "error",
            "error": f"unsupported file type: {suffix}",
            "supported": sorted(SUPPORTED_SUFFIXES),
        }

    result: dict[str, Any] = {
        "status": "success",
        "path": str(file_path),
        "filename": file_path.name,
        "suffix": suffix,
        "size_bytes": file_path.stat().st_size,
        "text": "",
        "page_count": None,
        "is_image": suffix in IMAGE_SUFFIXES,
        "is_pdf": suffix in PDF_SUFFIXES,
    }

    if suffix in PDF_SUFFIXES:
        try:
            media = validate_scan_file(file_path)
            page_texts = extract_pdf_page_texts_isolated(file_path)
            pages = [{"page": i + 1, "text": text} for i, text in enumerate(page_texts)]
            result["page_count"] = media.get("page_count", len(pages))
            result["pages"] = pages
            result["text"] = "\n\n".join(
                f"[Page {p['page']}]\n{p['text']}".strip() for p in pages
            ).strip()
            if not result["text"]:
                result["note"] = (
                    "No extractable text layer; treat as scanned PDF and infer "
                    "from available cues / multimodal analysis."
                )
        except MediaValidationError as exc:
            return {"status": "error", "error": str(exc), "code": exc.code}
        except MediaWorkerError as exc:
            return {"status": "error", "error": f"failed to read PDF: {exc}", "code": exc.code}
        except Exception as exc:  # noqa: BLE001 - surface to agent
            return {"status": "error", "error": f"failed to read PDF: {exc}"}
    else:
        try:
            media = validate_scan_file(file_path)
            result["media"] = media
        except MediaValidationError as exc:
            return {"status": "error", "error": str(exc), "code": exc.code}
        result["note"] = (
            "Image scan; no text layer. Use multimodal reasoning on the file "
            "path/name and any known context."
        )

    return result


def _short_descriptor(text: str, max_len: int = 40) -> str:
    stem = _safe_stem(text)
    return stem[:max_len] if len(stem) > max_len else stem


def propose_filename(
    doc_type: str,
    doc_date: str | None = None,
    counterparty: str | None = None,
    subject: str | None = None,
    amount: float | None = None,
    currency: str | None = None,
    original_path: str | None = None,
    extension: str | None = None,
) -> dict[str, Any]:
    """
    Build a collision-safe meaningful filename.

    Examples:
      2024-06-12_Medical_Blood-test-results_Dr-Weber.pdf
      2024-03-15_Invoice_Acme_EUR120.pdf
      undated_Letter_Rent-increase-notice_Landlord.pdf
    """
    date_part = (doc_date or "undated").replace("/", "-")
    type_part = _safe_stem((doc_type or "other").title())

    descriptor_parts: list[str] = []
    if subject and subject.strip():
        descriptor_parts.append(_short_descriptor(subject))
    if counterparty and counterparty.strip():
        party_part = _safe_stem(counterparty)
        if not descriptor_parts or party_part.lower() not in descriptor_parts[0].lower():
            descriptor_parts.append(party_part)
    descriptor = "_".join(descriptor_parts) if descriptor_parts else "Unknown"

    amount_part = ""
    if amount is not None:
        cur = (currency or "EUR").upper()
        if float(amount).is_integer():
            amount_part = f"_{cur}{int(amount)}"
        else:
            amount_part = f"_{cur}{amount:.2f}".replace(".", "p")

    if extension:
        ext = extension if extension.startswith(".") else f".{extension}"
    elif original_path:
        ext = Path(original_path).suffix.lower() or ".pdf"
    else:
        ext = ".pdf"

    base = f"{date_part}_{type_part}_{descriptor}{amount_part}"
    filename = f"{_safe_stem(base)}{ext}"
    return {"status": "success", "filename": filename}


def move_to_archive(
    source_path: str,
    filename: str,
    doc_type: str = "other",
    year: str | None = None,
    *,
    delete_source: bool = True,
) -> dict[str, Any]:
    """
    File a document into {category_folder}/{yyyy}/ with the given filename.

    The source must resolve inside the configured inbox (``get_source_dir()``).
    Category folders come from Setup settings; unknown types fall back to 'other'.
    Uses a numeric suffix if the destination already exists.

    Destination directories that are symlinks (or resolve outside the category
    root) are rejected so a local attacker cannot redirect filing via the
    archive tree. On POSIX, year directories are created/opened with ``O_NOFOLLOW``.

    With delete_source=False the file is copied instead of moved, so the caller
    can commit metadata first and only then remove the source (atomic filing).
    """
    confined = require_inbox_source(source_path)
    if isinstance(confined, dict):
        return confined
    src = confined

    safe_type = _safe_stem(doc_type or "other").lower()
    year_part = _normalize_archive_year(year)

    category_root = get_folder_for_category(safe_type)
    dest_dir = _ensure_archive_year_dir(category_root, year_part)
    if isinstance(dest_dir, dict):
        return dest_dir

    safe_name = Path(filename).name
    if not safe_name or safe_name in {".", ".."}:
        return {
            "status": "error",
            "error": "invalid archive filename",
            "code": "path_escape",
        }

    dest = _unique_archive_destination(dest_dir, safe_name)
    if isinstance(dest, dict):
        return dest

    placed = _place_file_in_archive_dir(
        src,
        dest_dir,
        dest.name,
        delete_source=delete_source,
    )
    if isinstance(placed, dict):
        return placed

    # Defense in depth: resolved archive path must remain under the category root.
    if not path_is_within(placed, category_root):
        try:
            placed.unlink(missing_ok=True)
        except OSError:
            pass
        return {
            "status": "error",
            "error": "archive path resolved outside the category folder",
            "code": "path_escape",
        }

    return {
        "status": "success",
        "archive_path": str(placed.resolve()),
        "filename": placed.name,
        "doc_type": safe_type,
        "year": year_part,
        "category_folder": str(category_root.resolve()),
    }


def _unique_inbox_destination(inbox: Path, safe_name: str) -> Path:
    """Pick inbox/safe_name, or inbox/stem_N.suffix if that name is taken."""
    dest = inbox / safe_name
    if not dest.exists():
        return dest
    stem = dest.stem
    suffix = dest.suffix
    n = 2
    while True:
        candidate = inbox / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def save_upload_to_inbox(filename: str, content: bytes) -> dict[str, Any]:
    """Persist an already-buffered upload into the configured source folder."""
    ensure_data_dirs()
    inbox = get_source_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    safe_name = Path(filename).name
    if not safe_name or safe_name in {".", ".."}:
        return {"status": "error", "error": "invalid filename"}
    if Path(safe_name).suffix.lower() not in SUPPORTED_SUFFIXES:
        return {
            "status": "error",
            "error": f"unsupported file type: {Path(safe_name).suffix.lower() or 'none'}",
            "supported": sorted(SUPPORTED_SUFFIXES),
        }
    dest = _unique_inbox_destination(inbox, safe_name)
    if not dest.resolve().is_relative_to(inbox.resolve()):
        return {"status": "error", "error": "upload path escapes inbox"}
    partial = inbox / f".{dest.name}.{uuid.uuid4().hex}.part"
    try:
        partial.write_bytes(content)
        if not partial.resolve().is_relative_to(inbox.resolve()):
            partial.unlink(missing_ok=True)
            return {"status": "error", "error": "upload path escapes inbox"}
        try:
            media = validate_scan_file(partial, suffix=dest.suffix)
        except MediaValidationError as exc:
            partial.unlink(missing_ok=True)
            return {"status": "error", "error": str(exc), "code": exc.code}
        os.replace(str(partial), str(dest))
    except OSError as exc:
        partial.unlink(missing_ok=True)
        return {"status": "error", "error": f"could not save file: {exc}"}
    return {
        "status": "success",
        "path": str(dest.resolve()),
        "filename": dest.name,
        "source_dir": str(inbox),
        "bytes": len(content),
        "media": media,
    }


def copy_local_scan_to_inbox(source: Path, *, max_bytes: int) -> dict[str, Any]:
    """Copy a local file (file-manager drop) into the inbox after validation."""
    ensure_data_dirs()
    try:
        src = Path(source).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        return {"status": "error", "error": f"invalid path: {exc}", "code": "invalid_path"}
    if not src.is_file():
        return {"status": "error", "error": "not a file", "code": "not_a_file"}
    suffix = src.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        return {
            "status": "error",
            "error": f"unsupported file type: {suffix or 'none'}",
            "code": "unsupported",
            "supported": sorted(SUPPORTED_SUFFIXES),
        }
    try:
        size = src.stat().st_size
    except OSError as exc:
        return {"status": "error", "error": f"could not read file: {exc}"}
    if size == 0:
        return {"status": "error", "error": "empty file", "code": "empty"}
    if max_bytes <= 0 or size > max_bytes:
        return {
            "status": "error",
            "error": f"file too large (max {max(0, max_bytes) // (1024 * 1024)} MB)",
            "code": "too_large",
        }
    inbox = get_source_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    if path_is_within(src, inbox):
        return {
            "status": "success",
            "path": str(src),
            "filename": src.name,
            "source_dir": str(inbox),
            "bytes": size,
            "already_in_inbox": True,
        }
    dest = _unique_inbox_destination(inbox, src.name)
    if not dest.resolve().is_relative_to(inbox.resolve()):
        return {"status": "error", "error": "upload path escapes inbox"}
    partial = inbox / f".{dest.name}.{uuid.uuid4().hex}.part"
    try:
        shutil.copyfile(str(src), str(partial))
        if not partial.resolve().is_relative_to(inbox.resolve()):
            partial.unlink(missing_ok=True)
            return {"status": "error", "error": "upload path escapes inbox"}
        try:
            media = validate_scan_file(partial, suffix=dest.suffix)
        except MediaValidationError as exc:
            partial.unlink(missing_ok=True)
            return {"status": "error", "error": str(exc), "code": exc.code}
        os.replace(str(partial), str(dest))
    except OSError as exc:
        partial.unlink(missing_ok=True)
        return {"status": "error", "error": f"could not save file: {exc}"}
    harden_app_owned_file(dest)
    return {
        "status": "success",
        "path": str(dest.resolve()),
        "filename": dest.name,
        "source_dir": str(inbox),
        "bytes": size,
        "media": media,
    }


async def stream_upload_to_inbox(
    filename: str,
    upload: Any,
    *,
    max_bytes: int,
    chunk_size: int = UPLOAD_CHUNK_BYTES,
) -> dict[str, Any]:
    """
    Stream an UploadFile-like object into the inbox without buffering it in RAM.

    Writes chunks to a temporary ``.part`` file, aborts as soon as ``max_bytes``
    is exceeded, then atomically renames into place on success.
    ``upload`` must provide ``async def read(size: int) -> bytes``.
    """
    ensure_data_dirs()
    inbox = get_source_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    safe_name = Path(filename).name
    if not safe_name or safe_name in {".", ".."}:
        return {"status": "error", "error": "invalid filename"}
    if Path(safe_name).suffix.lower() not in SUPPORTED_SUFFIXES:
        return {
            "status": "error",
            "error": f"unsupported file type: {Path(safe_name).suffix.lower() or 'none'}",
            "supported": sorted(SUPPORTED_SUFFIXES),
        }
    if max_bytes <= 0:
        return {"status": "error", "error": "invalid max_bytes", "code": "too_large"}

    dest = _unique_inbox_destination(inbox, safe_name)
    if not dest.resolve().is_relative_to(inbox.resolve()):
        return {"status": "error", "error": "upload path escapes inbox"}

    partial = inbox / f".{dest.name}.{uuid.uuid4().hex}.part"
    size = 0
    exceeded = False
    try:
        with open_private_file(partial, binary=True) as out:
            while True:
                chunk = await upload.read(chunk_size)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    exceeded = True
                    break
                out.write(chunk)

        if exceeded:
            partial.unlink(missing_ok=True)
            return {
                "status": "error",
                "error": (f"file too large (max {max_bytes // (1024 * 1024)} MB)"),
                "code": "too_large",
                "bytes_received": size,
            }

        if size == 0:
            partial.unlink(missing_ok=True)
            return {"status": "error", "error": "empty file", "code": "empty"}

        if not partial.resolve().is_relative_to(inbox.resolve()):
            partial.unlink(missing_ok=True)
            return {"status": "error", "error": "upload path escapes inbox"}

        try:
            media = validate_scan_file(partial, suffix=dest.suffix)
        except MediaValidationError as exc:
            partial.unlink(missing_ok=True)
            return {"status": "error", "error": str(exc), "code": exc.code}

        os.replace(str(partial), str(dest))
    except OSError as exc:
        partial.unlink(missing_ok=True)
        return {"status": "error", "error": f"could not save file: {exc}"}
    except Exception:
        partial.unlink(missing_ok=True)
        raise

    harden_app_owned_file(dest)
    return {
        "status": "success",
        "path": str(dest.resolve()),
        "filename": dest.name,
        "source_dir": str(inbox),
        "bytes": size,
        "media": media,
    }
