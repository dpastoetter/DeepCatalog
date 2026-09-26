"""SQLite metadata store for archived documents."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepcatalog.config import DB_PATH, ensure_data_dirs
from deepcatalog.env_permissions import harden_app_owned_file

logger = logging.getLogger(__name__)

# Explicit schema revision stored in SQLite PRAGMA user_version.
# Bump when adding migrations in _apply_migrations().
SCHEMA_VERSION = 2

# Common NL words that should not drive keyword / FTS matches for Ask.
_FTS_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "all",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "get",
        "got",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "please",
        "show",
        "tell",
        "than",
        "that",
        "the",
        "their",
        "them",
        "there",
        "these",
        "this",
        "those",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)


def _connect() -> sqlite3.Connection:
    ensure_data_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # The API server and the background inbox poller both write this DB.
    # WAL + a generous busy timeout prevent "database is locked" errors.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    # SQLite creates files under the process umask; re-assert owner-only modes.
    harden_app_owned_file(DB_PATH)
    harden_app_owned_file(Path(str(DB_PATH) + "-wal"))
    harden_app_owned_file(Path(str(DB_PATH) + "-shm"))
    return conn


def _ensure_fts(conn: sqlite3.Connection) -> None:
    """Create the FTS5 index and backfill when documents outpace it."""
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
            document_id UNINDEXED,
            filename,
            original_name,
            subject,
            counterparties,
            summary,
            extracted_json,
            tokenize = 'porter unicode61'
        )
        """
    )
    doc_count = int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
    fts_count = int(conn.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0])
    if doc_count and fts_count != doc_count:
        _rebuild_fts(conn)


def _rebuild_fts(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM documents_fts")
    rows = conn.execute("SELECT * FROM documents").fetchall()
    for row in rows:
        _upsert_fts_row(conn, row)


def _upsert_fts_row(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    doc_id = row["id"]
    conn.execute("DELETE FROM documents_fts WHERE document_id = ?", (doc_id,))
    conn.execute(
        """
        INSERT INTO documents_fts(
            document_id, filename, original_name, subject,
            counterparties, summary, extracted_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            doc_id,
            row["filename"] or "",
            row["original_name"] or "",
            row["subject"] or "",
            row["counterparties"] or "",
            row["summary"] or "",
            row["extracted_json"] or "",
        ),
    )


def build_fts_match_query(text: str) -> str | None:
    """
    Turn a natural-language string into an FTS5 OR-query of meaningful tokens.

    Keeps invoice numbers, names, dates, and other identifiers that embeddings
    often miss; drops common question stopwords.
    """
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9._+/-]*", text or "")
    terms: list[str] = []
    seen: set[str] = set()
    for raw in tokens:
        lowered = raw.lower()
        if lowered in _FTS_STOPWORDS:
            continue
        if len(raw) < 2 and not raw.isdigit():
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        safe = raw.replace('"', '""')
        terms.append(f'"{safe}"')
        if len(terms) >= 24:
            break
    if not terms:
        return None
    return " OR ".join(terms)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _is_duplicate_column_error(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "duplicate column" in msg


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Add a column only when missing; re-raise unexpected ALTER failures."""
    if column in _table_columns(conn, table):
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    except sqlite3.OperationalError as exc:
        if _is_duplicate_column_error(exc):
            return
        raise


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """
    Bring the documents schema up to SCHEMA_VERSION.

    Existing databases may already have later columns while user_version is still
    0; column presence checks keep migrations idempotent before bumping the
    pragma.
    """
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])

    # v2: duplicate-detection + subject metadata columns (+ indexes).
    if current < 2:
        _ensure_column(conn, "documents", "checksum", "TEXT")
        _ensure_column(conn, "documents", "content_hash", "TEXT")
        _ensure_column(conn, "documents", "subject", "TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_checksum ON documents(checksum)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_content_hash ON documents(content_hash)"
        )

    if current < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        logger.info(
            "SQLite schema migrated from user_version=%s to %s",
            current,
            SCHEMA_VERSION,
        )


def init_db() -> None:
    """Create the documents table if it does not exist, then apply migrations."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                original_name TEXT NOT NULL,
                filename TEXT NOT NULL,
                path TEXT NOT NULL,
                doc_type TEXT,
                doc_date TEXT,
                counterparties TEXT,
                amount REAL,
                currency TEXT,
                summary TEXT,
                extracted_json TEXT,
                created_at TEXT NOT NULL,
                indexed_at TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_doc_type ON documents(doc_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_doc_date ON documents(doc_date)")
        _apply_migrations(conn)
        _ensure_fts(conn)
        conn.commit()


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    if data.get("extracted_json"):
        try:
            data["extracted"] = json.loads(data["extracted_json"])
        except json.JSONDecodeError:
            data["extracted"] = None
    return data


def upsert_metadata(
    original_name: str,
    filename: str,
    path: str,
    doc_type: str = "other",
    doc_date: str | None = None,
    subject: str | None = None,
    counterparties: str | None = None,
    amount: float | None = None,
    currency: str | None = None,
    summary: str | None = None,
    extracted_json: str | None = None,
    document_id: str | None = None,
    checksum: str | None = None,
    content_hash: str | None = None,
) -> dict[str, Any]:
    """Insert or update a document metadata row. Returns the saved record."""
    init_db()
    doc_id = document_id or str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    with _connect() as conn:
        _ensure_fts(conn)
        existing = conn.execute(
            "SELECT id, created_at FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE documents SET
                    original_name = ?,
                    filename = ?,
                    path = ?,
                    doc_type = ?,
                    doc_date = ?,
                    subject = ?,
                    counterparties = ?,
                    amount = ?,
                    currency = ?,
                    summary = ?,
                    extracted_json = ?,
                    checksum = ?,
                    content_hash = ?
                WHERE id = ?
                """,
                (
                    original_name,
                    filename,
                    path,
                    doc_type,
                    doc_date,
                    subject,
                    counterparties,
                    amount,
                    currency,
                    summary,
                    extracted_json,
                    checksum,
                    content_hash,
                    doc_id,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO documents (
                    id, original_name, filename, path, doc_type, doc_date,
                    subject, counterparties, amount, currency, summary, extracted_json,
                    checksum, content_hash, created_at, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    doc_id,
                    original_name,
                    filename,
                    path,
                    doc_type,
                    doc_date,
                    subject,
                    counterparties,
                    amount,
                    currency,
                    summary,
                    extracted_json,
                    checksum,
                    content_hash,
                    now,
                ),
            )
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if row is not None:
            _upsert_fts_row(conn, row)
        conn.commit()

    return {
        "status": "success",
        "document_id": doc_id,
        "document": _row_to_dict(row),
    }


def mark_indexed(document_id: str) -> dict[str, Any]:
    """Set indexed_at timestamp after RAG indexing succeeds."""
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE documents SET indexed_at = ? WHERE id = ?",
            (now, document_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if row is None:
        return {"status": "error", "error": f"document_id not found: {document_id}"}
    return {"status": "success", "document": _row_to_dict(row)}


def get_document(document_id: str) -> dict[str, Any]:
    """Fetch a single document by id."""
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if row is None:
        return {"status": "error", "error": f"document_id not found: {document_id}"}
    return {"status": "success", "document": _row_to_dict(row)}


def search_metadata(
    query: str | None = None,
    doc_type: str | None = None,
    counterparty: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """
    Search document metadata with optional filters.

    Free-text ``query`` uses SQLite FTS5 (token OR) so invoice numbers, names,
    and dates match even inside longer Ask questions. Falls back to LIKE when
    FTS cannot run or returns nothing for a short exact substring.

    Pagination uses ``LIMIT`` / ``OFFSET`` with a limit+1 probe so callers get
    ``has_more`` without a separate COUNT query.
    """
    init_db()
    capped = max(1, min(limit, 100))
    skip = max(0, int(offset))
    fts_match = build_fts_match_query(query) if query else None

    filter_clauses: list[str] = []
    filter_params: list[Any] = []
    if doc_type:
        filter_clauses.append("doc_type = ?")
        filter_params.append(doc_type)
    if counterparty:
        filter_clauses.append("(counterparties LIKE ? OR subject LIKE ?)")
        like = f"%{counterparty}%"
        filter_params.extend([like, like])
    if date_from:
        filter_clauses.append("doc_date >= ?")
        filter_params.append(date_from)
    if date_to:
        filter_clauses.append("doc_date <= ?")
        filter_params.append(date_to)

    def _run(where_sql: str, where_params: list[Any]) -> list[sqlite3.Row]:
        parts = [c for c in [where_sql, *filter_clauses] if c]
        where = f"WHERE {' AND '.join(parts)}" if parts else ""
        # Fetch one extra row to detect has_more without COUNT(*).
        sql = (
            f"SELECT * FROM documents {where} "
            "ORDER BY COALESCE(doc_date, created_at) DESC LIMIT ? OFFSET ?"
        )
        with _connect() as conn:
            _ensure_fts(conn)
            return list(
                conn.execute(
                    sql,
                    [*where_params, *filter_params, capped + 1, skip],
                ).fetchall()
            )

    used = "filter"
    rows: list[sqlite3.Row] = []

    if query and fts_match:
        used = "fts"
        try:
            rows = _run(
                "id IN (SELECT document_id FROM documents_fts WHERE documents_fts MATCH ?)",
                [fts_match],
            )
        except sqlite3.OperationalError as exc:
            logger.warning("FTS metadata search failed (%s); using LIKE", exc)
            used = "like"
            like = f"%{query}%"
            rows = _run(
                "(filename LIKE ? OR summary LIKE ? OR subject LIKE ? "
                "OR counterparties LIKE ? OR extracted_json LIKE ? OR original_name LIKE ?)",
                [like, like, like, like, like, like],
            )
        if not rows and len(query.strip()) <= 64:
            like = f"%{query.strip()}%"
            like_rows = _run(
                "(filename LIKE ? OR summary LIKE ? OR subject LIKE ? "
                "OR counterparties LIKE ? OR extracted_json LIKE ? OR original_name LIKE ?)",
                [like, like, like, like, like, like],
            )
            if like_rows:
                rows = like_rows
                used = "like"
    elif query:
        used = "like"
        like = f"%{query}%"
        rows = _run(
            "(filename LIKE ? OR summary LIKE ? OR subject LIKE ? "
            "OR counterparties LIKE ? OR extracted_json LIKE ? OR original_name LIKE ?)",
            [like, like, like, like, like, like],
        )
    else:
        rows = _run("", [])

    has_more = len(rows) > capped
    page = rows[:capped]
    return {
        "status": "success",
        "count": len(page),
        "documents": [_row_to_dict(r) for r in page],
        "search": used,
        "has_more": has_more,
        "offset": skip,
        "limit": capped,
    }


def list_recent(limit: int = 50) -> dict[str, Any]:
    """Return recently created documents."""
    return search_metadata(limit=limit)


def list_all_documents() -> list[dict[str, Any]]:
    """Return every document row (for RAG rebuild)."""
    init_db()
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM documents ORDER BY created_at ASC").fetchall()
    docs: list[dict[str, Any]] = []
    for row in rows:
        item = _row_to_dict(row)
        if item is not None:
            docs.append(item)
    return docs


def clear_all_indexed_at() -> int:
    """Clear indexed_at on every document (index is about to be rebuilt)."""
    init_db()
    with _connect() as conn:
        cur = conn.execute("UPDATE documents SET indexed_at = NULL")
        conn.commit()
        return int(cur.rowcount or 0)
