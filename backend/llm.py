"""LLM utilities: model allowlist, chat model builders, and structured-output chains."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent

# Load backend/.env so module-level defaults work regardless of CWD.
load_dotenv(BASE_DIR / ".env")

# Provider -> allowlisted model names.
AVAILABLE_MODELS: dict[str, tuple[str, ...]] = {
    "ollama": ("llama3.1:8b", "qwen2.5:7b", "qwen2.5:14b"),
    "openai": ("gpt-4o-mini", "gpt-4o"),
}

DEFAULT_PROVIDER = os.getenv("DEFAULT_PROVIDER", "ollama")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "llama3.1:8b")
FALLBACK_PROVIDER = os.getenv("FALLBACK_PROVIDER", "openai")
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "gpt-4o-mini")
FALLBACK_ENABLED = os.getenv("FALLBACK_ENABLED", "true").strip().lower() == "true"
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")


def _openai_credentials_present() -> bool:
    """Return True when ChatOpenAI can be built without raising."""
    return bool(os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_ADMIN_KEY"))


def fallback_pair(provider: str, model: str) -> tuple[str, str] | None:
    """Return the fallback ``(provider, model)`` if it should be used, else None.

    This is a public helper so callers (e.g. the graph's draft node) can
    reproduce the same fallback decision that ``get_chain`` uses and record
    which model actually produced a structured output.
    """
    fallback_provider = FALLBACK_PROVIDER.strip().lower()
    fallback_model = FALLBACK_MODEL.strip()
    same_model = (fallback_provider, fallback_model) == (
        provider.strip().lower(),
        model.strip(),
    )

    if not FALLBACK_ENABLED or same_model:
        return None

    if fallback_provider == "openai" and not _openai_credentials_present():
        return None

    return fallback_provider, fallback_model


def build(provider: str, model: str, temperature: float = 0.0) -> ChatOllama | ChatOpenAI:
    """Create a ChatOllama or ChatOpenAI instance from the allowlist."""
    provider = provider.strip().lower()
    model = model.strip()

    if provider not in AVAILABLE_MODELS:
        raise ValueError(
            f"Unknown provider '{provider}'. "
            f"Available providers: {list(AVAILABLE_MODELS)}"
        )
    if model not in AVAILABLE_MODELS[provider]:
        raise ValueError(
            f"Model '{model}' is not in the allowlist for provider '{provider}'. "
            f"Available models: {AVAILABLE_MODELS[provider]}"
        )

    if provider == "ollama":
        return ChatOllama(
            model=model,
            base_url=OLLAMA_BASE_URL,
            temperature=temperature,
            num_ctx=8192,
        )

    if not _openai_credentials_present():
        raise ValueError(
            "The OpenAI provider requires OPENAI_API_KEY or OPENAI_ADMIN_KEY. "
            "Set one of these environment variables and restart the server."
        )

    return ChatOpenAI(model=model, temperature=temperature)


def get_chain(
    schema: type[BaseModel] | dict,
    temperature: float = 0.0,
    pref: tuple[str, str] | None = None,
) -> Any:
    """Return a structured-output chain with optional OpenAI fallback.

    Args:
        schema: A Pydantic model (or JSON schema dict) describing the
            structured output the chain should return.
        temperature: Sampling temperature used for both the preferred and
            the fallback model.
        pref: Optional preferred-model override as ``(provider, model)``.
            When omitted, ``DEFAULT_PROVIDER`` / ``DEFAULT_MODEL`` are used.

    Returns:
        A runnable that accepts a prompt and returns an instance of ``schema``.
        When ``FALLBACK_ENABLED`` is true and the fallback model differs from
        the preferred one, the returned chain falls back to
        ``FALLBACK_PROVIDER`` / ``FALLBACK_MODEL`` on failure.
    """
    provider, model = pref if pref is not None else (DEFAULT_PROVIDER, DEFAULT_MODEL)

    primary_chain = build(provider, model, temperature).with_structured_output(
        schema, method="json_schema"
    )

    fallback = fallback_pair(provider, model)
    if fallback is None:
        return primary_chain

    fallback_chain = build(*fallback, temperature).with_structured_output(
        schema, method="json_schema"
    )

    return primary_chain.with_fallbacks([fallback_chain])