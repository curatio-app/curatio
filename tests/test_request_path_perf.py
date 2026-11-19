"""Request-path caching and session-handling regressions."""

from __future__ import annotations

import asyncio
import os
import threading

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_request_path_perf.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")


def test_overlay_never_mutates_the_cached_payload():
    from app.main import _apply_detail_dynamic_overlay

    episodes = [{"season": 1, "episode": 1}]
    item = {"media_type": "movie", "content_rating": "PG"}
    payload = {"item": item, "seasons": [{"episodes": episodes}], "similar": []}
    overlay = {"similar": [{"id": 2}], "_item": {"content_rating": "R"}}

    refreshed = _apply_detail_dynamic_overlay(payload, overlay)

    assert refreshed["item"]["content_rating"] == "R"
    assert refreshed["similar"] == [{"id": 2}]
    assert item == {"media_type": "movie", "content_rating": "PG"}
    assert payload["similar"] == []
    assert payload["item"] is item


def test_meta_cache_evicts_oldest_write_first(monkeypatch):
    from app import main

    main._meta_cache.clear()
    monkeypatch.setattr(main, "_META_CACHE_MAX", 2)
    try:
        main._meta_cache["a"] = (1.0, {})
        main._meta_cache["b"] = (2.0, {})
        main._meta_cache["a"] = (3.0, {})  # rewrite moves "a" to newest
        main._meta_cache["c"] = (4.0, {})
        main._meta_cache_evict()
        assert list(main._meta_cache) == ["a", "c"]
    finally:
        main._meta_cache.clear()


@pytest.mark.asyncio
async def test_detail_type_repair_is_memoized(monkeypatch):
    from app import main

    main._meta_cache.clear()
    calls = []

    async def fake_db_call(fn, *args):
        calls.append(args)
        return ("tv", "requested_row")

    monkeypatch.setattr(main, "_detail_db_call", fake_db_call)
    try:
        assert await main._detail_media_type_repair(7, "tv") == ("tv", "requested_row")
        assert await main._detail_media_type_repair(7, "tv") == ("tv", "requested_row")
        assert calls == [(7, "tv")]
        main.clear_detail_response_cache()
        await main._detail_media_type_repair(7, "tv")
        assert len(calls) == 2
    finally:
        main._meta_cache.clear()


@pytest.mark.asyncio
async def test_request_db_call_releases_session_and_waits_on_cancel():
    from app.main import _request_db_call

    entered = threading.Event()
    release = threading.Event()
    events = []

    class FakeSession:
        def close(self):
            events.append("close")

    def work(db):
        entered.set()
        release.wait(5)
        events.append("work-done")
        return "ok"

    assert await _request_db_call(FakeSession(), lambda db: "ok") == "ok"
    assert events == ["close"]
    events.clear()

    task = asyncio.create_task(_request_db_call(FakeSession(), work))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()  # still waiting for the thread to leave the session
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["work-done", "close"]


def test_json_column_serializer_matches_stdlib_and_falls_back():
    import json

    from app.database import _json_serializer

    value = {"a": [1, 2.5, None, True], "b": {"c": "é"}, 3: "int key"}
    assert json.loads(_json_serializer(value)) == json.loads(json.dumps(value))
    huge = {"n": 2**70}  # beyond orjson's 64-bit integer range
    assert json.loads(_json_serializer(huge)) == huge


def test_tmdb_read_timeouts_are_not_retried_and_429_honours_retry_after():
    from types import SimpleNamespace

    import httpx

    from app.tmdb_client import _is_retryable, _retry_wait

    assert _is_retryable(httpx.ReadTimeout("slow")) is False

    request = httpx.Request("GET", "https://api.themoviedb.org/3/movie/1")
    limited = httpx.HTTPStatusError(
        "429",
        request=request,
        response=httpx.Response(429, headers={"Retry-After": "3"}, request=request),
    )

    def state(exc):
        outcome = SimpleNamespace(exception=lambda: exc, failed=True)
        return SimpleNamespace(outcome=outcome, attempt_number=1)

    assert _retry_wait(state(limited)) == 3.0
    assert _retry_wait(state(httpx.ConnectError("down"))) >= 2


@pytest.mark.asyncio
async def test_sync_backfill_remembers_ids_tmdb_does_not_have(monkeypatch):
    import httpx

    from workers import personalized_sync

    request = httpx.Request("GET", "https://api.themoviedb.org/3/movie/404")
    calls = []

    async def get_movie(tmdb_id):
        calls.append(tmdb_id)
        raise httpx.HTTPStatusError(
            "404", request=request, response=httpx.Response(404, request=request)
        )

    class FakeQuery:
        def filter(self, *_):
            return self

        def all(self):
            return []

    class FakeDb:
        def query(self, *_):
            return FakeQuery()

        def flush(self):
            pass

    monkeypatch.setattr(personalized_sync.tmdb_client, "get_movie", get_movie)
    personalized_sync._backfill_not_found.clear()
    try:
        await personalized_sync._backfill_metadata(FakeDb(), {"movie": {404}})
        await personalized_sync._backfill_metadata(FakeDb(), {"movie": {404}})
        assert calls == [404]
    finally:
        personalized_sync._backfill_not_found.clear()
