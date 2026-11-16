"""FastAPI application for the Acme support workflow.

Exposes webhook ingestion (Zendesk and email), the persisted ticket lifecycle
(list, detail, approve, resolve, reject, and stats), per-user model settings,
model listing, and a test-ticket runner.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent

# Storage location defaults to the backend/ directory next to the code. Docker
# sets ESCALATIONS_DB_PATH/TICKETS_DB_PATH/KB_DB_PATH/SETTINGS_PATH so that all
# mutable state lands inside the mounted /app/data volume.
DB_PATH = Path(os.getenv("ESCALATIONS_DB_PATH", str(BASE_DIR / "escalations.sqlite3")))

# Load backend/.env so settings like DRY_RUN and fallback flags work
# consistently no matter how the app is started.
load_dotenv(BASE_DIR / ".env")

# Absolute imports work when starting with `uvicorn main:app` from backend/;
# the relative fallback keeps the package import happy too.
try:
    import integrations
    import llm
    import rag
    import settings as user_settings
    import ticket_store
    from graph import graph as support_graph
except ImportError:  # pragma: no cover - package-relative fallback
    from . import integrations
    from . import llm
    from . import rag
    from . import settings as user_settings
    from . import ticket_store
    from .graph import graph as support_graph

logger = logging.getLogger("acme_support_api")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class TestTicketIn(BaseModel):
    subject: str
    body: str
    tier: str


class ApproveIn(BaseModel):
    reply: str | None = None          # Optional edited draft; omit to send original
    ticket_id: str | None = None      # Optional override, e.g. a Zendesk ticket id


class RejectIn(BaseModel):
    reason: str | None = None


class ResolveIn(BaseModel):
    answer: str | None = None         # Optional final answer; falls back to approved draft


class TicketApproveIn(BaseModel):
    reply: str | None = None          # Optional edited draft; omit to approve ai_draft
    resolved_by: str = "agent"        # Who handled the approval


class TicketResolveIn(BaseModel):
    answer: str                       # Agent-written answer, since no usable draft exists
    resolved_by: str = "agent"        # Who handled the resolution


class SettingsIn(BaseModel):
    provider: str
    model: str


# ---------------------------------------------------------------------------
# SQLite helpers + migration
# ---------------------------------------------------------------------------

_EXTRA_COLUMNS: dict[str, str] = {
    "status": "TEXT NOT NULL DEFAULT 'pending'",
    "ticket_id": "TEXT",
    "final_reply": "TEXT",
    "approved_at": "TEXT",
    "resolved_answer": "TEXT",
    "resolved_at": "TEXT",
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migrate() -> None:
    """Ensure the escalations table has the review/workflow columns.

    The graph's ``integrations.queue_escalation`` owns the base table schema;
    this migration only adds columns needed by the API (status, ticket id,
    approved/reviewed timestamps, and resolved-answer storage). It is
    idempotent so it can run on every startup.
    """
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS escalations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                ticket TEXT NOT NULL,
                urgency TEXT,
                complexity TEXT,
                department TEXT,
                summary TEXT,
                issue TEXT,
                sentiment TEXT,
                what_was_tried TEXT,
                recommended_action TEXT,
                suggested_department TEXT,
                reply_attempt TEXT,
                confidence REAL,
                issues TEXT
            )
            """
        )

        existing = {row["name"] for row in conn.execute("PRAGMA table_info(escalations)")}
        for name, ddl in _EXTRA_COLUMNS.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE escalations ADD COLUMN {name} {ddl}")
        conn.commit()
    except sqlite3.Error:
        logger.exception("Failed to migrate escalations table")
        raise
    finally:
        conn.close()


def _parse_issues(raw: str | None) -> list[str]:
    """Parse the ``issues`` JSON column into a list of strings."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return [str(parsed)]


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["issues"] = _parse_issues(data.get("issues"))
    return data


def _get_escalation_row(conn: sqlite3.Connection, escalation_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM escalations WHERE id = ?", (escalation_id,)
    ).fetchone()


def _latest_escalation_id() -> int | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT MAX(id) FROM escalations").fetchone()
        return row[0] if row and row[0] is not None else None
    finally:
        conn.close()


def _attach_ticket_id(ticket_id: str) -> None:
    """Attach the originating ticket id to the most recently queued escalation."""
    escalation_id = _latest_escalation_id()
    if escalation_id is None:
        return
    conn = _connect()
    try:
        conn.execute(
            "UPDATE escalations SET ticket_id = ? WHERE id = ?",
            (ticket_id, escalation_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Payload parsing / ticket construction
# ---------------------------------------------------------------------------

def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        body = value.get("body") or value.get("text") or value.get("description") or ""
        return _clean(body)
    return str(value)


def _parse_zendesk(payload: dict) -> tuple[str | None, str, str]:
    """Return ``(ticket_id, subject, body)`` from common Zendesk webhook shapes."""
    ticket_obj = payload.get("ticket")
    if not isinstance(ticket_obj, dict):
        ticket_obj = payload if isinstance(payload, dict) else {}

    ticket_id = payload.get("ticket_id") or payload.get("id") or ticket_obj.get("id")
    ticket_id = str(ticket_id) if ticket_id is not None else None

    subject = _clean(payload.get("subject") or ticket_obj.get("subject"))

    comment = ticket_obj.get("comment")
    if isinstance(comment, dict):
        body = _clean(comment.get("body") or comment.get("text") or comment.get("description"))
    elif comment:
        body = _clean(comment)
    else:
        body = _clean(
            payload.get("body")
            or payload.get("description")
            or ticket_obj.get("description")
        )

    return ticket_id, subject, body


def _parse_email(payload: dict) -> tuple[str | None, str, str, str]:
    """Return ``(email_id, sender, subject, body)`` from common email webhook shapes."""
    email_id = (
        payload.get("email_id")
        or payload.get("message_id")
        or payload.get("message-id")
        or payload.get("id")
    )
    email_id = str(email_id) if email_id is not None else None
    sender = _clean(payload.get("from") or payload.get("sender") or payload.get("from_addr"))
    subject = _clean(payload.get("subject") or payload.get("title"))
    body = _clean(payload.get("body") or payload.get("text") or payload.get("message") or payload.get("html"))
    return email_id, sender, subject, body


def _compose_ticket(
    subject: str = "",
    body: str = "",
    *,
    sender: str = "",
    ticket_id: str | None = None,
    email_id: str | None = None,
    tier: str = "",
) -> str:
    lines: list[str] = []
    if ticket_id:
        lines.append(f"Ticket ID: {ticket_id}")
    if email_id:
        lines.append(f"Email ID: {email_id}")
    if sender:
        lines.append(f"From: {sender}")
    if subject:
        lines.append(f"Subject: {subject}")
    if tier:
        lines.append(f"Priority tier: {tier}")
    lines.append("")
    lines.append(body or "")
    return "\n".join(lines)


def _split_ticket(ticket: str) -> tuple[str, str]:
    """Split a composed ticket string into (subject, body).

    The graph and webhook handlers build tickets with a ``Subject:`` line and
    a blank line, followed by the customer message. This helper is tolerant of
    tickets that never had a subject line.
    """
    if not ticket:
        return "", ""

    subject = ""
    body_start: int | None = None
    lines = ticket.splitlines()

    for idx, line in enumerate(lines):
        if not line.strip():
            body_start = idx
            break
        if line.lower().startswith("subject:"):
            subject = line.split(":", 1)[1].strip()

    if body_start is not None:
        body = "\n".join(lines[body_start + 1:]).strip()
    else:
        body = ticket.strip()

    return subject, body


def _retrieve_sources(ticket: str) -> list[dict[str, Any]]:
    """Retrieve matching knowledge-base sources for an escalation.

    RAG retrieval can fail when Ollama/Chroma is unavailable; the dashboard
    should still be able to render the escalation itself, so failures here are
    logged and returned as an empty list.
    """
    try:
        return [
            {
                "source": hit.get("source", "unknown"),
                "score": hit.get("score"),
                "text": hit.get("text", ""),
            }
            for hit in rag.retrieve(ticket)
        ]
    except Exception:
        logger.exception("RAG retrieval failed while fetching escalation sources")
        return []


# ---------------------------------------------------------------------------
# Graph execution
# ---------------------------------------------------------------------------

def _default_llm_pref() -> tuple[str, str] | None:
    """Return the default user's LLM preference, or None if unavailable."""
    try:
        pref = user_settings.get_pref("default")
        return tuple(pref) if pref else None
    except Exception:
        logger.exception("Unable to resolve default LLM preference; using graph defaults")
        return None


def _dump(value: Any) -> Any:
    """Serialize Pydantic models and other graph state into plain Python."""
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    if isinstance(value, (list, tuple)):
        return [_dump(item) for item in value]
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _run_graph(ticket: str) -> dict[str, Any]:
    pref = _default_llm_pref()
    config: dict[str, Any] = {"configurable": {}}
    if pref:
        config["configurable"]["llm_pref"] = pref
    return support_graph.invoke({"ticket": ticket}, config=config)


def _run_and_report(ticket: str, *, attach_ticket_id: str | None = None) -> dict[str, Any]:
    """Run the graph and build a JSON-safe response dict."""
    try:
        state = _run_graph(ticket)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Graph execution failed")
        raise HTTPException(
            status_code=500, detail=f"Graph execution failed: {exc}"
        ) from exc

    decision = state.get("decision")
    if decision == "escalate" and attach_ticket_id:
        _attach_ticket_id(attach_ticket_id)

    return {
        "decision": decision,
        "classification": _dump(state.get("classification")),
        "draft": _dump(state.get("draft")),
        "confidence": state.get("confidence"),
        "issues": _dump(state.get("issues", [])),
        "ticket": state.get("ticket"),
    }


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(_: FastAPI):
    _migrate()
    ticket_store.migrate()
    yield


app = FastAPI(title="Acme Support API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook/zendesk")
def zendesk_webhook(payload: dict = Body(...)) -> dict[str, Any]:
    """Run the support graph for a Zendesk ticket webhook."""
    ticket_id, subject, body = _parse_zendesk(payload)
    ticket = _compose_ticket(subject=subject, body=body, ticket_id=ticket_id)
    result = _run_and_report(ticket, attach_ticket_id=ticket_id)
    return {"source": "zendesk", "ticket_id": ticket_id, **result}


@app.post("/webhook/email")
def email_webhook(payload: dict = Body(...)) -> dict[str, Any]:
    """Run the support graph for an inbound email webhook."""
    # If the payload already contains a composed ticket (e.g. from poll_emails),
    # use it directly; otherwise build one from the common webhook fields.
    if payload.get("ticket"):
        ticket = _clean(payload.get("ticket"))
        result = _run_and_report(ticket)
        return {"source": "email", **result}

    email_id, sender, subject, body = _parse_email(payload)
    ticket = _compose_ticket(subject=subject, body=body, sender=sender, email_id=email_id)
    result = _run_and_report(ticket)
    return {"source": "email", "email_id": email_id, **result}


@app.post("/test-ticket")
def test_ticket(payload: TestTicketIn) -> dict[str, Any]:
    """Run the graph for an arbitrary ticket without touching Zendesk."""
    ticket = _compose_ticket(subject=payload.subject, body=payload.body, tier=payload.tier)
    result = _run_and_report(ticket)
    return {"source": "test-ticket", "input": payload.model_dump(), **result}


# ---------------------------------------------------------------------------
# Ticket lifecycle helpers
# ---------------------------------------------------------------------------

_TICKET_STATUS_FILTERS = {
    ticket_store.PENDING,
    ticket_store.RESOLVED,
    ticket_store.REJECTED,
    "all",
}


def _require_ticket(ticket_db_id: int) -> dict[str, Any]:
    """Fetch a ticket row or raise a 404."""
    row = ticket_store.get_ticket(ticket_db_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_db_id} not found")
    return row


def _ticket_text(row: dict[str, Any]) -> str:
    """Recompose the original ticket string from the stored ticket columns."""
    return _compose_ticket(
        subject=row.get("subject") or "",
        body=row.get("body") or "",
        ticket_id=row.get("ticket_id"),
        tier=row.get("customer_tier") or "",
    ).strip()


def _send_ticket_reply(row: dict[str, Any], reply: str) -> dict[str, Any]:
    """Reply through Zendesk when an external id exists, else the mock sender."""
    ticket_id = row.get("ticket_id")
    if ticket_id:
        return integrations.zendesk_reply(str(ticket_id), reply)
    return integrations.post_reply(_ticket_text(row), reply)


def _add_resolution_to_kb(row: dict[str, Any], answer: str) -> dict[str, Any]:
    """Save an agent-written answer into the Chroma knowledge base."""
    try:
        return rag.add_resolution(_ticket_text(row), answer)
    except Exception as exc:
        logger.exception("Failed to save resolution to Chroma")
        raise HTTPException(
            status_code=500, detail=f"Failed to save resolution: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Ticket lifecycle endpoints
# ---------------------------------------------------------------------------

@app.get("/tickets")
def list_tickets(
    status: str = "all",
    resolution_type: str | None = None,
) -> list[dict[str, Any]]:
    """Return tickets newest first, filtered by status and resolution type.

    ``status`` accepts pending, resolved, rejected, or all (the default).
    """
    if status not in _TICKET_STATUS_FILTERS:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of {sorted(_TICKET_STATUS_FILTERS)}",
        )
    if (
        resolution_type is not None
        and resolution_type not in ticket_store.ALLOWED_RESOLUTION_TYPES
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "resolution_type must be one of "
                f"{sorted(t for t in ticket_store.ALLOWED_RESOLUTION_TYPES if t)}"
            ),
        )

    return ticket_store.list_tickets(
        status=None if status == "all" else status,
        resolution_type=resolution_type,
    )


@app.get("/tickets/{ticket_db_id}")
def get_ticket(ticket_db_id: int) -> dict[str, Any]:
    """Return the full stored record for one ticket."""
    return _require_ticket(ticket_db_id)


@app.post("/tickets/{ticket_db_id}/approve")
def approve_ticket(
    ticket_db_id: int,
    payload: TicketApproveIn = TicketApproveIn(),
) -> dict[str, Any]:
    """Send the AI draft or an edited reply, then mark the ticket resolved.

    A reply identical to ``ai_draft`` is recorded as ``manual_approved``;
    anything else is recorded as ``manual_edited`` and saved to the knowledge
    base.
    """
    row = _require_ticket(ticket_db_id)
    if row["status"] != ticket_store.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"Ticket {ticket_db_id} cannot be approved (status: {row['status']})",
        )

    draft = (row.get("ai_draft") or "").strip()
    reply = (payload.reply if payload.reply is not None else draft).strip()
    if not reply:
        raise HTTPException(
            status_code=400,
            detail="No reply available to send; provide a reply or approve a ticket with an AI draft",
        )

    resolution_type = (
        ticket_store.MANUAL_APPROVED if reply == draft else ticket_store.MANUAL_EDITED
    )

    send_result = _send_ticket_reply(row, reply)
    if not send_result.get("ok"):
        raise HTTPException(
            status_code=502,
            detail={"message": "Failed to send approved reply", "send_result": send_result},
        )

    saved = None
    if resolution_type == ticket_store.MANUAL_EDITED:
        saved = _add_resolution_to_kb(row, reply)

    resolved_at = _now()
    ticket_store.update_ticket(
        ticket_db_id,
        status=ticket_store.RESOLVED,
        resolution_type=resolution_type,
        final_answer=reply,
        resolved_by=payload.resolved_by,
        resolved_at=resolved_at,
    )

    return {
        "id": ticket_db_id,
        "status": ticket_store.RESOLVED,
        "resolution_type": resolution_type,
        "final_answer": reply,
        "resolved_by": payload.resolved_by,
        "resolved_at": resolved_at,
        "send_result": send_result,
        "saved": saved,
    }


@app.post("/tickets/{ticket_db_id}/resolve")
def resolve_ticket(
    ticket_db_id: int,
    payload: TicketResolveIn,
) -> dict[str, Any]:
    """Resolve a ticket with an agent-written answer and save it to the KB."""
    row = _require_ticket(ticket_db_id)
    if row["status"] == ticket_store.RESOLVED:
        raise HTTPException(
            status_code=409,
            detail=f"Ticket {ticket_db_id} is already resolved",
        )
    if row["status"] == ticket_store.REJECTED:
        raise HTTPException(
            status_code=409,
            detail=f"Ticket {ticket_db_id} was rejected and cannot be resolved",
        )

    answer = (payload.answer or "").strip()
    if not answer:
        raise HTTPException(
            status_code=400, detail="An answer is required to resolve a ticket"
        )

    saved = _add_resolution_to_kb(row, answer)

    resolved_at = _now()
    ticket_store.update_ticket(
        ticket_db_id,
        status=ticket_store.RESOLVED,
        resolution_type=ticket_store.MANUAL_WRITTEN,
        final_answer=answer,
        resolved_by=payload.resolved_by,
        resolved_at=resolved_at,
    )

    return {
        "id": ticket_db_id,
        "status": ticket_store.RESOLVED,
        "resolution_type": ticket_store.MANUAL_WRITTEN,
        "final_answer": answer,
        "resolved_by": payload.resolved_by,
        "resolved_at": resolved_at,
        "saved": saved,
    }


@app.post("/tickets/{ticket_db_id}/reject")
def reject_ticket(ticket_db_id: int, payload: RejectIn = RejectIn()) -> dict[str, Any]:
    """Reject a pending ticket without sending a reply."""
    row = _require_ticket(ticket_db_id)
    if row["status"] != ticket_store.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"Ticket {ticket_db_id} cannot be rejected (status: {row['status']})",
        )

    resolved_at = _now()
    ticket_store.update_ticket(
        ticket_db_id,
        status=ticket_store.REJECTED,
        resolved_at=resolved_at,
    )

    return {
        "id": ticket_db_id,
        "status": ticket_store.REJECTED,
        "resolved_at": resolved_at,
        "reason": payload.reason,
    }


@app.get("/stats")
def get_ticket_stats() -> dict[str, Any]:
    """Return lifecycle counts plus the share of tickets resolved automatically."""
    rows = ticket_store.list_tickets()
    total = len(rows)
    auto_resolved = sum(
        1
        for row in rows
        if row["status"] == ticket_store.RESOLVED
        and row["resolution_type"] == ticket_store.AUTO
    )
    manual_resolved = sum(
        1
        for row in rows
        if row["status"] == ticket_store.RESOLVED
        and row["resolution_type"] in ticket_store.MANUAL_RESOLUTION_TYPES
    )

    return {
        "total": total,
        "pending": sum(1 for row in rows if row["status"] == ticket_store.PENDING),
        "auto_resolved": auto_resolved,
        "manual_resolved": manual_resolved,
        "rejected": sum(1 for row in rows if row["status"] == ticket_store.REJECTED),
        "auto_resolution_rate": round(auto_resolved / total, 4) if total else 0.0,
    }


@app.get("/escalations")
def list_escalations() -> list[dict[str, Any]]:
    """Alias for ``GET /tickets?status=pending`` -- the human review queue."""
    return ticket_store.list_tickets(status=ticket_store.PENDING)


@app.get("/escalations/{escalation_id}")
def get_escalation(escalation_id: int) -> dict[str, Any]:
    """Return a single escalation enriched with parsed subject/body and sources."""
    conn = _connect()
    try:
        row = _get_escalation_row(conn, escalation_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Escalation {escalation_id} not found")

        data = _row_to_dict(row)
        subject, body = _split_ticket(data.get("ticket") or "")
        data["subject"] = subject
        data["body"] = body
        data["sources"] = _retrieve_sources(data.get("ticket") or "")
        return data
    finally:
        conn.close()


@app.post("/escalations/{escalation_id}/approve")
def approve_escalation(escalation_id: int, payload: ApproveIn = ApproveIn()) -> dict[str, Any]:
    """Send the edited or original draft, then mark the escalation approved."""
    conn = _connect()
    try:
        row = _get_escalation_row(conn, escalation_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Escalation {escalation_id} not found")

        current_status = row["status"]
        if current_status != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"Escalation {escalation_id} cannot be approved (status: {current_status})",
            )

        reply = (payload.reply or row["reply_attempt"] or "").strip()
        ticket_id = payload.ticket_id or row["ticket_id"]

        if ticket_id:
            send_result = integrations.zendesk_reply(str(ticket_id), reply)
        else:
            send_result = integrations.post_reply(row["ticket"] or "", reply)

        if not send_result.get("ok"):
            raise HTTPException(
                status_code=502,
                detail={"message": "Failed to send approved reply", "send_result": send_result},
            )

        conn.execute(
            """
            UPDATE escalations
            SET status = 'approved',
                final_reply = ?,
                approved_at = ?,
                ticket_id = COALESCE(ticket_id, ?)
            WHERE id = ?
            """,
            (reply, _now(), str(ticket_id) if ticket_id else None, escalation_id),
        )
        conn.commit()

        return {
            "id": escalation_id,
            "status": "approved",
            "reply": reply,
            "send_result": send_result,
        }
    finally:
        conn.close()


@app.post("/escalations/{escalation_id}/reject")
def reject_escalation(escalation_id: int, payload: RejectIn = RejectIn()) -> dict[str, Any]:
    """Reject an escalation without sending the draft."""
    conn = _connect()
    try:
        row = _get_escalation_row(conn, escalation_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Escalation {escalation_id} not found")

        current_status = row["status"]
        if current_status != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"Escalation {escalation_id} cannot be rejected (status: {current_status})",
            )

        conn.execute(
            "UPDATE escalations SET status = 'rejected' WHERE id = ?",
            (escalation_id,),
        )
        conn.commit()

        return {"id": escalation_id, "status": "rejected", "reason": payload.reason}
    finally:
        conn.close()


@app.post("/escalations/{escalation_id}/resolve")
def resolve_escalation(escalation_id: int, payload: ResolveIn = ResolveIn()) -> dict[str, Any]:
    """Save the final answer into the RAG knowledge base."""
    conn = _connect()
    try:
        row = _get_escalation_row(conn, escalation_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Escalation {escalation_id} not found")

        current_status = row["status"]
        if current_status == "resolved":
            raise HTTPException(
                status_code=409,
                detail=f"Escalation {escalation_id} is already resolved",
            )
        if current_status == "rejected":
            raise HTTPException(
                status_code=409,
                detail=f"Escalation {escalation_id} was rejected and cannot be resolved",
            )

        answer = (payload.answer or row["final_reply"] or row["reply_attempt"] or "").strip()
        if not answer:
            raise HTTPException(
                status_code=400,
                detail="No answer available to save; provide an answer or approve the draft first",
            )

        try:
            saved = rag.add_resolution(row["ticket"] or "", answer)
        except Exception as exc:
            logger.exception("Failed to save resolution to Chroma")
            raise HTTPException(
                status_code=500, detail=f"Failed to save resolution: {exc}"
            ) from exc

        conn.execute(
            """
            UPDATE escalations
            SET status = 'resolved',
                resolved_answer = ?,
                resolved_at = ?
            WHERE id = ?
            """,
            (answer, _now(), escalation_id),
        )
        conn.commit()

        return {
            "id": escalation_id,
            "status": "resolved",
            "answer": answer,
            "saved": saved,
        }
    finally:
        conn.close()


@app.get("/models")
def get_models() -> dict[str, Any]:
    """Return the provider/model allowlist and configured defaults."""
    return {
        "available_models": llm.AVAILABLE_MODELS,
        "default_provider": llm.DEFAULT_PROVIDER,
        "default_model": llm.DEFAULT_MODEL,
        "fallback_provider": llm.FALLBACK_PROVIDER,
        "fallback_model": llm.FALLBACK_MODEL,
        "fallback_enabled": llm.FALLBACK_ENABLED,
    }


@app.get("/settings/{user_id}")
def get_settings(user_id: str) -> dict[str, str]:
    """Return the effective provider/model preference for a user."""
    try:
        provider, model = user_settings.get_pref(user_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Unable to resolve settings: {exc}"
        ) from exc
    return {"user_id": user_id, "provider": provider, "model": model}


@app.put("/settings/{user_id}")
def put_settings(user_id: str, payload: SettingsIn) -> dict[str, str]:
    """Persist a provider/model preference for a user."""
    try:
        provider, model = user_settings.set_pref(user_id, payload.provider, payload.model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"user_id": user_id, "provider": provider, "model": model}