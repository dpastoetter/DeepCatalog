"""One-time desktop UI bootstrap nonce (in-memory + 0600 file).

The native window cannot present DEEPCATALOG_API_TOKEN to JavaScript. It
mints a short-lived nonce that only the same uid can read, then opens
``/api/auth/desktop-bootstrap/{nonce}`` which sets the HttpOnly session
cookie and redirects to the SPA.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from deepcatalog import config
from deepcatalog.env_permissions import write_secret_text
from deepcatalog.local_security import token_matches

logger = logging.getLogger(__name__)

BOOTSTRAP_FILENAME = ".desktop-bootstrap"
DEFAULT_TTL_SECONDS = 60

_lock = threading.Lock()
_memory_nonce: str | None = None
_memory_expires_at: float = 0.0


def bootstrap_path() -> Path:
    return Path(config.DATA_DIR).expanduser().resolve() / BOOTSTRAP_FILENAME


def _ttl_seconds() -> int:
    raw = os.getenv("DEEPCATALOG_DESKTOP_BOOTSTRAP_TTL", "").strip()
    if not raw:
        return DEFAULT_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_TTL_SECONDS
    return max(5, min(value, 300))


def mint_desktop_bootstrap(*, ttl_seconds: int | None = None) -> str:
    """Create a single-use nonce and persist it owner-only under DATA_DIR."""
    nonce = secrets.token_urlsafe(32)
    ttl = _ttl_seconds() if ttl_seconds is None else max(5, ttl_seconds)
    expires_at = time.time() + ttl
    payload = json.dumps({"nonce": nonce, "expires_at": expires_at}, separators=(",", ":"))
    with _lock:
        global _memory_nonce, _memory_expires_at
        _memory_nonce = nonce
        _memory_expires_at = expires_at
        write_secret_text(bootstrap_path(), payload)
    return nonce


def _clear_unlocked() -> None:
    global _memory_nonce, _memory_expires_at
    _memory_nonce = None
    _memory_expires_at = 0.0
    path = bootstrap_path()
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove desktop bootstrap file %s", path)


def _load_file_unlocked() -> dict[str, Any] | None:
    path = bootstrap_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    nonce = data.get("nonce")
    expires_at = data.get("expires_at")
    if not isinstance(nonce, str) or not nonce.strip():
        return None
    if expires_at is None:
        return None
    try:
        exp = float(expires_at)
    except (TypeError, ValueError):
        return None
    return {"nonce": nonce.strip(), "expires_at": exp}


def consume_desktop_bootstrap(candidate: str | None) -> bool:
    """True once when ``candidate`` matches the minted nonce before expiry."""
    if not candidate or not str(candidate).strip():
        return False
    now = time.time()
    with _lock:
        global _memory_nonce, _memory_expires_at
        expected: str | None = None
        if _memory_nonce and _memory_expires_at > now:
            expected = _memory_nonce
        else:
            stored = _load_file_unlocked()
            if stored and float(stored["expires_at"]) > now:
                expected = str(stored["nonce"])
            elif stored:
                _clear_unlocked()
        if not expected:
            return False
        if not token_matches(str(candidate).strip(), expected):
            return False
        _clear_unlocked()
        return True


def clear_desktop_bootstrap() -> None:
    """Drop any outstanding nonce (tests / shutdown)."""
    with _lock:
        _clear_unlocked()
