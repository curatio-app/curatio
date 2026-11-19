"""
Tests for the one-button upgrade.

The button exists because chaining five workers by hand in dependency order is
not something an operator should have to get right. So the properties that
matter here are the ones that make one button safe rather than reckless:

* a source step failure preserves the old library while publication-stage
  failures block dependent publication,
* a step whose key is missing is skipped, not fatal,
* stopping is honoured between steps and keeps everything already written,
* pressing it twice is not destructive,
* and nothing runs at all until the required keys are present.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_upgrade_pipeline.db")
os.environ.setdefault("TMDB_API_KEY", "test")
os.environ.setdefault("GEMINI_API_KEY", "test")
os.environ.setdefault("MASTER_PASSWORD", "test")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SECRET_KEY", "t" * 40)
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app import database as app_database  # noqa: E402
from app.admin import verify_admin  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, MediaMetadata  # noqa: E402
from workers import upgrade as upgrade_module  # noqa: E402

TEST_DATABASE_URL = "sqlite:///./test_upgrade_pipeline.db"
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

    import app.admin as admin_module

    for module in (app_database, admin_module, upgrade_module):
        monkeypatch.setattr(module, "get_db", lambda: _Ctx())
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


@pytest.fixture()
def fake_steps(monkeypatch):
    """Replace every real step with a recorder. Returns the call log."""
    calls: list[str] = []

    async def _enrich(media_type=None, limit=None, force=False, on_progress=None):
        calls.append("enrich")
        if on_progress:
            on_progress(1, 2)
        return {"reused": 0, "fetched": 1, "failed": 0}

    async def _retag(
        media_type=None,
        limit=None,
        min_votes=0,
        force=False,
        enriched_only=False,
        source_done=None,
        on_progress=None,
    ):
        # Mirrors the real contract: when trailing the enricher, wait for it to
        # signal completion before returning, so the recorded order still shows
        # tagging finishing after enrichment.
        if source_done is not None:
            await source_done.wait()
        calls.append("retag")
        if on_progress:
            on_progress(1, 9, 0.01)
        return {"processed": 1, "tags_written": 9, "cost_usd": 0.01}

    def _relevance(db, media_type=None, *, create_indexes=True):
        calls.append("relevance")
        return {"tags": 1, "rows": 1, "vectors": 1}

    def _rebuild_shelves(db):
        calls.append("catalogs")
        return {"published": 1}

    async def _sync_users(db):
        # Real sync_all_users hits TMDB for trending/new-release rows, so it is
        # stubbed here as well -- the assertion is about ordering, not about
        # rebuilding profiles.
        calls.append("users")
        return {"synced": 2, "total": 2, "catalogs": 24, "failed": 0}

    import app.category_synth as category_synth
    import app.relevance as relevance
    import workers.enrich as enrich_worker
    import workers.personalized_sync as personalized_sync
    import workers.retag as retag_worker

    monkeypatch.setattr(enrich_worker, "enrich", _enrich)
    monkeypatch.setattr(retag_worker, "run_retag", _retag)
    monkeypatch.setattr(relevance, "refresh_tag_stats", _relevance)
    monkeypatch.setattr(category_synth, "rebuild_synthesized_shelves", _rebuild_shelves)
    monkeypatch.setattr(personalized_sync, "sync_all_users", _sync_users)
    return calls


def _run(**kwargs):
    return asyncio.run(upgrade_module.run_upgrade(**kwargs))


def test_steps_run_in_dependency_order(db, fake_steps):
    """Tagging before enrichment silently produces worse tags, so order is code.

    enrich/mdblist/retag now run concurrently, so what is asserted is the
    *dependency*, not the schedule: tagging still completes after enrichment
    (it trails it via ``source_done``), and relevance and catalogs still follow
    tagging, because they read what it produces.
    """
    report = _run()

    assert fake_steps == ["enrich", "retag", "relevance", "catalogs", "users"]
    assert [s["status"] for s in report["steps"]] == [
        "completed",
        "skipped",  # mdblist, no key
        "completed",
        "completed",
        "completed",
        "completed",  # users -- personalized rows are stale until this runs
    ]


def test_mdblist_is_skipped_not_fatal_without_a_key(db, fake_steps):
    report = _run()

    mdb = next(s for s in report["steps"] if s["key"] == "mdblist")
    assert mdb["status"] == "skipped"
    assert "No MDBList key" in mdb["detail"]
    # And the rest of the pipeline still ran.
    assert fake_steps == ["enrich", "retag", "relevance", "catalogs", "users"]


def test_a_failing_step_does_not_abort_the_rest(db, fake_steps, monkeypatch):
    """A partial re-tag still yields a coherent library, so keep going."""
    import workers.enrich as enrich_worker

    async def _boom(media_type=None, limit=None, force=False, on_progress=None):
        raise RuntimeError("TMDB is down")

    monkeypatch.setattr(enrich_worker, "enrich", _boom)

    report = _run()

    steps = {s["key"]: s for s in report["steps"]}
    assert steps["enrich"]["status"] == "failed"
    assert "TMDB is down" in steps["enrich"]["detail"]
    assert steps["retag"]["status"] == "completed"
    assert steps["catalogs"]["status"] == "completed"


def test_a_halted_retag_does_not_publish_partial_rows(db, fake_steps, monkeypatch):
    """A safety halt is not a completed upgrade and cannot reach catalogs."""
    import workers.retag as retag_worker

    async def _halted(*args, **kwargs):
        fake_steps.append("retag")
        return {
            "processed": 1000,
            "failed": 0,
            "requeued": 50,
            "tags_written": 9000,
            "cost_usd": 0.20,
            "halted": True,
            "halt_reason": "Halted: malformed response rate exceeded",
        }

    monkeypatch.setattr(retag_worker, "run_retag", _halted)

    report = _run()

    steps = {s["key"]: s for s in report["steps"]}
    assert steps["retag"]["status"] == "failed"
    assert "malformed response rate" in steps["retag"]["detail"]
    assert steps["relevance"]["status"] == "blocked"
    assert steps["catalogs"]["status"] == "blocked"
    assert steps["users"]["status"] == "blocked"
    assert "catalogs" not in fake_steps
    assert report["publication_blocked"] is True


def test_skip_leaves_a_step_out(db, fake_steps):
    report = _run(skip=("enrich", "retag"))

    assert fake_steps == ["relevance", "catalogs", "users"]
    steps = {s["key"]: s for s in report["steps"]}
    assert steps["enrich"]["status"] == "skipped"
    assert steps["retag"]["status"] == "skipped"


def test_limited_pilot_never_publishes_rows(db, fake_steps):
    """A validation sample may write tags but cannot replace live snapshots."""
    report = _run(limit=1000)

    assert fake_steps == ["enrich", "retag"]
    steps = {s["key"]: s for s in report["steps"]}
    for key in upgrade_module.PUBLICATION_STEPS:
        assert steps[key]["status"] == "skipped"
        assert "Pilot safety" in steps[key]["detail"]


def test_catalog_publication_requires_complete_current_version(db, fake_steps):
    """A catalog-only click cannot synthesize from a pilot-sized corpus."""
    db.add(
        MediaMetadata(
            tmdb_id=1,
            media_type="movie",
            title="Pending",
            overview="A complete synopsis with enough evidence for semantic tagging.",
            vote_count=2000,
            vote_average=8.0,
        )
    )
    db.commit()

    report = _run(skip=("enrich", "mdblist", "retag", "relevance", "users"))

    step = next(s for s in report["steps"] if s["key"] == "catalogs")
    assert step["status"] == "failed"
    assert "1 of 1 quality-eligible titles" in step["detail"]
    assert "catalogs" not in fake_steps
    assert report["publication_blocked"] is True


def test_stopping_halts_the_pipeline_and_keeps_what_landed(db, fake_steps, monkeypatch):
    from workers import build_control

    async def _enrich_then_stop(
        media_type=None, limit=None, force=False, on_progress=None
    ):
        fake_steps.append("enrich")
        build_control.request_build_stop()
        return {"reused": 0, "fetched": 1, "failed": 0}

    import workers.enrich as enrich_worker

    monkeypatch.setattr(enrich_worker, "enrich", _enrich_then_stop)

    try:
        report = _run()
    finally:
        build_control.reset_build_stop()

    assert report["stopped"] is True
    # The completed step is recorded, and nothing after it ran.
    steps = {s["key"]: s for s in report["steps"]}
    assert steps["enrich"]["status"] == "completed"
    assert "retag" not in fake_steps
    assert "catalogs" not in fake_steps


def test_running_twice_is_not_destructive(db, fake_steps):
    """Each step re-selects its own outstanding work, so a second press is safe."""
    _run()
    first = list(fake_steps)
    fake_steps.clear()

    report = _run()

    assert fake_steps == first
    assert all(s["status"] in ("completed", "skipped") for s in report["steps"])


def test_a_running_step_reports_progress_not_an_empty_spinner(db, fake_steps):
    """``detail`` used to be written only on completion.

    The enrich step runs for hours. A card that stays blank for all of them is
    indistinguishable from a wedged one, which is exactly how the first two
    production runs were misread.
    """
    seen: list[tuple[str, str]] = []

    def _on_step(state):
        entry = state["current"]
        if entry["status"] == "running" and entry["detail"]:
            seen.append((entry["key"], entry["detail"]))

    asyncio.run(upgrade_module.run_upgrade(on_step=_on_step))

    progressed = {key for key, _ in seen}
    assert "enrich" in progressed
    assert "retag" in progressed
    # And the text is the running count, not a placeholder.
    assert dict(seen)["enrich"] == "1 / 2"


def test_preflight_costs_nothing_and_reports_what_is_left(db):
    db.add(
        MediaMetadata(
            tmdb_id=1,
            media_type="movie",
            title="x",
            overview="A complete synopsis with enough evidence for semantic tagging.",
            vote_count=2000,
            vote_average=7.5,
        )
    )
    db.add(
        MediaMetadata(
            tmdb_id=2,
            media_type="movie",
            title="y",
            overview="A complete synopsis with enough evidence for semantic tagging.",
            vote_count=2000,
            vote_average=7.5,
        )
    )
    db.commit()

    report = upgrade_module.preflight()

    assert report["library_total"] == 2
    assert report["eligible_titles"] == 2
    assert report["excluded_by_quality"] == 0
    assert report["remaining"]["enrich"] == 2
    assert report["remaining"]["retag"] == 2
    assert report["estimated_ai_titles"] == 2
    assert report["estimated_local_only"] == 0
    assert report["est_cost_usd"] >= 0
    assert [s["key"] for s in report["steps"]] == list(upgrade_module.STEP_KEYS)


def test_preflight_marks_mdblist_unavailable_without_a_key(db):
    report = upgrade_module.preflight()

    mdb = next(s for s in report["steps"] if s["key"] == "mdblist")
    assert mdb["available"] is False
    assert report["keys"]["mdblist"] is False
    # But the upgrade as a whole is still runnable.
    assert report["ready"] is True


# ---- endpoints ----


def test_preflight_endpoint_shape(db, client):
    body = client.get("/admin/api/tagging/upgrade/preflight").json()
    assert {"keys", "ready", "steps", "est_cost_usd", "running"} <= set(body)


def test_keys_endpoint_stores_and_never_echoes_them(db, client):
    from app.models import AdminSetting

    response = client.post(
        "/admin/api/tagging/upgrade/keys",
        json={"gemini_api_key": "secret-gemini-key", "mdblist_api_key": "secret-mdb"},
    )

    assert response.status_code == 200
    stored = {
        r.key: r.value
        for r in db.query(AdminSetting).filter(
            AdminSetting.key.in_(("GEMINI_API_KEY", "mdblist_api_key"))
        )
    }
    assert stored.get("GEMINI_API_KEY") == "secret-gemini-key"

    body = client.get("/admin/api/tagging/upgrade/preflight").json()
    assert "secret-gemini-key" not in str(body)
    assert body["keys"]["mdblist"] is True


def test_admin_can_select_openai_without_exposing_the_key(db, client):
    from app.config import settings
    from app.models import AdminSetting

    original = (
        settings.tagging_provider,
        settings.openai_api_key,
        settings.openai_model,
    )
    try:
        response = client.post(
            "/admin/api/tagging/upgrade/keys",
            json={
                "tagging_provider": "openai",
                "model": "gpt-5-nano",
                "openai_api_key": "sk-secret-openai-key",
            },
        )
        assert response.status_code == 200
        stored = {
            row.key: row.value
            for row in db.query(AdminSetting).filter(
                AdminSetting.key.in_(
                    ("TAGGING_PROVIDER", "OPENAI_MODEL", "OPENAI_API_KEY")
                )
            )
        }
        assert stored == {
            "TAGGING_PROVIDER": "openai",
            "OPENAI_MODEL": "gpt-5-nano",
            "OPENAI_API_KEY": "sk-secret-openai-key",
        }

        body = client.get("/admin/api/tagging/upgrade/preflight").json()
        assert body["provider"] == "openai"
        assert body["model"] == "gpt-5-nano"
        assert body["keys"]["tagging"] is True
        assert "sk-secret-openai-key" not in str(body)
    finally:
        object.__setattr__(settings, "tagging_provider", original[0])
        object.__setattr__(settings, "openai_api_key", original[1])
        object.__setattr__(settings, "openai_model", original[2])


def test_start_refuses_alongside_a_running_job(db, client):
    from app.models import TaggingJob

    db.add(TaggingJob(job_type="database_build", status="running"))
    db.commit()

    response = client.post("/admin/api/tagging/upgrade/start", json={})

    assert response.status_code == 409


def test_status_is_readable_before_anything_runs(db, client):
    body = client.get("/admin/api/tagging/upgrade/status").json()
    assert body["running"] is False
    assert body["steps"] == []


def test_upgrade_endpoints_require_admin(db):
    app.dependency_overrides.clear()
    anon = TestClient(app)
    for method, url in (
        ("get", "/admin/api/tagging/upgrade/preflight"),
        ("post", "/admin/api/tagging/upgrade/keys"),
        ("post", "/admin/api/tagging/upgrade/start"),
        ("get", "/admin/api/tagging/upgrade/status"),
        ("post", "/admin/api/tagging/upgrade/stop"),
    ):
        assert getattr(anon, method)(url).status_code in (401, 403), url
