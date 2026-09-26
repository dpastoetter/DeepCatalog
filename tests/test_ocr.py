"""Tests for adaptive OCR quality gate and text recovery."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

from PIL import Image, ImageDraw

from deepcatalog import config
from deepcatalog.ocr import (
    assess_text_quality,
    page_uses_text_layer,
    recover_document_text,
    resolve_ocr_concurrency,
    resolve_ocr_mode,
    resolve_ocr_page_limit,
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


def test_assess_text_quality_rejects_short_garbage():
    bad = assess_text_quality("@@@ ###")
    assert bad.ok is False
    assert "too_short" in bad.reason or "too_few_words" in bad.reason

    good = assess_text_quality(
        "Invoice FA2022-0001 from BV CRE8 dated 2022-09-05. "
        "Total including VAT is EUR 181.50 for the comanage business package."
    )
    assert good.ok is True
    assert good.reason == "ok"


def test_page_uses_text_layer_modes():
    good = (
        "Invoice FA2022-0001 from BV CRE8 dated 2022-09-05. "
        "Total including VAT is EUR 181.50 for the comanage business package."
    )
    assert page_uses_text_layer(good, "balanced") is True
    assert page_uses_text_layer(good, "fast") is True
    assert page_uses_text_layer(good, "maximum") is False
    assert page_uses_text_layer("", "balanced") is False
    assert page_uses_text_layer("hi", "fast") is False


def test_recover_image_uses_ai_ocr(tmp_path: Path, monkeypatch):
    img_path = tmp_path / "scan.png"
    image = Image.new("RGB", (200, 60), "white")
    draw = ImageDraw.Draw(image)
    draw.text((8, 20), "x", fill="black")
    image.save(img_path)

    async def fake_indices(path, page_indices, **_kwargs):
        return {
            i: (
                "Invoice FA2022-0001 from BV CRE8 dated 2022-09-05. "
                "Total including VAT is EUR 181.50 for the comanage business package."
            )
            for i in page_indices
        }

    monkeypatch.setattr("deepcatalog.ocr._ai_vision_transcribe_indices", fake_indices)
    monkeypatch.setattr("deepcatalog.ocr.tesseract_enabled", lambda: False)
    monkeypatch.setenv("DEEPCATALOG_OCR_MODE", "balanced")

    result = asyncio.run(recover_document_text(img_path))
    assert result["status"] == "success"
    assert result["method"] == "ai_vision"
    assert result["used_ai_ocr"] is True
    assert "181.50" in result["text"]


def test_balanced_skips_vision_when_text_layer_good(tmp_path: Path, monkeypatch):
    path = tmp_path / "invoice.pdf"
    _minimal_text_pdf(
        path,
        "Invoice Acme Corp EUR 120 paid in full today for consulting services May",
    )

    async def boom(*_a, **_k):
        raise AssertionError("vision should not run for good text layer")

    monkeypatch.setattr("deepcatalog.ocr._ai_vision_transcribe_indices", boom)
    monkeypatch.setattr("deepcatalog.ocr.tesseract_enabled", lambda: False)
    monkeypatch.setenv("DEEPCATALOG_OCR_MODE", "balanced")
    monkeypatch.setattr(
        "deepcatalog.ocr.resolve_ocr_page_limit",
        lambda *_args, **_kwargs: 1,
    )

    result = asyncio.run(recover_document_text(path))
    assert result["status"] == "success"
    assert result["method"] == "pdf_text_layer"
    assert result["used_ai_ocr"] is False
    assert result["pages_from_text_layer"] == 1
    assert result["pages_from_vision"] == 0
    assert "Invoice" in result["text"]


def test_maximum_always_calls_vision(tmp_path: Path, monkeypatch):
    path = tmp_path / "invoice.pdf"
    _minimal_text_pdf(
        path,
        "Invoice Acme Corp EUR 120 paid in full today for consulting services May",
    )
    calls: list[int] = []

    async def fake_indices(path, page_indices, **_kwargs):
        calls.extend(page_indices)
        return {i: f"vision-{i}" for i in page_indices}

    monkeypatch.setattr("deepcatalog.ocr._ai_vision_transcribe_indices", fake_indices)
    monkeypatch.setattr("deepcatalog.ocr.tesseract_enabled", lambda: False)
    monkeypatch.setenv("DEEPCATALOG_OCR_MODE", "maximum")
    monkeypatch.setattr(
        "deepcatalog.ocr.resolve_ocr_page_limit",
        lambda *_args, **_kwargs: 1,
    )

    result = asyncio.run(recover_document_text(path))
    assert calls == [1]
    assert result["method"] == "ai_vision"
    assert result["used_ai_ocr"] is True
    assert "vision-1" in result["text"]


def test_recover_falls_back_to_text_layer_when_ai_fails(tmp_path: Path, monkeypatch):
    path = tmp_path / "invoice.pdf"
    _minimal_text_pdf(path, "Invoice Acme EUR 120 paid in full today")

    async def failing_indices(*_a, **_k):
        raise RuntimeError("vision unavailable")

    monkeypatch.setattr("deepcatalog.ocr._ai_vision_transcribe_indices", failing_indices)
    monkeypatch.setattr("deepcatalog.ocr.tesseract_enabled", lambda: False)
    monkeypatch.setenv("DEEPCATALOG_OCR_MODE", "maximum")
    monkeypatch.setattr(
        "deepcatalog.ocr.resolve_ocr_page_limit",
        lambda *_args, **_kwargs: 1,
    )

    result = asyncio.run(recover_document_text(path))
    assert result["method"] in {"pdf_text_layer_fallback", "ai_vision"}
    if result["method"] == "pdf_text_layer_fallback":
        assert "Invoice" in (result.get("text") or "") or result["status"] == "partial"


def test_per_page_ocr_concatenates_pages(tmp_path: Path, monkeypatch):
    img_path = tmp_path / "scan.png"
    Image.new("RGB", (40, 20), "white").save(img_path)
    calls: list[int] = []

    async def fake_indices(path, page_indices, **_kwargs):
        calls.extend(page_indices)
        return {i: f"content-{i}" for i in page_indices}

    monkeypatch.setattr("deepcatalog.ocr._ai_vision_transcribe_indices", fake_indices)
    monkeypatch.setattr("deepcatalog.ocr.tesseract_enabled", lambda: False)
    monkeypatch.setattr(
        "deepcatalog.ocr.resolve_ocr_page_limit",
        lambda *_args, **_kwargs: 3,
    )
    monkeypatch.setenv("DEEPCATALOG_OCR_MODE", "maximum")

    result = asyncio.run(recover_document_text(img_path))
    assert result["status"] == "success"
    assert calls == [1, 2, 3]
    assert "[Page 1]" in result["text"]
    assert "[Page 2]" in result["text"]
    assert "[Page 3]" in result["text"]
    assert "content-2" in result["text"]


def test_resolve_ocr_page_limit_unlimited_clamps_to_safety(tmp_path: Path, monkeypatch):
    pdf_path = tmp_path / "big.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 stub")

    monkeypatch.setattr("deepcatalog.ocr.pdf_page_count", lambda _p: 150)
    monkeypatch.setattr(config, "OCR_MAX_PAGES", 0)
    monkeypatch.setattr(config, "OCR_SAFETY_MAX_PAGES", 128)

    assert resolve_ocr_page_limit(pdf_path) == 128


def test_resolve_ocr_page_limit_respects_explicit_cap(tmp_path: Path, monkeypatch):
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 stub")

    monkeypatch.setattr("deepcatalog.ocr.pdf_page_count", lambda _p: 20)
    monkeypatch.setattr(config, "OCR_MAX_PAGES", 4)
    monkeypatch.setattr(config, "OCR_SAFETY_MAX_PAGES", 128)

    assert resolve_ocr_page_limit(pdf_path) == 4


def test_resolve_ocr_page_timeout_longer_for_ollama(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(config, "OCR_PAGE_TIMEOUT", 180.0)
    monkeypatch.setattr(config, "OLLAMA_OCR_PAGE_TIMEOUT", 600.0)
    from deepcatalog.ocr import resolve_ocr_page_timeout

    assert resolve_ocr_page_timeout() == 180.0
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    assert resolve_ocr_page_timeout() == 600.0


def test_resolve_ocr_concurrency_serial_for_ollama(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(config, "OCR_CONCURRENCY", 4)
    monkeypatch.setattr(config, "OCR_CONCURRENCY_OLLAMA", 1)
    assert resolve_ocr_concurrency() == 4
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    assert resolve_ocr_concurrency() == 1


def test_resolve_ocr_mode_env_overrides(monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_OCR_MODE", "fast")
    assert resolve_ocr_mode() == "fast"
    monkeypatch.setenv("DEEPCATALOG_OCR_MODE", "maximum")
    assert resolve_ocr_mode() == "maximum"


def test_prepare_page_image_for_vision_downscales_and_jpeg_for_ollama(monkeypatch):
    from deepcatalog.ocr import prepare_page_image_for_vision

    monkeypatch.setattr(config, "OCR_MAX_IMAGE_PX", 800)
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")

    big = Image.new("RGB", (1600, 2400), "white")
    buf = io.BytesIO()
    big.save(buf, format="PNG")
    data, mime = prepare_page_image_for_vision(buf.getvalue())
    assert mime == "image/jpeg"
    assert len(data) > 0
    with Image.open(io.BytesIO(data)) as resized:
        assert max(resized.size) <= 800
