# ruff: noqa: E402
"""Community intro/credits timestamps (IntroDB, TheIntroDB) and their place in
the served segments: manual markers first, then the databases, then fingerprints."""

import os
import sys
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_intro_sources.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app import intro_sources
from app import main as appmain
from app.models import (
    Base,
    EpisodeFingerprint,
    ExternalSegmentCache,
    SegmentFeedback,
    SegmentMarker,
    User,
)
from app.time_utils import utc_now
from workers import fingerprint_worker as fw

IMDB = "tt0903747"

INTRODB_BODY = {
    "imdb_id": IMDB,
    "season": 1,
    "episode": 2,
    "intro": {
        "start_sec": 314.5,
        "end_sec": 331,
        "confidence": 1,
        "submission_count": 5,
    },
    "recap": None,
    "outro": {
        "start_sec": 2843,
        "end_sec": 2901,
        "confidence": 1,
        "submission_count": 1,
    },
    "no_intro": False,
    "no_recap": False,
    "no_outro": False,
}
THEINTRODB_BODY = {
    "type": "tv",
    "season": 1,
    "episode": 2,
    "group_duration_ms": 2901920,
    "intro": [
        {
            "start_ms": 314568,
            "end_ms": 330969,
            "confidence": 0.56,
            "submission_count": 6,
        }
    ],
    "credits": [
        {"start_ms": 2843000, "end_ms": None, "confidence": 0.1, "submission_count": 1}
    ],
}


@pytest.fixture()
def db_session():
    # One shared connection, so endpoints run on TestClient's threads see it too.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        db.add(User(id=1, user_key="u1"))
        db.commit()
        yield db
    Base.metadata.drop_all(bind=engine)


def _client(responses: dict[str, httpx.Response], calls: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        host = request.url.host
        return responses.get(host, httpx.Response(404, json={"error": "missing"}))

    return httpx.Client(transport=httpx.MockTransport(handler))


def _both(introdb=INTRODB_BODY, theintrodb=THEINTRODB_BODY):
    return {
        "api.introdb.app": httpx.Response(200, json=introdb),
        "api.theintrodb.org": httpx.Response(200, json=theintrodb),
    }


# --- parsing ---------------------------------------------------------------


def test_introdb_maps_outro_to_credits_in_seconds():
    segments = intro_sources.parse_introdb(INTRODB_BODY)
    assert [(s["type"], s["start"], s["end"]) for s in segments] == [
        ("intro", 314.5, 331.0),
        ("credits", 2843.0, 2901.0),
    ]


def test_introdb_confirmed_none_is_skipped():
    body = dict(INTRODB_BODY, no_intro=True)
    assert [s["type"] for s in intro_sources.parse_introdb(body)] == ["credits"]


def test_theintrodb_converts_ms_and_fills_open_ends():
    body = dict(
        THEINTRODB_BODY,
        intro=[{"start_ms": None, "end_ms": 22500, "submission_count": 4}],
    )
    by_type = {s["type"]: s for s in intro_sources.parse_theintrodb(body)}
    assert (by_type["intro"]["start"], by_type["intro"]["end"]) == (0.0, 22.5)
    # Credits with no end run to the episode's length.
    assert (by_type["credits"]["start"], by_type["credits"]["end"]) == (2843.0, 2901.92)


def test_theintrodb_open_credits_without_duration_are_dropped():
    body = dict(THEINTRODB_BODY, group_duration_ms=None)
    assert [s["type"] for s in intro_sources.parse_theintrodb(body)] == ["intro"]


def test_theintrodb_picks_the_most_submitted_entry():
    body = dict(
        THEINTRODB_BODY,
        intro=[
            {"start_ms": 10000, "end_ms": 40000, "submission_count": 1},
            {"start_ms": 12000, "end_ms": 42000, "submission_count": 9},
        ],
    )
    intro = intro_sources.parse_theintrodb(body)[0]
    assert (intro["start"], intro["submission_count"]) == (12.0, 9)


@pytest.mark.parametrize(
    "seg_type,start,end,ok",
    [
        ("intro", 0, 4, False),  # shorter than 5 s
        ("intro", 0, 301, False),  # longer than 300 s
        ("intro", 901, 950, False),  # starts after 15 min
        ("intro", 60, 120, True),
        ("credits", 2000, 2004, False),
        ("credits", 2000, 2901, False),  # longer than 900 s
        ("credits", 2000, 2600, True),
    ],
)
def test_implausible_times_are_dropped(seg_type, start, end, ok):
    assert intro_sources._plausible(seg_type, start, end) is ok


def test_most_submissions_wins_and_introdb_wins_a_tie():
    a = {
        "type": "intro",
        "start": 1,
        "end": 30,
        "submission_count": 3,
        "source": "introdb",
    }
    b = {
        "type": "intro",
        "start": 2,
        "end": 31,
        "submission_count": 6,
        "source": "theintrodb",
    }
    assert intro_sources.best_by_type([a, b])["intro"] is b
    c = dict(b, submission_count=3)
    assert intro_sources.best_by_type([c, a])["intro"] is a


# --- lookup and cache ------------------------------------------------------


def test_lookup_fetches_both_then_serves_from_cache(db_session):
    calls: list = []
    segments, cached = intro_sources.lookup(
        db_session, IMDB, 1, 2, client=_client(_both(), calls)
    )
    assert not cached and len(calls) == 2
    assert {s["source"] for s in segments} == {"introdb", "theintrodb"}

    calls.clear()
    again, cached = intro_sources.lookup(
        db_session, IMDB, 1, 2, client=_client(_both(), calls)
    )
    assert cached and calls == []
    assert again == segments


def test_lookup_skips_ids_the_databases_cannot_answer(db_session):
    calls: list = []
    assert intro_sources.lookup(
        db_session, "ttphase", 1, 1, client=_client(_both(), calls)
    ) == (
        [],
        True,
    )
    assert calls == []


def test_lookup_skips_switched_off_sources(db_session, monkeypatch):
    monkeypatch.setattr(intro_sources.settings, "introdb_enabled", False)
    calls: list = []
    segments, _ = intro_sources.lookup(
        db_session, IMDB, 1, 2, client=_client(_both(), calls)
    )
    assert [r.url.host for r in calls] == ["api.theintrodb.org"]
    assert {s["source"] for s in segments} == {"theintrodb"}

    monkeypatch.setattr(intro_sources.settings, "theintrodb_enabled", False)
    calls.clear()
    assert intro_sources.lookup(
        db_session, IMDB, 1, 3, client=_client(_both(), calls)
    ) == ([], True)
    assert calls == []


@pytest.mark.parametrize(
    "response,status",
    [
        (httpx.Response(404, json={"code": "no_submissions"}), "none"),
        (httpx.Response(429, json={"error": "slow down"}), "error"),
        (httpx.Response(500, text="boom"), "error"),
        (httpx.Response(200, text="not json"), "error"),
    ],
)
def test_misses_and_errors_are_cached_with_their_status(db_session, response, status):
    intro_sources.lookup(
        db_session,
        IMDB,
        1,
        2,
        client=_client({"api.introdb.app": response, "api.theintrodb.org": response}),
    )
    rows = db_session.query(ExternalSegmentCache).all()
    assert {r.status for r in rows} == {status}


def test_timeouts_are_errors(db_session):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    segments, _ = intro_sources.lookup(
        db_session,
        IMDB,
        1,
        2,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert segments == []
    assert {r.status for r in db_session.query(ExternalSegmentCache).all()} == {"error"}


@pytest.mark.parametrize(
    "status,age,refetch",
    [
        ("hit", timedelta(days=6), False),
        ("hit", timedelta(days=8), True),
        ("none", timedelta(hours=23), False),
        ("none", timedelta(hours=25), True),
        ("error", timedelta(minutes=14), False),
        ("error", timedelta(minutes=16), True),
    ],
)
def test_cache_lifetimes(db_session, status, age, refetch):
    for source in intro_sources.SOURCES:
        db_session.add(
            ExternalSegmentCache(
                imdb_id=IMDB,
                season=1,
                episode=2,
                source=source,
                status=status,
                segments=[],
                fetched_at=utc_now() - age,
            )
        )
    db_session.commit()
    calls: list = []
    intro_sources.lookup(db_session, IMDB, 1, 2, client=_client(_both(), calls))
    assert bool(calls) is refetch


def test_theintrodb_key_is_sent_as_a_header(db_session, monkeypatch):
    monkeypatch.setattr(intro_sources.settings, "theintrodb_api_key", "secret-key")
    calls: list = []
    intro_sources.lookup(db_session, IMDB, 1, 2, client=_client(_both(), calls))
    theintrodb = next(r for r in calls if r.url.host == "api.theintrodb.org")
    assert theintrodb.headers["Authorization"] == "Bearer secret-key"
    assert "secret-key" not in str(theintrodb.url)


# --- served response -------------------------------------------------------


@pytest.fixture()
def fake_sources(monkeypatch):
    answers: dict[str, tuple[str, list]] = {}

    def fake_fetch(source, imdb_id, season, episode, client):
        return answers.get(source, ("none", []))

    monkeypatch.setattr(intro_sources, "_fetch", fake_fetch)
    return answers


def _marker(db, **kw):
    values = dict(
        imdb_id=IMDB,
        season=1,
        episode=2,
        segment_type="intro",
        start_sec=10.0,
        end_sec=70.0,
        confidence=0.9,
        source="chromaprint",
        fingerprint_version=fw.MARKER_FINGERPRINT_VERSION,
        detector_version=fw.DETECTOR_VERSION,
        is_user_provided=False,
    )
    values.update(kw)
    db.add(SegmentMarker(**values))
    db.commit()


def _response(db):
    return appmain._build_segments_response(db, f"{IMDB}:1:2", IMDB, 1, 2)


def test_community_answer_makes_an_unfingerprinted_episode_ready(
    db_session, fake_sources
):
    fake_sources["introdb"] = ("hit", intro_sources.parse_introdb(INTRODB_BODY))
    response = _response(db_session)
    assert response["status"] == "ready"
    intro = response["segments"][0]
    assert intro == {
        "type": "intro",
        "start": 314.5,
        "end": 331.0,
        "confidence": 1.0,
        "boundary_status": "confirmed",
        "detector_version": None,
        "source": "introdb",
    }
    assert [s["type"] for s in response["segments"]] == ["intro", "credits"]


def test_nothing_known_anywhere_is_still_not_found(db_session, fake_sources):
    response = _response(db_session)
    assert response["status"] == "not_found" and response["segments"] == []


def test_community_replaces_a_fingerprint_marker(db_session, fake_sources):
    _marker(db_session)
    fake_sources["theintrodb"] = (
        "hit",
        intro_sources.parse_theintrodb(THEINTRODB_BODY),
    )
    intro = _response(db_session)["segments"][0]
    assert (intro["source"], intro["start"]) == ("theintrodb", 314.568)


def test_fingerprint_marker_still_serves_when_databases_have_nothing(
    db_session, fake_sources
):
    _marker(db_session)
    intro = _response(db_session)["segments"][0]
    assert (intro["source"], intro["start"]) == ("chromaprint", 10.0)


def test_manual_marker_beats_the_databases(db_session, fake_sources):
    _marker(
        db_session, source="manual", is_user_provided=True, start_sec=5.0, end_sec=50.0
    )
    fake_sources["introdb"] = ("hit", intro_sources.parse_introdb(INTRODB_BODY))
    intro = _response(db_session)["segments"][0]
    assert (intro["source"], intro["start"]) == ("manual", 5.0)


def test_wrong_timing_feedback_suppresses_a_community_intro(db_session, fake_sources):
    fake_sources["introdb"] = ("hit", intro_sources.parse_introdb(INTRODB_BODY))
    db_session.add(User(id=2, user_key="u2"))
    db_session.commit()
    for user_id in (1, 2):
        db_session.add(
            SegmentFeedback(
                user_id=user_id,
                imdb_id=IMDB,
                season=1,
                episode=2,
                segment_type="intro",
                feedback="wrong",
            )
        )
    db_session.commit()
    types = [s["type"] for s in _response(db_session)["segments"]]
    assert "intro" not in types and "credits" in types


def test_user_adjustment_moves_a_community_intro(db_session, fake_sources):
    fake_sources["introdb"] = ("hit", intro_sources.parse_introdb(INTRODB_BODY))
    db_session.add(
        SegmentFeedback(
            user_id=1,
            imdb_id=IMDB,
            season=1,
            episode=2,
            segment_type="intro",
            feedback="adjusted",
            adjusted_start=300.0,
            adjusted_end=330.0,
        )
    )
    db_session.commit()
    intro = _response(db_session)["segments"][0]
    assert (intro["start"], intro["end"]) == (300.0, 330.0)


# --- admin switches ----------------------------------------------------------


def test_switching_off_intro_or_credits_removes_that_type(
    db_session, fake_sources, monkeypatch
):
    fake_sources["introdb"] = ("hit", intro_sources.parse_introdb(INTRODB_BODY))
    monkeypatch.setattr(appmain.settings, "skip_intro_enabled", False)
    assert [s["type"] for s in _response(db_session)["segments"]] == ["credits"]

    monkeypatch.setattr(appmain.settings, "skip_intro_enabled", True)
    monkeypatch.setattr(appmain.settings, "skip_credits_enabled", False)
    assert [s["type"] for s in _response(db_session)["segments"]] == ["intro"]


def test_both_types_off_reports_nothing_found(db_session, fake_sources, monkeypatch):
    fake_sources["introdb"] = ("hit", intro_sources.parse_introdb(INTRODB_BODY))
    monkeypatch.setattr(appmain.settings, "skip_intro_enabled", False)
    monkeypatch.setattr(appmain.settings, "skip_credits_enabled", False)
    response = _response(db_session)
    assert response["segments"] == [] and response["status"] == "not_found"


def test_fingerprint_switch_off_hides_learned_markers_and_progress(
    db_session, fake_sources, monkeypatch
):
    _marker(db_session)
    db_session.add(
        EpisodeFingerprint(
            imdb_id=IMDB,
            season=1,
            episode=2,
            fingerprint={},
            duration_sec=2900.0,
            status="pending",
        )
    )
    db_session.commit()
    monkeypatch.setattr(appmain.settings, "fingerprint_segments_enabled", False)
    response = _response(db_session)
    # No learned intro, and no "pending" that would make the apps retry.
    assert response["segments"] == []
    assert response["status"] == "not_found"


def test_fingerprint_switch_off_keeps_hand_set_markers(
    db_session, fake_sources, monkeypatch
):
    _marker(
        db_session, source="manual", is_user_provided=True, start_sec=5.0, end_sec=50.0
    )
    monkeypatch.setattr(appmain.settings, "fingerprint_segments_enabled", False)
    intro = _response(db_session)["segments"][0]
    assert (intro["source"], intro["start"]) == ("manual", 5.0)


def test_fingerprint_switch_off_queues_no_jobs(db_session, monkeypatch):
    queued: list = []
    monkeypatch.setattr(fw, "queue_fingerprint_job", lambda *a, **k: queued.append(a))
    monkeypatch.setattr(
        fw, "queue_stream_open_fingerprints", lambda *a, **k: queued.append(a)
    )
    monkeypatch.setattr(appmain.settings, "fingerprint_segments_enabled", False)
    appmain._trigger_episode_fingerprint(
        db_session, "u1", IMDB, 1, 2, "https://example.test/video.mkv"
    )
    assert queued == []


def test_admin_settings_read_and_save_the_switches(db_session, monkeypatch):
    from fastapi.testclient import TestClient

    import app.admin as admin_module
    from app.admin import verify_admin

    class _Ctx:
        def __enter__(self):
            return db_session

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(admin_module, "get_db", lambda: _Ctx())
    # Saving writes onto the live settings object; this restores it afterwards.
    monkeypatch.setattr(appmain.settings, "skip_intro_enabled", True)
    monkeypatch.setattr(appmain.settings, "theintrodb_enabled", True)
    appmain.app.dependency_overrides[verify_admin] = lambda: True
    try:
        client = TestClient(appmain.app)
        segments = client.get("/admin/api/settings").json()["segments"]
        assert segments == {
            "SKIP_INTRO_ENABLED": True,
            "SKIP_CREDITS_ENABLED": True,
            "INTRODB_ENABLED": True,
            "THEINTRODB_ENABLED": True,
            "FINGERPRINT_SEGMENTS_ENABLED": True,
        }

        saved = client.post(
            "/admin/api/settings",
            json={"SKIP_INTRO_ENABLED": False, "THEINTRODB_ENABLED": False},
        )
        assert saved.status_code == 200
        assert appmain.settings.skip_intro_enabled is False
        assert appmain.settings.theintrodb_enabled is False
        segments = client.get("/admin/api/settings").json()["segments"]
        assert segments["SKIP_INTRO_ENABLED"] is False
        assert segments["INTRODB_ENABLED"] is True
    finally:
        appmain.app.dependency_overrides.clear()


def test_admin_fingerprint_status_counts_community_lookups(db_session, monkeypatch):
    from fastapi.testclient import TestClient

    import app.admin as admin_module
    from app.admin import verify_admin

    class _Ctx:
        def __enter__(self):
            return db_session

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(admin_module, "get_db", lambda: _Ctx())
    for episode, source, status in (
        (1, "introdb", "hit"),
        (2, "introdb", "hit"),
        (1, "theintrodb", "none"),
        (2, "theintrodb", "error"),
    ):
        db_session.add(
            ExternalSegmentCache(
                imdb_id=IMDB,
                season=1,
                episode=episode,
                source=source,
                status=status,
                segments=[],
            )
        )
    db_session.commit()
    appmain.app.dependency_overrides[verify_admin] = lambda: True
    try:
        body = TestClient(appmain.app).get("/admin/api/fingerprint-status").json()
    finally:
        appmain.app.dependency_overrides.clear()
    assert body["community_lookups"] == {
        "introdb": {"hit": 2},
        "theintrodb": {"none": 1, "error": 1},
    }
