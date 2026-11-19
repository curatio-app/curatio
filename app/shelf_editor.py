"""Bounded provider-neutral AI editorial review for synthesized shelves.

The editor sees shelf summaries, never profiles or the title corpus. It may
approve/reject a deterministic formula and improve its display name; it cannot
change tags, membership, quality gates, or ranking. Decisions are cached for
both outcomes so an unchanged rebuild makes no paid request.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import quote

import httpx
from loguru import logger
from sqlalchemy.orm import Session

from app.config import settings
from app.models import SynthEditorialDecision
from app.tagging_provider import (
    PROVIDER_SPECS,
    active_api_key,
    active_model,
    normalize_provider,
    provider_label,
    provider_options,
)
from workers.tagging_common import estimate_cost

EDITOR_PROMPT_VERSION = "3"
EDITOR_BATCH_SIZE = 20
EDITOR_MAX_OUTPUT_TOKENS = 4_096
EDITOR_REASON_CODES = (
    "strong_concept",
    "weak_concept",
    "misleading",
    "format_mismatch",
    "family_safety",
    "low_title_quality",
    "redundant",
    "overly_generic",
    "overly_narrow",
    "awkward_name",
    "name_too_broad",
    "unsupported_name_concept",
)

# Premium choices belong only to the tiny shelf-summary pass. Keeping them out
# of PROVIDER_SPECS prevents an operator from accidentally choosing a premium
# model for the 100k-title tagging job. Prices in labels are standard
# synchronous API rates; the hard ceiling uses the same (or more conservative)
# values before every call.
EDITOR_ONLY_MODELS: dict[str, tuple[dict[str, Any], ...]] = {
    "openai": (
        {
            "id": "gpt-5.4-mini",
            "label": ("GPT-5.4 mini — recommended premium editor · $0.75/$4.50 per 1M"),
            "recommended": True,
            "editor_only": True,
        },
        {
            "id": "gpt-5.6-terra",
            "label": (
                "GPT-5.6 Terra — high-end editor · $2.50/$15 per 1M · "
                "higher ceiling may be needed"
            ),
            "editor_only": True,
        },
        {
            "id": "gpt-5.6-sol",
            "label": (
                "GPT-5.6 Sol — maximum-quality editor · $5/$30 per 1M · "
                "raise ceiling"
            ),
            "editor_only": True,
        },
    )
}


class ShelfEditorialError(RuntimeError):
    """A complete editorial pass could not safely finish."""


@dataclass(frozen=True)
class EditorialResult:
    decisions: dict[str, dict[str, Any]]
    cached: int
    reviewed: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    reserved_cost_usd: float
    provider: str
    model: str


def editor_provider() -> str:
    return normalize_provider(
        settings.synth_editor_provider or settings.tagging_provider
    )


def editor_model(provider: str | None = None) -> str:
    selected = editor_provider() if provider is None else normalize_provider(provider)
    return str(settings.synth_editor_model or active_model(selected)).strip()


def editor_provider_options() -> list[dict[str, Any]]:
    """Secret-free picker options, including models safe only for shelf review."""
    options = provider_options()
    for option in options:
        additions = EDITOR_ONLY_MODELS.get(str(option["id"]), ())
        if not additions:
            continue
        known = {str(model["id"]) for model in option.get("models", [])}
        option["models"] = [
            *option.get("models", []),
            *(dict(model) for model in additions if str(model["id"]) not in known),
        ]
    return options


def validate_editor_model(provider: str, model: str) -> str:
    """Validate the independent editor without widening full-tagger choices."""
    selected_provider = normalize_provider(provider)
    selected_model = str(model).strip()
    allowed = {
        str(item["id"])
        for item in (
            *PROVIDER_SPECS[selected_provider]["models"],
            *EDITOR_ONLY_MODELS.get(selected_provider, ()),
        )
    }
    if selected_model not in allowed:
        raise ValueError(
            f"Unsupported {provider_label(selected_provider)} shelf-editor model "
            f"'{selected_model}'. Choose one of: {', '.join(sorted(allowed))}."
        )
    return selected_model


def sample_hash(candidate: dict[str, Any]) -> str:
    sample = [
        {
            "tmdb_id": item.get("tmdb_id"),
            "title": item.get("title"),
            "year": item.get("year"),
            "quality": item.get("quality"),
            "vote_count": item.get("vote_count"),
            "popularity": item.get("popularity"),
            "genres": item.get("genres"),
            "content_rating": item.get("content_rating"),
            "formats": item.get("formats"),
            "adult": item.get("adult"),
            "overview": item.get("overview"),
        }
        for item in (candidate.get("sample_titles") or [])[:12]
    ]
    raw = json.dumps(sample, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode()).hexdigest()


def decision_cache_key(
    candidate: dict[str, Any], provider: str, model: str
) -> tuple[str, str]:
    current_sample_hash = sample_hash(candidate)
    raw = "|".join(
        (
            EDITOR_PROMPT_VERSION,
            provider,
            model,
            str(candidate["spec_hash"]),
            current_sample_hash,
        )
    )
    return hashlib.sha256(raw.encode()).hexdigest(), current_sample_hash


def _decision_schema(candidate_ids: list[str], *, gemini: bool = False) -> dict:
    type_name = (lambda value: value.upper()) if gemini else (lambda value: value)
    properties: dict[str, Any] = {
        "candidate_id": {"type": type_name("string")},
        "decision": {
            "type": type_name("string"),
            "enum": ["keep", "reject"],
        },
        "display_name": {"type": type_name("string")},
        "editorial_score": {
            "type": type_name("integer"),
            "minimum": 0,
            "maximum": 100,
        },
        "reason_codes": {
            "type": type_name("array"),
            "maxItems": 4,
            "items": {
                "type": type_name("string"),
                "enum": list(EDITOR_REASON_CODES),
            },
        },
    }
    item: dict[str, Any] = {
        "type": type_name("object"),
        "properties": properties,
        "required": list(properties),
    }
    if not gemini:
        item["additionalProperties"] = False
        properties["candidate_id"]["enum"] = candidate_ids
    root: dict[str, Any] = {
        "type": type_name("object"),
        "properties": {
            "decisions": {
                "type": type_name("array"),
                "minItems": len(candidate_ids),
                "maxItems": len(candidate_ids),
                "items": item,
            }
        },
        "required": ["decisions"],
    }
    if not gemini:
        root["additionalProperties"] = False
    return root


def _prompt(candidates: list[dict[str, Any]]) -> str:
    summaries = [
        {
            "candidate_id": str(candidate["candidate_id"]),
            "deterministic_name": candidate["deterministic_name"],
            "media_type": candidate["media_type"],
            "tags": candidate["tags"],
            "facets": candidate["facets"],
            "support": candidate["support"],
            "lift": candidate["lift"],
            "actual_item_count": candidate["actual_item_count"],
            "sample_titles": (candidate.get("sample_titles") or [])[:12],
        }
        for candidate in candidates
    ]
    return (
        "You are the senior collection editor for a premium streaming service.\n"
        "Judge browse usefulness, concept coherence, sample accuracy, and natural "
        "product copy. A clear, familiar collection is better than a novel but "
        "statistically unusual correlation; do not reject a useful shelf merely "
        "because it is broad or decade-based. Compare candidates in this batch and "
        "reject the weaker expression of the same browse intent.\n"
        "\n"
        "Hard rules:\n"
        "- Keep only a shelf whose supplied sample consistently satisfies both tags.\n"
        "- Family/Kids/All Ages shelves must have no adult, mature-rated, or visibly "
        "adult sample. One clear contradiction requires rejection.\n"
        "- Reject format mixtures that make the label misleading, such as "
        "documentaries inside a narrative sci-fi shelf, stand-up specials inside "
        "dark-comedy movies, or filmed stage performances inside a location shelf.\n"
        "- Reject a shelf dominated by obscure or interchangeable low-tier titles; "
        "support and lift do not compensate for weak browsing appeal.\n"
        "- Names must be 4-45 characters, concise and idiomatic. They may rephrase "
        "the two supplied tags but must not add a genre, theme, audience, format, "
        "place, period, or plot concept absent from those tags.\n"
        "- The tag 'Epic Runtime' means unusually long duration only. Its name must "
        "say 'Epic-Length' or otherwise clearly describe length; never shorten it "
        "to 'Epic' or imply narrative scale.\n"
        "- Never change tags, membership, formulas, media type, or quality rules.\n"
        "\n"
        "Scoring: 85-100 is exceptional, 75-84 is publishable, and below 75 must be "
        "rejected. Use at most four short snake_case reason codes, preferably "
        "strong_concept, weak_concept, misleading, format_mismatch, family_safety, "
        "low_title_quality, redundant, overly_generic, awkward_name, or "
        "unsupported_name_concept. Return every candidate exactly once as JSON "
        "matching the response schema.\n\nCANDIDATES:\n"
        + json.dumps(summaries, ensure_ascii=False, separators=(",", ":"))
    )


def _usage_from_payload(provider: str, payload: dict[str, Any]) -> tuple[int, int]:
    if provider == "gemini":
        usage = payload.get("usageMetadata") or {}
        input_tokens = usage.get("promptTokenCount")
        output_tokens = usage.get("candidatesTokenCount")
    elif provider == "openai":
        usage = payload.get("usage") or {}
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
    else:
        usage = payload.get("usage") or {}
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
    if not isinstance(input_tokens, (int, float)) or not isinstance(
        output_tokens, (int, float)
    ):
        raise ShelfEditorialError(f"{provider} shelf-editor response omitted usage")
    return max(0, int(input_tokens)), max(0, int(output_tokens))


def _response_text(provider: str, payload: dict[str, Any]) -> str:
    if provider == "gemini":
        candidates = payload.get("candidates") or []
        parts = (
            (candidates[0].get("content") or {}).get("parts") or []
            if candidates
            else []
        )
        return "".join(str(part.get("text") or "") for part in parts)
    if provider == "openai":
        return "".join(
            str(content.get("text") or "")
            for output in payload.get("output") or []
            for content in output.get("content") or []
            if content.get("type") == "output_text"
        )
    return "".join(
        str(block.get("text") or "")
        for block in payload.get("content") or []
        if block.get("type") == "text"
    )


def _provider_request(
    provider: str,
    model: str,
    api_key: str,
    prompt: str,
    candidate_ids: list[str],
) -> tuple[dict[str, Any], int, int]:
    timeout = httpx.Timeout(float(settings.gemini_request_timeout))
    body: dict[str, Any]
    headers: dict[str, str]
    if provider == "gemini":
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{quote(model.removeprefix('models/'), safe='-._')}:generateContent"
        )
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": EDITOR_MAX_OUTPUT_TOKENS,
                "responseMimeType": "application/json",
                "responseSchema": _decision_schema(candidate_ids, gemini=True),
                "thinkingConfig": {"thinkingBudget": 0, "includeThoughts": False},
            },
        }
        headers = {"x-goog-api-key": api_key, "content-type": "application/json"}
    elif provider == "openai":
        from app.external_tagging_client import _openai_reasoning

        url = "https://api.openai.com/v1/responses"
        body = {
            "model": model,
            "input": prompt,
            "max_output_tokens": EDITOR_MAX_OUTPUT_TOKENS,
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "curatio_shelf_editor",
                    "strict": True,
                    "schema": _decision_schema(candidate_ids),
                }
            },
        }
        reasoning = _openai_reasoning(model)
        if reasoning is not None:
            body["reasoning"] = reasoning
        headers = {
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        }
    else:
        url = "https://api.anthropic.com/v1/messages"
        schema = _decision_schema(candidate_ids)
        body = {
            "model": model,
            "max_tokens": EDITOR_MAX_OUTPUT_TOKENS,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, headers=headers, json=body)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        try:
            detail = str(
                (response.json().get("error") or {}).get("message") or response.text
            )
        except (ValueError, AttributeError):
            detail = response.text
        raise ShelfEditorialError(
            f"{provider} shelf editor HTTP {response.status_code}: "
            f"{' '.join(detail.split())[:500]}"
        ) from exc
    payload = response.json()
    if not isinstance(payload, dict):
        raise ShelfEditorialError(f"{provider} shelf editor returned a non-object")
    input_tokens, output_tokens = _usage_from_payload(provider, payload)
    return payload, input_tokens, output_tokens


def _parse_decisions(
    text: str, candidate_ids: Iterable[str]
) -> dict[str, dict[str, Any]]:
    try:
        decoded = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise ShelfEditorialError(
            f"shelf editor returned malformed JSON: {exc}"
        ) from exc
    rows = decoded.get("decisions") if isinstance(decoded, dict) else None
    if not isinstance(rows, list):
        raise ShelfEditorialError("shelf editor response has no decisions array")
    expected = set(candidate_ids)
    parsed: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        candidate_id = str(raw.get("candidate_id") or "")
        if candidate_id not in expected or candidate_id in parsed:
            continue
        decision = str(raw.get("decision") or "")
        name = " ".join(str(raw.get("display_name") or "").split())
        score = raw.get("editorial_score")
        reasons = raw.get("reason_codes")
        if decision not in {"keep", "reject"} or not isinstance(score, int):
            continue
        parsed[candidate_id] = {
            "decision": decision,
            "display_name": name,
            "editorial_score": max(0, min(100, score)),
            "reason_codes": (
                [str(value)[:40] for value in reasons[:4]]
                if isinstance(reasons, list)
                else []
            ),
        }
    if set(parsed) != expected:
        missing = sorted(expected - set(parsed))
        raise ShelfEditorialError(f"shelf editor omitted/invalid candidates: {missing}")
    return parsed


def review_candidates(
    db: Session,
    candidates: list[dict[str, Any]],
    *,
    request_fn=_provider_request,
) -> EditorialResult:
    """Review every changed candidate within one hard run-level ceiling."""
    provider = editor_provider()
    model = editor_model(provider)
    api_key = active_api_key(provider)
    if not api_key or api_key.startswith("your_"):
        raise ShelfEditorialError(f"A {provider} API key is required for shelf review")

    decisions: dict[str, dict[str, Any]] = {}
    uncached: list[tuple[dict[str, Any], str, str]] = []
    for candidate in candidates:
        cache_key, current_sample_hash = decision_cache_key(candidate, provider, model)
        cached = db.get(SynthEditorialDecision, cache_key)
        if cached is not None:
            decisions[str(candidate["candidate_id"])] = {
                "decision": cached.decision,
                "display_name": cached.display_name or "",
                "editorial_score": int(cached.editorial_score or 0),
                "reason_codes": list(cached.reason_codes or []),
                "cached": True,
                "cost_usd": 0.0,
            }
        else:
            uncached.append((candidate, cache_key, current_sample_hash))

    total_input = total_output = 0
    total_cost = 0.0
    total_reserved = 0.0
    ceiling = float(settings.synth_editor_cost_ceiling_usd)
    for start in range(0, len(uncached), EDITOR_BATCH_SIZE):
        batch = uncached[start : start + EDITOR_BATCH_SIZE]
        batch_candidates = [value[0] for value in batch]
        prompt = _prompt(batch_candidates)
        # Reserve conservatively before making a paid call. Rough tokenization
        # plus the full output cap means an unknown/verbose response cannot
        # silently cross the configured run ceiling.
        reserved_input = max(1, len(prompt.encode("utf-8")) // 3)
        reserved_cost = estimate_cost(
            reserved_input,
            EDITOR_MAX_OUTPUT_TOKENS,
            provider=provider,
            model=model,
        )
        if total_cost + reserved_cost > ceiling:
            raise ShelfEditorialError(
                f"Shelf editorial reserve ${total_cost + reserved_cost:.4f} "
                f"would exceed ${ceiling:.2f} ceiling"
            )
        total_reserved += reserved_cost

        payload, input_tokens, output_tokens = request_fn(
            provider,
            model,
            api_key,
            prompt,
            [str(value[0]["candidate_id"]) for value in batch],
        )
        measured_cost = estimate_cost(
            input_tokens,
            output_tokens,
            provider=provider,
            model=model,
        )
        if total_cost + measured_cost > ceiling:
            raise ShelfEditorialError(
                f"Shelf editorial spend ${total_cost + measured_cost:.4f} "
                f"exceeded ${ceiling:.2f} ceiling"
            )
        parsed = _parse_decisions(
            _response_text(provider, payload),
            [str(value[0]["candidate_id"]) for value in batch],
        )
        total_input += input_tokens
        total_output += output_tokens
        total_cost += measured_cost

        for candidate, cache_key, current_sample_hash in batch:
            candidate_id = str(candidate["candidate_id"])
            decision = parsed[candidate_id]
            decisions[candidate_id] = {
                **decision,
                "cached": False,
                "cost_usd": measured_cost / max(1, len(batch)),
            }
            db.add(
                SynthEditorialDecision(
                    cache_key=cache_key,
                    spec_hash=str(candidate["spec_hash"]),
                    sample_hash=current_sample_hash,
                    prompt_version=EDITOR_PROMPT_VERSION,
                    provider=provider,
                    model=model,
                    decision=decision["decision"],
                    display_name=decision["display_name"] or None,
                    editorial_score=decision["editorial_score"],
                    reason_codes=decision["reason_codes"],
                    input_tokens=input_tokens // max(1, len(batch)),
                    output_tokens=output_tokens // max(1, len(batch)),
                    cost_usd=measured_cost / max(1, len(batch)),
                )
            )
        # Editorial decisions do not affect the serving snapshot, so preserving
        # completed paid work across a later provider failure is safe.
        db.commit()

    logger.info(
        f"Shelf editor: {len(uncached)} reviewed, "
        f"{len(candidates) - len(uncached)} cached, ${total_cost:.4f}"
    )
    return EditorialResult(
        decisions=decisions,
        cached=len(candidates) - len(uncached),
        reviewed=len(uncached),
        input_tokens=total_input,
        output_tokens=total_output,
        cost_usd=round(total_cost, 6),
        reserved_cost_usd=round(total_reserved, 6),
        provider=provider,
        model=model,
    )
