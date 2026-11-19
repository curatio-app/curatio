"""One title on both Plex and Jellyfin: shown once, playable from either."""

# Fixtures are imported from test_sources and taken as parameters (F811).
# ruff: noqa: E402, F811
import pytest

from tests.test_sources import (  # noqa: F401  (env + fixtures)
    _Addon,
    _Lib,
    _src,
    jellyfin,
    plex,
    tables,
)

from app.database import get_db
from app.sources import availability, registry, resolver
from app.sources.models import MediaRef, PlaybackContext


@pytest.mark.asyncio
async def test_title_on_both_servers_is_one_card_with_both_sources(plex, jellyfin):
    await plex.sync()
    await jellyfin.sync()
    registry.set_providers_for_tests([plex, jellyfin])

    # Home/catalog card: one item, both servers listed as owners.
    card = {"id": "tt0113277", "type": "movie", "title": "Heat"}
    with get_db() as db:
        availability.annotate_tree(db, [card])
    assert card["availability"]["in_library"] is True
    assert {s["provider_id"] for s in card["availability"]["sources"]} == {
        "plex",
        "jellyfin",
    }

    # Playback: one resolve returns sources from both servers.
    sources = await resolver.resolve(
        MediaRef(type="movie", imdb_id="tt0113277"), PlaybackContext(user_key="k")
    )
    providers = [s.provider_id for s in sources]
    assert set(providers) == {"plex", "jellyfin"}
    assert all(s.kind == "library" for s in sources)
    # Different servers never claim to be the same file or the same route,
    # so a client can fail over from one to the other.
    plex_files = {s.file_id for s in sources if s.provider_id == "plex"}
    jf_files = {s.file_id for s in sources if s.provider_id == "jellyfin"}
    assert plex_files and jf_files and not (plex_files & jf_files)
    assert len({s.route_id for s in sources}) == len(sources)
    # The 4K Plex original comes first because it is the best copy.
    assert (sources[0].provider_id, sources[0].resolution) == ("plex", "2160p")


@pytest.mark.asyncio
async def test_best_copy_wins_across_servers_and_addons_stay_last():
    plex_lib = _Lib([_src("plex", "library", "original", "1080p", 1)])
    plex_lib.id = "plex"
    jf_lib = _Lib([_src("jellyfin", "library", "original", "2160p", 2)])
    jf_lib.id = "jellyfin"
    addon = _Addon([_src("fake:svc", "addon", resolution="2160p", n=3)])
    # Plex is registered first, but Jellyfin holds the 4K copy.
    registry.set_providers_for_tests([plex_lib, jf_lib, addon])
    sources = await resolver.resolve(
        MediaRef(type="movie", imdb_id="tt0113277", tmdb_id=949),
        PlaybackContext(user_key="k", quality="original"),
    )
    assert [(s.provider_id, s.resolution) for s in sources] == [
        ("jellyfin", "2160p"),
        ("plex", "1080p"),
        ("fake:svc", "2160p"),
    ]


@pytest.mark.asyncio
async def test_equal_copies_keep_server_order():
    plex_lib = _Lib([_src("plex", "library", "original", "1080p", 1)])
    plex_lib.id = "plex"
    jf_lib = _Lib([_src("jellyfin", "library", "original", "1080p", 2)])
    jf_lib.id = "jellyfin"
    registry.set_providers_for_tests([plex_lib, jf_lib])
    sources = await resolver.resolve(
        MediaRef(type="movie", imdb_id="tt0113277", tmdb_id=949),
        PlaybackContext(user_key="k", quality="original"),
    )
    assert [s.provider_id for s in sources] == ["plex", "jellyfin"]
