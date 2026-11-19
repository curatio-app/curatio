# ruff: noqa: E402
"""The guards that stop a re-tag from spending without bound.

Two independent defects made a ``force=True`` run unbounded, and each one alone
would have been survivable:

* ``_pending_query`` skips the version anti-join under ``force``, so the pending
  set never shrinks and ``run_retag``'s loop had no reachable exit.
* the cost ceiling was evaluated against a ``stats`` dict created fresh inside
  each ``tag_and_store`` call, so it measured a single pass rather than the run.
  One pass over 354k titles costs ~$25 against a $75 ceiling, so it could never
  trip -- which is precisely why it failed to stop the loop.

Together they meant a force re-tag would re-tag the whole library, then do it
again, indefinitely. Both tests below fail on the pre-fix code.
"""

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_retag_safety.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app import database as app_database  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import Base, MediaMetadata, MovieTag, Tag, TaggingJob  # noqa: E402
from app.time_utils import utc_now  # noqa: E402

TEST_DATABASE_URL = "sqlite:///./test_retag_safety.db"
engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def db(monkeypatch):
    # A setup failure cannot reach the finalizer below. Start from a clean
    # schema as well so one interrupted local run does not poison every later
    # test with duplicate fixture rows.
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()

    class _Ctx:
        def __enter__(self):
            return session

        def __exit__(self, *exc):
            return False

    import workers.retag as retag_module
    import workers.tagging_common as tagging_common

    for module in (app_database, retag_module, tagging_common):
        monkeypatch.setattr(module, "get_db", lambda: _Ctx(), raising=False)

    tag = Tag(name="Heist", category="plot_device")
    session.add(tag)
    session.commit()
    for tmdb_id in range(1, 6):
        session.add(
            MediaMetadata(
                tmdb_id=tmdb_id,
                media_type="movie",
                title=f"Movie {tmdb_id}",
                overview="A complete synopsis with enough evidence for semantic tagging.",
                popularity=float(10 - tmdb_id),
                vote_count=500,
                tag_facts={"title": f"Movie {tmdb_id}"},
            )
        )
        # Already tagged at the CURRENT version -- the default selection would
        # skip these entirely, which is what makes force the interesting case.
        session.add(
            MovieTag(
                tmdb_id=tmdb_id,
                media_type="movie",
                tag_id=tag.id,
                confidence=0.9,
                tagger_version=_version(),
            )
        )
    session.commit()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def _version():
    from workers.tagging_common import tagger_version

    return tagger_version()


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
def test_force_selects_titles_the_default_pass_would_skip(db):
    """The premise. Without this, the termination test proves nothing."""
    from workers.retag import _pending_query

    assert _pending_query(db, None, 0, False, eligible_only=False).count() == 0
    assert _pending_query(db, None, 0, True, eligible_only=False).count() == 5


def test_default_retag_scope_excludes_titles_the_catalog_cannot_show(db):
    from workers.retag import _pending_query

    good = db.query(MediaMetadata).filter(MediaMetadata.tmdb_id == 1).one()
    good.vote_count = 2000
    good.vote_average = 7.5
    dud = db.query(MediaMetadata).filter(MediaMetadata.tmdb_id == 2).one()
    dud.vote_count = 2000
    dud.vote_average = 4.8
    db.commit()

    ids = {row.tmdb_id for row in _pending_query(db, force=True).all()}
    assert ids == {1}


def test_tagger_stamp_changes_when_the_model_changes():
    from workers.tagging_common import tagger_version

    original = settings.gemini_model
    before = tagger_version()
    try:
        object.__setattr__(settings, "gemini_model", "gemini-probe")
        assert tagger_version() != before
    finally:
        object.__setattr__(settings, "gemini_model", original)


def test_tagger_stamp_includes_the_response_schema_contract(monkeypatch):
    import app.gemini_client as gemini_client
    from workers.tagging_common import tagger_version

    before = tagger_version()
    monkeypatch.setattr(gemini_client, "RESPONSE_SCHEMA_VERSION", "probe")

    assert tagger_version() != before


def test_tagger_stamp_includes_source_fact_versions(monkeypatch):
    """Repairing source facts must make old assignments eligible for re-tag."""
    import workers.enrich as enrich_worker
    import workers.mdb_sync as mdb_worker
    from workers.tagging_common import tagger_version

    before = tagger_version()
    monkeypatch.setattr(enrich_worker, "ENRICH_VERSION", "source-probe")
    assert tagger_version() != before

    after_enrich = tagger_version()
    monkeypatch.setattr(mdb_worker, "MDB_VERSION", "mdb-probe")
    assert tagger_version() != after_enrich


# ---------------------------------------------------------------------------
# Termination
# ---------------------------------------------------------------------------
def test_a_force_run_terminates(db, monkeypatch):
    """The headline. On the pre-fix code this hangs forever.

    ``force`` deliberately ignores the version anti-join, so every pass
    re-selects the same five titles and no other exit condition in the loop can
    fire. One pass is the whole job.
    """
    import asyncio

    import workers.retag as retag_module

    passes = {"n": 0}

    async def _fake_tag_and_store(db_, features, **kwargs):
        passes["n"] += 1
        assert passes["n"] < 10, "force re-selected the same titles and looped"
        return {
            "processed": len(features),
            "failed": 0,
            "tags_written": len(features) * 3,
            "off_vocabulary": 0,
            "requeued": 0,
            "blocked": 0,
            "input_tokens": 100,
            "output_tokens": 100,
            "api_calls": 1,
            "cost_usd": 0.01,
            "estimated_usage": False,
            "first_error": "",
        }

    monkeypatch.setattr(
        "workers.tagging_common.tag_and_store", _fake_tag_and_store, raising=False
    )
    monkeypatch.setattr(retag_module, "build_features", lambda row: {"tmdb_id": 1})

    import workers.tagging_common as tagging_common

    monkeypatch.setattr(tagging_common, "tag_and_store", _fake_tag_and_store)

    stats = asyncio.run(retag_module.run_retag(force=True, eligible_only=False))

    assert passes["n"] == 1, "force is a single pass, not a loop"
    assert stats["processed"] == 5


def test_the_default_path_still_terminates_on_an_empty_set(db):
    """Regression guard: the force break must not change the normal path."""
    import asyncio

    import workers.retag as retag_module

    stats = asyncio.run(retag_module.run_retag(eligible_only=False))

    assert stats["processed"] == 0


def test_retag_job_surfaces_the_breaker_not_an_earlier_batch_error(db, monkeypatch):
    """The production job stopped for requeues but displayed content blocking."""
    import asyncio

    import workers.retag as retag_module
    import workers.tagging_common as tagging_common

    async def _fake_tag_and_store(*args, **kwargs):
        return {
            "processed": 0,
            "failed": 0,
            "tags_written": 0,
            "off_vocabulary": 0,
            "unresolved_tags": 0,
            "extra_observations": 0,
            "requeued": 1,
            "missing_results": 1,
            "low_tag_results": 0,
            "blocked": 0,
            "input_tokens": 100,
            "output_tokens": 100,
            "api_calls": 1,
            "api_attempts": 1,
            "cached_tokens": 0,
            "thinking_tokens": 0,
            "cost_usd": 0.01,
            "estimated_usage": False,
            "pricing_basis": "test",
            "first_error": "GeminiContentBlocked: PROHIBITED_CONTENT",
            "halt_reason": "Halted: requeue rate 10.1% exceeded 10.0%",
            "halted": True,
        }

    monkeypatch.setattr(tagging_common, "tag_and_store", _fake_tag_and_store)
    monkeypatch.setattr(
        retag_module, "build_features", lambda row: {"tmdb_id": row.tmdb_id}
    )

    asyncio.run(retag_module.run_retag(force=True, eligible_only=False, limit=1))

    job = db.query(TaggingJob).order_by(TaggingJob.id.desc()).first()
    assert job.status == "failed"
    assert job.error_message == "Halted: requeue rate 10.1% exceeded 10.0%"
    assert job.job_metadata["first_batch_error"].startswith("GeminiContentBlocked")
    assert job.job_metadata["missing_results"] == 1


# ---------------------------------------------------------------------------
# The ceiling
# ---------------------------------------------------------------------------
def test_the_ceiling_measures_the_run_not_a_single_pass(db, monkeypatch):
    """``tag_and_store`` must refuse work once the RUN total reaches the ceiling.

    Before this, ``spent_usd`` did not exist and the guard read a per-call
    ``stats`` dict, so a caller looping over many calls had no spend guard at
    all -- each call sat far below the ceiling forever.
    """
    import asyncio

    from workers.tagging_common import tag_and_store

    monkeypatch.setattr(settings, "retag_cost_ceiling_usd", 10.0, raising=False)

    calls = {"n": 0}

    class _Engine:
        async def tag_items_raw(self, *a, **kw):
            calls["n"] += 1
            return [], {"input_tokens": 0, "output_tokens": 0}

    features = [
        {
            "tmdb_id": i,
            "media_type": "movie",
            "title": f"M{i}",
            "overview": "A cerebral mystery unfolds around a family.",
            "genres": ["Drama"],
            "release_date": "2020-01-01",
        }
        for i in range(1, 6)
    ]

    # Already at the ceiling before this call starts: no batch may run.
    asyncio.run(
        tag_and_store(db, features, engine=_Engine(), spent_usd=10.5, batch_size=1)
    )

    assert calls["n"] == 0, "the run had already reached the ceiling"


def test_below_the_ceiling_work_still_runs(db, monkeypatch):
    from workers.tagging_common import tag_and_store
    import asyncio

    monkeypatch.setattr(settings, "retag_cost_ceiling_usd", 10.0, raising=False)

    calls = {"n": 0}

    class _Engine:
        async def tag_items_raw(self, *a, **kw):
            calls["n"] += 1
            return [], {"input_tokens": 0, "output_tokens": 0}

    features = [
        {
            "tmdb_id": i,
            "media_type": "movie",
            "title": f"M{i}",
            "overview": "A cerebral mystery unfolds around a family.",
            "genres": ["Drama"],
            "release_date": "2020-01-01",
        }
        for i in range(1, 4)
    ]

    asyncio.run(
        tag_and_store(db, features, engine=_Engine(), spent_usd=0.0, batch_size=1)
    )

    assert calls["n"] > 0


def test_spent_usd_defaults_to_zero_so_existing_callers_are_unchanged(db):
    """The parameter is additive: a caller that does not pass it behaves as before."""
    import inspect

    from workers.tagging_common import tag_and_store

    assert inspect.signature(tag_and_store).parameters["spent_usd"].default == 0.0


def test_missing_synopsis_is_terminal_without_an_api_call(db):
    import asyncio

    from workers.tagging_common import tag_and_store, tagger_version

    calls = {"n": 0}

    class _Engine:
        async def tag_items_raw(self, *args, **kwargs):
            calls["n"] += 1
            raise AssertionError("an evidence-poor title reached the provider")

    stats = asyncio.run(
        tag_and_store(
            db,
            [{"tmdb_id": 1, "media_type": "movie", "title": "No Synopsis"}],
            engine=_Engine(),
        )
    )

    row = db.query(MediaMetadata).filter_by(tmdb_id=1, media_type="movie").one()
    assert calls["n"] == 0
    assert stats["evidence_skips"] == 1
    assert stats["api_calls"] == 0
    assert row.tag_attempt_version == tagger_version()
    assert row.tag_attempt_status == "insufficient_source_evidence"


def test_model_omissions_do_not_fail_deterministically_tagged_titles(db, monkeypatch):
    """An empty optional response cannot recreate the paid retry backlog."""
    import asyncio

    from workers.tagging_common import tag_and_store

    calls = {"n": 0}

    class _EmptyEngine:
        model_name = "gemini-2.5-flash-lite"

        async def tag_items_raw(self, *args, **kwargs):
            calls["n"] += 1
            return [], {
                "prompt_token_count": 100,
                "candidates_token_count": 10,
                "attempt_count": 1,
            }

    monkeypatch.setattr(settings, "gemini_concurrency", 1, raising=False)
    monkeypatch.setattr(settings, "tag_circuit_breaker_min_items", 2, raising=False)
    monkeypatch.setattr(settings, "tag_max_requeue_rate", 0.10, raising=False)
    features = [
        {
            "tmdb_id": i,
            "media_type": "movie",
            "title": f"M{i}",
            "overview": "A cerebral mystery unfolds around a family.",
            "genres": ["Drama"],
            "release_date": "2020-01-01",
        }
        for i in range(1, 11)
    ]

    stats = asyncio.run(
        tag_and_store(db, features, engine=_EmptyEngine(), batch_size=1)
    )

    assert stats["halted"] is False
    assert stats["processed"] == 10
    assert stats["requeued"] == 0
    assert stats["model_omissions"] == 10
    assert calls["n"] == 10
    assert stats["missing_retry_calls"] == 0


def test_small_healthy_pilot_is_not_stopped_by_normal_edge_cases(monkeypatch):
    """Six difficult titles in 200 is evidence to watch, not a broken run."""
    from workers.tagging_common import _circuit_breaker_reason

    monkeypatch.setattr(settings, "tag_circuit_breaker_min_items", 1000, raising=False)
    monkeypatch.setattr(settings, "tag_max_requeue_rate", 0.05, raising=False)
    monkeypatch.setattr(settings, "tag_catastrophic_min_items", 200, raising=False)
    monkeypatch.setattr(settings, "tag_catastrophic_rate", 0.20, raising=False)

    stats = {"processed": 194, "failed": 0, "requeued": 6}

    assert _circuit_breaker_reason(stats) == ""


def test_steady_breaker_tolerates_healthy_long_tail_variance(monkeypatch):
    """A sparse catalog segment must not discard a healthy full-run job."""
    from workers.tagging_common import _circuit_breaker_reason

    monkeypatch.setattr(settings, "tag_circuit_breaker_min_items", 1000, raising=False)
    monkeypatch.setattr(settings, "tag_max_requeue_rate", 0.10, raising=False)
    monkeypatch.setattr(settings, "tag_catastrophic_min_items", 200, raising=False)
    monkeypatch.setattr(settings, "tag_catastrophic_rate", 0.20, raising=False)

    # Production job 210: the 1,036 accepted outputs averaged 11 valid tags,
    # while 60 safely skipped titles made the old 5% threshold false-trigger.
    stats = {"processed": 1036, "failed": 0, "requeued": 60}

    assert _circuit_breaker_reason(stats) == ""


def test_default_steady_requeue_breaker_allows_catalog_strata_variance():
    assert type(settings).model_fields["tag_max_requeue_rate"].default == 0.10


def test_a_terminal_retry_backlog_does_not_trip_the_missing_result_breaker(
    monkeypatch,
):
    """Job 213 retried old sparse titles first; they are not a schema outage."""
    from workers.tagging_common import _circuit_breaker_reason

    monkeypatch.setattr(settings, "tag_catastrophic_min_items", 200, raising=False)
    monkeypatch.setattr(settings, "tag_catastrophic_rate", 0.20, raising=False)

    stats = {
        "processed": 145,
        "failed": 0,
        "requeued": 121,
        "missing_results": 3,
        "low_tag_results": 118,
        "blocked": 0,
    }

    assert _circuit_breaker_reason(stats) == ""


def test_an_all_low_tag_regression_still_trips_the_emergency_fuse(monkeypatch):
    from workers.tagging_common import _circuit_breaker_reason

    monkeypatch.setattr(settings, "tag_catastrophic_min_items", 200, raising=False)
    stats = {
        "processed": 0,
        "failed": 0,
        "requeued": 200,
        "missing_results": 0,
        "low_tag_results": 200,
        "blocked": 0,
    }

    reason = _circuit_breaker_reason(stats)
    assert "catastrophic low-tag rate 100.0%" in reason


def test_early_breaker_still_catches_a_catastrophic_regression(monkeypatch):
    """The longer sampling window must not turn spend protection off."""
    from workers.tagging_common import _circuit_breaker_reason

    monkeypatch.setattr(settings, "tag_circuit_breaker_min_items", 1000, raising=False)
    monkeypatch.setattr(settings, "tag_catastrophic_min_items", 200, raising=False)
    monkeypatch.setattr(settings, "tag_catastrophic_rate", 0.20, raising=False)

    stats = {"processed": 159, "failed": 0, "requeued": 41}

    reason = _circuit_breaker_reason(stats)
    assert "catastrophic requeue rate 20.5%" in reason
    assert "200 attempted titles" in reason


def test_halt_reason_is_not_masked_by_an_earlier_batch_error():
    """Admin Center must show why work stopped, not the first tolerated anomaly."""
    from workers.tagging_common import _latch_halt

    stats = {
        "halted": False,
        "first_error": "GeminiContentBlocked: PROHIBITED_CONTENT",
        "halt_reason": "",
    }

    _latch_halt(stats, "Halted: requeue rate 10.1% exceeded 10.0%")

    assert stats["halted"] is True
    assert stats["halt_reason"] == "Halted: requeue rate 10.1% exceeded 10.0%"
    assert stats["first_error"] == "GeminiContentBlocked: PROHIBITED_CONTENT"


def test_optional_model_anomalies_are_observed_without_requeue(db, monkeypatch):
    """Bad nuance is recorded while exact local facts still publish."""
    import asyncio

    from workers.tagging_common import tag_and_store

    constraints = []

    class _MixedEngine:
        model_name = "gemini-2.5-flash-lite"

        async def tag_items_raw(self, *args, **kwargs):
            constraints.append(kwargs)
            return [
                {
                    "tmdb_id": 1,
                    "tags": [{"name": "Heist", "confidence": 0.9}],
                    "extra": ["Dream Logic"],
                },
                {
                    "tmdb_id": 2,
                    "tags": [{"name": "Invented Tag", "confidence": 0.9}],
                },
            ], {
                "prompt_token_count": 100,
                "candidates_token_count": 100,
                "attempt_count": 1,
            }

    monkeypatch.setattr(settings, "gemini_concurrency", 1, raising=False)
    prior = db.query(MediaMetadata).filter_by(tmdb_id=1, media_type="movie").one()
    prior.tag_attempt_version = "prior-forced-attempt"
    prior.tag_attempt_status = "too_few_valid_tags"
    prior.tag_attempted_at = utc_now()
    db.commit()
    features = [
        {
            "tmdb_id": i,
            "media_type": "movie",
            "title": f"M{i}",
            "overview": "A suspenseful thief plans and executes an elaborate heist.",
            "genres": ["Crime"],
            "release_date": "2020-01-01",
            "_tag_attempt_version": "prior-forced-attempt" if i == 1 else None,
        }
        for i in range(1, 4)
    ]

    stats = asyncio.run(
        tag_and_store(db, features, engine=_MixedEngine(), batch_size=3)
    )

    assert stats["processed"] == 3
    assert stats["requeued"] == 0
    assert stats["terminal_skips"] == 0
    assert stats["low_tag_results"] == 0
    assert stats["missing_results"] == 0
    assert stats["model_omissions"] == 1
    assert stats["missing_retry_calls"] == 0
    assert stats["recovered_missing_results"] == 0
    assert stats["unresolved_tags"] == 1
    assert stats["extra_observations"] == 1
    assert stats["off_vocabulary"] == 2
    assert stats["low_tag_retry_calls"] == 0
    assert [call["expected_ids"] for call in constraints] == [[1, 2, 3]]
    assert "Suspenseful" in constraints[0]["allowed_tags"]
    assert "Heist" not in constraints[0]["allowed_tags"]
    assert "Slow-Burn" not in constraints[0]["allowed_tags"]
    db.expire_all()
    attempts = {
        row.tmdb_id: row.tag_attempt_status
        for row in db.query(MediaMetadata)
        .filter(MediaMetadata.tmdb_id.in_([1, 2, 3]))
        .all()
    }
    assert attempts[1] is None
    assert attempts[2] is None
    assert attempts[3] is None


def test_missing_model_result_is_not_retried(db, monkeypatch):
    import asyncio

    from workers.tagging_common import tag_and_store

    calls = 0

    class _RecoveryEngine:
        model_name = "gemini-2.5-flash-lite"

        async def tag_items_raw(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            ids = kwargs["expected_ids"]
            returned = ids[:1]
            return [
                {
                    "tmdb_id": tmdb_id,
                    "tags": [{"name": "Suspenseful", "confidence": 0.9}],
                }
                for tmdb_id in returned
            ], {
                "prompt_token_count": 100,
                "candidates_token_count": 100,
                "attempt_count": 1,
            }

    monkeypatch.setattr(settings, "gemini_concurrency", 1, raising=False)
    features = [
        {
            "tmdb_id": tmdb_id,
            "media_type": "movie",
            "title": f"M{tmdb_id}",
            "overview": "A suspenseful thief plans and executes an elaborate heist.",
            "genres": ["Crime"],
            "release_date": "2020-01-01",
        }
        for tmdb_id in (1, 2)
    ]

    stats = asyncio.run(
        tag_and_store(db, features, engine=_RecoveryEngine(), batch_size=2)
    )

    assert calls == 1
    assert stats["processed"] == 2
    assert stats["missing_results"] == 0
    assert stats["model_omissions"] == 1
    assert stats["missing_retry_calls"] == 0
    assert stats["recovered_missing_results"] == 0


def test_concurrent_model_omissions_do_not_latch_a_breaker(db, monkeypatch):
    import asyncio

    import workers.tagging_common as tagging_common

    class _EmptyEngine:
        model_name = "gemini-2.5-flash-lite"

        async def tag_items_raw(self, *args, **kwargs):
            return [], {
                "prompt_token_count": 100,
                "candidates_token_count": 10,
                "attempt_count": 1,
            }

    errors = []
    monkeypatch.setattr(settings, "gemini_concurrency", 4, raising=False)
    monkeypatch.setattr(settings, "tag_circuit_breaker_min_items", 2, raising=False)
    monkeypatch.setattr(settings, "tag_max_requeue_rate", 0.10, raising=False)
    monkeypatch.setattr(settings, "tag_catastrophic_min_items", 200, raising=False)
    monkeypatch.setattr(tagging_common.logger, "error", errors.append)
    features = [
        {
            "tmdb_id": i,
            "media_type": "movie",
            "title": f"M{i}",
            "overview": "A cerebral mystery unfolds around a family.",
            "genres": ["Drama"],
            "release_date": "2020-01-01",
        }
        for i in range(1, 11)
    ]

    stats = asyncio.run(
        tagging_common.tag_and_store(db, features, engine=_EmptyEngine(), batch_size=1)
    )

    assert stats["halted"] is False
    assert stats["processed"] == 10
    assert stats["model_omissions"] == 10
    assert errors == []


# ---------------------------------------------------------------------------
# Scoped selection
# ---------------------------------------------------------------------------
def test_an_id_list_restricts_the_selection(db):
    """What makes a validation pass affordable.

    Promoting an alias is not retroactive and cannot be scoped precisely from
    what we store, so the choice used to be a whole-library re-tag (~$25) or
    nothing. Re-tagging the recorded samples costs cents and answers the only
    question that matters first: did the promotion change the output?
    """
    from workers.retag import _pending_query

    assert (
        _pending_query(db, None, 0, True, tmdb_ids=[1, 3], eligible_only=False).count()
        == 2
    )
    assert {
        r.tmdb_id
        for r in _pending_query(
            db, None, 0, True, tmdb_ids=[1, 3], eligible_only=False
        ).all()
    } == {1, 3}


def test_terminal_attempts_leave_the_normal_pending_set(db):
    from workers.retag import _pending_query
    from workers.tagging_common import tagger_version

    db.query(MovieTag).filter_by(tmdb_id=1, media_type="movie").delete()
    row = db.query(MediaMetadata).filter_by(tmdb_id=1, media_type="movie").one()
    row.tag_attempt_version = tagger_version()
    row.tag_attempt_status = "too_few_valid_tags"
    row.tag_attempted_at = utc_now()
    db.commit()

    normal_ids = {
        item.tmdb_id
        for item in _pending_query(db, None, 0, False, eligible_only=False).all()
    }
    forced_ids = {
        item.tmdb_id
        for item in _pending_query(db, None, 0, True, eligible_only=False).all()
    }

    assert 1 not in normal_ids
    assert 1 in forced_ids


def test_an_empty_id_list_means_nothing_not_everything(db):
    """A caller that computed an empty set must get an empty run.

    Treating [] as "no filter" would turn a scoped request that happened to
    match nothing into a full-library re-tag -- the exact accident this
    parameter exists to prevent.
    """
    from workers.retag import _pending_query

    assert (
        _pending_query(db, None, 0, True, tmdb_ids=[], eligible_only=False).count() == 0
    )


def test_no_id_list_leaves_the_selection_alone(db):
    from workers.retag import _pending_query

    assert (
        _pending_query(db, None, 0, True, tmdb_ids=None, eligible_only=False).count()
        == 5
    )


def test_a_scoped_run_tags_only_those_titles(db, monkeypatch):
    import asyncio

    import workers.retag as retag_module
    import workers.tagging_common as tagging_common

    seen: list[int] = []

    async def _fake_tag_and_store(db_, features, **kwargs):
        seen.extend(f["tmdb_id"] for f in features)
        return {
            "processed": len(features),
            "failed": 0,
            "tags_written": 0,
            "off_vocabulary": 0,
            "requeued": 0,
            "blocked": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "api_calls": 1,
            "cost_usd": 0.0,
            "estimated_usage": False,
            "first_error": "",
        }

    monkeypatch.setattr(tagging_common, "tag_and_store", _fake_tag_and_store)
    monkeypatch.setattr(
        retag_module, "build_features", lambda row: {"tmdb_id": row.tmdb_id}
    )

    asyncio.run(
        retag_module.run_retag(force=True, tmdb_ids=[2, 4], eligible_only=False)
    )

    assert sorted(seen) == [2, 4]


def test_the_estimate_can_be_asked_about_a_scoped_run(db):
    """The cost of what you are about to do, before you do it."""
    from workers.retag import estimate

    scoped = estimate(force=True, tmdb_ids=[1, 2], eligible_only=False)
    whole = estimate(force=True, eligible_only=False)

    assert scoped["pending"] == 2
    assert whole["pending"] == 5
    assert scoped["est_cost_usd"] <= whole["est_cost_usd"]


def test_the_estimate_honours_the_same_limit_as_start(db):
    """The pilot estimate must price the pilot, not the whole pending corpus."""
    from workers.retag import estimate

    report = estimate(force=True, eligible_only=False, limit=2)

    assert report["pending"] == 2
    assert report["requests"] == 1


def test_request_estimate_allows_for_clustered_partial_batches():
    """Perfect packing underreported the production pilot by 67%."""
    from workers.retag import _estimated_requests

    assert _estimated_requests(5000, 20) == 417
    assert _estimated_requests(5000, 10) == 500
    assert _estimated_requests(0, 20) == 0


def test_v5_cost_estimate_prices_only_likely_ai_admissions():
    from workers.retag import estimated_ai_titles

    assert estimated_ai_titles(1000) == 600
    assert estimated_ai_titles(3) == 2
    assert estimated_ai_titles(0) == 0
