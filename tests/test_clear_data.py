"""Tests for wiping archived files, SQLite, and Chroma (safe deletion model)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.conftest import apply_test_client_auth

from app.main import app
from deepcatalog.config import ensure_data_dirs
from deepcatalog.settings import (
    clear_settings_cache,
    load_settings,
    refuse_dangerous_storage_path,
    save_settings,
    validate_settings,
)
from deepcatalog.tools import metadata_db, rag_index
from deepcatalog.tools.storage import CLEAR_DATA_CONFIRMATION, clear_all_stored_data


def _isolate_data(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr("deepcatalog.config.DATA_DIR", data)
    monkeypatch.setattr("deepcatalog.config.INBOX_DIR", data / "inbox")
    monkeypatch.setattr("deepcatalog.config.ARCHIVE_DIR", data / "archive")
    monkeypatch.setattr("deepcatalog.config.DB_PATH", data / "deepcatalog.db")
    monkeypatch.setattr("deepcatalog.config.CHROMA_DIR", data / "chroma")
    monkeypatch.setattr("deepcatalog.tools.metadata_db.DB_PATH", data / "deepcatalog.db")
    monkeypatch.setattr("deepcatalog.tools.rag_index.CHROMA_DIR", data / "chroma")
    clear_settings_cache()
    ensure_data_dirs()
    load_settings()
    return data


def test_clear_all_stored_data(tmp_path, monkeypatch):
    data = _isolate_data(tmp_path, monkeypatch)

    archive_file = data / "archive" / "invoice" / "2024" / "doc.pdf"
    archive_file.parent.mkdir(parents=True, exist_ok=True)
    archive_file.write_bytes(b"%PDF-1.4 stub")
    inbox_file = data / "inbox" / "scan.pdf"
    inbox_file.write_bytes(b"%PDF-1.4 stub")
    inbox_notes = data / "inbox" / "notes.txt"
    inbox_notes.write_text("keep me")

    saved = metadata_db.upsert_metadata(
        original_name="scan.pdf",
        filename="doc.pdf",
        path=str(archive_file),
        doc_type="invoice",
        summary="Test invoice",
    )
    doc_id = saved["document_id"]

    def fake_embed(texts):
        return [[0.1] * 8 for _ in texts]

    monkeypatch.setattr(rag_index, "embed_texts", fake_embed)
    indexed = rag_index.index_document(
        document_id=doc_id,
        text="Test invoice from Acme for EUR 120.",
        filename="doc.pdf",
        doc_type="invoice",
    )
    assert indexed["status"] == "success"
    assert (data / "chroma").exists()
    assert (data / "deepcatalog.db").exists()

    settings_before = save_settings(
        {
            "source_dir": str(data / "inbox"),
            "categories": [
                {"name": "invoice", "folder": str(data / "archive" / "invoice")},
                {"name": "other", "folder": str(data / "archive" / "other")},
            ],
            "batch": {"poll_interval_seconds": 30},
        }
    )

    result = clear_all_stored_data()
    assert result["status"] == "success"
    assert result["deleted_tracked_files"] == 1
    assert result["refused_tracked_files"] == 0
    assert not archive_file.exists()
    assert not inbox_file.exists()
    assert inbox_notes.exists(), "non-scan inbox files must not be deleted"
    assert metadata_db.list_recent().get("count") == 0
    assert (data / "settings.json").exists()
    assert Path(settings_before["source_dir"]).is_dir()

    hits = rag_index.retrieve_chunks("Acme invoice", top_k=3)
    assert hits["status"] == "success"
    assert hits["count"] == 0
    clear_settings_cache()


def test_clear_does_not_wipe_untracked_or_out_of_root_files(tmp_path, monkeypatch):
    data = _isolate_data(tmp_path, monkeypatch)
    # Simulate a misconfigured category that points at an external tree (still allowed
    # as a *root* for tracked files, but we must not rmtree its contents).
    external = tmp_path / "Downloads"
    external.mkdir()
    keep = external / "unrelated.pdf"
    keep.write_bytes(b"%PDF keep")
    orphan = external / "orphan-not-in-db.pdf"
    orphan.write_bytes(b"%PDF orphan")

    tracked = external / "tracked.pdf"
    tracked.write_bytes(b"%PDF tracked")

    save_settings(
        {
            "source_dir": str(data / "inbox"),
            "categories": [
                {"name": "invoice", "folder": str(external)},
                {"name": "other", "folder": str(data / "archive" / "other")},
            ],
            "batch": {"poll_interval_seconds": 0},
        }
    )
    metadata_db.upsert_metadata(
        original_name="tracked.pdf",
        filename="tracked.pdf",
        path=str(tracked),
        doc_type="invoice",
    )

    # Escape hatch: path in DB outside every archive root must be refused.
    escape = tmp_path / "secret.pdf"
    escape.write_bytes(b"%PDF secret")
    metadata_db.upsert_metadata(
        original_name="secret.pdf",
        filename="secret.pdf",
        path=str(escape),
        doc_type="other",
        document_id="escape-doc",
    )

    result = clear_all_stored_data()
    assert result["status"] == "success"
    assert result["deleted_tracked_files"] == 1
    assert result["refused_tracked_files"] >= 1
    assert not tracked.exists()
    assert keep.exists()
    assert orphan.exists(), "untracked files in category folder must survive"
    assert escape.exists(), "paths outside archive roots must not be unlinked"
    clear_settings_cache()


def test_clear_all_data_api_requires_confirmation(tmp_path, monkeypatch):
    _isolate_data(tmp_path, monkeypatch)

    client = TestClient(app)
    apply_test_client_auth(client)

    denied = client.request("DELETE", "/api/data", json={"confirmation": "yes"})
    assert denied.status_code == 400

    missing = client.request("DELETE", "/api/data")
    assert missing.status_code == 422

    ok = client.request(
        "DELETE",
        "/api/data",
        json={"confirmation": CLEAR_DATA_CONFIRMATION},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "success"
    clear_settings_cache()


def test_refuse_dangerous_storage_paths(tmp_path, monkeypatch):
    _isolate_data(tmp_path, monkeypatch)
    home = Path.home()
    with pytest.raises(ValueError, match="home directory"):
        refuse_dangerous_storage_path(home, label="source_dir")
    with pytest.raises(ValueError, match="Downloads"):
        refuse_dangerous_storage_path(home / "Downloads", label="source_dir")
    with pytest.raises(ValueError, match="filesystem root"):
        refuse_dangerous_storage_path(Path("/"), label="source_dir")

    with pytest.raises(ValueError, match="Downloads"):
        validate_settings(
            {
                "source_dir": str(tmp_path / "data" / "inbox"),
                "categories": [
                    {"name": "invoice", "folder": str(home / "Downloads")},
                    {"name": "other", "folder": str(tmp_path / "data" / "archive" / "other")},
                ],
            }
        )
