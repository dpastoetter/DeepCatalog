"""User privacy acknowledgements (cloud LLM processing disclaimer)."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepcatalog import config
from deepcatalog.env_permissions import write_secret_text

# Bump when the disclaimer text changes so users must re-approve.
CLOUD_DISCLAIMER_VERSION = "1"

_FILENAME = "privacy.json"
_lock = threading.RLock()
_cache: dict[str, Any] | None = None


def privacy_path() -> Path:
    return config.DATA_DIR / _FILENAME


def _default() -> dict[str, Any]:
    return {
        "cloud_disclaimer_version": None,
        "cloud_disclaimer_accepted_at": None,
    }


def _read() -> dict[str, Any]:
    path = privacy_path()
    if not path.exists():
        return _default()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default()
    if not isinstance(data, dict):
        return _default()
    base = _default()
    base.update(data)
    return base


def _write(data: dict[str, Any]) -> None:
    config.ensure_data_dirs()
    write_secret_text(privacy_path(), json.dumps(data, indent=2, sort_keys=True))


def load_privacy(*, reload: bool = False) -> dict[str, Any]:
    global _cache
    with _lock:
        if _cache is not None and not reload:
            return dict(_cache)
        _cache = _read()
        return dict(_cache)


def clear_privacy_cache() -> None:
    global _cache
    with _lock:
        _cache = None


def is_cloud_disclaimer_accepted() -> bool:
    data = load_privacy()
    return data.get("cloud_disclaimer_version") == CLOUD_DISCLAIMER_VERSION and bool(
        data.get("cloud_disclaimer_accepted_at")
    )


def cloud_disclaimer_status() -> dict[str, Any]:
    data = load_privacy()
    accepted = is_cloud_disclaimer_accepted()
    return {
        "version": CLOUD_DISCLAIMER_VERSION,
        "accepted": accepted,
        "accepted_at": data.get("cloud_disclaimer_accepted_at") if accepted else None,
    }


def accept_cloud_disclaimer() -> dict[str, Any]:
    """Record that the user approved sending documents to a cloud LLM provider."""
    global _cache
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with _lock:
        data = _read()
        data["cloud_disclaimer_version"] = CLOUD_DISCLAIMER_VERSION
        data["cloud_disclaimer_accepted_at"] = now
        _write(data)
        _cache = data
    return cloud_disclaimer_status()


def revoke_cloud_disclaimer() -> dict[str, Any]:
    """Clear the cloud-processing acknowledgement."""
    global _cache
    with _lock:
        data = _read()
        data["cloud_disclaimer_version"] = None
        data["cloud_disclaimer_accepted_at"] = None
        _write(data)
        _cache = data
    return cloud_disclaimer_status()


def require_cloud_disclaimer() -> None:
    """Raise PermissionError when cloud auth/provider actions are blocked."""
    if not is_cloud_disclaimer_accepted():
        raise PermissionError(
            "Approve the cloud processing disclaimer in Settings → AI provider "
            "before signing in or saving a cloud API key."
        )
