"""Cheap parent-process checks for untrusted scans (magic bytes + file size).

Structural PDF/image parsing (pypdf / Pillow / Poppler) runs only inside
``deepcatalog.media_worker`` — never import those libraries here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deepcatalog import config
from deepcatalog.media_worker import (
    MediaWorkerError,
    validate_image_structure_isolated,
    validate_pdf_structure_isolated,
)

# Keep sniffing cheap — enough for common scan formats.
_SNIFF_BYTES = 32

_KIND_BY_SUFFIX: dict[str, frozenset[str]] = {
    ".pdf": frozenset({"pdf"}),
    ".png": frozenset({"png"}),
    ".jpg": frozenset({"jpeg"}),
    ".jpeg": frozenset({"jpeg"}),
    ".webp": frozenset({"webp"}),
    ".tif": frozenset({"tiff"}),
    ".tiff": frozenset({"tiff"}),
    ".bmp": frozenset({"bmp"}),
}


class MediaValidationError(ValueError):
    """Reject untrusted or malformed scan media."""

    def __init__(self, message: str, *, code: str = "invalid_media") -> None:
        super().__init__(message)
        self.code = code


def sniff_media_kind(header: bytes) -> str | None:
    """Return a media kind from magic bytes, or None when unrecognized."""
    if len(header) < 4:
        return None
    if header.startswith(b"%PDF"):
        return "pdf"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if header.startswith(b"BM"):
        return "bmp"
    if header[:4] in {b"II*\x00", b"MM\x00*"}:
        return "tiff"
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "webp"
    return None


def expected_kinds_for_suffix(suffix: str) -> frozenset[str]:
    return _KIND_BY_SUFFIX.get(suffix.lower(), frozenset())


def _read_header(path: Path, n: int = _SNIFF_BYTES) -> bytes:
    with path.open("rb") as fh:
        return fh.read(n)


def _worker_error_to_validation(exc: MediaWorkerError) -> MediaValidationError:
    return MediaValidationError(str(exc), code=exc.code)


def validate_scan_file(path: Path | str, *, suffix: str | None = None) -> dict[str, Any]:
    """
    Validate a PDF/image after upload or before OCR.

    Parent process: file existence, size ceiling, magic bytes vs suffix.
    Structural decode (pypdf / Pillow) runs only in the media worker.
    ``suffix`` overrides ``path.suffix`` so ``.part`` upload temps can be checked
    against the intended destination extension.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise MediaValidationError(f"file not found: {file_path}", code="missing")

    try:
        size = file_path.stat().st_size
    except OSError as exc:
        raise MediaValidationError(f"unreadable file: {exc}", code="unparseable") from exc

    max_bytes = max(1, int(config.MEDIA_MAX_FILE_BYTES))
    if size <= 0:
        raise MediaValidationError("empty file", code="empty")
    if size > max_bytes:
        raise MediaValidationError(
            f"file too large ({size} bytes; max {max_bytes})",
            code="too_large",
        )

    check_suffix = (suffix or file_path.suffix).lower()
    if not check_suffix.startswith("."):
        check_suffix = f".{check_suffix}"
    expected = expected_kinds_for_suffix(check_suffix)
    if not expected:
        raise MediaValidationError(
            f"unsupported file type: {check_suffix or 'none'}",
            code="unsupported",
        )

    header = _read_header(file_path)
    kind = sniff_media_kind(header)
    if kind is None:
        raise MediaValidationError(
            "file content does not match a supported PDF/image type",
            code="type_mismatch",
        )
    if kind not in expected:
        raise MediaValidationError(
            f"file content looks like {kind}, but filename suffix is {check_suffix}",
            code="type_mismatch",
        )

    try:
        if kind == "pdf":
            info = validate_pdf_structure_isolated(file_path)
        else:
            info = validate_image_structure_isolated(file_path, expected_kind=kind)
    except MediaWorkerError as exc:
        raise _worker_error_to_validation(exc) from exc

    info["path"] = str(file_path.resolve())
    info["suffix"] = check_suffix
    info["sniffed_kind"] = kind
    info["size_bytes"] = size
    return info
