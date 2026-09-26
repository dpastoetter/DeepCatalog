"""Restrictive permissions for DeepCatalog-owned secrets and data directories.

Application-owned storage under ``DATA_DIR`` must not rely on the process umask:
directories are ``0700`` and sensitive files are ``0600``. User-configured inbox
or category folders *outside* ``DATA_DIR`` are created if missing but never
chmod'd (and never walked recursively).
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Any

from deepcatalog import config

logger = logging.getLogger(__name__)

# Owner read/write only — same posture as ~/.codex/auth.json.
SECRET_FILE_MODE = 0o600
# Owner-only directory for app-managed data trees.
PRIVATE_DIR_MODE = 0o700
# Any group/other read/write/execute bit is too open for secrets / private dirs.
_INSECURE_BITS = stat.S_IRWXG | stat.S_IRWXO


def is_posix() -> bool:
    return os.name == "posix"


def is_group_or_world_accessible(path: Path) -> bool:
    """True when group or other have any permission bits set."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return bool(mode & _INSECURE_BITS)


def path_is_under(path: Path, root: Path) -> bool:
    """True when ``path`` resolves inside ``root`` (inclusive)."""
    try:
        resolved = path.expanduser().resolve()
        base = root.expanduser().resolve()
    except OSError:
        return False
    return resolved == base or resolved.is_relative_to(base)


def chmod_secret_file(path: Path) -> None:
    """Best-effort ``chmod 0600`` (no-op / ignored where unsupported)."""
    if not is_posix():
        return
    try:
        os.chmod(path, SECRET_FILE_MODE)
    except OSError as exc:
        logger.warning("Could not set mode %04o on %s: %s", SECRET_FILE_MODE, path, exc)


def chmod_private_dir(path: Path) -> None:
    """Best-effort ``chmod 0700`` for an app-owned directory."""
    if not is_posix():
        return
    try:
        os.chmod(path, PRIVATE_DIR_MODE)
    except OSError as exc:
        logger.warning("Could not set mode %04o on %s: %s", PRIVATE_DIR_MODE, path, exc)


def ensure_private_directory(path: Path, *, under: Path | None = None) -> Path:
    """
    Create ``path`` if missing.

    When ``under`` is set, apply ``0700`` only to ``path`` and ancestors that
    resolve under that root (typically ``DATA_DIR``). Never recurses into
    children and never touches paths outside ``under``.
    """
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    if under is None:
        chmod_private_dir(target)
        return target
    try:
        under_res = Path(under).expanduser().resolve()
        resolved = target.resolve()
    except OSError:
        return target
    if not (resolved == under_res or resolved.is_relative_to(under_res)):
        return target
    cur = resolved
    while True:
        chmod_private_dir(cur)
        if cur == under_res:
            break
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return target


def write_secret_text(path: Path, text: str) -> None:
    """
    Atomically write ``text`` to ``path`` with mode ``0600``.

    Creates the temp file with restrictive permissions so a crash never leaves
    a world-readable secrets file behind.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path_is_under(path.parent, Path(config.DATA_DIR)):
        chmod_private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    # On Windows, the mode arg is masked; still set 0600 for POSIX.
    fd = os.open(str(tmp), flags, SECRET_FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            if text and not text.endswith("\n"):
                handle.write("\n")
        # Re-assert in case umask widened the mode.
        chmod_secret_file(tmp)
        os.replace(str(tmp), str(path))
        chmod_secret_file(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def open_private_file(path: Path, *, binary: bool = True):
    """
    Open ``path`` for writing with mode ``0600`` (umask-independent on POSIX).

    Caller owns closing the returned file object. Prefer this for new app-owned
    blobs under ``DATA_DIR`` (uploads, temp parts).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(path), flags, SECRET_FILE_MODE)
    mode = "wb" if binary else "w"
    handle = os.fdopen(fd, mode) if binary else os.fdopen(fd, mode, encoding="utf-8")
    try:
        chmod_secret_file(path)
    except OSError:
        pass
    return handle


def harden_secret_file(path: Path, *, fix: bool = True) -> dict[str, Any]:
    """
    Inspect ``path`` and optionally tighten permissions to ``0600``.

    Returns a status dict; never raises for permission probing failures.
    """
    target = Path(path)
    result: dict[str, Any] = {
        "path": str(target),
        "exists": target.is_file(),
        "was_insecure": False,
        "fixed": False,
        "mode": None,
    }
    if not target.is_file():
        return result
    if not is_posix():
        return result
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except OSError as exc:
        result["error"] = str(exc)
        return result
    result["mode"] = oct(mode)
    insecure = bool(mode & _INSECURE_BITS)
    result["was_insecure"] = insecure
    if insecure and fix:
        chmod_secret_file(target)
        try:
            new_mode = stat.S_IMODE(target.stat().st_mode)
            result["mode"] = oct(new_mode)
            result["fixed"] = not bool(new_mode & _INSECURE_BITS)
        except OSError as exc:
            result["error"] = str(exc)
    return result


def harden_app_owned_file(path: Path, *, data_root: Path | None = None) -> dict[str, Any]:
    """Tighten ``path`` to ``0600`` only when it lives under ``data_root`` (default DATA_DIR)."""
    root = Path(data_root) if data_root is not None else Path(config.DATA_DIR)
    target = Path(path)
    if not path_is_under(target, root):
        return {
            "path": str(target),
            "exists": target.is_file(),
            "skipped": True,
            "reason": "outside DATA_DIR",
        }
    return harden_secret_file(target, fix=True)


def candidate_env_paths() -> list[Path]:
    """Project and data-dir ``.env`` locations that may hold secrets."""
    paths = [config.PROJECT_ROOT / ".env", Path(config.DATA_DIR) / ".env"]
    # Preserve order, drop duplicates (e.g. DATA_DIR under PROJECT_ROOT).
    seen: set[Path] = set()
    out: list[Path] = []
    for path in paths:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def ensure_dotenv_permissions(*, fix: bool = True) -> list[dict[str, Any]]:
    """
    Check known ``.env`` files at startup; tighten and warn if world/group-readable.
    """
    reports: list[dict[str, Any]] = []
    for path in candidate_env_paths():
        report = harden_secret_file(path, fix=fix)
        reports.append(report)
        if report.get("was_insecure"):
            if report.get("fixed"):
                logger.warning(
                    "Tightened permissions on %s to 0600 (was group/world-accessible)",
                    report["path"],
                )
            else:
                logger.warning(
                    "Secret file %s is group/world-accessible (%s); "
                    "could not auto-fix — run: chmod 600 %s",
                    report["path"],
                    report.get("mode"),
                    report["path"],
                )
    return reports


def app_owned_data_dirs(data_root: Path | None = None) -> list[Path]:
    """Standard app-owned directory tree under ``DATA_DIR`` (safe to chmod 0700)."""
    root = Path(data_root) if data_root is not None else Path(config.DATA_DIR)
    root = root.expanduser()
    return [
        root,
        root / "inbox",
        root / "archive",
        root / "chroma",
    ]


def app_owned_sensitive_files(data_root: Path | None = None) -> list[Path]:
    """
    Known sensitive files under ``DATA_DIR`` that may safely be tightened to 0600.

    Does not recurse into Chroma or archive document trees.
    """
    root = Path(data_root) if data_root is not None else Path(config.DATA_DIR)
    root = root.expanduser()
    db = root / "deepcatalog.db"
    return [
        root / ".env",
        root / "sessions.json",
        root / "settings.json",
        root / "privacy.json",
        root / ".desktop-bootstrap",
        root / "chroma" / "index_meta.json",
        db,
        Path(str(db) + "-wal"),
        Path(str(db) + "-shm"),
    ]


def ensure_app_data_permissions(*, data_root: Path | None = None) -> dict[str, Any]:
    """
    Create and tighten the standard ``DATA_DIR`` layout (non-recursive).

    Safe for existing installs: only touches known directories and named files
    under ``DATA_DIR``. Never walks category folders configured outside that tree.
    """
    root = Path(data_root) if data_root is not None else Path(config.DATA_DIR)
    root = root.expanduser()
    dirs_ok: list[str] = []
    for directory in app_owned_data_dirs(root):
        ensure_private_directory(directory, under=root)
        dirs_ok.append(str(directory))
    files: list[dict[str, Any]] = []
    for path in app_owned_sensitive_files(root):
        if path.is_file():
            files.append(harden_app_owned_file(path, data_root=root))
    return {"data_dir": str(root), "directories": dirs_ok, "files": files}
