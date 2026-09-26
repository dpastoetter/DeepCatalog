"""Human-in-the-loop review queue: hold proposed filings until approved."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepcatalog import config
from deepcatalog.pipeline.agents import file_and_persist
from deepcatalog.prompt_safety import sanitize_filing_proposal
from deepcatalog.settings import get_category_names, get_source_dir
from deepcatalog.tools.metadata_db import _connect, init_db

# Proposal fields the human may override on approval.
EDITABLE_FIELDS = (
    "filename",
    "doc_type",
    "doc_date",
    "subject",
    "counterparties",
    "reference_ids",
    "amount",
    "currency",
    "summary",
)


def _init_reviews_table() -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reviews (
                id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                original_name TEXT NOT NULL,
                checksum TEXT,
                content_hash TEXT,
                proposal_json TEXT NOT NULL,
                duplicates_json TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                document_id TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_status ON reviews(status)")
        conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["proposal"] = json.loads(data.pop("proposal_json") or "{}")
    except json.JSONDecodeError:
        data["proposal"] = {}
    try:
        data["duplicates"] = json.loads(data.pop("duplicates_json") or "[]")
    except json.JSONDecodeError:
        data["duplicates"] = []
    return data


def create_review(
    source_path: str,
    original_name: str,
    proposal: dict[str, Any],
    *,
    checksum: str | None = None,
    content_hash: str | None = None,
    duplicates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Queue a proposed filing for human approval. Returns the stored record."""
    _init_reviews_table()
    review_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO reviews (
                id, source_path, original_name, checksum, content_hash,
                proposal_json, duplicates_json, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                review_id,
                str(source_path),
                original_name,
                checksum,
                content_hash,
                json.dumps(proposal, ensure_ascii=False),
                json.dumps(duplicates or [], ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
    return {"status": "success", "review_id": review_id, "review": _row_to_dict(row)}


def get_review(review_id: str) -> dict[str, Any] | None:
    _init_reviews_table()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_pending() -> dict[str, Any]:
    """Pending review items, oldest first."""
    _init_reviews_table()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reviews WHERE status = 'pending' ORDER BY created_at ASC"
        ).fetchall()
    reviews = [_row_to_dict(r) for r in rows]
    return {"status": "success", "count": len(reviews), "reviews": reviews}


def pending_source_paths() -> set[str]:
    """Source paths currently awaiting review (skipped by inbox processing)."""
    _init_reviews_table()
    with _connect() as conn:
        rows = conn.execute("SELECT source_path FROM reviews WHERE status = 'pending'").fetchall()
    return {row["source_path"] for row in rows}


def pending_checksums() -> set[str]:
    """Checksums of files currently awaiting review (for duplicate warnings)."""
    _init_reviews_table()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT checksum FROM reviews WHERE status = 'pending' AND checksum IS NOT NULL"
        ).fetchall()
    return {row["checksum"] for row in rows}


def _mark_resolved(review_id: str, status: str, document_id: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE reviews SET status = ?, document_id = ?, resolved_at = ? WHERE id = ?",
            (status, document_id, now, review_id),
        )
        conn.commit()


def _claim_pending(review_id: str) -> dict[str, Any] | None:
    """
    Atomically move a review from 'pending' to 'processing'.

    Returns the review record, or None if it was not pending (missing, already
    resolved, or claimed by a concurrent request — e.g. a double-clicked
    Approve button).
    """
    _init_reviews_table()
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE reviews SET status = 'processing' WHERE id = ? AND status = 'pending'",
            (review_id,),
        )
        conn.commit()
        if cursor.rowcount != 1:
            return None
        row = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
    return _row_to_dict(row)


def _release_claim(review_id: str) -> None:
    """Return a claimed review to 'pending' after a failed resolve attempt."""
    with _connect() as conn:
        conn.execute(
            "UPDATE reviews SET status = 'pending' WHERE id = ? AND status = 'processing'",
            (review_id,),
        )
        conn.commit()


def recover_stale_processing() -> int:
    """
    Reset reviews stuck in 'processing' back to 'pending'.

    Called at server startup: if the process died mid-approve, the item
    becomes visible and actionable again instead of disappearing forever.
    """
    _init_reviews_table()
    with _connect() as conn:
        cursor = conn.execute("UPDATE reviews SET status = 'pending' WHERE status = 'processing'")
        conn.commit()
    return cursor.rowcount


def approve_review(review_id: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Approve a pending review (with optional human corrections) and file it.

    This is the only path from a pending proposal to actual filesystem writes.
    """
    review = _claim_pending(review_id)
    if review is None:
        existing = get_review(review_id)
        if existing is None:
            return {"status": "error", "error": f"review not found: {review_id}"}
        return {"status": "error", "error": f"review already {existing['status']}"}

    source = Path(review["source_path"])
    try:
        inbox = get_source_dir().resolve()
        # Filing moves files; never act on a source outside the inbox.
        source_ok = source.exists() and source.is_file() and source.resolve().is_relative_to(inbox)
    except OSError:
        source_ok = False
    if not source_ok:
        _release_claim(review_id)
        return {
            "status": "error",
            "error": f"source file is missing or outside the inbox: {source}",
        }

    proposal = dict(review["proposal"])
    for key, value in (overrides or {}).items():
        if key in EDITABLE_FIELDS and value is not None:
            proposal[key] = value

    # Re-apply allowlist / clamps / basename after human edits — model output
    # and overrides are both untrusted for paths and categories.
    proposal = sanitize_filing_proposal(
        proposal,
        get_category_names(),
        original_name=review.get("original_name"),
    )

    filename = str(proposal.get("filename") or review["original_name"])
    doc_type = str(proposal.get("doc_type") or "other")
    if not config.is_financial_doc_type(doc_type):
        proposal["amount"] = None
        proposal["currency"] = None
    amount = proposal.get("amount")
    extracted_for_db = {k: v for k, v in proposal.items() if k != "filename"}

    result = file_and_persist(
        source_path=str(source),
        filename=filename,
        doc_type=doc_type,
        doc_date=proposal.get("doc_date") if isinstance(proposal.get("doc_date"), str) else None,
        subject=proposal.get("subject") if isinstance(proposal.get("subject"), str) else None,
        counterparties=(
            proposal.get("counterparties")
            if isinstance(proposal.get("counterparties"), str)
            else None
        ),
        amount=float(amount) if isinstance(amount, (int, float)) else None,
        currency=proposal.get("currency") if isinstance(proposal.get("currency"), str) else None,
        summary=proposal.get("summary") if isinstance(proposal.get("summary"), str) else None,
        extracted_json=json.dumps(extracted_for_db, ensure_ascii=False),
        full_text=(
            proposal.get("full_text") if isinstance(proposal.get("full_text"), str) else None
        ),
        checksum=review.get("checksum"),
        content_hash=review.get("content_hash"),
    )
    if result.get("status") not in {"success", "partial"}:
        _release_claim(review_id)
        return result

    _mark_resolved(review_id, "approved", result.get("document_id"))
    return {**result, "review_id": review_id}


def reject_review(review_id: str, *, delete_file: bool = True) -> dict[str, Any]:
    """Reject a pending review; optionally remove the scan from the inbox."""
    review = _claim_pending(review_id)
    if review is None:
        existing = get_review(review_id)
        if existing is None:
            return {"status": "error", "error": f"review not found: {review_id}"}
        return {"status": "error", "error": f"review already {existing['status']}"}

    removed = False
    if delete_file:
        source = Path(review["source_path"])
        try:
            inbox = get_source_dir().resolve()
            # Only ever delete files that still live inside the inbox.
            if source.exists() and source.resolve().parent == inbox:
                source.unlink()
                removed = True
        except OSError:
            _release_claim(review_id)
            return {"status": "error", "error": "could not remove file"}

    _mark_resolved(review_id, "rejected")
    return {
        "status": "success",
        "review_id": review_id,
        "file_removed": removed,
    }
