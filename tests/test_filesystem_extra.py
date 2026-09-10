"""Filesystem helpers: list/clear inbox and reveal_in_explorer (mocked OS)."""

from __future__ import annotations

from deepcatalog.settings import get_source_dir
from deepcatalog.tools import filesystem


def test_list_and_clear_inbox(isolated_data):
    from tests.media_fixtures import write_minimal_pdf, write_minimal_png

    inbox = get_source_dir()
    pdf = write_minimal_pdf(inbox / "a.pdf")
    png = write_minimal_png(inbox / "b.png")
    junk = inbox / "notes.txt"
    junk.write_text("ignore")

    listed = filesystem.list_inbox()
    assert listed["count"] == 2
    names = {f["name"] for f in listed["files"]}
    assert names == {"a.pdf", "b.png"}

    cleared = filesystem.clear_inbox()
    assert cleared["status"] == "success"
    assert cleared["removed_count"] == 2
    assert set(cleared["removed"]) == {"a.pdf", "b.png"}
    assert filesystem.list_inbox()["count"] == 0
    assert junk.exists()
    assert pdf.parent == inbox
    assert png.parent == inbox


def test_reveal_in_explorer_missing(isolated_data):
    result = filesystem.reveal_in_explorer(str(isolated_data / "missing.pdf"))
    assert result["status"] == "error"
    assert "not found" in result["error"].lower()


def test_reveal_in_explorer_linux(isolated_data, monkeypatch):
    target = isolated_data / "show.pdf"
    target.write_bytes(b"%PDF")
    launched: list[list[str]] = []

    monkeypatch.setattr(filesystem.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        filesystem.shutil,
        "which",
        lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None,
    )

    def fake_popen(cmd, **_kw):
        launched.append(list(cmd))
        return None

    monkeypatch.setattr(filesystem.subprocess, "Popen", fake_popen)
    result = filesystem.reveal_in_explorer(str(target))
    assert result["status"] == "success"
    assert launched
    assert launched[0][0] == "/usr/bin/xdg-open"


def test_reveal_in_explorer_darwin(isolated_data, monkeypatch):
    target = isolated_data / "mac.pdf"
    target.write_bytes(b"%PDF")
    launched: list[list[str]] = []

    monkeypatch.setattr(filesystem.platform, "system", lambda: "Darwin")

    def fake_popen(cmd, **_kw):
        launched.append(list(cmd))
        return None

    monkeypatch.setattr(filesystem.subprocess, "Popen", fake_popen)
    result = filesystem.reveal_in_explorer(str(target))
    assert result["status"] == "success"
    assert launched[0][:2] == ["open", "-R"]


def test_read_document_image_notes_vision(isolated_data):
    from tests.media_fixtures import write_minimal_png

    img = write_minimal_png(get_source_dir() / "scan.png")
    result = filesystem.read_document(str(img))
    assert result["status"] == "success"
    assert result.get("suffix") == ".png" or "image" in str(result).lower()


def test_copy_local_scan_to_inbox(isolated_data, tmp_path):
    from tests.media_fixtures import write_minimal_pdf

    inbox = get_source_dir()
    src = write_minimal_pdf(tmp_path / "scan.pdf")
    copied = filesystem.copy_local_scan_to_inbox(src, max_bytes=1024 * 1024)
    assert copied["status"] == "success"
    dest = inbox / "scan.pdf"
    assert dest.is_file()
    assert dest.read_bytes() == src.read_bytes()

    again = filesystem.copy_local_scan_to_inbox(dest, max_bytes=1024 * 1024)
    assert again["status"] == "success"
    assert again.get("already_in_inbox") is True

    junk = tmp_path / "notes.txt"
    junk.write_text("nope")
    rejected = filesystem.copy_local_scan_to_inbox(junk, max_bytes=1024 * 1024)
    assert rejected["status"] == "error"
    assert rejected.get("code") == "unsupported"

    huge = tmp_path / "huge.pdf"
    huge.write_bytes(src.read_bytes())
    too_big = filesystem.copy_local_scan_to_inbox(huge, max_bytes=1)
    assert too_big["status"] == "error"
    assert too_big.get("code") == "too_large"
