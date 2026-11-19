"""Provider-neutral configuration and errors for the taxonomy tagger.

Admin Center can switch the paid classification step between Google Gemini,
Anthropic Claude, and OpenAI.  The rest of the pipeline consumes one tiny
engine contract: ``tag_items_raw`` returns canonical item dictionaries and
authoritative (or conservatively reserved) token usage.
"""

from __future__ import annotations

import re

from typing import Any, Optional

from app.config import settings

# Version of the OpenAI/Anthropic JSON contract. Gemini retains its established
# provider-specific version so merely deploying multi-provider support does not
# invalidate an otherwise current Gemini-tagged corpus.
RESPONSE_SCHEMA_VERSION = "4"

PROVIDER_SPECS: dict[str, dict[str, Any]] = {
    "gemini": {
        "label": "Google Gemini",
        "key_field": "gemini_api_key",
        "setting_key": "GEMINI_API_KEY",
        "model_field": "gemini_model",
        "model_setting_key": "GEMINI_MODEL",
        "models": (
            {"id": "gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash-Lite"},
            {
                "id": "gemini-3.1-flash-lite-preview",
                "label": "Gemini 3.1 Flash-Lite Preview",
            },
        ),
    },
    "anthropic": {
        "label": "Anthropic (Claude API)",
        "key_field": "anthropic_api_key",
        "setting_key": "ANTHROPIC_API_KEY",
        "model_field": "anthropic_model",
        "model_setting_key": "ANTHROPIC_MODEL",
        "models": (
            {
                "id": "claude-haiku-4-5-20251001",
                "label": "Claude Haiku 4.5",
            },
            {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6"},
        ),
    },
    "openai": {
        "label": "OpenAI (ChatGPT API)",
        "key_field": "openai_api_key",
        "setting_key": "OPENAI_API_KEY",
        "model_field": "openai_model",
        "model_setting_key": "OPENAI_MODEL",
        "models": (
            {
                "id": "gpt-5.4-nano",
                "label": "GPT-5.4 nano — recommended · $0.20/$1.25 per 1M",
                "recommended": True,
                "production_ready": True,
            },
            {
                "id": "gpt-4o-mini",
                "label": "GPT-4o mini — balanced · $0.15/$0.60 per 1M",
                "production_ready": True,
            },
            {
                "id": "gpt-4.1-nano",
                "label": "GPT-4.1 nano — economy · $0.10/$0.40 per 1M",
                "production_ready": True,
            },
            {
                "id": "gpt-5-nano",
                "label": "GPT-5 nano — cheapest · $0.05/$0.40 per 1M · pilot only",
                "production_ready": False,
                "warning": (
                    "Job 221 showed severe generic-tag collapse on GPT-5 nano. "
                    "Use it only for a small comparison pilot, not a full rebuild."
                ),
            },
        ),
    },
}


PROVIDER_SPECS["local"] = {
    "label": "Local model (OpenAI-compatible)",
    "key_field": "local_api_key",
    "setting_key": "LOCAL_API_KEY",
    "model_field": "local_model",
    "model_setting_key": "LOCAL_MODEL",
    "url_field": "local_base_url",
    "url_setting_key": "LOCAL_BASE_URL",
    # Any model the local server has; there is no fixed list.
    "free_model": True,
    "models": (),
}

_LOCAL_MODEL = re.compile(r"^[A-Za-z0-9._:/@+-]{1,100}$")


class TaggingCallError(Exception):
    """A paid provider call failed, carrying measured or reserved usage."""

    def __init__(self, message: str, *, usage: Optional[dict[str, int]] = None):
        super().__init__(message)
        self.usage = usage or {}


class TaggingUsageUnavailable(TaggingCallError):
    """A successful response omitted usage; accepting it would hide spend."""


class TaggingRequestFailed(TaggingCallError):
    """A request failed after the bounded transient retry policy."""


class TaggingContentBlocked(TaggingCallError):
    """The provider returned no usable content for safety or policy reasons."""


def normalize_provider(value: str | None = None) -> str:
    provider = str(value or settings.tagging_provider).strip().lower()
    if provider not in PROVIDER_SPECS:
        raise ValueError(
            f"Unsupported tagging provider '{provider}'. "
            "Choose gemini, anthropic, openai, or local."
        )
    return provider


def provider_label(provider: str | None = None) -> str:
    return str(PROVIDER_SPECS[normalize_provider(provider)]["label"])


def active_model(provider: str | None = None) -> str:
    spec = PROVIDER_SPECS[normalize_provider(provider)]
    return str(getattr(settings, str(spec["model_field"])))


def active_model_spec(provider: str | None = None) -> dict[str, Any]:
    selected_provider = normalize_provider(provider)
    selected_model = active_model(selected_provider)
    return next(
        (
            dict(item)
            for item in PROVIDER_SPECS[selected_provider]["models"]
            if str(item["id"]) == selected_model
        ),
        {"id": selected_model, "label": selected_model},
    )


def production_model_ready(provider: str | None = None) -> bool:
    return bool(active_model_spec(provider).get("production_ready", True))


def active_api_key(provider: str | None = None) -> str:
    spec = PROVIDER_SPECS[normalize_provider(provider)]
    return str(getattr(settings, str(spec["key_field"])) or "").strip()


def local_base_url() -> str:
    return str(settings.local_base_url or "").strip().rstrip("/")


def provider_ready(provider: str | None = None) -> bool:
    if normalize_provider(provider) == "local":
        # A local server usually needs no key, only an address and a model.
        return bool(local_base_url() and str(settings.local_model or "").strip())
    value = active_api_key(provider)
    return bool(value and not value.startswith("your_"))


def provider_options() -> list[dict[str, Any]]:
    """Public, secret-free provider configuration for Admin Center."""
    options = []
    for provider, spec in PROVIDER_SPECS.items():
        key = active_api_key(provider)
        options.append(
            {
                "id": provider,
                "label": spec["label"],
                "key_set": bool(key and not key.startswith("your_")),
                "key_hint": (key[:4] + "…" + key[-3:]) if len(key) > 8 else "",
                "model": active_model(provider),
                "models": list(spec["models"]),
                "free_model": bool(spec.get("free_model")),
                "base_url": local_base_url() if provider == "local" else "",
            }
        )
    return options


def validate_model(provider: str, model: str) -> str:
    provider = normalize_provider(provider)
    model = str(model).strip()
    if PROVIDER_SPECS[provider].get("free_model"):
        if not _LOCAL_MODEL.match(model):
            raise ValueError("Enter the model name your local server uses.")
        return model
    allowed = {str(item["id"]) for item in PROVIDER_SPECS[provider]["models"]}
    if model not in allowed:
        raise ValueError(
            f"Unsupported {provider_label(provider)} model '{model}'. "
            f"Choose one of: {', '.join(sorted(allowed))}."
        )
    return model


def get_tagging_engine():
    """Return the selected singleton, synchronized with live admin settings."""
    provider = normalize_provider()
    if provider == "gemini":
        from app.gemini_client import gemini_engine

        engine = gemini_engine
    elif provider == "local":
        from app.external_tagging_client import local_engine

        local_engine.base_url = local_base_url()
        engine = local_engine
    else:
        from app.external_tagging_client import anthropic_engine, openai_engine

        engine = anthropic_engine if provider == "anthropic" else openai_engine
    engine.reconfigure(
        api_key=active_api_key(provider), model_name=active_model(provider)
    )
    return engine


def reconfigure_tagging_engines() -> None:
    """Refresh any instantiated clients after an Admin Center change."""
    # Lazily obtaining only the selected engine avoids importing network clients
    # during taxonomy/database-only commands.
    get_tagging_engine()
