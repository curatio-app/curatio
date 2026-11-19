"""Provider-neutral tagging, pricing, and structured response coverage."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

os.environ.setdefault("TMDB_API_KEY", "test")
os.environ.setdefault("GEMINI_API_KEY", "test")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_characters_long")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.external_tagging_client import (  # noqa: E402
    AnthropicTaggingEngine,
    OpenAITaggingEngine,
    _openai_reasoning,
    _response_schema,
)
from app.config import settings  # noqa: E402
from app.tagging_provider import PROVIDER_SPECS  # noqa: E402
from app.tagging_provider import TaggingRequestFailed  # noqa: E402
from workers.tagging_common import (  # noqa: E402
    effective_batch_size,
    estimate_cost,
    pricing_for_model,
    tagger_version,
)


def test_each_provider_has_a_key_and_supported_model() -> None:
    assert set(PROVIDER_SPECS) == {"gemini", "anthropic", "openai", "local"}
    for spec in PROVIDER_SPECS.values():
        assert spec["key_field"]
        assert spec["model_field"]
        # A local server has no fixed model list; any name it serves is valid.
        assert spec["models"] or spec.get("free_model")


def test_provider_pricing_is_not_charged_at_gemini_rates() -> None:
    assert pricing_for_model("gpt-5-nano", "openai") == (
        0.05,
        0.40,
        "model-table",
    )
    assert pricing_for_model("gpt-5.4-nano", "openai") == (
        0.20,
        1.25,
        "model-table",
    )
    assert pricing_for_model("gpt-5.6-luna", "openai") == (
        1.00,
        6.00,
        "model-table",
    )
    assert pricing_for_model("gpt-5.4-mini", "openai") == (
        0.75,
        4.50,
        "model-table",
    )
    assert pricing_for_model("gpt-5.6-terra", "openai") == (
        2.50,
        15.00,
        "model-table",
    )
    assert pricing_for_model("gpt-5.6-sol", "openai") == (
        5.00,
        30.00,
        "model-table",
    )
    assert pricing_for_model("claude-haiku-4-5-20251001", "anthropic") == (
        1.00,
        5.00,
        "model-table",
    )
    assert (
        estimate_cost(
            1_000_000,
            1_000_000,
            model="gpt-5.4-nano",
            provider="openai",
        )
        == 1.45
    )


def test_low_cost_openai_models_use_compatible_reasoning_settings() -> None:
    assert _openai_reasoning("gpt-5-nano") == {"effort": "minimal"}
    assert _openai_reasoning("gpt-5.6-luna") == {"effort": "none"}
    assert _openai_reasoning("gpt-4.1-nano") is None
    assert _openai_reasoning("gpt-4o-mini") is None
    picker_ids = {item["id"] for item in PROVIDER_SPECS["openai"]["models"]}
    assert "gpt-5.6-luna" not in picker_ids


def test_openai_schema_requires_a_complete_dense_batch() -> None:
    schema = _response_schema(
        ["Warmhearted", "Slow Burn", "Friendship"],
        expected_ids=[7, 8, 9],
        allowed_indexes_by_id={7: [0, 2], 8: [1, 2], 9: [0, 1]},
    )
    items = schema["properties"]["items"]
    assert items["required"] == ["7", "8", "9"]
    assert items["additionalProperties"] is False
    tags = items["properties"]["7"]["properties"]["tags"]
    assert "minItems" not in tags
    assert tags["maxItems"] == 3
    assert tags["items"]["properties"]["tag_index"] == {
        "type": "integer",
        "enum": [0, 2],
    }
    assert tags["items"]["properties"]["confidence"] == {
        "type": "number",
        "minimum": 0.5,
        "maximum": 1.0,
    }


def test_openai_batch_cap_stays_inside_structured_output_enum_limit() -> None:
    assert effective_batch_size(20, "openai") == 3
    assert effective_batch_size(20, "anthropic") == 5
    assert effective_batch_size(20, "gemini") == 20


def test_failed_pilot_model_is_not_production_ready() -> None:
    original = (settings.tagging_provider, settings.openai_model)
    try:
        object.__setattr__(settings, "tagging_provider", "openai")
        object.__setattr__(settings, "openai_model", "gpt-5-nano")
        from app.tagging_provider import production_model_ready

        assert production_model_ready() is False
        object.__setattr__(settings, "openai_model", "gpt-5.4-nano")
        assert production_model_ready() is True
    finally:
        object.__setattr__(settings, "tagging_provider", original[0])
        object.__setattr__(settings, "openai_model", original[1])


def test_openai_incomplete_response_is_never_accepted_as_a_partial_batch() -> None:
    engine = OpenAITaggingEngine("secret", "gpt-5-nano")
    with pytest.raises(TaggingRequestFailed, match="max_output_tokens"):
        engine._response_text(
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
            },
            {"prompt_token_count": 1, "candidates_token_count": 1},
        )


def test_switching_provider_creates_a_distinct_tagger_version() -> None:
    original = (settings.tagging_provider, settings.openai_model)
    before = tagger_version()
    try:
        object.__setattr__(settings, "tagging_provider", "openai")
        object.__setattr__(settings, "openai_model", "gpt-5.4-nano")
        assert tagger_version() != before
    finally:
        object.__setattr__(settings, "tagging_provider", original[0])
        object.__setattr__(settings, "openai_model", original[1])


def test_openai_response_is_mapped_back_to_canonical_names(monkeypatch) -> None:
    engine = OpenAITaggingEngine("secret", "gpt-5-nano")
    payload = {
        "output": [
            {
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {
                                "items": {
                                    "7": {
                                        "tags": [
                                            {"tag_index": 1, "confidence": 0.9},
                                            {"tag_index": 99, "confidence": 1},
                                        ],
                                        "extra": [],
                                    }
                                }
                            }
                        ),
                    }
                ]
            }
        ],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 25,
            "input_tokens_details": {"cached_tokens": 10},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }

    seen = {}

    async def fake_post(*args, **kwargs):
        seen.update(kwargs)
        return payload

    monkeypatch.setattr(engine, "_post", fake_post)
    items, usage = asyncio.run(
        engine.tag_items_raw(
            "### 7\nA title",
            allowed_tags=["Warmhearted", "Slow Burn"],
            expected_ids=[7],
            allowed_indexes_by_id={7: [0, 1]},
        )
    )
    assert items[0]["tags"] == [{"name": "Slow Burn", "confidence": 0.9}]
    assert seen["expected_ids"] == [7]
    assert seen["allowed_indexes_by_id"] == {7: [0, 1]}
    assert usage["prompt_token_count"] == 100
    assert usage["candidates_token_count"] == 25


def test_anthropic_usage_and_content_are_understood(monkeypatch) -> None:
    engine = AnthropicTaggingEngine("secret", "claude-haiku-4-5-20251001")
    payload = {
        "content": [
            {
                "type": "text",
                "text": '{"items":[{"tmdb_id":8,"tags":[],"extra":[]}]}',
            }
        ],
        "usage": {
            "input_tokens": 80,
            "output_tokens": 12,
            "cache_read_input_tokens": 5,
        },
        "stop_reason": "end_turn",
    }

    async def fake_post(*args, **kwargs):
        return payload

    monkeypatch.setattr(engine, "_post", fake_post)
    items, usage = asyncio.run(engine.tag_items_raw("### 8\nA title"))
    assert items[0]["tmdb_id"] == 8
    assert usage["prompt_token_count"] == 85
    assert usage["cached_content_token_count"] == 5
