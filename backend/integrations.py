"""External integrations: Zendesk replies, Slack alerts, and an IMAP email poller.

All outgoing calls are guarded and logged. Set DRY_RUN=1 to print intended
payloads instead of performing real network requests. Compatibility wrappers
used by graph.py are kept at the bottom.
"""

from __future__ import annotations

import email
import imaplib
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from email.header import decode_header
from email.message import Message
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger(__name__)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

DRY_RUN = os.getenv("DRY_RUN", "false").strip().lower() in ("1", "true", "yes", "on")

# -- Zendesk ---------------------------------------------------------------
ZENDESK_SUBDOMAIN = os.getenv("ZENDESK_SUBDOMAIN", "").strip()
ZENDESK_EMAIL = os.getenv("ZENDESK_EMAIL", "").strip()
ZENDESK_TOKEN = os.getenv("ZENDESK_TOKEN", "").strip()
ZENDESK_TIMEOUT = int(os.getenv("ZENDESK_TIMEOUT", "30"))

# -- Slack -----------------------------------------------------------------
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "").strip()
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "#support-escalations")

# -- IMAP email poller ------------------------------------------------------
IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("IMAP_USER", "").strip()
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "").strip()
IMAP_MAILBOX = os.getenv("IMAP_MAILBOX", "INBOX")


# ---------------------------------------------------------------------------
# Zendesk
# ---------------------------------------------------------------------------
def zendesk_reply(ticket_id: str, text: str, tags: list[str] | None = None) -> dict[str, Any]:
    """Add a public comment to a Zendesk ticket.

    Uses PUT /api/v2/tickets/{id}.json with basic auth where the username is
    ``{ZENDESK_EMAIL}/token`` and the password is the Zendesk API token.
    """
    tags = tags or []

    url = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2/tickets/{ticket_id}.json"
    payload = {
        "ticket": {
            "comment": {"body": text, "public": True},
            "tags": tags,
        }
    }

    if DRY_RUN:
        print(f"[DRY_RUN] zendesk_reply -> {url} payload={payload}")
        return {"ok": True, "dry_run": True, "url": url, "payload": payload}

    if not (ZENDESK_SUBDOMAIN and ZENDESK_EMAIL and ZENDESK_TOKEN):
        logger.error("Zendesk credentials are incomplete; cannot reply to ticket %s", ticket_id)
        return {"ok": False, "error": "incomplete_zendesk_config"}

    try:
        response = requests.put(
            url,
            json=payload,
            auth=(f"{ZENDESK_EMAIL}/token", ZENDESK_TOKEN),
            timeout=ZENDESK_TIMEOUT,
        )
        response.raise_for_status()
        logger.info("Posted Zendesk reply for ticket %s", ticket_id)
        return {"ok": True, "status_code": response.status_code, "data": response.json()}
    except requests.RequestException:
        logger.exception("Zendesk reply failed for ticket %s", ticket_id)
        return {"ok": False, "error": "zendesk_request_failed"}


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------
def slack_alert(summary: str, ticket_id: str | None = None) -> dict[str, Any]:
    """Send a Slack alert via ``chat_postMessage``."""
    message = f"🚨 *Support escalation alert*\n*Summary:* {summary}"
    if ticket_id:
        message += f"\n*Ticket ID:* {ticket_id}"

    if DRY_RUN:
        print(f"[DRY_RUN] slack_alert -> channel={SLACK_CHANNEL} text={message}")
        return {"ok": True, "dry_run": True, "channel": SLACK_CHANNEL, "text": message}

    if not SLACK_BOT_TOKEN:
        logger.error("SLACK_BOT_TOKEN is not set; cannot send Slack alert")
        return {"ok": False, "error": "missing_slack_token"}

    try:
        from slack_sdk import WebClient

        client = WebClient(token=SLACK_BOT_TOKEN)
        response = client.chat_postMessage(channel=SLACK_CHANNEL, text=message)
        ok = bool(response.get("ok", False))
        if not ok:
            logger.error("Slack chat_postMessage returned not ok: %s", response)
        else:
            logger.info("Slack alert sent for ticket %s", ticket_id)
        return {"ok": ok, "response": response}
    except Exception:
        logger.exception("Slack alert failed for ticket %s", ticket_id)
        return {"ok": False, "error": "slack_request_failed"}


# ---------------------------------------------------------------------------
# IMAP email poller
# ---------------------------------------------------------------------------
def _decode_header_value(value: str | bytes | None) -> str:
    """Decode an RFC 2047 encoded email header into a plain string."""
    if not value:
        return ""
    parts = decode_header(value)
    decoded: list[str] = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def _extract_email_body(message: Message) -> str:
    """Return a best-effort plain-text body from an ``email.message.Message``."""
    body_parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            if content_type == "text/plain":
                body_parts.append(payload.decode(charset, errors="replace"))
            elif content_type == "text/html" and not body_parts:
                # Fall back to HTML only when no plain text snippet has been found.
                body_parts.append(payload.decode(charset, errors="replace"))
    else:
        payload = message.get_payload(decode=True)
        if payload:
            charset = message.get_content_charset() or "utf-8"
            body_parts.append(payload.decode(charset, errors="replace"))
    return "\n".join(part.strip() for part in body_parts if part.strip())


def poll_emails(mark_seen: bool = False, limit: int = 20) -> list[dict[str, Any]]:
    """Poll the configured IMAP mailbox and return new emails as ticket dicts.

    Each dict contains the fields the rest of the support workflow expects:
    ``ticket`` (subject + body), ``subject``, ``body``, ``from``, and
    ``email_id``. The ``ticket_id`` is initially ``None`` because the poller
    turns emails into new tickets rather than reading existing Zendesk IDs.
    """
    if DRY_RUN:
        print(
            f"[DRY_RUN] poll_emails -> host={IMAP_HOST} port={IMAP_PORT} "
            f"user={IMAP_USER} mailbox={IMAP_MAILBOX}"
        )
        return []

    if not (IMAP_HOST and IMAP_USER and IMAP_PASSWORD):
        logger.error("IMAP credentials are incomplete; cannot poll emails")
        return []

    tickets: list[dict[str, Any]] = []
    connection: imaplib.IMAP4_SSL | None = None

    try:
        connection = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        connection.login(IMAP_USER, IMAP_PASSWORD)
        connection.select(IMAP_MAILBOX, readonly=not mark_seen)

        status, data = connection.search(None, "UNSEEN")
        if status != "OK":
            logger.error("IMAP SEARCH failed for mailbox %s: %s", IMAP_MAILBOX, status)
            return []

        ids = data[0].split()
        for num in ids[-limit:]:
            try:
                status, msg_data = connection.fetch(num, "(RFC822)")
                if status != "OK" or not msg_data or msg_data[0] is None:
                    continue

                raw = msg_data[0][1]
                message = (
                    email.message_from_bytes(raw)
                    if isinstance(raw, bytes)
                    else email.message_from_string(str(raw))
                )
                subject = _decode_header_value(message.get("Subject"))
                from_addr = _decode_header_value(message.get("From"))
                body = _extract_email_body(message)
                email_id = (
                    _decode_header_value(message.get("Message-ID"))
                    or f"imap-{num.decode() if isinstance(num, bytes) else num}"
                )

                tickets.append(
                    {
                        "ticket_id": None,
                        "email_id": email_id,
                        "subject": subject,
                        "body": body,
                        "ticket": f"Subject: {subject}\n\n{body}",
                        "from": from_addr,
                        "received_at": _decode_header_value(message.get("Date")),
                    }
                )

                if mark_seen:
                    connection.store(num, "+FLAGS", "\\Seen")
            except Exception:
                logger.exception("Failed to parse IMAP email %s", num)

        return tickets
    except (imaplib.IMAP4.error, OSError):
        logger.exception("IMAP polling failed for host %s", IMAP_HOST)
        return []
    finally:
        if connection is not None:
            try:
                connection.logout()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Compatibility wrappers used by graph.py
# ---------------------------------------------------------------------------
_lock = threading.RLock()
_memory_queue: list[dict[str, Any]] = []
_DB_PATH = Path(
    os.getenv("ESCALATIONS_DB_PATH", str(BASE_DIR / "escalations.sqlite3"))
)  # Same file as main.py; Docker keeps it in the /app/data volume.


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(value: Any) -> Any:
    """Serialize Pydantic models or plain dicts into plain dicts."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    return str(value)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
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
    conn.commit()


def queue_escalation(
    ticket: str,
    classification: Any = None,
    draft: Any = None,
    escalation: Any = None,
    issues: list[str] | None = None,
    confidence: float | None = None,
) -> dict[str, Any]:
    """Persist an escalation to an in-memory queue and SQLite."""
    classification_dict = _dump(classification) or {}
    draft_dict = _dump(draft) or {}
    escalation_dict = _dump(escalation) or {}

    item: dict[str, Any] = {
        "created_at": _now(),
        "ticket": ticket,
        "urgency": classification_dict.get("urgency"),
        "complexity": classification_dict.get("complexity"),
        "department": classification_dict.get("department"),
        "summary": classification_dict.get("summary"),
        "issue": escalation_dict.get("issue"),
        "sentiment": escalation_dict.get("sentiment"),
        "what_was_tried": escalation_dict.get("what_was_tried"),
        "recommended_action": escalation_dict.get("recommended_action"),
        "suggested_department": escalation_dict.get("suggested_department"),
        "reply_attempt": draft_dict.get("reply"),
        "confidence": confidence,
        "issues": json.dumps(issues or []),
    }

    conn: sqlite3.Connection | None = None
    with _lock:
        _memory_queue.append(item)
        item_id = len(_memory_queue)

        try:
            conn = _connect()
            _init_db(conn)
            cursor = conn.execute(
                """
                INSERT INTO escalations (
                    created_at, ticket, urgency, complexity, department, summary,
                    issue, sentiment, what_was_tried, recommended_action,
                    suggested_department, reply_attempt, confidence, issues
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["created_at"],
                    item["ticket"],
                    item["urgency"],
                    item["complexity"],
                    item["department"],
                    item["summary"],
                    item["issue"],
                    item["sentiment"],
                    item["what_was_tried"],
                    item["recommended_action"],
                    item["suggested_department"],
                    item["reply_attempt"],
                    item["confidence"],
                    item["issues"],
                ),
            )
            conn.commit()
            item_id = cursor.lastrowid or item_id
        except sqlite3.Error:
            logger.exception("SQLite escalation queue unavailable; kept in memory only")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        item["id"] = item_id
        return dict(item)


def post_reply(ticket: str, reply: str) -> dict[str, Any]:
    """Compatibility wrapper used by graph.py's ``auto_reply`` node.

    Kept as a lightweight local helper until graph.py is migrated to call
    ``zendesk_reply`` directly with a real Zendesk ticket ID.
    """
    if DRY_RUN:
        print(f"[DRY_RUN] post_reply -> ticket={ticket} reply={reply}")
        return {"ok": True, "dry_run": True, "ticket": ticket, "reply": reply}
    logger.info("post_reply (mock) called for ticket %s", ticket)
    return {"ok": True, "ticket": ticket, "reply": reply}


def send_slack_alert(item: dict[str, Any]) -> dict[str, Any]:
    """Compatibility wrapper for graph.py's previous Slack alert signature."""
    summary = str(item.get("issue") or item.get("summary") or "Support escalation")
    ticket_id = item.get("ticket_id") or item.get("ticket") or item.get("id")
    return slack_alert(summary, str(ticket_id) if ticket_id is not None else None)
