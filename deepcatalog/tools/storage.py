"""Destructive storage helpers: wipe tracked archive files, inbox scans, SQLite, Chroma."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from chromadb.api.client import SharedSystemClient

from deepcatalog import config
from deepcatalog.chroma_local import embedded_chroma_client
from deepcatalog.settings import get_source_dir, load_settings
from deepcatalog.tools.filesystem import SUPPORTED_SUFFIXES, clear_inbox
from deepcatalog.tools.metadata_db import init_db, list_recent

# Required verbatim on DELETE /api/data — UX confirms are not a security boundary.
CLEAR_DATA_CONFIRMATION = "DELETE ALL DEEPCATALOG DATA"


def _is_within(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def archive_roots_for_delete() -> list[Path]:
    """Roots under which a tracked archive file may be unlinked."""
    roots: list[Path] = []
    try:
        roots.append(Path(config.ARCHIVE_DIR).expanduser().resolve())
    except OSError:
        pass
    for category in load_settings().get("categories") or []:
        folder = category.get("folder")
        if not folder:
            continue
        try:
            roots.append(Path(folder).expanduser().resolve())
        except OSError:
            continue
    return roots


def _safe_rmtree(path: Path) -> bool:
    if not path.exists():
        return False
    if path.is_file():
        path.unlink(missing_ok=True)
        return True
    shutil.rmtree(path, ignore_errors=False)
    return True


def _delete_tracked_archive_files() -> tuple[int, int, int]:
    """
    Unlink files listed in metadata only when they resolve inside an archive root.

    Never recursively wipe category or inbox directories.
    """
    roots = archive_roots_for_delete()
    deleted = 0
    missing = 0
    refused = 0
    docs = list_recent(limit=100_000).get("documents") or []
    for doc in docs:
        path_value = doc.get("path")
        if not path_value:
            continue
        try:
            path = Path(path_value).expanduser().resolve()
        except OSError:
            missing += 1
            continue
        if not path.is_file():
            missing += 1
            continue
        if not any(_is_within(path, root) for root in roots):
            refused += 1
            continue
        path.unlink(missing_ok=True)
        deleted += 1
    return deleted, missing, refused


def clear_all_stored_data() -> dict[str, Any]:
    """
    Remove tracked archive files (path-confined), supported inbox scans, SQLite, and Chroma.

    Keeps Setup settings (settings.json) and app code. Does **not** recursively wipe
    user-configured source_dir or category folders.
    """
    config.ensure_data_dirs()

    deleted_files, missing_files, refused_files = _delete_tracked_archive_files()

    inbox = clear_inbox()
    inbox_removed = int(inbox.get("removed_count") or 0)
    source_dir = get_source_dir()

    # Reset SQLite (app-owned under DATA_DIR)
    db_path = Path(config.DB_PATH).expanduser().resolve()
    data_dir = Path(config.DATA_DIR).expanduser().resolve()
    if not _is_within(db_path, data_dir):
        raise RuntimeError(f"refusing to delete database outside DATA_DIR: {db_path}")
    db_removed = False
    if db_path.exists():
        db_path.unlink(missing_ok=True)
        db_removed = True
    for suffix in ("-wal", "-shm", "-journal"):
        side = Path(str(db_path) + suffix)
        if side.exists() and _is_within(side, data_dir):
            side.unlink(missing_ok=True)
    init_db()

    # Reset Chroma (app-owned under DATA_DIR)
    chroma_dir = Path(config.CHROMA_DIR).expanduser().resolve()
    if not _is_within(chroma_dir, data_dir):
        raise RuntimeError(f"refusing to delete Chroma outside DATA_DIR: {chroma_dir}")
    try:
        client = embedded_chroma_client(chroma_dir)
        for name in ("deepcatalog_chunks", "paperless_chunks"):
            try:
                client.delete_collection(name)
            except Exception:
                pass
        SharedSystemClient.clear_system_cache()
    except Exception:
        pass
    chroma_removed = _safe_rmtree(chroma_dir)
    try:
        SharedSystemClient.clear_system_cache()
    except Exception:
        pass
    chroma_dir.mkdir(parents=True, exist_ok=True)

    return {
        "status": "success",
        "deleted_tracked_files": deleted_files,
        "missing_tracked_files": missing_files,
        "refused_tracked_files": refused_files,
        "inbox_entries_removed": inbox_removed,
        "inbox_suffixes": sorted(SUPPORTED_SUFFIXES),
        "source_dir": str(source_dir),
        "database_removed": db_removed,
        "chroma_removed": chroma_removed,
        "message": (
            "Cleared tracked archive files (within configured archive roots), "
            "supported inbox scans, metadata database, and RAG index. "
            "Category folders were not recursively wiped."
        ),
    }
