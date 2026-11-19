# ruff: noqa: E402
"""Tests for the backend source-preflight probe + verified-source fingerprinting.

The probe Range-checks candidate sources server-side (so a remote host's CORS can't
false-fail playback) and, when episode context is supplied, hands the first
*online* source to the intro/credits fingerprint worker — never a dead one.
"""

import os
from pathlib import Path
import sys
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_playback_probe.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.auth import create_user_session
from app.database import get_db_dependency
from app.main import app
from app.models import Account, Base, EpisodeFingerprint, PasskeyCredential, User

engine = create_engine(
    "sqlite:///./test_playback_probe.db", connect_args={"check_same_thread": False}
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

PASSED = {"state": "passed", "reason": None, "status": 206}


def _fail(reason, status=None):
    return {"state": "failed", "reason": reason, "status": status}


def make_client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()

    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db_dependency] = override_get_db
    return TestClient(app), db


def cleanup(db):
    db.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def create_logged_in_user(client, db):
    account = Account(
        auth_source="local",
        email="viewer@example.com",
        password_hash="not-used",
        is_active=True,
        totp_enabled=True,
    )
    db.add(account)
    db.commit()
    # A finished passkey + TOTP enrollment, so the session clears the gate.
    db.add(
        PasskeyCredential(
            account_id=account.id,
            credential_id="test-credential",
            public_key="test-public-key",
        )
    )
    db.commit()
    user = User(
        user_key="web-user-key",
        account_id=account.id,
        auth_source="local",
        display_name="Viewer",
        is_active=True,
    )
    db.add(user)
    db.commit()
    token = create_user_session(account.id, db, active_profile_id=user.id)
    client.cookies.set("user_session", token)
    return user


def _scripted_probe(script):
    """Replacement for app.main._probe_one returning a canned verdict per URL."""

    async def fake(_client, url):
        return script[url]

    return fake


def test_probe_maps_statuses_and_keeps_origin_reachable_when_any_http_responds():
    client, db = make_client()
    try:
        create_logged_in_user(client, db)
        urls = [
            "http://cdn.example.com/ok.mp4",
            "http://cdn.example.com/missing.mp4",
            "http://cdn.example.com/slow.mp4",
            "http://cdn.example.com/down.mp4",
        ]
        script = {
            urls[0]: PASSED,
            urls[1]: _fail("http_error", 404),
            urls[2]: _fail("timeout"),
            urls[3]: _fail("network"),
        }
        with patch("app.main._probe_one", new=_scripted_probe(script)):
            resp = client.post("/account/api/playback/probe", json={"urls": urls})
        assert resp.status_code == 200
        body = resp.json()
        assert body["results"][urls[0]]["state"] == "passed"
        assert body["results"][urls[1]] == {
            "state": "failed",
            "reason": "http_error",
            "status": 404,
        }
        assert body["results"][urls[2]]["reason"] == "timeout"
        # Two network-level failures, but a 404 proves the origin answered HTTP.
        assert body["originLooksUnreachable"] is False
    finally:
        cleanup(db)


def test_probe_flags_origin_unreachable_when_all_network_level():
    client, db = make_client()
    try:
        create_logged_in_user(client, db)
        urls = ["http://cdn.example.com/a.mp4", "http://cdn.example.com/b.mp4"]
        script = {urls[0]: _fail("network"), urls[1]: _fail("network")}
        with patch("app.main._probe_one", new=_scripted_probe(script)):
            resp = client.post("/account/api/playback/probe", json={"urls": urls})
        assert resp.status_code == 200
        assert resp.json()["originLooksUnreachable"] is True
    finally:
        cleanup(db)


def test_probe_feeds_first_passed_source_to_fingerprint_worker():
    client, db = make_client()
    try:
        create_logged_in_user(client, db)
        from workers.fingerprint_worker import PRIORITY_STREAM_OPEN

        urls = ["http://cdn.example.com/dead.mp4", "http://cdn.example.com/live.mp4"]
        script = {urls[0]: _fail("network"), urls[1]: PASSED}
        with (
            patch("app.main._probe_one", new=_scripted_probe(script)),
            patch("workers.fingerprint_worker.queue_fingerprint_job") as qfj,
            patch("workers.fingerprint_worker.queue_stream_open_fingerprints"),
        ):
            resp = client.post(
                "/account/api/playback/probe",
                json={"urls": urls, "imdb_id": "tt123", "season": 1, "episode": 2},
            )
        assert resp.status_code == 200
        # Fingerprinted the first *online* source, not the dead streams[0].
        qfj.assert_called_once_with("tt123:1:2", urls[1], PRIORITY_STREAM_OPEN)
        row = (
            db.query(EpisodeFingerprint)
            .filter_by(imdb_id="tt123", season=1, episode=2)
            .first()
        )
        assert row is not None and row.status == "pending"
    finally:
        cleanup(db)


def test_probe_never_fingerprints_when_all_sources_fail():
    client, db = make_client()
    try:
        create_logged_in_user(client, db)
        urls = ["http://cdn.example.com/x.mp4", "http://cdn.example.com/y.mp4"]
        script = {urls[0]: _fail("http_error", 500), urls[1]: _fail("network")}
        with (
            patch("app.main._probe_one", new=_scripted_probe(script)),
            patch("workers.fingerprint_worker.queue_fingerprint_job") as qfj,
            patch("workers.fingerprint_worker.queue_stream_open_fingerprints"),
        ):
            resp = client.post(
                "/account/api/playback/probe",
                json={"urls": urls, "imdb_id": "tt123", "season": 1, "episode": 2},
            )
        assert resp.status_code == 200
        qfj.assert_not_called()
    finally:
        cleanup(db)


def test_probe_rejects_unsafe_url_without_request():
    client, db = make_client()
    try:
        create_logged_in_user(client, db)
        # Real _probe_one: an SSRF target is rejected before any HTTP request.
        with (
            patch("workers.fingerprint_worker.queue_fingerprint_job") as qfj,
            patch("workers.fingerprint_worker.queue_stream_open_fingerprints"),
        ):
            resp = client.post(
                "/account/api/playback/probe",
                json={
                    "urls": ["http://127.0.0.1/secret"],
                    "imdb_id": "tt9",
                    "season": 1,
                    "episode": 1,
                },
            )
        assert resp.status_code == 200
        assert (
            resp.json()["results"]["http://127.0.0.1/secret"]["reason"] == "unsafe_url"
        )
        qfj.assert_not_called()
    finally:
        cleanup(db)


def test_probe_requires_authentication():
    client, db = make_client()
    try:
        resp = client.post("/account/api/playback/probe", json={"urls": []})
        assert resp.status_code == 401
    finally:
        cleanup(db)


def test_confirm_source_queues_played_url():
    client, db = make_client()
    try:
        create_logged_in_user(client, db)
        from workers.fingerprint_worker import PRIORITY_STREAM_OPEN

        url = "http://cdn.example.com/played.mp4"
        with (
            patch("workers.fingerprint_worker.queue_fingerprint_job") as qfj,
            patch("workers.fingerprint_worker.queue_stream_open_fingerprints"),
            # The example host doesn't resolve, and unresolvable hosts are refused.
            patch("app.main.is_safe_source_url", return_value=True),
        ):
            resp = client.post(
                "/account/api/playback/confirm-source",
                json={"url": url, "imdb_id": "tt123", "season": 1, "episode": 2},
            )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        qfj.assert_called_once_with("tt123:1:2", url, PRIORITY_STREAM_OPEN)
    finally:
        cleanup(db)


def test_confirm_source_rejects_unsafe_url():
    client, db = make_client()
    try:
        create_logged_in_user(client, db)
        with (
            patch("workers.fingerprint_worker.queue_fingerprint_job") as qfj,
            patch("workers.fingerprint_worker.queue_stream_open_fingerprints"),
        ):
            resp = client.post(
                "/account/api/playback/confirm-source",
                json={
                    "url": "http://127.0.0.1/secret",
                    "imdb_id": "tt1",
                    "season": 1,
                    "episode": 1,
                },
            )
        assert resp.status_code == 400
        qfj.assert_not_called()
    finally:
        cleanup(db)


def test_timeouts_do_not_establish_an_origin_outage():
    client, db = make_client()
    try:
        create_logged_in_user(client, db)
        urls = ["https://cdn.example/a", "https://cdn.example/b"]
        script = {
            url: {"state": "indeterminate", "reason": "timeout", "status": None}
            for url in urls
        }
        with patch("app.main._probe_one", new=_scripted_probe(script)):
            response = client.post("/account/api/playback/probe", json={"urls": urls})
        assert response.json()["originLooksUnreachable"] is False
    finally:
        cleanup(db)


@__import__("pytest").mark.asyncio
async def test_probe_timeout_is_indeterminate(monkeypatch):
    import httpx
    import app.main as main
    from unittest.mock import AsyncMock

    monkeypatch.setattr(main, "_is_safe_source_url_async", AsyncMock(return_value=True))
    monkeypatch.setattr(
        main,
        "_send_with_validated_redirects",
        AsyncMock(side_effect=httpx.ReadTimeout("slow resolver")),
    )
    async with httpx.AsyncClient() as client:
        result = await main._probe_one(client, "https://aio.example/play")
    assert result == {"state": "indeterminate", "reason": "timeout", "status": None}
