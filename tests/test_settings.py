"""Tests for Setup settings persistence and category folder mapping."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from deepcatalog.config import ensure_data_dirs
from deepcatalog.settings import (
    SettingsError,
    clear_settings_cache,
    get_folder_for_category,
    get_source_dir,
    load_settings,
    save_settings,
)
from deepcatalog.tools import filesystem


@pytest.fixture()
def isolated_settings(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr("deepcatalog.config.DATA_DIR", data)
    monkeypatch.setattr("deepcatalog.config.INBOX_DIR", data / "inbox")
    monkeypatch.setattr("deepcatalog.config.ARCHIVE_DIR", data / "archive")
    monkeypatch.setattr("deepcatalog.config.DB_PATH", data / "deepcatalog.db")
    monkeypatch.setattr("deepcatalog.config.CHROMA_DIR", data / "chroma")
    clear_settings_cache()
    ensure_data_dirs()
    yield data
    clear_settings_cache()


def test_default_settings_created(isolated_settings):
    settings = load_settings()
    assert Path(settings["source_dir"]) == isolated_settings / "inbox"
    names = [c["name"] for c in settings["categories"]]
    assert "other" in names
    assert "invoice" in names
    assert (isolated_settings / "settings.json").exists()


def test_save_and_get_source_dir(isolated_settings):
    custom_inbox = isolated_settings / "scans"
    custom_invoice = isolated_settings / "filed" / "invoices"
    saved = save_settings(
        {
            "source_dir": str(custom_inbox),
            "categories": [
                {"name": "invoice", "folder": str(custom_invoice)},
                {"name": "other", "folder": str(isolated_settings / "filed" / "other")},
            ],
            "batch": {"poll_interval_seconds": 45},
        }
    )
    clear_settings_cache()
    assert get_source_dir() == custom_inbox.resolve()
    assert get_folder_for_category("invoice") == custom_invoice.resolve()
    assert saved["batch"]["poll_interval_seconds"] == 45
    assert custom_inbox.is_dir()
    assert custom_invoice.is_dir()


def test_move_to_archive_uses_category_folder(isolated_settings):
    custom = isolated_settings / "custom_archive" / "bills"
    save_settings(
        {
            "source_dir": str(isolated_settings / "inbox"),
            "categories": [
                {"name": "invoice", "folder": str(custom)},
                {
                    "name": "other",
                    "folder": str(isolated_settings / "archive" / "other"),
                },
            ],
            "batch": {"poll_interval_seconds": 30},
        }
    )
    src = isolated_settings / "inbox" / "scan.pdf"
    src.write_bytes(b"%PDF-1.4 stub")
    moved = filesystem.move_to_archive(
        source_path=str(src),
        filename="2024-01-01_Invoice_Acme_EUR10.pdf",
        doc_type="invoice",
        year="2024",
    )
    assert moved["status"] == "success"
    assert Path(moved["archive_path"]).exists()
    assert str(custom.resolve()) in moved["archive_path"]
    assert "/2024/" in moved["archive_path"]


def test_settings_api_poll_interval_and_process_all(isolated_settings, monkeypatch):
    custom_inbox = isolated_settings / "batch_inbox"
    custom_inbox.mkdir(parents=True)
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        (custom_inbox / name).write_bytes(b"%PDF-1.4 stub")

    from tests.conftest import apply_test_client_auth

    client = TestClient(app)
    apply_test_client_auth(client)
    put = client.put(
        "/api/settings",
        json={
            "source_dir": str(custom_inbox),
            "categories": [
                {
                    "name": "other",
                    "folder": str(isolated_settings / "archive" / "other"),
                },
            ],
            "batch": {"poll_interval_seconds": 15},
        },
    )
    assert put.status_code == 200
    assert put.json()["settings"]["batch"]["poll_interval_seconds"] == 15

    async def fake_run(path: str):
        return {"status": "success", "path": path}

    monkeypatch.setattr("deepcatalog.inbox_worker.run_pipeline_on_path", fake_run)
    response = client.post("/api/process-inbox")
    assert response.status_code == 200
    payload = response.json()
    assert payload["processed"] == 3


def test_settings_requires_other(isolated_settings):
    with pytest.raises(ValueError, match="other"):
        save_settings(
            {
                "source_dir": str(isolated_settings / "inbox"),
                "categories": [
                    {
                        "name": "invoice",
                        "folder": str(isolated_settings / "archive" / "invoice"),
                    },
                ],
                "batch": {},
            }
        )


def test_malformed_settings_are_not_replaced_with_defaults(isolated_settings):
    path = isolated_settings / "settings.json"
    path.write_text("{not-json", encoding="utf-8")
    clear_settings_cache()
    with pytest.raises(SettingsError, match="not valid JSON"):
        load_settings()
    # Corrupt file must remain so operators can recover archive paths.
    assert path.read_text(encoding="utf-8") == "{not-json"


def test_invalid_settings_are_not_replaced_with_defaults(isolated_settings):
    path = isolated_settings / "settings.json"
    bad = {
        "source_dir": str(isolated_settings / "inbox"),
        "categories": [{"name": "invoice", "folder": "/tmp/x"}],
        "batch": {},
    }
    path.write_text(json.dumps(bad), encoding="utf-8")
    clear_settings_cache()
    with pytest.raises(SettingsError, match="other"):
        load_settings()
    assert json.loads(path.read_text(encoding="utf-8")) == bad


def test_load_settings_migrates_missing_review_and_ocr(isolated_settings):
    path = isolated_settings / "settings.json"
    stub = {
        "source_dir": str(isolated_settings / "inbox"),
        "categories": [
            {
                "name": "other",
                "folder": str(isolated_settings / "archive" / "other"),
            },
        ],
        "batch": {"poll_interval_seconds": 30},
    }
    path.write_text(json.dumps(stub), encoding="utf-8")
    clear_settings_cache()
    settings = load_settings()
    assert settings["review"]["require_approval"] is True
    assert settings["ocr"]["mode"] == "balanced"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["review"]["require_approval"] is True
    assert on_disk["ocr"]["mode"] == "balanced"


def test_load_settings_keeps_require_approval_false_when_ocr_missing(
    isolated_settings,
):
    path = isolated_settings / "settings.json"
    stub = {
        "source_dir": str(isolated_settings / "inbox"),
        "categories": [
            {
                "name": "other",
                "folder": str(isolated_settings / "archive" / "other"),
            },
        ],
        "batch": {"poll_interval_seconds": 0},
        "review": {"require_approval": False},
    }
    path.write_text(json.dumps(stub), encoding="utf-8")
    clear_settings_cache()
    settings = load_settings()
    assert settings["review"]["require_approval"] is False
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["review"]["require_approval"] is False
    assert on_disk["ocr"]["mode"] == "balanced"


def test_put_require_approval_false_persists_to_disk(isolated_settings):
    from tests.conftest import apply_test_client_auth

    client = TestClient(app)
    apply_test_client_auth(client)
    settings = client.get("/api/settings").json()["settings"]
    settings["review"]["require_approval"] = False
    resp = client.put("/api/settings", json=settings)
    assert resp.status_code == 200
    assert resp.json()["settings"]["review"]["require_approval"] is False
    on_disk = json.loads((isolated_settings / "settings.json").read_text(encoding="utf-8"))
    assert on_disk["review"]["require_approval"] is False
