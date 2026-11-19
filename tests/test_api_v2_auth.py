# ruff: noqa: E402
"""The native profile API: /api/v2 paths, key in ``Authorization: Bearer``."""

import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_api_v2_auth.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

import app.main as main
from app.database import get_db_dependency
from app.main import app
from app.models import Account, Base, User

engine = create_engine(
    "sqlite:///./test_api_v2_auth.db", connect_args={"check_same_thread": False}
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _bearer(user_key):
    return {"Authorization": f"Bearer {user_key}"}


def _v2_routes() -> list[tuple[str, str]]:
    """Every /api/v2 route as (method, concrete path)."""
    out = []
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/api/v2/"):
            path = re.sub(r"\{[^}]+\}", "1", route.path)
            path = (
                path.replace("/1/", "/movie/", 1)
                if "{media_type}" in route.path
                else path
            )
            for method in sorted(route.methods - {"HEAD"}):
                out.append((method, path))
    return out


V2_ROUTES = _v2_routes()


@pytest.fixture
def client_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()

    def override_get_db():
        yield db

    app.dependency_overrides[get_db_dependency] = override_get_db
    main._user_cache.clear()
    main._v2_auth_failures.clear()
    try:
        yield TestClient(app), db
    finally:
        db.close()
        app.dependency_overrides.clear()
        main._user_cache.clear()
        main._v2_auth_failures.clear()
        Base.metadata.drop_all(bind=engine)


def _seed(db, n=2):
    account = Account(auth_source="local", email="v2@example.com", is_active=True)
    db.add(account)
    db.commit()
    profiles = []
    for i in range(n):
        profile = User(
            user_key=f"v2-key-{i}",
            account_id=account.id,
            auth_source="local",
            display_name=f"P{i}",
            is_active=True,
        )
        db.add(profile)
        profiles.append(profile)
    db.commit()
    return account, profiles


def test_route_table_covers_the_whole_native_api():
    paths = {path for _, path in V2_ROUTES}
    for expected in (
        "/api/v2/home",
        "/api/v2/home/rows",
        "/api/v2/bootstrap",
        "/api/v2/details",
        "/api/v2/search",
        "/api/v2/profiles",
        "/api/v2/progress",
        "/api/v2/playback/sources",
        "/api/v2/segments/1",
    ):
        assert expected in paths
    # Nothing on the server takes the profile key in its path any more.
    assert not [r.path for r in app.routes if "{user_key}" in getattr(r, "path", "")]


@pytest.mark.parametrize("method,path", V2_ROUTES)
def test_every_v2_route_requires_a_bearer_key(client_db, method, path):
    client, _ = client_db
    for headers in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}):
        response = client.request(method, path, headers=headers, json={})
        assert response.status_code == 401, (method, path, headers)
        assert response.headers["www-authenticate"].startswith("Bearer")
        assert "authorization" in response.headers["vary"].lower()


@pytest.mark.parametrize("path", ["/api/v2/profiles", "/api/v2/home", "/api/v2/facets"])
def test_unknown_key_is_401(client_db, path):
    client, db = client_db
    _seed(db)
    assert client.get(path, headers=_bearer("not-a-key")).status_code == 401


def test_old_key_in_path_routes_are_gone(client_db):
    client, db = client_db
    _seed(db)
    for path in ("/v2-key-0/profiles", "/v2-key-0/api/home", "/v2-key-0/progress"):
        assert client.get(path).status_code in (404, 405)


def test_v2_responses_are_private_and_vary_on_authorization(client_db):
    client, db = client_db
    _seed(db)
    with patch("app.main._schedule_local_sync"):
        response = client.get("/api/v2/profiles", headers=_bearer("v2-key-0"))
    assert response.status_code == 200
    assert "public" not in response.headers["cache-control"]
    assert "private" in response.headers["cache-control"]
    assert "authorization" in response.headers["vary"].lower()


def test_repeated_bad_keys_are_throttled_per_ip(client_db):
    client, _ = client_db
    for _ in range(main._V2_AUTH_FAILURE_LIMIT):
        assert client.get("/api/v2/profiles").status_code == 401
    assert client.get("/api/v2/profiles").status_code == 429


def test_rotating_a_key_retires_the_old_one(client_db):
    client, db = client_db
    _, profiles = _seed(db)
    target = profiles[1]
    with patch("app.main._schedule_local_sync"):
        assert (
            client.get("/api/v2/profiles", headers=_bearer("v2-key-1")).status_code
            == 200
        )
        response = client.post(
            f"/api/v2/profiles/{target.id}/rotate-key", headers=_bearer("v2-key-0")
        )
        assert response.status_code == 200
        new_key = response.json()["user_key"]
        assert new_key != "v2-key-1" and len(new_key) >= 40
        # The cached old key must not keep working.
        assert (
            client.get("/api/v2/profiles", headers=_bearer("v2-key-1")).status_code
            == 401
        )
        assert (
            client.get("/api/v2/profiles", headers=_bearer(new_key)).status_code == 200
        )


def test_rotation_is_limited_to_the_callers_account(client_db):
    client, db = client_db
    _seed(db)
    other = Account(auth_source="local", email="other@example.com", is_active=True)
    db.add(other)
    db.commit()
    stranger = User(user_key="stranger-key", account_id=other.id, is_active=True)
    db.add(stranger)
    db.commit()
    response = client.post(
        f"/api/v2/profiles/{stranger.id}/rotate-key", headers=_bearer("v2-key-0")
    )
    assert response.status_code == 404
    db.refresh(stranger)
    assert stranger.user_key == "stranger-key"


def test_version_advertises_bearer_auth(client_db):
    client, _ = client_db
    body = client.get("/api/version").json()
    assert body["auth"] == "bearer"
    assert body["api_prefix"] == "/api/v2"
