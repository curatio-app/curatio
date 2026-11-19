# ruff: noqa: E402
"""Tests for ``comingSoonDate`` on catalog previews.

Covers the pure TMDB extractors (``TMDBClient.next_episode_air_date`` and the two
derived fields ``extract_metadata`` records), the date-selection helper
(``_coming_soon_date``). All pure functions — no network or DB — but importing
the app modules needs the standard test env.
"""

import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_coming_soon.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.main import _coming_soon_date  # noqa: E402
from app.models import (  # noqa: E402
    Base,
    MediaMetadata,
    TaggingJob,
    UniversalCatalogContent,
    UniversalCategory,
)
from app.tmdb_client import TMDBClient  # noqa: E402
from workers import daily_update  # noqa: E402

TODAY = datetime.utcnow().strftime("%Y-%m-%d")
FUTURE = (datetime.utcnow() + timedelta(days=4)).strftime("%Y-%m-%d")
PAST = (datetime.utcnow() - timedelta(days=4)).strftime("%Y-%m-%d")


def _movie_item(**overrides) -> dict:
    item = {
        "tmdb_id": 1234567,
        "media_type": "movie",
        "title": "Some Film",
        "poster": "/poster.jpg",
    }
    item.update(overrides)
    return item


def _series_item(**overrides) -> dict:
    item = {
        "tmdb_id": 7654321,
        "media_type": "tv",
        "title": "Some Show",
        "poster": "/poster.jpg",
    }
    item.update(overrides)
    return item


class TestNextEpisodeAirDate:
    def test_returns_air_date_as_yyyy_mm_dd(self):
        detail = {"next_episode_to_air": {"air_date": "2026-07-31"}}
        assert TMDBClient.next_episode_air_date(detail) == "2026-07-31"

    def test_truncates_longer_timestamps(self):
        detail = {"next_episode_to_air": {"air_date": "2026-07-31T20:00:00Z"}}
        assert TMDBClient.next_episode_air_date(detail) == "2026-07-31"

    def test_returns_none_when_key_absent(self):
        assert TMDBClient.next_episode_air_date({"name": "Some Show"}) is None

    def test_returns_none_for_ended_series(self):
        # TMDB sends an explicit null once a series has finished airing.
        assert TMDBClient.next_episode_air_date({"next_episode_to_air": None}) is None

    def test_returns_none_when_air_date_missing(self):
        detail = {"next_episode_to_air": {"episode_number": 4, "air_date": None}}
        assert TMDBClient.next_episode_air_date(detail) is None

    def test_returns_none_for_non_dict_payload(self):
        assert TMDBClient.next_episode_air_date({"next_episode_to_air": "soon"}) is None


class TestExtractMetadataDates:
    def test_movie_detail_records_digital_release_date(self):
        detail = {
            "id": 1234567,
            "title": "Some Film",
            "release_date": "2026-05-01",
            "release_dates": {
                "results": [
                    {
                        "iso_3166_1": "US",
                        "release_dates": [{"type": 4, "release_date": "2026-07-31"}],
                    }
                ]
            },
        }
        meta = TMDBClient().extract_metadata(detail, "movie")
        assert meta["digital_release_date"] == "2026-07-31"
        assert meta["next_episode_air_date"] is None

    def test_tv_detail_records_next_episode_air_date(self):
        detail = {
            "id": 7654321,
            "name": "Some Show",
            "first_air_date": "2020-01-01",
            "next_episode_to_air": {"air_date": "2026-07-31"},
        }
        meta = TMDBClient().extract_metadata(detail, "tv")
        assert meta["next_episode_air_date"] == "2026-07-31"
        assert meta["digital_release_date"] is None

    def test_list_payloads_leave_both_fields_none(self):
        # Discover/trending results carry neither key. Both must come back None so the
        # callers' selective non-null updates don't clobber a stored detail-level date.
        movie = TMDBClient().extract_metadata(
            {"id": 1, "title": "Film", "release_date": "2026-01-01"}, "movie"
        )
        series = TMDBClient().extract_metadata(
            {"id": 2, "name": "Show", "first_air_date": "2026-01-01"}, "tv"
        )
        assert movie["digital_release_date"] is None
        assert movie["next_episode_air_date"] is None
        assert series["digital_release_date"] is None
        assert series["next_episode_air_date"] is None


class TestComingSoonDate:
    def test_movie_uses_digital_release_date(self):
        item = _movie_item(digital_release_date=FUTURE, next_episode_air_date=PAST)
        assert _coming_soon_date(item) == FUTURE

    def test_series_uses_next_episode_air_date(self):
        item = _series_item(next_episode_air_date=FUTURE, digital_release_date=PAST)
        assert _coming_soon_date(item) == FUTURE

    def test_today_counts_as_upcoming(self):
        # The client formatter has a "Coming today" case; a strict > would swallow it.
        assert _coming_soon_date(_movie_item(digital_release_date=TODAY)) == TODAY

    def test_past_date_is_suppressed(self):
        assert _coming_soon_date(_movie_item(digital_release_date=PAST)) is None

    def test_stale_series_date_is_suppressed(self):
        assert _coming_soon_date(_series_item(next_episode_air_date=PAST)) is None

    def test_returns_none_when_fields_absent(self):
        assert _coming_soon_date(_movie_item()) is None
        assert _coming_soon_date(_series_item()) is None

    def test_returns_none_when_fields_are_none(self):
        item = _movie_item(digital_release_date=None, next_episode_air_date=None)
        assert _coming_soon_date(item) is None

    def test_media_type_override_wins_over_item(self):
        # Row serializers pass the resolved type for items missing the key.
        item = {"next_episode_air_date": FUTURE, "digital_release_date": None}
        assert _coming_soon_date(item, "tv") == FUTURE

    def test_explicit_today_is_respected(self):
        item = _movie_item(digital_release_date="2026-07-31")
        assert _coming_soon_date(item, today="2026-07-01") == "2026-07-31"
        assert _coming_soon_date(item, today="2026-08-01") is None

    def test_truncates_longer_stored_values(self):
        item = _series_item(next_episode_air_date=f"{FUTURE}T20:00:00Z")
        assert _coming_soon_date(item) == FUTURE


@pytest.fixture
def worker_db(tmp_path, monkeypatch):
    """Isolated SQLite database for the next-air-date refresh worker tests."""
    engine = create_engine(f"sqlite:///{tmp_path / 'coming_soon.db'}")
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
    # The shelf the test series belong to (universal_catalog_content FKs to it).
    session.add(
        UniversalCategory(
            id="shelf",
            name="Shelf",
            tier=1,
            sort_order=1,
            media_type="tv",
            tag_formula={},
            is_active=True,
        )
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _add_series(db, tmdb_id: int, next_air: str | None, on_shelf: bool = True) -> None:
    db.add(
        MediaMetadata(
            tmdb_id=tmdb_id,
            media_type="tv",
            title=f"Show {tmdb_id}",
            release_date="2020-01-01",
            next_episode_air_date=next_air,
        )
    )
    if on_shelf:
        db.add(
            UniversalCatalogContent(
                category_id="shelf",
                tmdb_id=tmdb_id,
                rank=tmdb_id,
                match_score=1.0,
                media_type="tv",
            )
        )
    db.commit()


class TestRefreshTvNextAirDates:
    @pytest.mark.asyncio
    async def test_updates_clears_and_stamps(self, worker_db, monkeypatch):
        _add_series(worker_db, 301, PAST)  # stale date gets revived
        _add_series(worker_db, 302, FUTURE)  # ended series must be cleared

        payloads = {
            301: {"next_episode_to_air": {"air_date": FUTURE}},
            302: {"next_episode_to_air": None},
        }

        async def fake_get_tv_show(tmdb_id):
            return payloads[tmdb_id]

        monkeypatch.setattr(
            daily_update.tmdb_client, "get_tv_show_summary", fake_get_tv_show
        )
        refreshed = await daily_update._refresh_tv_next_air_dates(worker_db, 50)

        assert refreshed == 2
        rows = {r.tmdb_id: r for r in worker_db.query(MediaMetadata).all()}
        assert rows[301].next_episode_air_date == FUTURE
        # Cleared rather than left stale — the non-null guard is deliberately bypassed.
        assert rows[302].next_episode_air_date is None
        assert all(rows[i].next_episode_checked_at is not None for i in (301, 302))

    @pytest.mark.asyncio
    async def test_skips_series_not_on_any_shelf(self, worker_db, monkeypatch):
        _add_series(worker_db, 401, None, on_shelf=False)
        called = []

        async def fake_get_tv_show(tmdb_id):
            called.append(tmdb_id)
            return {"next_episode_to_air": None}

        monkeypatch.setattr(
            daily_update.tmdb_client, "get_tv_show_summary", fake_get_tv_show
        )
        assert await daily_update._refresh_tv_next_air_dates(worker_db, 50) == 0
        assert called == []

    @pytest.mark.asyncio
    async def test_honors_limit_and_least_recently_checked_order(
        self, worker_db, monkeypatch
    ):
        for tmdb_id in (501, 502, 503):
            _add_series(worker_db, tmdb_id, None)
        # 501 was already checked, so the two unchecked rows come first.
        worker_db.query(MediaMetadata).filter(MediaMetadata.tmdb_id == 501).update(
            {"next_episode_checked_at": datetime.utcnow()}
        )
        worker_db.commit()

        called = []

        async def fake_get_tv_show(tmdb_id):
            called.append(tmdb_id)
            return {"next_episode_to_air": None}

        monkeypatch.setattr(
            daily_update.tmdb_client, "get_tv_show_summary", fake_get_tv_show
        )
        await daily_update._refresh_tv_next_air_dates(worker_db, 2)

        assert sorted(called) == [502, 503]

    @pytest.mark.asyncio
    async def test_limit_of_zero_disables_the_refresh(self, worker_db, monkeypatch):
        _add_series(worker_db, 601, None)
        called = []

        async def fake_get_tv_show(tmdb_id):
            called.append(tmdb_id)
            return {"next_episode_to_air": None}

        monkeypatch.setattr(
            daily_update.tmdb_client, "get_tv_show_summary", fake_get_tv_show
        )
        assert await daily_update._refresh_tv_next_air_dates(worker_db, 0) == 0
        assert called == []

    @pytest.mark.asyncio
    async def test_failed_fetch_keeps_stored_date_but_advances_cursor(
        self, worker_db, monkeypatch
    ):
        _add_series(worker_db, 701, FUTURE)

        async def boom(tmdb_id):
            raise RuntimeError("tmdb down")

        monkeypatch.setattr(daily_update.tmdb_client, "get_tv_show", boom)
        assert await daily_update._refresh_tv_next_air_dates(worker_db, 50) == 0

        row = worker_db.query(MediaMetadata).one()
        assert row.next_episode_air_date == FUTURE
        # Stamped anyway, so one broken id can't wedge the round-robin queue.
        assert row.next_episode_checked_at is not None

    @pytest.mark.asyncio
    async def test_refresh_failure_does_not_fail_the_daily_update(
        self, worker_db, monkeypatch
    ):
        """A cosmetic badge refresh must never sink the whole daily run."""

        async def no_new_releases(media_type):
            return []

        async def explode(db, limit):
            raise RuntimeError("refresh exploded")

        monkeypatch.setattr(
            daily_update.tmdb_client, "get_new_releases_this_week", no_new_releases
        )
        monkeypatch.setattr(daily_update, "_refresh_tv_next_air_dates", explode)

        await daily_update.run_daily_update()

        job = worker_db.query(TaggingJob).one()
        assert job.status == "completed"
        assert job.job_metadata["series_next_air_refreshed"] == 0
