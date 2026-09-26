"""Tests for classical Tesseract OCR helpers and adaptive recovery tier."""

from __future__ import annotations

import asyncio
from pathlib import Path

from PIL import Image, ImageDraw

from deepcatalog.ocr import recover_document_text
from deepcatalog.tesseract_ocr import (
    ocr_png_bytes,
    reset_tesseract_availability_cache,
    tesseract_available,
    tesseract_enabled,
)


def _minimal_text_pdf(path: Path, line: str) -> None:
    content = f"BT /F1 12 Tf 100 700 Td ({line}) Tj ET"
    stream = content.encode("latin-1", errors="replace")
    objects = [
        b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n",
        b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n",
        (
            b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources<< /Font<< /F1 5 0 R >> >> >>endobj\n"
        ),
        f"4 0 obj<< /Length {len(stream)} >>stream\n".encode() + stream + b"\nendstream\nendobj\n",
        b"5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj\n",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(pdf))
        pdf.extend(obj)
    xref_pos = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n".encode())
    pdf.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        pdf.extend(f"{off:010d} 00000 n \n".encode())
    pdf.extend(
        f"trailer<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    )
    path.write_bytes(pdf)


def test_ocr_image_soft_fails_without_binary(monkeypatch):
    reset_tesseract_availability_cache()
    monkeypatch.setattr("deepcatalog.tesseract_ocr.resolve_tesseract_cmd", lambda: None)
    assert ocr_png_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16) == ""


def test_tesseract_enabled_respects_env(monkeypatch):
    monkeypatch.setattr("deepcatalog.config.TESSERACT_ENABLED", False)
    monkeypatch.setattr("deepcatalog.tesseract_ocr.tesseract_available", lambda: True)
    assert tesseract_enabled() is False


def test_tesseract_enabled_respects_settings(monkeypatch, isolated_data):
    monkeypatch.setattr("deepcatalog.config.TESSERACT_ENABLED", True)
    monkeypatch.setattr("deepcatalog.tesseract_ocr.tesseract_available", lambda: True)
    from deepcatalog.settings import load_settings, save_settings

    settings = load_settings()
    settings["ocr"] = {"mode": "balanced", "tesseract": False}
    save_settings(settings)
    assert tesseract_enabled() is False


def test_good_tesseract_skips_vision(tmp_path: Path, monkeypatch):
    img_path = tmp_path / "scan.png"
    image = Image.new("RGB", (200, 60), "white")
    ImageDraw.Draw(image).text((8, 20), "x", fill="black")
    image.save(img_path)

    good = (
        "Invoice FA2022-0001 from BV CRE8 dated 2022-09-05. "
        "Total including VAT is EUR 181.50 for the comanage business package."
    )

    async def boom(*_a, **_k):
        raise AssertionError("vision should not run when Tesseract quality is good")

    monkeypatch.setenv("DEEPCATALOG_OCR_MODE", "balanced")
    monkeypatch.setattr("deepcatalog.ocr.tesseract_enabled", lambda: True)
    monkeypatch.setattr("deepcatalog.ocr.ocr_png_bytes", lambda *_a, **_k: good)
    monkeypatch.setattr(
        "deepcatalog.ocr.render_document_page",
        lambda *_a, **_k: b"fake-png",
    )
    monkeypatch.setattr("deepcatalog.ocr._ai_vision_transcribe_indices", boom)

    result = asyncio.run(recover_document_text(img_path))
    assert result["status"] == "success"
    assert result["method"] == "tesseract"
    assert result["used_ai_ocr"] is False
    assert result["pages_from_tesseract"] == 1
    assert result["pages_from_vision"] == 0
    assert "181.50" in result["text"]
    assert result["tesseract_ran"] is True


def test_poor_tesseract_falls_through_to_vision(tmp_path: Path, monkeypatch):
    img_path = tmp_path / "scan.png"
    Image.new("RGB", (40, 20), "white").save(img_path)

    async def fake_vision(path, page_indices, **kwargs):
        assert page_indices == [1]
        assert kwargs.get("page_images")  # reused render
        return {1: "vision-page-1"}

    monkeypatch.setenv("DEEPCATALOG_OCR_MODE", "balanced")
    monkeypatch.setattr("deepcatalog.ocr.tesseract_enabled", lambda: True)
    monkeypatch.setattr("deepcatalog.ocr.ocr_png_bytes", lambda *_a, **_k: "hi")
    monkeypatch.setattr(
        "deepcatalog.ocr.render_document_page",
        lambda *_a, **_k: b"fake-png",
    )
    monkeypatch.setattr("deepcatalog.ocr._ai_vision_transcribe_indices", fake_vision)

    result = asyncio.run(recover_document_text(img_path))
    assert result["method"] == "ai_vision"
    assert result["used_ai_ocr"] is True
    assert result["pages_from_tesseract"] == 0
    assert result["pages_from_vision"] == 1
    assert "vision-page-1" in result["text"]


def test_maximum_skips_tesseract(tmp_path: Path, monkeypatch):
    path = tmp_path / "invoice.pdf"
    _minimal_text_pdf(
        path,
        "Invoice Acme Corp EUR 120 paid in full today for consulting services May",
    )
    tess_calls: list[int] = []

    async def fake_tess(path, page_indices, **_kwargs):
        tess_calls.extend(page_indices)
        return {}, {}, list(page_indices)

    async def fake_vision(path, page_indices, **_kwargs):
        return {i: f"vision-{i}" for i in page_indices}

    monkeypatch.setenv("DEEPCATALOG_OCR_MODE", "maximum")
    monkeypatch.setattr("deepcatalog.ocr.tesseract_enabled", lambda: True)
    monkeypatch.setattr("deepcatalog.ocr._tesseract_transcribe_indices", fake_tess)
    monkeypatch.setattr("deepcatalog.ocr._ai_vision_transcribe_indices", fake_vision)
    monkeypatch.setattr(
        "deepcatalog.ocr.resolve_ocr_page_limit",
        lambda *_a, **_k: 1,
    )

    result = asyncio.run(recover_document_text(path))
    assert tess_calls == []
    assert result["tesseract_ran"] is False
    assert result["method"] == "ai_vision"
    assert result["used_ai_ocr"] is True


def test_missing_binary_goes_straight_to_vision(tmp_path: Path, monkeypatch):
    img_path = tmp_path / "scan.png"
    Image.new("RGB", (40, 20), "white").save(img_path)

    async def fake_vision(path, page_indices, **_kwargs):
        return {i: f"vision-{i}" for i in page_indices}

    monkeypatch.setenv("DEEPCATALOG_OCR_MODE", "balanced")
    monkeypatch.setattr("deepcatalog.ocr.tesseract_enabled", lambda: False)
    monkeypatch.setattr("deepcatalog.ocr._ai_vision_transcribe_indices", fake_vision)

    result = asyncio.run(recover_document_text(img_path))
    assert result["tesseract_ran"] is False
    assert result["method"] == "ai_vision"
    assert result["pages_from_tesseract"] == 0
    assert "vision-1" in result["text"]


def test_disabled_flag_skips_tesseract(tmp_path: Path, monkeypatch):
    img_path = tmp_path / "scan.png"
    Image.new("RGB", (40, 20), "white").save(img_path)

    async def fake_vision(path, page_indices, **_kwargs):
        return {1: "from-vision"}

    monkeypatch.setenv("DEEPCATALOG_OCR_MODE", "fast")
    monkeypatch.setattr("deepcatalog.config.TESSERACT_ENABLED", False)
    reset_tesseract_availability_cache()
    monkeypatch.setattr("deepcatalog.tesseract_ocr.tesseract_available", lambda: True)
    monkeypatch.setattr("deepcatalog.ocr._ai_vision_transcribe_indices", fake_vision)

    result = asyncio.run(recover_document_text(img_path))
    assert result["tesseract_ran"] is False
    assert result["used_ai_ocr"] is True
    assert "from-vision" in result["text"]


def test_availability_probe_caches(monkeypatch):
    reset_tesseract_availability_cache()
    calls = {"n": 0}

    def fake_cmd():
        calls["n"] += 1
        return "/usr/bin/tesseract"

    monkeypatch.setattr("deepcatalog.tesseract_ocr.resolve_tesseract_cmd", fake_cmd)
    monkeypatch.setattr("deepcatalog.tesseract_ocr.ensure_tessdata_prefix", lambda: None)
    assert tesseract_available() is True
    assert tesseract_available() is True
    assert calls["n"] == 1
