"""Production filing helpers for ingest. ADK debug agents live in deepcatalog.adk_debug."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from deepcatalog.job_control import raise_if_cancelled
from deepcatalog.progress import emit_step_sync, llm_busy_detail, step_label
from deepcatalog.tools.filesystem import (
    move_to_archive,
    require_inbox_source,
)
from deepcatalog.tools.metadata_db import upsert_metadata
from deepcatalog.tools.rag_index import index_document

logger = logging.getLogger(__name__)


def file_and_persist(
    source_path: str,
    filename: str,
    doc_type: str,
    doc_date: str | None = None,
    subject: str | None = None,
    counterparties: str | None = None,
    amount: float | None = None,
    currency: str | None = None,
    summary: str | None = None,
    extracted_json: str | None = None,
    full_text: str | None = None,
    checksum: str | None = None,
    content_hash: str | None = None,
    *,
    after_file: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """
    File the source into the archive, save SQLite metadata, and RAG-index it.

    Filing is atomic from the caller's perspective: the document is copied to
    the archive first, metadata is committed, and only then is the inbox source
    removed. A failure at any point leaves the source untouched in the inbox.

    ``source_path`` must resolve inside the configured inbox — this check lives
    in the tool itself so ADK debug agents cannot file arbitrary paths.
    """
    confined = require_inbox_source(source_path)
    if isinstance(confined, dict):
        return confined

    raise_if_cancelled()
    source_name = Path(source_path).name
    year = None
    if doc_date and len(doc_date) >= 4:
        year = doc_date[:4]

    emit_step_sync(
        "file",
        label=step_label("file"),
        status="running",
        detail="Copying into the archive…",
        filename=source_name,
    )
    moved = move_to_archive(
        source_path=source_path,
        filename=filename,
        doc_type=doc_type,
        year=year,
        delete_source=False,
    )
    if moved.get("status") != "success":
        emit_step_sync(
            "file",
            label=step_label("file"),
            status="error",
            detail=moved.get("error"),
            filename=source_name,
        )
        return moved

    original_name = source_name
    try:
        saved = upsert_metadata(
            original_name=original_name,
            filename=moved["filename"],
            path=moved["archive_path"],
            doc_type=doc_type,
            doc_date=doc_date,
            subject=subject,
            counterparties=counterparties,
            amount=amount,
            currency=currency,
            summary=summary,
            extracted_json=extracted_json,
            checksum=checksum,
            content_hash=content_hash,
        )
    except Exception as exc:  # noqa: BLE001
        # Metadata failed: remove the archive copy so the inbox source stays
        # the single authoritative copy and can be reprocessed.
        Path(moved["archive_path"]).unlink(missing_ok=True)
        emit_step_sync(
            "file",
            label=step_label("file"),
            status="error",
            detail=str(exc),
            filename=source_name,
        )
        return {"status": "error", "error": f"metadata save failed: {exc}"}
    if saved.get("status") != "success":
        Path(moved["archive_path"]).unlink(missing_ok=True)
        emit_step_sync(
            "file",
            label=step_label("file"),
            status="error",
            detail=saved.get("error"),
            filename=source_name,
        )
        return saved

    # Both archive copy and metadata are in place — retire the inbox source.
    try:
        Path(source_path).unlink(missing_ok=True)
    except OSError as exc:
        # Duplicate copy remains in the inbox; safe direction (no data loss).
        logger.warning("Could not remove inbox source %s: %s", source_path, exc)

    emit_step_sync(
        "file",
        label=step_label("file"),
        status="done",
        detail=moved.get("archive_path"),
        filename=source_name,
    )
    if after_file is not None:
        after_file()

    document_id = saved["document_id"]
    text_for_index = full_text or summary or ""
    if extracted_json and not text_for_index:
        text_for_index = extracted_json
    emit_step_sync(
        "index",
        label=step_label("index"),
        status="running",
        detail=llm_busy_detail("Embedding text for Ask search"),
        filename=source_name,
    )
    indexed = index_document(
        document_id=document_id,
        text=text_for_index,
        filename=moved["filename"],
        doc_type=doc_type,
    )
    emit_step_sync(
        "index",
        label=step_label("index"),
        status="done" if indexed.get("status") == "success" else "error",
        detail=(
            f"{indexed.get('chunk_count')} searchable chunk"
            f"{'' if indexed.get('chunk_count') == 1 else 's'}"
            if indexed.get("status") == "success"
            else indexed.get("error")
        ),
        filename=source_name,
    )
    return {
        "status": "success" if indexed.get("status") == "success" else "partial",
        "document_id": document_id,
        "archive_path": moved["archive_path"],
        "filename": moved["filename"],
        "metadata": saved.get("document"),
        "index": indexed,
    }


def parse_json_blob(value: Any) -> dict[str, Any]:
    """Best-effort parse of agent JSON output that may include markdown fences."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    text = value.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start : end + 1])
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}
