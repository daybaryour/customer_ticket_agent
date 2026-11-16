"""LangGraph support workflow: triage -> retrieve -> draft -> score -> route.

Pipeline:
    classify -> retrieve -> draft_resolution -> score_confidence -> route

The ``route`` node populates ``decision``. A conditional edge then sends the
ticket to ``auto_reply`` only when the generated reply is safe and confident;
otherwise it is sent to ``escalate``, which writes a handoff note, queues the
item, and alerts Slack.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, TypedDict

from dotenv import load_dotenv
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load backend/.env so defaults behave the same regardless of CWD.
load_dotenv(os.path.join(BASE_DIR, ".env"))

CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.7"))

# ---------------------------------------------------------------------------
# Imports with run as script vs. package fallback, matching llm.py / settings.py.
# ---------------------------------------------------------------------------
try:
    from llm import (
        DEFAULT_MODEL,
        DEFAULT_PROVIDER,
        build,
        fallback_pair,
        get_chain,
    )
    from prompts import (
        CLASSIFICATION_PROMPT,
        CONFIDENCE_PROMPT,
        ESCALATION_PROMPT,
        RESOLUTION_PROMPT,
        Classification,
        Escalation,
        Resolution,
        Score,
        overall_confidence,
    )
    from rag import retrieve as retrieve_docs
    import integrations
    import ticket_store
except ImportError:  # pragma: no cover - package-relative fallback
    from .llm import (
        DEFAULT_MODEL,
        DEFAULT_PROVIDER,
        build,
        fallback_pair,
        get_chain,
    )
    from .prompts import (
        CLASSIFICATION_PROMPT,
        CONFIDENCE_PROMPT,
        ESCALATION_PROMPT,
        RESOLUTION_PROMPT,
        Classification,
        Escalation,
        Resolution,
        Score,
        overall_confidence,
    )
    from .rag import retrieve as retrieve_docs
    from . import integrations
    from . import ticket_store


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
class SupportState(TypedDict, total=False):
    """Shared state for the support-resolution graph."""

    ticket: str
    classification: Classification
    context: str
    draft: Resolution
    confidence: float
    issues: list[str]
    decision: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_llm_pref(config: RunnableConfig) -> tuple[str, str] | None:
    """Return the user/model preference from LangGraph runtime config.

    Expected shape: ``{"configurable": {"llm_pref": ("provider", "model")}}``.
    Returning ``None`` lets ``get_chain`` fall back to the global defaults.
    """
    configurable = config.get("configurable") or {}
    pref = configurable.get("llm_pref")
    return tuple(pref) if pref else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_llm_pref(config: RunnableConfig) -> tuple[str, str]:
    """Return the effective provider/model preference for a graph run.

    Precedence: runtime ``llm_pref`` config -> module defaults from ``llm.py``.
    """
    return _get_llm_pref(config) or (DEFAULT_PROVIDER, DEFAULT_MODEL)


def _model_used(config: RunnableConfig) -> str:
    """Return a ``provider/model`` label for the LLM configured for this run."""
    provider, model = _resolve_llm_pref(config)
    return f"{provider}/{model}"


def _extract_runtime_ticket_fields(config: RunnableConfig) -> dict[str, Any]:
    """Read optional structured ticket fields from the runtime config.

    The API composes legacy callers down to a single ``ticket`` string, so
    ``subject``, ``body``, ``customer_tier``, and ``ticket_id`` are normally
    supplied through ``configurable`` rather than the state itself.
    """
    configurable = config.get("configurable") or {}
    return {
        "subject": configurable.get("subject", ""),
        "body": configurable.get("body", ""),
        "customer_tier": configurable.get("customer_tier", ""),
        "ticket_id": configurable.get("ticket_id"),
    }


def _ticket_db_id(state: SupportState, config: RunnableConfig) -> int:
    """Return the persisted ticket row id for this run.

    Callers may supply ``configurable["ticket_db_id"]``. When they do not, a
    row is created from the runtime ticket fields so every processed ticket is
    still recorded in the ticket store.
    """
    configurable = config.get("configurable") or {}
    ticket_db_id = configurable.get("ticket_db_id")
    if ticket_db_id is not None:
        return int(ticket_db_id)

    fields = _extract_runtime_ticket_fields(config)
    return ticket_store.create_ticket(
        subject=fields["subject"],
        body=fields["body"] or state.get("ticket", ""),
        customer_tier=fields["customer_tier"],
        ticket_id=fields["ticket_id"],
    )


def _save_ticket(
    state: SupportState,
    config: RunnableConfig,
    *,
    status: str,
    resolution_type: str | None = None,
    final_answer: str | None = None,
    resolved_by: str | None = None,
    resolved_at: str | None = None,
) -> None:
    """Persist graph metadata and lifecycle state for the current ticket.

    Persistence failures are logged rather than raised: the customer-facing
    work (reply or escalation) has already happened by the time this runs.
    """
    draft = state.get("draft")
    try:
        ticket_store.finalize_ticket(
            _ticket_db_id(state, config),
            classification=state.get("classification"),
            draft=draft,
            confidence=state.get("confidence"),
            issues=state.get("issues"),
            sources_used=getattr(draft, "sources_used", None),
            model_used=_model_used(config),
            status=status,
            resolution_type=resolution_type,
            final_answer=final_answer,
            resolved_by=resolved_by,
            resolved_at=resolved_at,
        )
    except Exception:
        logger.exception("Failed to persist ticket state (status=%s)", status)


def _format_context(hits: list[dict]) -> str:
    """Turn RAG retrieval results into a single LLM-friendly context string."""
    if not hits:
        return "No relevant knowledge base context was found."

    blocks: list[str] = []
    for hit in hits:
        source = hit.get("source", "unknown")
        text = hit.get("text", "")
        score = hit.get("score")
        header = f"[Source: {source}]"
        if score is not None:
            header = f"[Source: {source} | score: {score:.4f}]"
        blocks.append(f"{header}\n{text}")
    return "\n\n".join(blocks)


def _meets_auto_reply_criteria(state: SupportState) -> bool:
    """Return True when the reply is safe for automated customer contact."""
    classification = state.get("classification")
    draft = state.get("draft")
    confidence = state.get("confidence")

    return (
        confidence is not None
        and confidence >= CONFIDENCE_THRESHOLD
        and bool(getattr(draft, "can_resolve", False))
        and getattr(classification, "complexity", None) == "simple"
        and getattr(classification, "urgency", None) != "critical"
    )


def _path_after_route(state: SupportState) -> str:
    """Conditional-edge function used after the ``route`` node."""
    return "auto_reply" if _meets_auto_reply_criteria(state) else "escalate"


# ---------------------------------------------------------------------------
# Node implementations
# ---------------------------------------------------------------------------
def classify(state: SupportState, config: RunnableConfig) -> dict[str, Any]:
    """Classify urgency, department, complexity, sentiment, and entities."""
    pref = _get_llm_pref(config)
    chain = get_chain(Classification, pref=pref)
    runnable = CLASSIFICATION_PROMPT | chain
    result = runnable.invoke({"ticket": state["ticket"]})
    return {"classification": result}


def retrieve(state: SupportState, config: RunnableConfig) -> dict[str, Any]:
    """Retrieve relevant knowledge-base chunks using the ticket text."""
    classification = state.get("classification")

    query_parts = [state["ticket"]]
    key_entities = getattr(classification, "key_entities", []) or []
    query_parts.extend(str(entity) for entity in key_entities)
    query = " ".join(part for part in query_parts if part)

    hits = retrieve_docs(query)
    return {"context": _format_context(hits)}


def draft_resolution(state: SupportState, config: RunnableConfig) -> dict[str, Any]:
    """Write a RAG-grounded draft reply using the retrieved context."""
    pref = _get_llm_pref(config)
    chain = get_chain(Resolution, pref=pref)
    runnable = RESOLUTION_PROMPT | chain
    result = runnable.invoke(
        {"ticket": state["ticket"], "context": state["context"]}
    )
    return {"draft": result}


def score_confidence(state: SupportState, config: RunnableConfig) -> dict[str, Any]:
    """Score the draft reply and derive overall confidence + issues."""
    pref = _get_llm_pref(config)
    chain = get_chain(Score, pref=pref)
    runnable = CONFIDENCE_PROMPT | chain
    score = runnable.invoke(
        {
            "ticket": state["ticket"],
            "context": state["context"],
            "reply": state["draft"].reply,
        }
    )
    return {
        "confidence": overall_confidence(score),
        "issues": score.issues,
    }


def route(state: SupportState, config: RunnableConfig) -> dict[str, Any]:
    """Decide whether this ticket can be answered automatically."""
    return {"decision": _path_after_route(state)}


def auto_reply(state: SupportState, config: RunnableConfig) -> dict[str, Any]:
    """Post the draft reply directly to the customer, then resolve the ticket."""
    reply = state["draft"].reply
    integrations.post_reply(ticket=state["ticket"], reply=reply)

    _save_ticket(
        state,
        config,
        status=ticket_store.RESOLVED,
        resolution_type=ticket_store.AUTO,
        final_answer=reply,
        resolved_by="ai",
        resolved_at=_now(),
    )
    return {"decision": "auto_reply"}


def escalate(state: SupportState, config: RunnableConfig) -> dict[str, Any]:
    """Generate a handoff note, queue the item, and alert Slack."""
    pref = _get_llm_pref(config)
    chain = get_chain(Escalation, pref=pref)
    runnable = ESCALATION_PROMPT | chain
    note = runnable.invoke(
        {"ticket": state["ticket"], "reply": state["draft"].reply}
    )

    item = integrations.queue_escalation(
        ticket=state["ticket"],
        classification=state["classification"],
        draft=state["draft"],
        escalation=note,
        issues=state.get("issues", []),
        confidence=state.get("confidence"),
    )
    integrations.send_slack_alert(item)

    _save_ticket(state, config, status=ticket_store.PENDING)
    return {"decision": "escalate"}


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------
def build_graph() -> Any:
    """Construct and compile the support workflow graph."""
    builder = StateGraph(SupportState)

    builder.add_node("classify", classify)
    builder.add_node("retrieve", retrieve)
    builder.add_node("draft_resolution", draft_resolution)
    builder.add_node("score_confidence", score_confidence)
    builder.add_node("route", route)
    builder.add_node("auto_reply", auto_reply)
    builder.add_node("escalate", escalate)

    builder.add_edge(START, "classify")
    builder.add_edge("classify", "retrieve")
    builder.add_edge("retrieve", "draft_resolution")
    builder.add_edge("draft_resolution", "score_confidence")
    builder.add_edge("score_confidence", "route")

    builder.add_conditional_edges(
        "route",
        _path_after_route,
        {
            "auto_reply": "auto_reply",
            "escalate": "escalate",
        },
    )

    builder.add_edge("auto_reply", END)
    builder.add_edge("escalate", END)

    return builder.compile()


# Exported compiled graph.
graph = build_graph()