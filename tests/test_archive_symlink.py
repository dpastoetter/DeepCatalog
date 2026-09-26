"""Archive destination symlink / path-escape confinement for move_to_archive."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.media_fixtures import write_minimal_pdf

from deepcatalog.settings import save_settings
from deepcatalog.tools.filesystem import (
    _ensure_archive_year_dir,
    _normalize_archive_year,
    move_to_archive,
    path_is_within,
)


@pytest.fixture()
def archive_layout(isolated_data):
    """Configured inbox + invoice/other category folders under the temp data dir."""
    inbox = isolated_data / "inbox"
    invoice = isolated_data / "archive" / "invoice"
    other = isolated_data / "archive" / "other"
    inbox.mkdir(parents=True, exist_ok=True)
    invoice.mkdir(parents=True, exist_ok=True)
    other.mkdir(parents=True, exist_ok=True)
    save_settings(
        {
            "source_dir": str(inbox),
            "categories": [
                {"name": "invoice", "folder": str(invoice)},
                {"name": "other", "folder": str(other)},
            ],
            "batch": {"poll_interval_seconds": 30},
        }
    )
    # Keep the settings cache warm so a mid-test category symlink is checked by
    # move_to_archive (is_symlink) rather than re-resolved via validate_settings.
    return {"inbox": inbox, "invoice": invoice, "other": other, "data": isolated_data}


def _inbox_pdf(archive_layout, name: str = "scan.pdf") -> Path:
    return write_minimal_pdf(archive_layout["inbox"] / name)


def test_normalize_archive_year_rejects_traversal():
    assert _normalize_archive_year("2024") == "2024"
    assert _normalize_archive_year("2024-03-15") == "2024"
    assert _normalize_archive_year("../etc") == "unknown"
    assert _normalize_archive_year("..") == "unknown"
    assert _normalize_archive_year("2024/../evil") == "unknown"
    assert _normalize_archive_year("unknown") == "unknown"


def test_move_to_archive_normal_path(archive_layout):
    src = _inbox_pdf(archive_layout)
    result = move_to_archive(
        source_path=str(src),
        filename="2024-01-01_Invoice_Acme.pdf",
        doc_type="invoice",
        year="2024",
    )
    assert result["status"] == "success"
    dest = Path(result["archive_path"])
    assert dest.is_file()
    assert not dest.is_symlink()
    assert path_is_within(dest, archive_layout["invoice"])
    assert dest.parent.name == "2024"
    assert not src.exists()


def test_move_to_archive_rejects_filename_traversal(archive_layout):
    src = _inbox_pdf(archive_layout)
    result = move_to_archive(
        source_path=str(src),
        filename="../../escape.pdf",
        doc_type="invoice",
        year="2024",
    )
    assert result["status"] == "success"
    dest = Path(result["archive_path"])
    assert dest.name == "escape.pdf"
    assert path_is_within(dest, archive_layout["invoice"])
    assert ".." not in dest.parts


def test_move_to_archive_rejects_category_symlink_escape(archive_layout):
    outside = archive_layout["data"].parent / "outside_category"
    outside.mkdir(parents=True, exist_ok=True)
    invoice = archive_layout["invoice"]
    # Replace category folder with a symlink that escapes the archive tree.
    # Keep the in-memory settings path (do not reload — reload would re-resolve).
    tmp = invoice.with_name("invoice.bak")
    invoice.rename(tmp)
    try:
        invoice.symlink_to(outside)
    except OSError:
        tmp.rename(invoice)
        pytest.skip("symlinks not available")
    try:
        assert invoice.is_symlink()
        src = _inbox_pdf(archive_layout)
        result = move_to_archive(
            source_path=str(src),
            filename="2024-01-01_Invoice_Sym.pdf",
            doc_type="invoice",
            year="2024",
        )
        assert result["status"] == "error"
        assert result.get("code") == "symlink_escape"
        assert src.exists()
        assert not any(outside.rglob("*.pdf"))
    finally:
        if invoice.is_symlink():
            invoice.unlink()
        if tmp.exists():
            tmp.rename(invoice)


def test_move_to_archive_rejects_year_symlink_escape(archive_layout):
    outside = archive_layout["data"].parent / "outside_year"
    outside.mkdir(parents=True, exist_ok=True)
    year_link = archive_layout["invoice"] / "2024"
    try:
        year_link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not available")
    src = _inbox_pdf(archive_layout)
    result = move_to_archive(
        source_path=str(src),
        filename="2024-01-01_Invoice_Year.pdf",
        doc_type="invoice",
        year="2024",
    )
    assert result["status"] == "error"
    assert result.get("code") == "symlink_escape"
    assert src.exists()
    assert list(outside.iterdir()) == []


def test_move_to_archive_rejects_year_symlink_inside_root(archive_layout):
    """Even an in-tree year symlink is refused (never follow dest dir links)."""
    invoice = archive_layout["invoice"]
    real_year = invoice / "2025"
    real_year.mkdir(parents=True, exist_ok=True)
    year_link = invoice / "2024"
    try:
        year_link.symlink_to(real_year)
    except OSError:
        pytest.skip("symlinks not available")
    src = _inbox_pdf(archive_layout)
    result = move_to_archive(
        source_path=str(src),
        filename="2024-01-01_Invoice_Inner.pdf",
        doc_type="invoice",
        year="2024",
    )
    assert result["status"] == "error"
    assert result.get("code") == "symlink_escape"
    assert src.exists()
    assert list(real_year.iterdir()) == []


def test_ensure_archive_year_dir_rejects_preexisting_year_symlink(archive_layout):
    """Year path that is already a symlink is refused before any write."""
    invoice = archive_layout["invoice"]
    outside = archive_layout["data"].parent / "race_outside"
    outside.mkdir(parents=True, exist_ok=True)
    year_link = invoice / "2030"
    try:
        year_link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not available")
    result = _ensure_archive_year_dir(invoice, "2030")
    assert isinstance(result, dict)
    assert result.get("code") == "symlink_escape"


def test_move_to_archive_skips_existing_dest_symlink_name(archive_layout):
    """An existing file symlink at the dest name must not be followed or replaced blindly."""
    src = _inbox_pdf(archive_layout)
    year = archive_layout["invoice"] / "2024"
    year.mkdir(parents=True, exist_ok=True)
    outside = archive_layout["data"].parent / "dest_target.pdf"
    outside.write_bytes(b"%PDF-1.4 outside")
    link = year / "2024-01-01_Invoice_Acme.pdf"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not available")

    result = move_to_archive(
        source_path=str(src),
        filename="2024-01-01_Invoice_Acme.pdf",
        doc_type="invoice",
        year="2024",
    )
    assert result["status"] == "success"
    dest = Path(result["archive_path"])
    assert dest.name != link.name or not dest.is_symlink()
    assert dest.resolve() != outside.resolve()
    assert outside.read_bytes() == b"%PDF-1.4 outside"
    assert path_is_within(dest, archive_layout["invoice"])


def test_path_is_within_uses_resolve_not_prefix():
    # ensure helper rejects obvious escapes (not a string-prefix check).
    assert path_is_within(Path("/etc/passwd"), Path("/var")) is False
