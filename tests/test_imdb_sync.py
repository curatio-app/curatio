# ruff: noqa: E402
"""Tests for IMDb-keyed (``tt...``) watch progress / scrobble / library sync.

Covers the new optional ``imdb_id`` contract: imdb-only writes are accepted,
canonicalized to a TMDB id when resolvable (and stored imdb-only otherwise),
deduplicated against their tmdb twin, and echoed back on reads — all without
changing the behaviour of existing tmdb-only traffic.
"""

import os
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_imdb_sync.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

import app.main as main  # noqa: E402
from app.database import get_db_dependency  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    Base,
    ImdbResolution,
    LibraryItem,
    MediaMetadata,
    User,
    WatchEvent,
    WatchProgress,
)

engine = create_engine(
    "sqlite:///./test_imdb_sync.db", connect_args={"check_same_thread": False}
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@contextmanager
def use_test_db(db):
    """Stand-in for app.main.get_db: commit-on-success, no close (test keeps it)."""
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def _bearer(user_key):
    """Profile credential header for the native /api/v2 API."""
    return {"Authorization": f"Bearer {user_key}"}


def make_client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    main._user_cache.clear()

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
    main._user_cache.clear()
    Base.metadata.drop_all(bind=engine)


def _seed_profile(db, user_key="imdb-key"):
    account = Account(auth_source="local", email="owner@example.com", is_active=True)
    db.add(account)
    db.commit()
    user = User(
        user_key=user_key,
        account_id=account.id,
        auth_source="local",
        display_name="Viewer",
        is_active=True,
    )
    db.add(user)
    db.commit()
    return user


# TMDB /find payloads keyed by the entity an imdb id points at.
FIND_MOVIE = {
    "movie_results": [{"id": 603}],
    "tv_results": [],
    "tv_episode_results": [],
}
FIND_TV = {"movie_results": [], "tv_results": [{"id": 1399}], "tv_episode_results": []}
FIND_EPISODE = {
    "movie_results": [],
    "tv_results": [],
    "tv_episode_results": [{"show_id": 1399, "season_number": 2, "episode_number": 5}],
}
FIND_NONE = {"movie_results": [], "tv_results": [], "tv_episode_results": []}


@contextmanager
def _patched(db, find_return=FIND_NONE):
    find_mock = AsyncMock(return_value=find_return)
    with patch("app.main.get_db", side_effect=lambda: use_test_db(db)), patch(
        "app.tmdb_client.tmdb_client.find_by_imdb_id", new=find_mock
    ), patch("app.main._schedule_local_sync"):
        yield find_mock


# --------------------------------------------------------------------------- #
# Validation                                                                    #
# --------------------------------------------------------------------------- #


def test_progress_requires_an_id():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        with _patched(db):
            resp = client.post(
                "/api/v2/progress",
                headers=_bearer(user.user_key),
                json={"media_type": "movie", "position": 10, "duration": 100},
            )
        assert resp.status_code == 400
    finally:
        cleanup(db)


# --------------------------------------------------------------------------- #
# Progress: imdb resolution + imdb-only fallback                                #
# --------------------------------------------------------------------------- #


def test_progress_imdb_only_resolvable_is_canonicalized():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        with _patched(db, FIND_MOVIE) as find_mock:
            resp = client.post(
                "/api/v2/progress",
                headers=_bearer(user.user_key),
                json={
                    "imdb_id": "tt0133093",
                    "media_type": "movie",
                    "position": 100,
                    "duration": 1000,
                },
            )
        assert resp.status_code == 200, resp.text
        rows = db.query(WatchProgress).filter(WatchProgress.user_id == user.id).all()
        assert len(rows) == 1
        assert rows[0].tmdb_id == 603  # canonicalized
        assert rows[0].imdb_id == "tt0133093"  # originating id retained
        find_mock.assert_awaited_once()
        # Resolution was cached persistently.
        cached = db.query(ImdbResolution).filter_by(imdb_id="tt0133093").one()
        assert cached.status == "found" and cached.tmdb_id == 603
    finally:
        cleanup(db)


def test_progress_imdb_only_unresolvable_is_stored_imdb_only():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        with _patched(db, FIND_NONE):
            resp = client.post(
                "/api/v2/progress",
                headers=_bearer(user.user_key),
                json={
                    "imdb_id": "tt9999999",
                    "media_type": "movie",
                    "position": 100,
                    "duration": 1000,
                },
            )
        assert resp.status_code == 200, resp.text
        row = db.query(WatchProgress).filter(WatchProgress.user_id == user.id).one()
        assert row.tmdb_id is None
        assert row.imdb_id == "tt9999999"
    finally:
        cleanup(db)


def test_progress_episode_imdb_maps_to_series_and_episode():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        with _patched(db, FIND_EPISODE):
            resp = client.post(
                "/api/v2/progress",
                headers=_bearer(user.user_key),
                json={
                    "imdb_id": "tt1234567",
                    "media_type": "tv",
                    "position": 100,
                    "duration": 1000,
                },
            )
        assert resp.status_code == 200, resp.text
        row = db.query(WatchProgress).filter(WatchProgress.user_id == user.id).one()
        assert row.tmdb_id == 1399  # the series tmdb id
        assert row.media_type == "tv"
        assert row.season == 2 and row.episode == 5
    finally:
        cleanup(db)


def test_progress_resolution_cached_resolves_once_per_imdb_id():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        with _patched(db, FIND_MOVIE) as find_mock:
            for pos in (100, 200):
                client.post(
                    "/api/v2/progress",
                    headers=_bearer(user.user_key),
                    json={
                        "imdb_id": "tt0133093",
                        "media_type": "movie",
                        "position": pos,
                        "duration": 1000,
                    },
                )
        # Second write reused the cached resolution — no extra TMDB call.
        assert find_mock.await_count == 1
        row = db.query(WatchProgress).filter(WatchProgress.user_id == user.id).one()
        assert row.position == 200
    finally:
        cleanup(db)


# --------------------------------------------------------------------------- #
# Dedup / merge                                                                 #
# --------------------------------------------------------------------------- #


def test_progress_tmdb_then_matching_imdb_collapses_to_one_row():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        with _patched(db, FIND_MOVIE):
            client.post(
                "/api/v2/progress",
                headers=_bearer(user.user_key),
                json={
                    "tmdb_id": 603,
                    "media_type": "movie",
                    "position": 100,
                    "duration": 1000,
                },
            )
            client.post(
                "/api/v2/progress",
                headers=_bearer(user.user_key),
                json={
                    "imdb_id": "tt0133093",
                    "media_type": "movie",
                    "position": 250,
                    "duration": 1000,
                },
            )
        rows = db.query(WatchProgress).filter(WatchProgress.user_id == user.id).all()
        assert len(rows) == 1
        assert rows[0].tmdb_id == 603
        assert rows[0].imdb_id == "tt0133093"
        assert rows[0].position == 250  # last write wins
    finally:
        cleanup(db)


def test_progress_merges_separate_tmdb_and_imdb_only_rows():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        # Row A: a tmdb-only row (e.g. from a legacy client).
        db.add(
            WatchProgress(
                user_id=user.id,
                tmdb_id=603,
                media_type="movie",
                season=0,
                episode=0,
                position=10,
                duration=1000,
                updated_at=datetime(2020, 1, 1),
            )
        )
        # Row B: an imdb-only row (resolution previously failed).
        db.add(
            WatchProgress(
                user_id=user.id,
                imdb_id="tt0133093",
                media_type="movie",
                season=0,
                episode=0,
                position=20,
                duration=1000,
                updated_at=datetime(2020, 1, 2),
            )
        )
        db.commit()
        assert db.query(WatchProgress).count() == 2

        # A new write that resolves to the same tmdb id AND carries the imdb id
        # should merge the two rows into one canonical row.
        with _patched(db, FIND_MOVIE):
            client.post(
                "/api/v2/progress",
                headers=_bearer(user.user_key),
                json={
                    "imdb_id": "tt0133093",
                    "media_type": "movie",
                    "position": 300,
                    "duration": 1000,
                },
            )
        rows = db.query(WatchProgress).filter(WatchProgress.user_id == user.id).all()
        assert len(rows) == 1
        assert rows[0].tmdb_id == 603
        assert rows[0].imdb_id == "tt0133093"
        assert rows[0].position == 300
    finally:
        cleanup(db)


# --------------------------------------------------------------------------- #
# Reads echo imdb_id                                                            #
# --------------------------------------------------------------------------- #


def test_history_echoes_imdb_id_for_imdb_only_and_via_metadata_fallback():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        # imdb-only row.
        db.add(
            WatchProgress(
                user_id=user.id,
                imdb_id="tt9999999",
                media_type="movie",
                season=0,
                episode=0,
                position=30,
                duration=1000,
                updated_at=datetime(2021, 1, 2),
            )
        )
        # tmdb-only row whose imdb_id is only known via the metadata cache.
        db.add(
            WatchProgress(
                user_id=user.id,
                tmdb_id=603,
                media_type="movie",
                season=0,
                episode=0,
                position=40,
                duration=1000,
                updated_at=datetime(2021, 1, 1),
            )
        )
        db.add(
            MediaMetadata(
                tmdb_id=603,
                media_type="movie",
                title="The Matrix",
                imdb_id="tt0133093",
            )
        )
        db.commit()

        with patch("app.main.get_db", side_effect=lambda: use_test_db(db)):
            resp = client.get("/api/v2/history", headers=_bearer(user.user_key))
        assert resp.status_code == 200, resp.text
        items = {i["tmdb_id"]: i for i in resp.json()["items"]}
        assert items[None]["imdb_id"] == "tt9999999"  # imdb-only row
        assert items[603]["imdb_id"] == "tt0133093"  # filled from metadata
    finally:
        cleanup(db)


# --------------------------------------------------------------------------- #
# Scrobble                                                                      #
# --------------------------------------------------------------------------- #


def test_scrobble_imdb_only_is_canonicalized():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        with _patched(db, FIND_MOVIE):
            resp = client.post(
                "/api/v2/scrobble",
                headers=_bearer(user.user_key),
                json={"imdb_id": "tt0133093", "media_type": "movie"},
            )
        assert resp.status_code == 200, resp.text
        event = db.query(WatchEvent).filter(WatchEvent.user_id == user.id).one()
        assert event.tmdb_id == 603
        assert event.imdb_id == "tt0133093"
    finally:
        cleanup(db)


def test_scrobble_requires_an_id():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        with _patched(db):
            resp = client.post(
                "/api/v2/scrobble",
                headers=_bearer(user.user_key),
                json={"media_type": "movie"},
            )
        assert resp.status_code == 400
    finally:
        cleanup(db)


# --------------------------------------------------------------------------- #
# Library                                                                       #
# --------------------------------------------------------------------------- #


def test_library_add_imdb_only_resolvable_and_get_echoes_imdb_id():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        with _patched(db, FIND_TV):
            add = client.post(
                "/api/v2/library",
                headers=_bearer(user.user_key),
                json={"imdb_id": "tt0903747", "media_type": "tv", "title": "BB"},
            )
        assert add.status_code == 200, add.text
        assert add.json()["added"] is True
        item = db.query(LibraryItem).filter(LibraryItem.user_id == user.id).one()
        assert item.tmdb_id == 1399 and item.imdb_id == "tt0903747"

        with patch("app.main.get_db", side_effect=lambda: use_test_db(db)):
            got = client.get("/api/v2/library", headers=_bearer(user.user_key))
        assert got.status_code == 200
        body = got.json()["items"]
        assert body[0]["imdb_id"] == "tt0903747"
        assert body[0]["tmdb_id"] == 1399
    finally:
        cleanup(db)


def test_library_add_tmdb_then_imdb_collapses_to_one_row():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        with _patched(db, FIND_TV):
            client.post(
                "/api/v2/library",
                headers=_bearer(user.user_key),
                json={"tmdb_id": 1399, "media_type": "tv"},
            )
            client.post(
                "/api/v2/library",
                headers=_bearer(user.user_key),
                json={"imdb_id": "tt0903747", "media_type": "tv"},
            )
        rows = db.query(LibraryItem).filter(LibraryItem.user_id == user.id).all()
        assert len(rows) == 1
        assert rows[0].tmdb_id == 1399 and rows[0].imdb_id == "tt0903747"
    finally:
        cleanup(db)


def test_library_delete_by_imdb_id():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        db.add(LibraryItem(user_id=user.id, imdb_id="tt9999999", media_type="movie"))
        db.commit()
        with patch("app.main.get_db", side_effect=lambda: use_test_db(db)):
            resp = client.delete(
                "/api/v2/library/movie/tt9999999", headers=_bearer(user.user_key)
            )
        assert resp.status_code == 200
        assert resp.json()["removed"] is True
        assert db.query(LibraryItem).filter(LibraryItem.user_id == user.id).count() == 0
    finally:
        cleanup(db)


def test_library_sync_with_mixed_ids():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        with _patched(db, FIND_MOVIE):
            resp = client.post(
                "/api/v2/library/sync",
                headers=_bearer(user.user_key),
                json={
                    "added": [
                        {"tmdb_id": 24428, "media_type": "movie", "title": "Avengers"},
                        {"imdb_id": "tt0133093", "media_type": "movie"},
                    ],
                    "removed": [],
                },
            )
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        ids = {(i["tmdb_id"], i["imdb_id"]) for i in items}
        assert (24428, None) in ids
        assert (603, "tt0133093") in ids  # imdb canonicalized in bulk sync
    finally:
        cleanup(db)


# --------------------------------------------------------------------------- #
# Backward compatibility (tmdb-only traffic unchanged)                          #
# --------------------------------------------------------------------------- #


def test_tmdb_only_progress_and_history_are_backward_compatible():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        with _patched(db) as find_mock:
            resp = client.post(
                "/api/v2/progress",
                headers=_bearer(user.user_key),
                json={
                    "tmdb_id": 603,
                    "media_type": "tv",
                    "season": 1,
                    "episode": 2,
                    "position": 100,
                    "duration": 1000,
                    "title": "Ep",
                },
            )
            assert resp.status_code == 200, resp.text
            # No TMDB resolution happens for tmdb-keyed writes.
            find_mock.assert_not_awaited()

            hist = client.get("/api/v2/history", headers=_bearer(user.user_key))
        row = db.query(WatchProgress).filter(WatchProgress.user_id == user.id).one()
        assert row.tmdb_id == 603 and row.imdb_id is None
        item = hist.json()["items"][0]
        assert item["tmdb_id"] == 603
        assert item["season"] == 1 and item["episode"] == 2
        assert item["imdb_id"] is None  # additive field, null when unknown
    finally:
        cleanup(db)


def test_tmdb_only_library_add_unchanged():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        with _patched(db) as find_mock:
            resp = client.post(
                "/api/v2/library",
                headers=_bearer(user.user_key),
                json={"tmdb_id": 1399, "media_type": "tv", "title": "GoT"},
            )
            find_mock.assert_not_awaited()
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["added"] is True
        assert body["item"]["tmdb_id"] == 1399
        assert body["item"]["imdb_id"] is None
    finally:
        cleanup(db)


# --------------------------------------------------------------------------- #
# Delete progress (un-mark watched)                                             #
# --------------------------------------------------------------------------- #


def test_delete_progress_tv_episode_targets_one_episode():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        db.add_all(
            [
                WatchProgress(
                    user_id=user.id,
                    tmdb_id=1399,
                    media_type="tv",
                    season=1,
                    episode=2,
                    position=100,
                    duration=1000,
                ),
                WatchProgress(
                    user_id=user.id,
                    tmdb_id=1399,
                    media_type="tv",
                    season=1,
                    episode=3,
                    position=200,
                    duration=1000,
                ),
            ]
        )
        db.commit()
        with patch("app.main.get_db", side_effect=lambda: use_test_db(db)):
            resp = client.delete(
                "/api/v2/progress/tv/1399",
                headers=_bearer(user.user_key),
                params={"season": 1, "episode": 2},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["removed"] is True
        remaining = (
            db.query(WatchProgress).filter(WatchProgress.user_id == user.id).all()
        )
        assert len(remaining) == 1
        assert (remaining[0].season, remaining[0].episode) == (1, 3)
    finally:
        cleanup(db)


def test_delete_progress_movie_by_tmdb():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        db.add(
            WatchProgress(
                user_id=user.id,
                tmdb_id=603,
                media_type="movie",
                season=0,
                episode=0,
                position=50,
                duration=1000,
            )
        )
        db.commit()
        with patch("app.main.get_db", side_effect=lambda: use_test_db(db)):
            resp = client.delete(
                "/api/v2/progress/movie/603", headers=_bearer(user.user_key)
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["removed"] is True
        assert (
            db.query(WatchProgress).filter(WatchProgress.user_id == user.id).count()
            == 0
        )
    finally:
        cleanup(db)


def test_delete_progress_by_imdb_id():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        db.add(
            WatchProgress(
                user_id=user.id,
                imdb_id="tt0133093",
                media_type="movie",
                season=0,
                episode=0,
                position=50,
                duration=1000,
            )
        )
        db.commit()
        with patch("app.main.get_db", side_effect=lambda: use_test_db(db)):
            resp = client.delete(
                "/api/v2/progress/movie/tt0133093", headers=_bearer(user.user_key)
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["removed"] is True
        assert (
            db.query(WatchProgress).filter(WatchProgress.user_id == user.id).count()
            == 0
        )
    finally:
        cleanup(db)


def test_delete_progress_is_idempotent_when_absent():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        with patch("app.main.get_db", side_effect=lambda: use_test_db(db)):
            resp = client.delete(
                "/api/v2/progress/tv/1399",
                headers=_bearer(user.user_key),
                params={"season": 9, "episode": 9},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["removed"] is False
    finally:
        cleanup(db)


def test_delete_progress_also_clears_watch_event():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        # A watched episode lives only in watch_events (progress was dropped at
        # >=90%). Un-marking must clear it so it stops showing as watched.
        db.add(
            WatchEvent(
                user_id=user.id,
                tmdb_id=1399,
                media_type="tv",
                season=1,
                episode=2,
                action="complete",
                title="Ep",
                created_at=datetime(2022, 1, 1),
            )
        )
        db.commit()
        with patch("app.main.get_db", side_effect=lambda: use_test_db(db)):
            resp = client.delete(
                "/api/v2/progress/tv/1399",
                headers=_bearer(user.user_key),
                params={"season": 1, "episode": 2},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["removed"] is True
        assert db.query(WatchEvent).filter(WatchEvent.user_id == user.id).count() == 0
    finally:
        cleanup(db)


# --------------------------------------------------------------------------- #
# Watched read endpoint (cross-device "caught up")                              #
# --------------------------------------------------------------------------- #


def test_watched_returns_completed_episodes():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        db.add_all(
            [
                WatchEvent(
                    user_id=user.id,
                    tmdb_id=1399,
                    media_type="tv",
                    season=1,
                    episode=1,
                    action="complete",
                    title="S1E1",
                    created_at=datetime(2022, 1, 1),
                ),
                WatchEvent(
                    user_id=user.id,
                    tmdb_id=1399,
                    media_type="tv",
                    season=1,
                    episode=2,
                    action="complete",
                    title="S1E2",
                    created_at=datetime(2022, 1, 2),
                ),
            ]
        )
        db.commit()
        with patch("app.main.get_db", side_effect=lambda: use_test_db(db)):
            resp = client.get("/api/v2/watched", headers=_bearer(user.user_key))
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert {(i["season"], i["episode"]) for i in items} == {(1, 1), (1, 2)}
        assert all(i["tmdb_id"] == 1399 and i["media_type"] == "tv" for i in items)
        # Most-recently-watched first.
        assert (items[0]["season"], items[0]["episode"]) == (1, 2)
    finally:
        cleanup(db)


def test_watched_dedups_repeat_completions():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        db.add_all(
            [
                WatchEvent(
                    user_id=user.id,
                    tmdb_id=603,
                    media_type="movie",
                    season=0,
                    episode=0,
                    action="complete",
                    title="Movie",
                    created_at=datetime(2022, 1, 1),
                ),
                WatchEvent(
                    user_id=user.id,
                    tmdb_id=603,
                    media_type="movie",
                    season=0,
                    episode=0,
                    action="complete",
                    title="Movie",
                    created_at=datetime(2022, 2, 1),
                ),
            ]
        )
        db.commit()
        with patch("app.main.get_db", side_effect=lambda: use_test_db(db)):
            resp = client.get("/api/v2/watched", headers=_bearer(user.user_key))
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["tmdb_id"] == 603
        assert "season" not in items[0]  # movie rows omit season/episode
    finally:
        cleanup(db)


def test_watched_is_empty_after_unmark():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        db.add(
            WatchEvent(
                user_id=user.id,
                tmdb_id=1399,
                media_type="tv",
                season=1,
                episode=1,
                action="complete",
                title="S1E1",
                created_at=datetime(2022, 1, 1),
            )
        )
        db.commit()
        with patch("app.main.get_db", side_effect=lambda: use_test_db(db)):
            client.delete(
                "/api/v2/progress/tv/1399",
                headers=_bearer(user.user_key),
                params={"season": 1, "episode": 1},
            )
            resp = client.get("/api/v2/watched", headers=_bearer(user.user_key))
        assert resp.status_code == 200, resp.text
        assert resp.json()["items"] == []
    finally:
        cleanup(db)


# --------------------------------------------------------------------------- #
# Scrobble dedup keys on season/episode                                         #
# --------------------------------------------------------------------------- #


def test_scrobble_distinct_episodes_are_not_deduped():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        with _patched(db):
            r1 = client.post(
                "/api/v2/scrobble",
                headers=_bearer(user.user_key),
                json={
                    "tmdb_id": 1399,
                    "media_type": "tv",
                    "season": 1,
                    "episode": 1,
                    "title": "S1E1",
                },
            )
            r2 = client.post(
                "/api/v2/scrobble",
                headers=_bearer(user.user_key),
                json={
                    "tmdb_id": 1399,
                    "media_type": "tv",
                    "season": 1,
                    "episode": 2,
                    "title": "S1E2",
                },
            )
        assert r1.status_code == 200 and r2.status_code == 200, r2.text
        assert r2.json().get("deduplicated") is not True
        events = db.query(WatchEvent).filter(WatchEvent.user_id == user.id).all()
        assert {(e.season, e.episode) for e in events} == {(1, 1), (1, 2)}
    finally:
        cleanup(db)


def test_scrobble_same_episode_is_deduped_within_window():
    client, db = make_client()
    try:
        user = _seed_profile(db)
        with _patched(db):
            client.post(
                "/api/v2/scrobble",
                headers=_bearer(user.user_key),
                json={"tmdb_id": 1399, "media_type": "tv", "season": 1, "episode": 1},
            )
            resp = client.post(
                "/api/v2/scrobble",
                headers=_bearer(user.user_key),
                json={"tmdb_id": 1399, "media_type": "tv", "season": 1, "episode": 1},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json().get("deduplicated") is True
        assert db.query(WatchEvent).filter(WatchEvent.user_id == user.id).count() == 1
    finally:
        cleanup(db)
