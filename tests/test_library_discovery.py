"""Discovery for your library: server watch history, library-only tagging, the
tag pack, rows picked from owned titles, "Not for me" and local AI models."""

# ruff: noqa: E402, F811
import gzip
import io
import json
from datetime import datetime, timedelta

import httpx
import pytest

from tests.test_sources import (  # noqa: F401  (env + fixtures)
    PLEX_TOKEN,
    _client,
    tables,
)

from app import library_catalog, owned_rows, server_history, tag_pack
from app.config import settings
from app.database import get_db
from app.models import (
    Account,
    AdminSetting,
    HiddenTitle,
    LibraryItem,
    MediaAvailability,
    MediaMetadata,
    MovieTag,
    Tag,
    User,
    UserTasteVector,
    WatchEvent,
)
from app.sources import registry
from app.sources.jellyfin import _watch_state
from app.sources.library_base import counts_as_watched
from app.sources.plex import PlexProvider
from app.time_utils import utc_now


# --- fixtures ---------------------------------------------------------------------
def _reset(db):
    for model in (
        HiddenTitle,
        WatchEvent,
        LibraryItem,
        UserTasteVector,
        MovieTag,
        MediaAvailability,
        MediaMetadata,
    ):
        db.query(model).delete()
    db.query(Tag).filter(Tag.name.like("Test %")).delete(synchronize_session=False)
    db.query(User).filter(User.user_key.like("disc-%")).delete(
        synchronize_session=False
    )
    db.query(Account).filter(Account.email.like("%@disc.test")).delete(
        synchronize_session=False
    )
    db.query(AdminSetting).filter(
        AdminSetting.key.in_(("tag_pack_version", "library_tagging_spend"))
    ).delete(synchronize_session=False)
    db.commit()


@pytest.fixture
def clean():
    with get_db() as db:
        _reset(db)
    yield
    with get_db() as db:
        _reset(db)


def _profiles(db, *, admin=True):
    """An admin account with two profiles; returns (owner, second)."""
    account = Account(email="owner@disc.test", password_hash="x", is_admin=admin)
    db.add(account)
    db.flush()
    owner = User(
        user_key="disc-owner",
        account_id=account.id,
        is_active=True,
        created_at=utc_now() - timedelta(days=2),
    )
    second = User(user_key="disc-second", account_id=account.id, is_active=True)
    db.add_all([owner, second])
    db.commit()
    return owner, second


def _own(db, media_type, tmdb_id, **extra):
    db.add(
        MediaAvailability(
            provider="plex",
            server_id="srv",
            item_key=f"{media_type}-{tmdb_id}",
            media_type=media_type,
            tmdb_id=tmdb_id,
            **extra,
        )
    )


# --- 1. server watch history ---------------------------------------------------------
def _plex_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers.get("X-Plex-Token") == PLEX_TOKEN
    path = request.url.path
    if path == "/library/sections":
        return httpx.Response(
            200,
            json={"MediaContainer": {"Directory": [{"key": "1", "type": "movie"}]}},
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
                            "viewCount": 2,
                            "lastViewedAt": 1_700_000_000,
                            "Guid": [{"id": "tmdb://949"}],
                        },
                        {
                            "ratingKey": "101",
                            "title": "Alien",
                            "Guid": [{"id": "tmdb://348"}],
                        },
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
            "client_id": "cid",
        }
    )
    monkeypatch.setattr("app.sources.plex.http_client", lambda: _client(_plex_handler))

    async def nothing():
        return None

    monkeypatch.setattr(library_catalog, "prepare_owned_metadata", nothing)
    monkeypatch.setattr(library_catalog, "queue_owned_tagging", nothing)
    monkeypatch.setattr(library_catalog, "invalidate_home", lambda: None)
    monkeypatch.setattr(server_history, "_schedule_rebuilds", lambda changed: None)
    registry.set_providers_for_tests([provider])
    yield provider
    provider.save_config(None)


@pytest.mark.asyncio
async def test_plex_sync_imports_history_for_the_owner_profile_only(plex, clean):
    with get_db() as db:
        owner, second = _profiles(db)
        owner_id, second_id = owner.id, second.id
    await plex.sync()
    with get_db() as db:
        heat = db.query(MediaAvailability).filter_by(item_key="100").one()
        alien = db.query(MediaAvailability).filter_by(item_key="101").one()
        assert (heat.play_count, alien.play_count) == (2, 0)
        assert heat.watched_at == datetime(2023, 11, 14, 22, 13, 20)
        events = db.query(WatchEvent).filter_by(action="server_watched").all()
        assert [(e.user_id, e.tmdb_id, e.media_type) for e in events] == [
            (owner_id, 949, "movie")
        ]
        assert events[0].created_at == heat.watched_at
        assert second_id not in {e.user_id for e in events}
    # A second sync adds nothing (idempotent).
    await plex.sync()
    assert server_history.import_all() == {}
    with get_db() as db:
        assert db.query(WatchEvent).filter_by(action="server_watched").count() == 1


def test_profile_setting_turns_history_on_and_off(clean, monkeypatch):
    monkeypatch.setattr(server_history, "_schedule_rebuilds", lambda changed: None)
    with get_db() as db:
        owner, second = _profiles(db)
        _own(db, "movie", 949, play_count=1)
        db.commit()
        assert server_history.uses_server_history(db, owner)
        assert not server_history.uses_server_history(db, second)
        second.server_history = "on"
        owner.server_history = "off"
        db.commit()
        assert [p.user_key for p in server_history.history_profiles(db)] == [
            "disc-second"
        ]
    assert list(server_history.import_all().values()) == [1]
    with get_db() as db:
        second = db.query(User).filter_by(user_key="disc-second").one()
        assert server_history.forget_for(db, second.id) == 1
        db.commit()


def test_non_admin_accounts_never_get_history_automatically(clean):
    with get_db() as db:
        owner, _ = _profiles(db, admin=False)
        assert not server_history.uses_server_history(db, owner)


def test_watch_state_rules():
    assert counts_as_watched("movie", 1)
    assert not counts_as_watched("series", 2)  # two episodes is a sample
    assert counts_as_watched("series", 3)
    assert _watch_state({"Type": "Movie"}) == (None, None)  # no user data
    assert _watch_state({"Type": "Movie", "UserData": {"Played": True}})[0] == 1
    assert _watch_state({"Type": "Series", "UserData": {"Played": True}})[0] == 3
    assert (
        _watch_state({"Type": "Series", "UserData": {"PlayedPercentage": 20}})[0] == 1
    )


def test_server_history_shapes_taste_and_seeds(clean):
    from app import relevance
    from workers.personalized_sync import SEED_ACTIONS

    assert "server_watched" in SEED_ACTIONS
    assert (
        relevance._EVENT_WEIGHTS["server_watched"]
        > relevance._EVENT_WEIGHTS["imported"]
    )
    with get_db() as db:
        owner, _ = _profiles(db)
        db.add(
            WatchEvent(
                user_id=owner.id,
                tmdb_id=949,
                media_type="movie",
                action="server_watched",
            )
        )
        db.commit()
        assert relevance.completion_weights(db, owner.id, "movie") == {949: 0.9}


# --- 2. library-only tagging ---------------------------------------------------------
def _meta(db, media_type, tmdb_id, **extra):
    fields = {"title": f"T{tmdb_id}", "vote_count": 1000, "vote_average": 7.5}
    fields.update(extra)
    db.add(MediaMetadata(tmdb_id=tmdb_id, media_type=media_type, **fields))


def _tag(db, name):
    tag = Tag(name=name, category="mood")
    db.add(tag)
    db.flush()
    return tag


def test_only_owned_and_watchlist_titles_are_queued(clean):
    from workers import library_tagging
    from workers.tagging_common import tagger_version

    with get_db() as db:
        owner, _ = _profiles(db)
        _own(db, "movie", 1)  # owned, untagged -> new
        _own(db, "movie", 2)  # owned, rule-tagged only -> upgrade with AI
        _own(db, "series", 3)  # owned show -> "tv"
        _own(db, "movie", 4)  # owned, pack-tagged -> skipped
        db.add(LibraryItem(user_id=owner.id, tmdb_id=5, media_type="movie"))
        tag = _tag(db, "Test Mood")
        version = tagger_version()
        db.add_all(
            [
                MovieTag(
                    tmdb_id=2,
                    media_type="movie",
                    tag_id=tag.id,
                    confidence=0.8,
                    tagger_version=version,
                    source="derived",
                ),
                MovieTag(
                    tmdb_id=4,
                    media_type="movie",
                    tag_id=tag.id,
                    confidence=0.8,
                    tagger_version="older",
                    source="pack",
                ),
                # Tagged but nobody owns or saved it: never queued.
                MovieTag(
                    tmdb_id=99,
                    media_type="movie",
                    tag_id=tag.id,
                    confidence=0.8,
                    tagger_version="older",
                    source="derived",
                ),
            ]
        )
        db.commit()
        todo = library_tagging.pending(db, ai_ready=True)
        assert todo["movie"] == {"new": [1, 5], "upgrade": [2]}
        assert todo["tv"] == {"new": [3], "upgrade": []}
        assert library_tagging.pending(db, ai_ready=False)["movie"]["upgrade"] == []


def test_catalog_tagging_beyond_library_is_off_by_default():
    assert settings.tag_catalog_beyond_library is False
    assert settings.library_tagging_monthly_cap_usd == 0.0


@pytest.mark.asyncio
async def test_no_key_uses_rule_tags_and_the_cap_stops_ai(clean, monkeypatch):
    from workers import library_tagging

    calls = []

    async def fake_tag(db, media_type, ids, *, deterministic_only, replace_all):
        calls.append((media_type, tuple(ids), deterministic_only, replace_all))
        cost = 0.0 if deterministic_only else 0.5  # rule tags are free
        return {"processed": len(ids), "tags_written": len(ids), "cost_usd": cost}

    monkeypatch.setattr(library_tagging, "_tag", fake_tag)
    monkeypatch.setattr(library_tagging, "_refresh_relevance", lambda: None)
    with get_db() as db:
        _own(db, "movie", 1)
        _own(db, "movie", 2)
        db.commit()
    result = await library_tagging._run(400, ai_ready=False)
    assert result["mode"] == "rules"
    assert calls == [("movie", (1, 2), True, False)]

    # With a key and a $0.40 cap: the first batch spends $0.50 and the month is
    # capped; the next run uses rules only.
    calls.clear()
    monkeypatch.setattr(settings, "library_tagging_monthly_cap_usd", 0.4)
    result = await library_tagging._run(400, ai_ready=True)
    assert calls[0][2] is False and result["capped"] is True
    calls.clear()
    result = await library_tagging._run(400, ai_ready=True)
    assert all(call[2] is True for call in calls) and result["mode"] == "rules"


# --- 3. tag pack -----------------------------------------------------------------------
def _seed_pack_source(db):
    heist, slow = _tag(db, "Test Heist"), _tag(db, "Test Slow-Burn")
    _meta(db, "movie", 949, imdb_id="tt0113277", vote_count=9000)
    _meta(db, "movie", 7, vote_count=3)  # too obscure for the pack
    for tmdb_id, tag, confidence in ((949, heist, 0.93), (949, slow, 0.71)):
        db.add(
            MovieTag(
                tmdb_id=tmdb_id,
                media_type="movie",
                tag_id=tag.id,
                confidence=confidence,
                source="gemini",
                tagger_version="v-test",
            )
        )
    db.add(
        MovieTag(
            tmdb_id=7,
            media_type="movie",
            tag_id=heist.id,
            confidence=0.9,
            source="gemini",
        )
    )
    db.commit()


def test_export_holds_only_ids_and_tags(clean):
    with get_db() as db:
        _seed_pack_source(db)
        path, manifest = tag_pack.export_pack(db, min_votes=50)
    with gzip.open(path, "rt") as handle:
        lines = [json.loads(line) for line in handle]
    assert lines[0]["curatio_tag_pack"] == 1 and lines[0]["titles"] == 1
    assert lines[0]["license"] == "CC0-1.0"
    assert lines[1] == {
        "tmdb_id": 949,
        "media_type": "movie",
        "imdb_id": "tt0113277",
        "tags": [["Test Heist", 0.93], ["Test Slow-Burn", 0.71]],
    }
    assert manifest["sha256"] and manifest["titles"] == 1


def test_export_counts_legacy_untagged_source_rows_and_skips_retired_tags(clean):
    with get_db() as db:
        live, retired = _tag(db, "Test Live"), _tag(db, "Test Retired")
        retired.is_active = False
        _meta(db, "movie", 62, vote_count=500)
        for tag in (live, retired):
            db.add(
                MovieTag(
                    tmdb_id=62,
                    media_type="movie",
                    tag_id=tag.id,
                    confidence=0.8,
                    source=None,  # tagged before the column existed
                )
            )
        db.commit()
        path, manifest = tag_pack.export_pack(db, min_votes=50)
    with gzip.open(path, "rt") as handle:
        lines = [json.loads(line) for line in handle]
    assert manifest["titles"] == 1
    assert lines[1]["tags"] == [["Test Live", 0.8]]


def _pack(*items, header=None):
    head = {
        "curatio_tag_pack": 1,
        "version": "2026.11.15.1200",
        "tagger_version": "pack-v1",
        "taxonomy_version": "5.1.0",
        "titles": len(items),
    }
    head.update(header or {})
    body = "\n".join(json.dumps(x) for x in (head, *items)) + "\n"
    return gzip.compress(body.encode())


def test_import_replaces_rule_tags_but_keeps_local_ai_tags(clean):
    with get_db() as db:
        heist = _tag(db, "Test Heist")
        db.add_all(
            [
                MovieTag(
                    tmdb_id=1,
                    media_type="movie",
                    tag_id=heist.id,
                    confidence=0.2,
                    source="derived",
                ),
                MovieTag(
                    tmdb_id=2,
                    media_type="movie",
                    tag_id=heist.id,
                    confidence=0.6,
                    source="openai",
                ),
            ]
        )
        db.commit()
        data = _pack(
            {"tmdb_id": 1, "media_type": "movie", "tags": [["Test Heist", 0.9]]},
            {"tmdb_id": 2, "media_type": "movie", "tags": [["Test Heist", 0.9]]},
            {"tmdb_id": 3, "media_type": "tv", "tags": [["Unknown Tag", 0.9]]},
        )
        result = tag_pack.import_pack(db, io.BytesIO(data))
        assert result["stored"] == 2 and result["kept_local_ai"] == 1
        assert result["unknown_tags"] == 1
        one = db.query(MovieTag).filter_by(tmdb_id=1).one()
        two = db.query(MovieTag).filter_by(tmdb_id=2).one()
        assert (one.source, one.confidence) == ("pack", 0.9)
        assert (two.source, two.confidence) == ("openai", 0.6)
        assert tag_pack.installed_version(db) == "2026.11.15.1200"
        # Importing the same pack again changes nothing.
        tag_pack.import_pack(db, io.BytesIO(data))
        assert db.query(MovieTag).filter_by(tmdb_id=1).count() == 1


@pytest.mark.parametrize(
    "data, message",
    [
        (b"not gzip", "gzip"),
        (_pack(header={"curatio_tag_pack": 2}), "unsupported"),
        (_pack(header={"overview": "x"}), "Unexpected header"),
        (
            _pack({"tmdb_id": 1, "media_type": "movie", "title": "Heat", "tags": []}),
            "fields other than ids and tags",
        ),
        (_pack({"tmdb_id": -1, "media_type": "movie", "tags": [["A", 1]]}), "tmdb_id"),
        (
            _pack({"tmdb_id": 1, "media_type": "movie", "tags": [["A", 2]]}),
            "tag entry",
        ),
    ],
)
def test_import_rejects_anything_but_a_clean_pack(clean, data, message):
    with get_db() as db, pytest.raises(tag_pack.TagPackError, match=message):
        tag_pack.import_pack(db, io.BytesIO(data))


def test_checksum_mismatch_aborts(clean):
    data = _pack({"tmdb_id": 1, "media_type": "movie", "tags": [["A", 0.5]]})
    with get_db() as db, pytest.raises(tag_pack.TagPackError, match="Checksum"):
        tag_pack.import_pack(db, io.BytesIO(data), expected_sha256="0" * 64)


# --- 4. rows picked from owned titles --------------------------------------------------
def _library(db, owner, count=12):
    """``count`` owned movies sharing a tag the profile likes, plus 1 unowned."""
    tag = _tag(db, "Test Slow-Burn")
    db.add(
        UserTasteVector(user_id=owner.id, media_type="movie", tag_id=tag.id, weight=1)
    )
    for tmdb_id in range(1, count + 1):
        _own(db, "movie", tmdb_id)
        _meta(db, "movie", tmdb_id, vote_count=100 * tmdb_id)
        db.add(
            MovieTag(
                tmdb_id=tmdb_id,
                media_type="movie",
                tag_id=tag.id,
                confidence=0.9,
                weight_norm=0.1 * tmdb_id,
            )
        )
    _meta(db, "movie", 500)
    db.add(
        MovieTag(
            tmdb_id=500, media_type="movie", tag_id=tag.id, confidence=1, weight_norm=5
        )
    )
    db.commit()
    return tag


def test_my_servers_rows_come_from_owned_titles_only(plex, clean):
    with get_db() as db:
        owner, _ = _profiles(db)
        _library(db, owner)
        db.add(
            WatchEvent(
                user_id=owner.id, tmdb_id=12, media_type="movie", action="complete"
            )
        )
        db.add(HiddenTitle(user_id=owner.id, tmdb_id=11, media_type="movie"))
        db.commit()
        owned = library_catalog.owned_entries(db)
        rows = owned_rows.build(db, owner, owned, "my_servers")
        ids = {row["id"] for row in rows}
        assert {"servers-picks-movie", owned_rows.UNWATCHED_ROW} <= ids
        for row in rows:
            members = {item["tmdb_id"] for item in row["items"]}
            assert 500 not in members  # not owned
            assert 11 not in members  # "Not for me"
            if row["id"] != owned_rows.REWATCH_ROW:
                assert 12 not in members  # already watched
        picks = next(r for r in rows if r["id"] == "servers-picks-movie")
        # Strongest taste match first, each with a reason.
        assert picks["items"][0]["tmdb_id"] == 10
        assert picks["items"][0]["reason"] == "Test Slow-Burn"
        unwatched = next(r for r in rows if r["id"] == owned_rows.UNWATCHED_ROW)
        assert unwatched["items"][0]["reason"] == "On your server · unwatched"


def test_everything_scope_adds_only_two_owned_rows(plex, clean):
    with get_db() as db:
        owner, _ = _profiles(db)
        _library(db, owner)
        rows = owned_rows.build(
            db, owner, library_catalog.owned_entries(db), "everything"
        )
        assert [row["id"] for row in rows] == [
            "servers-picks-movie",
            owned_rows.UNWATCHED_ROW,
        ]


def test_small_libraries_get_no_half_empty_rows(plex, clean):
    with get_db() as db:
        owner, _ = _profiles(db)
        _library(db, owner, count=owned_rows.OWNED_ROW_MIN_ITEMS - 1)
        rows = owned_rows.build(
            db, owner, library_catalog.owned_entries(db), "my_servers"
        )
        assert rows == []


def test_rewatch_row_uses_old_watches(plex, clean):
    with get_db() as db:
        owner, _ = _profiles(db)
        _library(db, owner)
        long_ago = utc_now() - timedelta(days=4 * 365)
        for tmdb_id in range(1, 10):
            db.add(
                WatchEvent(
                    user_id=owner.id,
                    tmdb_id=tmdb_id,
                    media_type="movie",
                    action="server_watched",
                    created_at=long_ago,
                )
            )
        db.commit()
        rows = owned_rows.build(
            db, owner, library_catalog.owned_entries(db), "my_servers"
        )
        rewatch = next(r for r in rows if r["id"] == owned_rows.REWATCH_ROW)
        assert rewatch["items"][0]["reason"] == f"Last watched {long_ago.year}"


def test_home_snapshot_keeps_reasons(clean):
    from app import home_feed

    row = {
        "id": "servers-unwatched",
        "title": "Unwatched",
        "type": "mixed",
        "members": [["movie", 1], ["movie", 2]],
        "reasons": {"movie:1": "On your server · unwatched"},
    }
    cards = {("movie", 1): {"id": "1"}, ("movie", 2): {"id": "2"}}
    page = home_feed._page(row, 0, 10, cards)
    assert page["items"][0]["reason"] == "On your server · unwatched"
    assert "reason" not in page["items"][1]


# --- 5. "Not for me" -------------------------------------------------------------------
def test_hide_and_unhide_through_the_api(clean, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "_schedule_local_sync", lambda user_id: None)
    with get_db() as db:
        owner, _ = _profiles(db)
        _meta(db, "movie", 949, title="Heat")
        db.commit()
        key, owner_id = owner.user_key, owner.id
    request = main.HiddenTitleRequest(tmdb_id=949, media_type="movie")
    assert main.hide_title(key, request)["changed"] is True
    assert main.hide_title(key, request)["changed"] is False
    listed = main.list_hidden_titles(key)["items"]
    assert [(i["tmdb_id"], i["title"]) for i in listed] == [(949, "Heat")]
    from app import relevance

    with get_db() as db:
        assert relevance.completion_weights(db, owner_id, "movie") == {
            949: relevance.HIDDEN_WEIGHT
        }
        assert owned_rows.hidden_keys(db, owner_id) == {("movie", 949)}
    assert main.unhide_title(key, "movie", 949)["changed"] is True
    assert main.list_hidden_titles(key)["items"] == []


def test_hidden_titles_join_the_taste_exclusions(clean, monkeypatch):
    from app import main

    with get_db() as db:
        owner, _ = _profiles(db)
        db.add(HiddenTitle(user_id=owner.id, tmdb_id=949, media_type="movie"))
        db.commit()
        main._taste_cache.pop(owner.id, None)
        _, excluded = main._get_user_taste_data(owner.id, db)
    assert 949 in excluded


# --- 6. local models --------------------------------------------------------------------
def test_local_provider_takes_any_model_and_costs_nothing(monkeypatch):
    from app import tagging_provider
    from workers.tagging_common import pricing_for_model

    assert tagging_provider.validate_model("local", "qwen2.5:14b") == "qwen2.5:14b"
    with pytest.raises(ValueError):
        tagging_provider.validate_model("local", "bad model; rm")
    monkeypatch.setattr(settings, "local_base_url", "")
    monkeypatch.setattr(settings, "local_model", "qwen2.5:14b")
    assert not tagging_provider.provider_ready("local")
    monkeypatch.setattr(settings, "local_base_url", "http://localhost:11434/v1/")
    assert tagging_provider.provider_ready("local")  # no key needed
    assert pricing_for_model("qwen2.5:14b", "local")[:2] == (0.0, 0.0)
    assert "local" in tag_pack.AI_SOURCES


@pytest.mark.asyncio
async def test_local_engine_speaks_chat_completions(monkeypatch):
    from app.external_tagging_client import LocalTaggingEngine

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"items": []}'}}]}
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(
        "app.external_tagging_client.httpx.AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler)),
    )
    engine = LocalTaggingEngine(model_name="qwen", base_url="http://box:11434/v1")
    payload = await engine._post("tag these", allowed_tags=["Heist"])
    usage = engine._extract_usage(payload)
    assert engine._response_text(payload, usage) == '{"items": []}'
    assert seen["url"] == "http://box:11434/v1/chat/completions"
    assert seen["body"]["response_format"]["type"] == "json_schema"
    assert seen["auth"] is None  # no key, no header
    assert usage["prompt_token_count"] == 0


# --- retrying titles that only got rule-based tags ------------------------------------
def _seed_rule_only(db):
    tag = _tag(db, "Test Genre")
    rows = [
        # (tmdb_id, votes, overview, source)
        (603, 28000, "A hacker learns the truth.", "derived"),  # retried first
        (550, 32000, "An insomniac starts a club.", "derived"),
        (7, 3, "Obscure.", "derived"),  # under the vote floor
        (8, 900, "", "derived"),  # no synopsis: the model has nothing to read
        (9, 900, "Already AI-tagged.", "gemini"),
    ]
    for tmdb_id, votes, overview, source in rows:
        _meta(db, "movie", tmdb_id, vote_count=votes, overview=overview)
        db.add(
            MovieTag(
                tmdb_id=tmdb_id,
                media_type="movie",
                tag_id=tag.id,
                confidence=0.5,
                source=source,
            )
        )
    db.commit()


def test_rule_only_titles_are_found_most_popular_first(clean):
    from workers import rule_only_retry

    with get_db() as db:
        _seed_rule_only(db)
        assert rule_only_retry.rule_only_titles(db, 5) == [
            ("movie", 550),
            ("movie", 603),
        ]
        assert rule_only_retry.count_rule_only(db, 5) == {"movie": 2, "tv": 0}


@pytest.mark.asyncio
async def test_retry_replaces_rule_tags_and_reports_progress(clean, monkeypatch):
    from app import tagging_provider
    from workers import rule_only_retry, tagging_common

    calls = []

    async def fake_tag_and_store(db, features, **kwargs):
        calls.append(([f["tmdb_id"] for f in features], kwargs))
        heist = db.query(Tag).filter_by(name="Test Genre").one()
        for feature in features:
            if feature["tmdb_id"] == 550:  # the model answers for this one only
                db.query(MovieTag).filter_by(tmdb_id=550).delete()
                db.add(
                    MovieTag(
                        tmdb_id=550,
                        media_type="movie",
                        tag_id=heist.id,
                        confidence=0.9,
                        source="gemini",
                    )
                )
        db.commit()
        return {"processed": len(features), "cost_usd": 0.01}

    monkeypatch.setattr(tagging_common, "tag_and_store", fake_tag_and_store)
    monkeypatch.setattr(tagging_provider, "provider_ready", lambda *a: True)
    monkeypatch.setattr(rule_only_retry, "_refresh_relevance", lambda: None)
    with get_db() as db:
        _seed_rule_only(db)
    result = await rule_only_retry.retry_rule_only(min_votes=5)
    assert calls and calls[0][1]["replace_all"] is True
    assert sorted(calls[0][0]) == [550, 603]
    assert (result["total"], result["ai_tagged"], result["still_rule_only"]) == (
        2,
        1,
        1,
    )
    assert result["cost_usd"] == pytest.approx(0.01)
    assert rule_only_retry.status["running"] is False


@pytest.mark.asyncio
async def test_retry_needs_an_ai_provider(monkeypatch):
    from app import tagging_provider
    from workers import rule_only_retry

    monkeypatch.setattr(tagging_provider, "provider_ready", lambda *a: False)
    assert (await rule_only_retry.retry_rule_only())["status"] == "no_ai_provider"


def test_local_provider_is_accepted_at_startup(monkeypatch):
    from app import config

    assert config.Settings.validate_tagging_provider("Local") == "local"
    monkeypatch.setattr(config.settings, "skip_api_validation", False)
    monkeypatch.setattr(config.settings, "tagging_provider", "local")
    monkeypatch.setattr(config.settings, "tmdb_api_key", "real-key")
    monkeypatch.setattr(config.settings, "master_password", "x" * 20)
    monkeypatch.setattr(config.settings, "secret_key", "s" * 40)
    assert config.validate_api_keys() is True
