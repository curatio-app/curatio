# ruff: noqa: E402
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_fingerprint.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.models import (
    Base,
    EpisodeFingerprint,
    SegmentMarker,
)  # noqa: E402
import workers.fingerprint_worker as fw  # noqa: E402


@pytest.fixture()
def fp_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'fp.db'}")
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


def test_disabled_feature_extraction_process_job_does_not_call_extractor(
    fp_db, monkeypatch
):
    monkeypatch.setattr(fw, "ENABLE_AUDIO_FEATURE_EXTRACTION", False)
    monkeypatch.setattr(fw, "_probe_duration", lambda url: 60.0)
    monkeypatch.setattr(
        fw, "_extract_chromaprint", lambda *args: ([1, 2, 3], "ffmpeg_chromaprint_raw")
    )
    monkeypatch.setattr(fw, "_try_learn_season", lambda *args: None)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("audio feature extraction should be gated off")

    monkeypatch.setattr(fw, "_extract_audio_features", fail_if_called)
    fw._process_job("tt1234567:1:1", "https://cdn.example/episode.mkv")

    with fp_db() as db:
        row = db.query(EpisodeFingerprint).one()
        assert row.status == "partial"
        assert row.fingerprint["head_features"] == []
        assert "tail_features" not in row.fingerprint
        assert row.fingerprint["phases"]["head"]["status"] == "done"


def test_disabled_feature_extraction_emits_no_invalid_time_warning(monkeypatch):
    monkeypatch.setattr(fw, "ENABLE_AUDIO_FEATURE_EXTRACTION", False)
    with patch.object(fw.logger, "warning") as warning:
        assert fw._extract_audio_features("url", 0, 10) == []
    warning.assert_not_called()


def test_invalid_feature_timestamps_return_empty_when_enabled(monkeypatch):
    monkeypatch.setattr(fw, "ENABLE_AUDIO_FEATURE_EXTRACTION", True)
    output = "\n".join("lavfi.astats.Overall.RMS_level=-30" for _ in range(20))
    monkeypatch.setattr(fw.subprocess, "check_output", lambda *args, **kwargs: output)
    with patch.object(fw.logger, "warning") as warning:
        assert fw._extract_audio_features("url", 0, 5) == []
    assert any(
        "invalid_time_units" in str(call.args) for call in warning.call_args_list
    )


def test_feature_failure_does_not_fail_fingerprint_job_when_enabled(fp_db, monkeypatch):
    monkeypatch.setattr(fw, "ENABLE_AUDIO_FEATURE_EXTRACTION", True)
    monkeypatch.setattr(fw, "_probe_duration", lambda url: 60.0)
    monkeypatch.setattr(
        fw, "_extract_chromaprint", lambda *args: ([1, 2, 3], "ffmpeg_chromaprint_raw")
    )
    monkeypatch.setattr(fw, "_extract_audio_features", lambda *args: [])
    monkeypatch.setattr(fw, "_try_learn_season", lambda *args: None)
    fw._process_job("tt1234567:1:2", "https://cdn.example/episode2.mkv")
    with fp_db() as db:
        row = db.query(EpisodeFingerprint).one()
        assert row.status == "partial"
        assert row.fingerprint["head_features"] == []


def test_waveform_fallback_save_blocked(fp_db, monkeypatch):
    monkeypatch.setattr(fw, "ENABLE_WAVEFORM_FALLBACK", False)
    with fp_db() as db:
        saved = fw._save_auto_marker(
            db,
            "tt1234567",
            1,
            1,
            "intro",
            {"start_sec": 10, "end_sec": 50, "source": "waveform_fallback"},
            0.65,
        )
        assert saved == 0
        assert db.query(SegmentMarker).count() == 0


def test_prelearning_selection_prefers_nearest_episodes():
    episode_ids = [f"tt1234567:1:{i}" for i in range(1, 9)]
    selected = fw._selected_prelearning_candidates(
        episode_ids, current_episode=5, mode="conservative"
    )
    assert selected[:4] == [
        "tt1234567:1:6",
        "tt1234567:1:4",
        "tt1234567:1:7",
        "tt1234567:1:3",
    ]


def test_queue_prelearning_skips_done_pending_and_caps(fp_db, monkeypatch):
    monkeypatch.setattr(fw, "SEASON_PRELEARNING_COOLDOWN_MINUTES", 0)
    monkeypatch.setattr(
        fw,
        "get_fingerprint_runtime_config",
        lambda: {
            "prelearning_enabled": True,
            "prelearning_mode": "conservative",
            "max_additional_episodes": 2,
            "max_queued_per_season": 2,
            "cooldown_minutes": 0,
        },
    )
    monkeypatch.setattr(
        fw,
        "_get_same_season_episode_ids",
        lambda *args, **kwargs: [f"tt1234567:1:{i}" for i in range(1, 7)],
    )
    monkeypatch.setattr(
        fw, "_resolve_prefetch_stream_url", lambda user_key, cid: f"https://cdn/{cid}"
    )
    queued = []
    monkeypatch.setattr(
        fw,
        "queue_fingerprint_job",
        lambda cid, url, priority=fw.PRIORITY_BACKGROUND: queued.append(cid),
    )
    with fp_db() as db:
        db.add(
            EpisodeFingerprint(
                imdb_id="tt1234567",
                season=1,
                episode=4,
                fingerprint={
                    "detector_version": fw.DETECTOR_VERSION,
                    "fingerprint_version": fw.FINGERPRINT_VERSION,
                    "extraction_mode": "ffmpeg_chromaprint_raw",
                    "head": [1] * 10,
                    "tail": [2] * 10,
                    "head_point_count": 10,
                    "tail_point_count": 10,
                    "tail_visual_version": fw.VISUAL_FEATURE_VERSION,
                    "tail_visual_runs": [],
                },
                duration_sec=60,
                status="done",
            )
        )
        db.add(
            EpisodeFingerprint(
                imdb_id="tt1234567",
                season=1,
                episode=6,
                fingerprint={},
                duration_sec=0,
                status="pending",
            )
        )
        db.commit()
    result = fw.queue_season_prelearning("user", "tt1234567", 1, 5)
    assert result["skipped_done"] == 1
    assert result["skipped_pending"] == 1
    assert result["queued"] == 1
    assert queued == ["tt1234567:1:3"]


def test_queue_prelearning_does_not_queue_movies():
    result = fw.queue_season_prelearning("user", "movie123", 1, 1)
    assert result["queued"] == 0


def test_aggressive_prelearning_resolves_without_checked_out_connection(
    fp_db, monkeypatch
):
    engine = fp_db.kw["bind"]
    observed = []
    queued = []
    monkeypatch.setattr(
        fw,
        "get_fingerprint_runtime_config",
        lambda: {
            "prelearning_enabled": True,
            "prelearning_mode": "aggressive",
            "max_additional_episodes": 8,
            "max_queued_per_season": 8,
            "cooldown_minutes": 0,
        },
    )
    monkeypatch.setattr(
        fw,
        "_get_same_season_episode_ids",
        lambda *args, **kwargs: [f"ttaggressive:1:{i}" for i in range(1, 10)],
    )

    def resolve(_user_key, candidate_id):
        observed.append(engine.pool.checkedout())
        return f"https://cdn/{candidate_id}"

    monkeypatch.setattr(fw, "_resolve_prefetch_stream_url", resolve)
    monkeypatch.setattr(
        fw,
        "queue_fingerprint_job",
        lambda cid, url, priority=fw.PRIORITY_BACKGROUND: queued.append(cid) or True,
    )

    result = fw.queue_season_prelearning(
        "user", "ttaggressive", 1, 5, mode="aggressive"
    )

    assert result["queued"] == 8
    assert len(queued) == 8
    assert observed == [0] * 8


def _fp_with_run(total, run_start, run_values, noise_seed):
    import random

    rng = random.Random(noise_seed)
    points = [rng.getrandbits(32) for _ in range(total)]
    for offset, value in enumerate(run_values):
        points[run_start + offset] = value
    return points


def test_robust_shift_discovery_finds_hamming_noisy_run():
    import random

    rng = random.Random(7)
    run_len = fw.seconds_to_point(40)
    base_run = [rng.getrandbits(32) for _ in range(run_len)]
    # Episode B's shared run flips a HIGH bit in 40% of points: huge integer delta (legacy
    # ±2 probe cannot align it) but Hamming distance 1, so the matcher still accepts it.
    run_b = [(v ^ (1 << 30)) if (i % 5 < 2) else v for i, v in enumerate(base_run)]
    total = fw.seconds_to_point(600)
    a = _fp_with_run(total, fw.seconds_to_point(50), base_run, 101)
    b = _fp_with_run(total, fw.seconds_to_point(70), run_b, 202)
    expected_shift = fw.seconds_to_point(70) - fw.seconds_to_point(50)

    shifts = fw._discover_candidate_shifts_robust(
        a, b, fw.MAX_POINT_BIT_DIFF, fw.seconds_to_point(300)
    )
    assert expected_shift in shifts

    ranges = fw._find_chromaprint_ranges(
        a,
        b,
        label="test",
        min_duration_sec=fw.SHORT_INTRO_FALLBACK_MIN_DURATION,
        max_duration_sec=fw.MAX_INTRO_DURATION,
        max_point_bit_diff=fw.MAX_POINT_BIT_DIFF,
    )
    assert ranges
    best = max(ranges, key=lambda r: r["duration_sec_a"])
    assert abs(best["start_sec_a"] - 50.0) < 1.0
    assert best["duration_sec_a"] >= 35.0


def test_inverted_multi_index_drops_non_discriminative_values():
    points = [7] * 50 + [11, 22, 33]
    index = fw._build_inverted_multi_index(points, max_freq=16)
    assert 7 not in index  # repeated 50x -> dropped
    assert index[11] == [50]
    assert index[33] == [52]


def test_candidate_quality_clean_vs_noisy():
    clean = [
        {"pair_key": "1x2", "avg_hamming_distance": 0.0, "duration_sec": 60.0},
        {"pair_key": "1x3", "avg_hamming_distance": 0.1, "duration_sec": 60.0},
    ]
    hq, da = fw._candidate_quality(clean, "intro")
    assert hq > 0.95 and da > 0.95

    noisy = [
        {"pair_key": "1x2", "avg_hamming_distance": 6.0, "duration_sec": 20.0},
        {"pair_key": "1x3", "avg_hamming_distance": 6.0, "duration_sec": 50.0},
    ]
    hq2, da2 = fw._candidate_quality(noisy, "intro")
    assert hq2 < 0.2  # at the bit-diff ceiling
    assert da2 < da  # wide duration spread -> lower agreement


def test_confidence_is_quality_aware_and_clamped():
    pairs = [(1, 2), (1, 3), (2, 3)]
    clean = [
        {"pair_key": "1x2", "avg_hamming_distance": 0.0, "duration_sec": 60.0},
        {"pair_key": "1x3", "avg_hamming_distance": 0.0, "duration_sec": 60.0},
    ]
    noisy = [
        {"pair_key": "1x2", "avg_hamming_distance": 5.0, "duration_sec": 60.0},
        {"pair_key": "1x3", "avg_hamming_distance": 5.0, "duration_sec": 60.0},
    ]
    _, _, c_clean = fw._candidate_confidence(clean, pairs, 1, "intro")
    _, _, c_noisy = fw._candidate_confidence(noisy, pairs, 1, "intro")
    assert c_clean == 1.0  # fully supported, pristine, consistent
    assert c_noisy < c_clean
    assert 0.0 <= c_noisy <= 1.0


def test_reliable_intro_markers_skip_conservative_prelearning(fp_db, monkeypatch):
    monkeypatch.setattr(fw, "SEASON_PRELEARNING_COOLDOWN_MINUTES", 0)
    with fp_db() as db:
        db.add(
            SegmentMarker(
                imdb_id="tt1234567",
                season=1,
                episode=1,
                segment_type="intro",
                start_sec=1,
                end_sec=30,
                confidence=0.95,
            )
        )
        db.add(
            SegmentMarker(
                imdb_id="tt1234567",
                season=1,
                episode=2,
                segment_type="intro",
                start_sec=1,
                end_sec=30,
                confidence=0.95,
            )
        )
        db.commit()
    with patch.object(fw, "_resolve_prefetch_stream_url") as resolver:
        result = fw.queue_season_prelearning(
            "user", "tt1234567", 1, 3, mode="conservative"
        )
    assert result["queued"] == 0
    assert result["skipped_ready"] == 1
    resolver.assert_not_called()
