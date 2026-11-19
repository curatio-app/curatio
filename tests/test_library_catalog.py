"""Library-first features: sync fields, "On Your Servers", My Servers scope,
the Library tab and search scope. AIOStreams-style addons must never count as
ownership."""

# ruff: noqa: E402
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from tests.test_sources import (  # noqa: F401  (env + fixtures)
    JF_TOKEN,
    PLEX_TOKEN,
    _Addon,
    _client,
    tables,
)

from app import library_catalog, main
from app.database import get_db
from app.models import MediaAvailability, MediaMetadata
from app.sources import availability, registry
from app.sources.jellyfin import JellyfinProvider
from app.sources.library_base import iso_to_datetime
from app.sources.plex import PlexProvider

# Plex addedAt (Unix seconds): Heat older, Alien newer, Show newest.
HEAT_ADDED = 1_600_000_000
ALIEN_ADDED = 1_700_000_000
SHOW_ADDED = 1_750_000_000


def _plex_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers.get("X-Plex-Token") == PLEX_TOKEN
    path = request.url.path
    if path == "/library/sections":
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Directory": [
                        {"key": "1", "type": "movie"},
                        {"key": "2", "type": "show"},
                    ]
                }
            },
        )
    if path == "/library/sections/1/all":
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "totalSize": 3,
                    "Metadata": [
                        {
                            "ratingKey": "100",
                            "title": "Heat",
                            "year": 1995,
                            "addedAt": HEAT_ADDED,
                            "thumb": "/library/metadata/100/thumb/1",
                            "Media": [
                                {"videoResolution": "1080"},
                                {"videoResolution": "4k"},
                            ],
                            "Guid": [{"id": "imdb://tt0113277"}, {"id": "tmdb://949"}],
                        },
                        {
                            "ratingKey": "101",
                            "title": "Alien",
                            "addedAt": ALIEN_ADDED,
                            "Media": [{"videoResolution": "720"}],
                            "Guid": [{"id": "tmdb://348"}],
                        },
                        {"ratingKey": "102", "title": "Home Video"},
                    ],
                }
            },
        )
    if path == "/library/sections/2/all":
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "totalSize": 1,
                    "Metadata": [
                        {
                            "ratingKey": "200",
                            "title": "Show",
                            "addedAt": SHOW_ADDED,
                            "Guid": [{"id": "tmdb://1399"}],
                        }
                    ],
                }
            },
        )
    return httpx.Response(404)


@pytest.fixture
def plex(monkeypatch):
    provider = PlexProvider()
    provider.save_config(
        {
            "enabled": True,
            "server_url": "http://192.168.1.5:32400",
            "token": PLEX_TOKEN,
            "server_id": "srv1",
            "server_name": "Home",
            "client_id": "cid",
        }
    )
    monkeypatch.setattr("app.sources.plex.http_client", lambda: _client(_plex_handler))
    calls = SimpleNamespace(prepared=0, invalidated=0)

    async def prepared():
        calls.prepared += 1
        return 0

    def invalidated():
        calls.invalidated += 1

    monkeypatch.setattr(library_catalog, "prepare_owned_metadata", prepared)
    monkeypatch.setattr(library_catalog, "invalidate_home", invalidated)
    provider.calls = calls
    registry.set_providers_for_tests([provider])
    yield provider
    provider.save_config(None)


@pytest.fixture
def metadata():
    """Catalog metadata for the owned titles plus one title nobody owns."""
    rows = [
        ("movie", 949, "Heat", "1995-12-15", 9000),
        ("movie", 348, "Alien", "1979-05-25", 15000),
        ("tv", 1399, "Show", "2011-04-17", 20000),
        ("movie", 603, "The Matrix", "1999-03-31", 25000),
    ]
    with get_db() as db:
        db.query(MediaMetadata).delete()
        for media_type, tmdb_id, title, released, votes in rows:
            db.add(
                MediaMetadata(
                    tmdb_id=tmdb_id,
                    media_type=media_type,
                    title=title,
                    release_date=released,
                    vote_count=votes,
                    poster_path=f"/{tmdb_id}.jpg",
                )
            )
        db.commit()
    yield
    with get_db() as db:
        db.query(MediaMetadata).delete()
        db.commit()


# --- sync ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_plex_sync_stores_added_date_best_resolution_and_artwork(plex):
    result = await plex.sync()
    assert result["titles"] == 3 and result["movies"] == 2 and result["shows"] == 1
    assert result["unmatched"] == 1  # "Home Video" is kept for Other Videos
    assert result["added"] == 4 and result["removed"] == 0
    with get_db() as db:
        heat = db.query(MediaAvailability).filter_by(item_key="100").one()
        alien = db.query(MediaAvailability).filter_by(item_key="101").one()
    assert heat.added_at == datetime(2020, 9, 13, 12, 26, 40)
    assert heat.resolution == "2160p"  # best of the two copies
    assert heat.thumb == "/library/metadata/100/thumb/1"
    assert alien.resolution == "720p"


@pytest.mark.asyncio
async def test_sync_refreshes_home_only_when_the_library_changes(plex):
    await plex.sync()
    assert (plex.calls.prepared, plex.calls.invalidated) == (1, 1)
    await plex.sync()  # nothing new, nothing removed
    assert (plex.calls.prepared, plex.calls.invalidated) == (1, 1)


def test_jellyfin_dates_parse_with_seven_fraction_digits():
    assert iso_to_datetime("2024-03-01T10:20:30.1234567Z") == datetime(
        2024, 3, 1, 10, 20, 30, 123456
    )
    assert iso_to_datetime("2024-03-01T12:00:00+02:00") == datetime(2024, 3, 1, 10)
    assert iso_to_datetime("not a date") is None and iso_to_datetime(None) is None


@pytest.mark.asyncio
async def test_jellyfin_sync_reads_date_resolution_and_primary_image(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Emby-Token") == JF_TOKEN
        assert "DateCreated" in request.url.params["Fields"]
        return httpx.Response(
            200,
            json={
                "TotalRecordCount": 1,
                "Items": [
                    {
                        "Id": "m1",
                        "Type": "Movie",
                        "Name": "Heat",
                        "Width": 3840,
                        "Height": 1600,
                        "DateCreated": "2024-03-01T10:20:30.1234567Z",
                        "ImageTags": {"Primary": "abc"},
                        "ProviderIds": {"Tmdb": "949"},
                    }
                ],
            },
        )

    provider = JellyfinProvider()
    provider.save_config(
        {
            "enabled": True,
            "server_url": "http://jf.lan:8096",
            "token": JF_TOKEN,
            "user_id": "u1",
            "server_id": "jf1",
        }
    )
    monkeypatch.setattr("app.sources.jellyfin.http_client", lambda: _client(handler))
    try:
        (item,) = await provider.list_items(provider.config())
    finally:
        provider.save_config(None)
    assert item.added_at == datetime(2024, 3, 1, 10, 20, 30, 123456)
    assert item.resolution == "2160p"
    assert item.thumb == "/Items/m1/Images/Primary"


# --- Home rows and scope --------------------------------------------------------------
@pytest.mark.asyncio
async def test_on_your_servers_row_is_newest_first(plex, metadata):
    await plex.sync()
    with get_db() as db:
        rows = library_catalog.home_rows(db, "everything")
    assert [row["id"] for row in rows] == [library_catalog.RECENT_ROW_ID]
    assert rows[0]["name"] == "On Your Servers"
    assert [(i["media_type"], i["tmdb_id"]) for i in rows[0]["items"]] == [
        ("tv", 1399),
        ("movie", 348),
        ("movie", 949),
    ]


@pytest.mark.asyncio
async def test_my_servers_scope_filters_discovery_rows(plex, metadata):
    await plex.sync()
    with get_db() as db:
        owned = library_catalog.owned_entries(db)
    owned_movie = {"media_type": "movie", "tmdb_id": 949}
    unowned = {"media_type": "movie", "tmdb_id": 603}
    rows = [
        {"id": "mostly-owned", "items": [owned_movie] * 6 + [unowned]},
        {"id": "barely-owned", "items": [owned_movie, unowned, unowned]},
    ]
    kept = library_catalog.filter_rows_to_owned(rows, owned)
    assert [row["id"] for row in kept] == ["mostly-owned"]
    assert all(item["tmdb_id"] == 949 for item in kept[0]["items"])


@pytest.mark.asyncio
async def test_no_library_means_no_library_rows(metadata):
    registry.set_providers_for_tests([])
    with get_db() as db:
        assert library_catalog.home_rows(db, "my_servers") == []
        assert library_catalog.owned_entries(db) == {}


# --- AIOStreams safety ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_searchable_addon_never_counts_as_owned(plex, metadata):
    await plex.sync()
    registry.set_providers_for_tests([plex, _Addon()])
    with get_db() as db:
        owned = library_catalog.owned_entries(db)
        cards = [
            {"id": "tmdb:603", "tmdb_id": 603, "type": "movie"},
            {"id": "tmdb:949", "tmdb_id": 949, "type": "movie"},
        ]
        availability.annotate_tree(db, cards)
        rows = library_catalog.home_rows(db, "everything")
    assert (("movie", 603) in owned) is False
    matrix, heat = cards
    assert matrix["availability"]["playable"] is True  # the addon can play it
    assert matrix["availability"]["in_library"] is False  # but nobody owns it
    assert heat["availability"]["in_library"] is True
    assert 603 not in {item["tmdb_id"] for item in rows[0]["items"]}


@pytest.mark.asyncio
async def test_search_in_my_servers_scope_keeps_only_owned_titles(plex, metadata):
    await plex.sync()
    registry.set_providers_for_tests([plex, _Addon()])
    payload = {
        "items": [
            {"tmdb_id": 603, "media_type": "movie", "type": "movie"},
            {"tmdb_id": 949, "media_type": "movie", "type": "movie"},
        ],
        "sections": [
            {"id": "a", "items": [{"tmdb_id": 603, "type": "movie"}]},
            {"id": "b", "items": [{"tmdb_id": 949, "type": "movie"}]},
        ],
    }
    with get_db() as db:
        availability.annotate_tree(db, payload)
    everything = main._scoped_search(
        SimpleNamespace(content_scope="everything"), dict(payload)
    )
    assert len(everything["items"]) == 2
    scoped = main._scoped_search(SimpleNamespace(content_scope="my_servers"), payload)
    assert [i["tmdb_id"] for i in scoped["items"]] == [949]
    assert [s["id"] for s in scoped["sections"]] == ["b"]


# --- Library tab --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_library_page_sorts_filters_and_pages(plex, metadata):
    await plex.sync()
    user = SimpleNamespace(id=999_999)
    with get_db() as db:
        page = main._owned_library_page(
            db, user, kind="movie", sort="title", filter_=None, offset=0, limit=1
        )
        assert [i["title"] for i in page["items"]] == ["Alien"]
        assert page["total"] == 2 and page["next_offset"] == 1
        assert page["counts"] == {"movies": 2, "shows": 1, "other": 1}
        assert page["items"][0]["availability"]["in_library"] is True
        assert page["items"][0]["type"] == "movie"

        shows = main._owned_library_page(
            db, user, kind="series", sort="added", filter_=None, offset=0, limit=10
        )
        assert [i["title"] for i in shows["items"]] == ["Show"]
        assert shows["items"][0]["type"] == "series"

        uhd = main._owned_library_page(
            db, user, kind=None, sort="added", filter_="4k", offset=0, limit=10
        )
        assert [i["title"] for i in uhd["items"]] == ["Heat"]
        assert uhd["items"][0]["server_resolution"] == "2160p"


@pytest.mark.asyncio
async def test_home_membership_leads_with_library_rows(plex, metadata):
    from app import home_feed
    from app.models import Account, User

    await plex.sync()
    with get_db() as db:
        account = Account(email="lib@example.test", password_hash="x", is_active=True)
        db.add(account)
        db.flush()
        user = User(user_key="lib-key", account_id=account.id, is_active=True)
        db.add(user)
        db.flush()
        try:
            membership = home_feed.build_membership(db, user)
            assert membership["rows"][0]["id"] == library_catalog.RECENT_ROW_ID
            assert membership["rows"][0]["type"] == "mixed"
            assert membership["rows"][0]["members"][0] == ["tv", 1399]
            before = home_feed._source_fingerprint(db, user)
            user.content_scope = "my_servers"
            assert home_feed._source_fingerprint(db, user) != before
        finally:
            db.rollback()


# --- incremental sync -------------------------------------------------------------------
@pytest.mark.asyncio
async def test_quick_sync_adds_new_arrivals_without_removing_anything(
    plex, monkeypatch
):
    await plex.sync()
    assert plex.calls.invalidated == 1
    plex.update_config(last_quick_sync_at=None)

    new_movie = {
        "ratingKey": "103",
        "title": "Brand New",
        "addedAt": SHOW_ADDED + 600,
        "Guid": [{"id": "tmdb://550"}],
    }
    requested = []

    def recent_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/library/sections/1/all":
            requested.append(dict(request.url.params))
            # Newest first; the listing stops paging at the first old title.
            return httpx.Response(
                200,
                json={
                    "MediaContainer": {
                        "totalSize": 4,
                        "Metadata": [
                            new_movie,
                            {
                                "ratingKey": "101",
                                "title": "Alien",
                                "addedAt": ALIEN_ADDED,
                                "Guid": [{"id": "tmdb://348"}],
                            },
                        ],
                    }
                },
            )
        if request.url.path == "/library/sections/2/all":
            return httpx.Response(
                200,
                json={
                    "MediaContainer": {
                        "totalSize": 1,
                        "Metadata": [
                            {
                                "ratingKey": "200",
                                "title": "Show",
                                "addedAt": SHOW_ADDED,
                                "Guid": [{"id": "tmdb://1399"}],
                            },
                        ],
                    }
                },
            )
        return _plex_handler(request)

    monkeypatch.setattr("app.sources.plex.http_client", lambda: _client(recent_handler))
    result = await plex.quick_sync()
    assert result["status"] == "ok" and result["added"] == 1
    assert requested[0]["sort"] == "addedAt:desc"
    with get_db() as db:
        keys = {
            r.item_key for r in db.query(MediaAvailability).filter_by(provider="plex")
        }
    assert keys == {"100", "101", "102", "103", "200"}  # nothing removed
    assert plex.calls.invalidated == 2

    # A run another worker just recorded is skipped.
    assert (await plex.quick_sync())["status"] == "skipped"


@pytest.mark.asyncio
async def test_quick_sync_waits_for_the_first_full_scan(plex):
    assert (await plex.quick_sync())["status"] == "skipped"


@pytest.mark.asyncio
async def test_jellyfin_recent_listing_stops_at_the_cutoff(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "TotalRecordCount": 500,
                "Items": [
                    {
                        "Id": "new",
                        "Type": "Movie",
                        "Name": "New",
                        "DateCreated": "2026-11-15T10:00:00Z",
                        "ProviderIds": {"Tmdb": "1"},
                    },
                    {
                        "Id": "old",
                        "Type": "Movie",
                        "Name": "Old",
                        "DateCreated": "2020-01-01T00:00:00Z",
                        "ProviderIds": {"Tmdb": "2"},
                    },
                ],
            },
        )

    provider = JellyfinProvider()
    provider.save_config(
        {
            "enabled": True,
            "server_url": "http://jf.lan:8096",
            "token": JF_TOKEN,
            "user_id": "u1",
            "server_id": "jf1",
        }
    )
    monkeypatch.setattr("app.sources.jellyfin.http_client", lambda: _client(handler))
    try:
        items = await provider.list_recent(provider.config(), datetime(2026, 11, 1))
    finally:
        provider.save_config(None)
    assert [i.item_key for i in items] == ["new"]
    assert len(calls) == 1  # did not page through the other 498
    assert calls[0]["SortBy"] == "DateCreated" and calls[0]["SortOrder"] == "Descending"


@pytest.mark.asyncio
async def test_tmdb_metadata_older_than_the_limit_is_refreshed(metadata, monkeypatch):
    from datetime import timedelta

    from app.time_utils import utc_now
    from workers import metadata_refresh

    with get_db() as db:
        db.query(MediaMetadata).filter_by(tmdb_id=949).update(
            {MediaMetadata.fetched_at: utc_now() - timedelta(days=200)}
        )
        db.commit()

    fetched = []

    async def get_movie(tmdb_id):
        fetched.append(tmdb_id)
        return {
            "id": tmdb_id,
            "title": "Heat (refreshed)",
            "release_date": "1995-12-15",
        }

    from app.tmdb_client import tmdb_client

    monkeypatch.setattr(tmdb_client, "get_movie", get_movie)
    monkeypatch.setattr(
        tmdb_client,
        "extract_metadata",
        lambda details, media_type: {
            "tmdb_id": details["id"],
            "media_type": media_type,
            "title": details["title"],
        },
    )
    result = await metadata_refresh.refresh_stale_metadata()
    assert result == {"status": "ok", "refreshed": 1, "failed": 0}
    assert fetched == [949]  # fresh rows are left alone
    with get_db() as db:
        heat = db.query(MediaMetadata).filter_by(tmdb_id=949).one()
    assert heat.title == "Heat (refreshed)"
    assert utc_now().replace(tzinfo=None) - heat.fetched_at.replace(
        tzinfo=None
    ) < timedelta(minutes=5)


# --- Other Videos (unmatched files) ---------------------------------------------------------
@pytest.mark.asyncio
async def test_unmatched_videos_are_listed_and_playable_by_item_key(plex, monkeypatch):
    from app.sources import resolver
    from app.sources.models import PlaybackContext

    await plex.sync()
    with get_db() as db:
        items, total = library_catalog.other_items(db)
        page = main._owned_library_page(
            db,
            SimpleNamespace(id=1),
            kind="other",
            sort="added",
            filter_=None,
            offset=0,
            limit=10,
        )
    assert total == 1 and items[0]["title"] == "Home Video"
    assert items[0]["library_item"] == "plex:102"
    assert items[0]["availability"]["in_library"] is True
    assert (
        page["counts"]["other"] == 1 and page["items"][0]["library_item"] == "plex:102"
    )
    # Owned-title views and Home never include it (no identity to match on).
    with get_db() as db:
        assert all(key[1] for key in library_catalog.owned_entries(db))

    async def fake_sources_for(cfg, row, media, quality, hints=None):
        assert row.item_key == "102"
        return ["source-for-102"]

    monkeypatch.setattr(plex, "sources_for", fake_sources_for)
    ctx = PlaybackContext(user_key="k")
    assert await resolver.resolve_library_item("plex:102", ctx) == ["source-for-102"]
    assert await resolver.resolve_library_item("plex:999", ctx) == []
    assert await resolver.resolve_library_item("aio:102", ctx) == []  # never an addon


# --- quality ladder and home-network links ----------------------------------------------------
def test_transcode_ladder_fits_the_device():
    from app.sources.library_base import transcode_ladder
    from app.sources.models import PlaybackHints

    def rungs(**hints):
        source_height = hints.pop("source_height", 2160)
        return [
            (r.resolution, r.bitrate_kbps, r.audio_channels)
            for r in transcode_ladder(PlaybackHints(**hints), source_height)
        ]

    # No hints: the historic 8 Mbps 1080p stereo, plus a lighter rung.
    assert rungs() == [("1080p", 8000, 2), ("720p", 4000, 2)]
    assert rungs(max_bitrate_kbps=40_000, audio_channels=6) == [
        ("1080p", 20000, 6),
        ("1080p", 8000, 6),
    ]
    assert rungs(max_bitrate_kbps=3_000) == [("720p", 2000, 2)]
    assert rungs(max_bitrate_kbps=40_000, max_resolution="720p") == [
        ("720p", 4000, 2),
        ("720p", 2000, 2),
    ]
    # Never a larger frame than the file itself.
    assert rungs(max_bitrate_kbps=40_000, source_height=480)[0][0] == "480p"


@pytest.mark.asyncio
async def test_plex_hands_out_a_home_network_link_with_a_transient_token(
    plex, monkeypatch
):
    from app.sources.plex import lan_base_url

    assert lan_base_url("http://192.168.1.5:32400") == "http://192.168.1.5:32400"
    assert lan_base_url("https://192-168-1-5.abc123.plex.direct:32400").startswith(
        "https://192-168-1-5"
    )
    assert lan_base_url("https://203-0-113-9.abc123.plex.direct:32400") is None
    assert lan_base_url("https://plex.example.com") is None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/security/token":
            return httpx.Response(
                200, json={"MediaContainer": {"token": "transient-abc"}}
            )
        return _plex_handler(request)

    monkeypatch.setattr("app.sources.plex.http_client", lambda: _client(handler))
    link = await plex._lan_link(plex.config(), "/library/parts/9/1/file.mkv")
    assert (
        link
        == "http://192.168.1.5:32400/library/parts/9/1/file.mkv?X-Plex-Token=transient-abc"
    )
    assert PLEX_TOKEN not in link  # never the stored admin token

    plex.update_config(lan_direct_play=False)
    assert await plex._lan_link(plex.config(), "/library/parts/9/1/file.mkv") is None
