"""Magic-byte and structural validation for untrusted scans."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from tests.media_fixtures import minimal_pdf_bytes, write_minimal_pdf, write_minimal_png

from deepcatalog.media_validate import (
    MediaValidationError,
    sniff_media_kind,
    validate_scan_file,
)


def test_sniff_detects_pdf_png_jpeg():
    assert sniff_media_kind(b"%PDF-1.7\n...") == "pdf"
    assert sniff_media_kind(b"\x89PNG\r\n\x1a\nxxxx") == "png"
    assert sniff_media_kind(b"\xff\xd8\xff\xe0JFIF") == "jpeg"
    assert sniff_media_kind(b"MZ executable") is None


def test_validate_accepts_real_pdf_and_png(tmp_path: Path):
    pdf = write_minimal_pdf(tmp_path / "ok.pdf")
    png = write_minimal_png(tmp_path / "ok.png")
    assert validate_scan_file(pdf)["kind"] == "pdf"
    assert validate_scan_file(png)["kind"] == "png"


def test_rejects_suffix_content_mismatch(tmp_path: Path):
    evil = tmp_path / "evil.pdf"
    evil.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    with pytest.raises(MediaValidationError, match="looks like png"):
        validate_scan_file(evil)


def test_rejects_unparseable_pdf_stub(tmp_path: Path):
    stub = tmp_path / "stub.pdf"
    stub.write_bytes(b"%PDF-1.4 stub that is not a real PDF")
    with pytest.raises(MediaValidationError) as exc:
        validate_scan_file(stub)
    assert exc.value.code == "unparseable"


def test_rejects_absurd_pdf_page_count(tmp_path: Path, monkeypatch):
    pdf = write_minimal_pdf(tmp_path / "many.pdf")

    def fake_validate(_path):
        from deepcatalog.media_worker import MediaWorkerError

        raise MediaWorkerError("PDF has too many pages (3; max 2)", code="too_many_pages")

    monkeypatch.setattr(
        "deepcatalog.media_validate.validate_pdf_structure_isolated",
        fake_validate,
    )
    with pytest.raises(MediaValidationError, match="too many pages") as exc:
        validate_scan_file(pdf)
    assert exc.value.code == "too_many_pages"


def test_rejects_oversized_image_pixels(tmp_path: Path, monkeypatch):
    path = tmp_path / "huge.png"
    monkeypatch.setattr("deepcatalog.media_worker.config.MEDIA_MAX_IMAGE_PIXELS", 1000)
    Image.new("RGB", (50, 50), "white").save(path, format="PNG")
    with pytest.raises(MediaValidationError) as exc:
        validate_scan_file(path)
    assert exc.value.code == "too_many_pixels"


def test_upload_rejects_mismatched_pdf_payload(client):
    resp = client.post(
        "/api/upload",
        files={"file": ("evil.pdf", b"\x89PNG\r\n\x1a\nnot-a-pdf", "application/pdf")},
    )
    assert resp.status_code == 415
    assert "png" in resp.json()["detail"].lower() or "match" in resp.json()["detail"].lower()


def test_upload_accepts_valid_pdf(client):
    payload = minimal_pdf_bytes()
    resp = client.post(
        "/api/upload",
        files={"file": ("scan.pdf", payload, "application/pdf")},
    )
    assert resp.status_code == 200
    assert resp.json()["filename"] == "scan.pdf"
    assert resp.json().get("media", {}).get("kind") == "pdf"
