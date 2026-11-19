"""
Regressions for the two live production bugs found on 2026-07-31.

Both had been running unnoticed for weeks:

1. **Every tagging call failed and the job reported "completed".**
   Google retired ``gemini-2.0-flash`` on 2026-06-01. Every call began returning
   404, ``tag_items_batch`` caught the exception per batch and incremented a
   counter, and the daily job recorded ``status="completed", items_processed=0,
   items_failed=49`` with no reason attached. The dashboard showed green. The
   outage went unnoticed for two months.

2. **A duplicate TMDB id aborted the whole daily run.**
   ``get_new_releases_this_week`` paged over an unstable sort without
   deduplicating, so one title could appear twice. ``SessionLocal`` uses
   ``autoflush=False``, so the second ``db.merge()`` could not see the first
   one's pending INSERT and both were flushed --
   ``UniqueViolation: duplicate key value violates "media_metadata_pkey"``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_tagging_failures.db")
os.environ.setdefault("TMDB_API_KEY", "test")
os.environ.setdefault("GEMINI_API_KEY", "test")
os.environ.setdefault("MASTER_PASSWORD", "test")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SECRET_KEY", "t" * 40)
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.models import Base, MediaMetadata, Tag  # noqa: E402

TEST_DATABASE_URL = "sqlite:///./test_tagging_failures.db"
engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    session.add(Tag(name="Dark", category="mood"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


# ---------------------------------------------------------------------------
# Bug 1: the provider failure reason must survive without discarding local tags
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_provider_failure_is_reported_without_losing_factual_tags(db):
    """A retired model loses optional nuance, not the whole tagged title."""
    from workers.daily_update import tag_items_batch

    boom = Exception("404 models/gemini-2.0-flash is not found for API version v1beta")
    items = [
        {
            "id": i,
            "title": f"m{i}",
            "overview": "A cerebral family mystery.",
            "genres": ["Drama"],
            "release_date": "2020-01-01",
        }
        for i in range(3)
    ]

    with patch(
        "app.gemini_client.gemini_engine.tag_items_raw", AsyncMock(side_effect=boom)
    ):
        processed, failed, first_error = await tag_items_batch(db, items, "movie")

    assert processed == 3
    assert failed == 0
    assert "404" in first_error
    assert "gemini-2.0-flash" in first_error, "the model id must appear in the reason"


@pytest.mark.asyncio
async def test_first_error_is_empty_on_success(db):
    from workers.daily_update import tag_items_batch

    # Four tags, because validate_item_tags requeues an item that resolves
    # fewer than four rather than storing a near-empty result.
    tagged = (
        [
            {
                "tmdb_id": 1,
                "tags": [
                    ["Dark", 0.9],
                    ["Heist", 0.8],
                    ["Sci-Fi", 0.8],
                    ["Cyberpunk", 0.8],
                ],
            }
        ],
        {"prompt_token_count": 10, "candidates_token_count": 10},
    )
    items = [{"id": 1, "title": "m", "overview": "x"}]

    with patch(
        "app.gemini_client.gemini_engine.tag_items_raw", AsyncMock(return_value=tagged)
    ):
        processed, failed, first_error = await tag_items_batch(db, items, "movie")

    assert failed == 0
    assert first_error == ""


@pytest.mark.asyncio
async def test_only_the_first_error_is_kept(db):
    """Later, different errors must not overwrite the original cause."""
    from workers.daily_update import tag_items_batch

    errors = [Exception("FIRST cause"), Exception("later noise")]
    items = [
        {
            "id": i,
            "title": f"m{i}",
            "overview": "A cerebral family mystery.",
            "genres": ["Drama"],
            "release_date": "2020-01-01",
        }
        for i in range(100)
    ]

    with patch(
        "app.gemini_client.gemini_engine.tag_items_raw", AsyncMock(side_effect=errors)
    ):
        _processed, _failed, first_error = await tag_items_batch(
            db, items, "movie", batch_size=50
        )

    assert "FIRST cause" in first_error
    assert "later noise" not in first_error


@pytest.mark.asyncio
async def test_initial_build_also_returns_the_reason(db):
    """The build worker swallowed errors the same way."""
    from workers.initial_build import tag_items_batch

    items = [
        {
            "id": 7,
            "title": "m",
            "overview": "A cerebral family mystery.",
            "genres": ["Drama"],
            "release_date": "2020-01-01",
        }
    ]
    with patch(
        "app.gemini_client.gemini_engine.tag_items_raw",
        AsyncMock(side_effect=Exception("PermissionDenied: 403")),
    ):
        processed, failed, first_error = await tag_items_batch(db, items, "movie")

    assert processed == 1 and failed == 0
    assert "403" in first_error


@pytest.mark.asyncio
async def test_initial_build_skips_a_current_terminal_attempt(db):
    """Restarting an initial build must not repay for deterministic rejects."""
    from workers.initial_build import tag_items_batch
    from workers.tagging_common import tagger_version

    db.add(
        MediaMetadata(
            tmdb_id=8,
            media_type="movie",
            title="Terminally Sparse",
            tag_attempt_version=tagger_version(),
            tag_attempt_status="too_few_valid_tags",
        )
    )
    db.commit()
    model_call = AsyncMock()

    with patch("app.gemini_client.gemini_engine.tag_items_raw", model_call):
        processed, failed, first_error = await tag_items_batch(
            db, [{"id": 8, "title": "Terminally Sparse"}], "movie"
        )

    assert (processed, failed, first_error) == (0, 0, "")
    model_call.assert_not_awaited()


# ---------------------------------------------------------------------------
# Bug 2: duplicate ids must not abort the run
# ---------------------------------------------------------------------------
def test_new_releases_are_deduplicated():
    """TMDB pages over an unstable sort and can repeat a title across pages."""
    import asyncio

    from app.tmdb_client import tmdb_client

    page = {"results": [{"id": 1}, {"id": 2}], "total_pages": 2}
    dupe = {"results": [{"id": 2}, {"id": 3}], "total_pages": 2}

    with patch.object(
        tmdb_client,
        "get_movies_released_in_date_range",
        AsyncMock(side_effect=[page, dupe]),
    ):
        result = asyncio.run(tmdb_client.get_new_releases_this_week("movie"))

    ids = [item["id"] for item in result]
    assert ids == [1, 2, 3], "id 2 appeared on both pages and must be returned once"
    assert len(ids) == len(set(ids))


def test_new_releases_drops_items_without_an_id():
    import asyncio

    from app.tmdb_client import tmdb_client

    page = {"results": [{"id": 1}, {"title": "no id"}], "total_pages": 1}
    with patch.object(
        tmdb_client, "get_movies_released_in_date_range", AsyncMock(return_value=page)
    ):
        result = asyncio.run(tmdb_client.get_new_releases_this_week("movie"))

    assert [i["id"] for i in result] == [1]


def test_store_metadata_survives_duplicate_ids(monkeypatch):
    """The belt to get_new_releases_this_week's braces.

    With autoflush=False a second merge() for a pending primary key cannot see
    the first, so both flush as INSERTs and the commit raises UniqueViolation.
    SQLite raises IntegrityError for the same reason, so this reproduces.
    """
    import workers.daily_update as daily

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()

    class _Ctx:
        def __enter__(self):
            return session

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(daily, "get_db", lambda: _Ctx())
    monkeypatch.setattr(daily, "_apply_statement_timeout", lambda _db: None)

    items = [
        {"id": 42, "title": "Dup", "overview": "a", "release_date": "2026-01-01"},
        {"id": 42, "title": "Dup", "overview": "a", "release_date": "2026-01-01"},
        {"id": 43, "title": "Other", "overview": "b", "release_date": "2026-01-01"},
    ]
    try:
        daily._store_metadata_threaded(items, "movie")
        stored = session.query(MediaMetadata).count()
        assert stored == 2, "duplicate must be collapsed, not inserted twice"
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


# ---------------------------------------------------------------------------
# Bug 1b: the job status must not claim success
# ---------------------------------------------------------------------------
def test_config_default_model_is_not_a_retired_id():
    """gemini-2.0-flash shut down 2026-06-01; 2.5-flash follows 2026-10-16."""
    from app.config import settings

    retired = {
        "gemini-2.0-flash",
        "gemini-2.0-flash-001",
        "gemini-2.0-flash-lite",
        "gemini-2.0-flash-lite-001",
    }
    assert (
        settings.gemini_model not in retired
    ), f"{settings.gemini_model} is past its shutdown date and 404s on every call"
