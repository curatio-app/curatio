# ruff: noqa: E402
"""Serve-time user-feedback override of auto segment markers (Track 1c)."""

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_segment_feedback.db")
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
    SegmentFeedback,
    User,
)
from app import main as appmain  # noqa: E402
from workers import fingerprint_worker as fw  # noqa: E402
from app.segment_policy import set_season_segment_enabled  # noqa: E402


@pytest.fixture()
def seg_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        db.add(User(id=1, user_key="u1"))
        db.add(User(id=2, user_key="u2"))
        db.commit()
    yield SessionLocal
    Base.metadata.drop_all(bind=engine)


def _add_marker(db, **kw):
    defaults = dict(
        imdb_id="tt1",
        season=1,
        episode=1,
        segment_type="intro",
        start_sec=10.0,
        end_sec=70.0,
        confidence=0.9,
        source="chromaprint",
        fingerprint_version=fw.MARKER_FINGERPRINT_VERSION,
        detector_version=fw.DETECTOR_VERSION,
        is_user_provided=False,
    )
    defaults.update(kw)
    if defaults["segment_type"] == "credits" and defaults["source"] == "chromaprint":
        defaults["source"] = "visual-v3+boundary"
        defaults["evidence"] = {
            "version": fw.VISUAL_FEATURE_VERSION,
            "score": 0.95,
            "qualifying_ratio": 0.95,
            "onset_sec": defaults["start_sec"],
            "boundary_confirmed": True,
        }
    db.add(SegmentMarker(**defaults))


def _add_feedback(db, user_id, feedback, **kw):
    defaults = dict(
        user_id=user_id,
        imdb_id="tt1",
        season=1,
        episode=1,
        segment_type="intro",
        feedback=feedback,
    )
    defaults.update(kw)
    db.add(SegmentFeedback(**defaults))


def _add_fingerprint(db, duration_sec=1800.0, **kw):
    defaults = dict(
        imdb_id="tt1",
        season=1,
        episode=1,
        fingerprint=[],
        duration_sec=duration_sec,
        status="completed",
    )
    defaults.update(kw)
    db.add(EpisodeFingerprint(**defaults))


def test_negative_feedback_suppresses_auto_marker(seg_db):
    with seg_db() as db:
        _add_marker(db)
        _add_feedback(db, 1, "wrong")
        _add_feedback(db, 2, "wrong")
        db.commit()
        markers = db.query(SegmentMarker).all()
        assert appmain._build_served_segments(db, "tt1", 1, 1, markers) == []


def test_season_override_suppresses_only_selected_segment_type(seg_db):
    with seg_db() as db:
        _add_fingerprint(db, duration_sec=1800)
        _add_marker(db, segment_type="intro", start_sec=10, end_sec=70)
        _add_marker(
            db,
            segment_type="credits",
            start_sec=1740,
            end_sec=1800,
            confidence=0.95,
        )
        set_season_segment_enabled(db, "tt1", 1, "credits", False)
        db.commit()

        markers = db.query(SegmentMarker).all()
        segments = appmain._build_served_segments(db, "tt1", 1, 1, markers)

        assert [segment["type"] for segment in segments] == ["intro"]
        assert segments[0]["boundary_status"] == "confirmed"


def test_user_provided_marker_never_suppressed(seg_db):
    with seg_db() as db:
        _add_marker(db, is_user_provided=True, source="manual")
        _add_feedback(db, 1, "wrong")
        _add_feedback(db, 2, "wrong")
        db.commit()
        markers = db.query(SegmentMarker).all()
        segs = appmain._build_served_segments(db, "tt1", 1, 1, markers)
        assert len(segs) == 1


def test_correct_feedback_boosts_marker_over_gate(seg_db):
    with seg_db() as db:
        # Below the credits 0.70 gate; one "correct" lifts it to 0.76.
        _add_fingerprint(db, duration_sec=1450)
        _add_marker(
            db, segment_type="credits", confidence=0.66, start_sec=1400, end_sec=1450
        )
        _add_feedback(db, 1, "correct", segment_type="credits")
        db.commit()
        markers = db.query(SegmentMarker).all()
        segs = appmain._build_served_segments(db, "tt1", 1, 1, markers)
        assert len(segs) == 1
        assert segs[0]["confidence"] == pytest.approx(0.76)


def test_user_adjustment_overrides_boundaries(seg_db):
    with seg_db() as db:
        _add_marker(db, confidence=0.2)  # below gate, but adjustment forces it through
        _add_feedback(db, 1, "wrong", adjusted_start=12.5, adjusted_end=64.0)
        db.commit()
        markers = db.query(SegmentMarker).all()
        segs = appmain._build_served_segments(db, "tt1", 1, 1, markers)
        assert len(segs) == 1
        assert segs[0]["start"] == 12.5
        assert segs[0]["end"] == 64.0


def test_stale_early_credits_auto_marker_suppressed(seg_db):
    # A confident credits marker persisted before the detector grew its
    # CREDITS_MIN_START_RATIO floor sits mid-episode (60%); it must not be
    # served, or the Next Episode prompt fires minutes early.
    with seg_db() as db:
        _add_fingerprint(db)
        _add_marker(
            db, segment_type="credits", confidence=0.95, start_sec=1080, end_sec=1170
        )
        db.commit()
        markers = db.query(SegmentMarker).all()
        assert appmain._build_served_segments(db, "tt1", 1, 1, markers) == []


def test_nonterminal_credits_marker_is_suppressed(seg_db):
    with seg_db() as db:
        _add_fingerprint(db, duration_sec=900.0)  # floor = 0.75 * 900 = 675
        _add_marker(
            db, segment_type="credits", confidence=0.95, start_sec=675, end_sec=740
        )
        db.commit()
        markers = db.query(SegmentMarker).all()
        assert appmain._build_served_segments(db, "tt1", 1, 1, markers) == []


def test_credits_marker_too_far_from_end_suppressed(seg_db):
    # The case the ratio floor structurally cannot catch: a 60-min episode with credits
    # at 3000s is ratio 0.833 — above CREDITS_MIN_START_RATIO — but 10 minutes early.
    with seg_db() as db:
        _add_fingerprint(db, duration_sec=3600.0)
        _add_marker(
            db, segment_type="credits", confidence=0.95, start_sec=3000, end_sec=3120
        )
        db.commit()
        markers = db.query(SegmentMarker).all()
        assert appmain._build_served_segments(db, "tt1", 1, 1, markers) == []


def test_credits_marker_exactly_at_distance_cap_is_served(seg_db):
    # Boundary companion: the distance check is a strict >, so exactly at the cap serves.
    from workers.fingerprint_worker import CREDITS_MAX_START_DISTANCE_FROM_END_SEC

    start = 3600 - CREDITS_MAX_START_DISTANCE_FROM_END_SEC
    with seg_db() as db:
        _add_fingerprint(db, duration_sec=3600.0)
        _add_marker(
            db,
            segment_type="credits",
            confidence=0.95,
            start_sec=start,
            end_sec=3600,
        )
        db.commit()
        markers = db.query(SegmentMarker).all()
        segs = appmain._build_served_segments(db, "tt1", 1, 1, markers)
        assert len(segs) == 1
        assert segs[0]["start"] == start


def test_user_marker_exempt_from_distance_cap(seg_db):
    # A user saying "credits start here" stays authoritative past the absolute cap too.
    with seg_db() as db:
        _add_fingerprint(db, duration_sec=3600.0)
        _add_marker(
            db,
            segment_type="credits",
            confidence=0.95,
            start_sec=3000,
            end_sec=3120,
            is_user_provided=True,
            source="manual",
        )
        db.commit()
        markers = db.query(SegmentMarker).all()
        segs = appmain._build_served_segments(db, "tt1", 1, 1, markers)
        assert len(segs) == 1
        assert segs[0]["start"] == 3000


def test_credits_marker_suppressed_when_duration_unknown(seg_db):
    # Unknown duration fails closed for auto-generated credits.
    with seg_db() as db:
        _add_marker(
            db, segment_type="credits", confidence=0.95, start_sec=1080, end_sec=1170
        )
        db.commit()
        markers = db.query(SegmentMarker).all()
        assert appmain._build_served_segments(db, "tt1", 1, 1, markers) == []


def test_user_provided_early_credits_marker_not_gated(seg_db):
    # A user saying "credits start here" is authoritative even below the floor.
    with seg_db() as db:
        _add_fingerprint(db)
        _add_marker(
            db,
            segment_type="credits",
            confidence=0.95,
            start_sec=900,
            end_sec=980,
            is_user_provided=True,
            source="manual",
        )
        db.commit()
        markers = db.query(SegmentMarker).all()
        segs = appmain._build_served_segments(db, "tt1", 1, 1, markers)
        assert len(segs) == 1
        assert segs[0]["start"] == 900


def test_user_adjustment_bypasses_credits_floor(seg_db):
    # Median user adjustments override the auto boundaries and stay exempt from
    # the plausibility floor, like every other user-sourced signal.
    with seg_db() as db:
        _add_fingerprint(db)
        _add_marker(
            db, segment_type="credits", confidence=0.95, start_sec=1080, end_sec=1170
        )
        _add_feedback(
            db,
            1,
            "wrong",
            segment_type="credits",
            adjusted_start=1100.0,
            adjusted_end=1160.0,
        )
        db.commit()
        markers = db.query(SegmentMarker).all()
        segs = appmain._build_served_segments(db, "tt1", 1, 1, markers)
        assert len(segs) == 1
        assert segs[0]["start"] == 1100.0
        assert segs[0]["end"] == 1160.0


def test_intro_marker_unaffected_by_credits_floor(seg_db):
    # The floor is credits-specific: an intro near the episode start still serves
    # with a fingerprint row present.
    with seg_db() as db:
        _add_fingerprint(db)
        _add_marker(db)
        db.commit()
        markers = db.query(SegmentMarker).all()
        segs = appmain._build_served_segments(db, "tt1", 1, 1, markers)
        assert len(segs) == 1
        assert segs[0]["type"] == "intro"


def test_stale_version_and_audio_only_auto_markers_are_suppressed(seg_db):
    with seg_db() as db:
        _add_fingerprint(db, duration_sec=1_500)
        _add_marker(
            db,
            segment_type="intro",
            fingerprint_version="chromaprint-v2:audio-fingerprint-v5",
        )
        _add_marker(
            db,
            segment_type="credits",
            start_sec=1_440,
            end_sec=1_500,
            source="audio-only",
        )
        db.commit()
        markers = db.query(SegmentMarker).all()
        assert appmain._build_served_segments(db, "tt1", 1, 1, markers) == []


def test_duplicate_auto_markers_collapse_to_current_canonical_marker(seg_db):
    with seg_db() as db:
        _add_marker(
            db,
            start_sec=5,
            end_sec=65,
            confidence=0.99,
            fingerprint_version="old",
        )
        _add_marker(db, start_sec=20, end_sec=80, confidence=0.8)
        db.commit()
        segments = appmain._build_served_segments(
            db, "tt1", 1, 1, db.query(SegmentMarker).all()
        )
        assert len(segments) == 1
        assert segments[0]["start"] == 20


def test_user_marker_takes_precedence_over_auto_duplicate(seg_db):
    with seg_db() as db:
        _add_marker(db, start_sec=20, end_sec=80, confidence=0.99)
        _add_marker(
            db,
            start_sec=30,
            end_sec=75,
            confidence=0.5,
            source="manual",
            is_user_provided=True,
        )
        db.commit()
        segments = appmain._build_served_segments(
            db, "tt1", 1, 1, db.query(SegmentMarker).all()
        )
        assert len(segments) == 1
        assert segments[0]["start"] == 30
