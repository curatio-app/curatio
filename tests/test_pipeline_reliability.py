# ruff: noqa: E402
"""Regression guards for the six defects the first full production run exposed.

Every test here fails on the code that produced this log:

    mdblist: 429, daily quota exhausted — stopping cleanly
    mdblist enrich complete: {'enriched': 8343, 'missed': 274232, 'stopped': False}
    RETAG COMPLETE — 1,336 titles ... API calls 103 ... measured cost $0.00
    TMDB request failed: <asyncio.locks.Event object ...> is bound to a
        different event loop  ->  502 Bad Gateway on series metadata requests

Read together they describe one run that stopped at 2.4% of the library while
reporting itself finished, spent an unmeasured amount of money with its ceiling
inert, and took client metadata requests down while it did so.
"""

import asyncio
import os
import random
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_pipeline_reliability.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")


# ---------------------------------------------------------------------------
# D10 — pacing, and a 429 that is not the end of the day
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, payload=None, status_code=200, headers=None):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.headers = headers or {}
        self.text = ""

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_requests_are_paced_to_the_configured_rate():
    """Concurrency bounds requests in flight; only the limiter bounds the rate.

    Unpaced, ``mdblist_concurrency`` requests went out as fast as responses
    returned -- which tripped a 429 after ~8,184 requests against a 500,000/day
    key, 1.6% of quota.
    """
    from app.config import settings
    from app.mdblist_client import MDBListClient
    from app.rate_limit import RateLimiter

    client = MDBListClient("k")
    # 20/s keeps the test well under a second while still being measurable.
    client._limiter = RateLimiter(20.0)

    stamps: list[float] = []

    class _Recorder:
        async def get(self, path, params=None):
            stamps.append(time.monotonic())
            return _Resp({"ok": True})

    async def _http():
        return _Recorder()

    client._http = _http  # type: ignore[method-assign]

    await asyncio.gather(*(client._get("/x") for _ in range(10)))

    assert len(stamps) == 10
    span = max(stamps) - min(stamps)
    # 10 calls at 20/s cannot complete in under ~0.45s however wide the fan-out.
    assert span >= 0.4, (
        f"10 paced calls spanned only {span:.3f}s — the limiter is not admitting "
        "them one interval apart, which is the bug that tripped MDBList's rate "
        "limit at 1.6% of quota"
    )
    assert settings.mdblist_rps > 0, "the shipped default must actually pace"


@pytest.mark.asyncio
async def test_a_transient_429_is_retried_not_fatal(monkeypatch):
    """One 429 is a rate limit. It must not end the day's enrichment."""
    from app.mdblist_client import MDBListClient

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    client = MDBListClient("k")
    calls = {"n": 0}

    class _Flaky:
        async def get(self, path, params=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return _Resp(None, status_code=429)
            return _Resp({"title": "ok"})

    async def _http():
        return _Flaky()

    client._http = _http  # type: ignore[method-assign]

    result = await client._get("/tmdb/movie/603/")
    assert result == {"title": "ok"}, "a retried 429 must return the real payload"
    assert calls["n"] == 2
    assert not client._exhausted, (
        "one 429 latched _exhausted, which is what ended a full-library "
        "enrichment at 2.4% with 98.4% of the quota unspent"
    )


@pytest.mark.asyncio
async def test_repeated_429s_stop_the_day_and_say_it_is_a_rate_limit(monkeypatch):
    from app.mdblist_client import MAX_RATE_LIMIT_RETRIES, MDBListClient

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    client = MDBListClient("k")
    calls = {"n": 0}

    class _AlwaysLimited:
        async def get(self, path, params=None):
            calls["n"] += 1
            return _Resp(None, status_code=429)

    async def _http():
        return _AlwaysLimited()

    client._http = _http  # type: ignore[method-assign]

    messages: list[str] = []
    from loguru import logger as loguru_logger

    sink = loguru_logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        assert await client._get("/tmdb/movie/603/") is None
    finally:
        loguru_logger.remove(sink)

    assert calls["n"] == MAX_RATE_LIMIT_RETRIES
    assert client._exhausted
    assert any("RATE limit" in m for m in messages), (
        "the operator must be told this is a rate limit rather than a spent "
        f"quota — that distinction is the whole fix. Captured: {messages}"
    )


@pytest.mark.asyncio
async def test_a_429_with_the_quota_spent_stops_immediately(monkeypatch):
    """A genuinely exhausted quota should not burn retries."""
    from app.mdblist_client import MDBListClient

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    client = MDBListClient("k")
    client._limit_seen = 1000
    client._used_today = 1000  # remaining == 0
    calls = {"n": 0}

    class _Limited:
        async def get(self, path, params=None):
            calls["n"] += 1
            return _Resp(None, status_code=429)

    async def _http():
        return _Limited()

    client._http = _http  # type: ignore[method-assign]
    client._exhausted = False
    # _blocked() would short-circuit on remaining<=0, so exercise _send directly.
    assert await client._send("GET /x", _Limited().get.__get__(_Limited())) is None


async def _no_sleep(_seconds):
    return None


# ---------------------------------------------------------------------------
# D9 — a quota wall must not look like a finished run
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_enrich_stops_when_the_client_latches_and_reports_deferred():
    """The loop's only exits were stop_event and remaining<=0.

    A latched client refuses requests, so ``_used_today`` stops rising and
    ``remaining`` freezes -- meaning neither exit can ever fire. The loop walked
    the remaining ~272k targets in about a second, counted them all as
    ``missed``, logged progress the whole way and returned ``stopped: False``.
    """
    from app.database import get_db, init_database
    from app.models import MediaMetadata
    from workers.mdb_sync import enrich_titles

    init_database()
    # More titles than one concurrency window covers, or the loop never reaches
    # a second iteration and the exhaustion check is never exercised.
    total = 200
    with get_db() as db:
        # Everything, not just this test's ids: enrich_titles selects every row
        # lacking the current mdb_version, so leftovers from other tests in this
        # module would land in the target set and skew the accounting assertion.
        db.query(MediaMetadata).delete()
        for i in range(total):
            db.add(
                MediaMetadata(
                    tmdb_id=900000 + i,
                    media_type="movie",
                    title=f"T{i}",
                    popularity=100.0 - i,
                )
            )
        db.commit()

        class _LatchingClient:
            """Serves one batch, then behaves exactly like a rate-limited client."""

            def __init__(self):
                self._exhausted = False
                self.batches = 0
                self._limit_seen = 500000
                self._used_today = 10
                self.max_batch_ids = 10

            @property
            def remaining(self):
                return self._limit_seen - self._used_today

            async def title_info_batch(self, ids, media_type):
                if self._exhausted:
                    return []
                self.batches += 1
                if self.batches >= 2:
                    self._exhausted = True
                return [{"ids": {"tmdb": i}} for i in ids]

            def extract_facts(self, payload):
                return {"keywords": []}

            def extract_ratings(self, payload):
                return []

            def _persist_quota(self):
                return None

        client = _LatchingClient()
        stats = await enrich_titles(db, client, None, "movie")

    assert stats["deferred"] > 0, (
        "a run halted by the client must report what it never attempted; "
        "collapsing that into `missed` is what made a 2.4% run look complete"
    )
    # Every requested id lands in exactly one bucket. Collapsing these is what
    # let a run storing 0.7% of its fetches report a clean finish.
    assert (
        stats["enriched"]
        + stats["unmatched_payload"]
        + stats["no_local_row"]
        + stats["missed"]
        + stats["deferred"]
        == total
    )
    assert client.batches <= 2, (
        f"the loop kept calling after the client latched ({client.batches} "
        "batches) — that is the phantom progress from the production log"
    )


# ---------------------------------------------------------------------------
# D11 — cost accounting that works on the pinned SDK
# ---------------------------------------------------------------------------
def test_usage_falls_back_when_the_sdk_reports_none():
    """``google-generativeai==0.3.2`` has no ``usage_metadata`` on the response.

    So every real run measured zero tokens, printed ``$0.00``, and compared that
    zero against ``retag_cost_ceiling_usd`` -- running with no ceiling at all.
    """
    from app.gemini_client import _estimated_usage, _extract_usage

    class _OldSdkResponse:
        text = "x" * 400

    assert _extract_usage(_OldSdkResponse()) is None

    usage = _estimated_usage("p" * 8000, "x" * 400)
    assert usage["prompt_token_count"] > 0
    assert usage["candidates_token_count"] > 0
    assert usage["estimated"] == 1


@pytest.mark.asyncio
async def test_rest_client_refuses_a_success_without_usage(monkeypatch):
    """An unmetered success must stop, not retry and double the bill."""
    from app.gemini_client import GeminiTaggingEngine, GeminiUsageUnavailable

    engine = GeminiTaggingEngine(api_key="test")
    calls = {"n": 0}

    async def _post(_prompt):
        calls["n"] += 1
        return {"candidates": [{"content": {"parts": [{"text": '{"items":[]}'}]}}]}

    monkeypatch.setattr(engine, "_post", _post)
    with pytest.raises(GeminiUsageUnavailable) as error:
        await engine.tag_items_raw("prompt")

    assert calls["n"] == 1
    assert error.value.usage.get("estimated") == 1
    assert error.value.usage.get("prompt_token_count", 0) > 0


@pytest.mark.asyncio
async def test_transient_retry_reserves_the_unmetered_attempt(monkeypatch):
    """A timeout may still bill; its reservation survives a later success."""
    from app.gemini_client import GeminiTaggingEngine

    engine = GeminiTaggingEngine(api_key="test")
    calls = {"n": 0}

    async def _post(_prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise asyncio.TimeoutError()
        return {
            "candidates": [{"content": {"parts": [{"text": '{"items":[]}'}]}}],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 5,
            },
        }

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(engine, "_post", _post)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    items, usage = await engine.tag_items_raw("p" * 300)

    assert items == []
    assert calls["n"] == 2
    assert usage["attempt_count"] == 2
    assert usage["estimated"] == 1
    assert usage["prompt_token_count"] > 10


def test_rest_request_disables_thinking_and_requires_structured_json():
    import json

    from app.gemini_client import GeminiTaggingEngine

    config = GeminiTaggingEngine._request_body(
        "\n### 1 | movie\n### 2 | movie",
        allowed_tags=["Heist", "Drama"],
        expected_ids=[1, 2],
    )["generationConfig"]
    assert config["thinkingConfig"]["thinkingBudget"] == 0
    assert config["responseMimeType"] == "application/json"
    assert "responseJsonSchema" not in config

    schema = config["responseSchema"]
    assert schema["required"] == ["items"]
    items = schema["properties"]["items"]
    assert "minItems" not in items and "maxItems" not in items
    item = items["items"]
    assert item["properties"]["tmdb_id"] == {"type": "INTEGER"}
    tags = item["properties"]["tags"]
    assert "minItems" not in tags
    assert tags["maxItems"] == 3
    choice = tags["items"]["properties"]["tag_index"]
    assert choice == {"type": "INTEGER", "minimum": 0, "maximum": 1}
    confidence = tags["items"]["properties"]["confidence"]
    assert confidence == {"type": "NUMBER"}
    assert "enum" not in json.dumps(schema)


@pytest.mark.asyncio
async def test_rest_client_maps_schema_tag_indexes_to_canonical_names(monkeypatch):
    from app.gemini_client import GeminiTaggingEngine

    engine = GeminiTaggingEngine(api_key="x", model_name="m")

    async def _generate(*_args, **_kwargs):
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": (
                                    '{"items":[{"tmdb_id":1,"tags":['
                                    '{"tag_index":1,"confidence":0.9},'
                                    '{"tag_index":99,"confidence":0.8}]}]}'
                                )
                            }
                        ]
                    }
                }
            ]
        }, {"prompt_token_count": 1, "candidates_token_count": 1}

    monkeypatch.setattr(engine, "_generate_with_backoff", _generate)
    items, _usage = await engine.tag_items_raw(
        "prompt", allowed_tags=["Heist", "Drama"], expected_ids=[1]
    )

    assert items[0]["tags"] == [{"name": "Drama", "confidence": 0.9}]


@pytest.mark.asyncio
async def test_malformed_gemini_json_preserves_measured_usage(monkeypatch):
    """A paid malformed response must enter the metered provider-error path."""
    from app.gemini_client import GeminiRequestFailed, GeminiTaggingEngine

    engine = GeminiTaggingEngine(api_key="x", model_name="m")
    usage = {"prompt_token_count": 23, "candidates_token_count": 17}

    async def _generate(*_args, **_kwargs):
        return {
            "candidates": [{"content": {"parts": [{"text": '{"items":[not-json'}]}}]
        }, usage

    monkeypatch.setattr(engine, "_generate_with_backoff", _generate)
    with pytest.raises(GeminiRequestFailed, match="malformed JSON") as error:
        await engine.tag_items_raw("prompt")

    assert error.value.usage == usage


@pytest.mark.asyncio
async def test_rest_client_preserves_gemini_error_detail(monkeypatch):
    import httpx

    from app.gemini_client import GeminiRequestFailed, GeminiTaggingEngine

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **_kwargs):
            request = httpx.Request("POST", url)
            return httpx.Response(
                400,
                request=request,
                json={"error": {"message": "schema has too many states"}},
            )

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: _Client())
    engine = GeminiTaggingEngine(api_key="x", model_name="m")

    with pytest.raises(GeminiRequestFailed, match="schema has too many states"):
        await engine.tag_items_raw("prompt")


def test_output_budget_fits_a_complete_safe_batch():
    """The v3 pilot omitted 146/320 titles under the old 220-token budget."""
    from app.gemini_client import GeminiTaggingEngine, OUTPUT_TOKENS_PER_TITLE
    from workers.tagging_common import (
        MAX_SAFE_TAGGING_BATCH_SIZE,
        effective_batch_size,
    )

    prompt = "\n".join(
        f"### {tmdb_id} | movie" for tmdb_id in range(MAX_SAFE_TAGGING_BATCH_SIZE)
    )
    budget = GeminiTaggingEngine._max_output_tokens(prompt)

    assert effective_batch_size(40) == 20
    assert budget >= MAX_SAFE_TAGGING_BATCH_SIZE * OUTPUT_TOKENS_PER_TITLE
    assert budget > 12_000


@pytest.mark.asyncio
async def test_the_spend_ceiling_halts_on_estimated_usage_alone(db):
    """The ceiling has to work off the fallback, or it is decorative."""
    from app.config import settings
    from workers.tagging_common import build_features, tag_and_store

    rows = [_title(db, tmdb_id=3100 + i) for i in range(6)]

    class _Expensive:
        model_name = "m"
        last_usage = None

        def __init__(self):
            self.calls = 0

        async def tag_items_raw(self, prompt, **_constraints):
            self.calls += 1
            # No usage_metadata anywhere -- only the character-length fallback.
            # One call is worth more than half the ceiling, so a working guard
            # must stop admitting by the third.
            return [], {
                "prompt_token_count": 400_000_000,
                "candidates_token_count": 0,
                "estimated": 1,
            }

    engine = _Expensive()
    # Serial, so the ceiling is checked against a settled total rather than
    # against zero by every concurrently-admitted batch.
    original = settings.gemini_concurrency
    object.__setattr__(settings, "gemini_concurrency", 1)
    try:
        stats = await tag_and_store(
            db, [build_features(r) for r in rows], engine=engine, batch_size=1
        )
    finally:
        object.__setattr__(settings, "gemini_concurrency", original)

    assert stats["cost_usd"] > 0.0, "estimated tokens must produce a real number"
    assert stats["estimated_usage"] is True
    assert stats["cost_usd"] >= settings.retag_cost_ceiling_usd
    assert engine.calls < len(rows), (
        "the ceiling never stopped admitting batches — with usage stuck at zero "
        "this guard could not fire at all"
    )
    assert "ceiling" in stats["first_error"]


@pytest.mark.asyncio
async def test_usage_is_returned_not_read_off_the_shared_engine(db):
    """Concurrent batches share one engine; an attribute read races."""
    from workers.tagging_common import build_features, tag_and_store

    rows = [_title(db, tmdb_id=3200 + i) for i in range(4)]

    class _Racy:
        model_name = "m"
        # Deliberately wrong: a caller reading this instead of the return value
        # would bill every batch at zero.
        last_usage = {"prompt_token_count": 0, "candidates_token_count": 0}

        async def tag_items_raw(self, prompt, **_constraints):
            return [], {"prompt_token_count": 1000, "candidates_token_count": 500}

    stats = await tag_and_store(
        db, [build_features(r) for r in rows], engine=_Racy(), batch_size=1
    )
    assert stats["input_tokens"] == 4000
    assert stats["output_tokens"] == 2000
    assert stats["missing_retry_calls"] == 0


# ---------------------------------------------------------------------------
# D13 — 504s and safety blocks must not cost forty titles each
# ---------------------------------------------------------------------------
def test_timeouts_and_server_errors_are_transient():
    """98,270 of 132,912 selected titles were tagged; 504s are most of the gap."""
    from app.gemini_client import GeminiTaggingEngine

    transient = GeminiTaggingEngine._is_transient
    assert transient(Exception("504 The request timed out"))
    assert transient(Exception("503 Service Unavailable"))
    assert transient(Exception("500 Internal error encountered"))
    assert transient(asyncio.TimeoutError())
    assert transient(Exception("429 Resource has been exhausted"))
    assert not transient(Exception("404 model not found")), (
        "a wrong model name must still fail fast — retrying it four times per "
        "batch would just make the outage slower"
    )


@pytest.mark.asyncio
async def test_concurrent_503s_share_a_provider_cooldown(monkeypatch):
    """One Gemini capacity incident must not create four independent retry storms."""
    from app.config import settings
    from app.gemini_client import GeminiTaggingEngine

    engine = GeminiTaggingEngine(api_key="x", model_name="m")
    calls = 0
    waits = []
    real_sleep = asyncio.sleep

    async def _post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RuntimeError("503 model is experiencing high demand")
        return {
            "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 2,
            },
        }

    async def _sleep(delay):
        waits.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(engine, "_post", _post)
    monkeypatch.setattr(asyncio, "sleep", _sleep)
    monkeypatch.setattr(random, "uniform", lambda *_args: 0.0)
    monkeypatch.setattr(settings, "gemini_max_attempts", 3, raising=False)

    results = await asyncio.gather(
        engine._generate_with_backoff("prompt one"),
        engine._generate_with_backoff("prompt two"),
    )

    assert len(results) == 2
    assert calls == 4
    assert waits
    assert max(waits) == pytest.approx(4.0, abs=0.1)


def test_default_gemini_attempt_budget_can_ride_out_a_capacity_burst():
    from app.config import settings

    assert type(settings).model_fields["gemini_max_attempts"].default == 8


@pytest.mark.asyncio
async def test_a_blocked_optional_call_keeps_every_titles_factual_tags(db):
    from app.gemini_client import GeminiContentBlocked
    from app.models import MediaMetadata
    from workers.tagging_common import build_features, tag_and_store

    poison = 3399
    rows = [_title(db, tmdb_id=3300 + i) for i in range(8)]
    rows.append(_title(db, tmdb_id=poison))
    for row in rows:
        row.overview = "A cerebral thief plans an elaborate heist."
        row.tag_facts = {"keywords": ["heist", "based on novel or book"]}
    db.commit()

    class _BlocksOnOneTitle:
        model_name = "m"
        last_usage = None

        async def tag_items_raw(self, prompt, **_constraints):
            if str(poison) in prompt:
                raise GeminiContentBlocked("BlockReason enum value: 4")
            tags = [["Heist", 0.85], ["Based on a Book", 0.9]]
            return [{"tmdb_id": r.tmdb_id, "tags": tags} for r in rows], {
                "prompt_token_count": 10,
                "candidates_token_count": 10,
            }

    stats = await tag_and_store(
        db, [build_features(r) for r in rows], engine=_BlocksOnOneTitle()
    )

    assert stats["api_calls"] == 1, "optional nuance must never trigger bisection"
    assert stats["blocked"] == len(rows)
    assert stats["provider_failures"] == len(rows)
    assert stats["failed"] == 0
    assert stats["processed"] == len(rows)
    db.expire_all()
    blocked_row = (
        db.query(MediaMetadata).filter_by(tmdb_id=poison, media_type="movie").one()
    )
    assert blocked_row.tag_attempt_status is None
    assert stats["terminal_skips"] == 0


# ---------------------------------------------------------------------------
# D12 — one connection pool per event loop
# ---------------------------------------------------------------------------
def test_tmdb_client_pools_per_event_loop():
    """The 502s on metadata requests while an upgrade ran.

    ``tmdb_client`` is a module singleton and the upgrade pipeline now runs on a
    worker thread with its own loop, so two loops shared one httpx pool. A
    pooled connection carries asyncio primitives bound to its creating loop, and
    whichever loop picked up the other's connection raised
    "<asyncio.locks.Event object ...> is bound to a different event loop".
    """
    from app.tmdb_client import TMDBClient

    client = TMDBClient(api_key="k")
    seen = {}

    async def _grab(label):
        seen[label] = client._loop_client()

    loop_a = asyncio.new_event_loop()
    loop_b = asyncio.new_event_loop()
    try:
        loop_a.run_until_complete(_grab("a"))
        loop_a.run_until_complete(_grab("a2"))
        loop_b.run_until_complete(_grab("b"))
    finally:
        loop_a.close()
        loop_b.close()

    assert seen["a"] is seen["a2"], "one pool per loop, not one per call"
    assert seen["a"] is not seen["b"], (
        "both loops got the same httpx pool — that is the cross-loop asyncio "
        "primitive that turned into 502s on the metadata endpoints"
    )


# ---------------------------------------------------------------------------
# D14 — the tagger must not race the enricher
# ---------------------------------------------------------------------------
def test_the_retag_waits_for_both_fetch_stages():
    """Tagging a title before its MDBList reviews land buys the work twice.

    ``stale_facts`` selects exactly those titles for a second, ~2x-prompt pass.
    The first run ended with 98,270 tagged and 8,343 enriched: ~91,000 re-tags
    pre-bought by running the stages together.
    """
    import inspect

    from workers import upgrade

    source = inspect.getsource(upgrade.run_upgrade)
    assert 'CONCURRENT = ("enrich", "mdblist")' in source
    assert '"retag"' in source.split("SEQUENTIAL")[1].split("\n")[0], (
        "retag must sit in SEQUENTIAL, after both fetch stages — in CONCURRENT "
        "it races MDBList and every title it beats becomes a paid re-tag"
    )


def test_the_upgrade_no_longer_calls_the_invented_list_slugs():
    """'top-rated-movies'/'top-rated-shows' do not exist; both 404 every run.

    They are why the card read "0 titles rated" while enrichment worked. The
    per-title batch endpoint returns ratings in the same payload, so the stage
    was redundant as well as broken.
    """
    from pathlib import Path

    source = Path(__file__).parent.parent / "workers" / "upgrade.py"
    text = source.read_text()
    call_sites = [
        line
        for line in text.splitlines()
        if "top-rated-movies" in line and not line.strip().startswith("#")
    ]
    assert not call_sites, f"invented slugs still requested: {call_sites}"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def db():
    from app.database import get_db, init_database

    init_database()
    with get_db() as session:
        yield session


def _title(db, tmdb_id=3000, **kw):
    from app.models import MediaMetadata

    row = db.query(MediaMetadata).filter(MediaMetadata.tmdb_id == tmdb_id).first()
    if row is None:
        row = MediaMetadata(
            tmdb_id=tmdb_id,
            media_type="movie",
            title=kw.get("title", f"Title {tmdb_id}"),
            overview=kw.get(
                "overview", "A cerebral hacker uncovers a conspiracy about reality."
            ),
            genres=["Action", "Science Fiction"],
            release_date="1999-03-30",
            original_language="en",
            popularity=50.0,
            vote_count=20000,
        )
        db.add(row)
        db.commit()
    return row


# ---------------------------------------------------------------------------
# D15 — a personalized slot's media type must be a property of its id
# ---------------------------------------------------------------------------
def _slot_media_type(slot_id: str) -> str | None:
    """The media type a typed slot id declares (``byw-series-2`` -> ``tv``)."""
    if slot_id.endswith("-series") or "-series-" in slot_id:
        return "tv"
    if slot_id.endswith("-movie") or "-movie-" in slot_id:
        return "movie"
    return None


def test_every_slot_id_declares_its_media_type():
    """A slot whose type came from its *content* flipped between syncs.

    Clients cached a row's type, so once ``byw-1`` changed from series to movie
    the installed client kept asking for the series variant, every item was
    dropped as a type mismatch, and the row rendered empty. Observed in
    production with all five BYW rows requested as ``series`` while all five
    held movies.
    """
    from workers.personalized_sync import SLOT_ORDER

    # discover-acclaimed is pinned to a literal media_type at its single write
    # site, so its id need not carry it. Up Next is no longer a discovery slot;
    # short activity data is served separately from shelves.
    exempt = {"discover-acclaimed"}
    undeclared = [
        slot
        for slot in SLOT_ORDER
        if slot not in exempt and _slot_media_type(slot) is None
    ]
    assert not undeclared, (
        "these slot ids do not encode a media type, so their row type is "
        f"whatever content last landed in them: {undeclared}"
    )


def test_slot_ids_map_to_the_type_they_name():
    from workers.personalized_sync import typed_slot_id

    assert typed_slot_id("byw", "tv", 2) == "byw-series-2"
    assert typed_slot_id("byw", "movie", 1) == "byw-movie-1"
    assert _slot_media_type("byw-series-2") == "tv"
    assert _slot_media_type("byw-movie-1") == "movie"
    assert _slot_media_type("taste-series-1") == "tv"
    assert _slot_media_type("gems-series") == "tv"
    assert _slot_media_type("picks-movie") == "movie"
    # Legacy ids declare nothing and must keep being served as before.
    assert _slot_media_type("byw-1") is None
    assert _slot_media_type("up-next") is None


def test_a_slot_type_survives_seeds_of_the_opposite_type():
    """The invariant the old numbering could not hold.

    Whatever mix of seeds arrives, slot N of a family is always the same type.
    """
    from workers.personalized_sync import BYW_SLOTS, typed_slot_id

    for seeds in (
        ["movie", "movie", "tv", "movie", "tv"],
        ["tv", "tv", "movie", "movie", "movie"],
        ["tv"] * 5,
    ):
        used = {"movie": 0, "tv": 0}
        assigned = []
        for media_type in seeds:
            if used[media_type] >= BYW_SLOTS[media_type]:
                continue
            used[media_type] += 1
            assigned.append(
                (typed_slot_id("byw", media_type, used[media_type]), media_type)
            )
        for slot_id, media_type in assigned:
            expected = "series" if media_type == "tv" else "movie"
            assert f"-{expected}-" in slot_id, (
                f"slot {slot_id} was assigned {media_type} content — the id and "
                "the content must never disagree"
            )


# ---------------------------------------------------------------------------
# D21/D22 — the supporter batch cap, and titles MDBList does not have
# ---------------------------------------------------------------------------
class _AbsentClient:
    """MDBList with no record of anything asked for."""

    def __init__(self, max_batch_ids=100):
        self._exhausted = False
        self._limit_seen = 500000
        self._used_today = 10
        self.max_batch_ids = max_batch_ids
        self.sizes: list[int] = []

    @property
    def remaining(self):
        return self._limit_seen - self._used_today

    async def title_info_batch(self, ids, media_type):
        self.sizes.append(len(ids))
        return []

    def extract_facts(self, payload):
        return {}

    def extract_ratings(self, payload):
        return []

    def _persist_quota(self):
        return None


@pytest.mark.asyncio
async def test_absent_titles_are_marked_so_they_leave_the_target_set():
    """27,776 titles at 0.4 popularity were re-requested on every single run.

    MDBList is IMDb/Trakt-rooted and does not carry the obscure tail of a TMDB
    library. Nothing marked them, so they stayed in the selection query forever
    and every run paid to rediscover the same absence.
    """
    from app.database import get_db, init_database
    from app.models import MediaMetadata
    from workers.mdb_sync import MDB_ABSENT_VERSION, enrich_titles

    init_database()
    with get_db() as db:
        db.query(MediaMetadata).delete()
        for i in range(30):
            db.add(
                MediaMetadata(
                    tmdb_id=800000 + i,
                    media_type="movie",
                    title=f"Obscure {i}",
                    popularity=0.4,
                )
            )
        db.commit()

        stats = await enrich_titles(db, _AbsentClient(), None, "movie")
        assert stats["enriched"] == 0
        assert stats["absent"] == 30, (
            "every title MDBList had no record of must be marked, or it "
            f"re-enters the target set next run: {stats}"
        )

        marked = (
            db.query(MediaMetadata)
            .filter(MediaMetadata.mdb_version == MDB_ABSENT_VERSION)
            .count()
        )
        assert marked == 30
        # The sentinel is not the success version: a reader, and the repair
        # queries, can still tell "absent" from "enriched".
        assert (
            db.query(MediaMetadata).filter(MediaMetadata.mdb_facts.isnot(None)).count()
            == 0
        )

        # The point of all of it: a second run finds nothing left to do.
        again = await enrich_titles(db, _AbsentClient(), None, "movie")
        assert again["absent"] == 0
        assert again["deferred"] == 0
        assert again["missed"] == 0


@pytest.mark.asyncio
async def test_a_supporter_key_actually_gets_the_larger_batch():
    """`min(setting, cap)` can only LOWER the batch.

    So a key reporting batch_cap=100 still ran at the setting's default of 10,
    and the tenfold reduction in requests never happened. Observed in production
    as "supporter=True batch_cap=100" followed immediately by "2,778 batch
    requests of up to 10".
    """
    from app.config import settings
    from app.database import get_db, init_database
    from app.models import MediaMetadata
    from workers.mdb_sync import DEFAULT_BATCH_SIZE, enrich_titles

    assert settings.mdblist_batch_size == DEFAULT_BATCH_SIZE, (
        "this test pins the default-means-ask-the-key behaviour; if the shipped "
        "default changed, the assertion below needs rethinking"
    )

    init_database()
    with get_db() as db:
        db.query(MediaMetadata).delete()
        for i in range(100):
            db.add(
                MediaMetadata(
                    tmdb_id=810000 + i,
                    media_type="movie",
                    title=f"T{i}",
                    popularity=1.0,
                )
            )
        db.commit()

        supporter = _AbsentClient(max_batch_ids=100)
        await enrich_titles(db, supporter, None, "movie")
        assert max(supporter.sizes) == 100, (
            f"a supporter key must batch 100 ids, not {max(supporter.sizes)} — "
            "that difference is 3,543 requests for a full library versus 35,428"
        )

        # A regular key is still held to 10.
        db.query(MediaMetadata).update({MediaMetadata.mdb_version: None})
        db.commit()
        regular = _AbsentClient(max_batch_ids=10)
        await enrich_titles(db, regular, None, "movie")
        assert max(regular.sizes) == 10


@pytest.mark.asyncio
async def test_the_stopping_message_names_the_right_cause():
    """Absence and a shape mismatch need opposite responses.

    The first version of this message asserted "the response shape does not
    match" unconditionally, and printed it against a run whose shape counters
    were both zero — sending the reader after a bug that did not exist.
    """
    from app.database import get_db, init_database
    from app.models import MediaMetadata
    from loguru import logger as loguru_logger
    from workers.mdb_sync import enrich_titles

    init_database()
    with get_db() as db:
        db.query(MediaMetadata).delete()
        for i in range(600):
            db.add(
                MediaMetadata(
                    tmdb_id=820000 + i,
                    media_type="movie",
                    title=f"T{i}",
                    popularity=0.4,
                )
            )
        db.commit()

        messages: list[str] = []
        sink = loguru_logger.add(lambda m: messages.append(str(m)), level="ERROR")
        try:
            await enrich_titles(db, _AbsentClient(), None, "movie")
        finally:
            loguru_logger.remove(sink)

    stopping = [m for m in messages if "STOPPING" in m]
    assert stopping, f"the yield guard should have fired: {messages}"
    assert any("no record of them" in m for m in stopping), (
        "an all-absent run must be reported as absence, not as a shape "
        f"mismatch: {stopping}"
    )
    assert not any("shape does not match" in m for m in stopping)
