"""Browser session exchange for DEEPCATALOG_API_TOKEN (hashed, short-lived)."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from deepcatalog import config
from deepcatalog.env_permissions import write_secret_text
from deepcatalog.local_security import COOKIE_NAME, get_api_token, token_matches

# Default 24h — long enough for a workday; override with DEEPCATALOG_SESSION_TTL_SECONDS.
_DEFAULT_TTL = 60 * 60 * 24
_STORE_NAME = "sessions.json"
_lock = threading.Lock()


def session_ttl_seconds() -> int:
    raw = os.getenv("DEEPCATALOG_SESSION_TTL_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_TTL
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_TTL
    return max(60, value)


def _store_path() -> Path:
    return Path(config.DATA_DIR).expanduser().resolve() / _STORE_NAME


def _hash_session(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_unlocked() -> dict[str, Any]:
    path = _store_path()
    if not path.exists():
        return {"sessions": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"sessions": {}}
    if not isinstance(data, dict):
        return {"sessions": {}}
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        return {"sessions": {}}
    return {"sessions": sessions}


def _save_unlocked(data: dict[str, Any]) -> None:
    path = _store_path()
    write_secret_text(path, json.dumps(data, indent=2, sort_keys=True))


def _prune_unlocked(data: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    sessions = data.get("sessions") or {}
    kept = {
        key: meta
        for key, meta in sessions.items()
        if isinstance(meta, dict) and float(meta.get("expires_at") or 0) > now
    }
    data["sessions"] = kept
    return data


def create_session() -> str:
    """Create a new session; return the raw id (cookie value only — never store raw)."""
    raw = secrets.token_urlsafe(32)
    digest = _hash_session(raw)
    expires_at = time.time() + session_ttl_seconds()
    with _lock:
        data = _prune_unlocked(_load_unlocked())
        data["sessions"][digest] = {"expires_at": expires_at}
        _save_unlocked(data)
    return raw


def session_is_valid(raw: str | None) -> bool:
    if not raw:
        return False
    digest = _hash_session(raw)
    now = time.time()
    with _lock:
        data = _prune_unlocked(_load_unlocked(), now=now)
        meta = data["sessions"].get(digest)
        if not isinstance(meta, dict):
            _save_unlocked(data)
            return False
        if float(meta.get("expires_at") or 0) <= now:
            data["sessions"].pop(digest, None)
            _save_unlocked(data)
            return False
        # Persist prune side-effects occasionally.
        _save_unlocked(data)
        return True


def revoke_session(raw: str | None) -> bool:
    if not raw:
        return False
    digest = _hash_session(raw)
    with _lock:
        data = _prune_unlocked(_load_unlocked())
        existed = digest in data["sessions"]
        data["sessions"].pop(digest, None)
        _save_unlocked(data)
    return existed


def api_token_accepted(api_token: str | None) -> bool:
    """True when ``api_token`` matches DEEPCATALOG_API_TOKEN (constant-time)."""
    expected = get_api_token()
    return bool(expected) and token_matches(api_token, expected)


def exchange_api_token(api_token: str | None) -> str | None:
    """Validate DEEPCATALOG_API_TOKEN and return a new raw session id, or None."""
    if not api_token_accepted(api_token):
        return None
    return create_session()


def clear_all_sessions() -> int:
    """Drop every stored session (tests / token rotation)."""
    with _lock:
        data = _load_unlocked()
        count = len(data.get("sessions") or {})
        _save_unlocked({"sessions": {}})
    return count


def attach_session_cookie(
    response: Any,
    raw_session_id: str,
    *,
    secure: bool = False,
) -> None:
    """Set the HttpOnly deepcatalog_session cookie (random id — never the API secret)."""
    response.set_cookie(
        key=COOKIE_NAME,
        # Random session id (hashed at rest). Not DEEPCATALOG_API_TOKEN.
        value=raw_session_id,  # codeql[py/clear-text-storage-sensitive-data]
        httponly=True,
        samesite="strict",
        path="/",
        max_age=session_ttl_seconds(),
        secure=secure,
    )
