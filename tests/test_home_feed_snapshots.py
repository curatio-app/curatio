# ruff: noqa: E402
"""Home requests hydrate a viewport; the durable worker owns feed construction."""

import os
from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import home_feed, main
from app.config import settings
from app.database import get_db_dependency
from app.models import (
    Account,
    AdminSetting,
    Base,
    HomeFeedSnapshot,
    HomeFeedState,
    MediaMetadata,
    User,
    UniversalCategory,
    UniversalCatalogContent,
    UserCatalog,
    UserCatalogContent,
    WatchProgress,
)
from app.time_utils import utc_now


def _bearer(user_key):
    """Profile credential header for the native /api/v2 API."""
    return {"Authorization": f"Bearer {user_key}"}


@pytest.fixture()
def store(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def context():
        db = factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def dependency():
        with context() as db:
            yield db

    monkeypatch.setattr(home_feed, "get_db", context)
    monkeypatch.setattr(main, "_schedule_local_sync", lambda *_: None)
    monkeypatch.setattr(settings, "hide_foreign", False)
    monkeypatch.setattr(settings, "hide_adult", False)
    monkeypatch.setattr(settings, "hide_unreleased", False)
    monkeypatch.setattr(settings, "enable_universal_catalogs", True)
    monkeypatch.setattr(settings, "enable_personalized_catalogs", True)
    main._taste_cache.clear()
    main._invalidate_dashboard_cache()
    main._user_cache.clear()
    monkeypatch.setitem(main.app.dependency_overrides, get_db_dependency, dependency)
    with context() as db:
        account = Account(
            email="home@example.test", password_hash="test", is_active=True
        )
        db.add(account)
        db.flush()
        db.add(
            User(
                user_key="home-key",
                account_id=account.id,
                is_active=True,
                last_sync=utc_now(),
            )
        )
        db.add(
            User(
                user_key="other-key",
                account_id=account.id,
                is_active=True,
                last_sync=utc_now(),
            )
        )
    client = TestClient(main.app)
    try:
        yield client, context, engine
    finally:
        client.close()
        main._taste_cache.clear()
        main._invalidate_dashboard_cache()
        main._user_cache.clear()
        engine.dispose()


def seed_membership(context, row_count=40, items=80):
    """Publish durable identities directly to isolate serving work from building."""
    with context() as db:
        user = db.query(User).filter_by(user_key="home-key").one()
        state = home_feed.ensure_state(db, user.id)
        rows = []
        for ordinal in range(row_count):
            members = []
            for index in range(items):
                identifier = ordinal * 1000 + index + 1
                members.append(["movie", identifier])
                db.add(
                    MediaMetadata(
                        tmdb_id=identifier,
                        media_type="movie",
                        title=f"Title {identifier}",
                        poster_path=f"/{identifier}.jpg",
                        backdrop_path=f"/back-{identifier}.jpg",
                        raw_data={"never_on_home": "x" * 1000},
                    )
                )
            rows.append(
                {
                    "id": f"row-{ordinal}",
                    "title": f"Row {ordinal}",
                    "type": "movie",
                    "members": members,
                }
            )
        snapshot_id = str(uuid4())
        db.add(
            HomeFeedSnapshot(
                id=snapshot_id,
                user_id=user.id,
                policy_fingerprint=home_feed.policy_fingerprint(),
                source_fingerprint="fixture",
                membership={
                    "rows": rows,
                    "hero": rows[-1]["members"][:8],
                    "hero_source_row_id": rows[-1]["id"],
                },
            )
        )
        state.snapshot_id = snapshot_id
        state.published_generation = state.generation
        state.next_check_at = utc_now() + timedelta(minutes=1)
    return snapshot_id


def test_missing_snapshot_queues_durably_without_building_on_request(
    store, monkeypatch
):
    client, context, _ = store
    monkeypatch.setattr(
        home_feed, "build_membership", lambda *_: pytest.fail("request built feed")
    )
    response = client.get("/api/v2/home", headers=_bearer("home-key"))
    assert response.status_code == 202
    assert response.json()["retry_after"] == 1
    assert response.headers["retry-after"] == "1"
    with context() as db:
        state = db.query(HomeFeedState).one()
        assert state.snapshot_id is None
        assert state.requested_at is not None


def test_initial_and_batch_hydration_are_bounded_before_query(store, monkeypatch):
    client, context, engine = store
    snapshot = seed_membership(context)
    calls = []
    statements = []
    hydrate = home_feed._hydrate

    def observed(db, members):
        calls.append(len(set(map(tuple, members))))
        return hydrate(db, members)

    monkeypatch.setattr(home_feed, "_hydrate", observed)
    monkeypatch.setattr(
        home_feed, "build_membership", lambda *_: pytest.fail("request built feed")
    )
    event.listen(
        engine,
        "before_cursor_execute",
        lambda conn, cursor, sql, *_: statements.append(sql),
    )
    first = client.get("/api/v2/home", headers=_bearer("home-key"))
    assert first.status_code == 200
    body = first.json()
    assert len(body["row_descriptors"]) == 40
    assert len(body["rows"]) == 6
    assert all(
        len(row["items"]) == 20 and row["total"] == 80 and row["next_cursor"] == 20
        for row in body["rows"]
    )
    assert calls == [128]
    assert "raw_data" not in " ".join(
        sql for sql in statements if "FROM media_metadata" in sql
    )
    second = client.post(
        "/api/v2/home/rows",
        headers=_bearer("home-key"),
        json={
            "snapshot_id": snapshot,
            "rows": [
                {"id": f"row-{index}", "cursor": 20, "limit": 20} for index in range(3)
            ],
        },
    )
    assert second.status_code == 200
    assert calls == [128, 60]
    assert set(second.json()) == {"snapshot_id", "rows"}
    assert all(
        row["cursor"] == 20 and row["next_cursor"] == 40
        for row in second.json()["rows"]
    )


def test_every_item_is_reachable_in_frozen_order(store):
    client, context, _ = store
    snapshot = seed_membership(context, row_count=3)
    first = client.get("/api/v2/home", headers=_bearer("home-key")).json()
    expected = list(range(1, 81))
    identifiers = [item["tmdbId"] for item in first["rows"][0]["items"]]
    for cursor in (20, 40, 60):
        row = client.post(
            "/api/v2/home/rows",
            headers=_bearer("home-key"),
            json={
                "snapshot_id": snapshot,
                "rows": [{"id": "row-0", "cursor": cursor}],
            },
        ).json()["rows"][0]
        identifiers.extend(item["tmdbId"] for item in row["items"])
    assert identifiers == expected
    assert row["next_cursor"] is None


def test_conditional_response_and_metadata_correction(store):
    client, context, _ = store
    seed_membership(context, row_count=1)
    first = client.get("/api/v2/home", headers=_bearer("home-key"))
    tag = first.headers["etag"]
    same = client.get(
        "/api/v2/home", headers={**_bearer("home-key"), "If-None-Match": tag}
    )
    assert same.status_code == 304 and same.content == b""
    with context() as db:
        db.query(MediaMetadata).filter_by(tmdb_id=1, media_type="movie").update(
            {"title": "Corrected"}
        )
    changed = client.get(
        "/api/v2/home", headers={**_bearer("home-key"), "If-None-Match": tag}
    )
    assert changed.status_code == 200
    assert changed.headers["etag"] != tag
    assert changed.json()["rows"][0]["items"][0]["name"] == "Corrected"


def test_missing_metadata_preserves_identity_and_cursor_progress(store):
    client, context, _ = store
    snapshot = seed_membership(context, row_count=1)
    with context() as db:
        db.query(MediaMetadata).filter(MediaMetadata.tmdb_id.in_([1, 21])).delete()
    first = client.get("/api/v2/home", headers=_bearer("home-key")).json()["rows"][0]
    second = client.post(
        "/api/v2/home/rows",
        headers=_bearer("home-key"),
        json={"snapshot_id": snapshot, "rows": [{"id": "row-0", "cursor": 20}]},
    ).json()["rows"][0]
    assert first["items"][0]["tmdbId"] == 1
    assert second["items"][0]["tmdbId"] == 21
    assert first["items"][0]["poster"] is None
    assert first["cursor"] == 0 and first["next_cursor"] == 20
    assert second["cursor"] == 20 and second["next_cursor"] == 40
    assert first["total"] == second["total"] == 80


def test_persisted_policy_rejects_old_snapshot_on_another_worker(store):
    client, context, _ = store
    snapshot = seed_membership(context, row_count=1)
    tag = client.get("/api/v2/home", headers=_bearer("home-key")).headers["etag"]
    with context() as db:
        db.add(AdminSetting(key="HIDE_ADULT", value="true"))
        home_feed.invalidate_discovery(db)
    # Simulate a process that has not hot-reloaded another worker's admin write.
    assert not settings.hide_adult
    assert (
        client.get(
            "/api/v2/home", headers={**_bearer("home-key"), "If-None-Match": tag}
        ).status_code
        == 412
    )
    assert client.get("/api/v2/home", headers=_bearer("home-key")).status_code == 202
    assert (
        client.post(
            "/api/v2/home/rows",
            headers=_bearer("home-key"),
            json={"snapshot_id": snapshot, "rows": [{"id": "row-0"}]},
        ).status_code
        == 412
    )
    assert not settings.hide_adult


def test_conditional_old_policy_is_rejected_after_new_publication(store):
    client, context, _ = store
    old_id = seed_membership(context, row_count=1)
    tag = client.get("/api/v2/home", headers=_bearer("home-key")).headers["etag"]
    new_id = str(uuid4())
    with context() as db:
        old = db.get(HomeFeedSnapshot, old_id)
        old.superseded_at = utc_now()
        db.add(AdminSetting(key="HIDE_ADULT", value="true"))
        db.flush()
        db.add(
            HomeFeedSnapshot(
                id=new_id,
                user_id=old.user_id,
                source_fingerprint="updated-fixture",
                policy_fingerprint=home_feed.policy_fingerprint(db),
                membership=old.membership,
            )
        )
        db.get(HomeFeedState, old.user_id).snapshot_id = new_id
    assert (
        client.get("/api/v2/home", headers=_bearer("home-key")).json()["snapshot_id"]
        == new_id
    )
    rejected = client.get(
        "/api/v2/home", headers={**_bearer("home-key"), "If-None-Match": tag}
    )
    assert rejected.status_code == 412
    assert rejected.json()["detail"] == "policy_invalidated"
    with context() as db:
        db.query(HomeFeedSnapshot).filter_by(id=old_id).delete()
    assert (
        client.get(
            "/api/v2/home", headers={**_bearer("home-key"), "If-None-Match": tag}
        ).status_code
        == 412
    )


def test_auth_ownership_and_request_bounds(store):
    client, context, _ = store
    snapshot = seed_membership(context, row_count=1)
    request = {"snapshot_id": snapshot, "rows": [{"id": "row-0"}]}
    assert client.get("/api/v2/home", headers=_bearer("no-key")).status_code == 401
    assert (
        client.post(
            "/api/v2/home/rows", headers=_bearer("other-key"), json=request
        ).status_code
        == 409
    )
    with context() as db:
        db.query(User).filter_by(user_key="other-key").update({"is_active": False})
    assert client.get("/api/v2/home", headers=_bearer("other-key")).status_code == 403
    for rows in (
        [{"id": "row-0"}] * 4,
        [{"id": "row-0", "limit": 21}],
        [{"id": "row-0", "cursor": -1}],
        [{"id": "row-0"}] * 2,
        [{"id": "missing"}],
    ):
        assert (
            client.post(
                "/api/v2/home/rows",
                headers=_bearer("home-key"),
                json={"snapshot_id": snapshot, "rows": rows},
            ).status_code
            == 422
        )
    with context() as db:
        db.query(Account).update({"is_active": False})
    assert client.get("/api/v2/home", headers=_bearer("home-key")).status_code == 403
    assert (
        client.post(
            "/api/v2/home/rows", headers=_bearer("home-key"), json=request
        ).status_code
        == 403
    )
    with context() as db:
        home_feed.invalidate_discovery(db)
        assert home_feed._claim_next(db) is None


def test_cursor_cannot_skip_beyond_a_short_rows_membership(store):
    client, context, _ = store
    snapshot = seed_membership(context, row_count=1, items=24)
    response = client.post(
        "/api/v2/home/rows",
        headers=_bearer("home-key"),
        json={"snapshot_id": snapshot, "rows": [{"id": "row-0", "cursor": 25}]},
    )
    assert response.status_code == 422


def test_expiration_and_policy_changes_do_not_serve_incompatible_membership(
    store, monkeypatch
):
    client, context, _ = store
    snapshot = seed_membership(context, row_count=1)
    request = {"snapshot_id": snapshot, "rows": [{"id": "row-0"}]}
    with context() as db:
        db.get(HomeFeedSnapshot, snapshot).superseded_at = utc_now() - timedelta(
            hours=25
        )
    assert (
        client.post(
            "/api/v2/home/rows", headers=_bearer("home-key"), json=request
        ).status_code
        == 409
    )
    with context() as db:
        db.get(HomeFeedSnapshot, snapshot).superseded_at = None
    tag = client.get("/api/v2/home", headers=_bearer("home-key")).headers["etag"]
    monkeypatch.setattr(settings, "hide_adult", True)
    assert client.get("/api/v2/home", headers=_bearer("home-key")).status_code == 202
    assert (
        client.get(
            "/api/v2/home", headers={**_bearer("home-key"), "If-None-Match": tag}
        ).status_code
        == 412
    )
    assert (
        client.post(
            "/api/v2/home/rows", headers=_bearer("home-key"), json=request
        ).status_code
        == 412
    )


def test_cross_session_claims_lease_recovery_and_obsolete_publish(store):
    _, context, _ = store
    with context() as db:
        user = db.query(User).filter_by(user_key="home-key").one()
        user_id = user.id
        home_feed.ensure_state(db, user_id)
    with context() as db:
        claim = home_feed._claim_next(db)
    with context() as db:
        assert home_feed._claim_next(db) is None
        db.get(HomeFeedState, user_id).lease_until = utc_now() - timedelta(seconds=1)
    with context() as db:
        replacement = home_feed._claim_next(db)
    assert replacement["token"] != claim["token"]
    with context() as db:
        assert not home_feed._finish_claim(
            db, claim, {"rows": []}, "source", home_feed.policy_fingerprint()
        )
        home_feed.invalidate_discovery(db, user_id)
    with context() as db:
        assert not home_feed._finish_claim(
            db, replacement, {"rows": []}, "source", home_feed.policy_fingerprint()
        )
        assert db.get(HomeFeedState, user_id).snapshot_id is None
        assert db.query(HomeFeedSnapshot).count() == 0


def test_builder_publishes_and_reuses_after_restart_and_failure(store, monkeypatch):
    client, context, _ = store
    assert client.get("/api/v2/home", headers=_bearer("home-key")).status_code == 202
    builds = []

    def build(*_):
        builds.append(1)
        return {"rows": [], "hero": [], "hero_source_row_id": None}

    monkeypatch.setattr(home_feed, "build_membership", build)
    assert home_feed.build_next()
    initial = client.get("/api/v2/home", headers=_bearer("home-key"))
    assert initial.status_code == 200
    with context() as db:
        state = db.query(HomeFeedState).one()
        state.next_check_at = utc_now() - timedelta(seconds=1)
    assert home_feed.build_next()
    assert len(builds) == 1
    assert (
        client.get("/api/v2/home", headers=_bearer("home-key")).json()["snapshot_id"]
        == initial.json()["snapshot_id"]
    )
    with context() as db:
        home_feed.invalidate_discovery(db)
    monkeypatch.setattr(
        home_feed,
        "build_membership",
        lambda *_: (_ for _ in ()).throw(RuntimeError("test failure")),
    )
    assert home_feed.build_next()
    assert (
        client.get("/api/v2/home", headers=_bearer("home-key")).json()["snapshot_id"]
        == initial.json()["snapshot_id"]
    )
    with context() as db:
        state = db.query(HomeFeedState).one()
        assert state.retry_at > utc_now() and state.lease_token is None


@pytest.mark.parametrize(
    "filtered,persisted_policy", [(False, False), (True, False), (True, True)]
)
def test_membership_matches_legacy_full_native_ranking_and_dedup(
    store, monkeypatch, filtered, persisted_policy
):
    _, context, _ = store
    monkeypatch.setattr(settings, "hide_foreign", filtered)
    monkeypatch.setattr(settings, "hide_adult", filtered)
    with context() as db:
        user = db.query(User).filter_by(user_key="home-key").one()
        stamp = utc_now()
        for index in range(4):
            category = UniversalCategory(
                id=f"fixture-{index}",
                name=f"Category {index}",
                tier=1,
                sort_order=index,
                media_type="movie",
                tag_formula={},
                source="generated",
                updated_at=stamp,
            )
            db.add(category)
            for ordinal in range(120):
                # Half-overlapping rows exercise claims and the 24-item density floor.
                identifier = 1 + index * 60 + ordinal
                if db.get(MediaMetadata, (identifier, "movie")) is None:
                    db.add(
                        MediaMetadata(
                            tmdb_id=identifier,
                            media_type="movie",
                            title=f"Film {identifier}",
                            backdrop_path=f"/{identifier}.jpg",
                            original_language="es" if identifier % 19 == 0 else "en",
                            adult=identifier % 23 == 0,
                        )
                    )
                    db.flush()
                db.add(
                    UniversalCatalogContent(
                        category_id=category.id,
                        tmdb_id=identifier,
                        media_type="movie",
                        rank=ordinal + 1,
                        match_score=1.0,
                        last_updated=stamp,
                    )
                )
        personal = UserCatalog(
            user_id=user.id,
            slot_id="picks-movie",
            name="Your picks",
            media_type="movie",
            generation_method="test",
            last_generated=stamp,
            generation_params={"generation_revision": stamp.isoformat()},
        )
        db.add(personal)
        db.flush()
        for ordinal in range(80):
            db.add(
                UserCatalogContent(
                    catalog_id=personal.id,
                    tmdb_id=ordinal + 1,
                    media_type="movie",
                    rank=ordinal + 1,
                    match_score=1,
                    added_at=stamp,
                )
            )
        db.commit()
        legacy = main._dashboard_payload(user, db, row_item_limit=80)["catalogs"]
        if persisted_policy:
            db.add_all(
                [
                    AdminSetting(key="HIDE_FOREIGN", value="true"),
                    AdminSetting(key="HIDE_ADULT", value="true"),
                ]
            )
            db.flush()
            monkeypatch.setattr(settings, "hide_foreign", False)
            monkeypatch.setattr(settings, "hide_adult", False)
        membership = home_feed.build_membership(db, user)
        assert [
            (
                row["id"],
                [[item["media_type"], item["tmdb_id"]] for item in row["items"]],
            )
            for row in legacy
        ] == [(row["id"], row["members"]) for row in membership["rows"]]
        assert all(24 <= len(row["members"]) <= 80 for row in membership["rows"])
        assert all(
            set(row) == {"id", "title", "type", "members"} for row in membership["rows"]
        )
        if persisted_policy:
            db.add(AdminSetting(key="ENABLE_UNIVERSAL_CATALOGS", value="false"))
            db.flush()
            disabled = home_feed.build_membership(db, user)
            assert all(row["id"].startswith("personal-") for row in disabled["rows"])
            assert settings.enable_universal_catalogs


def test_progress_invalidates_only_on_effective_taste_change(store):
    _, context, _ = store
    with context() as db:
        user = db.query(User).filter_by(user_key="home-key").one()
        state = home_feed.ensure_state(db, user.id)
        db.add(
            WatchProgress(
                user_id=user.id,
                tmdb_id=5,
                media_type="movie",
                position=1200,
                duration=6000,
                season=None,
                episode=None,
                title="Film",
            )
        )
        db.commit()
        initial = state.generation
        main._write_progress_row(
            db,
            "home-key",
            main.ProgressRequest(
                tmdb_id=5, media_type="movie", position=1300, duration=6000
            ),
            5,
            None,
            "movie",
            None,
            None,
        )
        db.commit()
        db.expire_all()
        assert db.get(HomeFeedState, user.id).generation == initial
        main._write_progress_row(
            db,
            "home-key",
            main.ProgressRequest(
                tmdb_id=5, media_type="movie", position=3000, duration=6000
            ),
            5,
            None,
            "movie",
            None,
            None,
        )
        db.commit()
        db.expire_all()
        assert db.get(HomeFeedState, user.id).generation == initial + 1


def test_invalidation_rolls_back_with_source_transaction(store):
    _, context, _ = store
    with context() as db:
        user_id = db.query(User.id).filter_by(user_key="home-key").scalar()
        home_feed.ensure_state(db, user_id)
    with pytest.raises(RuntimeError):
        with context() as db:
            home_feed.invalidate_discovery(db, user_id)
            raise RuntimeError("source publication failed")
    with context() as db:
        assert db.get(HomeFeedState, user_id).generation == 1


def test_new_publication_keeps_old_cursor_and_prunes_only_expired_snapshots(store):
    client, context, _ = store
    old_id = seed_membership(context, row_count=1)
    old_tag = client.get("/api/v2/home", headers=_bearer("home-key")).headers["etag"]
    with context() as db:
        state = db.query(HomeFeedState).one()
        home_feed.invalidate_discovery(db, state.user_id)
    with context() as db:
        claim = home_feed._claim_next(db)
    with context() as db:
        assert home_feed._finish_claim(
            db,
            claim,
            {"rows": [], "hero": [], "hero_source_row_id": None},
            "new-source",
            home_feed.policy_fingerprint(),
        )
    refreshed = client.get(
        "/api/v2/home", headers={**_bearer("home-key"), "If-None-Match": old_tag}
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["snapshot_id"] != old_id
    payload = {"snapshot_id": old_id, "rows": [{"id": "row-0", "cursor": 60}]}
    assert (
        client.post(
            "/api/v2/home/rows", headers=_bearer("home-key"), json=payload
        ).status_code
        == 200
    )
    with context() as db:
        assert home_feed.prune_snapshots(db) == 0
        db.get(HomeFeedSnapshot, old_id).superseded_at = utc_now() - timedelta(hours=25)
        db.flush()
        assert home_feed.prune_snapshots(db) == 1
        assert db.query(HomeFeedSnapshot).count() == 1
    assert (
        client.post(
            "/api/v2/home/rows", headers=_bearer("home-key"), json=payload
        ).status_code
        == 409
    )


def test_source_change_during_build_does_not_publish_a_mixed_snapshot(
    store, monkeypatch
):
    client, context, _ = store
    assert client.get("/api/v2/home", headers=_bearer("home-key")).status_code == 202
    fingerprints = iter(["before-publication", "after-publication"])
    monkeypatch.setattr(home_feed, "_source_fingerprint", lambda *_: next(fingerprints))
    monkeypatch.setattr(
        home_feed,
        "build_membership",
        lambda *_: {"rows": [], "hero": [], "hero_source_row_id": None},
    )
    assert home_feed.build_next()
    with context() as db:
        state = db.query(HomeFeedState).one()
        assert state.snapshot_id is None
        assert state.lease_token is None
        assert state.generation > state.published_generation
        assert db.query(HomeFeedSnapshot).count() == 0


def test_request_hint_is_throttled_and_never_fails_the_response(store, monkeypatch):
    client, context, _ = store
    seed_membership(context, row_count=1, items=2)
    assert client.get("/api/v2/home", headers=_bearer("home-key")).status_code == 200
    with context() as db:
        first = db.query(HomeFeedState).one().requested_at
    assert first is not None

    # Within the hint window a served snapshot writes nothing.
    assert client.get("/api/v2/home", headers=_bearer("home-key")).status_code == 200
    with context() as db:
        assert db.query(HomeFeedState).one().requested_at == first

    # A lock wait on the state row (a sync committing, a builder publishing)
    # drops the hint instead of stalling or failing the request.
    from sqlalchemy.exc import OperationalError

    def locked(*_args, **_kwargs):
        raise OperationalError("UPDATE", {}, Exception("lock timeout"))

    with context() as db:
        state = db.query(HomeFeedState).one()
        state.requested_at = utc_now() - timedelta(minutes=5)
    monkeypatch.setattr("sqlalchemy.orm.Query.update", locked)
    assert client.get("/api/v2/home", headers=_bearer("home-key")).status_code == 200


def test_save_user_catalog_leaves_invalidation_to_the_caller(store):
    from app.catalog_generator import CatalogGenerator

    _, context, _ = store
    with context() as db:
        user_id = db.query(User.id).filter_by(user_key="home-key").scalar()
        home_feed.ensure_state(db, user_id)
    with context() as db:
        CatalogGenerator(db).save_user_catalog(
            user_id=user_id,
            slot_id="rec-movie",
            name="Recommended Movies",
            media_type="movie",
            tmdb_ids=[1, 2, 3],
            generation_method="test",
        )
        assert db.get(HomeFeedState, user_id).generation == 1
