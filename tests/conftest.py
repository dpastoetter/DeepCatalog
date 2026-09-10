"""Shared fixtures: isolated data directory so tests never touch real data."""

from __future__ import annotations

import os

# Avoid auto-selecting a developer-machine Ollama during provider resolution.
os.environ.setdefault("DEEPCATALOG_SKIP_OLLAMA_PROBE", "1")
# Keep OCR/parse isolation off in the unit suite by default (spawn + coverage).
# Dedicated tests re-enable DEEPCATALOG_MEDIA_WORKER=1 explicitly.
os.environ.setdefault("DEEPCATALOG_MEDIA_WORKER", "0")

import pytest
from fastapi.testclient import TestClient

from app.main import CSRF_HEADER_NAME, CSRF_HEADER_VALUE, app
from deepcatalog.auth_rate_limit import reset_auth_rate_limiter
from deepcatalog.config import ensure_data_dirs
from deepcatalog.privacy import clear_privacy_cache
from deepcatalog.settings import clear_settings_cache, load_settings

# Stable secret for the unit suite — not a production value.
TEST_API_TOKEN = "pytest-local-api-token-not-for-production"


@pytest.fixture(autouse=True)
def _reset_auth_rate_limiter():
    reset_auth_rate_limiter()
    yield
    reset_auth_rate_limiter()


@pytest.fixture(autouse=True)
def _default_api_token(monkeypatch):
    """Require auth by default; tests that need a missing token must delenv it."""
    monkeypatch.setenv("DEEPCATALOG_API_TOKEN", TEST_API_TOKEN)
    monkeypatch.delenv("DEEPCATALOG_SINGLE_USER", raising=False)


@pytest.fixture()
def isolated_data(tmp_path, monkeypatch):
    """Point all storage (settings, DB, inbox, archive) at a temp directory."""
    data = tmp_path / "data"
    monkeypatch.setattr("deepcatalog.config.DATA_DIR", data)
    monkeypatch.setattr("deepcatalog.config.INBOX_DIR", data / "inbox")
    monkeypatch.setattr("deepcatalog.config.ARCHIVE_DIR", data / "archive")
    monkeypatch.setattr("deepcatalog.config.DB_PATH", data / "deepcatalog.db")
    monkeypatch.setattr("deepcatalog.config.CHROMA_DIR", data / "chroma")
    monkeypatch.setattr("deepcatalog.tools.rag_index.CHROMA_DIR", data / "chroma")
    # metadata_db froze DB_PATH at import time; patch its module copy too.
    monkeypatch.setattr("deepcatalog.tools.metadata_db.DB_PATH", data / "deepcatalog.db")
    clear_settings_cache()
    clear_privacy_cache()
    ensure_data_dirs()
    load_settings()
    yield data
    clear_settings_cache()
    clear_privacy_cache()


@pytest.fixture()
def stub_rag_index(monkeypatch):
    """Skip real embedding calls when file_and_persist indexes a document."""
    monkeypatch.setattr(
        "deepcatalog.pipeline.agents.index_document",
        lambda **_kw: {"status": "success", "chunk_count": 1},
    )


def apply_test_client_auth(client: TestClient, *, token: str | None = None) -> None:
    """Attach CSRF + Bearer headers expected by the default auth policy."""
    secret = token or os.environ.get("DEEPCATALOG_API_TOKEN") or TEST_API_TOKEN
    client.headers.update(
        {
            CSRF_HEADER_NAME: CSRF_HEADER_VALUE,
            "Authorization": f"Bearer {secret}",
        }
    )


@pytest.fixture()
def client(isolated_data):
    """TestClient that sends CSRF + Bearer required by mutating and API routes."""
    with TestClient(app) as tc:
        apply_test_client_auth(tc)
        yield tc
