# ruff: noqa: E402
"""Tests for multi-profile management: native (user_key) API + backward compat."""

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_profiles_api.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.auth import create_user_session  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import get_db_dependency  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    Base,
    PasskeyCredential,
    User,
    WatchEvent,
)

engine = create_engine(
    "sqlite:///./test_profiles_api.db", connect_args={"check_same_thread": False}
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _bearer(user_key):
    """Profile credential header for the native /api/v2 API."""
    return {"Authorization": f"Bearer {user_key}"}


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
    client = TestClient(app)
    return client, db


def cleanup(db):
    db.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


@contextmanager
def use_test_db(db):
    yield db


def _seed_account_with_profiles(db, n=1):
    """Create a fully enrolled account with n profiles. Returns (account, [profiles]).

    Enrolled means a passkey plus TOTP: these tests exercise the profile APIs, so
    the account has to be past the passkey/2FA gate that every cookie-authenticated
    route now enforces.
    """
    account = Account(
        auth_source="local",
        email="owner@example.com",
        is_active=True,
        totp_enabled=True,
    )
    db.add(account)
    db.commit()
    db.add(
        PasskeyCredential(
            account_id=account.id,
            credential_id="seeded-credential",
            public_key="seeded-public-key",
        )
    )
    db.commit()
    profiles = []
    for i in range(n):
        p = User(
            user_key=f"key-{i}",
            account_id=account.id,
            auth_source="local",
            display_name=f"Profile {i}",
            is_active=True,
        )
        db.add(p)
        profiles.append(p)
    db.commit()
    return account, profiles


def test_native_list_profiles_returns_siblings_with_distinct_keys():
    client, db = make_client()
    try:
        _account, profiles = _seed_account_with_profiles(db, n=2)
        key0 = profiles[0].user_key
        with patch("app.main._schedule_local_sync"):
            resp = client.get("/api/v2/profiles", headers=_bearer(key0))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["active_profile_id"] == profiles[0].id
        keys = {p["user_key"] for p in body["profiles"]}
        assert keys == {"key-0", "key-1"}
        assert all(p["api_base"] == settings.base_url for p in body["profiles"])
        # The key is a header credential now; no URL embeds it.
        assert all("server_url" not in p for p in body["profiles"])
        assert all("manifest_url" not in p for p in body["profiles"])
    finally:
        cleanup(db)


def test_native_create_update_delete_via_user_key():
    client, db = make_client()
    try:
        _account, profiles = _seed_account_with_profiles(db, n=1)
        key = profiles[0].user_key
        with patch("app.main._schedule_local_sync"):
            created = client.post(
                "/api/v2/profiles",
                headers=_bearer(key),
                json={"display_name": "Kid", "bandwidth_tier": "low"},
            )
            assert created.status_code == 200, created.text
            new = created.json()["profile"]
            assert new["user_key"] != key
            assert new["bandwidth_tier"] == "low"

            patched = client.patch(
                f"/api/v2/profiles/{new['id']}",
                headers=_bearer(key),
                json={"display_name": "Kiddo"},
            )
            assert patched.status_code == 200
            assert patched.json()["profile"]["display_name"] == "Kiddo"

            # "Automatic" quality, offered by the Apple and web profile editors.
            auto = client.patch(
                f"/api/v2/profiles/{new['id']}",
                headers=_bearer(key),
                json={"bandwidth_tier": "auto"},
            )
            assert auto.status_code == 200, auto.text
            assert auto.json()["profile"]["bandwidth_tier"] == "auto"
            bogus = client.patch(
                f"/api/v2/profiles/{new['id']}",
                headers=_bearer(key),
                json={"bandwidth_tier": "ultra"},
            )
            assert bogus.status_code == 422

            assert (
                client.delete(
                    f"/api/v2/profiles/{new['id']}", headers=_bearer(key)
                ).status_code
                == 200
            )
            remaining = client.get("/api/v2/profiles", headers=_bearer(key)).json()[
                "profiles"
            ]
            assert len(remaining) == 1
    finally:
        cleanup(db)


def test_native_unknown_user_key_is_rejected():
    client, db = make_client()
    try:
        _seed_account_with_profiles(db, n=1)
        assert (
            client.get(
                "/api/v2/profiles", headers=_bearer("not-a-real-key")
            ).status_code
            == 401
        )
    finally:
        cleanup(db)


def test_native_cannot_delete_only_profile():
    client, db = make_client()
    try:
        _account, profiles = _seed_account_with_profiles(db, n=1)
        key = profiles[0].user_key
        resp = client.delete(f"/api/v2/profiles/{profiles[0].id}", headers=_bearer(key))
        assert resp.status_code == 400
    finally:
        cleanup(db)


def test_native_create_respects_profile_cap():
    client, db = make_client()
    try:
        from app.main import MAX_PROFILES_PER_ACCOUNT

        _account, profiles = _seed_account_with_profiles(db, n=MAX_PROFILES_PER_ACCOUNT)
        key = profiles[0].user_key
        with patch("app.main._schedule_local_sync"):
            resp = client.post(
                "/api/v2/profiles",
                headers=_bearer(key),
                json={"display_name": "Overflow"},
            )
        assert resp.status_code == 400
    finally:
        cleanup(db)


def test_native_profile_list_is_account_isolated():
    """A user_key only ever sees its own account's profiles."""
    client, db = make_client()
    try:
        _seed_account_with_profiles(db, n=2)
        # Second, unrelated account.
        other = Account(auth_source="local", email="other@example.com", is_active=True)
        db.add(other)
        db.commit()
        db.add(
            User(
                user_key="other-key",
                account_id=other.id,
                auth_source="local",
                display_name="Stranger",
                is_active=True,
            )
        )
        db.commit()
        with patch("app.main._schedule_local_sync"):
            body = client.get("/api/v2/profiles", headers=_bearer("key-0")).json()
        keys = {p["user_key"] for p in body["profiles"]}
        assert "other-key" not in keys
    finally:
        cleanup(db)


def test_legacy_single_profile_account_behaves_unchanged():
    """A single-profile account keeps its user_key and content endpoints work."""
    client, db = make_client()
    try:
        _account, profiles = _seed_account_with_profiles(db, n=1)
        key = profiles[0].user_key
        # `scrobble` opens its own session via app.main.get_db (not the dependency),
        # so point that at the test DB too.
        with patch("app.main._schedule_local_sync"), patch(
            "app.main.get_db", side_effect=lambda: use_test_db(db)
        ):
            # The native API still resolves for the existing user_key.
            assert (
                client.get("/api/v2/profiles", headers=_bearer(key)).status_code == 200
            )
            # Scrobble + history still work and are scoped to this profile.
            assert (
                client.post(
                    "/api/v2/scrobble",
                    headers=_bearer(key),
                    json={"tmdb_id": 99, "media_type": "movie", "action": "complete"},
                ).status_code
                == 200
            )
        events = db.query(WatchEvent).filter(WatchEvent.user_id == profiles[0].id).all()
        assert len(events) == 1
        # The account exposes exactly one profile.
        body = client.get("/api/v2/profiles", headers=_bearer(key)).json()
        assert len(body["profiles"]) == 1
    finally:
        cleanup(db)


def test_cookie_and_native_apis_agree():
    """Profiles created via the cookie API are visible via the native API."""
    client, db = make_client()
    try:
        account, profiles = _seed_account_with_profiles(db, n=1)
        key = profiles[0].user_key
        token = create_user_session(account.id, db, active_profile_id=profiles[0].id)
        client.cookies.set("user_session", token)
        with patch("app.main.get_db", return_value=use_test_db(db)), patch(
            "app.main._schedule_local_sync"
        ):
            created = client.post("/account/api/profiles", json={"display_name": "Two"})
            assert created.status_code == 200, created.text
        client.cookies.clear()
        native = client.get("/api/v2/profiles", headers=_bearer(key)).json()
        names = {p["display_name"] for p in native["profiles"]}
        assert "Two" in names
    finally:
        cleanup(db)
