"""OpenAI and Anthropic structured-output engines for taxonomy tagging."""

from __future__ import annotations

import asyncio
import json
import math
import random
import threading
import time
from typing import Any, Mapping, Optional, Sequence

import httpx
from loguru import logger

from app.config import settings
from app.gemini_client import (
    MAX_OUTPUT_TOKENS,
    OUTPUT_TOKENS_PER_TITLE,
    _estimated_usage,
    _merge_usage,
    _salvage_items,
)
from app.tagging_provider import (
    TaggingContentBlocked,
    TaggingRequestFailed,
    TaggingUsageUnavailable,
)
from app.tag_validation import MAX_MODEL_TAGS


def _max_output_tokens(prompt: str) -> int:
    title_count = max(
        1, sum(1 for line in prompt.splitlines() if line.startswith("### "))
    )
    return min(
        MAX_OUTPUT_TOKENS,
        max(1_024, title_count * OUTPUT_TOKENS_PER_TITLE + 512),
    )


def _response_schema(
    allowed_tags: Optional[Sequence[str]],
    *,
    expected_ids: Optional[Sequence[int]] = None,
    allowed_indexes_by_id: Optional[Mapping[int, Sequence[int]]] = None,
) -> dict[str, Any]:
    names = list(dict.fromkeys(str(name) for name in (allowed_tags or ()) if name))
    expected = list(dict.fromkeys(int(tmdb_id) for tmdb_id in (expected_ids or ())))

    def result_schema(indexes: Optional[Sequence[int]] = None) -> dict[str, Any]:
        if names:
            clean_indexes = sorted(
                {
                    int(index)
                    for index in (indexes if indexes is not None else range(len(names)))
                    if 0 <= int(index) < len(names)
                }
            )
            index_schema: dict[str, Any] = {"type": "integer"}
            if indexes is not None and clean_indexes:
                index_schema["enum"] = clean_indexes
            else:
                index_schema.update({"minimum": 0, "maximum": max(0, len(names) - 1)})
            choice: dict[str, dict[str, Any]] = {"tag_index": index_schema}
            required_choice = "tag_index"
        else:
            choice = {"name": {"type": "string"}}
            required_choice = "name"

        confidence_schema: dict[str, Any] = {"type": "number"}
        if expected:
            confidence_schema.update({"minimum": 0.5, "maximum": 1.0})
        tags_schema: dict[str, Any] = {
            "type": "array",
            "maxItems": MAX_MODEL_TAGS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    **choice,
                    "confidence": confidence_schema,
                },
                "required": [required_choice, "confidence"],
            },
        }
        if expected:
            tags_schema["description"] = (
                "Choose zero to three distinct, strongly supported interpretive tag "
                "indexes. Never repeat an index and never fill a quota."
            )
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "tags": tags_schema,
                "extra": {
                    "type": "array",
                    "maxItems": 2,
                    "items": {"type": "string"},
                },
            },
            # Strict schemas require all properties. ``extra`` may be empty.
            "required": ["tags", "extra"],
        }

    per_title = allowed_indexes_by_id or {}
    if expected and names and all(tmdb_id in per_title for tmdb_id in expected):
        # A keyed object makes every requested title a required property, so a
        # small model cannot satisfy the schema by duplicating another ID. Each
        # title also receives its own enum of valid indexes, preventing tags
        # shortlisted for a neighboring title from leaking across the batch.
        item_keys = [str(tmdb_id) for tmdb_id in expected]
        items_schema: dict[str, Any] = {
            "type": "object",
            "description": (
                "Results keyed by requested TMDB ID. Return every required key "
                "exactly once."
            ),
            "additionalProperties": False,
            "properties": {
                str(tmdb_id): result_schema(per_title[tmdb_id]) for tmdb_id in expected
            },
            "required": item_keys,
        }
    else:
        # Anthropic and compatibility callers retain the original array shape.
        item_schema = result_schema()
        item_schema["properties"] = {
            "tmdb_id": {"type": "integer"},
            **item_schema["properties"],
        }
        item_schema["required"] = ["tmdb_id", "tags", "extra"]
        items_schema = {
            "type": "array",
            "items": item_schema,
        }
        if expected:
            items_schema.update(
                {
                    "description": (
                        "Return exactly one item for every requested title, in "
                        "the same order as the prompt."
                    ),
                    "minItems": len(expected),
                    "maxItems": len(expected),
                }
            )
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "items": items_schema,
        },
        "required": ["items"],
    }


def _openai_reasoning(model_name: str) -> Optional[dict[str, str]]:
    """Use the cheapest supported effort and omit it for non-reasoning models."""
    model = model_name.lower()
    if model.startswith(("gpt-5.6", "gpt-5.5", "gpt-5.4")):
        return {"effort": "none"}
    if model in {"gpt-5", "gpt-5-mini", "gpt-5-nano"} or model.startswith(
        ("gpt-5-202", "gpt-5-mini-202", "gpt-5-nano-202")
    ):
        return {"effort": "minimal"}
    # GPT-4.1 nano and GPT-4o mini are non-reasoning models. Sending a
    # reasoning parameter to them can make an otherwise valid request fail.
    return None


class StructuredRestTaggingEngine:
    """Shared paid-retry, usage, and canonicalization behavior."""

    provider_name = ""

    def __init__(self, api_key: str = "", model_name: str = ""):
        self.api_key = api_key
        self.model_name = model_name
        self.last_usage: Optional[dict[str, int]] = None
        self._provider_backoff_lock = threading.Lock()
        self._provider_failure_streak = 0
        self._provider_not_before = 0.0

    def reconfigure(
        self, api_key: Optional[str] = None, model_name: Optional[str] = None
    ) -> None:
        if api_key is not None:
            self.api_key = api_key
        if model_name and model_name != self.model_name:
            logger.info(
                f"{self.provider_name} model changed: "
                f"{self.model_name} -> {model_name}"
            )
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
                "overloaded",
                "500",
                "502",
                "503",
                "504",
                "unavailable",
                "timed out",
                "timeout",
            )
        )

    async def _wait_for_provider(self) -> None:
        with self._provider_backoff_lock:
            wait = max(0.0, self._provider_not_before - time.monotonic())
        if wait:
            await asyncio.sleep(math.ceil(wait * 1000.0) / 1000.0)

    def _record_provider_failure(self) -> float:
        with self._provider_backoff_lock:
            self._provider_failure_streak = min(self._provider_failure_streak + 1, 6)
            base = min(60.0, 2.0 * (2 ** (self._provider_failure_streak - 1)))
            delay = base + random.uniform(0, base / 4)
            self._provider_not_before = max(
                self._provider_not_before, time.monotonic() + delay
            )
            return max(0.0, self._provider_not_before - time.monotonic())

    def _record_provider_recovery(self) -> None:
        with self._provider_backoff_lock:
            self._provider_failure_streak = 0
            self._provider_not_before = 0.0

    async def _post(
        self,
        prompt: str,
        *,
        allowed_tags: Optional[Sequence[str]] = None,
        expected_ids: Optional[Sequence[int]] = None,
        allowed_indexes_by_id: Optional[Mapping[int, Sequence[int]]] = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _extract_usage(self, payload: dict[str, Any]) -> Optional[dict[str, int]]:
        raise NotImplementedError

    def _response_text(self, payload: dict[str, Any], usage: dict[str, int]) -> str:
        raise NotImplementedError

    async def _generate_with_backoff(
        self,
        prompt: str,
        *,
        allowed_tags: Optional[Sequence[str]] = None,
        expected_ids: Optional[Sequence[int]] = None,
        allowed_indexes_by_id: Optional[Mapping[int, Sequence[int]]] = None,
    ) -> tuple[dict[str, Any], dict[str, int]]:
        attempts = max(1, int(settings.gemini_max_attempts))
        output_reserve = _max_output_tokens(prompt)
        prior_usage: dict[str, int] = {}
        for attempt in range(1, attempts + 1):
            await self._wait_for_provider()
            try:
                payload = await self._post(
                    prompt,
                    allowed_tags=allowed_tags,
                    expected_ids=expected_ids,
                    allowed_indexes_by_id=allowed_indexes_by_id,
                )
            except Exception as exc:  # noqa: BLE001 - classified below
                prior_usage = _merge_usage(
                    prior_usage,
                    _estimated_usage(
                        prompt, "", output_reserve=output_reserve, attempts=1
                    ),
                )
                if not self._is_transient(exc) or attempt >= attempts:
                    self.last_usage = prior_usage
                    raise TaggingRequestFailed(
                        f"{self.provider_name} {type(exc).__name__}: {exc}",
                        usage=prior_usage,
                    ) from exc
                wait = self._record_provider_failure()
                logger.warning(
                    f"{self.provider_name} {type(exc).__name__} "
                    f"(attempt {attempt}/{attempts}); reserved possible spend, "
                    f"shared provider cooldown {wait:.1f}s"
                )
                continue

            measured = self._extract_usage(payload)
            if measured is None:
                reserved = _estimated_usage(
                    prompt,
                    json.dumps(payload, separators=(",", ":")),
                    output_reserve=output_reserve,
                )
                usage = _merge_usage(prior_usage, reserved)
                self.last_usage = usage
                raise TaggingUsageUnavailable(
                    f"{self.provider_name} response omitted usage; refusing unmetered work",
                    usage=usage,
                )
            self._record_provider_recovery()
            usage = _merge_usage(prior_usage, measured)
            self.last_usage = usage
            return payload, usage
        raise RuntimeError("unreachable")

    async def tag_items_raw(
        self,
        prompt: str,
        *,
        allowed_tags: Optional[Sequence[str]] = None,
        expected_ids: Optional[Sequence[int]] = None,
        allowed_indexes_by_id: Optional[Mapping[int, Sequence[int]]] = None,
    ) -> tuple[list[dict], dict[str, int]]:
        payload, usage = await self._generate_with_backoff(
            prompt,
            allowed_tags=allowed_tags,
            expected_ids=expected_ids,
            allowed_indexes_by_id=allowed_indexes_by_id,
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
            if not salvaged:
                raise TaggingRequestFailed(
                    f"{self.provider_name} returned malformed JSON: {exc}",
                    usage=usage,
                ) from exc
            logger.warning(f"Salvaged {len(salvaged)} complete items")
            items = salvaged
        else:
            raw_items = decoded.get("items") if isinstance(decoded, dict) else decoded
            if isinstance(raw_items, list):
                items = raw_items
            elif isinstance(raw_items, dict):
                items = []
                for raw_id, raw_item in raw_items.items():
                    if not isinstance(raw_item, dict):
                        continue
                    try:
                        tmdb_id = int(raw_id)
                    except (TypeError, ValueError):
                        continue
                    items.append({"tmdb_id": tmdb_id, **raw_item})
            else:
                items = []

        names = list(dict.fromkeys(str(name) for name in (allowed_tags or ()) if name))
        if names:
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
                    elif isinstance(tag.get("name"), str) and tag["name"] in names:
                        resolved.append(
                            {
                                "name": tag["name"],
                                "confidence": tag.get("confidence"),
                            }
                        )
                item["tags"] = resolved
        return items, usage


class OpenAITaggingEngine(StructuredRestTaggingEngine):
    provider_name = "OpenAI"

    async def _post(
        self,
        prompt: str,
        *,
        allowed_tags: Optional[Sequence[str]] = None,
        expected_ids: Optional[Sequence[int]] = None,
        allowed_indexes_by_id: Optional[Mapping[int, Sequence[int]]] = None,
    ) -> dict[str, Any]:
        body = {
            "model": self.model_name,
            "input": prompt,
            "max_output_tokens": _max_output_tokens(prompt),
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "curatio_tagging_batch",
                    "strict": True,
                    "schema": _response_schema(
                        allowed_tags,
                        expected_ids=expected_ids,
                        allowed_indexes_by_id=allowed_indexes_by_id,
                    ),
                }
            },
        }
        reasoning = _openai_reasoning(self.model_name)
        if reasoning is not None:
            body["reasoning"] = reasoning
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(float(settings.gemini_request_timeout))
        ) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "authorization": f"Bearer {self.api_key}",
                    "content-type": "application/json",
                },
                json=body,
            )
        self._raise_for_status(response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("OpenAI returned a non-object response")
        return payload

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            try:
                error = response.json().get("error") or {}
                detail = str(error.get("message") or response.text)
            except (ValueError, AttributeError):
                detail = response.text
            detail = " ".join(detail.split())[:800]
            raise httpx.HTTPStatusError(
                f"OpenAI HTTP {response.status_code}: {detail or 'no error detail'}",
                request=exc.request,
                response=exc.response,
            ) from exc

    def _extract_usage(self, payload: dict[str, Any]) -> Optional[dict[str, int]]:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return None
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if not isinstance(input_tokens, (int, float)) or not isinstance(
            output_tokens, (int, float)
        ):
            return None
        input_details = usage.get("input_tokens_details") or {}
        output_details = usage.get("output_tokens_details") or {}
        reasoning_tokens = max(0, int(output_details.get("reasoning_tokens") or 0))
        return {
            "prompt_token_count": max(0, int(input_tokens)),
            # The shared accountant adds visible and thinking tokens. OpenAI's
            # output_tokens already includes reasoning, so split it here.
            "candidates_token_count": max(0, int(output_tokens) - reasoning_tokens),
            "cached_content_token_count": max(
                0, int(input_details.get("cached_tokens") or 0)
            ),
            "thoughts_token_count": reasoning_tokens,
            "attempt_count": 1,
        }

    def _response_text(self, payload: dict[str, Any], usage: dict[str, int]) -> str:
        if payload.get("status") == "incomplete":
            detail = payload.get("incomplete_details") or {}
            reason = detail.get("reason") if isinstance(detail, dict) else str(detail)
            message = f"OpenAI returned an incomplete response: {reason or 'unknown'}"
            if reason == "content_filter":
                raise TaggingContentBlocked(message, usage=usage)
            raise TaggingRequestFailed(message, usage=usage)
        texts = []
        refusal = ""
        for output in payload.get("output") or []:
            for content in output.get("content") or []:
                if content.get("type") == "output_text" and content.get("text"):
                    texts.append(str(content["text"]))
                if content.get("type") == "refusal":
                    refusal = str(content.get("refusal") or "refusal")
        text = "".join(texts)
        if text.strip():
            return text
        reason = refusal or str(payload.get("incomplete_details") or "empty response")
        raise TaggingContentBlocked(
            f"OpenAI returned no content: {reason}", usage=usage
        )


class AnthropicTaggingEngine(StructuredRestTaggingEngine):
    provider_name = "Anthropic"

    async def _post(
        self,
        prompt: str,
        *,
        allowed_tags: Optional[Sequence[str]] = None,
        expected_ids: Optional[Sequence[int]] = None,
        allowed_indexes_by_id: Optional[Mapping[int, Sequence[int]]] = None,
    ) -> dict[str, Any]:
        _ = allowed_indexes_by_id
        body = {
            "model": self.model_name,
            "max_tokens": _max_output_tokens(prompt),
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": _response_schema(
                        allowed_tags,
                        expected_ids=expected_ids,
                    ),
                }
            },
        }
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(float(settings.gemini_request_timeout))
        ) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
            )
        self._raise_for_status(response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Anthropic returned a non-object response")
        return payload

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            try:
                error = response.json().get("error") or {}
                detail = str(error.get("message") or response.text)
            except (ValueError, AttributeError):
                detail = response.text
            detail = " ".join(detail.split())[:800]
            raise httpx.HTTPStatusError(
                f"Anthropic HTTP {response.status_code}: {detail or 'no error detail'}",
                request=exc.request,
                response=exc.response,
            ) from exc

    def _extract_usage(self, payload: dict[str, Any]) -> Optional[dict[str, int]]:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return None
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if not isinstance(input_tokens, (int, float)) or not isinstance(
            output_tokens, (int, float)
        ):
            return None
        cache_tokens = int(usage.get("cache_read_input_tokens") or 0) + int(
            usage.get("cache_creation_input_tokens") or 0
        )
        return {
            "prompt_token_count": max(0, int(input_tokens)) + cache_tokens,
            "candidates_token_count": max(0, int(output_tokens)),
            "cached_content_token_count": max(0, cache_tokens),
            "thoughts_token_count": 0,
            "attempt_count": 1,
        }

    def _response_text(self, payload: dict[str, Any], usage: dict[str, int]) -> str:
        text = "".join(
            str(block.get("text") or "")
            for block in payload.get("content") or []
            if block.get("type") == "text"
        )
        if text.strip():
            return text
        reason = payload.get("stop_reason") or "empty response"
        raise TaggingContentBlocked(
            f"Anthropic returned no content: {reason}", usage=usage
        )


class LocalTaggingEngine(StructuredRestTaggingEngine):
    """A model behind an OpenAI-compatible Chat Completions API.

    Ollama, LM Studio, vLLM and llama.cpp's server all accept
    ``response_format: json_schema``. Local calls cost nothing, so a server
    that omits ``usage`` is accepted (counted as zero tokens).
    """

    provider_name = "Local"

    def __init__(self, api_key: str = "", model_name: str = "", base_url: str = ""):
        super().__init__(api_key=api_key, model_name=model_name)
        self.base_url = base_url

    async def _post(
        self,
        prompt: str,
        *,
        allowed_tags: Optional[Sequence[str]] = None,
        expected_ids: Optional[Sequence[int]] = None,
        allowed_indexes_by_id: Optional[Mapping[int, Sequence[int]]] = None,
    ) -> dict[str, Any]:
        if not self.base_url:
            raise ValueError("No local model address is configured")
        body = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": _max_output_tokens(prompt),
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "curatio_tagging_batch",
                    "strict": True,
                    "schema": _response_schema(
                        allowed_tags,
                        expected_ids=expected_ids,
                        allowed_indexes_by_id=allowed_indexes_by_id,
                    ),
                },
            },
        }
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        async with httpx.AsyncClient(
            # Local models are slow on modest hardware; allow them time.
            timeout=httpx.Timeout(max(float(settings.gemini_request_timeout), 300.0))
        ) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions", headers=headers, json=body
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = " ".join(response.text.split())[:800]
            raise httpx.HTTPStatusError(
                f"Local model HTTP {response.status_code}: {detail or 'no detail'}",
                request=exc.request,
                response=exc.response,
            ) from exc
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("The local model returned a non-object response")
        return payload

    def _extract_usage(self, payload: dict[str, Any]) -> Optional[dict[str, int]]:
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return {
            "prompt_token_count": max(0, int(usage.get("prompt_tokens") or 0)),
            "candidates_token_count": max(0, int(usage.get("completion_tokens") or 0)),
            "cached_content_token_count": 0,
            "thoughts_token_count": 0,
            "attempt_count": 1,
        }

    def _response_text(self, payload: dict[str, Any], usage: dict[str, int]) -> str:
        choices = payload.get("choices") or []
        message = (choices[0] or {}).get("message") if choices else None
        text = str((message or {}).get("content") or "")
        if text.strip():
            return text
        raise TaggingContentBlocked("The local model returned no content", usage=usage)


openai_engine = OpenAITaggingEngine(
    api_key=settings.openai_api_key, model_name=settings.openai_model
)
anthropic_engine = AnthropicTaggingEngine(
    api_key=settings.anthropic_api_key, model_name=settings.anthropic_model
)
local_engine = LocalTaggingEngine(
    api_key=settings.local_api_key,
    model_name=settings.local_model,
    base_url=str(settings.local_base_url or "").strip().rstrip("/"),
)
