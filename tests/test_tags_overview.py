"""
Tests for GET /admin/api/tags/overview and the taste-cache fingerprint fix.

The overview endpoint exists because ~150k metadata rows (42% of production)
carried no tags at all and no existing statistic surfaced it -- the dashboard
reported "movies tagged" and "metadata entries" as unrelated numbers and nobody
subtracted them. ``zero_tag_items`` is that subtraction.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_tags_overview.db")
os.environ.setdefault("TMDB_API_KEY", "test")
os.environ.setdefault("GEMINI_API_KEY", "test")
os.environ.setdefault("MASTER_PASSWORD", "test")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SECRET_KEY", "t" * 40)
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app import database as app_database  # noqa: E402
from app.admin import verify_admin  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, MediaMetadata, MovieTag, Tag  # noqa: E402

TEST_DATABASE_URL = "sqlite:///./test_tags_overview.db"
engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def db(monkeypatch):
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()

    class _Ctx:
        def __enter__(self):
            return session

        def __exit__(self, *exc):
            return False

    # app.admin calls get_db() as a context manager on the real engine.
    monkeypatch.setattr(app_database, "get_db", lambda: _Ctx())
    import app.admin as admin_module

    monkeypatch.setattr(admin_module, "get_db", lambda: _Ctx())
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def client(db):
    app.dependency_overrides[verify_admin] = lambda: True
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed(db, tagged: int, untagged: int, media_type: str = "movie"):
    tag = Tag(name=f"_probe_{media_type}", category="mood")
    db.add(tag)
    db.flush()
    tmdb_id = 1 if media_type == "movie" else 10_000
    for _ in range(tagged):
        db.add(MediaMetadata(tmdb_id=tmdb_id, media_type=media_type, title="t"))
        db.add(
            MovieTag(
                tmdb_id=tmdb_id,
                media_type=media_type,
                tag_id=tag.id,
                confidence=0.9,
            )
        )
        tmdb_id += 1
    for _ in range(untagged):
        db.add(MediaMetadata(tmdb_id=tmdb_id, media_type=media_type, title="u"))
        tmdb_id += 1
    db.commit()


def test_reports_zero_tag_items(db, client):
    """The number that would have surfaced the 42% gap."""
    _seed(db, tagged=7, untagged=3)

    body = client.get("/admin/api/tags/overview").json()

    assert body["total_metadata"] == 10
    assert body["total_tagged"] == 7
    assert body["zero_tag_items"] == 3
    assert body["coverage_pct"] == 70.0


def test_splits_by_media_type(db, client):
    _seed(db, tagged=5, untagged=5, media_type="movie")
    _seed(db, tagged=1, untagged=3, media_type="tv")

    body = client.get("/admin/api/tags/overview").json()
    by_type = {row["media_type"]: row for row in body["by_media_type"]}

    assert by_type["movie"]["zero_tag_items"] == 5
    assert by_type["tv"]["zero_tag_items"] == 3
    assert by_type["tv"]["coverage_pct"] == 25.0


def test_empty_database_does_not_divide_by_zero(db, client):
    body = client.get("/admin/api/tags/overview").json()
    assert body["coverage_pct"] == 0.0
    assert body["zero_tag_items"] == 0


def test_reports_version_breakdown(db, client):
    _seed(db, tagged=2, untagged=0)
    body = client.get("/admin/api/tags/overview").json()
    assert body["version_breakdown"]
    assert {"tagger_version", "titles"} <= set(body["version_breakdown"][0])


def test_reports_zero_assignment_tags(db, client):
    """A tag with no assignments is the phantom-tag signature."""
    _seed(db, tagged=1, untagged=0)
    db.add(Tag(name="_never_assigned", category="mood"))
    db.commit()

    body = client.get("/admin/api/tags/overview").json()

    assert body["tags_zero_assignments"] >= 1


def test_reports_taxonomy_version(db, client):
    from app.taxonomy import TAXONOMY_VERSION

    assert client.get("/admin/api/tags/overview").json()["taxonomy_version"] == (
        TAXONOMY_VERSION
    )


def test_requires_admin(db):
    """No override -- the endpoint must not be publicly readable."""
    app.dependency_overrides.clear()
    response = TestClient(app).get("/admin/api/tags/overview")
    assert response.status_code in (401, 403)
