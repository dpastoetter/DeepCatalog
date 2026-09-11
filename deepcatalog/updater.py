"""Self-update: check GitHub for a newer release and install it in place."""

from __future__ import annotations

import hashlib
import hmac
import io
import logging
import os
import re
import shutil
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from deepcatalog import config
from deepcatalog.config import running_as_appimage
from deepcatalog.release_trust import (
    MANIFEST_NAME,
    MANIFEST_SIG_NAME,
    artifact_sha256,
    update_repo,
    verify_manifest_signature,
)
from deepcatalog.version import get_current_version

logger = logging.getLogger(__name__)

_SHA256_LINE_RE = re.compile(r"^\s*([A-Fa-f0-9]{64})\s+\*?(.+?)\s*$")
_SUMS_NAMES = frozenset({"SHA256SUMS", "SHA256SUMS.txt", "checksums.txt"})
_NON_ARCHIVE_NAMES = _SUMS_NAMES | {MANIFEST_NAME, MANIFEST_SIG_NAME}
GITHUB_API = "https://api.github.com"
GITHUB_CONNECT_TIMEOUT = 12.0
GITHUB_READ_TIMEOUT = 45.0
GITHUB_DOWNLOAD_READ_TIMEOUT = 180.0
GITHUB_MAX_ATTEMPTS = 4
_RETRYABLE_HTTP_STATUS = frozenset({429, 502, 503, 504})
_RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)

# Never overwritten by an update: user data, credentials, environments.
# Matched case-insensitively so a tarball cannot sneak past with Data/ or .ENV.
PROTECTED_TOP_LEVEL = {"data", ".env", ".venv", "venv", ".git", "node_modules"}


def parse_version(value: str) -> tuple[int, ...]:
    """'v1.2.3' → (1, 2, 3); non-numeric parts are ignored."""
    numbers = re.findall(r"\d+", value or "")
    return tuple(int(n) for n in numbers) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _github_headers(*, accept: str = "application/vnd.github+json") -> dict[str, str]:
    return {
        "Accept": accept,
        "User-Agent": f"DeepCatalog/{get_current_version()}",
    }


def _github_timeout(*, read: float | None = None) -> httpx.Timeout:
    return httpx.Timeout(
        connect=GITHUB_CONNECT_TIMEOUT,
        read=read if read is not None else GITHUB_READ_TIMEOUT,
        write=30.0,
        pool=10.0,
    )


def _github_get(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """GET with retries for flaky GitHub / CDN connections."""
    merged = {**_github_headers(), **(headers or {})}
    last_exc: Exception | None = None
    for attempt in range(GITHUB_MAX_ATTEMPTS):
        try:
            resp = client.get(url, headers=merged, params=params)
            if resp.status_code in _RETRYABLE_HTTP_STATUS and attempt < GITHUB_MAX_ATTEMPTS - 1:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                logger.warning(
                    "GitHub GET %s returned %s; retrying in %.1fs (attempt %s/%s)",
                    url,
                    resp.status_code,
                    delay,
                    attempt + 1,
                    GITHUB_MAX_ATTEMPTS,
                )
                time.sleep(delay)
                continue
            return resp
        except _RETRYABLE_EXCEPTIONS as exc:
            last_exc = exc
            if attempt >= GITHUB_MAX_ATTEMPTS - 1:
                raise
            delay = 2**attempt
            logger.warning(
                "GitHub GET %s failed (%s); retrying in %.1fs (attempt %s/%s)",
                url,
                exc,
                delay,
                attempt + 1,
                GITHUB_MAX_ATTEMPTS,
            )
            time.sleep(delay)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("GitHub GET failed without a response")


def parse_sha256sums(text: str) -> dict[str, str]:
    """Parse GNU sha256sum output into `{filename: hex digest}`."""
    mapping: dict[str, str] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _SHA256_LINE_RE.match(line)
        if not match:
            continue
        digest, filename = match.group(1).lower(), match.group(2).strip()
        # Also index by basename so "dist/foo.tar.gz" matches "foo.tar.gz".
        mapping[filename] = digest
        mapping[Path(filename).name] = digest
    return mapping


def _pick_archive_asset(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    archives = [
        asset
        for asset in assets
        if isinstance(asset.get("name"), str)
        and asset["name"] not in _NON_ARCHIVE_NAMES
        and asset["name"].lower().endswith((".tar.gz", ".tgz"))
    ]
    if not archives:
        return None
    preferred = [asset for asset in archives if asset["name"].lower().startswith("deepcatalog-")]
    return preferred[0] if preferred else archives[0]


def _pick_appimage_asset(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    images = [
        asset
        for asset in assets
        if isinstance(asset.get("name"), str) and asset["name"].lower().endswith(".appimage")
    ]
    if not images:
        return None
    preferred = [
        asset
        for asset in images
        if "x86_64" in asset["name"].lower() or "x86-64" in asset["name"].lower()
    ]
    return preferred[0] if preferred else images[0]


def _resolve_commit_sha(client: httpx.Client, tag: str) -> str | None:
    if not tag:
        return None
    resp = _github_get(client, f"{GITHUB_API}/repos/{update_repo()}/commits/{tag}")
    if resp.status_code != 200:
        return None
    sha = (resp.json() or {}).get("sha")
    return sha if isinstance(sha, str) and sha else None


def _asset_url(asset: dict[str, Any]) -> str | None:
    url = asset.get("browser_download_url") or asset.get("url")
    return url if isinstance(url, str) and url else None


def _pick_named_asset(assets: list[dict[str, Any]], filename: str) -> dict[str, Any] | None:
    for asset in assets:
        if isinstance(asset, dict) and asset.get("name") == filename:
            return asset
    return None


def _download_asset_bytes(client: httpx.Client, asset: dict[str, Any]) -> bytes | None:
    url = _asset_url(asset)
    if not url:
        return None
    resp = _github_get(client, url, headers=_github_headers(accept="application/octet-stream"))
    if not resp.is_success:
        return None
    return resp.content


def _unsigned_release_error() -> str:
    return (
        "Latest release is missing a signed release-manifest.json "
        f"(and {MANIFEST_SIG_NAME}) bound to the tag and commit. "
        "Checksum files alone are not authentic."
    )


def _select_signed_artifact(
    release: dict[str, Any],
    *,
    manifest: dict[str, Any],
) -> dict[str, Any] | None:
    """Choose the source archive whose SHA-256 is listed in the signed manifest."""
    assets = [
        asset
        for asset in (release.get("assets") or [])
        if isinstance(asset, dict) and isinstance(asset.get("name"), str)
    ]
    archive = _pick_archive_asset(assets)
    if archive is None:
        return None
    expected = artifact_sha256(manifest, archive["name"])
    if not expected:
        return None
    url = _asset_url(archive)
    if not url:
        return None
    return {
        "filename": archive["name"],
        "download_url": url,
        "expected_sha256": expected,
        "source": "signed-manifest",
        "kind": "tarball",
    }


def _select_signed_appimage_artifact(
    release: dict[str, Any],
    *,
    manifest: dict[str, Any],
) -> dict[str, Any] | None:
    """Choose the AppImage whose SHA-256 is listed in the signed manifest."""
    assets = [
        asset
        for asset in (release.get("assets") or [])
        if isinstance(asset, dict) and isinstance(asset.get("name"), str)
    ]
    image = _pick_appimage_asset(assets)
    if image is None:
        return None
    expected = artifact_sha256(manifest, image["name"])
    if not expected:
        return None
    url = _asset_url(image)
    if not url:
        return None
    return {
        "filename": image["name"],
        "download_url": url,
        "expected_sha256": expected,
        "source": "signed-manifest",
        "kind": "appimage",
    }


def _load_signed_manifest(
    client: httpx.Client, assets: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, str | None]:
    """Return (manifest, error). Manifest is None when signature/schema fails."""
    manifest_asset = _pick_named_asset(assets, MANIFEST_NAME)
    sig_asset = _pick_named_asset(assets, MANIFEST_SIG_NAME)
    if manifest_asset is None or sig_asset is None:
        return None, _unsigned_release_error()
    manifest_bytes = _download_asset_bytes(client, manifest_asset)
    sig_bytes = _download_asset_bytes(client, sig_asset)
    if manifest_bytes is None or sig_bytes is None:
        return None, "Could not download the signed release manifest."
    try:
        signature_text = sig_bytes.decode("ascii")
    except UnicodeDecodeError:
        return None, "Release manifest signature is not valid hex."
    try:
        manifest = verify_manifest_signature(manifest_bytes, signature_text)
    except ValueError as exc:
        return None, str(exc)
    return manifest, None


def _fetch_latest_release() -> dict[str, Any] | None:
    """Latest GitHub release with a signed manifest (checksum-only releases are not installable)."""
    repo = update_repo()
    with httpx.Client(
        timeout=_github_timeout(),
        follow_redirects=True,
    ) as client:
        resp = _github_get(client, f"{GITHUB_API}/repos/{repo}/releases/latest")
        if resp.status_code == 404:
            # No releases published — surface tags for "what's newest" only.
            resp = _github_get(
                client,
                f"{GITHUB_API}/repos/{repo}/tags",
                params={"per_page": 1},
            )
            resp.raise_for_status()
            tags = resp.json()
            if not tags:
                return None
            tag = tags[0].get("name") or ""
            commit_sha = _resolve_commit_sha(client, tag)
            return {
                "tag": tag,
                "name": tag,
                "notes": "",
                "published_at": None,
                "html_url": f"https://github.com/{repo}/releases",
                "tarball_url": f"{GITHUB_API}/repos/{repo}/tarball/{tag}",
                "commit_sha": commit_sha,
                "assets": [],
                "verifiable": False,
                "signed": False,
                "artifact": None,
                "appimage_artifact": None,
                "verification_error": (
                    "No GitHub release with a signed release manifest. "
                    "Tag-only installs are disabled."
                ),
            }
        resp.raise_for_status()
        data = resp.json()
        tag = data.get("tag_name") or ""
        assets = data.get("assets") or []
        if not isinstance(assets, list):
            assets = []
        typed_assets = [asset for asset in assets if isinstance(asset, dict)]

        commit_sha = _resolve_commit_sha(client, tag)
        release = {
            "tag": tag,
            "name": data.get("name") or tag,
            "notes": (data.get("body") or "")[:2000],
            "published_at": data.get("published_at"),
            "html_url": data.get("html_url"),
            "tarball_url": data.get("tarball_url"),
            "commit_sha": commit_sha,
            "assets": assets,
            "signed": False,
            "manifest_commit": None,
        }
        manifest, manifest_error = _load_signed_manifest(client, typed_assets)
        if manifest is None:
            release["artifact"] = None
            release["appimage_artifact"] = None
            release["verifiable"] = False
            release["verification_error"] = manifest_error or _unsigned_release_error()
            return release

        bind_error: str | None = None
        if manifest["repo"] != repo:
            bind_error = "Signed manifest repo does not match this build's update trust root."
        elif manifest["tag"] != tag:
            bind_error = "Signed manifest tag does not match the GitHub release tag."
        elif not commit_sha or manifest["commit"] != commit_sha.lower():
            bind_error = "Signed manifest commit does not match the release tag commit."
        if bind_error:
            release["artifact"] = None
            release["appimage_artifact"] = None
            release["verifiable"] = False
            release["verification_error"] = bind_error
            return release

        artifact = _select_signed_artifact(release, manifest=manifest)
        appimage_artifact = _select_signed_appimage_artifact(release, manifest=manifest)
        release["artifact"] = artifact
        release["appimage_artifact"] = appimage_artifact
        release["signed"] = True
        release["manifest_commit"] = manifest["commit"]
        release["verifiable"] = artifact is not None or appimage_artifact is not None
        if artifact is not None or appimage_artifact is not None:
            release["verification_error"] = None
        else:
            release["verification_error"] = (
                "Signed manifest does not list a SHA-256 for the release "
                ".tar.gz or .AppImage asset."
            )
        return release


def appimage_update_target() -> Path | None:
    """Writable path of the running AppImage file, if self-replace is possible."""
    raw = os.getenv("APPIMAGE", "").strip()
    if not raw:
        return None
    path = Path(raw)
    try:
        if not path.is_file():
            return None
        parent = path.parent
        if not os.access(parent, os.W_OK | os.X_OK):
            return None
        # Need to create a sibling temp file and replace the existing image.
        if path.exists() and not os.access(path, os.W_OK):
            # Still OK if we can unlink+replace via directory write bits.
            if not os.access(parent, os.W_OK):
                return None
        return path.resolve()
    except OSError:
        return None


def check_for_update() -> dict[str, Any]:
    """Compare the installed version against the latest GitHub release."""
    current = get_current_version()
    is_appimage = running_as_appimage()
    base = {
        "status": "success",
        "repo": update_repo(),
        "current_version": current,
        "update_available": False,
        "verifiable": False,
        "signed": False,
        "installable": False,
        "appimage": is_appimage,
    }
    try:
        latest = _fetch_latest_release()
    except httpx.HTTPError:
        logger.warning("could not reach GitHub for update check")
        return {
            "status": "error",
            "repo": update_repo(),
            "current_version": current,
            "error": "Could not reach GitHub. Check your network and try again.",
        }
    if latest is None:
        return {**base, "message": "No releases or tags published on GitHub yet."}

    tarball = latest.get("artifact") or {}
    appimage_art = latest.get("appimage_artifact") or {}
    if not isinstance(tarball, dict):
        tarball = {}
    if not isinstance(appimage_art, dict):
        appimage_art = {}

    raw_assets = latest.get("assets") or []
    assets = raw_assets if isinstance(raw_assets, list) else []
    appimage = _pick_appimage_asset(assets)
    appimage_url = None
    appimage_name = None
    if appimage is not None:
        appimage_name = appimage.get("name")
        url = appimage.get("browser_download_url") or appimage.get("url")
        appimage_url = url if isinstance(url, str) and url else None

    if is_appimage:
        active = appimage_art
        target = appimage_update_target()
        installable = bool(
            latest.get("signed")
            and active.get("download_url")
            and active.get("expected_sha256")
            and target is not None
        )
        verification_error = latest.get("verification_error")
        if latest.get("signed") and not active.get("download_url"):
            verification_error = "Signed manifest does not list a SHA-256 for the release AppImage."
        elif latest.get("signed") and target is None:
            verification_error = (
                "AppImage path is missing or not writable — cannot replace this file "
                "in place. Download the new AppImage manually from GitHub Releases."
            )
        verifiable = bool(active.get("download_url") and active.get("expected_sha256"))
    else:
        active = tarball
        installable = bool(
            latest.get("signed")
            and active.get("download_url")
            and active.get("expected_sha256")
            and latest.get("manifest_commit")
        )
        verification_error = latest.get("verification_error")
        verifiable = bool(latest.get("verifiable") and active.get("download_url"))

    return {
        **base,
        "latest_version": latest["tag"].lstrip("v"),
        "latest_tag": latest["tag"],
        "release_name": latest["name"],
        "notes": latest["notes"],
        "published_at": latest["published_at"],
        "html_url": latest["html_url"],
        "tarball_url": latest.get("tarball_url"),
        "commit_sha": latest.get("commit_sha"),
        "manifest_commit": latest.get("manifest_commit"),
        "update_available": is_newer(latest["tag"], current),
        "verifiable": verifiable,
        "signed": bool(latest.get("signed")),
        "verification_error": verification_error,
        "installable": installable,
        "artifact_name": active.get("filename"),
        "expected_sha256": active.get("expected_sha256"),
        "download_url": active.get("download_url"),
        "artifact_kind": active.get("kind") or ("appimage" if is_appimage else "tarball"),
        "appimage_name": appimage_name or appimage_art.get("filename"),
        "appimage_url": appimage_url or appimage_art.get("download_url"),
        "appimage_target": str(appimage_update_target()) if is_appimage else None,
    }


def _download_bytes(url: str) -> bytes:
    with httpx.Client(
        timeout=_github_timeout(read=GITHUB_DOWNLOAD_READ_TIMEOUT),
        follow_redirects=True,
    ) as client:
        resp = _github_get(
            client,
            url,
            headers=_github_headers(accept="application/octet-stream"),
        )
        resp.raise_for_status()
        return resp.content


def verify_sha256(data: bytes, expected_hex: str) -> None:
    """Raise ValueError when the payload does not match the expected digest."""
    expected = (expected_hex or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ValueError("invalid expected SHA-256 digest")
    actual = sha256_hex(data)
    if not hmac.compare_digest(actual, expected):
        raise ValueError(
            f"SHA-256 mismatch (expected {expected[:12]}…, got {actual[:12]}…) — update aborted"
        )


def _is_protected(relative: Path) -> bool:
    parts = relative.parts
    if not parts:
        return True
    if parts[0].lower() in PROTECTED_TOP_LEVEL:
        return True
    # Never clobber local env files anywhere in the tree.
    return relative.name.lower() == ".env"


def _root_matches_commit(source_root: Path, commit_sha: str | None) -> bool:
    """GitHub source archives unpack to `{owner}-{repo}-{fullsha}/`."""
    if not commit_sha:
        return True
    name = source_root.name.lower()
    sha = commit_sha.lower()
    return name.endswith(sha) or name.endswith(sha[:12]) or name.endswith(sha[:7])


def _read_release_file_list(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    return {line.strip() for line in lines if line.strip() and not line.strip().startswith("#")}


def _parse_release_commit_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("commit="):
            sha = line.split("=", 1)[1].strip().lower()
            if re.fullmatch(r"[a-f0-9]{40}", sha):
                return sha
    return None


def apply_tarball(
    tar_bytes: bytes,
    *,
    commit_sha: str | None = None,
    expect_commit_match: bool = False,
    expected_release_commit: str | None = None,
) -> dict[str, Any]:
    """
    Extract a release/source tarball and sync it over the install directory.

    User data (data/), credentials (.env), virtualenvs, and .git are untouched.
    Destinations that are symlinks (or would resolve outside PROJECT_ROOT) are
    skipped so a malicious archive cannot write through a symlink escape.

    When `.release-files` is present (current and/or previous install), files
    that were part of the previous release but absent from the new archive are
    removed so upgrades cannot leave stale modules behind.
    """
    root = Path(config.PROJECT_ROOT).resolve()
    updated: list[str] = []
    removed: list[str] = []
    with tempfile.TemporaryDirectory(prefix="deepcatalog-update-") as tmp:
        tmp_path = Path(tmp)
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tar:
            tar.extractall(tmp_path, filter="data")

        # GitHub / release tarballs wrap everything in a single root directory.
        entries = [p for p in tmp_path.iterdir() if p.is_dir()]
        if len(entries) != 1:
            return {"status": "error", "error": "unexpected tarball layout"}
        source_root = entries[0]

        if expect_commit_match and not _root_matches_commit(source_root, commit_sha):
            return {
                "status": "error",
                "error": (
                    f"Archive root '{source_root.name}' does not match release "
                    f"commit {commit_sha} — update aborted"
                ),
            }

        if expected_release_commit:
            packed = _parse_release_commit_file(source_root / ".release-commit")
            want = expected_release_commit.strip().lower()
            if not packed or packed != want:
                return {
                    "status": "error",
                    "error": (
                        "Archive .release-commit does not match the signed manifest "
                        "commit — update aborted"
                    ),
                }

        previous_files = _read_release_file_list(root / ".release-files")
        new_files: set[str] = set()

        for path in sorted(source_root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(source_root)
            if _is_protected(relative):
                continue
            dest = root / relative
            # Refuse to write through an existing symlink (could point outside).
            if dest.is_symlink() or any(parent.is_symlink() for parent in dest.parents):
                logger.warning("Skipping symlink destination during update: %s", relative)
                continue
            try:
                resolved = dest.resolve()
                if not resolved.is_relative_to(root):
                    logger.warning("Skipping path that escapes project root: %s", relative)
                    continue
            except OSError:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            rel_s = relative.as_posix()
            updated.append(rel_s)
            if rel_s not in {".release-commit", ".release-files"}:
                new_files.add(rel_s)

        archive_manifest = _read_release_file_list(source_root / ".release-files")
        if archive_manifest:
            new_files |= archive_manifest

        # After the first manifest-aware install, prune paths that disappeared.
        if previous_files:
            for rel_s in sorted(previous_files - new_files):
                relative = Path(rel_s)
                if _is_protected(relative):
                    continue
                dest = root / relative
                if dest.is_symlink():
                    continue
                if not dest.is_file():
                    continue
                try:
                    resolved = dest.resolve()
                    if not resolved.is_relative_to(root):
                        continue
                except OSError:
                    continue
                dest.unlink()
                removed.append(rel_s)
                parent = dest.parent
                while parent != root and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent

    return {
        "status": "success",
        "updated_count": len(updated),
        "updated": updated,
        "removed_count": len(removed),
        "removed": removed,
    }


def apply_appimage_bytes(image_bytes: bytes, *, target: Path) -> dict[str, Any]:
    """
    Replace the on-disk AppImage at ``target`` with ``image_bytes``.

    Writes a sibling temp file then ``os.replace`` so the running process can
    keep its mounted squashfs until relaunch.
    """
    parent = target.parent
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".new",
        dir=str(parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(image_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.chmod(0o755)
        os.replace(tmp_path, target)
    except OSError as exc:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        return {
            "status": "error",
            "error": f"Could not replace AppImage at {target}: {exc}",
        }
    return {
        "status": "success",
        "updated_count": 1,
        "updated": [str(target)],
        "removed_count": 0,
        "removed": [],
        "appimage_path": str(target),
    }


def apply_update() -> dict[str, Any]:
    """Download the latest verified release and install it over the current version."""
    info = check_for_update()
    if info.get("status") != "success":
        return info
    if not info.get("update_available"):
        return {
            "status": "error",
            "error": f"Already up to date (v{info['current_version']}).",
        }
    if not info.get("installable"):
        return {
            "status": "error",
            "installable": False,
            "error": info.get("verification_error")
            or (
                "This install cannot be updated in place. "
                "Download the latest release from GitHub and replace this install."
            ),
        }
    if (
        not info.get("verifiable")
        or not info.get("signed")
        or not info.get("expected_sha256")
        or not info.get("download_url")
    ):
        return {
            "status": "error",
            "error": info.get("verification_error")
            or "Refusing to install an unsigned release (checksums are not authentic).",
        }

    kind = info.get("artifact_kind") or "tarball"
    if kind == "appimage" or running_as_appimage():
        target = appimage_update_target()
        if target is None:
            return {
                "status": "error",
                "installable": False,
                "error": (
                    "AppImage path is missing or not writable — cannot replace this file in place."
                ),
            }
        try:
            image_bytes = _download_bytes(info["download_url"])
        except httpx.HTTPError as exc:
            logger.warning("AppImage update download failed: %s", type(exc).__name__)
            return {
                "status": "error",
                "error": "Download failed. Check your network and try again.",
            }
        try:
            verify_sha256(image_bytes, info["expected_sha256"])
        except ValueError:
            logger.exception("AppImage SHA-256 verification failed")
            return {"status": "error", "error": "Release verification failed (SHA-256 mismatch)"}
        result = apply_appimage_bytes(image_bytes, target=target)
        if result.get("status") != "success":
            return result
        return {
            **result,
            "installed_version": info.get("latest_version"),
            "previous_version": info["current_version"],
            "verified_sha256": info["expected_sha256"],
            "verified_commit": info.get("manifest_commit"),
            "artifact_name": info.get("artifact_name"),
            "artifact_kind": "appimage",
            "restart_required": True,
        }

    if not info.get("manifest_commit"):
        return {
            "status": "error",
            "error": info.get("verification_error")
            or "Refusing to install an unsigned release (checksums are not authentic).",
        }

    try:
        tar_bytes = _download_bytes(info["download_url"])
    except httpx.HTTPError as exc:
        logger.warning("update download failed: %s", type(exc).__name__)
        return {"status": "error", "error": "Download failed. Check your network and try again."}

    try:
        verify_sha256(tar_bytes, info["expected_sha256"])
    except ValueError:
        logger.exception("update SHA-256 verification failed")
        return {"status": "error", "error": "Release verification failed (SHA-256 mismatch)"}

    # Integrity: Ed25519 manifest + SHA-256 of these bytes + packed .release-commit.
    result = apply_tarball(tar_bytes, expected_release_commit=str(info["manifest_commit"]))
    if result.get("status") != "success":
        return result

    return {
        **result,
        "installed_version": info.get("latest_version"),
        "previous_version": info["current_version"],
        "verified_sha256": info["expected_sha256"],
        "verified_commit": info.get("manifest_commit"),
        "artifact_name": info.get("artifact_name"),
        "artifact_kind": "tarball",
        "restart_required": True,
    }


def schedule_restart(delay_seconds: float = 0.75) -> dict[str, Any]:
    """
    Restart the server process in-place after a short delay.

    AppImage builds re-exec the outer ``$APPIMAGE`` file so the new squashfs
    is loaded. Source installs re-exec ``sys.executable`` with the original
    argv (works for ``uvicorn …`` and ``python -m …``).
    """
    if running_as_appimage():
        image = appimage_update_target()
        if image is None:
            raw = os.getenv("APPIMAGE", "").strip()
            image = Path(raw) if raw else None
        if image is None or not image.is_file():
            return {
                "status": "error",
                "error": "Cannot restart: AppImage path is missing after update.",
            }
        argv = [str(image), *sys.argv[1:]]

        def _restart_appimage() -> None:
            logger.info("Restarting AppImage: %s", " ".join(argv))
            os.execv(str(image), argv)  # noqa: S606

        timer = threading.Timer(delay_seconds, _restart_appimage)
        timer.daemon = True
        timer.start()
        return {"status": "success", "message": "Restarting…", "command": argv}

    argv = [sys.executable, *sys.argv]

    def _restart() -> None:
        logger.info("Restarting: %s", " ".join(argv))
        os.execv(sys.executable, argv)  # noqa: S606

    timer = threading.Timer(delay_seconds, _restart)
    timer.daemon = True
    timer.start()
    return {"status": "success", "message": "Restarting…", "command": argv}
