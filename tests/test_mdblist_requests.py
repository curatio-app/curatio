"""
Tests for the shape of the MDBList requests themselves.

Every previous MDBList bug was in the *request*, not the handling: the client
asked for the wrong thing and then correctly processed the nothing it got back.
The extraction was fine; the review caps were fine; the code simply never asked
for reviews. So these pin the URL, the query string and the body rather than
the parsing.

The API reference these assert against:

    GET  /{media_provider}/{media_type}/{media_id}/
         ?append_to_response=extra,keyword,recommendations,review
    POST /{media_provider}/{media_type}/
         {"ids": [...], "append_to_response": ["keyword", ...]}

    media_provider: imdb, mal, mdblist, tmdb, trakt, tvdb
    media_type:     any, movie, show
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_mdblist_requests.db")
os.environ.setdefault("TMDB_API_KEY", "test")
os.environ.setdefault("GEMINI_API_KEY", "test")
os.environ.setdefault("MASTER_PASSWORD", "test")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SECRET_KEY", "t" * 40)
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.mdblist_client import (  # noqa: E402
    APPEND_FIELDS_LIST,
    MDBListClient,
)


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.headers: dict = {}
        self.text = ""

    def json(self):
        return self._payload


class _Recorder:
    """Stands in for httpx.AsyncClient and records what was asked for."""

    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {}
        self.gets: list[tuple[str, dict]] = []
        self.posts: list[tuple[str, dict, dict]] = []

    async def get(self, path, params=None):
        self.gets.append((path, dict(params or {})))
        return _Response(self.payload)

    async def post(self, path, params=None, json=None):
        self.posts.append((path, dict(params or {}), dict(json or {})))
        return _Response(self.payload)

    async def aclose(self):
        pass


@pytest.fixture()
def client(monkeypatch):
    engine = MDBListClient("test-key")
    recorder = _Recorder()

    async def _http():
        return recorder

    monkeypatch.setattr(engine, "_http", _http)
    engine.recorder = recorder  # type: ignore[attr-defined]
    return engine


# ---- the bug that produced zero reviews ----


@pytest.mark.asyncio
async def test_title_info_asks_for_reviews(client):
    """The whole reason mdb_facts never held a review: we never asked."""
    await client.title_info(603, "movie")

    path, params = client.recorder.gets[0]
    append = params["append_to_response"]
    assert "review" in append, (
        f"append_to_response={append!r} does not request reviews — this is "
        "exactly the omission that made every mdb_facts review-free"
    )
    assert "keyword" in append
    assert "extra" in append


@pytest.mark.asyncio
async def test_title_info_uses_the_documented_path(client):
    """Trailing slash, and 'show' rather than 'tv' for a series."""
    await client.title_info(603, "movie")
    await client.title_info(1396, "tv")

    movie_path = client.recorder.gets[0][0]
    show_path = client.recorder.gets[1][0]

    assert movie_path == "/tmdb/movie/603/"
    assert show_path == "/tmdb/show/1396/", (
        "MDBList's media_type values are any/movie/show — 'tv' is not one of "
        "them, and a wrong spelling makes every series silently miss"
    )


@pytest.mark.asyncio
async def test_recommendations_are_not_requested(client):
    """Valid, but nothing reads them and they are pure payload weight."""
    await client.title_info(603, "movie")
    assert "recommendations" not in client.recorder.gets[0][1]["append_to_response"]


# ---- the batch endpoint ----


@pytest.mark.asyncio
async def test_batch_posts_ids_and_appends_as_a_list(client):
    """The batch body takes a list where the GET takes a comma-joined string."""
    client.recorder.payload = []
    await client.title_info_batch([278, 238, 424], "movie")

    path, params, body = client.recorder.posts[0]
    assert path == "/tmdb/movie/"
    assert params["apikey"] == "test-key"
    assert body["ids"] == [278, 238, 424]
    assert isinstance(body["append_to_response"], list)
    assert "review" in body["append_to_response"]
    assert set(body["append_to_response"]) == set(APPEND_FIELDS_LIST)


@pytest.mark.asyncio
async def test_batch_returns_a_list_and_tolerates_an_envelope(client):
    client.recorder.payload = [{"id": 1}, {"id": 2}]
    assert len(await client.title_info_batch([1, 2], "movie")) == 2

    client.recorder.payload = {"results": [{"id": 3}]}
    assert len(await client.title_info_batch([3], "movie")) == 1

    client.recorder.payload = {"unexpected": "shape"}
    assert await client.title_info_batch([4], "movie") == []


@pytest.mark.asyncio
async def test_batch_with_no_ids_makes_no_request(client):
    assert await client.title_info_batch([], "movie") == []
    assert client.recorder.posts == []


# ---- the tier cap on batch size ----


@pytest.mark.asyncio
async def test_oversized_batch_is_split_not_sent(client):
    """The API allows 10 ids for a regular key, 100 for a supporter.

    Sending more than the ceiling would be rejected outright, losing every
    title in the batch. Splitting costs requests instead of results.
    """
    client.recorder.payload = [{"id": 1}]
    await client.title_info_batch(list(range(250)), "movie")

    sizes = [len(body["ids"]) for _, _, body in client.recorder.posts]
    assert sizes, "nothing was sent"
    assert max(sizes) <= 100, f"a batch exceeded the documented ceiling: {sizes}"
    assert sum(sizes) == 250, "splitting must not drop ids"


@pytest.mark.asyncio
async def test_an_empty_batch_result_is_retried_in_halves(client):
    """A regular key rejecting a 100-id batch must degrade, not lose titles."""
    seen: list[int] = []

    class _TierLimited(_Recorder):
        async def post(self, path, params=None, json=None):
            ids = (json or {}).get("ids", [])
            seen.append(len(ids))
            self.posts.append((path, dict(params or {}), dict(json or {})))
            # Mimic a tier that only accepts small batches.
            payload = [{"id": i} for i in ids] if len(ids) <= 10 else []
            return _Response(payload)

    recorder = _TierLimited()

    async def _http():
        return recorder

    client._http = _http  # type: ignore[method-assign]

    items = await client.title_info_batch(list(range(40)), "movie")

    assert len(items) == 40, (
        "halving should eventually deliver every title even when the tier "
        f"rejects the initial size; batch sizes attempted: {seen}"
    )


@pytest.mark.asyncio
async def test_a_genuinely_empty_single_id_does_not_recurse(client):
    """The halving must terminate rather than retry one id forever."""
    client.recorder.payload = []
    assert await client.title_info_batch([603], "movie") == []
    assert len(client.recorder.posts) == 1


# ---- matching results back to titles ----


def test_batch_items_are_matched_by_id_not_position():
    """Position matching would write one title's reviews onto another."""
    from workers.mdb_sync import _payload_tmdb_id

    assert _payload_tmdb_id({"tmdb_id": 603}) == 603
    assert _payload_tmdb_id({"ids": {"tmdb": 603}}) == 603
    # No id at all must be skipped rather than guessed at.
    assert _payload_tmdb_id({"title": "The Matrix"}) is None
    assert _payload_tmdb_id({}) is None


def test_a_bare_id_is_not_a_tmdb_id():
    """This assertion used to read ``_payload_tmdb_id({"id": 603}) == 603``.

    It was wrong, and it is why two full production runs stored 0.7% of what
    they fetched. A live probe of the real API settles it: ``id`` is MDBList's
    own primary key and the TMDB id lives at ``ids.tmdb``::

        {"id": 469990, "title": "The Matrix",
         "ids": {"imdb": "tt0133093", "trakt": 481, "tmdb": 603}}

    Reading ``id`` resolved every title to a row that does not exist, so every
    write was skipped -- silently, because the caller had already counted the
    payload as returned.
    """
    from workers.mdb_sync import _payload_tmdb_id

    real_payload = {
        "id": 469990,
        "title": "The Matrix",
        "ids": {"imdb": "tt0133093", "trakt": 481, "tmdb": 603, "tvdb": 169},
    }
    assert _payload_tmdb_id(real_payload) == 603, (
        "ids.tmdb must win over the bare id — matching on `id` is the defect "
        "that made MDBList enrichment store almost nothing"
    )

    # A payload carrying ONLY MDBList's own id resolves to nothing, rather than
    # confidently resolving to the wrong title.
    assert _payload_tmdb_id({"id": 469990}) is None

    # Confirmed identical for shows (Breaking Bad).
    assert (
        _payload_tmdb_id({"id": 406423, "type": "show", "ids": {"tmdb": 1396}}) == 1396
    )


# ---- failure visibility ----


@pytest.mark.asyncio
async def test_http_errors_are_logged_loudly(client, caplog):
    """These used to log at DEBUG, which is how two wrong request shapes ran
    in production reporting '0 titles rated' with no explanation."""

    class _Failing(_Recorder):
        async def get(self, path, params=None):
            self.gets.append((path, dict(params or {})))
            return _Response(None, status_code=404)

    recorder = _Failing()

    async def _http():
        return recorder

    client._http = _http  # type: ignore[method-assign]

    messages: list[str] = []
    from loguru import logger as loguru_logger

    sink_id = loguru_logger.add(lambda m: messages.append(m), level="WARNING")
    try:
        assert await client.title_info(603, "movie") is None
    finally:
        loguru_logger.remove(sink_id)

    assert any("404" in m for m in messages), (
        "a 404 must be visible at WARNING — an operator cannot diagnose a "
        f"wrong request shape from silence. Captured: {messages}"
    )
