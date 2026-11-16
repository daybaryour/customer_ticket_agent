"""Per-user model preference storage backed by settings.json."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
# Docker overrides SETTINGS_PATH so preferences live in the /app/data volume.
SETTINGS_PATH = Path(os.getenv("SETTINGS_PATH", str(BASE_DIR / "settings.json")))

# Load backend/.env so defaults behave the same regardless of CWD.
load_dotenv(BASE_DIR / ".env")

# Import the model allowlist and env defaults from llm.py. The try/except
# keeps this working whether backend is imported as a package or scripts are
# run directly with backend/ on sys.path.
try:
    from llm import AVAILABLE_MODELS, DEFAULT_MODEL, DEFAULT_PROVIDER
except ImportError:  # pragma: no cover - package-relative fallback
    from .llm import AVAILABLE_MODELS, DEFAULT_MODEL, DEFAULT_PROVIDER

# RLock so set_pref() can safely call _load() while already holding the lock.
_lock = threading.RLock()

# In-memory cache of the parsed settings.json contents.
_cache: dict | None = None


def _load() -> dict:
    """Read settings.json once and cache the parsed dictionary in memory.

    A malformed or missing file is treated as an empty settings dictionary so
    callers can always fall back to the default preference chain.
    """
    global _cache

    with _lock:
        if _cache is not None:
            return _cache

        if SETTINGS_PATH.exists():
            try:
                parsed = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                parsed = {}
            _cache = parsed if isinstance(parsed, dict) else {}
        else:
            _cache = {}

        return _cache


def _save(settings: dict) -> None:
    """Persist the settings dict to disk. The caller must hold ``_lock``."""
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _is_valid_pair(provider: str, model: str) -> bool:
    """Return True if ``(provider, model)`` is in the global allowlist."""
    return (
        provider in AVAILABLE_MODELS
        and model in AVAILABLE_MODELS[provider]
    )


def get_pref(user_id: str) -> tuple[str, str]:
    """Return the ``(provider, model)`` preference for ``user_id``.

    Lookup order:
      1. settings.json entry for ``user_id``
      2. settings.json entry for ``"default"``
      3. ``DEFAULT_PROVIDER`` / ``DEFAULT_MODEL`` from the environment
      4. The first pair in ``AVAILABLE_MODELS`` (safety net)
    """
    settings = _load()

    for key in (user_id, "default"):
        entry = settings.get(key)
        if isinstance(entry, dict):
            provider = str(entry.get("provider", "")).strip().lower()
            model = str(entry.get("model", "")).strip()
            if _is_valid_pair(provider, model):
                return provider, model

    provider = str(DEFAULT_PROVIDER).strip().lower()
    model = str(DEFAULT_MODEL).strip()
    if _is_valid_pair(provider, model):
        return provider, model

    # Safety net if the env defaults are missing or no longer allowlisted.
    for provider, models in AVAILABLE_MODELS.items():
        if models:
            return provider, models[0]

    raise ValueError("AVAILABLE_MODELS is empty")


def set_pref(user_id: str, provider: str, model: str) -> tuple[str, str]:
    """Store a user's provider/model preference after allowlist validation.

    Args:
        user_id: The user (or ``"default"``) whose preference to set.
        provider: Provider name, e.g. ``"ollama"`` or ``"openai"``.
        model: Model name, e.g. ``"llama3.1:8b"`` or ``"gpt-4o-mini"``.

    Raises:
        ValueError: If the provider or model is not in ``AVAILABLE_MODELS``.

    Returns:
        The normalized ``(provider, model)`` pair that was persisted.
    """
    if not user_id.strip():
        raise ValueError("user_id must not be empty")

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

    settings = _load()
    with _lock:
        settings[user_id] = {"provider": provider, "model": model}
        _save(settings)

    return provider, model