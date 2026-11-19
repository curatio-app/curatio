# ruff: noqa: E402
"""Phase 6 surfaces — the tags finally reaching a user.

Everything the overhaul bought was invisible outside the catalog rows: chips
were never rendered, ``title_ratings`` had ~326k rows and zero readers, "Similar
Titles" came from TMDB's popularity-driven ``/similar``, and search was
``title ILIKE '%q%'`` against a hardcoded client-side genre list.

The tests that matter here are the ones about *falling back honestly*: a title
we have not tagged must still get a similar rail, and it must not be labelled
as ours.
"""

import asyncio
import os
import sys
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_browse_api.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.auth import create_user_session  # noqa: E402
from app.database import get_db_dependency  # noqa: E402
from app import main  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    Base,
    DetailSnapshot,
    LibraryItem,
    MediaMetadata,
    MovieTag,
    PasskeyCredential,
    Tag,
    TagStats,
    TitleRating,
    User,
    UniversalCatalogContent,
    WatchProgress,
)
from app.relevance import refresh_tag_stats  # noqa: E402
from app.tmdb_client import TMDBServerError  # noqa: E402
from app.time_utils import utc_now  # noqa: E402

engine = create_engine(
    "sqlite:///./test_browse_api.db", connect_args={"check_same_thread": False}
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@contextmanager
def _ctx():
    """A fresh session per call, as the real ``get_db`` gives.

    Handing every caller one shared Session hid the concurrency contract that
    batch details depend on: a Session is not safe for concurrent use, and the
    batch runs its items gathered.
    """
    db = TestingSessionLocal()
    try:
        yield db
        db.commit()
    finally:
        db.close()


def _bearer(user_key):
    """Profile credential header for the native /api/v2 API."""
    return {"Authorization": f"Bearer {user_key}"}


@pytest.fixture()
def client_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()

    def _override():
        yield db

    app.dependency_overrides[get_db_dependency] = _override

    # Some routes reach the database twice: through the dependency (overridden
    # above) and through the module-global `get_db`. That global binds to
    # whichever test module won the `DATABASE_URL` setdefault race at import time -- a database this file
    # neither creates nor controls, and which its owner drops at teardown. So
    # point it here too, rather than depending on collection order.
    db_patch = patch("app.main.get_db", side_effect=_ctx)
    db_patch.start()

    # These tests exercise browse/details rendering, not personalized-sync
    # scheduling. A never-synced local user can launch that fire-and-forget
    # worker; because get_db is deliberately patched above, the
    # worker would receive this exact fixture Session and could commit while
    # teardown closes it. That is an invalid cross-thread Session handoff and
    # produced an intermittent IllegalStateChangeError in CI.
    sync_patch = patch("app.main._schedule_local_sync")
    sync_patch.start()

    client = TestClient(app)

    # Both caches are module-level and outlive a test. The detail cache is
    # keyed by (media_type, tmdb_id) alone, so the second test to request
    # title 1 would read the first test's payload and assert against a
    # fixture it never seeded.
    from app.main import _meta_cache, clear_detail_response_cache

    _meta_cache.clear()
    clear_detail_response_cache()

    account = Account(
        auth_source="local",
        email="browser@example.com",
        password_hash="not-used",
        is_active=True,
        totp_enabled=True,
    )
    db.add(account)
    db.commit()
    db.add(
        PasskeyCredential(
            account_id=account.id,
            credential_id="browse-credential",
            public_key="browse-public-key",
        )
    )
    db.commit()
    user = User(
        user_key="browse-user-key",
        account_id=account.id,
        auth_source="local",
        display_name="Browser",
        is_active=True,
    )
    db.add(user)
    db.commit()
    client.cookies.set("user_session", create_user_session(account.id, db, user.id))

    try:
        yield client, db
    finally:
        # Drain TestClient's request portal before releasing its database
        # dependencies and dropping the schema.
        client.close()
        sync_patch.stop()
        db_patch.stop()
        db.close()
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)


# ---------------------------------------------------------------------------
# Fixture corpus
# ---------------------------------------------------------------------------
def _seed_corpus(db):
    """Twelve titles sharing a common tag, three sharing a rare one.

    The filler tag matters: ``weight_norm`` is L2-normalised per title, so a
    title holding exactly one tag has ``weight_norm = 1.0`` whatever the tag's
    IDF. Any fixture reasoning about rarity on single-tag titles is degenerate.
    """
    tags = {}
    for name, facet in (
        ("Neo-Noir", "subgenre"),
        ("Drama", "subgenre"),
        ("Bleak", "mood"),
        ("Internal Only", "maturity"),
    ):
        tag = Tag(name=name, category=facet, display=name != "Internal Only")
        db.add(tag)
        db.commit()
        tags[name] = tag

    for i in range(1, 13):
        db.add(
            MediaMetadata(
                tmdb_id=i,
                media_type="movie",
                title=f"Movie {i}",
                overview="A complete synopsis with enough evidence for semantic tagging.",
                popularity=float(20 - i),
                vote_average=7.0,
                vote_count=1000,
                release_date="2015-01-01",
            )
        )
        db.add(
            MovieTag(
                tmdb_id=i, media_type="movie", tag_id=tags["Drama"].id, confidence=0.8
            )
        )
        if i <= 3:
            db.add(
                MovieTag(
                    tmdb_id=i,
                    media_type="movie",
                    tag_id=tags["Neo-Noir"].id,
                    confidence=0.9,
                )
            )
        if i <= 2:
            db.add(
                MovieTag(
                    tmdb_id=i,
                    media_type="movie",
                    tag_id=tags["Bleak"].id,
                    confidence=0.7,
                )
            )
        if i == 1:
            db.add(
                MovieTag(
                    tmdb_id=i,
                    media_type="movie",
                    tag_id=tags["Internal Only"].id,
                    confidence=0.9,
                )
            )
    db.commit()
    refresh_tag_stats(db, create_indexes=False)
    return tags


def _seed_one_series(db):
    """One tagged series, so the per-type split is observable."""
    tag = db.query(Tag).filter(Tag.name == "Drama").first()
    for tmdb_id in (101, 102):
        db.add(
            MediaMetadata(
                tmdb_id=tmdb_id,
                media_type="tv",
                title=f"Show {tmdb_id}",
                overview="A complete synopsis with enough evidence for semantic tagging.",
                popularity=5.0,
                vote_average=8.0,
                vote_count=500,
                release_date="2018-01-01",
            )
        )
        db.add(
            MovieTag(tmdb_id=tmdb_id, media_type="tv", tag_id=tag.id, confidence=0.8)
        )
    db.commit()
    refresh_tag_stats(db, create_indexes=False)


# ---------------------------------------------------------------------------
# Facets
# ---------------------------------------------------------------------------
def test_facets_group_by_facet_and_report_title_counts(client_db):
    client, db = client_db
    _seed_corpus(db)

    body = client.get("/account/api/facets?media_type=movie&min_titles=1").json()

    by_facet = {f["facet"]: f["tags"] for f in body["facets"]}
    assert "subgenre" in by_facet and "mood" in by_facet
    counts = {t["name"]: t["count"] for t in by_facet["subgenre"]}
    assert counts == {"Drama": 12, "Neo-Noir": 3}
    # Within a facet, the most-used tag comes first: the sidebar's default
    # ordering is what a user scans, and burying Drama below a 3-title tag
    # makes the list look arbitrary.
    assert [t["name"] for t in by_facet["subgenre"]] == ["Drama", "Neo-Noir"]


def test_facets_hide_internal_tags(client_db):
    """``display=False`` tags exist to steer the tagger, not to be browsed."""
    client, db = client_db
    _seed_corpus(db)

    body = client.get("/account/api/facets?media_type=movie&min_titles=1").json()

    names = [t["name"] for f in body["facets"] for t in f["tags"]]
    assert "Internal Only" not in names


def test_facets_apply_the_minimum_title_floor(client_db):
    """A facet value matching two titles is a dead end, not a filter."""
    client, db = client_db
    _seed_corpus(db)

    body = client.get("/account/api/facets?media_type=movie&min_titles=5").json()

    names = [t["name"] for f in body["facets"] for t in f["tags"]]
    assert names == ["Drama"]


def test_facets_require_a_signed_in_profile(client_db):
    client, _db = client_db
    client.cookies.clear()
    assert client.get("/account/api/facets").status_code in (401, 403)


def test_native_discovery_uses_profile_key_without_a_browser_cookie(client_db):
    client, db = client_db
    tags = _seed_corpus(db)
    client.cookies.clear()

    facets = client.get(
        "/api/v2/facets?media_type=movie&min_titles=1",
        headers=_bearer("browse-user-key"),
    )
    browse = client.get(
        f"/api/v2/browse?tags={tags['Neo-Noir'].id}&media_type=movie",
        headers=_bearer("browse-user-key"),
    )
    search = client.get("/api/v2/search?q=Movie", headers=_bearer("browse-user-key"))

    assert facets.status_code == 200
    assert browse.status_code == 200
    assert browse.json()["items"]
    assert search.status_code == 200
    assert search.json()["items"]


def test_native_bootstrap_returns_profile_home_without_a_browser_cookie(client_db):
    client, db = client_db
    _seed_corpus(db)
    client.cookies.clear()

    response = client.get("/api/v2/bootstrap", headers=_bearer("browse-user-key"))

    assert response.status_code == 200
    body = response.json()
    assert body["account"]["active_profile_id"]
    assert body["account"]["profiles"][0]["user_key"] == "browse-user-key"
    assert "rows" in body["dashboard"]


def test_native_bootstrap_pages_forty_rows_and_exposes_revision(client_db):
    client, _db = client_db
    rows = [
        {"id": f"row-{index}", "name": f"Row {index}", "items": []}
        for index in range(40)
    ]
    dashboard = {
        "hero": None,
        "continue_watching": [],
        "recent_history": [],
        "library": [],
        "catalogs": rows,
        "movie_catalogs": [],
        "series_catalogs": [],
        "catalog_sync_pending": False,
        "catalog_revision": "revision-40",
    }

    # The ETag is now derived from `_catalog_state_revision` directly, because the
    # conditional-request short circuit has to answer before the dashboard is
    # assembled. Patch both so the mocked payload and the mocked revision agree,
    # which is the invariant that holds in production (the handler resolves the
    # revision once and passes it into `_dashboard_payload`).
    with (
        patch("app.main._dashboard_payload", return_value=dashboard),
        patch("app.main._catalog_state_revision", return_value="revision-40"),
    ):
        first = client.get("/api/v2/bootstrap", headers=_bearer("browse-user-key"))
        second = client.get(
            "/api/v2/bootstrap?cursor=24&limit=16", headers=_bearer("browse-user-key")
        )

    assert first.status_code == 200
    assert len(first.json()["dashboard"]["rows"]) == 24
    assert first.json()["dashboard"]["row_page"] == {
        "cursor": 0,
        "limit": 24,
        "total": 40,
        "next_cursor": 24,
    }
    assert [row["id"] for row in second.json()["dashboard"]["rows"]] == [
        f"row-{index}" for index in range(24, 40)
    ]
    assert second.json()["dashboard"]["catalog_revision"] == "revision-40"
    assert second.headers["cache-control"] == "private, no-cache"
    # The response shape is part of the entity tag, so a client switching to
    # `fields=card` is never answered 304 from the full-shape entity.
    assert second.headers["etag"] == '"revision-40:24:16:full"'


def test_native_catalog_revision_is_private_and_small(client_db):
    client, _db = client_db

    response = client.get(
        "/api/v2/catalog-revision", headers=_bearer("browse-user-key")
    )

    assert response.status_code == 200
    assert len(response.json()["catalog_revision"]) == 24
    assert response.json()["max_rows"] == 40
    assert response.json()["page_size"] == 24
    assert response.headers["cache-control"] == "private, no-store"


def test_bootstrap_rows_carry_the_backdrop_under_the_key_apple_clients_decode():
    """The card model's `background` is what the Apple mapper reads.

    Emitting only `backdrop` left `MediaItem.backdropURL` nil for every
    native-bootstrap card, so the hero billboard and card preview art fell back
    to the poster. `backdrop`/`backdrop_path` stay for the web dashboard, which
    reads them in src/api/mediaUtils.ts.
    """
    row = main._catalog_row_from_items(
        "Row",
        "personal-test",  # personal rows are not tier-shuffled, so order is stable
        "movie",
        [
            {
                "tmdb_id": 550,
                "title": "Fight Club",
                "poster": "/poster.jpg",
                "backdrop": "/backdrop.jpg",
            }
        ],
    )

    item = row["items"][0]
    assert item["backdrop"].endswith("/backdrop.jpg")
    assert item["background"] == item["backdrop"]
    assert item["backdrop_path"] == "/backdrop.jpg"


def test_fields_card_drops_only_what_an_apple_client_cannot_decode(client_db):
    """`fields=card` must keep every key the Apple card model reads, and no more."""
    client, _db = client_db
    row = {
        "id": "personal-x",
        "name": "Row",
        "items": main._catalog_row_from_items(
            "Row",
            "personal-x",
            "movie",
            [
                {
                    "tmdb_id": 550,
                    "title": "Fight Club",
                    "poster": "/p.jpg",
                    "backdrop": "/b.jpg",
                    "logo": "/l.png",
                    "description": "An insomniac office worker...",
                    "imdb_id": "tt0137523",
                    "year": "1999",
                    "content_rating": "R",
                }
            ],
        )["items"],
    }
    dashboard = {
        "hero": None,
        "continue_watching": [],
        "recent_history": [],
        "library": [],
        "catalogs": [row],
        "movie_catalogs": [],
        "series_catalogs": [],
        "catalog_sync_pending": False,
        "catalog_revision": "rev-card",
    }

    with (
        patch("app.main._dashboard_payload", return_value=dashboard),
        patch("app.main._catalog_state_revision", return_value="rev-card"),
    ):
        full = client.get(
            "/api/v2/bootstrap", headers=_bearer("browse-user-key")
        ).json()
        card = client.get(
            "/api/v2/bootstrap?fields=card", headers=_bearer("browse-user-key")
        ).json()

    full_item = full["dashboard"]["rows"][0]["items"][0]
    card_item = card["dashboard"]["rows"][0]["items"][0]

    # Everything the Swift model declares must survive the projection.
    for key in (
        "id",
        "type",
        "name",
        "poster",
        "background",
        "logo",
        "description",
        "releaseInfo",
        "contentRating",
        "tmdbId",
        "imdbId",
        "genres",
    ):
        assert key in card_item, f"projection dropped {key}, which the client reads"

    assert set(full_item) - set(card_item) == main._CARD_PROJECTION_DROP
    # And the cached row the browser dashboard shares must be untouched.
    assert "poster_path" in dashboard["catalogs"][0]["items"][0]


def test_fields_card_leaves_the_activity_collections_alone(client_db):
    """Continue Watching genuinely needs the keys the projection drops."""
    client, _db = client_db
    progress_item = main._serialize_media_item(
        tmdb_id=1399,
        media_type="tv",
        title="Game of Thrones",
        season=1,
        episode=2,
        position=600,
        duration=3600,
    )
    dashboard = {
        "hero": None,
        "continue_watching": [progress_item],
        "recent_history": [],
        "library": [],
        "catalogs": [],
        "movie_catalogs": [],
        "series_catalogs": [],
        "catalog_sync_pending": False,
        "catalog_revision": "rev-activity",
    }

    with (
        patch("app.main._dashboard_payload", return_value=dashboard),
        patch("app.main._catalog_state_revision", return_value="rev-activity"),
    ):
        body = client.get(
            "/api/v2/bootstrap?fields=card", headers=_bearer("browse-user-key")
        ).json()

    resumed = body["dashboard"]["continue_watching"][0]
    assert resumed["season"] == 1
    assert resumed["episode"] == 2
    assert resumed["position"] == 600
    assert resumed["duration"] == 3600


def test_catalog_revision_moves_when_activity_changes_on_another_device(client_db):
    """Progress and library writes must be visible to the foreground probe.

    The revision covers the discovery rows *and* the activity collections that
    ship in the same bootstrap document. Before it covered activity, a title
    started on another device changed nothing this value could see, so the
    client's "refetch only when the revision moves" rule never fired and
    Continue Watching stayed stale.
    """
    client, db = client_db
    profile = db.query(User).filter(User.user_key == "browse-user-key").first()

    def revision() -> str:
        return client.get(
            "/api/v2/catalog-revision", headers=_bearer("browse-user-key")
        ).json()["catalog_revision"]

    before = revision()

    db.add(
        WatchProgress(
            user_id=profile.id,
            tmdb_id=550,
            media_type="movie",
            season=0,
            episode=0,
            position=120,
            duration=7200,
            title="Fight Club",
        )
    )
    db.commit()
    after_progress = revision()
    assert after_progress != before

    db.add(
        LibraryItem(
            user_id=profile.id,
            tmdb_id=603,
            media_type="movie",
            title="The Matrix",
        )
    )
    db.commit()
    assert revision() != after_progress


def test_bootstrap_answers_a_matching_if_none_match_with_304(client_db):
    """An unchanged home revalidates without rebuilding or re-sending it."""
    client, _db = client_db

    first = client.get("/api/v2/bootstrap", headers=_bearer("browse-user-key"))
    assert first.status_code == 200
    etag = first.headers["etag"]

    revalidated = client.get(
        "/api/v2/bootstrap",
        headers={**_bearer("browse-user-key"), "If-None-Match": etag},
    )

    assert revalidated.status_code == 304
    assert revalidated.headers["etag"] == etag
    assert not revalidated.content

    # A stale tag must still get the full document.
    assert (
        client.get(
            "/api/v2/bootstrap",
            headers={
                **_bearer("browse-user-key"),
                "If-None-Match": '"something-else:0:24"',
            },
        ).status_code
        == 200
    )


def test_bootstrap_etag_changes_when_the_library_does(client_db):
    """The ETag must not survive a change to the activity half of the payload."""
    client, db = client_db
    profile = db.query(User).filter(User.user_key == "browse-user-key").first()

    etag = client.get("/api/v2/bootstrap", headers=_bearer("browse-user-key")).headers[
        "etag"
    ]

    db.add(
        LibraryItem(
            user_id=profile.id,
            tmdb_id=1858,
            media_type="movie",
            title="Transformers",
        )
    )
    db.commit()

    assert (
        client.get(
            "/api/v2/bootstrap",
            headers={**_bearer("browse-user-key"), "If-None-Match": etag},
        ).status_code
        == 200
    )


def test_native_discovery_rejects_an_unknown_profile_key(client_db):
    client, _db = client_db
    client.cookies.clear()

    # No credential at all is a 401 too, with a Bearer challenge.
    missing = client.get("/api/v2/facets")
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"].startswith("Bearer")

    assert (
        client.get("/api/v2/facets", headers=_bearer("unknown-key")).status_code == 401
    )
    assert (
        client.get("/api/v2/browse?tags=1", headers=_bearer("unknown-key")).status_code
        == 401
    )
    assert (
        client.get("/api/v2/search?q=Movie", headers=_bearer("unknown-key")).status_code
        == 401
    )
    assert (
        client.get(
            "/api/v2/details?tmdb_id=1&media_type=movie", headers=_bearer("unknown-key")
        ).status_code
        == 401
    )


# ---------------------------------------------------------------------------
# Browse
# ---------------------------------------------------------------------------
def test_browse_all_mode_requires_every_tag(client_db):
    client, db = client_db
    tags = _seed_corpus(db)

    body = client.get(
        f"/account/api/browse?tags={tags['Neo-Noir'].id},{tags['Bleak'].id}&mode=all"
    ).json()

    assert sorted(i["tmdb_id"] for i in body["items"]) == [1, 2]


def test_browse_any_mode_unions_them(client_db):
    client, db = client_db
    tags = _seed_corpus(db)

    body = client.get(
        f"/account/api/browse?tags={tags['Neo-Noir'].id},{tags['Bleak'].id}&mode=any"
    ).json()

    assert sorted(i["tmdb_id"] for i in body["items"]) == [1, 2, 3]


def test_browse_ranks_by_relevance_not_insertion_order(client_db):
    """Matching two of the query's tags must outrank matching one.

    This is the same property the catalog cutover fixed. A browse that ordered
    by popularity or by primary key would return the same *set* and a worse
    answer, which is exactly how the old ``avg(confidence)`` ordering failed.
    """
    client, db = client_db
    tags = _seed_corpus(db)

    body = client.get(
        f"/account/api/browse?tags={tags['Neo-Noir'].id},{tags['Bleak'].id}&mode=any"
    ).json()

    ids = [i["tmdb_id"] for i in body["items"]]
    relevances = [i["relevance"] for i in body["items"]]
    assert set(ids[:2]) == {1, 2}, "the two-tag titles must come first"
    assert ids[2] == 3, "matching one of the two query tags ranks below matching both"
    assert relevances[0] > relevances[-1]
    assert relevances == sorted(relevances, reverse=True)


def test_browse_paginates_and_says_whether_more_exist(client_db):
    client, db = client_db
    tags = _seed_corpus(db)

    first = client.get(f"/account/api/browse?tags={tags['Drama'].id}&limit=5").json()
    second = client.get(
        f"/account/api/browse?tags={tags['Drama'].id}&limit=5&skip=10"
    ).json()

    assert len(first["items"]) == 5
    assert first["has_more"] is True
    assert len(second["items"]) == 2
    assert second["has_more"] is False


def test_browse_with_no_tags_returns_nothing_rather_than_everything(client_db):
    """An empty filter is an unanswered question, not "the whole library"."""
    client, db = client_db
    _seed_corpus(db)

    body = client.get("/account/api/browse?tags=").json()

    assert body["items"] == []


def test_browse_ignores_junk_tag_ids(client_db):
    client, db = client_db
    tags = _seed_corpus(db)

    body = client.get(
        f"/account/api/browse?tags=abc,{tags['Neo-Noir'].id},;drop"
    ).json()

    assert body["total_tags"] == 1
    assert sorted(i["tmdb_id"] for i in body["items"]) == [1, 2, 3]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def test_search_intersects_with_tags_in_sql(client_db):
    client, db = client_db
    tags = _seed_corpus(db)

    body = client.get(f"/account/api/search?q=Movie&tags={tags['Bleak'].id}").json()

    assert body["result_type"] == "filtered"
    assert sorted(i["tmdb_id"] for i in body["items"]) == [1, 2]


def test_searching_a_vibe_returns_that_tags_titles(client_db):
    """Searching "Neo-Noir" should work like Netflix, not like grep."""
    client, db = client_db
    _seed_corpus(db)

    body = client.get("/account/api/search?q=Neo-Noir").json()

    assert body["result_type"] == "tag"
    assert body["tag"]["name"] == "Neo-Noir"
    assert sorted(i["tmdb_id"] for i in body["items"]) == [1, 2, 3]


def test_a_broad_genre_that_is_also_a_title_still_finds_the_title(client_db):
    """Genre intent must not hide a title literally named after the genre.

    "Drama" names a root genre and one exact title here. The search reads it
    as the genre -- that is what people typing "drama" want -- but the film
    called Drama must still lead the results.
    """
    client, db = client_db
    _seed_corpus(db)
    db.query(MediaMetadata).filter(MediaMetadata.tmdb_id == 1).update(
        {"title": "Drama"}
    )
    db.commit()

    body = client.get("/account/api/search?q=Drama").json()

    assert body["items"][0]["title"] == "Drama"
    assert body["genre"]["name"] == "Drama"
    assert len(body["items"]) == 12


def test_a_narrow_tag_that_is_also_a_title_still_searches_titles(client_db):
    """Only broad genres take precedence; a narrow tag keeps title-first rules.

    "Neo-Noir" is a sub-genre here. With a film called exactly that, the query
    is a title search, as before genre search existed.
    """
    client, db = client_db
    tags = _seed_corpus(db)
    tags["Neo-Noir"].parent_id = tags["Drama"].id
    db.query(MediaMetadata).filter(MediaMetadata.tmdb_id == 5).update(
        {"title": "Neo-Noir"}
    )
    db.commit()

    body = client.get("/account/api/search?q=Neo-Noir").json()

    assert body.get("result_type") != "tag"
    assert body["items"][0]["title"] == "Neo-Noir"


# ---------------------------------------------------------------------------
# Genre search
# ---------------------------------------------------------------------------
def _seed_genres(db):
    """Horror with two sub-genres, Comedy, a decade, and a horror series.

    Titles 101-104 are Folk Horror, 105-108 Slasher. Every horror film is
    *named* "Horror ..." so a title search for "horror" would match them all --
    which is exactly the case the old five-title ceiling turned away.
    """
    from app.models import UserTasteVector  # noqa: F401  (registers the table)

    def tag(name, facet="subgenre", parent=None):
        row = Tag(name=name, category=facet, parent_id=parent.id if parent else None)
        db.add(row)
        db.commit()
        return row

    tags = {"Horror": tag("Horror")}
    tags["Folk Horror"] = tag("Folk Horror", parent=tags["Horror"])
    tags["Slasher"] = tag("Slasher", parent=tags["Horror"])
    tags["Comedy"] = tag("Comedy")
    tags["1980s"] = tag("1980s", facet="release_decade")
    tags["Bleak"] = tag("Bleak", facet="mood")

    def title(tmdb_id, name, tag_names, media_type="movie", rating=7.0):
        db.add(
            MediaMetadata(
                tmdb_id=tmdb_id,
                media_type=media_type,
                title=name,
                overview="A complete synopsis with enough evidence for semantic tagging.",
                popularity=10.0,
                vote_average=rating,
                vote_count=1000,
                release_date="1985-01-01" if "1980s" in tag_names else "2015-01-01",
            )
        )
        for tag_name in tag_names:
            db.add(
                MovieTag(
                    tmdb_id=tmdb_id,
                    media_type=media_type,
                    tag_id=tags[tag_name].id,
                    confidence=0.9,
                )
            )

    for i in range(101, 105):
        title(i, f"Horror Folk {i}", ["Folk Horror", "Bleak"])
    for i in range(105, 109):
        title(i, f"Horror Slash {i}", ["Slasher", "1980s"], rating=7.5)
    title(109, "Plain Comedy", ["Comedy"])
    title(110, "Comedy Horror", ["Comedy", "Horror"])
    title(201, "Horror Series", ["Folk Horror"], media_type="tv")
    db.commit()
    refresh_tag_stats(db, create_indexes=False)
    return tags


def _give_taste(db, tags, *names, media_type="movie"):
    from app.models import UserTasteVector

    user = db.query(User).filter(User.user_key == "browse-user-key").one()
    for name in names:
        db.add(
            UserTasteVector(
                user_id=user.id, media_type=media_type, tag_id=tags[name].id, weight=1.0
            )
        )
    db.commit()
    return user


@pytest.mark.parametrize(
    "query, names, media_types, explicit",
    [
        ("horror", ["Horror"], ("movie", "tv"), True),
        ("Horror movies", ["Horror"], ("movie",), True),
        ("scary films", ["Horror"], ("movie",), True),
        ("comedies", ["Comedy"], ("movie", "tv"), True),
        ("best horror shows", ["Horror"], ("tv",), True),
        ("80s horror", ["1980s", "Horror"], ("movie", "tv"), True),
        ("slasher", ["Slasher"], ("movie", "tv"), False),
    ],
)
def test_genre_queries_parse_into_tags_and_types(
    client_db, query, names, media_types, explicit
):
    from app.genre_search import parse_genre_query

    _, db = client_db
    _seed_genres(db)

    parsed = parse_genre_query(db, query)

    assert [t.name for t in parsed.tags] == names
    assert parsed.media_types == media_types
    assert parsed.explicit is explicit


@pytest.mark.parametrize("query", ["american horror story", "the thing", "zzz"])
def test_titles_with_extra_words_are_not_genre_queries(client_db, query):
    from app.genre_search import parse_genre_query

    _, db = client_db
    _seed_genres(db)

    assert parse_genre_query(db, query) is None


def test_a_root_genre_includes_its_subgenres(client_db):
    """Searching "horror" finds Folk Horror and Slasher titles too, not a title grep."""
    client, db = client_db
    _seed_genres(db)

    body = client.get("/account/api/search?q=horror").json()

    ids = {i["tmdb_id"] for i in body["items"]}
    assert body["result_type"] == "tag"
    assert {101, 105, 110, 201} <= ids
    assert 109 not in ids


def test_genre_search_is_ordered_by_the_profiles_taste(client_db):
    client, db = client_db
    tags = _seed_genres(db)

    # Without taste, nothing pulls the lower-rated Folk Horror films forward.
    neutral = client.get("/account/api/search?q=horror movies").json()
    assert neutral["items"][0]["tmdb_id"] not in range(101, 105)

    _give_taste(db, tags, "Folk Horror", "Bleak")
    tasted = client.get("/account/api/search?q=horror movies").json()

    assert all(i["media_type"] == "movie" for i in tasted["items"])
    assert {i["tmdb_id"] for i in tasted["items"][:4]} == set(range(101, 105))
    assert set(tasted["items"][0]["reason"].split(" · ")) == {"Folk Horror", "Bleak"}


def test_genre_search_groups_results_into_sections(client_db):
    client, db = client_db
    tags = _seed_genres(db)
    _give_taste(db, tags, "Folk Horror")

    body = client.get("/account/api/search?q=horror").json()
    titles = [s["title"] for s in body["sections"]]

    assert titles[0] == "Top Horror picks for you"
    assert "Because you like Folk Horror" in titles
    assert "Highly rated Horror" in titles
    assert "Horror series" in titles
    because = next(s for s in body["sections"] if s["id"].startswith("because-"))
    assert {i["tmdb_id"] for i in because["items"]} == {101, 102, 103, 104, 201}


def test_finished_films_sink_below_unwatched_ones(client_db):
    from app.models import WatchEvent

    client, db = client_db
    tags = _seed_genres(db)
    user = _give_taste(db, tags, "Folk Horror")
    db.add(
        WatchEvent(user_id=user.id, tmdb_id=101, media_type="movie", action="complete")
    )
    db.commit()

    body = client.get("/account/api/search?q=horror movies").json()
    ids = [i["tmdb_id"] for i in body["items"]]

    assert ids.index(101) > ids.index(102)
    top = body["sections"][0]["items"]
    assert 101 not in {i["tmdb_id"] for i in top}


def test_combined_terms_require_every_term(client_db):
    client, db = client_db
    _seed_genres(db)

    body = client.get("/account/api/search?q=80s horror").json()

    assert {i["tmdb_id"] for i in body["items"]} == set(range(105, 109))


def test_native_genre_search_uses_the_profile_key(client_db):
    client, db = client_db
    tags = _seed_genres(db)
    _give_taste(db, tags, "Slasher")
    client.cookies.clear()

    body = client.get(
        "/api/v2/search?q=scary movies", headers=_bearer("browse-user-key")
    ).json()

    assert body["items"][0]["tmdb_id"] in range(105, 109)
    assert body["sections"][0]["title"] == "Top Horror picks for you"


# ---------------------------------------------------------------------------
# Detail page
# ---------------------------------------------------------------------------
def _tmdb_detail(tmdb_id=1, title="Movie 1"):
    return {
        "id": tmdb_id,
        "title": title,
        "overview": "x",
        "genres": [],
        "release_date": "2015-01-01",
        "credits": {"cast": [], "crew": []},
        "external_ids": {"imdb_id": f"tt{tmdb_id:07d}"},
        "release_dates": {},
    }


@contextmanager
def _patch_tmdb(similar=None, tmdb_id=1):
    """Stub TMDB and hand back the /similar mock, so a test can assert it was
    never awaited -- "we used our own tags" and "we called TMDB anyway and
    ignored the answer" are indistinguishable from the payload alone."""
    similar_mock = AsyncMock(return_value={"results": similar or []})
    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_movie=AsyncMock(return_value=_tmdb_detail(tmdb_id)),
        get_similar_movies=similar_mock,
    ):
        yield similar_mock


def _assert_addon_type(payload, expected):
    """``addon_type`` matches ``content_type`` on the item and every similar entry.

    Every similar entry also needs ``media_type``:
    deriving the type from it is a client's fallback when ``addon_type`` is absent,
    so an entry carrying neither would be mistyped as a movie.
    """
    item = payload["item"]
    assert item["addon_type"] == item["content_type"] == expected
    for entry in payload["similar"]:
        assert entry["media_type"] in {"movie", "tv"}
        assert entry["addon_type"] == entry["content_type"] == expected


def test_details_carry_display_tags_ordered_by_weight(client_db):
    client, db = client_db
    _seed_corpus(db)

    with _patch_tmdb():
        body = client.get("/account/api/details?tmdb_id=1&media_type=movie").json()

    names = [t["name"] for t in body["tags"]]
    assert "Internal Only" not in names, "display=False tags are not chips"
    # Neo-Noir is on 3 of 12 titles, Drama on all 12, so Neo-Noir carries the
    # higher IDF weight and leads the chip row. Ordering by confidence would
    # have put Drama first on ties, which is the uninformative answer.
    assert names[0] == "Neo-Noir"
    assert set(names) == {"Neo-Noir", "Drama", "Bleak"}


def test_details_similar_comes_from_our_tags_and_says_so(client_db):
    client, db = client_db
    _seed_corpus(db)

    with _patch_tmdb() as similar_mock:
        body = client.get("/account/api/details?tmdb_id=1&media_type=movie").json()

    assert body["similar_source"] == "tags"
    assert sorted(i["tmdb_id"] for i in body["similar"])[:2] == [2, 3]
    _assert_addon_type(body, "movie")
    # And TMDB's /similar was not called at all -- it used to be awaited
    # serially on every detail view.
    similar_mock.assert_not_awaited()


def test_details_fall_back_to_tmdb_for_an_untagged_title(client_db):
    """An untagged title still gets a rail, and it is labelled honestly."""
    client, db = client_db
    _seed_corpus(db)
    db.add(
        MediaMetadata(tmdb_id=999, media_type="movie", title="Untagged", popularity=1.0)
    )
    db.commit()

    detail = {
        "id": 999,
        "title": "Untagged",
        "overview": "",
        "genres": [],
        "release_date": "",
        "credits": {"cast": [], "crew": []},
        "external_ids": {},
        "release_dates": {},
    }
    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_movie=AsyncMock(return_value=detail),
        get_similar_movies=AsyncMock(
            return_value={"results": [{"id": 5, "title": "From TMDB"}]}
        ),
    ):
        body = client.get("/account/api/details?tmdb_id=999&media_type=movie").json()

    assert body["similar_source"] == "tmdb"
    assert [i["tmdb_id"] for i in body["similar"]] == [5]
    _assert_addon_type(body, "movie")


def test_details_fall_back_to_local_catalog_when_tmdb_has_no_similar(client_db):
    """A provider gap must not become a permanently missing detail row."""
    client, db = client_db
    _seed_corpus(db)
    db.add(
        MediaMetadata(
            tmdb_id=999,
            media_type="movie",
            title="Untagged",
            genres=["Drama"],
            popularity=1.0,
        )
    )
    db.commit()

    detail = {
        "id": 999,
        "title": "Untagged",
        "overview": "",
        "genres": [{"name": "Drama"}],
        "release_date": "",
        "credits": {"cast": [], "crew": []},
        "external_ids": {},
        "release_dates": {},
    }
    with (
        patch.multiple(
            "app.tmdb_client.tmdb_client",
            get_movie=AsyncMock(return_value=detail),
            get_similar_movies=AsyncMock(return_value={"results": []}),
        ),
        patch("app.relevance.similar_by_vector", return_value=[]),
    ):
        body = client.get("/account/api/details?tmdb_id=999&media_type=movie").json()

    assert body["similar_source"] == "catalog"
    assert body["similar"]
    assert all(item["tmdb_id"] != 999 for item in body["similar"])
    _assert_addon_type(body, "movie")


def test_details_carry_multi_source_ratings_when_mdblist_covered_the_title(client_db):
    client, db = client_db
    _seed_corpus(db)
    db.add_all(
        [
            TitleRating(
                tmdb_id=1,
                media_type="movie",
                source="imdb",
                value=6.7,
                normalized=6.7,
                votes=2259494,
            ),
            TitleRating(
                tmdb_id=1,
                media_type="movie",
                source="tomatoes",
                value=93,
                normalized=9.3,
            ),
            TitleRating(
                tmdb_id=1,
                media_type="movie",
                source="metacritic",
                value=85,
                normalized=8.5,
            ),
            TitleRating(
                tmdb_id=1,
                media_type="movie",
                source="rogerebert",
                value=3,
                normalized=7.5,
            ),
        ]
    )
    db.commit()

    with _patch_tmdb():
        body = client.get("/account/api/details?tmdb_id=1&media_type=movie").json()

    assert [r["source"] for r in body["ratings"]] == [
        "tomatoes",
        "metacritic",
        "rogerebert",
        "imdb",
    ]
    assert body["ratings"][0]["label"] == "Rotten Tomatoes"
    # Product order is independent of score magnitude, and the server supplies
    # one percentage representation while retaining native `value` compatibility.
    assert [r["percentage"] for r in body["ratings"]] == [93, 85, 75, 67]
    assert body["ratings"][2]["value"] == 3
    assert body["ratings"][2]["normalized"] == 7.5


def test_details_ratings_are_empty_rather_than_invented(client_db):
    client, db = client_db
    _seed_corpus(db)

    with _patch_tmdb():
        body = client.get("/account/api/details?tmdb_id=1&media_type=movie").json()

    assert body["ratings"] == []


def test_details_include_meta_carries_movie_playback_keys(client_db):
    client, db = client_db
    _seed_corpus(db)
    client.cookies.clear()

    with _patch_tmdb():
        response = client.get(
            "/api/v2/details" "?tmdb_id=1&media_type=movie&include=meta",
            headers=_bearer("browse-user-key"),
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, max-age=1800"
    meta = response.json()["meta"]
    assert (meta["id"], meta["type"], meta["name"]) == (
        "tmdb:1",
        "movie",
        "Movie 1",
    )
    assert meta["default_video_id"] == "tt0000001"
    assert [video["id"] for video in meta["videos"]] == ["tt0000001"]


def test_details_include_meta_echoes_requested_imdb_movie_identity(client_db):
    client, db = client_db
    _seed_corpus(db)
    client.cookies.clear()
    detail = _tmdb_detail()
    detail["external_ids"] = {"imdb_id": "tt9999999"}

    with (
        patch(
            "app.main._resolve_imdb_to_tmdb",
            new=AsyncMock(return_value={"tmdb_id": 1, "media_type": "movie"}),
        ),
        patch.multiple(
            "app.tmdb_client.tmdb_client",
            get_movie=AsyncMock(return_value=detail),
            get_similar_movies=AsyncMock(return_value={"results": []}),
        ),
    ):
        response = client.get(
            "/api/v2/details?imdb_id=tt0000001&include=meta",
            headers=_bearer("browse-user-key"),
        )

    meta = response.json()["meta"]
    assert meta["id"] == "tt0000001"
    assert meta["default_video_id"] == "tt0000001"
    assert [video["id"] for video in meta["videos"]] == ["tt0000001"]


def test_details_inline_meta_keeps_legacy_rich_fields(client_db):
    client, db = client_db
    _seed_corpus(db)
    client.cookies.clear()
    detail = _tmdb_detail()
    detail.update(
        {
            "poster_path": "/poster.jpg",
            "backdrop_path": "/backdrop.jpg",
            "runtime": 125,
            "credits": {
                "cast": [{"name": "Lead Actor", "profile_path": "/actor.jpg"}],
                "crew": [{"name": "Director Name", "job": "Director"}],
            },
            "images": {"logos": [{"file_path": "/logo.png"}]},
            "release_dates": {
                "results": [
                    {
                        "iso_3166_1": "US",
                        "release_dates": [
                            {"type": 4, "release_date": "2026-08-01T00:00:00Z"}
                        ],
                    }
                ]
            },
            "videos": {
                "results": [
                    {
                        "id": "trailer",
                        "type": "Trailer",
                        "site": "YouTube",
                        "key": "t1",
                    },
                    {"id": "teaser", "type": "Teaser", "site": "YouTube", "key": "t2"},
                ]
            },
        }
    )

    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_movie=AsyncMock(return_value=detail),
        get_similar_movies=AsyncMock(return_value={"results": []}),
    ):
        response = client.get(
            "/api/v2/details" "?tmdb_id=1&media_type=movie&include=meta",
            headers=_bearer("browse-user-key"),
        )

    meta = response.json()["meta"]
    assert meta["digitalReleaseInfo"] == "2026-08-01"
    assert meta["poster"] == "https://image.tmdb.org/t/p/original/poster.jpg"
    assert meta["background"] == "https://image.tmdb.org/t/p/original/backdrop.jpg"
    assert meta["logo"] == "https://image.tmdb.org/t/p/original/logo.png"
    assert meta["runtime"] == "2h 5min"
    assert [trailer["source"] for trailer in meta["trailers"]] == ["t1"]
    assert [(link["name"], link["category"]) for link in meta["links"]] == [
        ("Lead Actor", "Cast"),
        ("Director Name", "Directors"),
    ]


def test_details_include_meta_carries_series_episode_ids(client_db):
    client, _ = client_db
    client.cookies.clear()
    detail = {
        "id": 202,
        "name": "Deep Space",
        "overview": "A crew explores.",
        "genres": [],
        "first_air_date": "2025-01-01",
        "credits": {"cast": [], "crew": []},
        "external_ids": {"imdb_id": "tt0202000"},
        "episode_run_time": [60],
        "seasons": [{"season_number": 1, "name": "Season 1", "episode_count": 1}],
    }
    season = {
        "episodes": [
            {
                "season_number": 1,
                "episode_number": 1,
                "name": "First Light",
                "air_date": "2025-01-01",
            },
            {
                "season_number": 1,
                "episode_number": None,
                "name": "Malformed",
            },
        ]
    }
    season_mock = AsyncMock(side_effect=[TMDBServerError("temporary 500"), season])
    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_tv_show=AsyncMock(return_value=detail),
        get_tv_season=season_mock,
        get_similar_tv_shows=AsyncMock(return_value={"results": []}),
    ):
        response = client.get(
            "/api/v2/details" "?tmdb_id=202&media_type=tv&include=meta",
            headers=_bearer("browse-user-key"),
        )
        with patch(
            "app.main._resolve_imdb_to_tmdb",
            new=AsyncMock(return_value={"tmdb_id": 202, "media_type": "tv"}),
        ):
            imdb_response = client.get(
                "/api/v2/details?imdb_id=tt0202000&include=meta",
                headers=_bearer("browse-user-key"),
            )

    assert response.status_code == 200
    meta = response.json()["meta"]
    assert (meta["id"], meta["type"], meta["name"]) == (
        "tmdb:202",
        "series",
        "Deep Space",
    )
    assert [video["id"] for video in meta["videos"]] == ["tmdb:202:1:1"]
    assert "default_video_id" not in meta
    assert meta["episodesComplete"] is True
    assert meta["runtime"] == "60min"
    assert response.json()["episodes_complete"] is True
    assert response.json()["seasons"][0]["episodes_complete"] is True
    body = response.json()
    # The two vocabularies stay distinct: media_type keeps TMDB's "tv" while
    # addon_type mirrors content_type's "series".
    assert body["item"]["media_type"] == "tv"
    assert body["item"]["addon_type"] == body["item"]["content_type"] == "series"
    # Episodes are deliberately left out. The client decodes the object but never
    # reads the type from it, and the meta block already carries its own `type`.
    episode = body["seasons"][0]["episodes"][0]
    assert episode["content_type"] == "series"
    assert "addon_type" not in episode
    assert "addon_type" not in meta
    imdb_meta = imdb_response.json()["meta"]
    assert imdb_meta["id"] == "tt0202000"
    assert [video["id"] for video in imdb_meta["videos"]] == ["tt0202000:1:1"]
    assert "default_video_id" not in imdb_meta
    assert season_mock.await_count == 2


def test_find_result_prefers_the_requested_media_type():
    from app.tmdb_client import parse_find_result

    both = {
        "movie_results": [{"id": 11}],
        "tv_results": [{"id": 22}],
        "tv_episode_results": [],
    }
    assert parse_find_result(both)["media_type"] == "movie"
    assert parse_find_result(both, prefer_media_type="movie")["tmdb_id"] == 11
    assert parse_find_result(both, prefer_media_type="tv") == {
        "tmdb_id": 22,
        "media_type": "tv",
        "season": None,
        "episode": None,
    }
    # A preference never invents a result of that type.
    movie_only = {"movie_results": [{"id": 11}], "tv_results": []}
    assert parse_find_result(movie_only, prefer_media_type="tv")["tmdb_id"] == 11


def test_imdb_details_follow_the_requested_type_over_a_movie_stub(client_db):
    """TMDB can list one IMDb id under a movie stub and the real series."""
    client, _ = client_db
    client.cookies.clear()
    detail = {
        "id": 404,
        "name": "Twice Listed",
        "overview": "A series with a stub movie record.",
        "genres": [],
        "first_air_date": "2025-01-01",
        "credits": {"cast": [], "crew": []},
        "external_ids": {"imdb_id": "tt0404000"},
        "episode_run_time": [45],
        "seasons": [{"season_number": 1, "name": "Season 1", "episode_count": 1}],
    }
    season = {
        "episodes": [
            {
                "season_number": 1,
                "episode_number": 1,
                "name": "Pilot",
                "air_date": "2025-01-01",
            }
        ]
    }
    find_mock = AsyncMock(
        return_value={
            "movie_results": [{"id": 9404}],
            "tv_results": [{"id": 404}],
            "tv_episode_results": [],
        }
    )
    with (
        patch(
            "app.main._resolve_imdb_to_tmdb",
            new=AsyncMock(return_value={"tmdb_id": 9404, "media_type": "movie"}),
        ),
        patch.multiple(
            "app.tmdb_client.tmdb_client",
            find_by_imdb_id=find_mock,
            get_tv_show=AsyncMock(return_value=detail),
            get_tv_season=AsyncMock(return_value=season),
            get_similar_tv_shows=AsyncMock(return_value={"results": []}),
        ),
    ):
        response = client.get(
            "/api/v2/details" "?imdb_id=tt0404000&media_type=tv&include=meta",
            headers=_bearer("browse-user-key"),
        )
        repeat = client.get(
            "/api/v2/details" "?imdb_id=tt0404000&media_type=tv&include=meta",
            headers=_bearer("browse-user-key"),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["item"]["media_type"] == "tv"
    assert body["episodes_complete"] is True
    assert (body["meta"]["id"], body["meta"]["type"]) == ("tt0404000", "series")
    assert [video["id"] for video in body["meta"]["videos"]] == ["tt0404000:1:1"]
    assert repeat.status_code == 200
    # The typed lookup is memoized; the second request does not ask TMDB again.
    assert find_mock.await_count == 1


def test_partial_series_details_are_flagged_and_not_cached(client_db):
    client, _ = client_db
    client.cookies.clear()
    detail = {
        "id": 303,
        "name": "Interrupted",
        "genres": [],
        "credits": {"cast": [], "crew": []},
        "external_ids": {"imdb_id": "tt0303000"},
        "seasons": [{"season_number": 1, "name": "Season 1", "episode_count": 8}],
    }
    show_mock = AsyncMock(return_value=detail)
    season_mock = AsyncMock(side_effect=RuntimeError("season unavailable"))
    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_tv_show=show_mock,
        get_tv_season=season_mock,
        get_similar_tv_shows=AsyncMock(return_value={"results": []}),
    ):
        first = client.get(
            "/api/v2/details" "?tmdb_id=303&media_type=tv&include=meta",
            headers=_bearer("browse-user-key"),
        )
        second = client.get(
            "/api/v2/details" "?tmdb_id=303&media_type=tv&include=meta",
            headers=_bearer("browse-user-key"),
        )

    for response in (first, second):
        body = response.json()
        assert response.headers["cache-control"] == "private, no-store"
        assert body["episodes_complete"] is False
        assert body["seasons"][0]["episodes_complete"] is False
        assert body["meta"]["episodesComplete"] is False
        assert body["meta"]["videos"] == []
        assert "default_video_id" not in body["meta"]
    assert show_mock.await_count == 2
    assert season_mock.await_count == 2


def test_episode_empty_complete_snapshot_is_rebuilt_on_read(client_db):
    client, db = client_db
    client.cookies.clear()
    db.add(
        DetailSnapshot(
            tmdb_id=404,
            media_type="tv",
            payload={
                "item": {"tmdb_id": 404, "media_type": "tv", "title": "Popular"},
                "episodes_complete": True,
                "seasons": [
                    {
                        "season": 1,
                        "episode_count": 10,
                        "episodes_complete": True,
                        "episodes": [],
                    }
                ],
            },
            episodes_complete=True,
            schema_version=main._DETAIL_SNAPSHOT_SCHEMA_VERSION,
        )
    )
    db.commit()

    detail = {
        "id": 404,
        "name": "Popular",
        "genres": [],
        "credits": {"cast": [], "crew": []},
        "seasons": [{"season_number": 1, "name": "Season 1", "episode_count": 10}],
    }
    season = {
        "episodes": [
            {
                "season_number": 1,
                "episode_number": 1,
                "name": "Pilot",
                "air_date": "2020-01-01",
            }
        ]
    }
    show_mock = AsyncMock(return_value=detail)
    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_tv_show=show_mock,
        get_tv_season=AsyncMock(return_value=season),
        get_similar_tv_shows=AsyncMock(return_value={"results": []}),
    ):
        response = client.get(
            "/api/v2/details?tmdb_id=404&media_type=tv&include=meta",
            headers=_bearer("browse-user-key"),
        )

    assert response.status_code == 200
    assert [video["id"] for video in response.json()["meta"]["videos"]] == [
        "tmdb:404:1:1"
    ]
    show_mock.assert_awaited_once()


def test_tv_with_no_regular_seasons_is_not_frozen_as_complete(client_db):
    """A provider body with no seasons array is a failed build, not a finished show.

    `all([])` is True, so this used to pass every completeness check and be
    persisted as an authoritative, permanently episode-less snapshot.
    """
    client, db = client_db
    client.cookies.clear()
    detail = {
        "id": 505,
        "name": "Seasonless",
        "genres": [],
        "credits": {"cast": [], "crew": []},
        "number_of_episodes": 40,
        # No "seasons" key at all — a trimmed or partial TMDB response.
    }
    show_mock = AsyncMock(return_value=detail)
    season_mock = AsyncMock(return_value={"episodes": []})
    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_tv_show=show_mock,
        get_tv_season=season_mock,
        get_similar_tv_shows=AsyncMock(return_value={"results": []}),
    ):
        first = client.get(
            "/api/v2/details?tmdb_id=505&media_type=tv&include=meta",
            headers=_bearer("browse-user-key"),
        )
        second = client.get(
            "/api/v2/details?tmdb_id=505&media_type=tv&include=meta",
            headers=_bearer("browse-user-key"),
        )

    for response in (first, second):
        body = response.json()
        assert body["episodes_complete"] is False
        assert body["meta"]["videos"] == []
        assert response.headers["cache-control"] == "private, no-store"
    assert db.get(DetailSnapshot, (505, "tv")) is None
    # Not memoized either: the second request rebuilt from the provider.
    assert show_mock.await_count == 2
    assert season_mock.await_count == 0


def test_tv_with_genuinely_no_regular_seasons_is_still_complete(client_db):
    """The legitimate case must keep working: a specials-only, unaired show."""
    client, db = client_db
    client.cookies.clear()
    detail = {
        "id": 506,
        "name": "Not Yet Aired",
        "genres": [],
        "credits": {"cast": [], "crew": []},
        "number_of_episodes": 0,
        "seasons": [{"season_number": 0, "name": "Specials", "episode_count": 2}],
    }
    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_tv_show=AsyncMock(return_value=detail),
        get_tv_season=AsyncMock(return_value={"episodes": []}),
        get_similar_tv_shows=AsyncMock(return_value={"results": []}),
    ):
        response = client.get(
            "/api/v2/details?tmdb_id=506&media_type=tv&include=meta",
            headers=_bearer("browse-user-key"),
        )

    assert response.json()["episodes_complete"] is True
    assert response.headers["cache-control"] == "private, max-age=1800"
    assert db.get(DetailSnapshot, (506, "tv")) is not None


def test_zero_episode_count_seasons_are_not_persisted(client_db):
    """A 200 whose `episodes` array is empty is a failure, like a 5xx would be."""
    client, db = client_db
    client.cookies.clear()
    detail = {
        "id": 507,
        "name": "Empty Envelope",
        "genres": [],
        "credits": {"cast": [], "crew": []},
        # No episode_count, so `_detail_expected_regular_episode_count` is 0 and
        # only the outcome check can catch this.
        "seasons": [{"season_number": 1, "name": "Season 1"}],
    }
    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_tv_show=AsyncMock(return_value=detail),
        get_tv_season=AsyncMock(return_value={"episodes": []}),
        get_similar_tv_shows=AsyncMock(return_value={"results": []}),
    ):
        response = client.get(
            "/api/v2/details?tmdb_id=507&media_type=tv&include=meta",
            headers=_bearer("browse-user-key"),
        )

    body = response.json()
    assert body["episodes_complete"] is False
    assert body["meta"]["videos"] == []
    assert response.headers["cache-control"] == "private, no-store"
    assert db.get(DetailSnapshot, (507, "tv")) is None


def test_episodeless_tv_snapshot_is_quarantined_on_read(client_db):
    """The production migration: rows written before the rule heal on next access.

    Distinct from `test_episode_empty_complete_snapshot_is_rebuilt_on_read`, whose
    payload carries `episode_count: 10` and is therefore caught by the expected-count
    rule. Here the expected count is 0 from every source, which is exactly the shape
    the old `_provider_detail`-presence check accepted forever: the provider body is
    present but says nothing about seasons, while `number_of_episodes` proves the
    show does in fact have episodes.
    """
    client, db = client_db
    client.cookies.clear()
    db.add(
        DetailSnapshot(
            tmdb_id=508,
            media_type="tv",
            payload={
                "item": {"tmdb_id": 508, "media_type": "tv", "title": "Frozen Empty"},
                "episodes_complete": True,
                "seasons": [],
                "_provider_detail": {"id": 508, "number_of_episodes": 6},
            },
            episodes_complete=True,
            schema_version=main._DETAIL_SNAPSHOT_SCHEMA_VERSION,
        )
    )
    db.commit()

    detail = {
        "id": 508,
        "name": "Frozen Empty",
        "genres": [],
        "credits": {"cast": [], "crew": []},
        "seasons": [{"season_number": 1, "name": "Season 1", "episode_count": 1}],
    }
    season = {
        "episodes": [
            {"season_number": 1, "episode_number": 1, "name": "Pilot"},
        ]
    }
    show_mock = AsyncMock(return_value=detail)
    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_tv_show=show_mock,
        get_tv_season=AsyncMock(return_value=season),
        get_similar_tv_shows=AsyncMock(return_value={"results": []}),
    ):
        response = client.get(
            "/api/v2/details?tmdb_id=508&media_type=tv&include=meta",
            headers=_bearer("browse-user-key"),
        )

    assert [video["id"] for video in response.json()["meta"]["videos"]] == [
        "tmdb:508:1:1"
    ]
    show_mock.assert_awaited_once()
    db.expire_all()
    assert (
        main._detail_usable_episode_count(db.get(DetailSnapshot, (508, "tv")).payload)
        == 1
    )


def test_detail_response_header_reports_episode_counts(client_db):
    client, _ = client_db
    client.cookies.clear()
    detail = {
        "id": 509,
        "name": "Counted",
        "genres": [],
        "credits": {"cast": [], "crew": []},
        "seasons": [{"season_number": 1, "name": "Season 1", "episode_count": 2}],
    }
    season = {
        "episodes": [
            {"season_number": 1, "episode_number": 1, "name": "One"},
            {"season_number": 1, "episode_number": 2, "name": "Two"},
        ]
    }
    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_tv_show=AsyncMock(return_value=detail),
        get_tv_season=AsyncMock(return_value=season),
        get_similar_tv_shows=AsyncMock(return_value={"results": []}),
    ):
        response = client.get(
            "/api/v2/details?tmdb_id=509&media_type=tv&include=meta",
            headers=_bearer("browse-user-key"),
        )

    header = response.headers["x-curatio-detail-episodes"]
    assert "usable=2" in header
    assert "expected=2" in header
    assert "complete=true" in header
    assert "type=tv" in header
    assert "source=build" in header


def test_clear_detail_response_cache_drops_meta_cache_details():
    """Overlay-only invalidation left episode bodies serving for an hour."""
    main._meta_cache["web-detail:tv:900"] = (main.time(), {"item": {}})
    main._meta_cache["meta:tv:900"] = (main.time(), {"id": "tmdb:900"})
    main._meta_cache["unrelated:900"] = (main.time(), {})
    try:
        main.clear_detail_response_cache()
        assert "web-detail:tv:900" not in main._meta_cache
        assert "meta:tv:900" not in main._meta_cache
        assert "unrelated:900" in main._meta_cache
    finally:
        main._meta_cache.pop("unrelated:900", None)


def test_details_refresh_bypasses_server_snapshot_and_memory_cache(client_db):
    client, db = client_db
    _seed_corpus(db)
    client.cookies.clear()
    movie_mock = AsyncMock(return_value=_tmdb_detail())

    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_movie=movie_mock,
        get_similar_movies=AsyncMock(return_value={"results": []}),
    ):
        first = client.get(
            "/api/v2/details?tmdb_id=1&media_type=movie&include=meta",
            headers=_bearer("browse-user-key"),
        )
        cached = client.get(
            "/api/v2/details?tmdb_id=1&media_type=movie&include=meta",
            headers=_bearer("browse-user-key"),
        )
        refreshed = client.get(
            "/api/v2/details" "?tmdb_id=1&media_type=movie&include=meta&refresh=true",
            headers=_bearer("browse-user-key"),
        )
        cooldown_hit = client.get(
            "/api/v2/details" "?tmdb_id=1&media_type=movie&include=meta&refresh=true",
            headers=_bearer("browse-user-key"),
        )

    assert (
        first.status_code
        == cached.status_code
        == refreshed.status_code
        == cooldown_hit.status_code
        == 200
    )
    assert movie_mock.await_count == 2


def test_details_refresh_is_not_allowed_for_batches(client_db):
    client, _ = client_db
    client.cookies.clear()
    response = client.get(
        "/api/v2/details?ids=movie:1&include=meta&refresh=true",
        headers=_bearer("browse-user-key"),
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "refresh cannot be combined with ids"


def test_details_batch_is_capped_and_keeps_partial_results(client_db):
    client, db = client_db
    _seed_corpus(db)
    client.cookies.clear()

    async def movie_detail(tmdb_id):
        if tmdb_id == 2:
            raise RuntimeError("provider failure")
        if tmdb_id == 3:
            request = httpx.Request("GET", "https://api.themoviedb.org/3/movie/3")
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("not found", request=request, response=response)
        return _tmdb_detail(tmdb_id)

    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_movie=AsyncMock(side_effect=movie_detail),
        get_similar_movies=AsyncMock(return_value={"results": []}),
    ):
        response = client.get(
            "/api/v2/details" "?ids=movie:1,bad-id,movie:2,movie:3&include=meta",
            headers=_bearer("browse-user-key"),
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    body = response.json()
    assert (body["requested"], body["returned"]) == (4, 1)
    assert body["results"][0]["id"] == "movie:1"
    assert body["results"][0]["details"]["meta"]["id"] == "tmdb:1"
    details = body["results"][0]["details"]
    assert details["similar"], "movie:1 is tagged, so its rail is populated"
    _assert_addon_type(details, "movie")
    assert [(error["id"], error["status"]) for error in body["errors"]] == [
        ("bad-id", 400),
        ("movie:2", 502),
        ("movie:3", 404),
    ]

    over_limit = ",".join(f"movie:{index}" for index in range(1, 42))
    capped = client.get(
        f"/api/v2/details?ids={over_limit}", headers=_bearer("browse-user-key")
    )
    assert capped.status_code == 400
    assert capped.json()["detail"] == "ids accepts at most 40 items"


def test_details_batch_coalesces_duplicate_ids(client_db):
    client, db = client_db
    _seed_corpus(db)
    client.cookies.clear()
    movie_mock = AsyncMock(return_value=_tmdb_detail())

    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_movie=movie_mock,
        get_similar_movies=AsyncMock(return_value={"results": []}),
    ):
        response = client.get(
            "/api/v2/details?ids=movie:1,movie:1&include=meta",
            headers=_bearer("browse-user-key"),
        )

    body = response.json()
    assert (body["requested"], body["returned"]) == (2, 2)
    assert [result["id"] for result in body["results"]] == ["movie:1", "movie:1"]
    movie_mock.assert_awaited_once()


def test_details_batch_types_each_entry_by_its_own_media_type(client_db):
    """A mixed batch types every entry individually, not once for the request."""
    client, db = client_db
    _seed_corpus(db)
    _seed_one_series(db)
    client.cookies.clear()

    series = {
        "id": 101,
        "name": "Show 101",
        "overview": "x",
        "genres": [],
        "first_air_date": "2018-01-01",
        "credits": {"cast": [], "crew": []},
        "external_ids": {"imdb_id": "tt0000101"},
        "episode_run_time": [45],
        "seasons": [{"season_number": 1, "name": "Season 1", "episode_count": 1}],
    }
    season = {
        "episodes": [
            {
                "season_number": 1,
                "episode_number": 1,
                "name": "Pilot",
                "air_date": "2018-01-01",
            }
        ]
    }
    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_movie=AsyncMock(return_value=_tmdb_detail()),
        get_similar_movies=AsyncMock(return_value={"results": []}),
        get_tv_show=AsyncMock(return_value=series),
        get_tv_season=AsyncMock(return_value=season),
        get_similar_tv_shows=AsyncMock(return_value={"results": []}),
    ):
        response = client.get(
            "/api/v2/details?ids=movie:1,tv:101&include=meta",
            headers=_bearer("browse-user-key"),
        )

    assert response.status_code == 200
    entries = {result["id"]: result["details"] for result in response.json()["results"]}
    assert set(entries) == {"movie:1", "tv:101"}
    assert entries["movie:1"]["similar"] and entries["tv:101"]["similar"]
    _assert_addon_type(entries["movie:1"], "movie")
    _assert_addon_type(entries["tv:101"], "series")
    assert entries["tv:101"]["item"]["media_type"] == "tv"


def test_details_batch_gives_each_item_its_own_session(client_db):
    """Gathered items must not share one Session.

    Batch details fan out concurrently and run their queries on worker threads, so
    a shared Session would be used from two threads at once.
    """
    client, db = client_db
    _seed_corpus(db)
    client.cookies.clear()

    sessions = []
    real_facts = main._detail_build_facts

    def record_session(session, tmdb_id, media_type):
        sessions.append(id(session))
        return real_facts(session, tmdb_id, media_type)

    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_movie=AsyncMock(side_effect=lambda tmdb_id: _tmdb_detail(tmdb_id)),
        get_similar_movies=AsyncMock(return_value={"results": []}),
    ):
        with patch.object(main, "_detail_build_facts", side_effect=record_session):
            response = client.get(
                "/api/v2/details?ids=movie:1,movie:2,movie:3",
                headers=_bearer("browse-user-key"),
            )

    assert response.json()["returned"] == 3
    assert len(sessions) == len(set(sessions)) == 3


@pytest.mark.asyncio
async def test_detail_provider_wait_holds_no_database_connection(client_db):
    """TMDB awaits must happen after the facts transaction releases its checkout."""
    _, db = client_db
    _seed_corpus(db)
    checked_out_during_provider = []

    async def movie_detail(tmdb_id):
        checked_out_during_provider.append(engine.pool.checkedout())
        await asyncio.sleep(0)
        checked_out_during_provider.append(engine.pool.checkedout())
        return _tmdb_detail(tmdb_id)

    async def similar_movies(_tmdb_id):
        checked_out_during_provider.append(engine.pool.checkedout())
        return {"results": []}

    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_movie=AsyncMock(side_effect=movie_detail),
        get_similar_movies=AsyncMock(side_effect=similar_movies),
    ):
        payload = await main._singleflight_detail_build(1, "movie", background=False)

    assert payload["item"]["tmdb_id"] == 1
    assert len(checked_out_during_provider) >= 2
    assert set(checked_out_during_provider) == {0}


@pytest.mark.asyncio
async def test_concurrent_detail_provider_waits_do_not_consume_pool(client_db):
    """A cache-miss burst may fan out providers without fanning out checkouts."""
    _, db = client_db
    _seed_corpus(db)
    build_count = 12
    entered = 0
    all_entered = asyncio.Event()
    observed = []

    async def held_movie_detail(tmdb_id):
        nonlocal entered
        entered += 1
        if entered == build_count:
            observed.append(engine.pool.checkedout())
            all_entered.set()
        await asyncio.wait_for(all_entered.wait(), timeout=2)
        return _tmdb_detail(tmdb_id)

    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_movie=AsyncMock(side_effect=held_movie_detail),
        get_similar_movies=AsyncMock(return_value={"results": []}),
    ):
        payloads = await asyncio.gather(
            *[
                main._singleflight_detail_build(i, "movie", background=False)
                for i in range(1, build_count + 1)
            ]
        )

    assert len(payloads) == build_count
    assert observed == [0]


@pytest.mark.asyncio
async def test_imdb_cache_lookup_does_not_block_event_loop(client_db):
    """A contended synchronous lookup is offloaded instead of freezing Uvicorn."""
    import time as stdlib_time

    heartbeat_ran = asyncio.Event()

    def slow_cached_lookup(_db, _imdb_id):
        stdlib_time.sleep(0.08)
        return (
            {"tmdb_id": 1, "media_type": "movie", "season": None, "episode": None},
            True,
        )

    async def heartbeat():
        await asyncio.sleep(0.01)
        heartbeat_ran.set()

    with patch.object(main, "_read_imdb_resolution_cache", slow_cached_lookup):
        resolved, _ = await asyncio.gather(
            main._resolve_imdb_to_tmdb("tt1234567"), heartbeat()
        )

    assert heartbeat_ran.is_set()
    assert resolved["tmdb_id"] == 1


def test_complete_detail_snapshot_survives_memory_cache_reset(client_db):
    client, db = client_db
    _seed_corpus(db)
    client.cookies.clear()
    movie_mock = AsyncMock(return_value=_tmdb_detail())

    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_movie=movie_mock,
        get_similar_movies=AsyncMock(return_value={"results": []}),
    ):
        first = client.get(
            "/api/v2/details?tmdb_id=1&media_type=movie&include=meta",
            headers=_bearer("browse-user-key"),
        )
        from app.main import _meta_cache

        _meta_cache.clear()
        second = client.get(
            "/api/v2/details?tmdb_id=1&media_type=movie&include=meta",
            headers=_bearer("browse-user-key"),
        )

    assert first.status_code == second.status_code == 200
    assert first.json()["meta"] == second.json()["meta"]
    assert db.get(DetailSnapshot, (1, "movie")) is not None
    movie_mock.assert_awaited_once()


def test_complete_details_cache_composed_dynamic_fields(client_db):
    client, db = client_db
    _seed_corpus(db)
    client.cookies.clear()

    with (
        patch.multiple(
            "app.tmdb_client.tmdb_client",
            get_movie=AsyncMock(return_value=_tmdb_detail()),
            get_similar_movies=AsyncMock(return_value={"results": []}),
        ),
        patch("app.relevance.similar_by_vector", return_value=[]) as similar,
    ):
        first = client.get(
            "/api/v2/details?tmdb_id=1&media_type=movie&include=meta",
            headers=_bearer("browse-user-key"),
        )
        calls_after_first = similar.call_count
        second = client.get(
            "/api/v2/details?tmdb_id=1&media_type=movie&include=meta",
            headers=_bearer("browse-user-key"),
        )

    assert first.status_code == second.status_code == 200
    assert calls_after_first > 0
    assert similar.call_count == calls_after_first
    assert second.headers["server-timing"].startswith("curatio-details;dur=")
    # The alias cannot depend on which layer served the request: the response is
    # cacheable for 1800s, so a client may read either of these bodies.
    for served in (first.json(), second.json()):
        assert served["similar"]
        _assert_addon_type(served, "movie")


def test_snapshot_write_seeds_composed_detail_cache(client_db):
    _, db = client_db
    from app.main import (
        _detail_response_cache_get,
        _detail_response_cache_store,
        _persist_detail_snapshot,
    )

    _detail_response_cache_store(1, "movie", {"item": {"title": "Old"}})
    assert _detail_response_cache_get(1, "movie") is not None

    assert _persist_detail_snapshot(
        db,
        1,
        "movie",
        {
            "item": {"title": "New"},
            "ratings": [{"source": "TMDB", "value": 9.0}],
        },
    )
    assert _detail_response_cache_get(1, "movie")["ratings"] == [
        {"source": "TMDB", "value": 9.0}
    ]
    assert db.get(DetailSnapshot, (1, "movie")).overlay_refreshed_at is not None


def test_startup_migration_adds_overlay_freshness_column(tmp_path, monkeypatch):
    from sqlalchemy import inspect, text

    import app.database as database

    migration_engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    migration_sessions = sessionmaker(
        autocommit=False, autoflush=False, bind=migration_engine
    )
    Base.metadata.create_all(bind=migration_engine)
    with migration_engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE detail_snapshots DROP COLUMN overlay_refreshed_at")
        )

    assert "overlay_refreshed_at" not in {
        column["name"]
        for column in inspect(migration_engine).get_columns("detail_snapshots")
    }
    monkeypatch.setattr(database, "engine", migration_engine)
    monkeypatch.setattr(database, "SessionLocal", migration_sessions)

    database._add_missing_columns()

    assert "overlay_refreshed_at" in {
        column["name"]
        for column in inspect(migration_engine).get_columns("detail_snapshots")
    }


@pytest.mark.asyncio
async def test_warmer_skips_expensive_composition_for_fresh_snapshot(client_db):
    _, db = client_db
    from workers.detail_snapshot_warm import Candidate, warm_candidate

    db.add(
        DetailSnapshot(
            tmdb_id=1,
            media_type="movie",
            payload={"item": {"tmdb_id": 1, "media_type": "movie"}},
            episodes_complete=True,
            refreshed_at=utc_now(),
            last_accessed_at=utc_now(),
        )
    )
    db.commit()

    composed = AsyncMock(
        return_value=({"item": {"tmdb_id": 1, "media_type": "movie"}}, "tmdb:1")
    )
    with (
        patch("workers.detail_snapshot_warm.get_db", side_effect=_ctx),
        patch("workers.detail_snapshot_warm._discovery_details_payload", composed),
    ):
        outcome = await warm_candidate(Candidate(1, "movie"))

    assert outcome == "reused"
    composed.assert_not_awaited()


@pytest.mark.asyncio
async def test_cold_snapshot_serves_persisted_overlay_before_background_refresh(
    client_db,
):
    _, db = client_db
    from app.main import (
        _detail_response_cache_remove,
        _discovery_details_payload,
        _meta_cache,
    )

    db.add(
        DetailSnapshot(
            tmdb_id=1,
            media_type="movie",
            payload={
                "item": {
                    "tmdb_id": 1,
                    "media_type": "movie",
                    "title": "Persisted",
                },
                "tags": ["warm-tag"],
                "ratings": [{"source": "TMDB", "value": 8.5}],
                "similar": [],
            },
            episodes_complete=True,
            refreshed_at=utc_now(),
            last_accessed_at=utc_now(),
        )
    )
    db.commit()
    _meta_cache.clear()
    _detail_response_cache_remove(1, "movie")

    with (
        patch("app.main._schedule_detail_dynamic_refresh") as schedule_refresh,
        patch(
            "app.main._compute_detail_dynamic_overlay",
            side_effect=AssertionError("request path recomputed the dynamic overlay"),
        ),
    ):
        payload, identity_id = await _discovery_details_payload(1, "movie", None)

    assert identity_id == "tmdb:1"
    assert payload["tags"] == ["warm-tag"]
    assert payload["ratings"] == [{"source": "TMDB", "value": 8.5}]
    schedule_refresh.assert_called_once_with(1, "movie")


@pytest.mark.asyncio
async def test_fresh_persisted_overlay_survives_process_cache_reset(client_db):
    _, db = client_db
    from app.main import (
        _detail_response_cache_remove,
        _discovery_details_payload,
        _meta_cache,
    )

    db.add(
        DetailSnapshot(
            tmdb_id=1,
            media_type="movie",
            payload={
                "item": {"tmdb_id": 1, "media_type": "movie"},
                "tags": ["persisted"],
                "similar": [],
            },
            episodes_complete=True,
            refreshed_at=utc_now(),
            overlay_refreshed_at=utc_now(),
            last_accessed_at=utc_now(),
        )
    )
    db.commit()
    _meta_cache.clear()
    _detail_response_cache_remove(1, "movie")

    with patch("app.main._schedule_detail_dynamic_refresh") as schedule_refresh:
        payload, _ = await _discovery_details_payload(1, "movie", None)

    assert payload["tags"] == ["persisted"]
    schedule_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_batch_style_read_never_schedules_stale_overlay_refresh(client_db):
    _, db = client_db
    from app.main import (
        _detail_response_cache_remove,
        _discovery_details_batch,
        _discovery_details_payload,
        _meta_cache,
    )

    db.add(
        DetailSnapshot(
            tmdb_id=1,
            media_type="movie",
            payload={
                "item": {"tmdb_id": 1, "media_type": "movie"},
                "tags": ["legacy"],
                "similar": [],
            },
            episodes_complete=True,
            refreshed_at=utc_now(),
            overlay_refreshed_at=None,
            last_accessed_at=utc_now(),
        )
    )
    db.commit()
    _meta_cache.clear()
    _detail_response_cache_remove(1, "movie")

    with patch("app.main._schedule_detail_dynamic_refresh") as schedule_refresh:
        response = await _discovery_details_batch(["movie:1"], None, include_meta=False)

    assert response.status_code == 200
    schedule_refresh.assert_not_called()

    # The batch read must not accidentally bless the legacy overlay as fresh.
    with patch("app.main._schedule_detail_dynamic_refresh") as schedule_refresh:
        await _discovery_details_payload(1, "movie", None)
    schedule_refresh.assert_called_once_with(1, "movie")


def test_empty_snapshot_similar_is_backfilled_from_catalog_on_read(client_db):
    _, db = client_db
    from app.main import _load_detail_snapshot

    db.add_all(
        [
            MediaMetadata(
                tmdb_id=701,
                media_type="movie",
                title="Reference",
                genres=["Comedy"],
                popularity=1.0,
            ),
            MediaMetadata(
                tmdb_id=702,
                media_type="movie",
                title="Fallback",
                genres=["Comedy"],
                popularity=50.0,
            ),
            DetailSnapshot(
                tmdb_id=701,
                media_type="movie",
                payload={
                    "item": {
                        "tmdb_id": 701,
                        "media_type": "movie",
                        "title": "Reference",
                        "genres": ["Comedy"],
                    },
                    "similar": [],
                    "similar_source": "tmdb",
                },
                episodes_complete=True,
                refreshed_at=utc_now(),
                last_accessed_at=utc_now(),
            ),
        ]
    )
    db.commit()

    payload, stale = _load_detail_snapshot(db, 701, "movie")

    assert stale is False
    assert payload["similar_source"] == "catalog"
    assert [item["tmdb_id"] for item in payload["similar"]] == [702]
    db.expire_all()
    assert db.get(DetailSnapshot, (701, "movie")).payload["similar"]


def test_addon_type_is_served_from_a_snapshot_that_never_stored_it(client_db):
    """A warmed snapshot predating the field still answers with it.

    This is why the alias is derived on the way out instead of written by the
    serializers: stored snapshots stay valid for up to 90 days, so stamping at
    build time would have meant bumping the snapshot schema version and cold-
    rebuilding every warmed title against TMDB just to add one key.
    """
    client, db = client_db
    from app.main import _detail_response_cache_remove, _meta_cache

    db.add_all(
        [
            MediaMetadata(tmdb_id=404, media_type="tv", title="Legacy", popularity=1.0),
            DetailSnapshot(
                tmdb_id=404,
                media_type="tv",
                # Deliberately the pre-cutover shape: no content_type anywhere, so
                # the media_type fallback is what has to answer -- and it has to
                # answer "series", not media_type's own "tv".
                payload={
                    "item": {
                        "tmdb_id": 404,
                        "media_type": "tv",
                        "title": "Legacy",
                    },
                    "episodes_complete": True,
                    "seasons": [
                        {
                            "season": 1,
                            "title": "Season 1",
                            "episodes_complete": True,
                            "episodes": [
                                {
                                    "season": 1,
                                    "episode": 1,
                                    "media_type": "tv",
                                    "video_id": "tmdb:404:1:1",
                                }
                            ],
                        }
                    ],
                    # Non-empty, or the read backfills the rail from the catalog
                    # and stops exercising the stored entries.
                    "similar": [{"tmdb_id": 405, "media_type": "tv", "title": "Peer"}],
                    "similar_source": "tmdb",
                    "tags": [],
                    "ratings": [],
                },
                episodes_complete=True,
                refreshed_at=utc_now(),
                last_accessed_at=utc_now(),
            ),
        ]
    )
    db.commit()
    _meta_cache.clear()
    _detail_response_cache_remove(404, "tv")
    client.cookies.clear()

    with (
        patch("app.main._schedule_detail_dynamic_refresh"),
        patch.multiple(
            "app.tmdb_client.tmdb_client",
            get_tv_show=AsyncMock(
                side_effect=AssertionError("a usable snapshot was rebuilt")
            ),
            get_similar_tv_shows=AsyncMock(
                side_effect=AssertionError("a usable snapshot was rebuilt")
            ),
        ),
    ):
        response = client.get(
            "/api/v2/details?tmdb_id=404&media_type=tv",
            headers=_bearer("browse-user-key"),
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, max-age=1800"
    body = response.json()
    assert body["item"]["media_type"] == "tv"
    assert body["item"]["addon_type"] == "series"
    assert body["similar"][0]["addon_type"] == "series"
    # Derived, not copied: this snapshot never carried content_type.
    assert "content_type" not in body["item"]
    assert "addon_type" not in body["seasons"][0]["episodes"][0]

    # And serving it did not write the alias back into the stored payload.
    db.expire_all()
    stored = db.get(DetailSnapshot, (404, "tv")).payload
    assert "addon_type" not in stored["item"]
    assert "addon_type" not in stored["similar"][0]


@pytest.mark.asyncio
async def test_dynamic_overlay_background_refresh_is_singleflight(client_db):
    from app.main import (
        _detail_dynamic_refresh_tasks,
        _loop_task_map,
        _schedule_detail_dynamic_refresh,
    )

    started = asyncio.Event()
    release = asyncio.Event()

    async def controlled_refresh(*args):
        started.set()
        await release.wait()
        return True

    with patch("app.main._detail_db_call", side_effect=controlled_refresh) as refresh:
        _schedule_detail_dynamic_refresh(1, "movie")
        _schedule_detail_dynamic_refresh(1, "movie")
        await asyncio.wait_for(started.wait(), timeout=0.5)
        task = _loop_task_map(_detail_dynamic_refresh_tasks)[("movie", 1)]
        assert refresh.await_count == 1
        release.set()
        await task

    assert refresh.await_count == 1


def test_background_dynamic_refresh_persists_ready_overlay(client_db):
    _, db = client_db
    from app.main import (
        _detail_response_cache_get,
        _detail_response_cache_remove,
        _refresh_persisted_detail_dynamic_overlay,
    )

    db.add(
        DetailSnapshot(
            tmdb_id=1,
            media_type="movie",
            payload={
                "item": {
                    "tmdb_id": 1,
                    "media_type": "movie",
                    "title": "Persisted",
                    "content_rating": "PG",
                },
                "tags": ["old-tag"],
                "ratings": [{"source": "TMDB", "value": 7.0}],
            },
            episodes_complete=True,
            refreshed_at=utc_now(),
            last_accessed_at=utc_now(),
        )
    )
    db.commit()
    _detail_response_cache_remove(1, "movie")

    computed = {
        "tags": ["new-tag"],
        "ratings": [{"source": "TMDB", "value": 9.0}],
        "_item": {"content_rating": "PG-13", "comingSoonDate": None},
    }
    with patch("app.main._compute_detail_dynamic_overlay", return_value=computed):
        assert _refresh_persisted_detail_dynamic_overlay(db, 1, "movie")

    db.expire_all()
    snapshot = db.get(DetailSnapshot, (1, "movie"))
    assert snapshot.overlay_refreshed_at is not None
    assert snapshot.payload["tags"] == ["new-tag"]
    assert snapshot.payload["ratings"] == [{"source": "TMDB", "value": 9.0}]
    assert snapshot.payload["item"]["content_rating"] == "PG-13"
    assert _detail_response_cache_get(1, "movie")["ratings"] == [
        {"source": "TMDB", "value": 9.0}
    ]


def test_failed_dynamic_refresh_preserves_overlay_and_freshness(client_db):
    _, db = client_db
    from app.main import _refresh_persisted_detail_dynamic_overlay

    original_freshness = utc_now() - timedelta(days=2)
    snapshot = DetailSnapshot(
        tmdb_id=1,
        media_type="movie",
        payload={
            "item": {"tmdb_id": 1, "media_type": "movie"},
            "tags": ["still-valid"],
        },
        episodes_complete=True,
        overlay_refreshed_at=original_freshness,
    )
    db.add(snapshot)
    db.commit()

    with (
        patch(
            "app.main._compute_detail_dynamic_overlay",
            side_effect=RuntimeError("database timeout"),
        ),
        pytest.raises(RuntimeError, match="database timeout"),
    ):
        _refresh_persisted_detail_dynamic_overlay(db, 1, "movie")

    db.rollback()
    db.expire_all()
    snapshot = db.get(DetailSnapshot, (1, "movie"))
    assert snapshot.payload["tags"] == ["still-valid"]
    assert snapshot.overlay_refreshed_at == original_freshness


@pytest.mark.asyncio
async def test_dynamic_overlay_refreshes_run_one_at_a_time(client_db):
    from app.main import (
        _detail_dynamic_refresh_tasks,
        _loop_task_map,
        _schedule_detail_dynamic_refresh,
    )

    active = 0
    maximum_active = 0
    first_started = asyncio.Event()
    release = asyncio.Event()

    async def controlled_refresh(*args):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        first_started.set()
        await release.wait()
        active -= 1
        return True

    with patch("app.main._detail_db_call", side_effect=controlled_refresh):
        _schedule_detail_dynamic_refresh(1, "movie")
        _schedule_detail_dynamic_refresh(2, "movie")
        await asyncio.wait_for(first_started.wait(), timeout=0.5)
        await asyncio.sleep(0)
        assert maximum_active == 1
        release.set()
        tasks = list(_loop_task_map(_detail_dynamic_refresh_tasks).values())
        await asyncio.gather(*tasks)

    assert maximum_active == 1


def test_stale_snapshot_returns_without_provider_wait_and_schedules_refresh(client_db):
    client, db = client_db
    _seed_corpus(db)
    client.cookies.clear()
    movie_mock = AsyncMock(return_value=_tmdb_detail())

    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_movie=movie_mock,
        get_similar_movies=AsyncMock(return_value={"results": []}),
    ):
        assert (
            client.get(
                "/api/v2/details?tmdb_id=1&media_type=movie&include=meta",
                headers=_bearer("browse-user-key"),
            ).status_code
            == 200
        )

    snapshot = db.get(DetailSnapshot, (1, "movie"))
    snapshot.refreshed_at = utc_now() - timedelta(days=8)
    db.commit()
    from app.main import _meta_cache

    _meta_cache.clear()
    with patch("app.main._schedule_detail_snapshot_refresh") as schedule_refresh:
        response = client.get(
            "/api/v2/details?tmdb_id=1&media_type=movie&include=meta",
            headers=_bearer("browse-user-key"),
        )

    assert response.status_code == 200
    schedule_refresh.assert_called_once_with(1, "movie")
    movie_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_single_and_batch_style_detail_builds_share_one_provider_call(client_db):
    _, db = client_db
    _seed_corpus(db)
    movie_mock = AsyncMock(return_value=_tmdb_detail())

    from app.main import _discovery_details_payload, _meta_cache

    _meta_cache.clear()
    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_movie=movie_mock,
        get_similar_movies=AsyncMock(return_value={"results": []}),
    ):
        interactive, background = await asyncio.gather(
            _discovery_details_payload(1, "movie", None, background=False),
            _discovery_details_payload(1, "movie", None, background=True),
        )

    assert interactive[0]["item"]["tmdb_id"] == background[0]["item"]["tmdb_id"] == 1
    movie_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_interactive_join_promotes_waiting_background_detail_build(client_db):
    _, db = client_db
    _seed_corpus(db)

    from app.main import _detail_tmdb_slot

    background_semaphore = asyncio.Semaphore(0)
    foreground_semaphore = asyncio.Semaphore(1)
    promotion_event = asyncio.Event()
    entered = asyncio.Event()

    async def promoted_request() -> None:
        async with _detail_tmdb_slot(background=True, promotion_event=promotion_event):
            entered.set()

    with (
        patch(
            "app.main._detail_background_tmdb_semaphore",
            return_value=background_semaphore,
        ),
        patch("app.main._detail_tmdb_semaphore", return_value=foreground_semaphore),
    ):
        task = asyncio.create_task(promoted_request())
        await asyncio.sleep(0)
        assert not entered.is_set()

        promotion_event.set()
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        await task

    assert background_semaphore._value == 0


@pytest.mark.asyncio
async def test_interactive_join_signals_existing_background_singleflight(client_db):
    _, db = client_db
    _seed_corpus(db)

    from app.main import _singleflight_detail_build

    build_started = asyncio.Event()
    promotion_observed = asyncio.Event()
    payload = {"item": {"tmdb_id": 1, "media_type": "movie"}}

    async def controlled_build(
        tmdb_id,
        media_type,
        *,
        background,
        promotion_event,
    ):
        assert background is True
        build_started.set()
        await asyncio.wait_for(promotion_event.wait(), timeout=0.5)
        promotion_observed.set()
        return payload

    with (
        patch(
            "app.main._build_discovery_details_payload",
            side_effect=controlled_build,
        ) as build_mock,
        patch("app.main._persist_detail_snapshot", return_value=True),
    ):
        background = asyncio.create_task(
            _singleflight_detail_build(1, "movie", background=True)
        )
        await asyncio.wait_for(build_started.wait(), timeout=0.5)
        interactive = asyncio.create_task(
            _singleflight_detail_build(1, "movie", background=False)
        )
        background_result, interactive_result = await asyncio.gather(
            background, interactive
        )

    assert promotion_observed.is_set()
    assert background_result is interactive_result is payload
    build_mock.assert_awaited_once()


def test_partial_series_payload_cannot_replace_complete_snapshot(client_db):
    _, db = client_db
    complete = {
        "tmdb_id": 303,
        "media_type": "tv",
        "episodes_complete": True,
        "seasons": [
            {
                "season_number": 1,
                "episodes_complete": True,
                "episodes": [{"season_number": 1, "episode_number": 1}],
            }
        ],
        "meta": {"id": "tmdb:303", "type": "series", "episodesComplete": True},
    }
    db.add(
        DetailSnapshot(
            tmdb_id=303,
            media_type="tv",
            payload=complete,
            episodes_complete=True,
        )
    )
    db.commit()

    from app.main import _persist_detail_snapshot

    partial = {
        **complete,
        "episodes_complete": False,
        "seasons": [{"season_number": 1, "episodes_complete": False, "episodes": []}],
        "meta": {"id": "tmdb:303", "type": "series", "episodesComplete": False},
    }
    assert _persist_detail_snapshot(db, 303, "tv", partial) is False
    db.expire_all()
    snapshot = db.get(DetailSnapshot, (303, "tv"))
    assert snapshot.episodes_complete is True
    assert snapshot.payload["seasons"][0]["episodes"]


def test_native_details_expose_reviews_content_rating_and_extra_facts(client_db):
    client, db = client_db
    _seed_corpus(db)
    title = db.get(MediaMetadata, (1, "movie"))
    title.mdb_facts = {
        "certification": "PG-13",
        "age_rating": 13,
        "commonsense": "Some intense sequences",
        "keywords": ["city at night"],
        "reviews": ["A concise critic excerpt stored during enrichment."],
    }
    title.digital_release_date = "2099-12-31"
    db.commit()
    client.cookies.clear()

    detail = _tmdb_detail()
    detail.update(
        {
            "tagline": "The night has a story.",
            "status": "Released",
            "original_language": "en",
            "reviews": {
                "total_results": 42,
                "results": [
                    {
                        "id": "review-1",
                        "author": "Alex Reviewer",
                        "author_details": {
                            "username": "alex",
                            "rating": 8.5,
                            "avatar_path": "/avatar.jpg",
                        },
                        "content": "A smart and beautifully photographed thriller.",
                        "created_at": "2026-01-01T00:00:00Z",
                        "url": "https://www.themoviedb.org/review/review-1",
                    }
                ],
            },
            "videos": {
                "results": [
                    {
                        "id": "video-1",
                        "name": "Official Trailer",
                        "type": "Trailer",
                        "site": "YouTube",
                        "key": "abc123",
                        "official": True,
                    }
                ]
            },
            "keywords": {"keywords": [{"name": "neo noir"}]},
            "release_dates": {
                "results": [
                    {
                        "iso_3166_1": "US",
                        "release_dates": [{"certification": "R"}],
                    }
                ]
            },
        }
    )
    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_movie=AsyncMock(return_value=detail),
        get_similar_movies=AsyncMock(return_value={"results": []}),
    ):
        response = client.get(
            "/api/v2/details?tmdb_id=1&media_type=movie",
            headers=_bearer("browse-user-key"),
        )

    assert response.status_code == 200
    body = response.json()
    assert (
        body["content_rating"] == "PG-13"
    ), "stored enrichment wins over live fallback"
    assert body["item"]["content_rating"] == "PG-13"
    assert body["comingSoonDate"] == "2099-12-31"
    assert body["item"]["comingSoonDate"] == "2099-12-31"
    assert body["content_advisory"] == {
        "certification": "PG-13",
        "age_rating": 13,
        "common_sense": "Some intense sequences",
        "source": "database",
    }
    assert [review["source"] for review in body["reviews"]] == ["tmdb", "mdblist"]
    assert body["reviews"][0]["author"] == "Alex Reviewer"
    assert body["reviews"][1]["kind"] == "review_excerpt"
    assert body["review_summary"] == {
        "returned": 2,
        "total_available": 43,
        "average_user_rating": 8.5,
        "rated_review_count": 1,
        "sources": ["tmdb", "mdblist"],
    }
    assert body["trailers"][0]["url"] == "https://www.youtube.com/watch?v=abc123"
    assert set(body["keywords"]) == {"neo noir", "city at night"}
    assert body["tagline"] == "The night has a story."


def test_native_browse_cards_include_stored_content_rating(client_db):
    client, db = client_db
    tags = _seed_corpus(db)
    title = db.get(MediaMetadata, (1, "movie"))
    title.tag_facts = {"certification": "PG-13"}
    title.digital_release_date = "2099-12-31"
    db.commit()
    client.cookies.clear()

    body = client.get(
        f"/api/v2/browse?tags={tags['Neo-Noir'].id}&media_type=movie",
        headers=_bearer("browse-user-key"),
    ).json()

    by_id = {item["tmdb_id"]: item for item in body["items"]}
    assert by_id[1]["content_rating"] == "PG-13"
    assert by_id[1]["comingSoonDate"] == "2099-12-31"


@pytest.mark.asyncio
async def test_tmdb_details_request_reviews_and_regional_content_ratings():
    from app.tmdb_client import TMDBClient

    client = TMDBClient("test-key")
    client._request = AsyncMock(return_value={})

    await client.get_movie(1)
    await client.get_tv_show(2)

    movie_fields = client._request.await_args_list[0].kwargs["params"][
        "append_to_response"
    ]
    tv_fields = client._request.await_args_list[1].kwargs["params"][
        "append_to_response"
    ]
    assert "reviews" in movie_fields.split(",")
    assert "release_dates" in movie_fields.split(",")
    assert "reviews" in tv_fields.split(",")
    assert "content_ratings" in tv_fields.split(",")


def test_tag_stats_backs_the_facets_rather_than_a_live_group_by(client_db):
    """``/facets`` must be cheap enough to call on every page load.

    Reading ``tag_stats`` is a few hundred pre-aggregated rows; a GROUP BY over
    ``movie_tags`` is millions. Deleting the stats must therefore empty the
    endpoint -- if it does not, it is not reading them.
    """
    client, db = client_db
    _seed_corpus(db)
    db.query(TagStats).delete()
    db.commit()

    body = client.get("/account/api/facets?media_type=movie&min_titles=1").json()

    assert body["facets"] == []


# ---------------------------------------------------------------------------
# Universal row cap (category synthesis)
# ---------------------------------------------------------------------------
def test_universal_rows_are_capped_and_contain_only_generated_rows(
    client_db, monkeypatch
):
    """Compatibility rows remain stored but are never shown."""
    from app.config import settings
    from app.models import UniversalCategory

    client, db = client_db
    # Rotation off: this test is about the CUT, not about which slice of the
    # generated pool is on show this week. With rotation on, "gen-0 is present"
    # is true only in one period out of many -- an assertion about the calendar
    # rather than about the cap.
    monkeypatch.setattr(settings, "synth_rotation_days", 0)
    _seed_corpus(db)
    for i in range(30):
        db.add(
            UniversalCategory(
                id=f"curated-{i}",
                name=f"Curated {i}",
                tier=1,
                sort_order=i + 1,
                media_type="movie",
                tag_formula={"required": ["Drama"], "min_required": 1},
                source="curated",
                manifest_priority=1000,
            )
        )
    for i in range(60):
        category = UniversalCategory(
            id=f"gen-{i}",
            name=f"Generated {i}",
            tier=6,
            sort_order=1000 + i,
            media_type="movie",
            tag_formula={"required": ["Drama", "Bleak"], "min_required": 2},
            source="generated",
            # Descending, so the cut must keep the low-index (best-lift) ones.
            manifest_priority=999 - i,
        )
        db.add(category)
        for rank in range(1, 25):
            db.add(
                UniversalCatalogContent(
                    category_id=category.id,
                    tmdb_id=900_000 + rank,
                    rank=rank,
                    match_score=1.0,
                    media_type="movie",
                )
            )
    db.commit()

    generated_limit = 20
    category_ids = {
        c.id for c in main._universal_row_categories(db, limit=generated_limit)
    }
    assert len(category_ids) == generated_limit
    assert not any(category_id.startswith("curated-") for category_id in category_ids)
    assert "gen-0" in category_ids
    assert f"gen-{generated_limit - 1}" in category_ids
    assert f"gen-{generated_limit}" not in category_ids


def test_the_capped_universal_rows_still_render_in_sort_order(client_db):
    """The cap decides *which* rows ship, never what order they ship in.

    Taste reordering and the curated tier's hand-set order both work by
    mutating sort_order, so rows emitted in priority order would silently
    discard both.
    """
    from app.models import UniversalCategory

    client, db = client_db
    _seed_corpus(db)
    for i in range(5):
        category = UniversalCategory(
            id=f"row-{i}",
            name=f"Row {i}",
            tier=1,
            sort_order=5 - i,  # deliberately the reverse of priority
            media_type="movie",
            tag_formula={"required": ["Drama"], "min_required": 1},
            source="generated",
            manifest_priority=1000 + i,
        )
        db.add(category)
        for rank in range(1, 25):
            db.add(
                UniversalCatalogContent(
                    category_id=category.id,
                    tmdb_id=910_000 + rank,
                    rank=rank,
                    match_score=1.0,
                    media_type="movie",
                )
            )
    db.commit()

    ordered = [
        c.id for c in main._universal_row_categories(db) if c.id.startswith("row-")
    ]
    assert ordered == ["row-4", "row-3", "row-2", "row-1", "row-0"]


# ---------------------------------------------------------------------------
# Generated-row rotation
# ---------------------------------------------------------------------------
def _seed_rotating_rows(db, generated=30, curated=5):
    from app.models import UniversalCatalogContent, UniversalCategory

    for i in range(curated):
        db.add(
            UniversalCategory(
                id=f"cur-{i}",
                name=f"Curated {i}",
                tier=1,
                sort_order=i + 1,
                media_type="movie",
                tag_formula={"required": ["Drama"], "min_required": 1},
                source="curated",
                manifest_priority=1000,
            )
        )
    for i in range(generated):
        category = UniversalCategory(
            id=f"gen-{i}",
            name=f"Generated {i}",
            tier=6,
            sort_order=1000 + i,
            media_type="movie",
            tag_formula={"required": ["Drama", "Bleak"], "min_required": 2},
            source="generated",
            manifest_priority=999 - i,
        )
        db.add(category)
        for rank in range(1, 25):
            db.add(
                UniversalCatalogContent(
                    category_id=category.id,
                    tmdb_id=920_000 + rank,
                    rank=rank,
                    match_score=1.0,
                    media_type="movie",
                )
            )
    db.commit()


def test_the_generated_rows_on_show_rotate_over_time(client_db, monkeypatch):
    """Synthesis finds far more rows than a home screen can carry.

    Pinning the best-lift ones permanently means most of what it found is
    never seen by anyone. Rotation is what makes the surplus worth generating.
    """
    from datetime import date

    from app.main import _universal_row_categories

    _client, db = client_db
    _seed_rotating_rows(db)

    week1 = {
        c.id for c in _universal_row_categories(db, limit=15, today=date(2026, 1, 1))
    }
    week2 = {
        c.id for c in _universal_row_categories(db, limit=15, today=date(2026, 1, 8))
    }

    assert week1 != week2, "the lineup must actually change"
    assert all(row.startswith("gen-") for row in week1 | week2)


def test_curated_compatibility_rows_never_enter_the_universal_rows(
    client_db, monkeypatch
):
    from datetime import date

    from app.main import _universal_row_categories

    _client, db = client_db
    _seed_rotating_rows(db)

    for day in (date(2026, 1, 1), date(2026, 3, 9), date(2026, 7, 20)):
        ids = {c.id for c in _universal_row_categories(db, limit=15, today=day)}
        assert not any(row.startswith("cur-") for row in ids), day
        assert all(row.startswith("gen-") for row in ids), day


def test_the_lineup_is_stable_within_a_rotation_period(client_db, monkeypatch):
    """Requests inside one period must agree, or clients thrash.

    The two dates are derived rather than picked: buckets are ordinal-day
    aligned, not week-aligned, so two hand-chosen dates can straddle a
    boundary and the test would be measuring the calendar, not the code.
    """
    from datetime import date

    from app.config import settings
    from app.main import _universal_row_categories

    _client, db = client_db
    _seed_rotating_rows(db)
    monkeypatch.setattr(settings, "synth_rotation_days", 7)

    start = date(2026, 4, 2).toordinal()
    start -= start % 7  # first day of its bucket
    same_bucket = [date.fromordinal(start), date.fromordinal(start + 6)]
    next_bucket = date.fromordinal(start + 7)

    first = [
        c.id for c in _universal_row_categories(db, limit=15, today=same_bucket[0])
    ]
    second = [
        c.id for c in _universal_row_categories(db, limit=15, today=same_bucket[1])
    ]
    after = [c.id for c in _universal_row_categories(db, limit=15, today=next_bucket)]

    assert first == second, "same rotation period, same answer"
    assert first != after, "and the next period differs"


def test_rotation_can_be_switched_off(client_db, monkeypatch):
    from datetime import date

    from app.config import settings
    from app.main import _universal_row_categories

    _client, db = client_db
    _seed_rotating_rows(db)
    monkeypatch.setattr(settings, "synth_rotation_days", 0)

    a = [c.id for c in _universal_row_categories(db, limit=15, today=date(2026, 1, 1))]
    b = [c.id for c in _universal_row_categories(db, limit=15, today=date(2027, 9, 9))]
    assert a == b


def test_hero_view_defers_rails_and_reuses_provider_core_for_full(client_db):
    client, db = client_db
    client.cookies.clear()
    detail = _tmdb_detail(810)
    detail["credits"] = {
        "cast": [
            {"name": f"Actor {i}", "profile_path": f"/actor-{i}.jpg"} for i in range(5)
        ],
        "crew": [{"name": f"Director {i}", "job": "Director"} for i in range(3)],
    }
    movie = AsyncMock(return_value=detail)
    similar = AsyncMock(return_value={"results": []})
    with (
        patch.multiple(
            "app.tmdb_client.tmdb_client", get_movie=movie, get_similar_movies=similar
        ),
        patch.object(
            main, "_detail_build_facts", wraps=main._detail_build_facts
        ) as rails,
        patch.object(main, "_title_reviews", wraps=main._title_reviews) as reviews,
    ):
        hero = client.get(
            "/api/v2/details?tmdb_id=810&media_type=movie&include=meta&view=hero",
            headers=_bearer("browse-user-key"),
        )
        assert hero.status_code == 200
        body = hero.json()
        assert body["hero_complete"] is True
        assert body["details_complete"] is False
        assert len(body["cast"]) == 3
        assert len(body["crew"]["directors"]) == 2
        assert "profile" not in body["cast"][0]
        assert "similar" not in body and "reviews" not in body
        assert "_provider_detail" not in body
        assert db.get(DetailSnapshot, (810, "movie")) is None
        rails.assert_not_called()
        reviews.assert_not_called()
        similar.assert_not_awaited()

        full = client.get(
            "/api/v2/details?tmdb_id=810&media_type=movie&include=meta",
            headers=_bearer("browse-user-key"),
        )
        assert full.status_code == 200
        assert full.json()["hero_complete"] is True
        assert full.json()["details_complete"] is True
        assert len(full.json()["cast"]) == 5
        assert "similar" in full.json() and "reviews" in full.json()
        rails.assert_called_once()
        reviews.assert_called_once()
    movie.assert_awaited_once()
    assert db.get(DetailSnapshot, (810, "movie")) is not None


def _hero_series_fixture(tmdb_id=811):
    return {
        "id": tmdb_id,
        "name": "Series with two seasons",
        "genres": [],
        "credits": {"cast": [], "crew": []},
        "external_ids": {"imdb_id": "tt0811000"},
        "seasons": [
            {"season_number": 1, "episode_count": 1, "poster_path": "/season1.jpg"},
            {"season_number": 2, "episode_count": 1, "poster_path": "/season2.jpg"},
        ],
    }


def _hero_season_fixture(_tmdb_id, season):
    return {
        "episodes": [
            {
                "season_number": season,
                "episode_number": 1,
                "name": f"Season {season} premiere",
                "overview": "The episode synopsis used by the hero.",
                "air_date": "2025-01-01",
                "still_path": f"/episode-{season}.jpg",
            }
        ]
    }


def test_hero_series_keeps_complete_playback_index_without_rail_images(client_db):
    client, _ = client_db
    client.cookies.clear()
    show = AsyncMock(return_value=_hero_series_fixture())
    season = AsyncMock(side_effect=_hero_season_fixture)
    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_tv_show=show,
        get_tv_season=season,
        get_similar_tv_shows=AsyncMock(return_value={"results": []}),
    ):
        hero = client.get(
            "/api/v2/details?tmdb_id=811&media_type=tv&include=meta&view=hero",
            headers=_bearer("browse-user-key"),
        )
        full = client.get(
            "/api/v2/details?tmdb_id=811&media_type=tv&include=meta",
            headers=_bearer("browse-user-key"),
        )
    assert hero.status_code == full.status_code == 200
    body = hero.json()
    assert body["hero_complete"] is True and body["episodes_complete"] is True
    assert body["details_complete"] is False
    assert [video["id"] for video in body["meta"]["videos"]] == [
        "tmdb:811:1:1",
        "tmdb:811:2:1",
    ]
    assert all(
        video["overview"] and video["released"] for video in body["meta"]["videos"]
    )
    assert all("thumbnail" not in video for video in body["meta"]["videos"])
    assert all("poster" not in row for row in body["seasons"])
    assert all("still" not in row["episodes"][0] for row in body["seasons"])
    assert full.json()["seasons"][0]["episodes"][0]["still"]
    assert show.await_count == 1 and season.await_count == 2


def test_empty_expected_season_does_not_become_hero_ready_or_cached(client_db):
    client, db = client_db
    client.cookies.clear()
    show = AsyncMock(return_value=_hero_series_fixture(812))

    async def missing_second(tmdb_id, season):
        return (
            _hero_season_fixture(tmdb_id, season) if season == 1 else {"episodes": []}
        )

    seasons = AsyncMock(side_effect=missing_second)
    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_tv_show=show,
        get_tv_season=seasons,
        get_similar_tv_shows=AsyncMock(return_value={"results": []}),
    ):
        first = client.get(
            "/api/v2/details?tmdb_id=812&media_type=tv&include=meta&view=hero",
            headers=_bearer("browse-user-key"),
        )
        assert first.status_code == 200
        assert first.headers["cache-control"] == "private, no-store"
        assert first.json()["hero_complete"] is False
        assert first.json()["episodes_complete"] is False
        assert first.json()["seasons"][1]["episodes_complete"] is False
        assert first.json()["meta"]["episodesComplete"] is False
        assert db.get(DetailSnapshot, (812, "tv")) is None
        assert "detail-core:tv:812" not in main._meta_cache
        assert "detail-hero:tv:812" not in main._meta_cache
        seasons.side_effect = _hero_season_fixture
        second = client.get(
            "/api/v2/details?tmdb_id=812&media_type=tv&include=meta&view=hero",
            headers=_bearer("browse-user-key"),
        )
    assert second.json()["hero_complete"] is True
    assert show.await_count == 2


def test_hero_snapshot_projection_never_recomputes_below_hero_fields(client_db):
    client, db = client_db
    client.cookies.clear()
    db.add(
        DetailSnapshot(
            tmdb_id=813,
            media_type="movie",
            episodes_complete=True,
            payload={
                "item": {
                    "tmdb_id": 813,
                    "media_type": "movie",
                    "title": "Ready snapshot",
                },
                "cast": [],
                "crew": {},
                "seasons": [],
                "similar": [],
                "ratings": [],
            },
        )
    )
    db.commit()
    with (
        patch.object(
            main, "_catalog_similar_fallback", side_effect=AssertionError("rail work")
        ),
        patch.object(
            main,
            "_compute_detail_dynamic_overlay",
            side_effect=AssertionError("overlay work"),
        ),
        patch.object(
            main,
            "_build_detail_core_payload",
            new=AsyncMock(side_effect=AssertionError("provider work")),
        ),
    ):
        response = client.get(
            "/api/v2/details?tmdb_id=813&media_type=movie&include=meta&view=hero",
            headers=_bearer("browse-user-key"),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["hero_complete"] is True and body["details_complete"] is False
    assert body["cast"] == [] and body["ratings"] == []
    assert body["meta"]["name"] == "Ready snapshot"


@pytest.mark.asyncio
async def test_hero_completes_while_shared_full_request_waits_for_optional_similar(
    client_db,
):
    _, _db = client_db
    similar_started = asyncio.Event()
    release_similar = asyncio.Event()

    async def slow_similar(_tmdb_id):
        similar_started.set()
        await release_similar.wait()
        return {"results": []}

    movie = AsyncMock(return_value=_tmdb_detail(814))
    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_movie=movie,
        get_similar_movies=AsyncMock(side_effect=slow_similar),
    ):
        full = asyncio.create_task(main._discovery_details_payload(814, "movie", None))
        try:
            await asyncio.wait_for(similar_started.wait(), timeout=1)
            hero, _ = await asyncio.wait_for(
                main._discovery_details_payload(814, "movie", None, view="hero"),
                timeout=0.5,
            )
            assert hero["hero_complete"] is True
            assert hero["details_complete"] is False
            assert not full.done()
        finally:
            release_similar.set()
            await full
    movie.assert_awaited_once()


def test_hero_batch_preserves_explicit_readiness_and_never_builds_rails(client_db):
    client, _ = client_db
    client.cookies.clear()
    with (
        patch(
            "app.tmdb_client.tmdb_client.get_movie",
            new=AsyncMock(side_effect=_tmdb_detail),
        ),
        patch.object(
            main, "_detail_build_facts", side_effect=AssertionError("rail work")
        ),
    ):
        response = client.get(
            "/api/v2/details?ids=movie:815,movie:816&include=meta&view=hero",
            headers=_bearer("browse-user-key"),
        )
    assert response.status_code == 200
    assert response.json()["errors"] == []
    assert len(response.json()["results"]) == 2
    for result in response.json()["results"]:
        assert result["details"]["hero_complete"] is True
        assert result["details"]["details_complete"] is False


def test_hero_only_payload_cannot_be_persisted_as_full(client_db):
    _, db = client_db
    payload = {
        "item": {"tmdb_id": 817, "media_type": "movie"},
        "hero_complete": True,
        "details_complete": False,
    }
    assert main._persist_detail_snapshot(db, 817, "movie", payload) is False
    assert db.get(DetailSnapshot, (817, "movie")) is None


def test_retrying_hero_refreshes_following_full_page_from_the_same_new_core(client_db):
    client, _ = client_db
    client.cookies.clear()
    old = {**_tmdb_detail(818), "title": "Old snapshot"}
    fresh = {**old, "title": "Fresh hero"}
    movie = AsyncMock(side_effect=[old, fresh])
    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_movie=movie,
        get_similar_movies=AsyncMock(return_value={"results": []}),
    ):
        initial = client.get(
            "/api/v2/details?tmdb_id=818&media_type=movie",
            headers=_bearer("browse-user-key"),
        )
        assert initial.json()["item"]["title"] == "Old snapshot"
        hero = client.get(
            "/api/v2/details?tmdb_id=818&media_type=movie&view=hero&refresh=true",
            headers=_bearer("browse-user-key"),
        )
        assert hero.json()["item"]["title"] == "Fresh hero"
        full = client.get(
            "/api/v2/details?tmdb_id=818&media_type=movie",
            headers=_bearer("browse-user-key"),
        )
        assert full.json()["item"]["title"] == "Fresh hero"
        assert full.json()["details_complete"] is True
        assert (
            "_full_refresh_pending" not in main._meta_cache["detail-core:movie:818"][1]
        )
    assert movie.await_count == 2


@pytest.mark.asyncio
async def test_concurrent_hero_full_and_legacy_consumers_share_cold_provider_work(
    client_db,
):
    _, _db = client_db
    provider_started = asyncio.Event()
    consumers_joined = asyncio.Event()
    release_provider = asyncio.Event()
    joined = 0
    real_core = main._singleflight_detail_core

    async def observed_core(*args, **kwargs):
        nonlocal joined
        joined += 1
        if joined == 3:
            consumers_joined.set()
        return await real_core(*args, **kwargs)

    async def held_movie(tmdb_id):
        provider_started.set()
        await release_provider.wait()
        return _tmdb_detail(tmdb_id)

    movie = AsyncMock(side_effect=held_movie)
    with (
        patch.multiple(
            "app.tmdb_client.tmdb_client",
            get_movie=movie,
            get_similar_movies=AsyncMock(return_value={"results": []}),
        ),
        patch.object(main, "_singleflight_detail_core", side_effect=observed_core),
    ):
        full = asyncio.create_task(
            main._discovery_details_payload(819, "movie", None, background=True)
        )
        consumers = [full]
        try:
            await asyncio.wait_for(provider_started.wait(), timeout=1)
            consumers.extend(
                [
                    asyncio.create_task(
                        main._discovery_details_payload(819, "movie", None, view="hero")
                    ),
                    asyncio.create_task(
                        main._discovery_details_payload(819, "movie", None, view="core")
                    ),
                ]
            )
            await asyncio.wait_for(consumers_joined.wait(), timeout=1)
        finally:
            release_provider.set()
            results = await asyncio.gather(*consumers)
    assert len(results) == 3
    assert results[0][0]["details_complete"] is True
    assert results[1][0]["hero_complete"] is True
    assert results[1][0]["details_complete"] is False
    assert all(identity == "tmdb:819" for _, identity in results)
    movie.assert_awaited_once()


@pytest.mark.parametrize(
    "media_type,failed_block",
    [
        ("movie", "credits"),
        ("movie", "external_ids"),
        ("movie", "images"),
        ("movie", "videos"),
        ("movie", "release_dates"),
        ("tv", "content_ratings"),
    ],
)
def test_failed_appended_hero_source_is_retryable_not_authoritatively_absent(
    client_db, media_type, failed_block
):
    client, db = client_db
    client.cookies.clear()
    healthy = _hero_series_fixture(820) if media_type == "tv" else _tmdb_detail(820)
    broken = {
        **healthy,
        failed_block: {
            "success": False,
            "status_code": 9,
            "status_message": "Service offline",
        },
    }
    provider = AsyncMock(side_effect=[broken, healthy])
    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        **{"get_tv_show" if media_type == "tv" else "get_movie": provider},
        get_tv_season=AsyncMock(side_effect=_hero_season_fixture),
    ):
        url = f"/api/v2/details?tmdb_id=820&media_type={media_type}&include=meta&view=hero"
        first = client.get(url, headers=_bearer("browse-user-key"))
        assert first.status_code == 200
        assert first.json()["hero_complete"] is False
        assert first.json()["details_complete"] is False
        assert first.headers["cache-control"] == "private, no-store"
        if media_type == "tv":
            # The episode index itself succeeded; a failed certification request
            # must not mislabel otherwise valid season data as missing.
            assert first.json()["episodes_complete"] is True
        assert f"detail-core:{media_type}:820" not in main._meta_cache
        assert f"detail-hero:{media_type}:820" not in main._meta_cache
        assert db.get(DetailSnapshot, (820, media_type)) is None
        second = client.get(url, headers=_bearer("browse-user-key"))
    assert second.json()["hero_complete"] is True
    assert second.headers["cache-control"] == "private, max-age=1800"
    assert provider.await_count == 2


def test_optional_below_hero_provider_failure_does_not_block_hero(client_db):
    client, _ = client_db
    client.cookies.clear()
    detail = {**_tmdb_detail(821), "reviews": {"success": False, "status_code": 9}}
    with patch(
        "app.tmdb_client.tmdb_client.get_movie", new=AsyncMock(return_value=detail)
    ):
        response = client.get(
            "/api/v2/details?tmdb_id=821&media_type=movie&include=meta&view=hero",
            headers=_bearer("browse-user-key"),
        )
    assert response.json()["hero_complete"] is True
    assert response.headers["cache-control"] == "private, max-age=1800"


def test_upcoming_zero_episode_series_has_stable_authoritative_reminder_date(client_db):
    client, db = client_db
    client.cookies.clear()
    detail = {
        "id": 822,
        "name": "Future Premiere",
        "genres": [],
        "credits": {"cast": [], "crew": []},
        "number_of_episodes": 0,
        "seasons": [],
        "first_air_date": "2099-12-31",
    }
    season = AsyncMock(side_effect=AssertionError("no episodes exist yet"))
    with patch.multiple(
        "app.tmdb_client.tmdb_client",
        get_tv_show=AsyncMock(return_value=detail),
        get_tv_season=season,
        get_similar_tv_shows=AsyncMock(return_value={"results": []}),
    ):
        response = client.get(
            "/api/v2/details?tmdb_id=822&media_type=tv&include=meta&view=hero",
            headers=_bearer("browse-user-key"),
        )
        assert response.json()["hero_complete"] is True
        assert response.json()["episodes_complete"] is True
        assert response.json()["meta"]["episodesComplete"] is True
        assert response.json()["meta"]["videos"] == []
        assert response.json()["item"]["comingSoonDate"] == "2099-12-31"
        assert (
            client.get(
                "/api/v2/details?tmdb_id=822&media_type=tv",
                headers=_bearer("browse-user-key"),
            ).status_code
            == 200
        )
    snapshot = db.get(DetailSnapshot, (822, "tv"))
    overlay = main._compute_detail_dynamic_overlay(db, snapshot.payload, 822, "tv")
    assert overlay["_item"]["comingSoonDate"] == "2099-12-31"
    season.assert_not_awaited()


@pytest.mark.parametrize("has_future_episode_date", [False, True])
def test_missing_episode_counts_are_not_positive_zero_episode_evidence(
    client_db, has_future_episode_date
):
    client, _ = client_db
    client.cookies.clear()
    detail = {"id": 823, "name": "Sparse response", "genres": [], "seasons": []}
    if has_future_episode_date:
        detail["next_episode_to_air"] = {"air_date": "2099-12-31"}
    with patch(
        "app.tmdb_client.tmdb_client.get_tv_show", new=AsyncMock(return_value=detail)
    ):
        response = client.get(
            "/api/v2/details?tmdb_id=823&media_type=tv&include=meta&view=hero",
            headers=_bearer("browse-user-key"),
        )
    assert response.json()["hero_complete"] is False
    assert response.json()["episodes_complete"] is False
    assert response.headers["cache-control"] == "private, no-store"
