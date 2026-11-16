"""SQLite-backed store for support tickets.

Every ticket processed by the graph is persisted here, including the ones the
AI resolves automatically. Columns include the original ticket fields,
classification, the AI draft, confidence/issues/sources, model provenance,
and resolution metadata.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
# Docker overrides TICKETS_DB_PATH so the SQLite file lives in the /app/data volume.
DB_PATH = Path(os.getenv("TICKETS_DB_PATH", str(BASE_DIR / "tickets.sqlite3")))

logger = logging.getLogger(__name__)

PENDING = "pending"
RESOLVED = "resolved"
REJECTED = "rejected"

AUTO = "auto"
MANUAL_APPROVED = "manual_approved"
MANUAL_EDITED = "manual_edited"
MANUAL_WRITTEN = "manual_written"

MANUAL_RESOLUTION_TYPES = {MANUAL_APPROVED, MANUAL_EDITED, MANUAL_WRITTEN}

ALLOWED_RESOLUTION_TYPES = {None, AUTO, *MANUAL_RESOLUTION_TYPES}

_UPDATEABLE_COLUMNS = {
    "subject",
    "body",
    "customer_tier",
    "ticket_id",
    "urgency",
    "department",
    "complexity",
    "sentiment",
    "summary",
    "ai_draft",
    "confidence",
    "issues",
    "sources_used",
    "status",
    "resolution_type",
    "final_answer",
    "resolved_by",
    "resolved_at",
    "model_used",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _dump(value: Any) -> Any:
    """Serialize Pydantic models and other objects into plain Python data."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    return str(value)


def _dumps_list(items: Any) -> str:
    if items is None:
        return "[]"
    if isinstance(items, str):
        items = [items]
    return json.dumps([str(item) for item in items])


def _parse_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return [str(parsed)]


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["issues"] = _parse_list(data.get("issues"))
    data["sources_used"] = _parse_list(data.get("sources_used"))
    return data


def migrate() -> None:
    """Create the tickets table and add missing columns idempotently."""
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                subject TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                customer_tier TEXT NOT NULL DEFAULT '',
                ticket_id TEXT,
                urgency TEXT,
                department TEXT,
                complexity TEXT,
                sentiment TEXT,
                summary TEXT,
                ai_draft TEXT,
                confidence REAL,
                issues TEXT,
                sources_used TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                resolution_type TEXT,
                final_answer TEXT,
                resolved_by TEXT,
                resolved_at TEXT,
                model_used TEXT
            )
            """
        )

        existing = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)")}
        column_ddl = {
            "subject": "TEXT NOT NULL DEFAULT ''",
            "body": "TEXT NOT NULL DEFAULT ''",
            "customer_tier": "TEXT NOT NULL DEFAULT ''",
            "ticket_id": "TEXT",
            "urgency": "TEXT",
            "department": "TEXT",
            "complexity": "TEXT",
            "sentiment": "TEXT",
            "summary": "TEXT",
            "ai_draft": "TEXT",
            "confidence": "REAL",
            "issues": "TEXT",
            "sources_used": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'pending'",
            "resolution_type": "TEXT",
            "final_answer": "TEXT",
            "resolved_by": "TEXT",
            "resolved_at": "TEXT",
            "model_used": "TEXT",
        }
        for name, ddl in column_ddl.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE tickets ADD COLUMN {name} {ddl}")
        conn.commit()
    except sqlite3.Error:
        logger.exception("Failed to migrate tickets table")
        raise
    finally:
        conn.close()


def create_ticket(
    *,
    subject: str = "",
    body: str = "",
    customer_tier: str = "",
    ticket_id: str | None = None,
) -> int:
    """Create a ticket row and return its database id."""
    conn = _connect()
    try:
        cursor = conn.execute(
            """
            INSERT INTO tickets (created_at, subject, body, customer_tier, ticket_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (_now(), subject or "", body or "", customer_tier or "", ticket_id),
        )
        conn.commit()
        return cursor.lastrowid or 0
    finally:
        conn.close()


def get_ticket(ticket_db_id: int) -> dict[str, Any] | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_db_id,)
        ).fetchone()
        return row_to_dict(row) if row else None
    finally:
        conn.close()


def list_tickets(
    status: str | None = None,
    resolution_type: str | None = None,
) -> list[dict[str, Any]]:
    """Return tickets newest first, optionally filtered by status/resolution type."""
    clauses: list[str] = []
    params: list[str] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if resolution_type:
        clauses.append("resolution_type = ?")
        params.append(resolution_type)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM tickets{where} ORDER BY id DESC", params
        ).fetchall()
        return [row_to_dict(row) for row in rows]
    finally:
        conn.close()


def _validate_update_fields(fields: dict[str, Any]) -> None:
    unknown = set(fields) - _UPDATEABLE_COLUMNS
    if unknown:
        raise ValueError(f"Unknown ticket fields: {sorted(unknown)}")


def update_ticket(ticket_db_id: int, **fields: Any) -> None:
    """Update a subset of ticket columns.

    JSON-serializable list fields may be passed as Python lists and are
    encoded into their TEXT storage form automatically.
    """
    _validate_update_fields(fields)

    encoded = dict(fields)
    if "issues" in encoded:
        encoded["issues"] = _dumps_list(encoded["issues"])
    if "sources_used" in encoded:
        encoded["sources_used"] = _dumps_list(encoded["sources_used"])

    assignments = ", ".join(f"{name} = ?" for name in encoded)
    values = list(encoded.values()) + [ticket_db_id]

    conn = _connect()
    try:
        conn.execute(
            f"UPDATE tickets SET {assignments} WHERE id = ?",
            values,
        )
        conn.commit()
    finally:
        conn.close()


def finalize_ticket(
    ticket_db_id: int,
    *,
    classification: Any = None,
    draft: Any = None,
    confidence: float | None = None,
    issues: list[str] | None = None,
    sources_used: list[str] | None = None,
    model_used: str | None = None,
    status: str = PENDING,
    resolution_type: str | None = None,
    final_answer: str | None = None,
    resolved_by: str | None = None,
    resolved_at: str | None = None,
) -> None:
    """Persist graph-produced metadata and lifecycle state in one update."""
    classification_dict = _dump(classification) or {}
    draft_dict = _dump(draft) or {}

    if resolution_type not in ALLOWED_RESOLUTION_TYPES:
        raise ValueError(f"Invalid resolution_type: {resolution_type!r}")

    conn = _connect()
    try:
        conn.execute(
            """
            UPDATE tickets
            SET urgency = ?,
                department = ?,
                complexity = ?,
                sentiment = ?,
                summary = ?,
                ai_draft = ?,
                confidence = ?,
                issues = ?,
                sources_used = ?,
                model_used = ?,
                status = ?,
                resolution_type = ?,
                final_answer = ?,
                resolved_by = ?,
                resolved_at = ?
            WHERE id = ?
            """,
            (
                classification_dict.get("urgency"),
                classification_dict.get("department"),
                classification_dict.get("complexity"),
                classification_dict.get("sentiment"),
                classification_dict.get("summary"),
                draft_dict.get("reply"),
                confidence,
                _dumps_list(issues or []),
                _dumps_list(sources_used or []),
                model_used,
                status,
                resolution_type,
                final_answer,
                resolved_by,
                resolved_at,
                ticket_db_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()