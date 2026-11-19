# ruff: noqa: E402
"""Playback sources: Plex/Jellyfin providers, signed media URLs, registry, resolver."""

import os
import time

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_sources.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

import httpx
import pytest
from fastapi import HTTPException

from app import media_proxy
from app.config import settings
from app.database import active_engine, get_db
from app.models import Base, MediaAvailability
from app.sources import availability, registry, resolver
from app.sources.base import SourceProvider
from app.sources.jellyfin import JellyfinProvider
from app.sources.models import MediaRef, PlaybackContext, PlaybackSource
from app.sources.plex import PlexProvider

PLEX_TOKEN = "plex-secret-token-123"
JF_TOKEN = "jellyfin-secret-token-456"


@pytest.fixture(autouse=True)
def tables():
    Base.metadata.create_all(bind=active_engine())
    with get_db() as db:
        db.query(MediaAvailability).delete()
        db.commit()
    resolver.clear_cache()
    yield
    registry.set_providers_for_tests(None)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- Plex -----------------------------------------------------------------------
def plex_handler(request: httpx.Request) -> httpx.Response:
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
                        {"key": "3", "type": "artist"},
                    ]
                }
            },
        )
    if path == "/library/sections/1/all":
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "totalSize": 2,
                    "Metadata": [
                        {
                            "ratingKey": "100",
                            "title": "Heat",
                            "year": 1995,
                            "Guid": [{"id": "imdb://tt0113277"}, {"id": "tmdb://949"}],
                        },
                        {"ratingKey": "101", "title": "No IDs"},
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
                            "Guid": [
                                {"id": "imdb://tt0944947"},
                                {"id": "tvdb://121361"},
                            ],
                        }
                    ],
                }
            },
        )
    if path == "/library/metadata/100":
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Metadata": [
                        {
                            "ratingKey": "100",
                            "Media": [
                                {
                                    "width": 3840,
                                    "height": 2160,
                                    "videoCodec": "hevc",
                                    "audioCodec": "eac3",
                                    "audioChannels": 6,
                                    "bitrate": 24000,
                                    "container": "mkv",
                                    "Part": [
                                        {
                                            "id": 9,
                                            "key": "/library/parts/9/1/file.mkv",
                                            "file": "/m/Heat (1995).mkv",
                                            "size": 123,
                                            "duration": 10000,
                                            "container": "mkv",
                                            "Stream": [
                                                {
                                                    "streamType": 1,
                                                    "codec": "hevc",
                                                    "DOVIPresent": True,
                                                },
                                                {
                                                    "streamType": 2,
                                                    "codec": "eac3",
                                                    "channels": 6,
                                                    "languageCode": "eng",
                                                    "selected": True,
                                                },
                                                {
                                                    "streamType": 3,
                                                    "id": 77,
                                                    "key": "/library/streams/77",
                                                    "codec": "srt",
                                                    "languageCode": "eng",
                                                    "displayTitle": "English",
                                                },
                                            ],
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                }
            },
        )
    if path == "/library/metadata/200/allLeaves":
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Metadata": [
                        {"ratingKey": "301", "parentIndex": 1, "index": 1},
                        {"ratingKey": "302", "parentIndex": 1, "index": 2},
                    ]
                }
            },
        )
    if path == "/library/metadata/302":
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Metadata": [
                        {
                            "ratingKey": "302",
                            "Media": [
                                {
                                    "width": 1920,
                                    "height": 1080,
                                    "videoCodec": "h264",
                                    "audioCodec": "aac",
                                    "audioChannels": 2,
                                    "Part": [
                                        {
                                            "id": 5,
                                            "key": "/library/parts/5/1/file.mp4",
                                            "Stream": [],
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
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
    monkeypatch.setattr("app.sources.plex.http_client", lambda: _client(plex_handler))
    yield provider
    provider.save_config(None)


@pytest.mark.asyncio
async def test_plex_sync_and_resolve_movie(plex):
    result = await plex.sync()
    assert result["status"] == "ok" and result["titles"] == 2
    owned = plex.availability([MediaRef(type="movie", imdb_id="tt0113277").title_ref()])
    assert list(owned.values()) == [True]

    sources = await plex.resolve(
        MediaRef(type="movie", tmdb_id=949), PlaybackContext(user_key="k")
    )
    # Original, then the 8 Mbps default transcode and a lighter 720p rung.
    assert [s.variant for s in sources] == ["original", "optimized", "optimized"]
    original, optimized, lighter = sources
    assert (optimized.bitrate, lighter.bitrate) == (8_000_000, 4_000_000)
    assert lighter.resolution == "720p"
    assert original.kind == "library" and original.provider_id == "plex"
    assert (
        original.resolution == "2160p"
        and original.video_codec == "hevc"
        and original.hdr == "dv"
    )
    assert original.audio_channels == 6 and original.languages == ["en"]
    assert original.label == "4K · HEVC · Dolby Vision · 5.1"
    assert original.detail == "Heat (1995).mkv"
    assert optimized.delivery == "hls" and optimized.url.endswith(".m3u8")
    assert original.file_id == optimized.file_id
    for source in sources:
        assert source.url.startswith(f"{settings.base_url}/media/")
        assert PLEX_TOKEN not in source.model_dump_json()
        assert "192.168.1.5" not in source.model_dump_json()
    provider_id, path = media_proxy.read_token(original.url.rsplit("/", 1)[1])
    assert (provider_id, path) == ("plex", "/library/parts/9/1/file.mkv")
    assert original.subtitles[0].lang == "en"


@pytest.mark.asyncio
async def test_plex_resolves_episode_and_skips_missing(plex):
    await plex.sync()
    ctx = PlaybackContext(user_key="k")
    sources = await plex.resolve(
        MediaRef(type="series", imdb_id="tt0944947", season=1, episode=2), ctx
    )
    assert sources and sources[0].resolution == "1080p"
    assert (
        await plex.resolve(
            MediaRef(type="series", imdb_id="tt0944947", season=9, episode=9), ctx
        )
        == []
    )
    assert await plex.resolve(MediaRef(type="movie", imdb_id="tt9999999"), ctx) == []


@pytest.mark.asyncio
async def test_resync_removes_titles_no_longer_on_server(plex, monkeypatch):
    await plex.sync()

    def smaller(request):
        if request.url.path == "/library/sections/2/all":
            return httpx.Response(
                200, json={"MediaContainer": {"totalSize": 0, "Metadata": []}}
            )
        return plex_handler(request)

    monkeypatch.setattr("app.sources.plex.http_client", lambda: _client(smaller))
    result = await plex.sync()
    assert result["removed"] == 1
    with get_db() as db:
        # The unmatched "No IDs" file stays (Library "Other Videos").
        assert {r.imdb_id for r in db.query(MediaAvailability)} == {"tt0113277", None}


# --- Jellyfin ---------------------------------------------------------------------
def jf_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers.get("X-Emby-Token") == JF_TOKEN
    path = request.url.path
    if path == "/Users/u1/Items" and "Ids" not in request.url.params:
        return httpx.Response(
            200,
            json={
                "TotalRecordCount": 2,
                "Items": [
                    {
                        "Id": "m1",
                        "Type": "Movie",
                        "Name": "Heat",
                        "ProductionYear": 1995,
                        "ProviderIds": {"Imdb": "tt0113277", "Tmdb": "949"},
                    },
                    {
                        "Id": "s1",
                        "Type": "Series",
                        "Name": "Show",
                        "ProviderIds": {"Tmdb": "1399"},
                    },
                ],
            },
        )
    if path == "/Users/u1/Items":
        return httpx.Response(
            200,
            json={
                "Items": [
                    {
                        "Id": "m1",
                        "RunTimeTicks": 60_000_000_000,
                        "MediaSources": [
                            {
                                "Id": "ms1",
                                "Container": "mkv,webm",
                                "Size": 5,
                                "Bitrate": 8_000_000,
                                "Path": "/media/Heat.mkv",
                                "DefaultAudioStreamIndex": 1,
                                "MediaStreams": [
                                    {
                                        "Type": "Video",
                                        "Codec": "h264",
                                        "Width": 1920,
                                        "Height": 1080,
                                    },
                                    {
                                        "Type": "Audio",
                                        "Index": 1,
                                        "Codec": "ac3",
                                        "Channels": 6,
                                        "Language": "eng",
                                    },
                                    {
                                        "Type": "Subtitle",
                                        "Index": 2,
                                        "Language": "eng",
                                        "IsExternal": True,
                                        "DisplayTitle": "English",
                                    },
                                ],
                            }
                        ],
                    }
                ]
            },
        )
    if path == "/Shows/s1/Episodes":
        assert request.url.params["Season"] == "2"
        return httpx.Response(
            200,
            json={
                "Items": [
                    {
                        "Id": "e5",
                        "IndexNumber": 5,
                        "MediaSources": [
                            {
                                "Id": "ms5",
                                "Container": "mp4",
                                "MediaStreams": [
                                    {
                                        "Type": "Video",
                                        "Codec": "hevc",
                                        "Height": 2160,
                                        "VideoRangeType": "HDR10",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            },
        )
    return httpx.Response(404)


@pytest.fixture
def jellyfin(monkeypatch):
    provider = JellyfinProvider()
    provider.save_config(
        {
            "enabled": True,
            "server_url": "http://jf.lan:8096",
            "token": JF_TOKEN,
            "user_id": "u1",
            "device_id": "dev",
            "server_id": "jf1",
            "server_name": "JF",
        }
    )
    monkeypatch.setattr("app.sources.jellyfin.http_client", lambda: _client(jf_handler))
    yield provider
    provider.save_config(None)


@pytest.mark.asyncio
async def test_jellyfin_sync_and_resolve(jellyfin):
    assert (await jellyfin.sync())["titles"] == 2
    movie = await jellyfin.resolve(
        MediaRef(type="movie", imdb_id="tt0113277"), PlaybackContext(user_key="k")
    )
    assert [s.variant for s in movie] == ["original", "optimized", "optimized"]
    assert (
        movie[0].container == "mkv"
        and movie[0].audio_codec == "ac3"
        and movie[0].duration_seconds == 6000
    )
    assert JF_TOKEN not in movie[0].model_dump_json()
    _, path = media_proxy.read_token(movie[0].url.rsplit("/", 1)[1])
    assert path == "/Videos/m1/stream?static=true&MediaSourceId=ms1"
    episode = await jellyfin.resolve(
        MediaRef(type="series", tmdb_id=1399, season=2, episode=5),
        PlaybackContext(user_key="k"),
    )
    assert episode[0].hdr == "hdr10" and episode[0].resolution == "2160p"


# --- signed media URLs -------------------------------------------------------------
def test_media_tokens_expire_and_reject_tampering():
    good = media_proxy.mint_path("plex", "/a").split("/")[-1]
    assert media_proxy.read_token(good) == ("plex", "/a")
    assert media_proxy.read_token(good + ".m3u8") == ("plex", "/a")
    with pytest.raises(HTTPException):
        media_proxy.read_token(good[:-4] + "AAAA")
    expired = media_proxy.mint_path("plex", "/a", ttl=-1).split("/")[-1]
    with pytest.raises(HTTPException):
        media_proxy.read_token(expired)


def test_playlist_rewrite_signs_children_and_strips_secrets():
    text = "\n".join(
        [
            "#EXTM3U",
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin?X-Plex-Token=t"',
            "#EXT-X-STREAM-INF:BANDWIDTH=1",
            "session/abc/index.m3u8?X-Plex-Token=t&x=1",
            "https://elsewhere.example/evil.ts",
        ]
    )
    out = media_proxy.rewrite_playlist(
        text,
        provider_id="plex",
        upstream_url="http://srv:32400/video/:/transcode/start.m3u8",
        server_base="http://srv:32400",
        strip_params=("X-Plex-Token",),
    )
    lines = out.strip().splitlines()
    assert (
        "X-Plex-Token" not in out and "elsewhere" not in out and "srv:32400" not in out
    )
    child = lines[3]
    assert child.startswith("/media/") and child.endswith(".m3u8")
    assert media_proxy.read_token(child.split("/")[-1]) == (
        "plex",
        "/video/:/transcode/session/abc/index.m3u8?x=1",
    )
    assert len(lines) == 4  # the off-server URI is blanked out


# --- registry --------------------------------------------------------------------
class _Addon(SourceProvider):
    id = "fake"
    label = "Fake"
    kind = "addon"
    searchable = True

    def __init__(self, result=None, sdk=1, error=None):
        self._result, self._error = result or [], error
        self.sdk_version = sdk

    def enabled(self):
        return True

    async def resolve(self, media, ctx):
        if self._error:
            raise self._error
        return self._result


def test_registry_rejects_wrong_sdk_and_library_claims(monkeypatch):
    class EP:
        def __init__(self, name, obj):
            self.name, self._obj = name, obj

        def load(self):
            return self._obj

    lib = _Addon()
    lib.kind = "library"
    lib.id = "sneaky"
    eps = [EP("old", _Addon(sdk=0)), EP("lib", lib), EP("ok", _Addon())]
    monkeypatch.setattr(registry, "entry_points", lambda group: eps)
    registry._rejected.clear()
    loaded = registry._load_addons({"plex", "jellyfin"})
    assert [p.id for p in loaded] == ["fake"]
    reasons = {r["entry_point"]: r["reason"] for r in registry._rejected}
    assert "SDK" in reasons["old"] and "addon" in reasons["lib"]


# --- resolver & availability ------------------------------------------------------
def _src(provider_id, kind, variant="original", resolution=None, n=0):
    return PlaybackSource(
        id=f"{provider_id}{n}{variant}",
        provider_id=provider_id,
        provider_label=provider_id,
        kind=kind,
        variant=variant,
        url="https://x/y",
        route_id=f"r{n}",
        resolution=resolution,
    )


class _Lib(_Addon):
    id = "lib"
    kind = "library"


@pytest.mark.asyncio
async def test_resolver_orders_library_first_and_tolerates_partial_failure():
    lib = _Lib(
        [
            _src("lib", "library", "optimized", "1080p", 1),
            _src("lib", "library", "original", "1080p", 2),
        ]
    )
    addon = _Addon([_src("fake:svc", "addon", n=3)])
    broken = _Addon(error=RuntimeError("boom"))
    broken.id = "broken"
    registry.set_providers_for_tests([addon, broken, lib])
    ctx = PlaybackContext(user_key="k", quality="original")
    sources = await resolver.resolve(
        MediaRef(type="movie", imdb_id="tt0113277", tmdb_id=949), ctx
    )
    assert [(s.provider_id, s.variant) for s in sources] == [
        ("lib", "original"),
        ("lib", "optimized"),
        ("fake:svc", "original"),
    ]
    assert ctx.diagnostics["status"] == "partial"


@pytest.mark.asyncio
async def test_resolver_raises_only_when_every_provider_fails():
    broken = _Addon(error=RuntimeError("boom"))
    registry.set_providers_for_tests([broken])
    with pytest.raises(resolver.ProvidersUnavailable):
        await resolver.resolve(
            MediaRef(type="movie", imdb_id="tt0113277", tmdb_id=949),
            PlaybackContext(user_key="k"),
        )
    registry.set_providers_for_tests([])
    assert (
        await resolver.resolve(
            MediaRef(type="movie", imdb_id="tt0113277", tmdb_id=1),
            PlaybackContext(user_key="k"),
        )
        == []
    )


@pytest.mark.asyncio
async def test_availability_marks_owned_titles(plex):
    await plex.sync()
    registry.set_providers_for_tests([plex])
    items = [
        {"id": "tt0113277", "type": "movie", "title": "Heat"},
        {"tmdb_id": 1, "type": "movie"},
        {"rows": [{"imdb_id": "tt0944947", "media_type": "tv"}]},
    ]
    with get_db() as db:
        availability.annotate_tree(db, items)
    assert items[0]["availability"] == {
        "playable": True,
        "in_library": True,
        "sources": [{"provider_id": "plex", "label": "Plex", "kind": "library"}],
    }
    assert items[1]["availability"]["playable"] is False
    assert items[2]["rows"][0]["availability"]["in_library"] is True
    registry.set_providers_for_tests([plex, _Addon()])
    with get_db() as db:
        index = availability.AvailabilityIndex.for_items(db, items[1:2])
    assert (
        index.for_item(items[1]).playable is True
    )  # a searchable addon makes it playable


def test_media_ref_validation():
    with pytest.raises(ValueError):
        MediaRef(type="series", imdb_id="tt0944947")
    with pytest.raises(ValueError):
        MediaRef(type="movie")
    with pytest.raises(ValueError):
        MediaRef(type="movie", imdb_id="nope")
    assert (
        MediaRef(type="series", imdb_id="tt0944947", season=1, episode=2).key()
        == "series:tt0944947:1:2"
    )
    assert time.time() > 0
