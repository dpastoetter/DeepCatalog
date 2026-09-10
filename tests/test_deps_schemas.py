"""deps helpers and schema validation."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.deps import archive_roots, is_within, path_is_auth_exempt, require_cloud_disclaimer_or_403
from app.schemas import AskRequest, OcrSetting, SettingsRequest


def test_is_within_basic(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    child = root / "a" / "b.txt"
    child.parent.mkdir(parents=True)
    child.write_text("x")
    assert is_within(child.resolve(), root.resolve()) is True
    assert is_within((tmp_path / "other").resolve(), root.resolve()) is False


def test_path_is_auth_exempt():
    assert path_is_auth_exempt("/api/health") is True
    assert path_is_auth_exempt("/api/auth/session") is True
    assert path_is_auth_exempt("/api/inbox") is False
    assert path_is_auth_exempt("/api/auth/desktop-bootstrap/abc") is True
    assert path_is_auth_exempt("/api/auth/desktop-bootstrap") is False


def test_archive_roots_includes_categories(isolated_data):
    roots = archive_roots()
    assert roots
    assert any("archive" in str(r) for r in roots)


def test_ask_request_rejects_blank():
    with pytest.raises(ValidationError):
        AskRequest(question="")


def test_settings_request_rejects_bad_ocr_mode():
    with pytest.raises(ValidationError):
        OcrSetting(mode="ultra")
    with pytest.raises(ValidationError):
        SettingsRequest(
            source_dir="/tmp/inbox",
            categories=[{"name": "other", "folder": "/tmp/other"}],
            ocr={"mode": "nope"},
        )


def test_require_cloud_disclaimer_or_403(monkeypatch):
    monkeypatch.setattr(
        "app.deps.require_cloud_disclaimer",
        lambda: (_ for _ in ()).throw(PermissionError("accept first")),
    )
    with pytest.raises(HTTPException) as exc:
        require_cloud_disclaimer_or_403()
    assert exc.value.status_code == 403
