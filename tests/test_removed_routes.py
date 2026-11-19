# ruff: noqa: E402
"""The legacy third-party addon surface is gone, not merely hidden.

Curatio's clients use the native API (``/{user_key}/api/...`` and
``/account/api/...``). The old manifest, catalog, meta and stream resource
routes must answer 404 even for a valid, active profile key.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_removed_routes.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.database import get_db_dependency  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Base, User  # noqa: E402

engine = create_engine(
    "sqlite:///./test_removed_routes.db", connect_args={"check_same_thread": False}
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _bearer(user_key):
    """Profile credential header for the native /api/v2 API."""
    return {"Authorization": f"Bearer {user_key}"}


KEY = "removed-routes-key"


@pytest.fixture
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    account = Account(auth_source="local", email="routes@example.com", is_active=True)
    db.add(account)
    db.commit()
    db.add(
        User(
            user_key=KEY,
            account_id=account.id,
            auth_source="local",
            display_name="Viewer",
            is_active=True,
        )
    )
    db.commit()

    def override_get_db():
        yield db

    app.dependency_overrides[get_db_dependency] = override_get_db
    try:
        with patch("app.main._schedule_local_sync"):
            yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_the_profile_key_is_valid(client):
    """Control: the key itself works, so the 404s below are about the routes."""
    assert client.get("/api/v2/profiles", headers=_bearer(KEY)).status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        f"/{KEY}/catalog/movie/x.json",
        f"/{KEY}/meta/movie/tt1.json",
        f"/{KEY}/stream/movie/tt1.json",
    ],
)
def test_legacy_addon_routes_are_404(client, path):
    assert client.get(path).status_code == 404
