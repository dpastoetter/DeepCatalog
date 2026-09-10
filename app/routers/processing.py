"""Inbox upload and ingest processing routes."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.datastructures import UploadFile

from app.deps import MAX_UPLOAD_BYTES
from app.schemas import ProcessCancelRequest, ProcessRequest, ProcessRetryRequest
from deepcatalog.inbox_worker import (
    cancel_active_file,
    process_inbox,
    process_single_file,
    retry_file,
)
from deepcatalog.progress import PIPELINE_STEPS, subscribe
from deepcatalog.tools.filesystem import (
    SUPPORTED_SUFFIXES,
    clear_inbox,
    confined_inbox_file,
    list_inbox,
    stream_upload_to_inbox,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["processing"])


class RequestBodyReader:
    """Expose ``request.stream()`` as the async ``read()`` used by inbox streaming."""

    def __init__(self, request: Request) -> None:
        self._iterator = request.stream().__aiter__()
        self._buffer = b""
        self._done = False

    async def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        if size < 0:
            chunks = [self._buffer]
            self._buffer = b""
            while not self._done:
                try:
                    chunks.append(await self._iterator.__anext__())
                except StopAsyncIteration:
                    self._done = True
            return b"".join(chunks)
        while len(self._buffer) < size and not self._done:
            try:
                self._buffer += await self._iterator.__anext__()
            except StopAsyncIteration:
                self._done = True
        out = self._buffer[:size]
        self._buffer = self._buffer[size:]
        return out


def upload_filename_from_request(request: Request) -> str:
    raw = (request.headers.get("x-file-name") or request.query_params.get("filename") or "").strip()
    if not raw:
        return ""
    return Path(unquote(raw)).name


async def persist_inbox_upload(filename: str, upload: Any) -> dict[str, Any]:
    if not filename:
        raise HTTPException(status_code=400, detail="filename required")
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported file type: {suffix or 'none'} "
            f"(supported: {', '.join(sorted(SUPPORTED_SUFFIXES))})",
        )
    try:
        saved = await stream_upload_to_inbox(
            filename,
            upload,
            max_bytes=MAX_UPLOAD_BYTES,
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not save file: {exc}") from exc
    if saved.get("status") != "success":
        code = saved.get("code")
        detail = saved.get("error", "upload failed")
        if code == "too_large":
            raise HTTPException(status_code=413, detail=detail)
        if code == "empty":
            raise HTTPException(status_code=400, detail=detail)
        if code in {
            "type_mismatch",
            "unparseable",
            "encrypted",
            "too_many_pages",
            "too_many_pixels",
            "bad_page_size",
            "bad_image_size",
            "unsupported",
            "invalid_media",
        }:
            raise HTTPException(status_code=415, detail=detail)
        raise HTTPException(status_code=400, detail=detail)
    return saved


@router.get("/api/inbox")
def api_inbox() -> dict[str, Any]:
    return list_inbox()


@router.delete("/api/inbox")
def api_clear_inbox() -> dict[str, Any]:
    return clear_inbox()


@router.post("/api/upload")
async def api_upload(request: Request) -> dict[str, Any]:
    """
    Multipart ``file`` field, or a raw body with ``X-File-Name``.

    WebKitGTK drops custom headers on ``FormData`` fetch, so the desktop
    shell sends the scan as the request body instead of multipart.
    """
    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in content_type:
        form = await request.form()
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise HTTPException(status_code=400, detail="filename required")
        filename = upload.filename or ""
        try:
            return await persist_inbox_upload(filename, upload)
        finally:
            await upload.close()
    filename = upload_filename_from_request(request)
    return await persist_inbox_upload(filename, RequestBodyReader(request))


@router.post("/api/process")
async def api_process(body: ProcessRequest) -> dict[str, Any]:
    try:
        path = confined_inbox_file(body.path)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="only files inside the configured inbox can be processed",
        ) from None
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    try:
        return await process_single_file(str(path))
    except Exception:
        logger.exception("process file failed")
        raise HTTPException(status_code=500, detail="could not process file") from None


@router.post("/api/process-inbox")
async def api_process_inbox() -> dict[str, Any]:
    return await process_inbox()


@router.post("/api/process/cancel")
async def api_process_cancel(body: ProcessCancelRequest) -> dict[str, Any]:
    """Cancel the currently running file in an active ingest job."""
    result = await cancel_active_file(body.file_id)
    if result.get("status") != "success":
        raise HTTPException(status_code=409, detail=result.get("error", "cancel failed"))
    return result


@router.post("/api/process/retry")
async def api_process_retry(body: ProcessRetryRequest) -> dict[str, Any]:
    """Retry processing one inbox file (when idle, or cancel-then-retry when active)."""
    result = await retry_file(body.path)
    if result.get("status") == "error":
        raise HTTPException(status_code=409, detail=result.get("error", "retry failed"))
    return result


@router.get("/api/process/pipeline")
def api_process_pipeline() -> dict[str, Any]:
    """Static pipeline step definitions for the workflow UI."""
    return {"status": "success", "steps": list(PIPELINE_STEPS)}


@router.get("/api/process/events")
async def api_process_events() -> StreamingResponse:
    """Server-Sent Events stream of live ingest progress."""

    async def event_stream():
        hello = json.dumps(
            {"type": "hello", "steps": list(PIPELINE_STEPS)},
            ensure_ascii=False,
        )
        yield f"data: {hello}\n\n"
        async for event in subscribe(replay=True):
            payload = json.dumps(event, ensure_ascii=False, default=str)
            yield f"data: {payload}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
