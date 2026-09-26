"""In-memory progress bus for live ingest workflow visualization (SSE)."""

from __future__ import annotations

import asyncio
import contextvars
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

# Bound to the file currently being ingested (set by process_inbox / ingest).
current_file_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "progress_file_id", default=None
)
current_job_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "progress_job_id", default=None
)

_BUFFER_SIZE = 200
_lock = asyncio.Lock()
_buffer: deque[dict[str, Any]] = deque(maxlen=_BUFFER_SIZE)
_subscribers: set[asyncio.Queue[dict[str, Any] | None]] = set()


def _now() -> float:
    return time.time()


async def publish(event: dict[str, Any]) -> dict[str, Any]:
    """Publish a progress event to the ring buffer and all SSE subscribers."""
    payload = {
        **event,
        "ts": event.get("ts") or _now(),
        "job_id": event.get("job_id") or current_job_id.get(),
        "file_id": event.get("file_id") or current_file_id.get(),
    }
    async with _lock:
        _buffer.append(payload)
        dead: list[asyncio.Queue[dict[str, Any] | None]] = []
        for queue in _subscribers:
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(queue)
        for queue in dead:
            _subscribers.discard(queue)
    return payload


def publish_sync(event: dict[str, Any]) -> None:
    """Schedule publish from sync code (best-effort)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(publish(event))


async def emit_step(
    step_id: str,
    *,
    label: str,
    status: str,
    detail: str | None = None,
    file_id: str | None = None,
    job_id: str | None = None,
    filename: str | None = None,
) -> None:
    await publish(
        {
            "type": "step",
            "step_id": step_id,
            "label": label,
            "status": status,
            "detail": detail,
            "file_id": file_id,
            "job_id": job_id,
            "filename": filename,
        }
    )


def emit_step_sync(
    step_id: str,
    *,
    label: str,
    status: str,
    detail: str | None = None,
    filename: str | None = None,
) -> None:
    publish_sync(
        {
            "type": "step",
            "step_id": step_id,
            "label": label,
            "status": status,
            "detail": detail,
            "filename": filename,
        }
    )


async def subscribe(*, replay: bool = True) -> AsyncIterator[dict[str, Any]]:
    """Yield progress events; optionally replay recent buffer first."""
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=256)
    async with _lock:
        snapshot = list(_buffer) if replay else []
        _subscribers.add(queue)
    try:
        for item in snapshot:
            yield item
        while True:
            event = await queue.get()
            if event is None:
                break
            yield event
    finally:
        async with _lock:
            _subscribers.discard(queue)


def new_job_id() -> str:
    return str(uuid.uuid4())


def new_file_id() -> str:
    return str(uuid.uuid4())


PIPELINE_STEP_LABELS: dict[str, str] = {
    "read": "Open file",
    "ai_ocr": "Transcribe",
    "extract": "Find details",
    "name": "Name file",
    "file": "Save",
    "review": "Review",
    "index": "Make searchable",
}

PIPELINE_STEP_DESCRIPTIONS: dict[str, str] = {
    "read": "Load the scan and read any embedded PDF text layer.",
    "ai_ocr": "Recover text with local Tesseract when possible, then AI vision if needed.",
    "extract": "Pull out dates, parties, amounts, and other metadata with the LLM.",
    "name": "Propose a clear filename from the extracted details.",
    "file": "Move the document into the archive folder for its category.",
    "review": "Pause for your approval before anything is written to disk.",
    "index": "Chunk the text and store embeddings so Ask can search it.",
}

PIPELINE_STEPS = tuple(
    {
        "id": step_id,
        "label": PIPELINE_STEP_LABELS[step_id],
        "description": PIPELINE_STEP_DESCRIPTIONS[step_id],
    }
    for step_id in PIPELINE_STEP_LABELS
)


def step_label(step_id: str) -> str:
    """Human-readable label for a pipeline step id."""
    return PIPELINE_STEP_LABELS.get(step_id, step_id)


def step_description(step_id: str) -> str:
    """Short hover/help text explaining what a pipeline step does."""
    return PIPELINE_STEP_DESCRIPTIONS.get(step_id, step_label(step_id))


def llm_busy_detail(action: str, *, model: str | None = None) -> str:
    """Explain a long-running LLM call so the UI does not look stuck."""
    _ = model
    return f"{action} — can take a while"
