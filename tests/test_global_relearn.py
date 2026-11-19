# ruff: noqa: E402
"""Global markers-only relearn: re-derive every season's markers from stored fingerprints.

Covers the admin sweep added so a change to how markers are detected can be applied to a
library that was already fingerprinted, without re-downloading any audio.
"""

import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_global_relearn.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.models import (  # noqa: E402
    Base,
    EpisodeFingerprint,
    SeasonLearningState,
    SeasonSegmentReference,
    SegmentMarker,
)
from app import admin as appadmin  # noqa: E402


@pytest.fixture()
def relearn_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'relearn.db'}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)

    @contextmanager
    def get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr(appadmin, "get_db", get_db)
    # The sweep is about bookkeeping; marker derivation itself is covered by the eval
    # harness, so stub the expensive learner and record which seasons it was asked for.
    learned: list[tuple[str, int]] = []
    import workers.fingerprint_worker as fw

    monkeypatch.setattr(
        fw,
        "_try_learn_season",
        lambda imdb_id, season: learned.append((imdb_id, season)),
    )
    monkeypatch.setattr(
        fw, "_pair_match_cache_clear_season", lambda imdb_id, season: None
    )
    yield SessionLocal, learned
    Base.metadata.drop_all(bind=engine)


def _seed_season(db, imdb_id, season, episodes=(1, 2)):
    for ep in episodes:
        db.add(
            EpisodeFingerprint(
                imdb_id=imdb_id,
                season=season,
                episode=ep,
                fingerprint={},
                duration_sec=1800.0,
                status="done",
            )
        )
    db.add(
        SeasonLearningState(
            imdb_id=imdb_id, season=season, done_episodes=list(episodes)
        )
    )
    db.add(
        SeasonSegmentReference(
            imdb_id=imdb_id,
            season=season,
            segment_type="credits",
            fingerprint={},
            duration_sec=60.0,
            confidence=0.9,
            derived_from_episodes=2,
        )
    )


def test_fingerprint_series_exact_lookup_preserves_paging(relearn_db):
    SessionLocal, _ = relearn_db
    with SessionLocal() as db:
        for imdb_id in ("tt1", "tt2", "tt3"):
            _seed_season(db, imdb_id, 1)
        db.commit()

    page = appadmin.get_fingerprint_series(request=None, skip=1, limit=1, _=True)
    assert page["total"] == 3
    assert page["has_more"] is True
    assert [row["imdb_id"] for row in page["series"]] == ["tt2"]

    exact = appadmin.get_fingerprint_series(
        request=None, skip=0, limit=1, imdb_id="tt3", _=True
    )
    assert exact["total"] == 1
    assert exact["has_more"] is False
    assert [
        (row["imdb_id"], row["seasons"][0]["season"]) for row in exact["series"]
    ] == [("tt3", 1)]

    missing = appadmin.get_fingerprint_series(
        request=None, skip=0, limit=1, imdb_id="ttmissing", _=True
    )
    assert missing["total"] == 0
    assert missing["series"] == []


def test_relearn_all_sweeps_every_fingerprinted_season(relearn_db):
    SessionLocal, learned = relearn_db
    with SessionLocal() as db:
        _seed_season(db, "tt1", 1)
        _seed_season(db, "tt1", 2)
        _seed_season(db, "tt2", 1)
        db.commit()

    appadmin._relearn_all_state.clear()
    appadmin._relearn_all_state.update(
        {
            "state": "starting",
            "seasons_total": 0,
            "seasons_done": 0,
            "markers_deleted": 0,
            "failures": 0,
        }
    )
    appadmin._run_relearn_all()

    assert appadmin._relearn_all_state["state"] == "complete"
    assert appadmin._relearn_all_state["seasons_total"] == 3
    assert appadmin._relearn_all_state["seasons_done"] == 3
    assert appadmin._relearn_all_state["failures"] == 0
    assert sorted(learned) == [("tt1", 1), ("tt1", 2), ("tt2", 1)]


def test_relearn_season_clears_state_and_references_but_keeps_user_data(relearn_db):
    SessionLocal, _learned = relearn_db
    with SessionLocal() as db:
        _seed_season(db, "tt1", 1)
        db.add(
            SegmentMarker(
                imdb_id="tt1",
                season=1,
                episode=1,
                segment_type="credits",
                start_sec=1000,
                end_sec=1060,
                confidence=0.9,
                source="chromaprint",
                is_user_provided=False,
            )
        )
        db.add(
            SegmentMarker(
                imdb_id="tt1",
                season=1,
                episode=2,
                segment_type="credits",
                start_sec=1700,
                end_sec=1760,
                confidence=0.9,
                source="manual",
                is_user_provided=True,
            )
        )
        db.commit()

    deleted = appadmin._relearn_season("tt1", 1)

    assert deleted == 1  # only the auto marker
    with SessionLocal() as db:
        remaining = db.query(SegmentMarker).all()
        assert len(remaining) == 1
        assert remaining[0].is_user_provided is True
        # Both would otherwise make the relearn a no-op or re-seed the old markers.
        assert (
            db.query(SeasonLearningState).filter_by(imdb_id="tt1", season=1).count()
            == 0
        )
        assert (
            db.query(SeasonSegmentReference).filter_by(imdb_id="tt1", season=1).count()
            == 0
        )


@pytest.mark.asyncio
async def test_relearn_all_rejects_a_concurrent_start(relearn_db):
    import asyncio

    from fastapi import HTTPException

    async def _never_finishes():
        await asyncio.sleep(30)

    running = asyncio.create_task(_never_finishes())
    appadmin._relearn_all_task = running
    try:
        with pytest.raises(HTTPException) as exc:
            await appadmin.relearn_all_seasons(request=None, _=None)
        assert exc.value.status_code == 409
    finally:
        running.cancel()
        appadmin._relearn_all_task = None


def test_relearn_all_survives_one_failing_season(relearn_db, monkeypatch):
    SessionLocal, _learned = relearn_db
    with SessionLocal() as db:
        _seed_season(db, "tt1", 1)
        _seed_season(db, "tt2", 1)
        db.commit()

    def _boom(imdb_id, season):
        if imdb_id == "tt1":
            raise RuntimeError("season exploded")
        return 0

    monkeypatch.setattr(appadmin, "_relearn_season", _boom)
    appadmin._relearn_all_state.clear()
    appadmin._relearn_all_state.update(
        {
            "state": "starting",
            "seasons_total": 0,
            "seasons_done": 0,
            "markers_deleted": 0,
            "failures": 0,
        }
    )
    appadmin._run_relearn_all()

    assert appadmin._relearn_all_state["state"] == "complete"
    assert appadmin._relearn_all_state["seasons_done"] == 2
    assert appadmin._relearn_all_state["failures"] == 1
