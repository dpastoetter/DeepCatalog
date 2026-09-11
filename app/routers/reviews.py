"""Human review queue routes."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.deps import is_within
from app.schemas import ReviewApproveRequest, ReviewRejectRequest
from deepcatalog.review import approve_review, get_review, list_pending, reject_review
from deepcatalog.settings import get_source_dir
from deepcatalog.tools.filesystem import open_with_os

router = APIRouter(tags=["reviews"])


def _pending_review_path(review_id: str) -> Path:
    review = get_review(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail=f"review not found: {review_id}")
    path = Path(review["source_path"]).expanduser().resolve()
    if not is_within(path, get_source_dir().resolve()):
        raise HTTPException(status_code=403, detail="scan is outside the inbox")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="source scan is missing on disk")
    return path


@router.get("/api/reviews")
def api_reviews() -> dict[str, Any]:
    """Pending human-in-the-loop review items."""
    return list_pending()


@router.post("/api/reviews/{review_id}/approve")
def api_approve_review(review_id: str, body: ReviewApproveRequest) -> dict[str, Any]:
    """Approve a pending filing, optionally with human corrections."""
    overrides = {k: v for k, v in body.model_dump().items() if v is not None}
    result = approve_review(review_id, overrides)
    if result.get("status") not in {"success", "partial"}:
        raise HTTPException(status_code=409, detail="could not approve review")
    return result


@router.post("/api/reviews/{review_id}/reject")
def api_reject_review(review_id: str, body: ReviewRejectRequest) -> dict[str, Any]:
    """Reject a pending filing; by default also removes the inbox scan."""
    result = reject_review(review_id, delete_file=body.delete_file)
    if result.get("status") != "success":
        raise HTTPException(status_code=409, detail="could not reject review")
    return result


@router.get("/api/reviews/{review_id}/file")
@router.head("/api/reviews/{review_id}/file")
def api_review_file(review_id: str) -> FileResponse:
    """Serve the original scan of a pending review for in-browser viewing."""
    path = _pending_review_path(review_id)
    guessed, _ = mimetypes.guess_type(str(path))
    media_type = (
        "application/pdf"
        if path.suffix.lower() == ".pdf"
        else (guessed or "application/octet-stream")
    )
    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
        content_disposition_type="inline",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.post("/api/reviews/{review_id}/open")
def api_open_review(review_id: str) -> dict[str, Any]:
    """Open a pending review scan with the OS default application."""
    path = _pending_review_path(review_id)
    opened = open_with_os(str(path))
    if opened.get("status") != "success":
        raise HTTPException(
            status_code=500,
            detail=opened.get("error") or "could not open the file",
        )
    return opened
