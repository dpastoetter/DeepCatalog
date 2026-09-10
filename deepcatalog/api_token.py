"""Generate and persist DEEPCATALOG_API_TOKEN so loopback is never an auth boundary."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from deepcatalog import config
from deepcatalog.local_security import TOKEN_ENV, generate_api_token, get_api_token
from deepcatalog.ollama_setup import upsert_env_values

logger = logging.getLogger(__name__)


def token_env_path() -> Path:
    """Owner-only ``.env`` under DATA_DIR (never the possibly shared project file in tests)."""
    return Path(config.DATA_DIR).expanduser().resolve() / ".env"


def ensure_api_token(*, persist: bool = True) -> str:
    """
    Return the API token, generating one on first launch when unset.

    Loopback TCP is reachable by every local account and many containers, so a
    missing token must not mean "anyone on 127.0.0.1 is authenticated".
    """
    existing = get_api_token()
    if existing:
        return existing
    token = generate_api_token()
    os.environ[TOKEN_ENV] = token
    if persist:
        path = token_env_path()
        upsert_env_values({TOKEN_ENV: token}, path=path)
        logger.info(
            "Generated %s and stored it in %s (mode 0600). "
            "Loopback is not trusted; unlock the UI with this token or open the desktop window.",
            TOKEN_ENV,
            path,
        )
    return token
