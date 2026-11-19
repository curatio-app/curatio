"""
Regression tests for the four ways the Upgrade button could hurt a live install.

Each of these pins a defect that was found by tracing the runtime path rather
than by a failing test, which is why they are written as "this must not happen
again" rather than as feature coverage:

* the pause gate deadlocking the web server,
* the re-tag query dragging ``raw_data`` into memory,
* Stop being ignored for the length of the longest step,
* a model fixed in the admin UI not reaching the client that makes the calls.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_upgrade_safety.db")
os.environ.setdefault("TMDB_API_KEY", "test")
os.environ.setdefault("GEMINI_API_KEY", "test")
os.environ.setdefault("MASTER_PASSWORD", "test")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SECRET_KEY", "t" * 40)
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.models import MediaMetadata  # noqa: E402
from workers import build_control  # noqa: E402

# ---- the deadlock ----


def test_pause_gate_blocks_its_own_thread_not_the_caller():
    """``check_pause`` must never be awaited on the main event loop.

    It parks on ``threading.Event.wait()`` with no timeout. On a worker thread
    that is correct and cheap. On the event loop it parks the loop itself, so
    the Resume request that would release it can never be read off the socket --
    an unrecoverable freeze of the whole server.

    This asserts the property that makes the worker-thread placement safe: a
    blocked pause gate leaves *other* work running, and setting the event from
    outside releases it.
    """
    build_control.pause_event.clear()
    released = threading.Event()

    def worker():
        asyncio.run(build_control.check_pause())
        released.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        # The gate is holding that thread, and only that thread.
        assert not released.wait(timeout=0.3)

        # The caller is still live enough to release it -- which is exactly what
        # the admin Resume endpoint does.
        build_control.pause_event.set()
        assert released.wait(timeout=3), "the pause gate never released"
    finally:
        build_control.pause_event.set()
        thread.join(timeout=3)


def test_request_build_stop_wakes_a_parked_pause_gate():
    """Stop must not be swallowed by a paused job."""
    build_control.reset_build_stop()
    build_control.pause_event.clear()
    released = threading.Event()

    def worker():
        asyncio.run(build_control.check_pause())
        released.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        assert not released.wait(timeout=0.2)
        build_control.request_build_stop()
        assert released.wait(timeout=3), "stop did not wake the pause gate"
        assert build_control.stop_event.is_set()
    finally:
        build_control.reset_build_stop()
        build_control.pause_event.set()
        thread.join(timeout=3)


# ---- the memory bug ----


def test_retag_query_does_not_select_raw_data():
    """``raw_data`` is ~5KB per row after enrichment and is never read here.

    Selecting it pulled roughly a gigabyte of JSON into memory in one
    synchronous ``.all()``. Compile the statement and assert the column is
    absent, the same technique ``tests/test_relevance.py`` uses for the
    portability guarantees.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from workers.retag import _pending_query

    engine = create_engine("sqlite://")
    session = sessionmaker(bind=engine)()
    try:
        sql = str(_pending_query(session).statement.compile(engine))
    finally:
        session.close()

    assert "raw_data" not in sql, (
        "the re-tag query selects raw_data again — that is ~1GB of JSON "
        f"deserialized for nothing:\n{sql}"
    )
    # The columns it genuinely needs must still be there.
    assert "overview" in sql


def test_card_and_search_queries_do_not_select_raw_data():
    """Same guarantee for the two request-path reads, which are hotter.

    ``_MEDIA_CARD_COLUMNS`` backs the bootstrap activity collections and browse;
    ``_SEARCH_ROW_COLUMNS`` backs title search, which reads up to 200 rows. Search
    consults ``raw_data`` only to test for three keys, and now does that with a
    separate query over the few rows that can still change answer, so neither
    whitelist may start pulling the blob back in.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import load_only, sessionmaker

    from app.main import _MEDIA_CARD_COLUMNS, _SEARCH_ROW_COLUMNS
    from app.models import MediaMetadata

    engine = create_engine("sqlite://")
    session = sessionmaker(bind=engine)()
    try:
        for label, columns in (
            ("card", _MEDIA_CARD_COLUMNS),
            ("search", _SEARCH_ROW_COLUMNS),
        ):
            sql = str(
                session.query(MediaMetadata)
                .options(load_only(*columns))
                .statement.compile(engine)
            )
            assert "raw_data" not in sql, f"the {label} query selects raw_data:\n{sql}"
            # The columns these paths genuinely need must still be there.
            assert "overview" in sql
            assert "poster_path" in sql
        # Search additionally ranks and classifies on these.
        search_sql = str(
            session.query(MediaMetadata)
            .options(load_only(*_SEARCH_ROW_COLUMNS))
            .statement.compile(engine)
        )
        for column in ("popularity", "vote_count", "rating_consensus"):
            assert column in search_sql
    finally:
        session.close()
    assert "tag_facts" in sql


def test_retag_can_restrict_to_enriched_titles():
    """The predicate that lets tagging trail enrichment instead of following it."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from workers.retag import _pending_query

    engine = create_engine("sqlite://")
    session = sessionmaker(bind=engine)()
    try:
        plain = str(_pending_query(session).statement.compile(engine))
        gated = str(
            _pending_query(session, enriched_only=True).statement.compile(engine)
        )
    finally:
        session.close()

    # enrich_version is a column of MediaMetadata so it appears in the SELECT
    # list either way; what distinguishes the modes is the WHERE predicate.
    predicate = "media_metadata.enrich_version = "
    assert predicate not in plain
    assert predicate in gated


# ---- repairing work done before a source was fixed ----


def test_stale_facts_selects_tags_that_predate_their_mdb_data():
    """The precise answer to "re-tag what the broken enrichment affected".

    A title tagged before its MDBList facts arrived carries tags that never saw
    the reviews. No version bump finds it -- the taxonomy did not change, only
    the inputs did -- so the selection is a timestamp comparison.
    """
    from datetime import datetime, timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models import Base, MovieTag, Tag
    from workers.retag import _pending_query
    from workers.tagging_common import tagger_version

    engine = create_engine(
        "sqlite:///./test_stale_facts.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()

    early = datetime(2026, 1, 1)
    late = early + timedelta(days=1)
    version = tagger_version()

    try:
        tag = Tag(name="_probe", category="mood")
        session.add(tag)
        session.flush()

        def add(tmdb_id, tagged_at, mdb_at):
            session.add(
                MediaMetadata(
                    tmdb_id=tmdb_id,
                    media_type="movie",
                    title=f"t{tmdb_id}",
                    mdb_enriched_at=mdb_at,
                )
            )
            session.add(
                MovieTag(
                    tmdb_id=tmdb_id,
                    media_type="movie",
                    tag_id=tag.id,
                    confidence=0.9,
                    tagged_at=tagged_at,
                    tagger_version=version,
                )
            )

        add(1, tagged_at=early, mdb_at=late)  # stale: tagged before the facts
        add(2, tagged_at=late, mdb_at=early)  # fine: tagged after
        add(3, tagged_at=early, mdb_at=None)  # no mdb data at all -- not stale
        session.commit()

        ids = {
            row.tmdb_id
            for row in _pending_query(
                session, stale_facts=True, eligible_only=False
            ).all()
        }
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)

    assert ids == {1}, (
        "stale_facts must select exactly the titles tagged before their MDBList "
        f"data landed, not the whole library; got {ids}"
    )


def test_stale_facts_replaces_every_row_for_a_title():
    """These titles are already at the current version.

    The normal swap only deletes rows from a *different* version, and the upsert
    only overwrites tag_ids that recur -- so a tag the model no longer assigns
    would survive forever. Re-tagging on changed inputs has to clear the lot.
    """
    import inspect

    from workers import tagging_common

    source = inspect.getsource(tagging_common._tag_one_batch)
    assert "if not replace_all:" in source, (
        "the version predicate must be skipped when replace_all is set, or "
        "superseded tags outlive the re-tag"
    )


# ---- stop actually stopping ----


@pytest.mark.asyncio
async def test_enrich_stops_between_chunks(monkeypatch):
    """Stop used to be ignored for the whole ~5h enrichment step.

    The flag was only read between pipeline steps, so the confirm dialog's
    promise that "you can stop at any point" was false for the longest step in
    the run.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import workers.enrich as enrich_worker
    from app.models import Base

    engine = create_engine(
        "sqlite:///./test_upgrade_safety_enrich.db",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    for i in range(5):
        session.add(MediaMetadata(tmdb_id=i + 1, media_type="movie", title=f"t{i}"))
    session.commit()

    class _Ctx:
        def __enter__(self):
            return session

        def __exit__(self, *exc):
            session.commit()
            return False

    monkeypatch.setattr(enrich_worker, "get_db", lambda: _Ctx())
    monkeypatch.setattr(enrich_worker.settings, "enrich_batch_size", 1)

    fetched: list[int] = []

    async def _fake_movie(tmdb_id):
        fetched.append(tmdb_id)
        # Ask to stop as soon as the first title is in hand.
        build_control.request_build_stop()
        return {"id": tmdb_id, "credits": {"cast": [], "crew": []}, "genres": []}

    monkeypatch.setattr(enrich_worker.tmdb_client, "get_movie", _fake_movie)

    build_control.reset_build_stop()
    try:
        stats = await enrich_worker.enrich()
    finally:
        build_control.reset_build_stop()
        session.close()
        Base.metadata.drop_all(bind=engine)

    assert stats["stopped"] is True
    assert (
        len(fetched) < 5
    ), f"enrich ignored the stop flag and fetched all {len(fetched)} titles"


@pytest.mark.asyncio
async def test_enrich_repairs_canonical_metadata_from_a_stored_detail(monkeypatch):
    """A local detail payload should fix dates without another TMDB request."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import workers.enrich as enrich_worker
    from app.models import Base

    engine = create_engine(
        "sqlite:///./test_upgrade_safety_enrich.db",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    payload = {
        "id": 603,
        "title": "The Matrix",
        "original_title": "The Matrix",
        "release_date": "1999-03-30",
        "overview": "A hacker learns the truth.",
        "genres": [{"id": 878, "name": "Science Fiction"}],
        "vote_average": 8.2,
        "vote_count": 28000,
        "credits": {"cast": [], "crew": []},
        "keywords": {"keywords": []},
    }
    session.add(
        MediaMetadata(
            tmdb_id=603,
            media_type="movie",
            title="Wrong title",
            release_date="2018-01-01",
            vote_average=4.0,
            vote_count=10,
            raw_data=payload,
            enrich_version="old",
        )
    )
    session.commit()

    class _Ctx:
        def __enter__(self):
            return session

        def __exit__(self, *exc):
            session.commit()
            return False

    monkeypatch.setattr(enrich_worker, "get_db", lambda: _Ctx())
    monkeypatch.setattr(enrich_worker.settings, "enrich_batch_size", 10)
    build_control.reset_build_stop()
    try:
        stats = await enrich_worker.enrich()
        session.expire_all()
        repaired = session.get(MediaMetadata, (603, "movie"))
        assert repaired is not None
        assert repaired.title == "The Matrix"
        assert repaired.release_date == "1999-03-30"
        assert repaired.vote_average == 8.2
        assert repaired.vote_count == 28000
        assert stats["reused"] == 1
    finally:
        build_control.reset_build_stop()
        session.close()
        Base.metadata.drop_all(bind=engine)


# ---- the model that both screens lie about ----


def test_changing_the_model_reconfigures_the_client():
    """Mutating settings alone used to leave every API call on the old model.

    Both admin screens read the settings object, so an operator who fixed a
    retired model in the UI saw the new value everywhere while the client kept
    404ing until a restart.
    """
    from app.gemini_client import gemini_engine

    original = gemini_engine.model_name
    try:
        gemini_engine.reconfigure(model_name="gemini-probe-model")
        assert gemini_engine.model_name == "gemini-probe-model"
    finally:
        gemini_engine.model_name = original


def test_apply_setting_propagates_the_model_to_the_client(monkeypatch):
    """The admin write path, not just the engine method."""
    from app.admin import _apply_setting
    from app.config import settings
    from app.gemini_client import gemini_engine

    original_setting = settings.gemini_model
    original_model = gemini_engine.model_name
    try:
        _apply_setting("GEMINI_MODEL", "gemini-probe-2")
        assert settings.gemini_model == "gemini-probe-2"
        assert gemini_engine.model_name == "gemini-probe-2", (
            "settings changed but the client kept the old model — the exact "
            "shape of the original two-month outage"
        )
    finally:
        object.__setattr__(settings, "gemini_model", original_setting)
        gemini_engine.model_name = original_model
