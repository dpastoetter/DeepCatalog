"""API-level checks for empty inbox processing."""

from fastapi.testclient import TestClient

from app.main import app
from deepcatalog.settings import clear_settings_cache, load_settings


def test_process_inbox_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("deepcatalog.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("deepcatalog.config.INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr("deepcatalog.config.ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr("deepcatalog.config.DB_PATH", tmp_path / "deepcatalog.db")
    monkeypatch.setattr("deepcatalog.config.CHROMA_DIR", tmp_path / "chroma")
    clear_settings_cache()
    (tmp_path / "inbox").mkdir(parents=True)
    load_settings()

    from tests.conftest import apply_test_client_auth

    client = TestClient(app)
    apply_test_client_auth(client)
    response = client.post("/api/process-inbox")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "empty"
    assert payload["processed"] == 0
    assert "empty" in payload["message"].lower()
    clear_settings_cache()
