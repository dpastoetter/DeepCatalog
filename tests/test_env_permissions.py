"""Tests for restrictive .env / secret-file / DATA_DIR permissions."""

from __future__ import annotations

import stat

import pytest

from deepcatalog.env_permissions import (
    PRIVATE_DIR_MODE,
    SECRET_FILE_MODE,
    ensure_app_data_permissions,
    ensure_dotenv_permissions,
    ensure_private_directory,
    harden_secret_file,
    is_group_or_world_accessible,
    is_posix,
    open_private_file,
    path_is_under,
    write_secret_text,
)

pytestmark = pytest.mark.skipif(not is_posix(), reason="POSIX permission modes only")


def test_write_secret_text_sets_0600(tmp_path):
    path = tmp_path / ".env"
    write_secret_text(path, "DEEPCATALOG_API_TOKEN=secret\n")
    assert path.read_text(encoding="utf-8") == "DEEPCATALOG_API_TOKEN=secret\n"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == SECRET_FILE_MODE
    assert not is_group_or_world_accessible(path)


def test_harden_secret_file_fixes_world_readable(tmp_path):
    path = tmp_path / ".env"
    path.write_text("OPENAI_API_KEY=x\n", encoding="utf-8")
    path.chmod(0o644)
    assert is_group_or_world_accessible(path)
    report = harden_secret_file(path, fix=True)
    assert report["was_insecure"] is True
    assert report["fixed"] is True
    assert not is_group_or_world_accessible(path)
    assert stat.S_IMODE(path.stat().st_mode) == SECRET_FILE_MODE


def test_harden_secret_file_reports_without_fix(tmp_path):
    path = tmp_path / ".env"
    path.write_text("x=1\n", encoding="utf-8")
    path.chmod(0o666)
    report = harden_secret_file(path, fix=False)
    assert report["was_insecure"] is True
    assert report["fixed"] is False
    assert is_group_or_world_accessible(path)


def test_ensure_dotenv_permissions_fixes_project_env(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("DEEPCATALOG_API_TOKEN=tok\n", encoding="utf-8")
    env_path.chmod(0o664)
    monkeypatch.setattr(
        "deepcatalog.env_permissions.candidate_env_paths",
        lambda: [env_path],
    )
    reports = ensure_dotenv_permissions(fix=True)
    assert len(reports) == 1
    assert reports[0]["was_insecure"] is True
    assert reports[0]["fixed"] is True
    assert stat.S_IMODE(env_path.stat().st_mode) == SECRET_FILE_MODE


def test_ensure_private_directory_sets_0700(tmp_path):
    root = tmp_path / "data"
    child = root / "inbox"
    ensure_private_directory(child, under=root)
    assert child.is_dir()
    assert stat.S_IMODE(child.stat().st_mode) == PRIVATE_DIR_MODE
    assert stat.S_IMODE(root.stat().st_mode) == PRIVATE_DIR_MODE


def test_ensure_private_directory_does_not_chmod_outside_root(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    external = tmp_path / "Documents" / "invoices"
    ensure_private_directory(external, under=data)
    assert external.is_dir()
    external.chmod(0o755)
    ensure_private_directory(external, under=data)
    assert stat.S_IMODE(external.stat().st_mode) == 0o755


def test_ensure_app_data_permissions_layout(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr("deepcatalog.config.DATA_DIR", data)
    report = ensure_app_data_permissions(data_root=data)
    for name in ("", "inbox", "archive", "chroma"):
        path = data if name == "" else data / name
        assert path.is_dir()
        assert stat.S_IMODE(path.stat().st_mode) == PRIVATE_DIR_MODE
    assert report["data_dir"] == str(data)

    sessions = data / "sessions.json"
    sessions.write_text('{"sessions":{}}\n', encoding="utf-8")
    sessions.chmod(0o644)
    ensure_app_data_permissions(data_root=data)
    assert stat.S_IMODE(sessions.stat().st_mode) == SECRET_FILE_MODE


def test_open_private_file_sets_0600(tmp_path):
    path = tmp_path / "scan.pdf"
    with open_private_file(path, binary=True) as handle:
        handle.write(b"%PDF")
    assert path.read_bytes() == b"%PDF"
    assert stat.S_IMODE(path.stat().st_mode) == SECRET_FILE_MODE


def test_path_is_under(tmp_path):
    base = tmp_path / "root"
    base.mkdir()
    child = base / "child"
    child.mkdir()
    assert path_is_under(child, base)
    assert path_is_under(base, base)
    assert not path_is_under(tmp_path, base)


def test_sessions_written_0600(isolated_data):
    from deepcatalog.sessions import clear_all_sessions, create_session

    clear_all_sessions()
    create_session()
    path = isolated_data / "sessions.json"
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == SECRET_FILE_MODE


def test_ensure_data_dirs_posix_modes(isolated_data):
    from deepcatalog.config import ensure_data_dirs

    ensure_data_dirs()
    for name in ("inbox", "archive", "chroma"):
        mode = stat.S_IMODE((isolated_data / name).stat().st_mode)
        assert mode == PRIVATE_DIR_MODE
    assert stat.S_IMODE(isolated_data.stat().st_mode) == PRIVATE_DIR_MODE


def test_sqlite_db_hardened_0600(isolated_data):
    from deepcatalog.tools.metadata_db import init_db

    init_db()
    db = isolated_data / "deepcatalog.db"
    assert db.is_file()
    assert stat.S_IMODE(db.stat().st_mode) == SECRET_FILE_MODE
