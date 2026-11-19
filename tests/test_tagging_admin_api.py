"""
Tests for the admin Tagging tab's endpoints.

The toggle is the piece most likely to be misread, so it is asserted rather
than described: turning it off blocks the *next* rebuild and makes future
tagging runs use the legacy path. It does not — cannot — un-write tags already
stored. A test that pinned "off means the old tags come back" would encode a
promise the system does not make.
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

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_tagging_admin_api.db")
os.environ.setdefault("TMDB_API_KEY", "test")
os.environ.setdefault("GEMINI_API_KEY", "test")
os.environ.setdefault("MASTER_PASSWORD", "test")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SECRET_KEY", "t" * 40)
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app import database as app_database  # noqa: E402
from app.admin import verify_admin  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, MediaMetadata, TaggingJob  # noqa: E402

TEST_DATABASE_URL = "sqlite:///./test_tagging_admin_api.db"
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

    monkeypatch.setattr(app_database, "get_db", lambda: _Ctx())
    import app.admin as admin_module
    import workers.retag as retag_module

    monkeypatch.setattr(admin_module, "get_db", lambda: _Ctx())
    monkeypatch.setattr(retag_module, "get_db", lambda: _Ctx())
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


def test_config_reports_what_the_tab_renders(db, client):
    body = client.get("/admin/api/tagging/config").json()

    for key in (
        "taxonomy_version",
        "taxonomy_size",
        "model",
        "library_total",
        "tmdb_enriched",
        "mdb_enriched",
        "rating_consensus",
        "mdblist_key_set",
        "rebuild_running",
    ):
        assert key in body, f"config is missing {key}"
    assert body["taxonomy_size"] > 0
    assert body["cost_ceiling_usd"] > 0

    app_settings = client.get("/admin/api/settings").json()["app"]
    for key in (
        "CATALOG_NICHE_MIN_QUALITY",
        "CATALOG_NICHE_MIN_VOTE_COUNT",
        "CATALOG_NICHE_MIN_AGE_DAYS",
        "RETAG_COST_CEILING_USD",
    ):
        assert key in app_settings


def test_quality_preview_uses_niche_inputs_without_mutating_live_settings(db, client):
    from app.config import settings

    db.add(
        MediaMetadata(
            tmdb_id=77,
            media_type="movie",
            title="Acclaimed niche film",
            overview="An acclaimed film with enough synopsis evidence for tagging.",
            release_date="2020-01-01",
            vote_count=500,
            vote_average=8.0,
        )
    )
    db.commit()
    before = (
        settings.catalog_min_quality,
        settings.catalog_niche_min_quality,
        settings.catalog_niche_min_vote_count,
    )

    excluded = client.get(
        "/admin/api/catalog/quality/preview",
        params={
            "min_quality": 6.8,
            "min_vote_count": 1000,
            "min_rating_sources": 2,
            "niche_min_quality": 8.5,
            "niche_min_vote_count": 250,
            "niche_min_age_days": 180,
        },
    ).json()
    included = client.get(
        "/admin/api/catalog/quality/preview",
        params={
            "min_quality": 6.8,
            "min_vote_count": 1000,
            "min_rating_sources": 2,
            "niche_min_quality": 7.4,
            "niche_min_vote_count": 250,
            "niche_min_age_days": 180,
        },
    ).json()

    assert excluded["counts"]["movie"]["kept"] == 0
    assert included["counts"]["movie"]["kept"] == 1
    assert (
        settings.catalog_min_quality,
        settings.catalog_niche_min_quality,
        settings.catalog_niche_min_vote_count,
    ) == before


def test_the_dead_v2_toggle_is_gone(db, client):
    """It was a switch that switched nothing.

    ``tagging_v2_enabled`` was consulted in exactly two places, and in both it
    only raised 409 -- no worker ever read it. Turning it off did not move any
    tagger onto a legacy path; it disabled two buttons while Daily Update and
    Database Build carried on tagging at v2 regardless. The legacy 75-tag
    prompt it claimed to switch to has been reachable only from a CLI
    diagnostic since the overhaul.
    """
    body = client.get("/admin/api/tagging/config").json()
    assert "tagging_v2_enabled" not in body

    # An install that stored the flag as false must not stay locked out of its
    # own rebuild button.
    from app.models import AdminSetting

    db.add(AdminSetting(key="tagging_v2_enabled", value="false"))
    db.commit()

    assert client.post("/admin/api/tagging/rebuild/start", json={}).status_code != 409


def test_mdblist_key_stores_and_clears(db, client):
    client.post("/admin/api/tagging/config", json={"mdblist_api_key": "abc123def456"})
    body = client.get("/admin/api/tagging/config").json()
    assert body["mdblist_key_set"] is True
    assert "abc" in body["mdblist_key_hint"]
    # The full key never leaves the server.
    assert "abc123def456" not in str(body)

    client.post("/admin/api/tagging/config", json={"mdblist_api_key": ""})
    assert client.get("/admin/api/tagging/config").json()["mdblist_key_set"] is False


def test_estimate_costs_nothing_and_writes_nothing(db, client):
    db.add(
        MediaMetadata(
            tmdb_id=1,
            media_type="movie",
            title="x",
            overview="A complete synopsis with enough evidence for semantic tagging.",
            vote_count=100,
            rating_source_count=5,
            rating_consensus=7.5,
            mdb_version="1.1.0",
        )
    )
    db.add(
        MediaMetadata(
            tmdb_id=2,
            media_type="movie",
            title="y",
            overview="A complete synopsis with enough evidence for semantic tagging.",
            vote_count=1,
            rating_source_count=5,
            rating_consensus=7.5,
            mdb_version="1.1.0",
        )
    )
    db.commit()

    body = client.post("/admin/api/tagging/rebuild/estimate", json={}).json()

    assert body["pending"] == 2
    assert body["est_cost_usd"] >= 0
    assert body["requests"] >= 1
    assert db.query(TaggingJob).count() == 0


def test_estimate_honours_the_vote_floor(db, client):
    """Titles below the catalog floor cannot appear in a row, so skip them."""
    db.add(
        MediaMetadata(
            tmdb_id=1,
            media_type="movie",
            title="x",
            overview="A complete synopsis with enough evidence for semantic tagging.",
            vote_count=100,
            rating_source_count=5,
            rating_consensus=7.5,
            mdb_version="1.1.0",
        )
    )
    db.add(
        MediaMetadata(
            tmdb_id=2,
            media_type="movie",
            title="y",
            overview="A complete synopsis with enough evidence for semantic tagging.",
            vote_count=1,
            rating_source_count=5,
            rating_consensus=7.5,
            mdb_version="1.1.0",
        )
    )
    db.commit()

    body = client.post(
        "/admin/api/tagging/rebuild/estimate", json={"min_votes": 10}
    ).json()

    assert body["pending"] == 1


def test_rebuild_refuses_to_start_alongside_a_running_job(db, client):
    db.add(TaggingJob(job_type="initial_build", status="running"))
    db.commit()

    response = client.post("/admin/api/tagging/rebuild/start", json={})

    assert response.status_code == 409
    assert "already running" in response.json()["detail"]


def test_status_is_readable_before_any_rebuild(db, client):
    body = client.get("/admin/api/tagging/rebuild/status").json()
    assert body["job"] is None
    assert "version_breakdown" in body


def test_pause_and_resume_move_the_shared_event(db, client):
    from workers.build_control import pause_event

    try:
        assert client.post("/admin/api/tagging/rebuild/pause", json={}).json()["paused"]
        assert not pause_event.is_set()

        assert not (
            client.post("/admin/api/tagging/rebuild/resume", json={}).json()["paused"]
        )
        assert pause_event.is_set()
    finally:
        pause_event.set()


def test_unknown_control_action_is_a_404(db, client):
    assert client.post("/admin/api/tagging/rebuild/explode", json={}).status_code == 404


def test_a_restart_never_relaunches_a_rebuild_as_a_legacy_build(db, monkeypatch):
    """The deploy trap: auto_resume_build used to ignore job_type.

    An orphaned ``manual_retag`` row has ``job_metadata`` but no
    ``movies_target``, so the old code fell through to the 100k/50k defaults and
    started a legacy ``initial_build`` -- a multi-hour TMDB crawl plus a full
    tagging pass at the old vocabulary, with no vote floor and no cost ceiling,
    triggered by nothing but a container restart.
    """
    import asyncio

    import app.admin as admin_module

    relaunched: list = []
    monkeypatch.setattr(
        admin_module, "_relaunch_build", lambda *a, **k: relaunched.append(a)
    )

    db.add(
        TaggingJob(
            job_type="manual_retag",
            status="running",
            job_metadata={"target_version": "2.0.0", "model": "x"},
        )
    )
    db.commit()

    asyncio.run(admin_module.auto_resume_build())

    assert not relaunched, "a restart started work the operator never asked for"
    job = db.query(TaggingJob).first()
    assert job.status == "cancelled"
    assert "start it again" in (job.error_message or "")


def test_a_restart_still_resumes_a_real_build(db, monkeypatch):
    """The filter must not break the behaviour it is narrowing."""
    import asyncio

    import app.admin as admin_module

    relaunched: list = []
    monkeypatch.setattr(
        admin_module, "_relaunch_build", lambda *a, **k: relaunched.append(a)
    )

    db.add(
        TaggingJob(
            job_type="database_build",
            status="running",
            job_metadata={"movies_target": 500, "shows_target": 200},
        )
    )
    db.commit()

    asyncio.run(admin_module.auto_resume_build())

    assert relaunched == [(500, 200)]


def test_orphaned_rebuild_does_not_block_the_next_start(db, client, monkeypatch):
    """A dead 'running' row must not 409 the operator out of restarting."""
    import asyncio

    import app.admin as admin_module

    monkeypatch.setattr(admin_module, "_relaunch_build", lambda *a, **k: None)
    db.add(TaggingJob(job_type="manual_retag", status="running", job_metadata={"a": 1}))
    db.commit()

    asyncio.run(admin_module.auto_resume_build())

    assert db.query(TaggingJob).filter(TaggingJob.status == "running").count() == 0


def test_config_flags_a_model_pinned_in_the_database(db, client):
    """A stored GEMINI_MODEL outranks .env forever; it must be visible."""
    from app.models import AdminSetting

    body = client.get("/admin/api/tagging/config").json()
    assert body["model_pinned_in_db"] is False

    db.add(AdminSetting(key="GEMINI_MODEL", value="gemini-2.0-flash"))
    db.commit()

    body = client.get("/admin/api/tagging/config").json()
    assert body["model_pinned_in_db"] is True
    assert body["model_default"] == "gemini-2.5-flash-lite"


def test_pilot_only_model_cannot_start_an_unbounded_rebuild(db, client):
    from app.config import settings

    original = (settings.tagging_provider, settings.openai_model)
    try:
        object.__setattr__(settings, "tagging_provider", "openai")
        object.__setattr__(settings, "openai_model", "gpt-5-nano")

        config = client.get("/admin/api/tagging/config").json()
        assert config["model_production_ready"] is False

        response = client.post("/admin/api/tagging/rebuild/start", json={})
        assert response.status_code == 400
        assert "at most 1,000" in response.json()["detail"]
    finally:
        object.__setattr__(settings, "tagging_provider", original[0])
        object.__setattr__(settings, "openai_model", original[1])


def test_clearing_the_pinned_model_restores_the_default(db, client):
    from app.config import settings
    from app.models import AdminSetting

    db.add(AdminSetting(key="GEMINI_MODEL", value="gemini-2.0-flash"))
    db.commit()
    object.__setattr__(settings, "gemini_model", "gemini-2.0-flash")

    try:
        client.post("/admin/api/tagging/config", json={"clear_pinned_model": True})

        body = client.get("/admin/api/tagging/config").json()
        assert body["model_pinned_in_db"] is False
        assert body["model"] == body["model_default"]
        assert (
            db.query(AdminSetting).filter(AdminSetting.key == "GEMINI_MODEL").count()
            == 0
        )
    finally:
        object.__setattr__(
            settings,
            "gemini_model",
            type(settings).model_fields["gemini_model"].default,
        )


def test_endpoints_require_admin(db):
    app.dependency_overrides.clear()
    anon = TestClient(app)
    for method, url in (
        ("get", "/admin/api/tagging/config"),
        ("post", "/admin/api/tagging/config"),
        ("post", "/admin/api/tagging/rebuild/estimate"),
        ("post", "/admin/api/tagging/rebuild/start"),
        ("get", "/admin/api/tagging/rebuild/status"),
        ("post", "/admin/api/tagging/rebuild/stop"),
    ):
        response = getattr(anon, method)(url)
        assert response.status_code in (401, 403), f"{url} is publicly reachable"
