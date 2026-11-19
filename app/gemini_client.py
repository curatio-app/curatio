"""Cost-accounted Gemini client for Curatio's closed-vocabulary tagger.

The old ``google-generativeai`` client frequently returned no usage metadata.
That made the displayed cost zero and, more importantly, made the spend ceiling
zero-aware rather than spend-aware.  This module uses Gemini's documented REST
surface directly so every successful response is parsed from the same JSON that
contains ``usageMetadata``.  A request with missing usage is never silently
accepted, and a timed-out/retried request reserves a conservative amount before
another potentially billable attempt is made.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

import httpx
from loguru import logger

from app.config import settings
from app.tagging_provider import (
    TaggingCallError,
    TaggingContentBlocked,
    TaggingRequestFailed,
    TaggingUsageUnavailable,
)
from app.tag_validation import MAX_MODEL_TAGS

GEMINI_API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
OUTPUT_TOKENS_PER_TITLE = 600
MAX_OUTPUT_TOKENS = 32_000
MAX_PROVIDER_BACKOFF_SECONDS = 60.0

# Included in the stored tagger version. Version 3 replaces the provider-hostile
# repeated string enum from version 2 with compact request-local tag indexes.
# The server maps those indexes back to canonical names before validation, so
# output remains closed-vocabulary without exceeding Gemini's schema-state cap.
RESPONSE_SCHEMA_VERSION = "4"

# Deliberately more conservative than the usual English-prose approximation.
# This is a budget reservation for calls the service may have billed but for
# which no usage response reached us, not a claim about exact tokenisation.
CHARS_PER_TOKEN = 3


def _is_retryable_error(exception: BaseException) -> bool:
    """Compatibility entrypoint shared by diagnostics and the engine."""
    return GeminiTaggingEngine._is_transient(exception)


def _read_field(value: Any, *names: str) -> Any:
    """Read a response field from a dict or SDK-like object."""
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        candidate = getattr(value, name, None)
        if candidate is not None:
            return candidate
    return None


def _extract_usage(response: Any) -> Optional[Dict[str, int]]:
    """Return authoritative usage from a REST response or SDK-shaped object."""
    usage = _read_field(response, "usageMetadata", "usage_metadata")
    if usage is None:
        return None
    fields = {
        "prompt_token_count": ("promptTokenCount", "prompt_token_count"),
        "candidates_token_count": (
            "candidatesTokenCount",
            "candidates_token_count",
        ),
        "total_token_count": ("totalTokenCount", "total_token_count"),
        "thoughts_token_count": ("thoughtsTokenCount", "thoughts_token_count"),
        "cached_content_token_count": (
            "cachedContentTokenCount",
            "cached_content_token_count",
        ),
    }
    out: Dict[str, int] = {}
    for canonical, names in fields.items():
        raw = _read_field(usage, *names)
        if isinstance(raw, (int, float)):
            out[canonical] = max(0, int(raw))
    if not out:
        return None
    out["attempt_count"] = 1
    return out


def _estimated_usage(
    prompt: str,
    text: str,
    *,
    output_reserve: int = 0,
    attempts: int = 1,
) -> Dict[str, int]:
    """Conservative usage reservation for an unmetered/failed request."""
    return {
        "prompt_token_count": max(1, len(prompt) // CHARS_PER_TOKEN) * attempts,
        "candidates_token_count": max(
            output_reserve * attempts,
            max(1, len(text) // CHARS_PER_TOKEN),
        ),
        "attempt_count": attempts,
        "estimated": 1,
    }


def _merge_usage(*usages: Optional[Dict[str, int]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for usage in usages:
        for key, value in (usage or {}).items():
            if key == "estimated":
                out[key] = int(bool(out.get(key)) or bool(value))
            elif isinstance(value, (int, float)):
                out[key] = out.get(key, 0) + int(value)
    return out


class GeminiCallError(TaggingCallError):
    """A Gemini call failed, carrying any measured or reserved usage."""

    def __init__(self, message: str, *, usage: Optional[Dict[str, int]] = None):
        super().__init__(message)
        self.usage = usage or {}


class GeminiUsageUnavailable(GeminiCallError, TaggingUsageUnavailable):
    """A successful response omitted billing metadata; do not retry it."""


class GeminiRequestFailed(GeminiCallError, TaggingRequestFailed):
    """The request failed after the bounded transient retry policy."""


class GeminiContentBlocked(GeminiCallError, TaggingContentBlocked):
    """The response had no usable content due to a safety/empty candidate."""


def _salvage_items(text: str) -> Optional[List[Dict[str, Any]]]:
    """Recover complete item objects from a truncated JSON response."""
    decoder = json.JSONDecoder()
    found: dict[int, Dict[str, Any]] = {}
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("tmdb_id"), int):
            if isinstance(value.get("tags"), (list, dict)):
                found[value["tmdb_id"]] = value
    return list(found.values()) or None


class GeminiTaggingEngine:
    """Async REST client with structured output and bounded paid retries."""

    def __init__(self, api_key: Optional[str] = None, model_name: Optional[str] = None):
        self.api_key = api_key or settings.gemini_api_key
        self.model_name = model_name or settings.gemini_model
        self.provider_name = "gemini"
        self.last_usage: Optional[Dict[str, int]] = None
        # A capacity outage is provider-wide, not batch-specific. Without a
        # shared gate, all four tagging coroutines independently retried every
        # two seconds and turned one Gemini 503 burst into 1,041 failed titles.
        # A threading lock is intentional: the singleton may be used from more
        # than one event loop, while asyncio locks bind themselves to one loop.
        self._provider_backoff_lock = threading.Lock()
        self._provider_failure_streak = 0
        self._provider_not_before = 0.0

    def reconfigure(
        self, api_key: Optional[str] = None, model_name: Optional[str] = None
    ) -> None:
        if api_key is not None:
            self.api_key = api_key
        if model_name and model_name != self.model_name:
            logger.info(f"Gemini model changed: {self.model_name} -> {model_name}")
            self.model_name = model_name

    @staticmethod
    def _is_transient(exc: BaseException) -> bool:
        if isinstance(
            exc,
            (
                asyncio.TimeoutError,
                TimeoutError,
                ConnectionError,
                OSError,
                httpx.TimeoutException,
                httpx.NetworkError,
            ),
        ):
            return True
        message = str(exc).lower()
        return any(
            token in message
            for token in (
                "429",
                "rate limit",
                "quota",
                "resource_exhausted",
                "500",
                "502",
                "503",
                "504",
                "internal error",
                "unavailable",
                "deadline",
                "timed out",
                "timeout",
            )
        )

    async def _wait_for_provider(self) -> None:
        """Wait once for the provider-wide cooldown shared by all callers."""
        with self._provider_backoff_lock:
            wait = max(0.0, self._provider_not_before - time.monotonic())
        if wait:
            # Round upward to the nearest millisecond. Subtracting two
            # monotonic readings can otherwise under-sleep the shared gate by
            # a few microseconds and start the retry just before the deadline.
            await asyncio.sleep(math.ceil(wait * 1000.0) / 1000.0)

    def _record_provider_failure(self) -> float:
        """Extend the shared exponential cooldown and return its remaining wait."""
        with self._provider_backoff_lock:
            self._provider_failure_streak = min(self._provider_failure_streak + 1, 6)
            base = min(
                MAX_PROVIDER_BACKOFF_SECONDS,
                2.0 * (2 ** (self._provider_failure_streak - 1)),
            )
            delay = base + random.uniform(0, base / 4)
            self._provider_not_before = max(
                self._provider_not_before, time.monotonic() + delay
            )
            return max(0.0, self._provider_not_before - time.monotonic())

    def _record_provider_recovery(self) -> None:
        """A measured response proves the shared capacity outage has cleared."""
        with self._provider_backoff_lock:
            self._provider_failure_streak = 0
            self._provider_not_before = 0.0

    @staticmethod
    def _max_output_tokens(prompt: str) -> int:
        # The first production v3 pilot proved the old 220-token allowance was
        # not enough for the structured name/confidence objects: Gemini emitted
        # complete, healthy results for only 174/320 titles before stopping.
        # Count headings line-by-line (including a heading at byte zero) and
        # reserve enough for 18 tags plus `extra` without buying the model's
        # full output window for small daily batches.
        title_count = max(
            1, sum(1 for line in prompt.splitlines() if line.startswith("### "))
        )
        return min(
            MAX_OUTPUT_TOKENS,
            max(1_024, title_count * OUTPUT_TOKENS_PER_TITLE + 512),
        )

    @classmethod
    def _request_body(
        cls,
        prompt: str,
        *,
        allowed_tags: Optional[Sequence[str]] = None,
        expected_ids: Optional[Sequence[int]] = None,
    ) -> Dict[str, Any]:
        """Build a request whose schema enforces the classification contract.

        The prompt has always said "ONLY from ALLOWED TAGS", but one pilot
        returned 1,634 unauthorized names. A string enum fixed that logically,
        but its 120-180 names were repeated under nested arrays and Gemini
        rejected every request as an over-complex schema. A bounded local
        integer has the same closed-vocabulary property with a tiny constraint.
        """
        names = list(dict.fromkeys(str(name) for name in (allowed_tags or ()) if name))
        # ``expected_ids`` remains part of the call contract. Exact coverage is
        # enforced by the application validator: a 20-value ID enum plus exact
        # outer-array cardinality multiplied schema states without preventing
        # duplicate IDs.
        _ = expected_ids

        tag_choice_schema: Dict[str, Any]
        if names:
            tag_choice_schema = {
                "tag_index": {
                    "type": "INTEGER",
                    "minimum": 0,
                    "maximum": len(names) - 1,
                }
            }
        else:
            # Compatibility for generic probes that do not provide a menu.
            tag_choice_schema = {"name": {"type": "STRING"}}

        item_schema = {
            "type": "OBJECT",
            "properties": {
                "tmdb_id": {"type": "INTEGER"},
                "tags": {
                    "type": "ARRAY",
                    "maxItems": MAX_MODEL_TAGS,
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            **tag_choice_schema,
                            "confidence": {"type": "NUMBER"},
                        },
                        "required": [
                            "tag_index" if names else "name",
                            "confidence",
                        ],
                    },
                },
                "extra": {
                    "type": "ARRAY",
                    "maxItems": 2,
                    "items": {"type": "STRING"},
                },
            },
            "required": ["tmdb_id", "tags"],
        }
        response_schema = {
            "type": "OBJECT",
            "properties": {
                "items": {
                    "type": "ARRAY",
                    "items": item_schema,
                }
            },
            "required": ["items"],
        }
        return {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": settings.gemini_temperature,
                "maxOutputTokens": cls._max_output_tokens(prompt),
                "responseMimeType": "application/json",
                # This stable schema surface worked in the first production
                # pilots. The bounded local integer enforces the vocabulary
                # without a large repeated string enum.
                "responseSchema": response_schema,
                # Tagging is classification. Paying for hidden reasoning adds
                # cost and variance without improving the closed-vocabulary job.
                "thinkingConfig": {
                    "thinkingBudget": 0,
                    "includeThoughts": False,
                },
            },
        }

    async def _post(
        self,
        prompt: str,
        *,
        allowed_tags: Optional[Sequence[str]] = None,
        expected_ids: Optional[Sequence[int]] = None,
    ) -> Dict[str, Any]:
        model = quote(self.model_name.removeprefix("models/"), safe="-._")
        url = f"{GEMINI_API_ROOT}/models/{model}:generateContent"
        timeout = httpx.Timeout(float(settings.gemini_request_timeout))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url,
                headers={
                    "x-goog-api-key": self.api_key,
                    "content-type": "application/json",
                },
                json=self._request_body(
                    prompt, allowed_tags=allowed_tags, expected_ids=expected_ids
                ),
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # httpx's default message hides Gemini's JSON error body, which
            # made a whole pilot report only "400 Bad Request". Preserve a
            # short provider diagnostic without logging request headers or the
            # API key.
            try:
                error = response.json().get("error") or {}
                detail = str(error.get("message") or response.text)
            except (ValueError, AttributeError):
                detail = response.text
            detail = " ".join(detail.split())[:800]
            raise httpx.HTTPStatusError(
                f"Gemini HTTP {response.status_code}: {detail or 'no error detail'}",
                request=exc.request,
                response=exc.response,
            ) from exc
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Gemini returned a non-object response")
        return payload

    async def _generate_with_backoff(
        self,
        prompt: str,
        *,
        allowed_tags: Optional[Sequence[str]] = None,
        expected_ids: Optional[Sequence[int]] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, int]]:
        attempts = max(1, int(settings.gemini_max_attempts))
        output_reserve = self._max_output_tokens(prompt)
        prior_usage: Dict[str, int] = {}

        for attempt in range(1, attempts + 1):
            await self._wait_for_provider()
            try:
                if allowed_tags is None and expected_ids is None:
                    # Preserve the simple single-argument seam used by probes
                    # and callers that only need generic structured JSON.
                    payload = await self._post(prompt)
                else:
                    payload = await self._post(
                        prompt,
                        allowed_tags=allowed_tags,
                        expected_ids=expected_ids,
                    )
            except Exception as exc:  # noqa: BLE001 - classified below
                reserved = _estimated_usage(
                    prompt, "", output_reserve=output_reserve, attempts=1
                )
                prior_usage = _merge_usage(prior_usage, reserved)
                transient = self._is_transient(exc)
                if not transient or attempt >= attempts:
                    self.last_usage = prior_usage
                    raise GeminiRequestFailed(
                        f"{type(exc).__name__}: {exc}", usage=prior_usage
                    ) from exc
                wait = self._record_provider_failure()
                logger.warning(
                    f"Gemini {type(exc).__name__} (attempt {attempt}/{attempts}); "
                    f"reserved possible spend, shared provider cooldown "
                    f"{wait:.1f}s"
                )
                continue

            measured = _extract_usage(payload)
            if measured is None:
                # Retrying a successful unmetered call could double the bill, so
                # stop this batch and let the run-level breaker halt cleanly.
                reserved = _estimated_usage(
                    prompt,
                    json.dumps(payload, separators=(",", ":")),
                    output_reserve=output_reserve,
                )
                usage = _merge_usage(prior_usage, reserved)
                self.last_usage = usage
                raise GeminiUsageUnavailable(
                    "Gemini response omitted usageMetadata; refusing unmetered work",
                    usage=usage,
                )

            self._record_provider_recovery()
            usage = _merge_usage(prior_usage, measured)
            self.last_usage = usage
            return payload, usage

        raise RuntimeError("unreachable")

    @staticmethod
    def _response_text(payload: Dict[str, Any], usage: Dict[str, int]) -> str:
        candidates = payload.get("candidates") or []
        if candidates:
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(str(part.get("text") or "") for part in parts)
            if text.strip():
                return text
        feedback = payload.get("promptFeedback") or {}
        finish = candidates[0].get("finishReason") if candidates else None
        reason = feedback.get("blockReason") or finish or "empty candidate"
        raise GeminiContentBlocked(f"Gemini returned no content: {reason}", usage=usage)

    async def tag_items_raw(
        self,
        prompt: str,
        *,
        allowed_tags: Optional[Sequence[str]] = None,
        expected_ids: Optional[Sequence[int]] = None,
    ) -> Tuple[List[Dict], Dict[str, int]]:
        """Send a taxonomy prompt and return parsed items plus billed usage."""
        payload, usage = await self._generate_with_backoff(
            prompt, allowed_tags=allowed_tags, expected_ids=expected_ids
        )
        text = self._response_text(payload, usage).strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            salvaged = _salvage_items(text)
            if salvaged:
                logger.warning(f"Salvaged {len(salvaged)} complete items")
                items = salvaged
            else:
                # Carry the measured usage through the normal provider-error
                # path. A bare JSONDecodeError used to turn the batch into an
                # unmetered hard failure—the exact misleading terminal error
                # seen after the long Gemini rebuild.
                raise GeminiRequestFailed(
                    f"Gemini returned malformed JSON: {exc}", usage=usage
                ) from exc
        else:
            items = decoded.get("items") if isinstance(decoded, dict) else decoded
            items = items if isinstance(items, list) else []
        names = list(dict.fromkeys(str(name) for name in (allowed_tags or ()) if name))
        if names:
            # Convert the provider-constrained local indexes back into the
            # canonical shape consumed by validation and storage. Invalid
            # indexes are dropped defensively even though the schema bounds
            # them; provider behavior must not become a trust boundary.
            for item in items:
                if not isinstance(item, dict):
                    continue
                resolved = []
                raw_tags = item.get("tags") or []
                if isinstance(raw_tags, dict):
                    raw_tags = [
                        {"name": name, "confidence": confidence}
                        for name, confidence in raw_tags.items()
                    ]
                for tag in raw_tags:
                    if not isinstance(tag, dict):
                        continue
                    index = tag.get("tag_index")
                    if isinstance(index, int) and 0 <= index < len(names):
                        resolved.append(
                            {
                                "name": names[index],
                                "confidence": tag.get("confidence"),
                            }
                        )
                        continue
                    # Preserve the old diagnostic/mock response shape only
                    # when it is already an exact member of this request's
                    # menu. Production's response schema requires tag_index,
                    # so this compatibility seam cannot reopen vocabulary.
                    name = tag.get("name")
                    if isinstance(name, str) and name in names:
                        resolved.append(
                            {"name": name, "confidence": tag.get("confidence")}
                        )
                item["tags"] = resolved
        return items, usage

    async def tag_items(self, items: List[Dict]) -> List[Dict]:
        """Compatibility wrapper used by diagnostics and older callers."""
        if not items:
            return []
        from app.tagging_prompt import build_prompt

        prompt, vocabulary = build_prompt(items)
        tagged, _ = await self.tag_items_raw(
            prompt,
            allowed_tags=[tag.name for tag in vocabulary],
            expected_ids=[int(item["tmdb_id"]) for item in items],
        )
        # The legacy method returned a confidence mapping. Preserve that shape
        # while the production path keeps the compact structured list.
        for item in tagged:
            raw = item.get("tags")
            if isinstance(raw, list):
                mapped = {}
                for entry in raw:
                    if isinstance(entry, dict) and entry.get("name") is not None:
                        mapped[str(entry["name"])] = entry.get("confidence")
                    elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                        mapped[str(entry[0])] = entry[1]
                item["tags"] = mapped
        return tagged


gemini_engine = GeminiTaggingEngine()
