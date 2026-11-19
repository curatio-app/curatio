"""Regression tests for daily update cancellation behavior."""

import asyncio
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Ensure app settings can be loaded in isolation.
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_daily_update.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.models import Base, TaggingJob  # noqa: E402
from workers import daily_update  # noqa: E402


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    """Use an isolated SQLite database for daily update worker tests."""
    engine = create_engine(f"sqlite:///{tmp_path / 'daily_update.db'}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    @contextmanager
    def override_get_db():
        session = SessionLocal()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(daily_update, "get_db", override_get_db)

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.mark.asyncio
async def test_run_daily_update_marks_job_cancelled(db_session, monkeypatch):
    """Cancelling the worker should persist a cancelled job instead of leaking running state."""
    started_fetch = asyncio.Event()

    async def never_return_new_releases(media_type):
        started_fetch.set()
        await asyncio.sleep(60)
        return []

    monkeypatch.setattr(
        daily_update.tmdb_client,
        "get_new_releases_this_week",
        never_return_new_releases,
    )

    task = asyncio.create_task(daily_update.run_daily_update())
    await asyncio.wait_for(started_fetch.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    job = db_session.query(TaggingJob).one()
    assert job.status == "cancelled"
    assert job.completed_at is not None
    assert job.error_message == "Cancelled by user"


def test_gemini_timeout_errors_are_retryable():
    """A stalled Gemini call (bounded by wait_for) must be retried, not fatal.

    The daily update relies on this: a hung generate_content surfaces as a
    TimeoutError, which the tagging batch retries instead of aborting the run.
    """
    from app.gemini_client import _is_retryable_error

    assert _is_retryable_error(asyncio.TimeoutError()) is True
    assert _is_retryable_error(TimeoutError()) is True
    assert _is_retryable_error(RuntimeError("504 Deadline")) is True
    assert _is_retryable_error(ConnectionError()) is True
    # Non-transient errors must stay fatal so we don't retry-loop forever.
    assert _is_retryable_error(ValueError("bad json")) is False


@pytest.mark.asyncio
async def test_run_daily_update_times_out_and_marks_job(db_session, monkeypatch):
    """A run that exceeds its deadline should be killed and marked 'timed_out'."""

    async def hang_forever(media_type):
        await asyncio.sleep(60)
        return []

    monkeypatch.setattr(
        daily_update.tmdb_client,
        "get_new_releases_this_week",
        hang_forever,
    )

    with pytest.raises(asyncio.TimeoutError):
        await daily_update.run_daily_update(timeout_seconds=0.1)

    job = db_session.query(TaggingJob).one()
    assert job.status == "timed_out"
    assert job.completed_at is not None
    assert "deadline" in (job.error_message or "")


@pytest.mark.asyncio
async def test_run_daily_update_skips_catalog_regeneration_when_no_items(
    db_session, monkeypatch
):
    """No-op daily updates should not rescan and rebuild every universal catalog."""

    async def no_new_releases(media_type):
        return []

    class ExplodingCatalogGenerator:
        def __init__(self, db):
            pass

        def regenerate_all_universal_catalogs(self):
            raise AssertionError("catalog regeneration should be skipped")

    monkeypatch.setattr(
        daily_update.tmdb_client,
        "get_new_releases_this_week",
        no_new_releases,
    )
    monkeypatch.setattr(daily_update, "CatalogGenerator", ExplodingCatalogGenerator)

    await daily_update.run_daily_update()

    job = db_session.query(TaggingJob).one()
    assert job.status == "completed"
    assert job.items_processed == 0
    assert job.job_metadata["catalogs_regenerated"] is False


@pytest.mark.asyncio
async def test_daily_update_tags_both_media_before_one_full_relevance_refresh(
    db_session, monkeypatch
):
    """Movie and TV additions publish through one corpus-wide weighting pass."""
    events = []

    async def releases(media_type):
        return [{"id": 1 if media_type == "movie" else 2}]

    async def tag(_db, items, media_type):
        events.append(f"tag-{media_type}")
        return len(items), 0, ""

    def relevance(_job_name="daily_update"):
        events.append("relevance")
        return {"tags": 2, "rows": 2, "vectors": 2}

    async def next_air(_db, _limit):
        events.append("next-air")
        return 0

    def regenerate():
        events.append("catalogs")

    monkeypatch.setattr(
        daily_update.tmdb_client, "get_new_releases_this_week", releases
    )
    monkeypatch.setattr(
        daily_update, "_filter_new_items_threaded", lambda items, _media: items
    )
    monkeypatch.setattr(
        daily_update, "_store_metadata_threaded", lambda _items, _media: None
    )
    monkeypatch.setattr(daily_update, "tag_items_batch", tag)
    monkeypatch.setattr(daily_update, "refresh_relevance_threaded", relevance)
    monkeypatch.setattr(daily_update, "_refresh_tv_next_air_dates", next_air)
    monkeypatch.setattr(daily_update, "_regen_universal_catalogs_threaded", regenerate)

    async def owned_tagging():
        events.append("owned")
        return {"processed": 0}

    monkeypatch.setattr("workers.library_tagging.tag_owned_titles", owned_tagging)
    # Catalog titles nobody owns are only tagged when the owner opts in.
    monkeypatch.setattr(daily_update.settings, "tag_catalog_beyond_library", True)

    await daily_update.run_daily_update()

    assert events == [
        "tag-movie",
        "tag-tv",
        "owned",
        "relevance",
        "next-air",
        "catalogs",
    ]

    job = db_session.query(TaggingJob).one()
    assert job.job_metadata["relevance_refreshed"] is True


@pytest.mark.asyncio
async def test_daily_update_tags_only_owned_titles_by_default(db_session, monkeypatch):
    """Library-first: new releases are stored but not AI-tagged by default;
    owned and Watchlist titles still are."""
    events = []

    async def releases(media_type):
        return [{"id": 1 if media_type == "movie" else 2}]

    async def tag(_db, items, media_type):
        events.append(f"tag-{media_type}")
        return len(items), 0, ""

    def relevance(_job_name="daily_update"):
        events.append("relevance")
        return {"tags": 2, "rows": 2, "vectors": 2}

    async def next_air(_db, _limit):
        events.append("next-air")
        return 0

    def regenerate():
        events.append("catalogs")

    monkeypatch.setattr(
        daily_update.tmdb_client, "get_new_releases_this_week", releases
    )
    monkeypatch.setattr(
        daily_update, "_filter_new_items_threaded", lambda items, _media: items
    )
    monkeypatch.setattr(
        daily_update, "_store_metadata_threaded", lambda _items, _media: None
    )
    monkeypatch.setattr(daily_update, "tag_items_batch", tag)
    monkeypatch.setattr(daily_update, "refresh_relevance_threaded", relevance)
    monkeypatch.setattr(daily_update, "_refresh_tv_next_air_dates", next_air)
    monkeypatch.setattr(daily_update, "_regen_universal_catalogs_threaded", regenerate)

    async def owned_tagging():
        events.append("owned")
        return {"processed": 0}

    monkeypatch.setattr("workers.library_tagging.tag_owned_titles", owned_tagging)
    monkeypatch.setattr(daily_update.settings, "tag_catalog_beyond_library", False)

    await daily_update.run_daily_update()

    assert events == ["owned", "next-air"]

    job = db_session.query(TaggingJob).one()
    # Nothing new was tagged, so no corpus-wide refresh was needed.
    assert job.job_metadata["relevance_refreshed"] is False
