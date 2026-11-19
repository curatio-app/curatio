# ruff: noqa: E402
"""Guards keyed to the MDBList API's *actual* response, not an assumed one.

Every expensive bug in this integration has been the same mistake: a guessed key
name, shipped, silently wrong, discovered hours and dollars later.

* Matching on ``id`` when the TMDB id lives at ``ids.tmdb`` — two full runs
  stored 0.7% of what they fetched while logging steady progress.
* Guessed per-source rating scales — four of nine wrong, three clamping to a
  perfect 10.0.
* Guessed quota field names — the ceiling resolved only because the right key
  happened to sort first in a tuple.

The payloads below are **verbatim** from live probes of api.mdblist.com (trimmed
of fields nothing reads). They are the ground truth these guards exist to pin.
That host is unreachable from CI, which is precisely why the shape is committed
here rather than fetched.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_mdblist_live_shape.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")


#: POST /tmdb/movie/ {"ids":[603]} — The Matrix.
MOVIE_603 = {
    "id": 469990,
    "title": "The Matrix",
    "year": 1999,
    "runtime": 136,
    "score": 82,
    "score_average": 82,
    "ids": {
        "imdb": "tt0133093",
        "trakt": 481,
        "tmdb": 603,
        "tvdb": 169,
        "mal": None,
        "mdblist": "a2na",
    },
    "type": "movie",
    "ratings": [
        {"source": "imdb", "value": 8.7, "score": 87, "votes": 2259494},
        {"source": "metacritic", "value": 73, "score": 73, "votes": 36},
        {"source": "metacriticuser", "value": 8.9, "score": 89.0, "votes": 1838},
        {"source": "trakt", "value": 87, "score": 87, "votes": 57501},
        {"source": "tomatoes", "value": 83, "score": 83, "votes": 209},
        {"source": "popcorn", "value": 85, "score": 85, "votes": 1307885},
        {"source": "tmdb", "value": 82, "score": 82, "votes": 28256},
        {"source": "letterboxd", "value": 8.4, "score": 84, "votes": 2947782},
        {"source": "rogerebert", "value": 3.0, "score": None, "votes": None},
        {"source": "myanimelist", "value": None, "score": None, "votes": None},
    ],
}

#: POST /tmdb/show/ {"ids":[1396]} — Breaking Bad. Note `runtime` is the total
#: across all 62 episodes, not the per-episode figure TMDB reports.
SHOW_1396 = {
    "id": 406423,
    "title": "Breaking Bad",
    "year": 2008,
    "runtime": 2995,
    "score": 92,
    "score_average": 92,
    "ids": {
        "imdb": "tt0903747",
        "trakt": 1388,
        "tmdb": 1396,
        "tvdb": 81189,
        "mal": None,
        "mdblist": "8plj",
    },
    "type": "show",
    "ratings": [
        {"source": "imdb", "value": 9.5, "score": 95, "votes": 2642930},
        {"source": "trakt", "value": 93, "score": 93, "votes": 68767},
        {"source": "letterboxd", "value": None, "score": None, "votes": None},
    ],
}

#: GET /user — the quota and tier block.
USER_INFO = {
    "api_requests": 500000,
    "api_requests_count": 14485,
    "user_id": 210670,
    "plan": "VIP 10",
    "is_supporter": True,
    "rate_limit": 500000,
    "rate_limit_remaining": 485515,
    "auth_method": "apikey",
}


# ---------------------------------------------------------------------------
# The id
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload,expected", [(MOVIE_603, 603), (SHOW_1396, 1396)], ids=["movie", "show"]
)
def test_the_tmdb_id_comes_from_ids_tmdb(payload, expected):
    """``id`` is MDBList's own key and resolves to no local row."""
    from workers.mdb_sync import _payload_tmdb_id

    assert _payload_tmdb_id(payload) == expected
    assert _payload_tmdb_id(payload) != payload["id"], (
        "matched MDBList's internal id instead of the TMDB id — this is the "
        "defect that made two full enrichment runs store almost nothing"
    )


# ---------------------------------------------------------------------------
# The ratings
# ---------------------------------------------------------------------------
def test_no_rating_source_clamps_to_a_perfect_ten():
    from app.mdblist_client import MDBListClient

    ratings = MDBListClient("k").extract_ratings(MOVIE_603)
    by_source = {r["source"]: r["normalized"] for r in ratings}

    assert by_source == pytest.approx(
        {
            "imdb": 8.7,
            "metacritic": 7.3,
            "metacriticuser": 8.9,
            "trakt": 8.7,
            "tomatoes": 8.3,
            "popcorn": 8.5,
            "tmdb": 8.2,
            "letterboxd": 8.4,
            "rogerebert": 7.5,
        },
        abs=0.05,
    )
    # myanimelist is all-null and must be dropped, not coerced to zero.
    assert "myanimelist" not in by_source


def test_consensus_lands_inside_the_range_its_sources_report():
    """The scale bugs moved consensus in *both* directions, not just up.

    Letterboxd, trakt and tmdb clamping to a perfect 10.0 push the
    votes-weighted mean up; Roger Ebert's 3.0 read as 3/10 instead of 3/4 pulls
    it down, and hard. Which one wins depends entirely on the sources a given
    title happens to carry — The Matrix came out at 7.859 on the old code, below
    every one of its nine sources except the misread Ebert score.

    So the invariant worth pinning is not a direction but a bound: a
    votes-weighted mean cannot sit outside the range of the values it averages.
    """
    from app.mdblist_client import MDBListClient, consensus

    ratings = MDBListClient("k").extract_ratings(MOVIE_603)
    values = [r["normalized"] for r in ratings]
    score, count = consensus([(r["normalized"], r["votes"]) for r in ratings])

    assert count == 9
    assert score is not None
    assert min(values) <= score <= max(values), (
        f"consensus {score} sits outside [{min(values)}, {max(values)}] — a "
        "weighted mean cannot do that unless a source was mis-normalised"
    )
    # The old mapping produced 7.859 here; the corrected one lands near 8.5.
    assert 8.0 <= score <= 9.0, f"consensus {score} does not match the sources"


# ---------------------------------------------------------------------------
# The facts
# ---------------------------------------------------------------------------
def test_show_runtime_is_kept_apart_from_movie_runtime():
    """2995 is Breaking Bad's *total* runtime, not an episode length."""
    from app.mdblist_client import MDBListClient

    client = MDBListClient("k")
    movie_facts = client.extract_facts(MOVIE_603)
    show_facts = client.extract_facts(SHOW_1396)

    assert movie_facts["runtime"] == 136
    assert "runtime" not in show_facts, (
        "a show's total runtime stored under 'runtime' would read as a "
        "fifty-hour episode the moment anything merges mdb_facts over tag_facts"
    )
    assert show_facts["total_runtime"] == 2995


# ---------------------------------------------------------------------------
# The quota
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_user_info_separates_the_ceiling_from_the_usage():
    """``api_requests`` is the limit; ``api_requests_count`` is what's spent.

    Both were in one tuple scanned for a max, so the ceiling was correct only
    because the account was nowhere near it.
    """
    from app.mdblist_client import MDBListClient

    client = MDBListClient("k")

    async def _get(path, **params):
        return USER_INFO

    client._get = _get  # type: ignore[method-assign]
    await client.user_info()

    assert client._limit_seen == 500000
    assert client._used_today == 14485
    assert client.is_supporter is True
    # A supporter key takes 100 ids per batch: 3,543 requests for a 354k
    # library instead of 35,428.
    assert client.max_batch_ids == 100


@pytest.mark.asyncio
async def test_a_regular_key_keeps_the_conservative_batch_cap():
    from app.mdblist_client import MDBListClient

    client = MDBListClient("k")

    async def _get(path, **params):
        return {"api_requests": 1000, "api_requests_count": 4, "is_supporter": False}

    client._get = _get  # type: ignore[method-assign]
    await client.user_info()

    assert (
        client.max_batch_ids == 10
    ), "over-batching a regular key is rejected by the API"
    assert client._limit_seen == 1000


@pytest.mark.asyncio
async def test_the_batch_cap_is_enforced_when_splitting():
    """A configured size above the key's cap is split, not sent and refused."""
    from app.mdblist_client import MDBListClient

    client = MDBListClient("k")
    client.max_batch_ids = 10
    sent: list[int] = []

    class _Recorder:
        async def post(self, path, params=None, json=None):
            sent.append(len(json["ids"]))

            class _R:
                status_code = 200
                headers: dict = {}
                text = ""

                @staticmethod
                def json():
                    return []

            return _R()

    async def _http():
        return _Recorder()

    client._http = _http  # type: ignore[method-assign]
    await client.title_info_batch(list(range(100)), "movie")

    assert sent, "no request was made at all"
    assert max(sent) <= 10, f"a batch exceeded the key's cap of 10: {sorted(set(sent))}"


# ---------------------------------------------------------------------------
# The tail: 27,776 titles MDBList has no record of
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_empty_batch_at_or_below_the_regular_cap_does_not_halve():
    """Halving recovers from a rejected batch SIZE, nothing else.

    Against the obscure tail of the library -- 11,650 movies and 16,126 shows
    averaging 0.4 popularity, which MDBList genuinely does not carry -- a batch
    of 10 used to descend 10 -> 5+5 -> 2+3 -> ... -> singles, ~19 requests to
    learn nothing. At mdblist_rps=5 that saturated the limiter: 152 requests per
    30s window, all wasted, ~52,700 to enrich zero rows.
    """
    from app.mdblist_client import REGULAR_KEY_BATCH_IDS, MDBListClient

    client = MDBListClient("k")
    client.max_batch_ids = 100
    sent: list[int] = []

    class _AlwaysEmpty:
        async def post(self, path, params=None, json=None):
            sent.append(len(json["ids"]))

            class _R:
                status_code = 200
                headers: dict = {}
                text = ""

                @staticmethod
                def json():
                    return []

            return _R()

    async def _http():
        return _AlwaysEmpty()

    client._http = _http  # type: ignore[method-assign]

    assert await client.title_info_batch(list(range(10)), "movie") == []
    assert sent == [10], (
        "an empty result at or below the regular-key cap means the titles are "
        f"absent, not that the batch was too big — halved anyway: {sent}"
    )
    assert REGULAR_KEY_BATCH_IDS == 10


@pytest.mark.asyncio
async def test_an_oversized_empty_batch_still_halves():
    """Above the regular-key cap a size rejection is genuinely plausible."""
    from app.mdblist_client import MDBListClient

    client = MDBListClient("k")
    client.max_batch_ids = 100
    sent: list[int] = []

    class _AlwaysEmpty:
        async def post(self, path, params=None, json=None):
            sent.append(len(json["ids"]))

            class _R:
                status_code = 200
                headers: dict = {}
                text = ""

                @staticmethod
                def json():
                    return []

            return _R()

    async def _http():
        return _AlwaysEmpty()

    client._http = _http  # type: ignore[method-assign]
    await client.title_info_batch(list(range(40)), "movie")

    assert sent[0] == 40
    assert 20 in sent, f"a 40-id batch must halve toward the cap: {sent}"
    # ...but it must stop halving once it reaches the cap, not descend to singles.
    assert 1 not in sent, f"halving ran past the cap down to single ids: {sent}"
