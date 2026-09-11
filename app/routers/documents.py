"""Document archive list/search/file serving and Ask routes."""

from __future__ import annotations

import logging
import mimetypes
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.deps import archive_roots, is_within
from app.schemas import AskRequest
from deepcatalog.runner import run_query
from deepcatalog.tools.filesystem import open_with_os, reveal_in_explorer
from deepcatalog.tools.metadata_db import get_document, search_metadata
from deepcatalog.tools.rag_index import retrieve_chunks

logger = logging.getLogger(__name__)
router = APIRouter(tags=["documents"])

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _archived_document_path(document_id: str) -> Path:
    """Resolve a document id to a file under the configured archive roots."""
    result = get_document(document_id)
    if result.get("status") != "success":
        raise HTTPException(status_code=404, detail=result.get("error", "not found"))
    document = result.get("document") or {}
    path_value = document.get("path")
    if not path_value:
        raise HTTPException(status_code=404, detail="document has no path")
    path = Path(path_value).expanduser().resolve()
    if not any(is_within(path, root) for root in archive_roots()):
        raise HTTPException(status_code=403, detail="file is outside the archive")
    if not path.exists() or not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=(
                "File is missing on disk. It may have been deleted or filed to a "
                "temporary path. Re-process the scan, or use Remove all stored data "
                "and ingest again."
            ),
        )
    return path


def _optional_doc_date(value: str | None, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not _DATE_RE.match(value):
        raise HTTPException(
            status_code=422,
            detail=f"{field} must be YYYY-MM-DD",
        )
    return value


@router.get("/api/documents")
def api_documents(
    q: str | None = None,
    doc_type: str | None = None,
    counterparty: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = Query(default=40, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    return search_metadata(
        query=q,
        doc_type=doc_type,
        counterparty=counterparty,
        date_from=_optional_doc_date(date_from, "date_from"),
        date_to=_optional_doc_date(date_to, "date_to"),
        limit=limit,
        offset=offset,
    )


@router.get("/api/documents/{document_id}")
def api_document(document_id: str) -> dict[str, Any]:
    result = get_document(document_id)
    if result.get("status") != "success":
        raise HTTPException(status_code=404, detail=result.get("error", "not found"))
    return result


@router.post("/api/documents/{document_id}/reveal")
def api_reveal_document(document_id: str) -> dict[str, Any]:
    """Reveal an archived document in the system file manager."""
    path = _archived_document_path(document_id)
    revealed = reveal_in_explorer(str(path))
    if revealed.get("status") != "success":
        raise HTTPException(
            status_code=500,
            detail=revealed.get("error") or "could not open the file manager",
        )
    return revealed


@router.post("/api/documents/{document_id}/open")
def api_open_document(document_id: str) -> dict[str, Any]:
    """Open an archived document with the OS default application."""
    path = _archived_document_path(document_id)
    opened = open_with_os(str(path))
    if opened.get("status") != "success":
        raise HTTPException(
            status_code=500,
            detail=opened.get("error") or "could not open the file",
        )
    return opened


@router.get("/api/documents/{document_id}/file")
@router.head("/api/documents/{document_id}/file")
def api_document_file(document_id: str) -> FileResponse:
    """Serve an archived document file for in-browser viewing."""
    result = get_document(document_id)
    if result.get("status") != "success":
        raise HTTPException(status_code=404, detail=result.get("error", "not found"))
    document = result.get("document") or {}
    path = _archived_document_path(document_id)
    guessed, _ = mimetypes.guess_type(str(path))
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        media_type = "application/pdf"
    elif guessed:
        media_type = guessed
    else:
        media_type = "application/octet-stream"
    safe_filename = "".join(
        ch for ch in (document.get("filename") or path.name) if ch.isprintable()
    ).replace('"', "")
    return FileResponse(
        path,
        media_type=media_type,
        filename=safe_filename or path.name,
        content_disposition_type="inline",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/api/retrieve")
def api_retrieve(
    q: str = Query(..., min_length=1),
    top_k: int = Query(default=6, ge=1, le=20),
    doc_type: str | None = None,
) -> dict[str, Any]:
    return retrieve_chunks(query=q, top_k=top_k, doc_type=doc_type)


@router.post("/api/ask")
async def api_ask(body: AskRequest) -> dict[str, Any]:
    history = [{"role": t.role, "content": t.content} for t in body.history]
    try:
        return await run_query(body.question, history=history)
    except Exception:
        logger.exception("ask failed")
        raise HTTPException(status_code=500, detail="could not answer the question") from None
