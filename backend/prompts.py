"""Prompt templates and structured-output schemas for the Acme support bot."""

from __future__ import annotations

from typing import List, Literal

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

CLASSIFICATION_PROMPT = ChatPromptTemplate.from_template(
    """You are an AI support triage assistant for Acme, a SaaS company.
Classify the support ticket below.

Rules:
- "Critical" urgency means outage, data loss, security issue, legal threat, or payment failure.
- When unsure between two levels, choose the higher urgency and higher complexity.
- sentiment should be a short label such as "frustrated", "neutral", "angry", "confused", "urgent", or "grateful".
- key_entities should include product names, plan names, error codes, dates, and people when present.

Ticket:
{ticket}

Return the structured classification with: urgency, department, complexity, sentiment, key_entities, summary, reasoning."""
)

RESOLUTION_PROMPT = ChatPromptTemplate.from_template(
    """You are an AI support assistant for Acme, a SaaS company.
Answer ONLY from the provided context below.

Rules:
- Never invent policies, prices, links, timelines, or features.
- If the context does not contain enough information to fully answer, set can_resolve to false and list what is missing in missing_info.
- When you can answer, cite the source files you actually used in sources_used.
- reply should be a clear, concise answer for the customer.

Context:
{context}

Ticket:
{ticket}

Return: can_resolve, reply, sources_used, missing_info."""
)

CONFIDENCE_PROMPT = ChatPromptTemplate.from_template(
    """You are evaluating the quality and safety of an AI-generated support reply.

Score each dimension from 0.0 (terrible) to 1.0 (perfect):
- grounded: every factual claim is supported by the context; no invented policies, prices, or links.
- complete: the reply fully addresses the customer's question and includes all necessary details.
- tone: the reply is professional, empathetic, and on-brand.
- risk: 1.0 means zero risk of misleading the customer; lower scores mean higher risk.

Also return issues: a list of specific problems found in the reply, or an empty list if none.

Ticket:
{ticket}

Context:
{context}

Reply:
{reply}

Return: grounded, complete, tone, risk, issues."""
)

ESCALATION_PROMPT = ChatPromptTemplate.from_template(
    """You are an AI support assistant preparing an escalation summary for a human agent.
Keep the whole summary under 100 words.

Ticket:
{ticket}

Resolution attempt:
{reply}

Return: issue, sentiment, what_was_tried, recommended_action, suggested_department."""
)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class Classification(BaseModel):
    """Structured output for ticket triage."""

    urgency: Literal["low", "medium", "high", "critical"] = Field(
        description="How urgent the ticket is. 'critical' means outage, data loss, security issue, legal threat, or payment failure."
    )
    department: Literal["billing", "technical", "account", "sales", "general"] = Field(
        description="Which department should own the ticket."
    )
    complexity: Literal["simple", "moderate", "complex"] = Field(
        description="How much effort or expertise the ticket will likely require."
    )
    sentiment: str = Field(description="Short sentiment label, e.g. frustrated, neutral, angry, confused, urgent, grateful.")
    key_entities: List[str] = Field(description="Product names, plan names, error codes, dates, and people present in the ticket.")
    summary: str = Field(description="One or two sentence summary of the ticket.")
    reasoning: str = Field(description="Why these classification values were chosen.")


class Resolution(BaseModel):
    """Structured output for a RAG-grounded answer."""

    can_resolve: bool = Field(description="True only if the context fully answers the ticket.")
    reply: str = Field(description="Customer-facing reply drawn solely from the context.")
    sources_used: List[str] = Field(default_factory=list, description="Source filenames actually used to build the reply.")
    missing_info: List[str] = Field(default_factory=list, description="What information is missing if can_resolve is false.")


class Score(BaseModel):
    """Structured output for confidence scoring."""

    grounded: float = Field(ge=0.0, le=1.0, description="Groundedness: every factual claim is supported by context.")
    complete: float = Field(ge=0.0, le=1.0, description="Completeness: the reply fully addresses the ticket.")
    tone: float = Field(ge=0.0, le=1.0, description="Tone: professional, empathetic, and on-brand.")
    risk: float = Field(ge=0.0, le=1.0, description="Risk factor: 1.0 means zero risk of misleading the customer.")
    issues: List[str] = Field(default_factory=list, description="Specific problems found in the reply, if any.")


class Escalation(BaseModel):
    """Structured output for human-agent escalation."""

    issue: str = Field(description="One sentence describing the customer's problem.")
    sentiment: str = Field(description="Short sentiment label for the customer.")
    what_was_tried: str = Field(description="What the AI already attempted to resolve the issue.")
    recommended_action: str = Field(description="Next step for the human agent to take.")
    suggested_department: str = Field(description="Department that should likely handle the escalation.")


# ---------------------------------------------------------------------------
# Confidence helper
# ---------------------------------------------------------------------------

def overall_confidence(score: Score) -> float:
    """Weighted confidence score: 0.4*grounded + 0.3*complete + 0.1*tone + 0.2*risk."""
    return 0.4 * score.grounded + 0.3 * score.complete + 0.1 * score.tone + 0.2 * score.risk
