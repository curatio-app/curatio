# ruff: noqa: E402
"""Visual/silence credits detection and fusion (Track 4)."""

import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_blackframe.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.models import Base, SegmentMarker  # noqa: E402
import workers.fingerprint_worker as fw  # noqa: E402

SAMPLE_STDERR = """
frame= 100 fps=0 q=-0.0 size=N/A time=00:00:04.00 bitrate=N/A
[blackdetect @ 0x55] black_start:39.5 black_end:41.0 black_duration:1.5
[silencedetect @ 0x66] silence_start: 39.2
[silencedetect @ 0x66] silence_end: 42.0 | silence_duration: 2.8
[blackdetect @ 0x55] black_start:10.0 black_end:10.3 black_duration:0.3
"""


def test_parse_black_silence_offsets_to_absolute():
    parsed = fw._parse_black_silence(SAMPLE_STDERR, offset_sec=1200.0)
    assert (1239.5, 1241.0) in parsed["black"]
    assert (1210.0, 1210.3) in parsed["black"]
    assert parsed["silence"] == [(1239.2, 1242.0)]


def test_vision_anchors_require_black_and_silence_cooccurrence():
    black = [(40.0, 41.0), (10.0, 10.3)]  # only the first overlaps silence
    silence = [(39.2, 42.0)]
    anchors = fw._credits_vision_anchors(black, silence)
    assert anchors == [40.0]


def test_fuse_snaps_audio_start_to_anchor():
    audio = {"start_sec": 1444.0, "end_sec": 1495.0, "source": "chromaprint"}
    black = [(1440.0, 1441.5)]
    silence = [(1439.5, 1442.0)]
    fused, source = fw._fuse_credits_with_vision(audio, black, silence, 1500.0)
    assert source == "audio+blackframe"
    assert fused["start_sec"] == 1440.0
    assert fused["end_sec"] == 1495.0


def test_fuse_keeps_audio_when_anchor_far():
    audio = {"start_sec": 1444.0, "end_sec": 1495.0, "source": "chromaprint"}
    black = [(1400.0, 1401.0)]  # >8s away
    silence = [(1399.5, 1402.0)]
    fused, source = fw._fuse_credits_with_vision(audio, black, silence, 1500.0)
    assert source == "chromaprint"
    assert fused["start_sec"] == 1444.0


def test_fuse_vision_only_terminal_credits_capped():
    black = [(1450.0, 1451.0)]
    silence = [(1449.5, 1452.0)]
    seg, source = fw._fuse_credits_with_vision(None, black, silence, 1500.0)
    assert source == "blackframe"
    assert seg["start_sec"] == 1450.0
    assert seg["confidence"] == fw.VISION_ONLY_CONFIDENCE_CAP


def test_detect_black_and_silence_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(fw, "ENABLE_BLACKFRAME_CREDITS", False)
    assert fw._detect_black_and_silence("url", 0, 10) == {"black": [], "silence": []}


@pytest.fixture()
def fp_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'bf.db'}")
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


def test_blackframe_marker_save_blocked_when_disabled(fp_db, monkeypatch):
    monkeypatch.setattr(fw, "ENABLE_BLACKFRAME_CREDITS", False)
    with fp_db() as db:
        saved = fw._save_auto_marker(
            db,
            "tt1",
            1,
            1,
            "credits",
            {"start_sec": 1450, "end_sec": 1500, "source": "blackframe"},
            0.55,
        )
        assert saved == 0
        assert db.query(SegmentMarker).count() == 0
