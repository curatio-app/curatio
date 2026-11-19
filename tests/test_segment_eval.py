# ruff: noqa: E402
"""Offline precision/recall + boundary-error eval for intro/credits detection.

Drives the real ``_try_learn_season`` pipeline with synthetic fingerprints (no media) and
measures how well the detector recovers planted segments. Acts as a regression gate: tighten
the asserted baselines as the matcher improves.
"""

import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_segment_eval.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.models import Base, EpisodeFingerprint, SegmentMarker  # noqa: E402
import workers.fingerprint_worker as fw  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent / "fixtures" / "segments"))
import _generate as gen  # noqa: E402

IOU_MATCH_THRESHOLD = 0.5

# Regression baselines — tightened to sit just below measured performance, so a real
# regression trips them instead of hiding inside two orders of magnitude of slack.
# Measured with the current detector: intro recall/precision 1.00 mae 0.05s,
# credits recall/precision 1.00 mae 0.27s (the music-bleed case carries the error).
BASELINE_INTRO_RECALL = 1.0
BASELINE_CREDITS_RECALL = 0.9
BASELINE_BOUNDARY_MAE = 0.5
# Precision guards against spuriously-early (or otherwise wrong) credits markers, which
# surface as false positives. Previously computed but never asserted, so a too-early
# credits regression slipped through unmeasured.
BASELINE_CREDITS_PRECISION = 0.9


@pytest.fixture()
def fp_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'eval.db'}")
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


def _iou(a: list[float], b: list[float]) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    union = (a[1] - a[0]) + (b[1] - b[0]) - inter
    return inter / union if union > 0 else 0.0


def _evaluate_case(case: dict, SessionLocal) -> dict:
    rows, labels = gen.expand_case(case)
    with SessionLocal() as db:
        for row in rows:
            db.add(
                EpisodeFingerprint(
                    imdb_id=case["imdb_id"],
                    season=case["season"],
                    episode=row["episode"],
                    fingerprint=row["fingerprint"],
                    duration_sec=row["duration_sec"],
                    source_url_hash="evalhash",
                    status="done",
                )
            )
        db.commit()

    fw._try_learn_season(case["imdb_id"], case["season"])

    with SessionLocal() as db:
        markers = (
            db.query(SegmentMarker)
            .filter_by(imdb_id=case["imdb_id"], season=case["season"])
            .all()
        )
    by_key: dict[tuple[int, str], SegmentMarker] = {
        (m.episode, m.segment_type): m for m in markers
    }

    stats: dict[str, dict] = {
        "intro": {"tp": 0, "fp": 0, "labels": 0, "abs_err": []},
        "credits": {"tp": 0, "fp": 0, "labels": 0, "abs_err": []},
    }
    matched_keys: set[tuple[int, str]] = set()
    for ep, label in labels.items():
        for seg_type, interval in label.items():
            stats[seg_type]["labels"] += 1
            marker = by_key.get((int(ep), seg_type))
            if marker is None:
                continue
            pred = [marker.start_sec, marker.end_sec]
            if _iou(pred, interval) >= IOU_MATCH_THRESHOLD:
                stats[seg_type]["tp"] += 1
                stats[seg_type]["abs_err"].append(abs(pred[0] - interval[0]))
                stats[seg_type]["abs_err"].append(abs(pred[1] - interval[1]))
                matched_keys.add((int(ep), seg_type))
    for (ep, seg_type), marker in by_key.items():
        if (ep, seg_type) not in matched_keys:
            stats.setdefault(seg_type, {"tp": 0, "fp": 0, "labels": 0, "abs_err": []})
            stats[seg_type]["fp"] += 1
    return stats


def _aggregate(all_stats: list[dict]) -> dict[str, dict]:
    agg: dict[str, dict] = {}
    for stats in all_stats:
        for seg_type, s in stats.items():
            a = agg.setdefault(seg_type, {"tp": 0, "fp": 0, "labels": 0, "abs_err": []})
            a["tp"] += s["tp"]
            a["fp"] += s["fp"]
            a["labels"] += s["labels"]
            a["abs_err"].extend(s["abs_err"])
    for seg_type, a in agg.items():
        a["recall"] = a["tp"] / a["labels"] if a["labels"] else 0.0
        a["precision"] = a["tp"] / (a["tp"] + a["fp"]) if (a["tp"] + a["fp"]) else 1.0
        a["mae"] = sum(a["abs_err"]) / len(a["abs_err"]) if a["abs_err"] else 0.0
    return agg


def test_segment_detection_eval(fp_db, capsys):
    cases = gen.load_cases()
    assert cases, "no eval fixtures found"
    all_stats = [_evaluate_case(case, fp_db) for case in cases]
    agg = _aggregate(all_stats)

    with capsys.disabled():
        print("\n=== segment detection eval ===")
        for seg_type in ("intro", "credits"):
            a = agg.get(seg_type)
            if not a:
                continue
            print(
                f"{seg_type:8s} recall={a['recall']:.2f} precision={a['precision']:.2f} "
                f"boundary_mae={a['mae']:.2f}s (tp={a['tp']} fp={a['fp']} labels={a['labels']})"
            )

    assert agg["intro"]["recall"] >= BASELINE_INTRO_RECALL
    assert agg["credits"]["recall"] >= BASELINE_CREDITS_RECALL
    assert agg["credits"]["precision"] >= BASELINE_CREDITS_PRECISION
    all_err = [e for a in agg.values() for e in a["abs_err"]]
    overall_mae = sum(all_err) / len(all_err) if all_err else 0.0
    assert overall_mae <= BASELINE_BOUNDARY_MAE
