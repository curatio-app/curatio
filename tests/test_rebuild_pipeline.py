"""
End-to-end tests for the rebuild pipeline: enrichment, prompt, tagging, MDBList.

These exercise the whole path on SQLite with a mocked model, which is only
possible because ``tagging_common.upsert_movie_tags`` branches on dialect. Both
old copies of the tagger imported the PostgreSQL insert unconditionally, so the
tagging path had never been covered by a test at all.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_rebuild.db")
os.environ.setdefault("TMDB_API_KEY", "test")
os.environ.setdefault("GEMINI_API_KEY", "test")
os.environ.setdefault("MASTER_PASSWORD", "test")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SECRET_KEY", "t" * 40)
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.models import Base, MediaMetadata, MovieTag, TagObservation  # noqa: E402
from app.taxonomy_sync import sync_taxonomy  # noqa: E402

TEST_DATABASE_URL = "sqlite:///./test_rebuild.db"
engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    sync_taxonomy(session)
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def _title(db, tmdb_id=603, media_type="movie", **kw):
    row = MediaMetadata(
        tmdb_id=tmdb_id,
        media_type=media_type,
        title=kw.get("title", "The Matrix"),
        overview=kw.get(
            "overview",
            "A cerebral hacker uncovers a suspenseful conspiracy about reality.",
        ),
        genres=kw.get("genres", ["Action", "Science Fiction"]),
        release_date=kw.get("release_date", "1999-03-30"),
        original_language="en",
        popularity=kw.get("popularity", 100.0),
        vote_count=kw.get("vote_count", 20000),
        tag_facts=kw.get("tag_facts"),
        mdb_facts=kw.get("mdb_facts"),
    )
    db.add(row)
    db.commit()
    return row


class _FakeEngine:
    """Stands in for Gemini: returns canned tags and records the prompt."""

    model_name = "gemini-2.5-flash-lite"

    def __init__(self, tags_by_id, usage=None):
        self._tags = tags_by_id
        self.last_usage = usage or {
            "prompt_token_count": 6000,
            "candidates_token_count": 5600,
        }
        self.prompts = []

    async def tag_items_raw(self, prompt, **_constraints):
        # Usage is returned WITH the items, not read off the engine afterwards:
        # the real engine is a singleton and gemini_concurrency batches call it
        # at once, so an attribute read after the await belongs to whichever
        # batch finished last.
        self.prompts.append(prompt)
        return [
            {"tmdb_id": tmdb_id, "tags": tags, "extra": ["Dream Logic"]}
            for tmdb_id, tags in self._tags.items()
        ], self.last_usage


# ---------------------------------------------------------------------------
# raw_data pruning — the storage guard
# ---------------------------------------------------------------------------
def test_prune_strips_the_bulk_but_keeps_episode_counts():
    """workers/fingerprint_worker.py reads seasons[].episode_count and nothing
    else reads raw_data at all."""
    from workers.enrich import prune_raw_data

    payload = {
        "id": 1,
        "title": "X",
        "images": {"posters": [{"file_path": f"/p{i}.jpg"} for i in range(400)]},
        "videos": {"results": [{"key": f"v{i}"} for i in range(50)]},
        "credits": {
            "cast": [{"name": f"Actor {i}", "order": i} for i in range(120)],
            "crew": [
                {"name": "Dir", "job": "Director"},
                {"name": "Grip", "job": "Key Grip"},
            ],
        },
        "seasons": [
            {"season_number": 1, "episode_count": 8, "air_date": "2020-01-01", "x": "y"}
        ],
        "release_dates": {
            "results": [
                {"iso_3166_1": "US", "release_dates": [{"certification": "R"}]},
                {"iso_3166_1": "FR", "release_dates": [{"certification": "16"}]},
            ]
        },
    }
    pruned = prune_raw_data(payload)

    assert "images" not in pruned and "videos" not in pruned
    assert len(pruned["credits"]["cast"]) == 20
    assert [c["job"] for c in pruned["credits"]["crew"]] == ["Director"]
    assert pruned["seasons"][0]["episode_count"] == 8, "the one field anything reads"
    assert [r["iso_3166_1"] for r in pruned["release_dates"]["results"]] == ["US"]
    assert len(json.dumps(pruned)) < len(json.dumps(payload)) / 5


def test_prune_is_idempotent():
    from workers.enrich import prune_raw_data

    payload = {"id": 1, "seasons": [{"season_number": 1, "episode_count": 6}]}
    assert prune_raw_data(prune_raw_data(payload)) == prune_raw_data(payload)


def test_prune_tolerates_junk():
    from workers.enrich import prune_raw_data

    assert prune_raw_data({}) == {}
    assert prune_raw_data(None) == {}
    assert "seasons" not in prune_raw_data({"id": 1})


def test_build_tag_facts_extracts_the_signal():
    from workers.enrich import build_tag_facts

    facts = build_tag_facts(
        {
            "runtime": 136,
            "credits": {
                "cast": [{"name": "Keanu Reeves"}],
                "crew": [{"name": "Lana Wachowski", "job": "Director"}],
            },
            "keywords": {
                "keywords": [{"name": "dystopia"}, {"name": "virtual reality"}]
            },
            "production_countries": [{"iso_3166_1": "US"}],
            "release_dates": {
                "results": [
                    {"iso_3166_1": "US", "release_dates": [{"certification": "R"}]}
                ]
            },
        },
        "movie",
    )
    assert facts["runtime"] == 136
    assert facts["directors"] == ["Lana Wachowski"]
    assert facts["cast"] == ["Keanu Reeves"]
    assert "dystopia" in facts["keywords"]
    assert facts["certification"] == "R"
    assert facts["countries"] == ["US"]


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
def test_prompt_allows_the_model_to_abstain() -> None:
    from app.tagging_prompt import SYSTEM_BLOCK

    assert "Choose 0-3 precise interpretive tags" in SYSTEM_BLOCK
    assert "Returning zero is valid" in SYSTEM_BLOCK
    assert "Never use 0.60 as a default or filler" in SYSTEM_BLOCK


def test_prompt_carries_enrichment(db):
    from app.tagging_prompt import build_prompt
    from workers.tagging_common import build_features

    row = _title(
        db,
        tag_facts={
            "keywords": ["dystopia", "virtual reality", "thought provoking"],
            "directors": ["Lana Wachowski"],
            "cast": ["Keanu Reeves"],
            "certification": "R",
            "runtime": 136,
        },
        mdb_facts={"reviews": ["A blistering, paranoid action picture."]},
    )
    prompt, vocab = build_prompt([build_features(row)])

    assert "dystopia" in prompt, "TMDB keywords are the highest-signal field"
    assert "Lana Wachowski" in prompt and "Keanu Reeves" in prompt
    assert "blistering, paranoid" not in prompt
    assert "Review:" not in prompt, "user-review prose must not influence tags"
    assert "ALLOWED TAGS" in prompt and vocab
    assert '"tag_index"' in prompt
    assert any(
        line.startswith("0: [") for line in prompt.splitlines()
    ), "the schema's local integer must map to the displayed vocabulary"

    # The header carries id | type | year | runtime | certification.
    header = next(ln for ln in prompt.splitlines() if ln.startswith("### "))
    assert header == "### 603 | movie | 1999 | 136min | R", header


def test_prompt_omits_absent_blocks(db):
    """An un-enriched title must still produce a usable prompt."""
    from app.tagging_prompt import build_prompt
    from workers.tagging_common import build_features

    row = _title(db, tmdb_id=999)
    prompt, _vocab = build_prompt([build_features(row)])
    assert "Keywords:" not in prompt and "Review:" not in prompt
    assert "The Matrix" in prompt


def test_prompt_vocabulary_stays_within_budget(db):
    from app.tagging_prompt import build_prompt
    from app.taxonomy import estimate_tokens
    from workers.tagging_common import build_features

    rows = [
        _title(db, tmdb_id=1000 + i, genres=["Horror"], release_date="1985-01-01")
        for i in range(40)
    ]
    prompt, vocab = build_prompt([build_features(r) for r in rows])
    assert len(vocab) < 250
    assert estimate_tokens(prompt) < 20000


def test_each_title_gets_its_own_allowed_indexes(db):
    """A batch union must not become permission to borrow a neighbour's tags."""
    from app.tagging_prompt import _allowed_indexes_by_facet, build_prompt
    from app.taxonomy import model_shortlist
    from workers.tagging_common import build_features

    rows = [
        _title(
            db,
            tmdb_id=2001,
            title="A Christmas Romance",
            genres=["Romance", "Comedy"],
            overview="A heartwarming romance brings a family together.",
            tag_facts={"keywords": ["christmas", "wedding", "family"]},
        ),
        _title(
            db,
            tmdb_id=2002,
            title="Possessed in the Woods",
            genres=["Horror"],
            overview="A chilling possession terrorizes friends in the woods.",
            tag_facts={"keywords": ["demon", "possession", "slasher"]},
        ),
    ]
    features = [build_features(row) for row in rows]
    prompt, vocab = build_prompt(features)
    index_by_name = {tag.name: index for index, tag in enumerate(vocab)}

    expected = [
        _allowed_indexes_by_facet(
            vocab,
            [
                index_by_name[tag.name]
                for tag in model_shortlist(item)
                if tag.name in index_by_name
            ],
        )
        for item in features
    ]
    allowed_lines = [
        line.removeprefix("Allowed tag_index values by facet: ")
        for line in prompt.splitlines()
        if line.startswith("Allowed tag_index values by facet: ")
    ]

    assert allowed_lines == expected
    assert allowed_lines[0] != allowed_lines[1]


# ---------------------------------------------------------------------------
# Tagging — the path that had never been testable
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tag_and_store_writes_versioned_rows(db):
    from workers.tagging_common import build_features, tag_and_store, tagger_version

    row = _title(db)
    fake = _FakeEngine(
        {
            603: [
                ["Cerebral", 0.85],
                ["Suspenseful", 0.9],
                ["Friendship", 0.95],
                ["Anti-Hero", 1.0],
            ]
        }
    )

    stats = await tag_and_store(db, [build_features(row)], engine=fake)

    assert stats["processed"] == 1
    written = db.query(MovieTag).filter(MovieTag.tmdb_id == 603).all()
    assert written
    assert all(w.tagger_version == tagger_version() for w in written)
    assert all(w.source in ("gemini", "derived") for w in written)
    assert all(
        (w.source == "derived" and w.model is None)
        or (w.source == "gemini" and w.model == "gemini-2.5-flash-lite")
        for w in written
    )


@pytest.mark.asyncio
async def test_one_model_tag_is_not_retried_to_fill_a_quota(db):
    from workers.tagging_common import build_features, tag_and_store

    row = _title(db)

    class RecoveringEngine:
        provider_name = "gemini"
        model_name = "test"

        def __init__(self):
            self.calls = 0

        async def tag_items_raw(self, _prompt, **_constraints):
            self.calls += 1
            tags = [["Cerebral", 0.9]]
            return [{"tmdb_id": 603, "tags": tags, "extra": []}], {
                "prompt_token_count": 10,
                "candidates_token_count": 5,
            }

    engine = RecoveringEngine()
    stats = await tag_and_store(db, [build_features(row)], engine=engine)

    assert engine.calls == 1
    assert stats["processed"] == 1
    assert stats["low_tag_retry_calls"] == 0
    assert stats["recovered_low_tag_results"] == 0


@pytest.mark.asyncio
async def test_model_abstention_keeps_deterministic_tags(db):
    from workers.tagging_common import build_features, tag_and_store

    row = _title(db)
    fake = _FakeEngine({603: []})
    stats = await tag_and_store(db, [build_features(row)], engine=fake)

    assert stats["processed"] == 1
    assert stats["low_tag_results"] == 0
    assert stats["low_tag_retry_calls"] == 0
    assert stats["low_tag_diagnostics"] == []


@pytest.mark.asyncio
async def test_derived_tags_are_merged(db):
    """Era and origin are computed, never asked of the model."""
    from workers.tagging_common import build_features, tag_and_store

    row = _title(db, tag_facts={"countries": ["US"], "runtime": 136})
    fake = _FakeEngine(
        {
            603: [
                ["Cerebral", 0.9],
                ["Suspenseful", 0.9],
                ["Friendship", 0.9],
                ["Anti-Hero", 0.9],
            ]
        }
    )

    await tag_and_store(db, [build_features(row)], engine=fake)

    from app.models import Tag

    names = {
        db.query(Tag).filter(Tag.id == w.tag_id).one().name
        for w in db.query(MovieTag).filter(MovieTag.tmdb_id == 603)
    }
    assert "1990s" in names, "release_date -> decade, computed not guessed"
    assert "American" in names


@pytest.mark.asyncio
async def test_off_vocabulary_is_recorded(db):
    """The whole reason the validation layer exists."""
    from workers.tagging_common import build_features, tag_and_store

    row = _title(db)
    fake = _FakeEngine(
        {
            603: [
                ["Cerebral", 0.9],
                ["Suspenseful", 0.9],
                ["Friendship", 0.9],
                ["Anti-Hero", 0.9],
                ["Vibes Based Cinema", 0.8],
            ]
        }
    )

    stats = await tag_and_store(db, [build_features(row)], engine=fake)

    assert stats["off_vocabulary"] >= 1
    observed = {o.raw_name for o in db.query(TagObservation).all()}
    assert "Vibes Based Cinema" in observed
    assert (
        "Dream Logic" in observed
    ), "the prompt's 'extra' escape hatch is recorded too"


def test_observation_queries_scale_with_batches_not_tag_count(db):
    from workers.tagging_common import record_observations

    statements = 0

    def count_statement(*_args):
        nonlocal statements
        statements += 1

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        record_observations(db, [("One novel tag", "movie", 1)])
        db.flush()
        single_count = statements
        db.rollback()

        statements = 0
        record_observations(
            db,
            [(f"Novel tag {index}", "movie", index) for index in range(500)],
        )
        db.flush()
        assert statements <= single_count + 2
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)


@pytest.mark.asyncio
async def test_retag_swaps_stale_rows_atomically(db):
    """Old-version rows for a title go; new ones land; nothing else is touched."""
    from app.models import Tag
    from workers.tagging_common import build_features, tag_and_store, tagger_version

    row = _title(db)
    stale_tag = db.query(Tag).filter(Tag.name == "Drama").one()
    db.add(
        MovieTag(
            tmdb_id=603,
            media_type="movie",
            tag_id=stale_tag.id,
            confidence=0.5,
            tagger_version="1.0.0",
        )
    )
    other = db.query(Tag).filter(Tag.name == "Comedy").one()
    db.add(
        MovieTag(
            tmdb_id=999,
            media_type="movie",
            tag_id=other.id,
            confidence=0.5,
            tagger_version="1.0.0",
        )
    )
    db.commit()

    fake = _FakeEngine(
        {
            603: [
                ["Cerebral", 0.9],
                ["Suspenseful", 0.9],
                ["Friendship", 0.9],
                ["Anti-Hero", 0.9],
            ]
        }
    )
    await tag_and_store(db, [build_features(row)], engine=fake)

    versions = {
        w.tagger_version for w in db.query(MovieTag).filter(MovieTag.tmdb_id == 603)
    }
    assert versions == {tagger_version()}, "stale rows for this title must be gone"
    untouched = db.query(MovieTag).filter(MovieTag.tmdb_id == 999).one()
    assert untouched.tagger_version == "1.0.0", "other titles must not be disturbed"


@pytest.mark.asyncio
async def test_cost_and_tokens_are_recorded(db):
    from workers.tagging_common import build_features, tag_and_store

    row = _title(db)
    fake = _FakeEngine(
        {
            603: [
                ["Cerebral", 0.9],
                ["Suspenseful", 0.9],
                ["Friendship", 0.9],
                ["Anti-Hero", 0.9],
            ]
        },
        usage={"prompt_token_count": 6000, "candidates_token_count": 5600},
    )
    stats = await tag_and_store(db, [build_features(row)], engine=fake)

    assert stats["input_tokens"] == 6000
    assert stats["output_tokens"] == 5600
    assert stats["cost_usd"] > 0
    assert stats["api_calls"] == 1


@pytest.mark.asyncio
async def test_thinking_tokens_count_as_output(db):
    """They bill as output and are easy to leave out of a budget."""
    from workers.tagging_common import build_features, tag_and_store

    row = _title(db)
    fake = _FakeEngine(
        {
            603: [
                ["Cerebral", 0.9],
                ["Suspenseful", 0.9],
                ["Friendship", 0.9],
                ["Anti-Hero", 0.9],
            ]
        },
        usage={
            "prompt_token_count": 1000,
            "candidates_token_count": 500,
            "thoughts_token_count": 2000,
        },
    )
    stats = await tag_and_store(db, [build_features(row)], engine=fake)
    assert stats["output_tokens"] == 2500


@pytest.mark.asyncio
async def test_a_provider_failure_keeps_deterministic_tags_and_records_reason(db):
    from workers.tagging_common import build_features, tag_and_store

    row = _title(db)

    class _Boom:
        model_name = "m"
        last_usage = None

        async def tag_items_raw(self, prompt, **_constraints):
            raise RuntimeError("404 model not found")

    stats = await tag_and_store(db, [build_features(row)], engine=_Boom())
    assert stats["processed"] == 1
    assert stats["failed"] == 0
    assert stats["provider_failures"] == 1
    assert "404" in stats["first_error"]


@pytest.mark.asyncio
async def test_stop_request_unwinds_cleanly(db):
    from workers.tagging_common import build_features, tag_and_store

    rows = [_title(db, tmdb_id=2000 + i) for i in range(5)]
    fake = _FakeEngine({})
    stats = await tag_and_store(
        db,
        [build_features(r) for r in rows],
        engine=fake,
        batch_size=1,
        should_stop=lambda: True,
    )
    assert stats["processed"] == 0
    assert fake.prompts == [], "no API call should be made after a stop"


# ---------------------------------------------------------------------------
# MDBList
# ---------------------------------------------------------------------------
def test_ratings_normalize_onto_one_scale():
    """The per-source ``value`` fallback, used only when ``score`` is null.

    The letterboxd assertion here used to be ``normalize_rating("letterboxd",
    4.2) == 8.4`` — written from Letterboxd's own 0-5 scale. A live probe shows
    MDBList already reports it doubled onto 0-10 (``value: 8.4`` for a 4.2/5
    film), so the doubling produced 16.8 and clamped to a perfect 10.0. Same
    class of error for trakt and tmdb, which arrive as percentages.
    """
    from app.mdblist_client import normalize_rating

    assert normalize_rating("tomatoes", 87) == 8.7
    assert normalize_rating("metacritic", 74) == 7.4
    assert normalize_rating("imdb", 8.7) == 8.7
    # Percentages, not 0-10 — these clamped to 10.0 before.
    assert normalize_rating("trakt", 87) == pytest.approx(8.7)
    assert normalize_rating("tmdb", 82) == pytest.approx(8.2)
    # Already on 0-10 as MDBList reports it, not 0-5.
    assert normalize_rating("letterboxd", 8.4) == pytest.approx(8.4)
    # Four stars, not ten.
    assert normalize_rating("rogerebert", 3.0) == pytest.approx(7.5)
    assert normalize_rating("imdb", "nonsense") is None
    assert normalize_rating("imdb", None) is None


def test_score_is_preferred_over_per_source_value():
    """``score`` is MDBList-normalised to 0-100 for every source.

    Deriving from it deletes the per-source scale table's ability to be wrong,
    which it was for four of nine sources.
    """
    from app.mdblist_client import MDBListClient, normalize_score

    assert normalize_score(87) == pytest.approx(8.7)
    assert normalize_score(94.0) == pytest.approx(9.4)
    assert normalize_score(None) is None
    assert normalize_score("nonsense") is None

    # The real payload: every value on a different scale, every score on one.
    ratings = MDBListClient("k").extract_ratings(
        {
            "ratings": [
                {"source": "imdb", "value": 8.7, "score": 87, "votes": 2259494},
                {"source": "trakt", "value": 87, "score": 87, "votes": 57501},
                {"source": "tmdb", "value": 82, "score": 82, "votes": 28256},
                {"source": "letterboxd", "value": 8.4, "score": 84, "votes": 2947782},
                {"source": "metacritic", "value": 73, "score": 73, "votes": 36},
                # score null -> falls back to the per-source value map (0-4).
                {"source": "rogerebert", "value": 3.0, "score": None, "votes": None},
            ]
        }
    )
    by_source = {r["source"]: r["normalized"] for r in ratings}
    assert by_source["imdb"] == pytest.approx(8.7)
    assert by_source["trakt"] == pytest.approx(8.7)
    assert by_source["tmdb"] == pytest.approx(8.2)
    assert by_source["letterboxd"] == pytest.approx(8.4)
    assert by_source["metacritic"] == pytest.approx(7.3)
    assert by_source["rogerebert"] == pytest.approx(7.5)
    assert not [s for s, v in by_source.items() if v >= 10.0], (
        "no source may clamp to a perfect 10 — three of them did, which skewed "
        f"rating_consensus high across the whole library: {by_source}"
    )


def test_consensus_needs_a_source_floor():
    from app.mdblist_client import consensus

    assert consensus([(8.7, 1000)], min_sources=2)[0] is None
    score, count = consensus([(8.7, 900000), (7.4, 40)], min_sources=2)
    assert score is not None and count == 2
    assert 7.4 <= score <= 8.7


def test_consensus_does_not_let_one_huge_source_dominate():
    from app.mdblist_client import consensus

    score, _ = consensus([(10.0, 5_000_000), (2.0, 10)], min_sources=2)
    assert score < 9.0, "vote weighting must be dampened, not linear"


def test_client_is_disabled_without_a_key():
    from app.mdblist_client import MDBListClient

    assert MDBListClient(None).enabled is False
    assert MDBListClient("").enabled is False
    assert MDBListClient("k").enabled is True


def test_api_key_round_trips_through_admin_settings(db):
    from app.mdblist_client import get_api_key, set_api_key

    assert get_api_key(db) is None
    set_api_key(db, "abc123")
    assert get_api_key(db) == "abc123"
    set_api_key(db, None)
    assert get_api_key(db) is None


def test_extract_facts_caps_review_text():
    """Uncapped, reviews alone would be gigabytes across the library."""
    from app.mdblist_client import MDBListClient

    client = MDBListClient("k")
    facts = client.extract_facts(
        {
            "reviews": [
                {"content": "x" * 5000},
                {"content": "y" * 5000},
                {"content": "z"},
            ],
            "keywords": [{"name": f"kw{i}"} for i in range(40)],
            "certification": "R",
        }
    )
    assert len(facts["reviews"]) <= 2
    assert all(len(r) <= 400 for r in facts["reviews"])
    assert len(facts["keywords"]) <= 15
    assert facts["certification"] == "R"


@pytest.mark.asyncio
async def test_client_never_raises_on_transport_failure():
    """An MDBList outage must degrade the library, not fail a build."""
    from app.mdblist_client import MDBListClient

    client = MDBListClient("k")
    client._http = AsyncMock(side_effect=RuntimeError("network gone"))
    assert await client.title_info(1, "movie") is None
    assert await client.list_items("slug") is None
