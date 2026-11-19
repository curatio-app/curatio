# ruff: noqa: E402
"""Cross-season reuse of intro/credits references (Track 2)."""

import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_cross_season.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.models import (  # noqa: E402
    Base,
    EpisodeFingerprint,
    SegmentMarker,
    SeasonSegmentReference,
)
import workers.fingerprint_worker as fw  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent / "fixtures" / "segments"))
import _generate as gen  # noqa: E402

IMDB = "tt_cross"


@pytest.fixture()
def fp_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'cross.db'}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)

    @contextmanager
    def get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr(fw, "get_db", get_db)
    yield SessionLocal
    Base.metadata.drop_all(bind=engine)


def _seed_season(SessionLocal, season, episodes, seed):
    """Seed a season's fingerprints. The same seed reproduces the same intro/credits
    signature, mimicking a show whose intro/credits repeat across seasons."""
    case = {
        "imdb_id": IMDB,
        "season": season,
        "head_duration_sec": 600,
        "tail_duration_sec": 600,
        "episode_duration_sec": 1500,
        "seed": seed,
        "intro": {"start": 30.0, "duration": 60.0},
        "credits": {"abs_start": 1440.0, "duration": 55.0},
        "episodes": episodes,
    }
    rows, _labels = gen.expand_case(case)
    with SessionLocal() as db:
        for row in rows:
            db.add(
                EpisodeFingerprint(
                    imdb_id=IMDB,
                    season=season,
                    episode=row["episode"],
                    fingerprint=row["fingerprint"],
                    duration_sec=row["duration_sec"],
                    source_url_hash="h",
                    status="done",
                )
            )
        db.commit()


def test_intro_reference_written_and_reused_on_new_season_episode_one(fp_db):
    # Season 1: three episodes -> confident intro -> intro reference written.
    _seed_season(fp_db, 1, [1, 2, 3], seed=999)
    fw._try_learn_season(IMDB, 1)
    with fp_db() as db:
        refs = db.query(SeasonSegmentReference).filter_by(imdb_id=IMDB).all()
    assert "intro" in {r.segment_type for r in refs}

    # Season 2: only episode 1 (cannot form a pair) -> filled from the season-1 reference.
    _seed_season(fp_db, 2, [1], seed=999)
    fw._try_learn_season(IMDB, 2)
    with fp_db() as db:
        intro = (
            db.query(SegmentMarker)
            .filter_by(imdb_id=IMDB, season=2, episode=1, segment_type="intro")
            .first()
        )
    assert intro is not None, "season 2 ep1 intro not filled from reference"
    assert intro.source == "cross_season"
    assert intro.confidence <= fw.CROSS_SEASON_CONFIDENCE_CAP + 1e-6
    assert abs(intro.start_sec - 30.0) < 3.0  # planted 30-90s window


def test_credits_reference_reused_on_new_season_episode_one(fp_db):
    # Insert a credits reference directly (the credits signature for seed=777), so the reuse
    # path is exercised deterministically without depending on season-1 credits confidence.
    credits_sig = gen._signature(fw.seconds_to_point(55.0), 777 + 2)
    with fp_db() as db:
        db.add(
            SeasonSegmentReference(
                imdb_id=IMDB,
                season=1,
                segment_type="credits",
                fingerprint={
                    "points": credits_sig,
                    "fingerprint_version": fw.FINGERPRINT_VERSION,
                    "detector_version": fw.DETECTOR_VERSION,
                },
                duration_sec=55.0,
                confidence=0.95,
                derived_from_episodes=3,
            )
        )
        db.commit()

    _seed_season(fp_db, 2, [1], seed=777)
    fw._try_learn_season(IMDB, 2)
    with fp_db() as db:
        credits = (
            db.query(SegmentMarker)
            .filter_by(imdb_id=IMDB, season=2, episode=1, segment_type="credits")
            .first()
        )
    assert credits is not None, "season 2 ep1 credits not filled from reference"
    assert credits.source == "audio+visual"
    assert credits.evidence["version"] == fw.VISUAL_FEATURE_VERSION
    assert credits.confidence <= fw.CROSS_SEASON_CONFIDENCE_CAP + 1e-6
    assert abs(credits.start_sec - 1440.0) < 4.0  # planted ~1440-1495s


def _seed_credits_season(SessionLocal, season, seed, credits):
    """Seed a one-episode season whose credits signature is planted at a chosen position."""
    case = {
        "imdb_id": IMDB,
        "season": season,
        "head_duration_sec": 600,
        "tail_duration_sec": 600,
        "episode_duration_sec": 1500,
        "seed": seed,
        "intro": {"start": 30.0, "duration": 60.0},
        "credits": credits,
        "episodes": [1],
    }
    rows, _labels = gen.expand_case(case)
    with SessionLocal() as db:
        for row in rows:
            db.add(
                EpisodeFingerprint(
                    imdb_id=IMDB,
                    season=season,
                    episode=row["episode"],
                    fingerprint=row["fingerprint"],
                    duration_sec=row["duration_sec"],
                    source_url_hash="h",
                    status="done",
                )
            )
        db.commit()


def test_cross_season_credits_reference_rejected_when_too_early(fp_db):
    # A >=60s credits reference that aligns to an implausibly-early tail position
    # (start ratio < CREDITS_MIN_START_RATIO) must NOT seed a credits marker. The old
    # cross-season guard only rejected `ratio < 0.85 AND duration < 60`, so a long early
    # match — a recurring mid-tail cue, not the real credits — slipped through.
    seed = 555
    credits_dur = 70.0  # >= 60s, so the old `duration < 60` clause never fired
    credits_sig = gen._signature(fw.seconds_to_point(credits_dur), seed + 2)
    with fp_db() as db:
        db.add(
            SeasonSegmentReference(
                imdb_id=IMDB,
                season=1,
                segment_type="credits",
                fingerprint={
                    "points": credits_sig,
                    "fingerprint_version": fw.FINGERPRINT_VERSION,
                    "detector_version": fw.DETECTOR_VERSION,
                },
                duration_sec=credits_dur,
                confidence=0.95,
                derived_from_episodes=3,
            )
        )
        db.commit()

    # Early alignment: abs_start 990 / 1500 = 0.66 (< 0.75) -> rejected despite being long.
    _seed_credits_season(fp_db, 2, seed, {"abs_start": 990.0, "duration": credits_dur})
    fw._try_learn_season(IMDB, 2)
    with fp_db() as db:
        early = (
            db.query(SegmentMarker)
            .filter_by(imdb_id=IMDB, season=2, episode=1, segment_type="credits")
            .first()
        )
    assert early is None, "too-early cross-season credits marker should be rejected"

    # Positive control: the same reference at a plausible late position IS reused, proving
    # the rejection above is due to position, not a failure to match.
    _seed_credits_season(fp_db, 3, seed, {"abs_start": 1400.0, "duration": credits_dur})
    fw._try_learn_season(IMDB, 3)
    with fp_db() as db:
        late = (
            db.query(SegmentMarker)
            .filter_by(imdb_id=IMDB, season=3, episode=1, segment_type="credits")
            .first()
        )
    assert late is not None, "valid late cross-season credits marker should be created"
    assert late.source == "audio+visual"
    assert abs(late.start_sec - 1400.0) < 4.0  # planted 1400-1470s


def test_version_mismatched_reference_is_ignored(fp_db):
    with fp_db() as db:
        db.add(
            SeasonSegmentReference(
                imdb_id=IMDB,
                season=1,
                segment_type="intro",
                fingerprint={
                    "points": [1, 2, 3, 4],
                    "fingerprint_version": "OLD",
                    "detector_version": "OLD",
                },
                duration_sec=60.0,
                confidence=0.95,
                derived_from_episodes=3,
            )
        )
        db.commit()
        refs = fw._load_season_references(db, IMDB, "intro")
    assert refs == []
