# ruff: noqa: E402
import os
import queue
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_fingerprint_v6.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app import main as appmain
from app.models import Base, EpisodeFingerprint
from workers import fingerprint_worker as fw


def _candidate(pair, start, end, hamming=0.0, coverage=1.0):
    return {
        "episode": 1,
        "pair_key": pair,
        "start_sec": start,
        "end_sec": end,
        "duration_sec": end - start,
        "avg_hamming_distance": hamming,
        "matched_point_count": fw.seconds_to_point(end - start),
        "match_density": coverage,
    }


def test_inconsistent_pair_candidates_cannot_pool_credits_confidence():
    selected, reason, diagnostics = fw._choose_credits_candidate(
        [
            _candidate("1x2", 1_700, 1_800),
            _candidate("1x3", 1_600, 1_800),
        ],
        [(1, 2), (1, 3)],
        1,
        1_800,
    )
    assert selected is None
    assert reason == "no_reliable_credits_candidate"
    assert diagnostics["candidate_cluster_count"] == 2
    assert {cluster["support"] for cluster in diagnostics["clusters"]} == {1}


def test_single_pristine_twenty_second_intro_is_allowed():
    selected, reason, diagnostics = fw._choose_intro_candidate(
        [_candidate("1x2", 5, 25, hamming=2.0, coverage=0.85)],
        [(1, 2)],
        1,
    )
    assert reason is None
    assert selected is not None
    assert selected["duration_sec"] == 20
    assert diagnostics["point_coverage"] == pytest.approx(0.85)


def test_intro_cluster_ranking_uses_earlier_start_after_equal_evidence():
    candidates = [
        _candidate("1x2", 10, 40),
        _candidate("1x3", 10, 40),
        _candidate("1x2", 100, 130),
        _candidate("1x3", 100, 130),
    ]
    selected, reason, _diagnostics = fw._choose_intro_candidate(
        candidates, [(1, 2), (1, 3)], 1
    )
    assert reason is None
    assert selected["start_sec"] == 10


def test_sparse_gap_bridged_run_is_rejected_by_point_coverage():
    samples = [(index * 1.8, index * 1.8, 0, 0, 0) for index in range(20)]
    accepted = []
    rejected = []
    fw._append_chromaprint_run(
        accepted,
        samples,
        0,
        "sparse",
        fw.MIN_INTRO_DURATION,
        fw.MAX_INTRO_DURATION,
        rejected,
    )
    assert accepted == []
    assert rejected[0]["reason"] == "match_density_too_low"
    assert rejected[0]["match_density"] < fw.MIN_MATCH_DENSITY


def test_visual_score_separates_credit_rows_from_scene_noise_and_subtitles():
    credits = np.zeros((144, 256, 3), dtype=np.uint8)
    for y in range(25, 125, 14):
        credits[y : y + 2, 55:205] = 255
    first_score, first_mask = fw._visual_frame_score(credits, None)
    scrolling_score, _ = fw._visual_frame_score(
        np.roll(credits, -2, axis=0), first_mask
    )

    scene = np.random.default_rng(3).integers(0, 256, (144, 256, 3), dtype=np.uint8)
    scene_score, _ = fw._visual_frame_score(scene, None)
    subtitle = np.zeros((144, 256, 3), dtype=np.uint8)
    subtitle[120:123, 60:196] = 255
    subtitle_score, _ = fw._visual_frame_score(subtitle, None)

    assert first_score >= fw.CREDITS_VISUAL_MIN_SCORE
    assert scrolling_score > first_score
    assert scene_score < fw.CREDITS_VISUAL_MIN_SCORE
    assert subtitle_score < fw.CREDITS_VISUAL_MIN_SCORE


def test_hybrid_credits_snap_to_later_visual_onset_and_fail_without_it():
    audio = {
        "start_sec": 1_650,
        "end_sec": 1_800,
        "duration_sec": 150,
        "confidence": 0.95,
        "support_count": 2,
        "source": "chromaprint",
    }
    visual = [
        {
            "start_sec": 230,
            "end_sec": 240,
            "score": 0.92,
            "qualifying_ratio": 0.9,
        }
    ]
    fused, reason = fw._fuse_credits_with_visual(audio, visual, 1_800, 240)
    assert reason is None
    assert fused["start_sec"] == 1_790
    assert fused["audio_start_sec"] == 1_650
    assert fused["source"] == "audio+visual"

    missing, reason = fw._fuse_credits_with_visual(audio, [], 1_800, 240)
    assert missing is None
    assert reason == "missing_audio_or_visual_evidence"


def test_visual_first_credits_requires_terminal_text_run():
    late, reason = fw._visual_first_credits_candidate(
        [
            {
                "start_sec": 180,
                "end_sec": 200,
                "score": 0.92,
                "qualifying_ratio": 0.9,
            }
        ],
        1_800,
        240,
    )
    assert late is not None
    assert reason is None
    assert late["start_sec"] == 1_740

    early, reason = fw._visual_first_credits_candidate(
        [
            {
                "start_sec": 20,
                "end_sec": 40,
                "score": 0.95,
                "qualifying_ratio": 0.95,
            }
        ],
        1_800,
        240,
    )
    assert early is None
    assert reason == "no_terminal_visual_credits_run"


def test_visual_first_credits_uses_short_boundary_confirmation(monkeypatch):
    candidate = {
        "start_sec": 1_740.0,
        "end_sec": 1_800.0,
        "duration_sec": 60.0,
        "visual_score": 0.92,
        "qualifying_ratio": 0.9,
        "terminal_score": 0.5,
    }
    calls = []

    def fake_detect(_url, start, duration, *, force=False):
        calls.append((start, duration, force))
        return {"black": [(1_739.5, 1_741.0)], "silence": [(1_739.0, 1_742.0)]}

    monkeypatch.setattr(fw, "_detect_black_and_silence", fake_detect)
    confirmed = fw._confirm_visual_credits_candidate(
        "https://cdn.example/ep", candidate
    )
    assert confirmed is not None
    assert confirmed["source"] == "visual-v3+boundary"
    assert confirmed["visual_evidence"]["boundary_confirmed"] is True
    assert calls == [(1_734.0, fw.CREDITS_V2_AUDIO_CONFIRM_WINDOW_SEC, True)]


@pytest.fixture()
def phase_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'phases.db'}")
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
    monkeypatch.setattr(fw, "_try_learn_season", lambda *args: None)
    yield SessionLocal
    Base.metadata.drop_all(bind=engine)


def test_head_success_remains_usable_and_tail_completes_later(phase_db, monkeypatch):
    monkeypatch.setattr(fw, "_probe_duration", lambda _url: 1_800.0)
    monkeypatch.setattr(
        fw,
        "_extract_chromaprint",
        lambda _url, _start, _duration, label: (
            [1, 2, 3] if label.endswith("head") else [4, 5, 6],
            "ffmpeg_chromaprint_raw",
        ),
    )
    monkeypatch.setattr(
        fw,
        "_extract_tail_visual_runs",
        lambda *_args: [
            {"start_sec": 580, "end_sec": 600, "score": 0.9, "qualifying_ratio": 0.9}
        ],
    )
    monkeypatch.setattr(
        fw,
        "_confirm_visual_credits_candidate",
        lambda _url, candidate: {
            "start_sec": candidate["start_sec"],
            "end_sec": candidate["end_sec"],
            "duration_sec": candidate["duration_sec"],
            "confidence": 0.95,
            "source": "visual-v3+boundary",
            "visual_evidence": {
                "version": fw.VISUAL_FEATURE_VERSION,
                "score": 0.9,
                "onset_sec": candidate["start_sec"],
            },
        },
    )
    canonical = "ttphase:1:1"
    url = "https://cdn.example/episode.mkv"

    fw._process_job(canonical, url, phase="head")
    with phase_db() as db:
        row = db.query(EpisodeFingerprint).one()
        assert row.status == "partial"
        assert fw._audio_phase_ready(row.fingerprint, "head")
        assert not fw._audio_phase_ready(row.fingerprint, "tail")

    fw._process_job(canonical, url, phase="tail")
    with phase_db() as db:
        row = db.query(EpisodeFingerprint).one()
        assert row.status == "done"
        assert row.fingerprint["phases"]["tail"]["status"] == "done"
        assert fw._visual_phase_ready(row.fingerprint)
        assert not fw._audio_phase_ready(row.fingerprint, "tail")
        assert (
            row.fingerprint["credits_v2"]["candidate"]["source"] == "visual-v3+boundary"
        )


def test_fingerprint_media_work_holds_no_database_connection(phase_db, monkeypatch):
    """ffprobe and Chromaprint run only after the state checkout is returned."""
    bind = phase_db.kw["bind"] if hasattr(phase_db, "kw") else phase_db().get_bind()
    observed = []

    def checked_out():
        metric = getattr(bind.pool, "checkedout", None)
        return metric() if callable(metric) else 0

    def probe(_url):
        observed.append(("probe", checked_out()))
        return 1_800.0

    def extract(_url, _start, _duration, _label):
        observed.append(("extract", checked_out()))
        return [1, 2, 3], "ffmpeg_chromaprint_raw"

    monkeypatch.setattr(fw, "_probe_duration", probe)
    monkeypatch.setattr(fw, "_extract_chromaprint", extract)
    fw._process_job("ttfree:1:1", "https://cdn.example/free.mkv", phase="head")

    assert observed == [("probe", 0), ("extract", 0)]


def test_tail_visual_failure_preserves_completed_head_phase(phase_db, monkeypatch):
    monkeypatch.setattr(fw, "_probe_duration", lambda _url: 1_800.0)
    monkeypatch.setattr(
        fw,
        "_extract_chromaprint",
        lambda *_args: ([7, 8, 9], "ffmpeg_chromaprint_raw"),
    )
    monkeypatch.setattr(
        fw,
        "_extract_tail_visual_runs",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("visual decode failed")),
    )
    canonical = "ttphase:1:2"
    url = "https://cdn.example/episode2.mkv"
    fw._process_job(canonical, url, phase="head")
    fw._process_job(canonical, url, phase="tail")

    with phase_db() as db:
        row = db.query(EpisodeFingerprint).one()
        assert row.status == "partial"
        assert fw._audio_phase_ready(row.fingerprint, "head")
        assert not fw._audio_phase_ready(row.fingerprint, "tail")
        assert row.fingerprint["phases"]["tail"]["status"] == "failed"
        assert "visual decode failed" in row.error_message


def test_visual_only_augmentation_reuses_existing_audio(phase_db, monkeypatch):
    canonical = "ttphase:1:3"
    url = "https://cdn.example/episode3.mkv"
    payload = {
        "head": [1, 2, 3],
        "tail": [4, 5, 6],
        "head_point_count": 3,
        "tail_point_count": 3,
        "head_duration_sec": 300,
        "tail_duration_sec": 240,
        "fingerprint_version": fw.FINGERPRINT_VERSION,
        "detector_version": "audio-fingerprint-v5",
    }
    with phase_db() as db:
        db.add(
            EpisodeFingerprint(
                imdb_id="ttphase",
                season=1,
                episode=3,
                fingerprint=payload,
                duration_sec=1_800,
                source_url_hash=fw._source_url_hash(url),
                status="partial",
            )
        )
        db.commit()

    monkeypatch.setattr(
        fw,
        "_extract_chromaprint",
        lambda *_args: (_ for _ in ()).throw(AssertionError("audio must be reused")),
    )
    monkeypatch.setattr(fw, "_extract_tail_visual_runs", lambda *_args: [])
    fw._process_job(canonical, url, phase="tail_visual")

    with phase_db() as db:
        row = db.query(EpisodeFingerprint).one()
        assert row.status == "done"
        assert row.fingerprint["head"] == [1, 2, 3]
        assert row.fingerprint["tail"] == [4, 5, 6]
        assert fw._visual_phase_ready(row.fingerprint)


def test_tail_completion_does_not_schedule_extended_head_retry(phase_db, monkeypatch):
    canonical = "ttphase:1:4"
    url = "https://cdn.example/episode4.mkv"
    with phase_db() as db:
        db.add(
            EpisodeFingerprint(
                imdb_id="ttphase",
                season=1,
                episode=4,
                fingerprint={
                    "head": [1, 2, 3],
                    "head_point_count": 3,
                    "head_duration_sec": fw.HEAD_WINDOW,
                    "fingerprint_version": fw.FINGERPRINT_VERSION,
                    "detector_version": fw.DETECTOR_VERSION,
                },
                duration_sec=1_800,
                source_url_hash=fw._source_url_hash(url),
                status="partial",
            )
        )
        db.commit()

    monkeypatch.setattr(fw, "_extract_tail_visual_runs", lambda *_args: [])
    queued = []
    monkeypatch.setattr(
        fw,
        "queue_fingerprint_job",
        lambda canonical_id, source_url, priority, phase="head": queued.append(phase)
        or True,
    )
    with fw._inflight_lock:
        fw._inflight.add((canonical, "tail"))
    fw._process_job(canonical, url, phase="tail", priority=fw.PRIORITY_CURRENT_TAIL)

    with phase_db() as db:
        row = db.query(EpisodeFingerprint).one()
        assert row.status == "done"
        assert "extended_head" not in row.fingerprint["phases"]
        response = appmain._build_segments_response(db, canonical, "ttphase", 1, 4)
        assert response["fingerprint_status"] == "complete"
    assert queued == []


def test_staged_queue_orders_companion_heads_before_current_tail(monkeypatch):
    local_queue = queue.PriorityQueue()
    monkeypatch.setattr(fw, "_job_queue", local_queue)
    monkeypatch.setattr(fw, "_inflight", set())
    fw.queue_fingerprint_job("ttq:1:1", "u1", fw.PRIORITY_STREAM_OPEN)
    fw.queue_fingerprint_job("ttq:1:2", "u2", fw.PRIORITY_LOOKAHEAD)
    fw.queue_fingerprint_job("ttq:1:3", "u3", fw.PRIORITY_LOOKAHEAD)
    fw.queue_fingerprint_job("ttq:1:1", "u1", fw.PRIORITY_CURRENT_TAIL, phase="tail")
    phases = [local_queue.get_nowait()[4] for _ in range(4)]
    assert phases == ["head", "head", "head", "tail"]


def test_episode_fingerprint_identity_is_unique(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'unique.db'}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        db.add(
            EpisodeFingerprint(
                imdb_id="ttunique",
                season=1,
                episode=1,
                fingerprint={},
                duration_sec=0,
                status="pending",
            )
        )
        db.commit()
        db.add(
            EpisodeFingerprint(
                imdb_id="ttunique",
                season=1,
                episode=1,
                fingerprint={},
                duration_sec=0,
                status="pending",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
    Base.metadata.drop_all(bind=engine)


def test_segments_response_fingerprint_status_transitions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'status.db'}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        unavailable = appmain._build_segments_response(
            db, "ttstatus:1:1", "ttstatus", 1, 1
        )
        assert unavailable["fingerprint_status"] == "unavailable"

        row = EpisodeFingerprint(
            imdb_id="ttstatus",
            season=1,
            episode=1,
            fingerprint={},
            duration_sec=0,
            status="pending",
        )
        db.add(row)
        db.commit()
        pending = appmain._build_segments_response(db, "ttstatus:1:1", "ttstatus", 1, 1)
        assert pending["fingerprint_status"] == "pending"
        assert pending["retry_after_sec"] == 5

        row.fingerprint = {
            "head": [1],
            "head_point_count": 1,
            "fingerprint_version": fw.FINGERPRINT_VERSION,
        }
        row.status = "partial"
        db.commit()
        partial = appmain._build_segments_response(db, "ttstatus:1:1", "ttstatus", 1, 1)
        assert partial["fingerprint_status"] == "partial"

        row.fingerprint = {
            **row.fingerprint,
            "tail": [2],
            "tail_point_count": 1,
            "tail_visual_runs": [],
            "tail_visual_version": fw.VISUAL_FEATURE_VERSION,
            "detector_version": fw.DETECTOR_VERSION,
        }
        row.status = "done"
        db.commit()
        complete = appmain._build_segments_response(
            db, "ttstatus:1:1", "ttstatus", 1, 1
        )
        assert complete["fingerprint_status"] == "complete"
        assert "retry_after_sec" not in complete
    Base.metadata.drop_all(bind=engine)
