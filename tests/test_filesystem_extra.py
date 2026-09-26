"""Filesystem helpers: list/clear inbox and reveal_in_explorer (mocked OS)."""

from __future__ import annotations

import subprocess

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
    clean = {"PATH": "/usr/bin", "HOME": str(isolated_data)}

    monkeypatch.setattr(filesystem.platform, "system", lambda: "Linux")
    monkeypatch.setattr(filesystem, "host_desktop_env", lambda: clean)
    monkeypatch.setattr(
        filesystem,
        "_which_host",
        lambda name, _env: "/usr/bin/xdg-open" if name == "xdg-open" else None,
    )
    monkeypatch.setattr(filesystem, "_reveal_via_file_manager1", lambda *_a, **_k: False)

    def fake_run(cmd, **kw):
        launched.append(list(cmd))
        assert kw.get("env") == clean
        assert "LD_LIBRARY_PATH" not in (kw.get("env") or {})
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(filesystem.subprocess, "run", fake_run)
    result = filesystem.reveal_in_explorer(str(target))
    assert result["status"] == "success"
    assert launched
    assert launched[0] == ["/usr/bin/xdg-open", str(target.parent)]


def test_open_with_os_linux(isolated_data, monkeypatch):
    target = isolated_data / "open-me.pdf"
    target.write_bytes(b"%PDF-1.4")
    launched: list[list[str]] = []
    clean = {"PATH": "/usr/bin", "HOME": str(isolated_data)}

    monkeypatch.setattr(filesystem.platform, "system", lambda: "Linux")
    monkeypatch.setattr(filesystem, "host_desktop_env", lambda: clean)
    monkeypatch.setattr(
        filesystem,
        "_which_host",
        lambda name, _env: {
            "gio": "/usr/bin/gio",
            "xdg-open": "/usr/bin/xdg-open",
        }.get(name),
    )

    def fake_run(cmd, **kw):
        launched.append(list(cmd))
        assert kw.get("env") == clean
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(filesystem.subprocess, "run", fake_run)
    result = filesystem.open_with_os(str(target))
    assert result["status"] == "success"
    assert launched[0] == ["/usr/bin/gio", "open", str(target)]


def test_reveal_in_explorer_darwin(isolated_data, monkeypatch):
    target = isolated_data / "mac.pdf"
    target.write_bytes(b"%PDF")
    launched: list[list[str]] = []

    monkeypatch.setattr(filesystem.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        filesystem,
        "host_desktop_env",
        lambda: {"PATH": "/usr/bin", "HOME": str(isolated_data)},
    )

    def fake_run(cmd, **_kw):
        launched.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(filesystem.subprocess, "run", fake_run)
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
