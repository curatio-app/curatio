# ruff: noqa: E402
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_web_dashboard.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.auth import create_user_session  # noqa: E402
from app.database import get_db_dependency  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (
    Account,
    Base,
    MediaMetadata,
    PasskeyCredential,
    User,
    WatchEvent,
    WatchProgress,
    InviteCode,
    LibraryItem,
    SegmentFeedback,
    UserCatalog,
    UserCatalogContent,
    UniversalCategory,
    UniversalCatalogContent,
)  # noqa: E402
from app.time_utils import utc_now  # noqa: E402

engine = create_engine(
    "sqlite:///./test_web_dashboard.db", connect_args={"check_same_thread": False}
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _bearer(user_key):
    """Profile credential header for the native /api/v2 API."""
    return {"Authorization": f"Bearer {user_key}"}


def make_client():
    from app.main import _invalidate_dashboard_cache

    _invalidate_dashboard_cache()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()

    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db_dependency] = override_get_db
    client = TestClient(app)
    return client, db


def cleanup(db):
    from app.main import _invalidate_dashboard_cache

    _invalidate_dashboard_cache()
    db.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def test_bootstrap_query_count_is_bounded_for_forty_categories_and_many_shelves():
    """Shelf count must not translate into one SQL round trip per shelf."""
    from app.main import _invalidate_dashboard_cache, _taste_cache
    from workers.personalized_sync import SLOT_ORDER

    client, db = make_client()
    try:
        user = create_logged_in_user(client, db)
        user.last_sync = utc_now()
        metadata = []
        categories = []
        universal_content = []
        snapshot_revision = utc_now()
        for index in range(40):
            media_type = "movie" if index % 2 == 0 else "tv"
            category_id = f"perf-{index:02d}"
            categories.append(
                UniversalCategory(
                    id=category_id,
                    name=f"Category {index}",
                    tier=1,
                    sort_order=index,
                    media_type=media_type,
                    tag_formula={"required": []},
                    is_active=True,
                    source="generated",
                    manifest_priority=100,
                    updated_at=snapshot_revision,
                )
            )
            for item_index in range(24):
                tmdb_id = 10_000 + index * 100 + item_index
                metadata.append(
                    MediaMetadata(
                        tmdb_id=tmdb_id,
                        media_type=media_type,
                        title=f"Universal {index}-{item_index}",
                    )
                )
                universal_content.append(
                    UniversalCatalogContent(
                        category_id=category_id,
                        tmdb_id=tmdb_id,
                        rank=item_index + 1,
                        match_score=1.0,
                        media_type=media_type,
                        last_updated=snapshot_revision,
                    )
                )
        db.add_all(metadata + categories + universal_content)
        personal_slots = sorted(SLOT_ORDER, key=SLOT_ORDER.get)

        def add_personal_shelves(start: int, count: int) -> None:
            for index in range(start, start + count):
                media_type = "tv" if "series" in personal_slots[index] else "movie"
                catalog = UserCatalog(
                    user_id=user.id,
                    slot_id=personal_slots[index],
                    name=f"Personal {index}",
                    media_type=media_type,
                    generation_method="performance_fixture",
                    generation_params={
                        "generation_revision": snapshot_revision.isoformat()
                    },
                    last_generated=snapshot_revision,
                    is_active=True,
                )
                db.add(catalog)
                db.flush()
                for item_index in range(24):
                    tmdb_id = 20_000 + index * 100 + item_index
                    db.add(
                        MediaMetadata(
                            tmdb_id=tmdb_id,
                            media_type=media_type,
                            title=f"Personal title {index}-{item_index}",
                        )
                    )
                    db.add(
                        UserCatalogContent(
                            catalog_id=catalog.id,
                            tmdb_id=tmdb_id,
                            rank=item_index + 1,
                            match_score=1.0,
                            media_type=media_type,
                            added_at=snapshot_revision,
                        )
                    )

        add_personal_shelves(0, 2)
        db.commit()

        statements = 0

        def count_statement(*_args):
            nonlocal statements
            statements += 1

        event.listen(engine, "before_cursor_execute", count_statement)
        try:
            _taste_cache.clear()
            _invalidate_dashboard_cache()
            cold = client.get("/account/api/bootstrap")
            cold_count = statements
            assert cold.status_code == 200
            rows = cold.json()["dashboard"]["rows"]
            assert len(rows) == 40
            assert rows[0]["source"] == "personal"
            assert len({row["id"] for row in rows}) == len(rows)
            # +2 constant: availability revision and one batched ownership lookup.
            assert cold_count <= 20

            statements = 0
            warm = client.get("/account/api/bootstrap")
            assert warm.status_code == 200
            assert statements <= 10

            add_personal_shelves(2, 10)
            db.commit()
            _invalidate_dashboard_cache(user.id)
            statements = 0
            expanded = client.get("/account/api/bootstrap")
            assert expanded.status_code == 200
            assert statements <= cold_count + 1
            expanded_rows = expanded.json()["dashboard"]["rows"]
            assert len({row["id"] for row in expanded_rows}) == len(expanded_rows)
        finally:
            event.remove(engine, "before_cursor_execute", count_statement)
    finally:
        cleanup(db)


@contextmanager
def use_test_db(db):
    yield db


def create_logged_in_user(client, db):
    """Create an account + one default profile, and a session selecting it.

    Returns the profile (User), since callers attach watch data and read the
    native API via the profile's user_key. The account is fully enrolled (passkey +
    TOTP) so it clears the security gate that every signed-in route enforces.
    """
    account = Account(
        auth_source="local",
        email="viewer@example.com",
        password_hash="not-used",
        is_active=True,
        totp_enabled=True,
    )
    db.add(account)
    db.commit()
    db.add(
        PasskeyCredential(
            account_id=account.id,
            credential_id="dashboard-credential",
            public_key="dashboard-public-key",
        )
    )
    db.commit()
    user = User(
        user_key="web-user-key",
        account_id=account.id,
        auth_source="local",
        display_name="Viewer",
        is_active=True,
    )
    db.add(user)
    db.commit()
    token = create_user_session(account.id, db, active_profile_id=user.id)
    client.cookies.set("user_session", token)
    return user


def test_new_user_registration_schedules_initial_catalog_sync():
    client, db = make_client()
    try:
        db.add(InviteCode(code="invite-sync-test"))
        db.commit()

        with patch("app.main.get_db", return_value=use_test_db(db)), patch(
            "app.main._schedule_local_sync"
        ) as schedule_sync:
            response = client.post(
                "/auth/register",
                json={
                    "email": "new-viewer@example.com",
                    "password": "correct horse battery staple",
                    "name": "New Viewer",
                    "invite": "invite-sync-test",
                },
            )

        assert response.status_code == 200
        created_account = (
            db.query(Account).filter(Account.email == "new-viewer@example.com").first()
        )
        assert created_account is not None
        created = db.query(User).filter(User.account_id == created_account.id).first()
        assert created is not None
        schedule_sync.assert_called_once_with(created.id)
    finally:
        cleanup(db)


def test_unsynced_dashboard_schedules_sync_and_reports_pending():
    client, db = make_client()
    try:
        user = create_logged_in_user(client, db)
        user.last_sync = None
        db.commit()

        with patch("app.main._schedule_local_sync") as schedule_sync:
            response = client.get("/account/api/dashboard")

        assert response.status_code == 200
        data = response.json()
        assert data["catalog_sync_pending"] is True
        schedule_sync.assert_called_once_with(user.id)
    finally:
        cleanup(db)


def test_dashboard_requires_account_login():
    client, db = make_client()
    try:
        response = client.get("/account/dashboard", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/account/login?next=/account/dashboard"
    finally:
        cleanup(db)


def test_dashboard_api_returns_continue_watching_and_completed_history():
    client, db = make_client()
    try:
        user = create_logged_in_user(client, db)
        db.add(
            MediaMetadata(
                tmdb_id=101,
                media_type="movie",
                title="Apex",
                overview="A climber fights for survival.",
                release_date="2026-01-01",
                genres=["Action", "Thriller"],
                poster_path="/poster.jpg",
                backdrop_path="/backdrop.jpg",
                imdb_id="tt0000101",
                popularity=99,
            )
        )
        db.add(
            WatchProgress(
                user_id=user.id,
                tmdb_id=101,
                media_type="movie",
                season=0,
                episode=0,
                position=600,
                duration=3600,
                title="Apex",
            )
        )
        db.add(
            WatchEvent(
                user_id=user.id,
                tmdb_id=101,
                media_type="movie",
                action="complete",
                title="Apex",
                created_at=datetime.utcnow(),
            )
        )
        db.commit()

        response = client.get("/account/api/dashboard")
        assert response.status_code == 200
        data = response.json()
        assert data["continue_watching"][0]["title"] == "Apex"
        assert data["continue_watching"][0]["progress_percent"] == 17
        assert data["recent_history"][0]["action"] == "complete"
        assert data["recent_history"][0]["imdb_id"] == "tt0000101"
    finally:
        cleanup(db)


def test_dashboard_hero_prefers_a_personal_recommendation_over_continue_watching():
    client, db = make_client()
    try:
        user = create_logged_in_user(client, db)
        user.last_sync = datetime.utcnow()
        db.add(
            UserCatalog(
                user_id=user.id,
                slot_id="rec-movie",
                name="Recommended Movies",
                media_type="movie",
                generation_method="tag_recommendations",
                is_active=True,
            )
        )
        db.add(
            MediaMetadata(
                tmdb_id=101,
                media_type="movie",
                title="Continue Title",
                backdrop_path="/continue-bg.jpg",
                poster_path="/continue.jpg",
            )
        )
        db.add(
            WatchProgress(
                user_id=user.id,
                tmdb_id=101,
                media_type="movie",
                season=0,
                episode=0,
                position=900,
                duration=3600,
                title="Continue Title",
            )
        )
        db.commit()

        def catalog_content(catalog_ids, *args, **kwargs):
            return {
                catalog_id: (
                    [
                        {
                            "tmdb_id": 202 + index,
                            "title": (
                                "Trending Feature" if index == 0 else f"Feature {index}"
                            ),
                            "poster": "/trend.jpg",
                            "backdrop": "/trend-bg.jpg",
                            "year": "2026",
                        }
                        for index in range(24)
                    ]
                    if catalog_id == "rec-movie"
                    else []
                )
                for catalog_id in catalog_ids
            }

        with patch(
            "app.main.CatalogGenerator.get_catalog_contents_batch",
            side_effect=catalog_content,
        ):
            response = client.get("/account/api/dashboard")

        assert response.status_code == 200
        data = response.json()
        assert data["hero"]["title"] == "Trending Feature"
        assert data["continue_watching"][0]["title"] == "Continue Title"
        assert data["catalogs"][0]["id"] == "personal-rec-movie"
    finally:
        cleanup(db)


def test_dashboard_keeps_personal_movie_and_series_rows():
    client, db = make_client()
    try:
        user = create_logged_in_user(client, db)
        user.last_sync = datetime.utcnow()
        db.add_all(
            [
                UserCatalog(
                    user_id=user.id,
                    slot_id="rec-movie",
                    name="Recommended Movies",
                    media_type="movie",
                    generation_method="tag_recommendations",
                    is_active=True,
                ),
                UserCatalog(
                    user_id=user.id,
                    slot_id="rec-series",
                    name="Recommended Shows",
                    media_type="tv",
                    generation_method="tag_recommendations",
                    is_active=True,
                ),
            ]
        )
        db.commit()

        def catalog_content(catalog_ids, *args, **kwargs):
            content = {
                "rec-movie": [
                    {"tmdb_id": 200 + index, "title": f"Recommended Movie {index}"}
                    for index in range(24)
                ],
                "rec-series": [
                    {"tmdb_id": 300 + index, "title": f"Recommended Series {index}"}
                    for index in range(24)
                ],
            }
            return {
                catalog_id: content.get(catalog_id, []) for catalog_id in catalog_ids
            }

        with patch(
            "app.main.CatalogGenerator.get_catalog_contents_batch",
            side_effect=catalog_content,
        ):
            response = client.get("/account/api/dashboard")

        assert response.status_code == 200
        rows = response.json()["catalogs"]
        assert [row["id"] for row in rows] == [
            "personal-rec-movie",
            "personal-rec-series",
        ]
        assert [row["media_type"] for row in rows] == ["movie", "tv"]
        # Both rows belong on the page. What did not belong was both being
        # called "Trending Now": the home feed flattens movie and series rows
        # into one list and the shelf header renders the name alone, so the
        # viewer saw the same title twice with no way to tell them apart.
        names = [row["name"] for row in rows]
        assert len(set(names)) == len(names), f"two shelves share a name: {names}"
    finally:
        cleanup(db)


def test_byw_name_reason_revision_and_contents_share_the_stored_seed():
    client, db = make_client()
    try:
        user = create_logged_in_user(client, db)
        user.last_sync = utc_now()
        db.add(
            UserCatalog(
                user_id=user.id,
                slot_id="byw-movie-1",
                name="Because You Watched The Wrong Seed",
                media_type="movie",
                generation_method="because_you_watched",
                generation_params={
                    "seed": {
                        "tmdb_id": 1858,
                        "media_type": "movie",
                        "title": "Transformers",
                    },
                    "basis_tags": ["Robots", "Alien Invasion"],
                    "generation_revision": "2026-08-06T12:00:00Z",
                },
                is_active=True,
            )
        )
        db.commit()
        items = [
            {"tmdb_id": 500 + index, "title": f"Match {index}"} for index in range(24)
        ]
        with patch(
            "app.main.CatalogGenerator.get_catalog_contents_batch",
            return_value={"byw-movie-1": items},
        ):
            response = client.get("/account/api/bootstrap")

        assert response.status_code == 200
        row = response.json()["dashboard"]["rows"][0]
        assert row["name"] == "Because You Watched Transformers"
        assert row["reason"]["seed"]["tmdb_id"] == 1858
        assert row["reason"]["basis_tags"] == ["Robots", "Alien Invasion"]
        assert row["revision"] == "2026-08-06T12:00:00Z"
        assert row["item_count"] == 24
    finally:
        cleanup(db)


def test_catalog_cache_key_changes_with_personal_generation_revision():
    from app.main import _catalog_cache, _get_cached_catalog

    _catalog_cache.clear()
    db = make_client()[1]
    try:
        with patch(
            "app.main.CatalogGenerator.get_catalog_content",
            side_effect=[[{"tmdb_id": 1}], [{"tmdb_id": 2}]],
        ) as get_content:
            first = _get_cached_catalog(
                "byw-movie-1", db, user_id=1, revision="revision-1"
            )
            warm = _get_cached_catalog(
                "byw-movie-1", db, user_id=1, revision="revision-1"
            )
            rebuilt = _get_cached_catalog(
                "byw-movie-1", db, user_id=1, revision="revision-2"
            )
        assert first == warm == [{"tmdb_id": 1}]
        assert rebuilt == [{"tmdb_id": 2}]
        assert get_content.call_count == 2
    finally:
        cleanup(db)


def test_dashboard_catalog_items_include_native_addon_aliases():
    from app.main import _catalog_row_from_items

    row = _catalog_row_from_items(
        "Smart Thrillers",
        "universal-smart-thrillers",
        "movie",
        [
            {
                "tmdb_id": 42,
                "title": "The Answer",
                "overview": "A mystery.",
                "year": "2026",
                "imdb_id": "tt0000042",
                "content_rating": "PG-13",
            }
        ],
        limit=1,
    )

    item = row["items"][0]
    assert item["id"] == "tmdb:42"
    assert item["type"] == "movie"
    assert item["name"] == "The Answer"
    assert item["description"] == "A mystery."
    assert item["releaseInfo"] == "2026"
    assert item["imdbId"] == "tt0000042"
    assert item["tmdbId"] == 42
    assert item["contentRating"] == "PG-13"


def test_dashboard_series_catalog_id_is_title_identity_not_playback_identity():
    from app.main import _catalog_row_from_items

    row = _catalog_row_from_items(
        "Trending Series",
        "personal-trending-series",
        "series",
        [{"tmdb_id": 615, "title": "Futurama"}],
        limit=1,
    )

    item = row["items"][0]
    assert item["id"] == "tmdb:615"
    assert item["video_id"] == "tmdb:615:1:1"
    assert item["type"] == "series"


def test_details_api_returns_cast_episodes_and_similar_titles():
    client, db = make_client()
    try:
        create_logged_in_user(client, db)
        tv_detail = {
            "id": 202,
            "name": "Deep Space",
            "first_air_date": "2026-02-01",
            "overview": "A crew maps impossible stars.",
            "genres": [{"name": "Sci-Fi & Fantasy"}],
            "poster_path": "/poster.jpg",
            "backdrop_path": "/backdrop.jpg",
            "vote_average": 8.3,
            "episode_run_time": [52],
            "external_ids": {"imdb_id": "tt0000202"},
            "created_by": [{"name": "Ada Writer"}],
            "credits": {
                "cast": [
                    {
                        "name": "Lead Actor",
                        "character": "Captain Nova",
                        "profile_path": "/lead.jpg",
                    }
                ],
                "crew": [],
            },
            "seasons": [
                {
                    "season_number": 1,
                    "name": "Season 1",
                    "overview": "Launch year.",
                    "episode_count": 1,
                    "poster_path": "/season.jpg",
                }
            ],
        }
        season_detail = {
            "episodes": [
                {
                    "season_number": 1,
                    "episode_number": 1,
                    "name": "First Light",
                    "overview": "The mission begins.",
                    "air_date": "2026-02-01",
                    "still_path": "/still.jpg",
                }
            ]
        }
        similar_detail = {
            "results": [
                {
                    "id": 303,
                    "name": "Nearby Galaxy",
                    "first_air_date": "2025-01-01",
                    "overview": "Another crew explores.",
                    "poster_path": "/sim.jpg",
                    "backdrop_path": "/sim-bg.jpg",
                    "vote_average": 7.2,
                }
            ]
        }

        # Detail snapshot work intentionally opens its own session in a worker
        # thread. Pin that helper to this test database as well as overriding the
        # request dependency; otherwise collection order can leave it attached
        # to another module's SQLite URL.
        with patch("app.main.get_db", side_effect=lambda: use_test_db(db)), patch(
            "app.tmdb_client.tmdb_client.get_tv_show",
            new=AsyncMock(return_value=tv_detail),
        ), patch(
            "app.tmdb_client.tmdb_client.get_tv_season",
            new=AsyncMock(return_value=season_detail),
        ), patch(
            "app.tmdb_client.tmdb_client.get_similar_tv_shows",
            new=AsyncMock(return_value=similar_detail),
        ):
            response = client.get("/account/api/details?tmdb_id=202&media_type=tv")

        assert response.status_code == 200
        data = response.json()
        assert data["item"]["title"] == "Deep Space"
        assert data["cast"][0]["name"] == "Lead Actor"
        assert data["seasons"][0]["episodes"][0]["title"] == "First Light"
        assert data["similar"][0]["title"] == "Nearby Galaxy"
    finally:
        cleanup(db)


def test_dashboard_html_has_clean_remote_playback_controls():
    client, db = make_client()
    try:
        create_logged_in_user(client, db)
        response = client.get("/account/dashboard")
        assert response.status_code == 200
        html = response.text
        # The player was modularized out of App.tsx into src/player/*. Read the
        # player component, its extracted engine, and the shared source loader
        # (cached discovery used by both the player and the source picker) as the
        # playback source surface.
        _src = Path(__file__).parent.parent / "src"
        app_source = (
            (_src / "player" / "Player.tsx").read_text()
            + "\n"
            + (_src / "player" / "playerEngine.ts").read_text()
            + "\n"
            + (_src / "api" / "playbackSources.ts").read_text()
        )
        css_source = (_src / "player" / "Player.module.css").read_text()
        account_client_source = (_src / "api" / "accountClient.ts").read_text()

        assert '<div id="root">' in html or "Dashboard build missing" in html
        assert "Curatio Theatre" not in html
        assert "Audio & Subtitles" not in app_source
        assert 'aria-label="Close player"' in app_source
        assert "function Player(" in app_source
        assert "PlaybackSettings" in app_source
        assert "seekToMovieTime" in app_source
        assert "togglePlay" in app_source
        # v2 playback API (docs/playback-api.md): structured sources only.
        assert "resolvePlaybackSources" in app_source
        assert "fetchDirectStreamMetadata" not in app_source
        assert "/account/api/playback/sources" in account_client_source
        assert "/account/api/playback/streams" not in account_client_source
        assert "server_resolve" not in account_client_source
        # Server order is the ranking, as on tvOS/iOS; browsers only
        # demote what they cannot decode.
        assert "playableStreams" in app_source
        # Capability-aware playback: classify each source for THIS browser, hide
        # the ones it can't play, auto-fall-back, and remux only when needed.
        assert "classifyStream" in app_source
        assert "playableStreams" in app_source
        # Resilient failover: service-first source selection + a typed failure path.
        assert "selectNextSource" in app_source
        assert "handleSourceFailure" in app_source
        compat_source = (
            Path(__file__).parent.parent / "src" / "api" / "streamCompat.ts"
        ).read_text()
        assert "MediaSource.isTypeSupported" in compat_source
        assert "getBrowserCaps" in compat_source
        assert 'id="localVideoPicker"' not in html
        assert "Open local video" not in app_source
        assert "URL.createObjectURL(file)" not in app_source
        assert "arrayBuffer" not in app_source
        assert "cursor: none" in css_source or "cursor:none" in css_source
        assert "navigator.mediaSession" in app_source
        assert "MediaMetadata" in app_source
        assert "wakeLock.request('screen')" in app_source
        assert "attachExternalSubtitles" in app_source
        assert "srtToVtt" in app_source
        assert "URL.createObjectURL(blob)" in app_source
        assert "subtitleEntriesFromStream" in app_source
        assert "stream.subtitles" in app_source
        assert "canPlayType('application/vnd.apple.mpegurl')" in app_source
        assert "canPlayType('application/x-mpegURL')" in app_source
        assert "Hls.Events.AUDIO_TRACKS_UPDATED" in app_source
        assert "Hls.Events.AUDIO_TRACK_SWITCHED" in app_source
        assert "Hls.Events.SUBTITLE_TRACKS_UPDATED" in app_source
        assert "Hls.Events.SUBTITLE_TRACK_SWITCH" in app_source
        assert "attachNativeTrackListeners" in app_source
        assert "items.slice(0, 40)" not in app_source
        # tvOS empty-state copy for the track menus.
        assert "No audio tracks available" in app_source
        assert "No subtitle tracks available" in app_source
        # Display tab offers the iPad Fit / Fill modes.
        assert "Whole video visible" in app_source
        assert "Fill screen" in app_source
        assert "Off" in app_source
    finally:
        cleanup(db)


def test_presence_auth_ownership_and_terminal_state():
    from uuid import uuid4
    from app.models import PlaybackPresence

    client, db = make_client()
    try:
        payload = dict(
            session_id=str(uuid4()),
            sequence=0,
            device_id="web-device",
            platform="web",
            media_id="tt123",
            media_type="movie",
            state="playing",
            selected_provider="Plex",
        )
        assert (
            client.post("/account/api/playback/presence", json=payload).status_code
            == 401
        )
        user = create_logged_in_user(client, db)
        response = client.post("/account/api/playback/presence", json=payload)
        assert response.status_code == 200 and response.json()["accepted"]
        assert db.get(PlaybackPresence, payload["session_id"]).user_id == user.id
        response = client.post(
            "/api/v2/playback/presence",
            headers=_bearer("web-user-key"),
            json={**payload, "sequence": 1, "state": "ended"},
        )
        assert response.status_code == 200 and response.json()["accepted"]
        response = client.post(
            "/api/v2/playback/presence",
            headers=_bearer("web-user-key"),
            json={**payload, "sequence": 2},
        )
        assert response.status_code == 200 and not response.json()["accepted"]
        assert client.post(
            "/api/v2/playback/presence", headers=_bearer("wrong-key"), json=payload
        ).status_code in (401, 404)
    finally:
        cleanup(db)


def test_live_activity_is_admin_only_and_upstream_outage_keeps_player_reports():
    from uuid import uuid4
    from app.admin import verify_admin

    client, db = make_client()
    try:
        create_logged_in_user(client, db)
        with patch("app.admin.get_db", lambda: use_test_db(db)):
            assert client.get("/admin/api/live-activity").status_code in (401, 403)
        client.post(
            "/account/api/playback/presence",
            json={
                "session_id": str(uuid4()),
                "sequence": 0,
                "device_id": "one",
                "platform": "web",
                "media_id": "tt123",
                "media_type": "movie",
                "state": "playing",
                "selected_provider": "Plex",
            },
        )
        from app.sources import registry
        from app.sources.base import SourceProvider

        class FlakyAddon(SourceProvider):
            id, label, kind = "flaky", "Flaky", "addon"

            def enabled(self):
                return True

            async def live_activity(self):
                raise RuntimeError("unavailable")

        app.dependency_overrides[verify_admin] = lambda: True
        registry.set_providers_for_tests([FlakyAddon()])
        try:
            with patch("app.admin.get_db", lambda: use_test_db(db)):
                response = client.get("/admin/api/live-activity")
        finally:
            registry.set_providers_for_tests(None)
        assert response.status_code == 200
        assert response.json()["counts"]["playing"] == 1
        assert response.json()["upstream"][0]["state"] == "unavailable"
        assert response.json()["playback"][0]["selected_provider"] == "Plex"
    finally:
        cleanup(db)


def test_web_presence_keeps_initiating_profile_when_another_tab_switches():
    from uuid import uuid4
    from app.models import PlaybackPresence, UserSession

    client, db = make_client()
    try:
        first = create_logged_in_user(client, db)
        second = User(
            user_key="second-profile",
            account_id=first.account_id,
            display_name="Second",
            is_active=True,
        )
        outsider = User(
            user_key="unrelated-profile", display_name="Unrelated", is_active=True
        )
        db.add_all([second, outsider])
        db.commit()
        payload = dict(
            session_id=str(uuid4()),
            profile_id=first.id,
            sequence=0,
            device_id="first-tab",
            platform="web",
            media_id="tt123",
            media_type="movie",
            state="playing",
        )
        assert client.post("/account/api/playback/presence", json=payload).json()[
            "accepted"
        ]
        # Another tab selects a different profile on the same account cookie.
        db.query(UserSession).one().active_profile_id = second.id
        db.commit()
        response = client.post(
            "/account/api/playback/presence",
            json={**payload, "sequence": 1, "state": "paused"},
        )
        assert response.status_code == 200 and response.json()["accepted"]
        db.expire_all()
        row = db.get(PlaybackPresence, payload["session_id"])
        assert row.user_id == first.id and row.state == "paused"
        # A first report racing the profile switch still belongs to its original viewer.
        fresh = {**payload, "session_id": str(uuid4())}
        assert client.post("/account/api/playback/presence", json=fresh).json()[
            "accepted"
        ]
        assert db.get(PlaybackPresence, fresh["session_id"]).user_id == first.id
        assert (
            client.post(
                "/account/api/playback/presence",
                json={**fresh, "profile_id": outsider.id},
            ).status_code
            == 403
        )
    finally:
        cleanup(db)


def test_account_routes_require_a_session():
    client, db = make_client()
    try:
        assert client.delete("/account/api/library/movie/1").status_code in (401, 403)
        assert client.delete("/account/api/progress/movie/1").status_code in (401, 403)
        assert client.get("/account/api/watched").status_code in (401, 403)
        assert client.post(
            "/account/api/scrobble", json={"tmdb_id": 1, "media_type": "movie"}
        ).status_code in (401, 403)
    finally:
        cleanup(db)


def test_account_remove_library_item_uses_active_profile():
    client, db = make_client()
    try:
        user = create_logged_in_user(client, db)
        db.add(LibraryItem(user_id=user.id, tmdb_id=101, media_type="movie"))
        db.add(LibraryItem(user_id=user.id, imdb_id="tt0000202", media_type="tv"))
        db.commit()
        with patch("app.main.get_db", return_value=use_test_db(db)):
            assert client.delete("/account/api/library/movie/101").status_code == 200
        with patch("app.main.get_db", return_value=use_test_db(db)):
            assert client.delete("/account/api/library/tv/tt0000202").status_code == 200
        assert db.query(LibraryItem).filter_by(user_id=user.id).count() == 0
    finally:
        cleanup(db)


def test_account_mark_watched_then_unwatched_round_trips():
    client, db = make_client()
    try:
        user = create_logged_in_user(client, db)
        with patch("app.main.get_db", return_value=use_test_db(db)), patch(
            "app.main._canonicalize_write_ids",
            new=AsyncMock(return_value=(303, "tt0000303", "movie", None, None)),
        ):
            response = client.post(
                "/account/api/scrobble",
                json={"tmdb_id": 303, "imdb_id": "tt0000303", "media_type": "movie"},
            )
        assert response.status_code == 200
        assert db.query(WatchEvent).filter_by(user_id=user.id, tmdb_id=303).count() == 1

        with patch("app.main.get_db", return_value=use_test_db(db)):
            watched = client.get("/account/api/watched")
        assert watched.status_code == 200
        assert any(item.get("tmdb_id") == 303 for item in watched.json()["items"])

        with patch("app.main.get_db", return_value=use_test_db(db)):
            removed = client.delete("/account/api/progress/movie/303")
        assert removed.status_code == 200
        assert removed.json()["removed"] is True
        assert db.query(WatchEvent).filter_by(user_id=user.id, tmdb_id=303).count() == 0
    finally:
        cleanup(db)


def test_account_segment_feedback_records_for_active_profile():
    client, db = make_client()
    try:
        user = create_logged_in_user(client, db)
        with patch("app.main.get_db", return_value=use_test_db(db)):
            response = client.post(
                "/account/api/segments/tt0000404:1:2/feedback",
                json={"segment_type": "intro", "feedback": "wrong"},
            )
        assert response.status_code == 200
        # The real get_db() commits on exit; the test stand-in does not.
        db.flush()
        row = db.query(SegmentFeedback).filter_by(user_id=user.id).one()
        assert (row.imdb_id, row.season, row.episode) == ("tt0000404", 1, 2)
    finally:
        cleanup(db)


def test_playback_sources_api_for_native_and_web_clients():
    """v2 playback lookup: one request, structured sources, availability."""
    from app.sources.models import PlaybackSource
    from app.sources import resolver as source_resolver

    client, db = make_client()
    try:
        user = create_logged_in_user(client, db)
        source = PlaybackSource(
            id="s1",
            provider_id="plex",
            provider_label="Plex",
            kind="library",
            url="http://localhost:8000/media/token",
            route_id="r1",
        )
        calls, reports = [], []

        async def fake_resolve(media, ctx):
            calls.append((media, ctx.quality, ctx.refresh))
            return [source]

        async def fake_report(report):
            reports.append(report)

        with patch("app.main.get_db", lambda: use_test_db(db)), patch(
            "app.main.source_resolver.resolve", fake_resolve
        ), patch("app.main.source_resolver.report_failure", fake_report), patch(
            "app.main._trigger_episode_fingerprint", lambda *a, **k: None
        ):
            response = client.post(
                "/api/v2/playback/sources",
                headers=_bearer(user.user_key),
                json={
                    "type": "series",
                    "imdb_id": "tt0944947",
                    "season": 1,
                    "episode": 2,
                },
            )
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            body = response.json()
            assert body["sources"][0]["id"] == "s1"
            assert body["quality"] == "original"
            assert set(body["availability"]) == {"playable", "in_library", "sources"}
            assert (calls[-1][0].season, calls[-1][0].episode) == (1, 2)

            response = client.post(
                "/account/api/playback/sources",
                json={
                    "type": "movie",
                    "tmdb_id": 949,
                    "quality": "optimized",
                    "refresh": True,
                },
            )
            assert response.status_code == 200
            assert calls[-1][1:] == ("optimized", True)

            bad = client.post(
                "/api/v2/playback/sources",
                headers=_bearer(user.user_key),
                json={"type": "series", "imdb_id": "tt0944947"},
            )
            assert bad.status_code == 400
            assert bad.json()["detail"]["code"] == "invalid_media"

            ok = client.post(
                "/api/v2/playback/failure",
                headers=_bearer(user.user_key),
                json={
                    "source_id": "s1",
                    "provider_id": "plex",
                    "failure_category": "network",
                },
            )
            assert ok.json() == {"status": "ok"}
            assert reports[0].provider_id == "plex"
            assert (
                client.post(
                    "/api/v2/playback/failure", headers=_bearer("nobody"), json={}
                ).status_code
                == 401
            )

        async def unavailable(media, ctx):
            raise source_resolver.ProvidersUnavailable()

        with patch("app.main.get_db", lambda: use_test_db(db)), patch(
            "app.main.source_resolver.resolve", unavailable
        ):
            response = client.post(
                "/api/v2/playback/sources",
                headers=_bearer(user.user_key),
                json={"type": "movie", "imdb_id": "tt0113277"},
            )
        assert response.status_code == 502
        assert response.json()["detail"]["code"] == "providers_unavailable"
    finally:
        cleanup(db)


def test_items_carry_availability_and_version_is_public():
    client, db = make_client()
    try:
        assert client.get("/api/version").json()["api_version"] == 2
        user = create_logged_in_user(client, db)
        with patch("app.main.get_db", lambda: use_test_db(db)):
            response = client.get("/api/v2/bootstrap", headers=_bearer(user.user_key))
        assert response.status_code == 200
        assert "playback_providers" in response.json()
    finally:
        cleanup(db)
