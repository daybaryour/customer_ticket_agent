"""SQLite-backed store for uploaded knowledge-base documents.

Each row tracks one indexed document: where it came from, how many chunks were
written to Chroma, whether indexing succeeded, and the SHA-256 hash used to
reject duplicate uploads. Typed documents also keep their full content so the
dashboard can load them back into the editor.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
# Docker overrides KB_DB_PATH so the SQLite file lives in the /app/data volume.
DB_PATH = Path(os.getenv("KB_DB_PATH", str(BASE_DIR / "kb.sqlite3")))

logger = logging.getLogger(__name__)

INDEXED = "indexed"
FAILED = "failed"

SOURCE_TYPES = {"pdf", "csv", "docx", "txt", "md", "typed"}

_UPDATEABLE_COLUMNS = {
    "title",
    "source_type",
    "original_filename",
    "chunk_count",
    "status",
    "error",
    "uploaded_by",
    "chroma_doc_id",
    "content_hash",
    "content",
    "preview",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def migrate() -> None:
    """Create the kb_documents table and its indexes idempotently."""
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kb_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                source_type TEXT NOT NULL,
                original_filename TEXT,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'indexed',
                error TEXT,
                uploaded_by TEXT NOT NULL DEFAULT 'agent',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                chroma_doc_id TEXT NOT NULL DEFAULT '',
                content_hash TEXT,
                content TEXT,
                preview TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kb_documents_created_at "
            "ON kb_documents(created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kb_documents_content_hash "
            "ON kb_documents(content_hash)"
        )
        conn.commit()
    finally:
        conn.close()


def create_document(
    *,
    title: str,
    source_type: str,
    chroma_doc_id: str,
    original_filename: str | None = None,
    chunk_count: int = 0,
    status: str = INDEXED,
    error: str | None = None,
    uploaded_by: str = "agent",
    content_hash: str | None = None,
    content: str | None = None,
    preview: str | None = None,
) -> dict[str, Any]:
    """Insert a kb_documents row and return it as a dictionary."""
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"Invalid source_type: {source_type!r}")
    if status not in (INDEXED, FAILED):
        raise ValueError(f"Invalid status: {status!r}")

    now = _now()
    conn = _connect()
    try:
        cursor = conn.execute(
            """
            INSERT INTO kb_documents (
                title,
                source_type,
                original_filename,
                chunk_count,
                status,
                error,
                uploaded_by,
                created_at,
                updated_at,
                chroma_doc_id,
                content_hash,
                content,
                preview
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                source_type,
                original_filename,
                chunk_count,
                status,
                error,
                uploaded_by,
                now,
                now,
                chroma_doc_id,
                content_hash,
                content,
                preview,
            ),
        )
        conn.commit()
        doc_id = cursor.lastrowid
    finally:
        conn.close()

    record = get_document(int(doc_id))
    if record is None:  # pragma: no cover - insert just succeeded
        raise RuntimeError("Failed to read back the inserted kb document")
    return record


def list_documents() -> list[dict[str, Any]]:
    """Return every document newest first, with its chunk count."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM kb_documents ORDER BY created_at DESC, id DESC"
        ).fetchall()
        return [row_to_dict(row) for row in rows]
    finally:
        conn.close()


def get_document(doc_id: int) -> dict[str, Any] | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM kb_documents WHERE id = ?", (doc_id,)
        ).fetchone()
        return row_to_dict(row) if row is not None else None
    finally:
        conn.close()


def find_by_hash(content_hash: str) -> dict[str, Any] | None:
    """Return an indexed document with this content hash, if one exists.

    Failed rows are ignored so a file can be retried after an indexing error.
    """
    if not content_hash:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT * FROM kb_documents
            WHERE content_hash = ? AND status = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (content_hash, INDEXED),
        ).fetchone()
        return row_to_dict(row) if row is not None else None
    finally:
        conn.close()



def update_document(doc_id: int, **fields: Any) -> dict[str, Any] | None:
    """Update a subset of kb_documents columns and return the fresh row."""
    unknown = set(fields) - _UPDATEABLE_COLUMNS
    if unknown:
        raise ValueError(f"Unknown kb document fields: {sorted(unknown)}")

    if "source_type" in fields and fields["source_type"] not in SOURCE_TYPES:
        raise ValueError(f"Invalid source_type: {fields['source_type']!r}")
    if "status" in fields and fields["status"] not in (INDEXED, FAILED):
        raise ValueError(f"Invalid status: {fields['status']!r}")

    encoded = dict(fields)
    encoded["updated_at"] = _now()

    assignments = ", ".join(f"{name} = ?" for name in encoded)
    values = list(encoded.values()) + [doc_id]

    conn = _connect()
    try:
        conn.execute(
            f"UPDATE kb_documents SET {assignments} WHERE id = ?",
            values,
        )
        conn.commit()
    finally:
        conn.close()

    return get_document(doc_id)


def delete_document(doc_id: int) -> bool:
    """Delete a kb_documents row. Returns True when a row was removed."""
    conn = _connect()
    try:
        cursor = conn.execute("DELETE FROM kb_documents WHERE id = ?", (doc_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()

