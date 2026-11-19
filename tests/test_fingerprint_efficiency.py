# ruff: noqa: E402
"""Tests for the fingerprint learning efficiency work: pair-match memoization and the
no-op early-exit guard in ``_try_learn_season``. Both must avoid recomputing matches
without changing the markers that get written.
"""

import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_fp_efficiency.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.models import Base, EpisodeFingerprint, SeasonLearningState  # noqa: E402
import workers.fingerprint_worker as fw  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent / "fixtures" / "segments"))
import _generate as gen  # noqa: E402


@pytest.fixture()
def fp_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'eff.db'}")
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
    # Each test starts with an empty pair-match cache to avoid cross-test bleed.
    with fw._pair_match_cache_lock:
        fw._pair_match_cache.clear()
    yield SessionLocal
    with fw._pair_match_cache_lock:
        fw._pair_match_cache.clear()
    Base.metadata.drop_all(bind=engine)


def _seed_basic_season(SessionLocal):
    case = {
        "imdb_id": "tt_eff",
        "season": 1,
        "head_duration_sec": 600,
        "tail_duration_sec": 600,
        "episode_duration_sec": 1500,
        "seed": 99,
        "intro": {"start": 35.0, "duration": 60.0},
        "credits": {"abs_start": 1440.0, "duration": 55.0},
        "episodes": [1, 2, 3],
    }
    rows, _labels = gen.expand_case(case)
    with SessionLocal() as db:
        for row in rows:
            db.add(
                EpisodeFingerprint(
                    imdb_id=case["imdb_id"],
                    season=case["season"],
                    episode=row["episode"],
                    fingerprint=row["fingerprint"],
                    duration_sec=row["duration_sec"],
                    source_url_hash="effhash",
                    status="done",
                )
            )
        db.commit()
    return case


def _count_calls(monkeypatch):
    calls = {"n": 0}
    original = fw._find_chromaprint_ranges

    def spy(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(fw, "_find_chromaprint_ranges", spy)
    return calls


def test_pair_matches_are_memoized_across_relearn(fp_db, monkeypatch):
    case = _seed_basic_season(fp_db)
    fw._try_learn_season(case["imdb_id"], case["season"])

    # Drop the learning state so the no-op guard cannot fire — this isolates the pair-match
    # cache as the only thing that can prevent recomputation on the second run.
    with fp_db() as db:
        db.query(SeasonLearningState).delete()
        db.commit()

    calls = _count_calls(monkeypatch)
    fw._try_learn_season(case["imdb_id"], case["season"])
    assert calls["n"] == 0, "cached pair matches must not be recomputed"


def test_noop_guard_skips_unchanged_complete_season(fp_db, monkeypatch):
    case = _seed_basic_season(fp_db)
    fw._try_learn_season(case["imdb_id"], case["season"])

    with fp_db() as db:
        state = (
            db.query(SeasonLearningState)
            .filter_by(imdb_id=case["imdb_id"], season=case["season"])
            .first()
        )
        assert (
            state is not None and state.last_reason is None
        ), "fixture should resolve cleanly for the guard to apply"

    # Empty the pair cache: now only the no-op guard can prevent matching work.
    with fw._pair_match_cache_lock:
        fw._pair_match_cache.clear()
    calls = _count_calls(monkeypatch)
    fw._try_learn_season(case["imdb_id"], case["season"])
    assert calls["n"] == 0, "unchanged complete season must skip the pairwise pass"


def test_noop_guard_reruns_when_episode_content_changes(fp_db, monkeypatch):
    case = _seed_basic_season(fp_db)
    fw._try_learn_season(case["imdb_id"], case["season"])

    # Simulate a silent re-fingerprint: same episode set, new source hash on one episode.
    with fp_db() as db:
        row = (
            db.query(EpisodeFingerprint)
            .filter_by(imdb_id=case["imdb_id"], season=case["season"], episode=2)
            .first()
        )
        row.source_url_hash = "effhash_changed"
        db.commit()

    with fw._pair_match_cache_lock:
        fw._pair_match_cache.clear()
    calls = _count_calls(monkeypatch)
    fw._try_learn_season(case["imdb_id"], case["season"])
    assert calls["n"] > 0, "a changed episode hash must force a re-learn"


def _current_fingerprint_payload():
    return {
        "head": [1, 2, 3],
        "tail": [4, 5, 6],
        "head_duration_sec": fw.HEAD_WINDOW,
        "tail_duration_sec": fw.TAIL_WINDOW,
        "head_point_count": 3,
        "tail_point_count": 3,
        "fingerprint_version": fw.FINGERPRINT_VERSION,
        "detector_version": fw.DETECTOR_VERSION,
        "tail_visual_version": fw.VISUAL_FEATURE_VERSION,
        "tail_visual_runs": [],
        "extraction_mode": "ffmpeg_chromaprint_raw",
    }


def test_ffmpeg_fingerprint_decode_is_single_threaded_by_default(monkeypatch):
    captured = {}

    def fake_check_output(command, **_kwargs):
        captured["command"] = command
        return (1).to_bytes(4, "little")

    monkeypatch.setattr(fw.subprocess, "check_output", fake_check_output)
    assert fw._extract_ffmpeg_chromaprint_raw("https://cdn.example/ep.mkv", 0, 30)

    command = captured["command"]
    input_index = command.index("-i")
    threads_index = command.index("-threads")
    assert command[threads_index + 1] == "1"
    assert threads_index < input_index, "thread limit must apply to input decoding"


def test_process_job_skips_extraction_when_already_current(fp_db, monkeypatch):
    imdb_id, season, episode = "tt_skip", 1, 1
    url = "https://cdn.example/ep1.mkv"
    source_hash = fw._source_url_hash(url)
    with fp_db() as db:
        db.add(
            EpisodeFingerprint(
                imdb_id=imdb_id,
                season=season,
                episode=episode,
                status="done",
                source_url_hash=source_hash,
                duration_sec=1500.0,
                fingerprint=_current_fingerprint_payload(),
            )
        )
        db.commit()

    def fail(*args, **kwargs):
        raise AssertionError("must not re-extract an already-current fingerprint")

    monkeypatch.setattr(fw, "_probe_duration", fail)
    monkeypatch.setattr(fw, "_extract_chromaprint", fail)
    monkeypatch.setattr(fw, "_try_learn_season", lambda *a, **k: None)

    fw._process_job(f"{imdb_id}:{season}:{episode}", url)

    with fp_db() as db:
        row = (
            db.query(EpisodeFingerprint)
            .filter_by(imdb_id=imdb_id, season=season, episode=episode)
            .one()
        )
        assert row.status == "done"
        assert row.fingerprint["head"] == [1, 2, 3]


def test_process_job_reextracts_when_source_changes(fp_db, monkeypatch):
    imdb_id, season, episode = "tt_drift", 1, 1
    url = "https://cdn.example/ep1.mkv"
    with fp_db() as db:
        db.add(
            EpisodeFingerprint(
                imdb_id=imdb_id,
                season=season,
                episode=episode,
                status="done",
                source_url_hash="stale_hash",  # differs from _source_url_hash(url)
                duration_sec=1500.0,
                fingerprint=_current_fingerprint_payload(),
            )
        )
        db.commit()

    calls = {"n": 0}

    def fake_extract(url, start, dur, label):
        calls["n"] += 1
        return ([10, 11, 12], "ffmpeg_chromaprint_raw")

    monkeypatch.setattr(fw, "_probe_duration", lambda u: 1500.0)
    monkeypatch.setattr(fw, "_extract_chromaprint", fake_extract)
    monkeypatch.setattr(fw, "_try_learn_season", lambda *a, **k: None)

    fw._process_job(f"{imdb_id}:{season}:{episode}", url)

    assert calls["n"] == 1, "the first phase re-extracts only head audio"
    with fp_db() as db:
        row = (
            db.query(EpisodeFingerprint)
            .filter_by(imdb_id=imdb_id, season=season, episode=episode)
            .one()
        )
        assert row.fingerprint["head"] == [10, 11, 12]
        assert row.status == "partial"
        assert "tail" not in row.fingerprint
        assert row.source_url_hash == fw._source_url_hash(url)


def test_process_job_reuses_trusted_analysis_when_signed_url_rotates(
    fp_db, monkeypatch
):
    imdb_id, season, episode = "tt_rotated", 1, 1
    old_url = "https://cdn.example/ep1.mkv?token=old"
    new_url = "https://cdn.example/ep1.mkv?token=new"
    payload = _current_fingerprint_payload()
    sentinel = [91, 92, 93]
    payload["media_identity"] = fw._duration_media_identity(1500.0, sentinel)
    with fp_db() as db:
        db.add(
            EpisodeFingerprint(
                imdb_id=imdb_id,
                season=season,
                episode=episode,
                status="done",
                source_url_hash=fw._source_url_hash(old_url),
                duration_sec=1500.0,
                fingerprint=payload,
            )
        )
        db.commit()

    calls = []

    def fake_extract(_url, start, duration, label):
        calls.append((start, duration, label))
        if label.endswith(":identity"):
            return sentinel, "ffmpeg_chromaprint_raw"
        raise AssertionError("compatible signed URL rotation must not re-extract")

    monkeypatch.setattr(fw, "_probe_duration", lambda _url: 1500.5)
    monkeypatch.setattr(fw, "_extract_chromaprint", fake_extract)
    monkeypatch.setattr(fw, "_try_learn_season", lambda *a, **k: None)

    fw._process_job(f"{imdb_id}:{season}:{episode}", new_url)

    with fp_db() as db:
        row = (
            db.query(EpisodeFingerprint)
            .filter_by(imdb_id=imdb_id, season=season, episode=episode)
            .one()
        )
        assert row.status == "done"
        assert row.source_url_hash == fw._source_url_hash(new_url)
        assert row.fingerprint["analysis_metrics"]["head"]["source_reused"] is True
    assert calls == [
        (
            0,
            fw.MEDIA_IDENTITY_SENTINEL_WINDOW_SEC,
            f"{imdb_id}:{season}:{episode}:identity",
        )
    ]


def test_queue_coalesces_a_newer_source_for_an_inflight_phase(monkeypatch):
    local_queue = fw.queue.PriorityQueue()
    monkeypatch.setattr(fw, "_job_queue", local_queue)
    monkeypatch.setattr(fw, "_inflight", set())
    monkeypatch.setattr(fw, "_superseding_sources", {})

    assert fw.queue_fingerprint_job("tt_coalesce:1:1", "old", phase="head")
    assert not fw.queue_fingerprint_job("tt_coalesce:1:1", "new", phase="head")
    assert local_queue.qsize() == 1
    assert fw._superseding_sources[("tt_coalesce:1:1", "head")] == "new"
