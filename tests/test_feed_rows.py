# ruff: noqa: E402
"""The feed rows -- Trending Now, New Releases, Popular, Coming Soon.

These were the only rows in the product that were not a function of the viewer.
One TMDB fetch per sync, copied verbatim into every profile, served in TMDB's
own popularity order, with the taste re-rank structurally unable to reach them:
``get_catalog_content`` gated it on ``not user_id``, and the two serving call
sites computed ``taste_tags``/``exclude_ids`` and then handed them only to the
universal branch.

The operator's complaint was that they "feel disconnected". They were: the tag
layer, the IDF weights and the MDBList consensus applied to every other shelf
and to none of these.
"""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_feed_rows.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.catalog_generator import (  # noqa: E402
    CatalogGenerator,
    is_tmdb_sourced_slot,
)
from app.models import (  # noqa: E402
    Base,
    MediaMetadata,
    MovieTag,
    Tag,
    UniversalCatalogContent,
    UniversalCategory,
    User,
    UserCatalog,
    UserCatalogContent,
)

TEST_DATABASE_URL = "sqlite:///./test_feed_rows.db"
engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def _title(db, tmdb_id, *, media_type="movie", **kw):
    db.add(
        MediaMetadata(
            tmdb_id=tmdb_id,
            media_type=media_type,
            title=kw.get("title", f"T{tmdb_id}"),
            popularity=kw.get("popularity", 10.0),
            vote_average=kw.get("vote_average", 7.0),
            vote_count=kw.get("vote_count", 1000),
            release_date=kw.get("release_date", "2015-01-01"),
            rating_consensus=kw.get("rating_consensus"),
            digital_release_date=kw.get("digital_release_date"),
            imdb_id=kw.get("imdb_id"),
            tag_facts=kw.get("tag_facts"),
            mdb_facts=kw.get("mdb_facts"),
        )
    )


def _user(db, user_id=1):
    """Create the profile if it is not already there.

    Not optional bookkeeping: another test file registers a
    ``PRAGMA foreign_keys=ON`` listener on the Engine class, so a UserCatalog
    with a dangling user_id inserts fine when this file runs alone and raises
    IntegrityError in a full suite run. Valid data rather than pragma-dependent
    data.
    """
    existing = db.get(User, user_id)
    if existing is None:
        existing = User(id=user_id, user_key=f"key-{user_id}")
        db.add(existing)
        db.flush()
    return existing


def _feed_row(db, slot_id, tmdb_ids, *, user_id=1, media_type="movie"):
    """A personal catalog holding ids in TMDB's order, as the sync writes it."""
    _user(db, user_id)
    catalog = UserCatalog(
        user_id=user_id,
        slot_id=slot_id,
        name=slot_id,
        media_type=media_type,
        generation_method="test",
        is_active=True,
    )
    db.add(catalog)
    db.flush()
    for rank, tmdb_id in enumerate(tmdb_ids, start=1):
        db.add(
            UserCatalogContent(
                catalog_id=catalog.id,
                tmdb_id=tmdb_id,
                media_type=media_type,
                rank=rank,
                match_score=0.0,
            )
        )
    db.flush()
    return catalog


def _universal_row(db, category_id, tmdb_ids, *, media_type="movie"):
    db.add(
        UniversalCategory(
            id=category_id,
            name=category_id,
            tier=1,
            sort_order=1,
            media_type=media_type,
            tag_formula={"required": []},
            is_active=True,
        )
    )
    db.flush()
    for rank, tmdb_id in enumerate(tmdb_ids, start=1):
        db.add(
            UniversalCatalogContent(
                category_id=category_id,
                tmdb_id=tmdb_id,
                media_type=media_type,
                rank=rank,
                match_score=0.0,
            )
        )
    db.flush()


def _tag(db, tmdb_id, tag_id, weight_norm, media_type="movie"):
    # Same reason as _user: MovieTag.tag_id is a real FK, enforced only once
    # another file has switched the pragma on. Flush first -- the session is
    # autoflush=False, so a Tag the test itself just added is still pending and
    # invisible to db.get.
    db.flush()
    if db.get(Tag, tag_id) is None:
        db.add(Tag(id=tag_id, name=f"Tag{tag_id}", category="subgenre"))
        db.flush()
    db.add(
        MovieTag(
            tmdb_id=tmdb_id,
            tag_id=tag_id,
            media_type=media_type,
            confidence=0.9,
            weight_norm=weight_norm,
        )
    )


# ---------------------------------------------------------------------------
# Which slots the taste layer may touch
# ---------------------------------------------------------------------------
def test_only_the_tmdb_sourced_slots_are_feed_slots():
    """Every other personal row is already ordered by this same taste vector at
    build time -- re-ranking those a second time would destroy the tier shuffle
    they are deliberately given."""
    for slot in (
        "trending-movie",
        "new-releases-series",
        "popular-movie",
        "coming-soon-series",
    ):
        assert is_tmdb_sourced_slot(slot), slot
    for slot in (
        "byw-movie-1",
        "picks-series",
        "rec-movie",
        "taste-movie-1",
        "gems-series",
        "up-next",
        "discover-movie",
    ):
        assert not is_tmdb_sourced_slot(slot), slot


# ---------------------------------------------------------------------------
# Home-only mockbuster policy
# ---------------------------------------------------------------------------
def test_home_loaders_hide_mockbusters_and_backfill_both_catalog_types(db):
    from app.catalog_policy import KNOWN_HOME_MOCKBUSTER_TMDB_IDS

    asylum_odyssey = 1698863
    asylum_master = 1698856
    assert {asylum_odyssey, asylum_master} == KNOWN_HOME_MOCKBUSTER_TMDB_IDS
    studio_odyssey = 1084242
    studio_master = 454639
    tmdb_tagged = 700003
    mdb_tagged = 700004
    asylum_original = 700005
    backfill = 700006

    _title(db, asylum_odyssey, imdb_id="tt41605854")
    _title(db, asylum_master, imdb_id="tt41087705")
    _title(db, studio_odyssey, imdb_id="tt33764258")
    _title(db, studio_master, imdb_id="tt0427340")
    _title(db, tmdb_tagged, tag_facts={"keywords": ["mockbuster"]})
    _title(db, mdb_tagged, mdb_facts={"keywords": ["tmdb.mockbuster"]})
    _title(db, asylum_original, tag_facts={"studios": ["The Asylum"]})
    _title(db, backfill)
    ordered = [
        asylum_odyssey,
        studio_odyssey,
        tmdb_tagged,
        studio_master,
        asylum_master,
        mdb_tagged,
        asylum_original,
        backfill,
    ]
    _feed_row(db, "trending-movie", ordered)
    _universal_row(db, "universal-test", ordered)
    db.commit()

    generator = CatalogGenerator(db)
    expected = [studio_odyssey, studio_master, asylum_original, backfill]
    personal = generator.get_catalog_content("trending-movie", user_id=1)
    batched_personal = generator.get_catalog_contents_batch(
        ["trending-movie"], user_id=1
    )["trending-movie"]
    universal = generator.get_catalog_content("universal-test")
    batched_universal = generator.get_catalog_contents_batch(["universal-test"])[
        "universal-test"
    ]

    for items in (personal, batched_personal, universal, batched_universal):
        assert [item["tmdb_id"] for item in items] == expected


def test_known_movie_id_does_not_suppress_same_number_in_tv_namespace():
    from app.catalog_policy import is_home_mockbuster

    assert not is_home_mockbuster(None, tmdb_id=1698856, media_type="tv")


def test_movie_discovery_excludes_anime_and_mockbuster_keywords():
    from app.catalog_policy import (
        HOME_MOVIE_WITHOUT_KEYWORDS,
        TMDB_MOCKBUSTER_KEYWORD_ID,
    )

    assert HOME_MOVIE_WITHOUT_KEYWORDS.split("|") == [
        "210024",
        str(TMDB_MOCKBUSTER_KEYWORD_ID),
    ]


@pytest.mark.asyncio
async def test_trending_keyword_screen_drops_tagged_and_known_mockbusters(
    monkeypatch,
):
    import workers.personalized_sync as personalized_sync

    ordinary = 700010
    tagged = 700011
    known = 1698863
    get_keywords = AsyncMock(
        side_effect=lambda tmdb_id: {
            "id": tmdb_id,
            "keywords": (
                [{"id": 208879, "name": "mockbuster"}]
                if tmdb_id == tagged
                else [{"id": 818, "name": "based on novel or book"}]
            ),
        }
    )
    monkeypatch.setattr(
        personalized_sync.tmdb_client,
        "get_movie_keywords",
        get_keywords,
    )

    kept = await personalized_sync._filter_home_mockbusters([known, ordinary, tagged])

    assert kept == [ordinary]
    assert [call.args[0] for call in get_keywords.await_args_list] == [
        ordinary,
        tagged,
    ]


@pytest.mark.asyncio
async def test_trending_keyword_screen_fails_open_on_provider_error(monkeypatch):
    import workers.personalized_sync as personalized_sync

    monkeypatch.setattr(
        personalized_sync.tmdb_client,
        "get_movie_keywords",
        AsyncMock(side_effect=RuntimeError("TMDB unavailable")),
    )

    assert await personalized_sync._filter_home_mockbusters([700012]) == [700012]


# ---------------------------------------------------------------------------
# The re-rank itself
# ---------------------------------------------------------------------------
def test_a_feed_row_is_reordered_by_the_viewers_taste(db):
    """The whole complaint, as one assertion.

    Two profiles asking for the same slot must not get the same order once
    their taste differs. Before this, the row was one shared TMDB fetch copied
    verbatim into every profile.
    """
    db.add(Tag(id=1, name="Neo-Noir", category="subgenre"))
    db.add(Tag(id=2, name="Feel-Good", category="mood"))
    for tmdb_id in (10, 11, 12, 13, 14, 15):
        _title(db, tmdb_id)
    # 15 is last in TMDB's order and is the only Neo-Noir title.
    _tag(db, 15, 1, 1.0)
    _tag(db, 10, 2, 1.0)
    _feed_row(db, "trending-movie", [10, 11, 12, 13, 14, 15])
    db.commit()

    gen = CatalogGenerator(db)
    noir_fan = gen.get_catalog_content(
        "trending-movie", user_id=1, user_taste_tag_ids=[1]
    )
    feelgood_fan = gen.get_catalog_content(
        "trending-movie", user_id=1, user_taste_tag_ids=[2]
    )

    assert [i["tmdb_id"] for i in noir_fan] != [i["tmdb_id"] for i in feelgood_fan], (
        "two profiles got a byte-identical Trending Now -- which is exactly the "
        "state this change exists to end"
    )

    def position_of(items, tmdb_id):
        return next(n for n, i in enumerate(items) if i["tmdb_id"] == tmdb_id)

    assert position_of(noir_fan, 15) < position_of(feelgood_fan, 15), (
        "the only Neo-Noir title must sit higher for the profile that likes "
        "Neo-Noir than for the one that does not"
    )
    assert position_of(feelgood_fan, 10) < position_of(noir_fan, 10)


def test_the_feed_blend_cannot_drag_the_tail_to_the_front(db):
    """Blend, do not replace.

    "Trending Now" makes a claim about the world. A row whose top title is
    TMDB's 20th-most-trending because it happens to match your profile has
    quietly stopped being the row it says it is. The stated guarantee is that a
    title climbs at most _MAX_FEED_CLIMB places, anywhere in the row.
    """
    from app.catalog_generator import _MAX_FEED_CLIMB

    db.add(Tag(id=1, name="Neo-Noir", category="subgenre"))
    ids = list(range(100, 130))
    for tmdb_id in ids:
        _title(db, tmdb_id)
    _tag(db, ids[-1], 1, 1.0)  # the very last title is a perfect taste match
    _feed_row(db, "trending-movie", ids)
    db.commit()

    ordered = [
        i["tmdb_id"]
        for i in CatalogGenerator(db).get_catalog_content(
            "trending-movie", user_id=1, user_taste_tag_ids=[1]
        )
    ]

    moved = ordered.index(ids[-1])
    assert moved > 0, "a perfect match from the tail took the top slot"
    assert moved < len(ids) - 1, "it should still have moved up"
    assert moved >= len(ids) - 1 - _MAX_FEED_CLIMB, (
        f"climbed {len(ids) - 1 - moved} places, more than the "
        f"{_MAX_FEED_CLIMB} the design promises"
    )
    assert ordered[0] in ids[:3], "the head of the feed must stay near the head"


def test_the_climb_limit_holds_at_the_head_as_well_as_the_tail(db):
    """The reason position decays geometrically.

    Under a linear ramp the ratio between adjacent positions grows as you go
    down the row -- ~1.05 near the top, 2.0 at the tail of a 20-item row -- so
    a bounded multiplier reorders the head freely and the tail not at all.
    Exactly backwards: the head is what a viewer sees anyway, and a strong
    match buried deep is the one worth surfacing.
    """
    from app.catalog_generator import _MAX_FEED_CLIMB

    db.add(Tag(id=1, name="Neo-Noir", category="subgenre"))
    ids = list(range(200, 230))
    for tmdb_id in ids:
        _title(db, tmdb_id)
    _tag(db, ids[15], 1, 1.0)  # a match in the middle, not the tail
    _feed_row(db, "trending-movie", ids)
    db.commit()

    ordered = [
        i["tmdb_id"]
        for i in CatalogGenerator(db).get_catalog_content(
            "trending-movie", user_id=1, user_taste_tag_ids=[1]
        )
    ]

    climbed = 15 - ordered.index(ids[15])
    assert 0 < climbed <= _MAX_FEED_CLIMB, (
        f"a mid-row match climbed {climbed} places; the guarantee is the same "
        "fixed number wherever a title sits"
    )


def test_a_row_with_no_taste_matches_keeps_tmdbs_order(db):
    """The honest degradation: when we know nothing about the titles, TMDB's
    ordering is the best answer available, so it is left exactly alone."""
    db.add(Tag(id=1, name="Neo-Noir", category="subgenre"))
    ids = [30, 31, 32, 33]
    for tmdb_id in ids:
        _title(db, tmdb_id)
    _feed_row(db, "trending-movie", ids)
    db.commit()

    ordered = [
        i["tmdb_id"]
        for i in CatalogGenerator(db).get_catalog_content(
            "trending-movie", user_id=1, user_taste_tag_ids=[1]
        )
    ]
    assert ordered == ids


def test_a_non_feed_personal_row_is_never_reordered(db):
    """byw/picks/taste/gems are ordered by this same signal at build time and
    then tier-shuffled. Sorting them again here would undo the shuffle."""
    db.add(Tag(id=1, name="Neo-Noir", category="subgenre"))
    ids = [40, 41, 42]
    for tmdb_id in ids:
        _title(db, tmdb_id)
    _tag(db, 42, 1, 1.0)
    _feed_row(db, "byw-movie-1", ids)
    db.commit()

    ordered = [
        i["tmdb_id"]
        for i in CatalogGenerator(db).get_catalog_content(
            "byw-movie-1", user_id=1, user_taste_tag_ids=[1]
        )
    ]
    assert ordered == ids


def test_watched_titles_drop_out_of_a_feed_row(db):
    """exclude_ids never reached the personal branch, so a film you finished
    sat at the top of your home screen forever."""
    for tmdb_id in (50, 51, 52):
        _title(db, tmdb_id)
    _feed_row(db, "trending-movie", [50, 51, 52])
    db.commit()

    ordered = [
        i["tmdb_id"]
        for i in CatalogGenerator(db).get_catalog_content(
            "trending-movie", user_id=1, exclude_ids={50}
        )
    ]
    assert ordered == [51, 52]


# ---------------------------------------------------------------------------
# The rating the card shows
# ---------------------------------------------------------------------------
def test_the_card_shows_the_consensus_that_ranked_it(db):
    """Cards emitted vote_average -- labelled "imdbRating" -- while the row
    had been ordered by rating_consensus. The
    visible number disagreed with the ordering it was explaining."""
    _title(db, 60, vote_average=6.1, rating_consensus=8.4)
    _title(db, 61, vote_average=6.1)
    _feed_row(db, "trending-movie", [60, 61])
    db.commit()

    items = {
        i["tmdb_id"]: i
        for i in CatalogGenerator(db).get_catalog_content("trending-movie", user_id=1)
    }
    assert items[60]["rating"] == pytest.approx(8.4)
    assert items[60]["rating_source"] == "consensus"
    assert items[61]["rating"] == pytest.approx(6.1)
    assert items[61]["rating_source"] == "tmdb", "no consensus, so say so"


def test_the_private_scoring_keys_never_reach_a_client(db):
    _title(db, 70)
    _feed_row(db, "trending-movie", [70])
    db.commit()

    item = CatalogGenerator(db).get_catalog_content(
        "trending-movie", user_id=1, user_taste_tag_ids=[1]
    )[0]
    for key in ("_quality", "_taste_score", "_feed_score"):
        assert key not in item


# ---------------------------------------------------------------------------
# Coming Soon
# ---------------------------------------------------------------------------
def test_coming_soon_carries_the_dates_the_countdown_needs(db):
    """The chip could never fire on a shelf card: the web serializer dropped
    both date fields get_catalog_content had already SELECTed, so Coming Soon
    would have been a row of release dates showing no release dates."""
    _title(db, 80, digital_release_date="2030-01-15")
    _feed_row(db, "coming-soon-movie", [80])
    db.commit()

    item = CatalogGenerator(db).get_catalog_content("coming-soon-movie", user_id=1)[0]
    assert item["digital_release_date"] == "2030-01-15"


def test_coming_soon_is_never_shuffled(db):
    """Its order IS its content -- soonest first. Shuffling would destroy the
    only signal an unreleased title has."""
    from app.main import _shuffle_items

    items = [{"tmdb_id": i} for i in range(20)]
    for slot in ("personal-coming-soon-movie", "coming-soon-movie"):
        assert _shuffle_items(items, slot) == items, slot


# ---------------------------------------------------------------------------
# Row ordering and dedupe
# ---------------------------------------------------------------------------
def test_only_timely_feeds_are_in_the_personal_slot_contract():
    from workers.personalized_sync import VALID_SLOTS, get_slot_sort_order

    first_personal = get_slot_sort_order("byw-movie-1")
    for timely in (
        "trending-movie",
        "trending-series",
        "new-releases-movie",
        "new-releases-series",
    ):
        assert timely in VALID_SLOTS
        assert get_slot_sort_order(timely) < first_personal

    for retired in (
        "up-next",
        "coming-soon-movie",
        "popular-movie",
        "discover-movie",
        "discover-acclaimed",
    ):
        assert retired not in VALID_SLOTS
        assert get_slot_sort_order(retired) > first_personal


def test_popular_does_not_repeat_new_releases():
    """popular-movie is the same /discover query as new-releases-movie without
    the date bound, so it is a strict superset by construction and nothing
    deduped the two adjacent rows."""
    from workers.personalized_sync import _dedupe_common_slots

    result = {
        "trending-movie": [1, 2, 3],
        "new-releases-movie": [3, 4, 5],
        "popular-movie": [1, 4, 6],
        "popular-series": [1, 7],
    }
    _dedupe_common_slots(result)

    assert result["trending-movie"] == [1, 2, 3]
    assert result["new-releases-movie"] == [4, 5]
    assert result["popular-movie"] == [6]
    # Separate namespaces: a movie id must not suppress a series with the same
    # number. 1 is Trending as a movie and stays in Popular as a series.
    assert result["popular-series"] == [1, 7]


# ---------------------------------------------------------------------------
# Retiring the old default rows
# ---------------------------------------------------------------------------
def test_personal_sync_cleanup_keeps_timely_feeds_and_removes_legacy_defaults(db):
    from workers.personalized_sync import _cleanup_stale_catalogs

    user = _user(db)
    for slot in ("discover-movie", "discover-series", "discover-acclaimed"):
        _feed_row(db, slot, [1, 2])
    _feed_row(db, "trending-movie", [3])
    _feed_row(db, "rec-movie", [4, 5])
    db.commit()

    assert _cleanup_stale_catalogs(user.id, db) == 3
    db.commit()

    remaining = {c.slot_id for c in db.query(UserCatalog).all()}
    assert remaining == {"trending-movie", "rec-movie"}
    assert db.query(UserCatalogContent).count() == 3, "content must go too"


@pytest.mark.asyncio
async def test_zero_history_profile_gets_timely_feeds_without_legacy_discovery_rows(
    db, monkeypatch
):
    from workers.personalized_sync import sync_user_catalogs

    user = _user(db)
    monkeypatch.setattr("workers.personalized_sync._backfill_metadata", AsyncMock())
    common_data = {
        "trending-movie": [1],
        "trending-series": [2],
        "new-releases-movie": [3],
        "new-releases-series": [4],
    }

    count = await sync_user_catalogs(user, db, common_data=common_data)

    assert count == 4
    assert user.last_sync is not None
    assert {row.slot_id for row in db.query(UserCatalog).all()} == set(common_data)


@pytest.mark.asyncio
async def test_bulk_sync_fetches_timely_feeds_once_and_shares_them(db, monkeypatch):
    import workers.personalized_sync as personalized_sync

    first = _user(db, 1)
    second = _user(db, 2)
    db.commit()
    common_data = {
        "trending-movie": [1],
        "trending-series": [2],
        "new-releases-movie": [3],
        "new-releases-series": [4],
    }
    prefetch = AsyncMock(return_value=common_data)
    sync_user = AsyncMock(return_value=4)
    monkeypatch.setattr(personalized_sync, "_prefetch_common_catalog_data", prefetch)
    monkeypatch.setattr(personalized_sync, "sync_user_catalogs", sync_user)
    monkeypatch.setattr(personalized_sync.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(personalized_sync, "_common_data_cache", None)

    stats = await personalized_sync.sync_all_users(db)

    prefetch.assert_awaited_once_with(db)
    assert {call.args[0].id for call in sync_user.await_args_list} == {
        first.id,
        second.id,
    }
    assert all(
        call.kwargs["common_data"] is common_data for call in sync_user.await_args_list
    )
    assert stats == {"total": 2, "synced": 2, "failed": 0, "catalogs": 8}


@pytest.mark.asyncio
async def test_new_release_filter_rejects_interstellar_storefront_rerelease(
    db, monkeypatch
):
    """A new retailer listing must not turn a 2014 movie into a 2026 release."""
    import workers.personalized_sync as personalized_sync

    interstellar = 157336
    genuine_new_movie = 999001
    _title(db, interstellar, release_date="2014-11-07")
    _title(db, genuine_new_movie, release_date="2026-07-01")
    db.flush()

    release_payloads = {
        interstellar: {
            "results": [
                {
                    "iso_3166_1": "US",
                    "release_dates": [
                        {"type": 3, "release_date": "2014-11-07T00:00:00.000Z"},
                        {"type": 5, "release_date": "2015-03-31T00:00:00.000Z"},
                        {"type": 4, "release_date": "2026-08-03T00:00:00.000Z"},
                    ],
                },
                {
                    "iso_3166_1": "BE",
                    "release_dates": [
                        {"type": 4, "release_date": "2014-11-07T00:00:00.000Z"}
                    ],
                },
            ]
        },
        genuine_new_movie: {
            "results": [
                {
                    "iso_3166_1": "US",
                    "release_dates": [
                        {"type": 3, "release_date": "2026-07-01T00:00:00.000Z"},
                        {"type": 4, "release_date": "2026-08-03T00:00:00.000Z"},
                    ],
                }
            ]
        },
    }
    monkeypatch.setattr(
        personalized_sync.tmdb_client,
        "get_movie_release_dates",
        AsyncMock(side_effect=lambda tmdb_id: release_payloads[tmdb_id]),
    )

    kept = await personalized_sync._filter_home_released_movies(
        db,
        [interstellar, genuine_new_movie],
        digital_window=("2026-05-17", "2026-08-15"),
    )

    assert kept == [genuine_new_movie]
    stored = {
        row.tmdb_id: row.digital_release_date
        for row in db.query(MediaMetadata).filter(
            MediaMetadata.tmdb_id.in_([interstellar, genuine_new_movie])
        )
    }
    assert stored == {
        interstellar: "2014-11-07",
        genuine_new_movie: "2026-08-03",
    }


@pytest.mark.asyncio
async def test_release_filter_clears_an_unverifiable_stale_digital_date(
    db, monkeypatch
):
    import workers.personalized_sync as personalized_sync

    legacy_movie = 999002
    _title(
        db,
        legacy_movie,
        release_date="2010-05-01",
        digital_release_date="2026-08-03",
    )
    db.flush()
    monkeypatch.setattr(
        personalized_sync.tmdb_client,
        "get_movie_release_dates",
        AsyncMock(
            return_value={
                "results": [
                    {
                        "iso_3166_1": "US",
                        "release_dates": [
                            {
                                "type": 3,
                                "release_date": "2010-05-01T00:00:00.000Z",
                            },
                            {
                                "type": 4,
                                "release_date": "2026-08-03T00:00:00.000Z",
                            },
                        ],
                    }
                ]
            }
        ),
    )

    kept = await personalized_sync._filter_home_released_movies(
        db,
        [legacy_movie],
        digital_window=("2026-05-17", "2026-08-15"),
    )

    assert kept == []
    assert (
        db.query(MediaMetadata)
        .filter(MediaMetadata.tmdb_id == legacy_movie)
        .one()
        .digital_release_date
        is None
    )


# ---------------------------------------------------------------------------
# The movie/TV id-namespace collision
# ---------------------------------------------------------------------------
def test_a_watched_movie_does_not_suppress_a_series_with_the_same_id(db):
    """TMDB ids are per-type: 1396 is Breaking Bad as a series and an unrelated
    film as a movie. The taste rows filtered MovieTag.tmdb_id against a union
    of both watched sets with no media_type predicate."""
    from workers.personalized_sync import _typed_id_filter

    _title(db, 1396, media_type="movie", title="Some Film")
    _title(db, 1396, media_type="tv", title="Breaking Bad")
    _tag(db, 1396, 1, 1.0, media_type="movie")
    _tag(db, 1396, 1, 1.0, media_type="tv")
    db.commit()

    watched = _typed_id_filter({1396}, set())
    rows = db.query(MovieTag).filter(~watched).all()

    assert [(r.tmdb_id, r.media_type) for r in rows] == [
        (1396, "tv")
    ], "the series was suppressed by an unrelated movie sharing its id number"


# ---------------------------------------------------------------------------
# The hero
# ---------------------------------------------------------------------------
def test_the_hero_prefers_a_personal_row_over_trending():
    """The biggest thing on the page was TMDB's top trending title, identical
    for every viewer, under a hardcoded "Trending Now" eyebrow."""
    from app.main import _dashboard_hero_for_user

    rows = [
        {
            "id": "personal-trending-movie",
            "name": "Trending Now",
            "media_type": "movie",
            "items": [{"tmdb_id": 1, "backdrop": "/a.jpg"}],
        },
        {
            "id": "personal-picks-movie",
            "name": "Top Picks For You",
            "media_type": "movie",
            "items": [{"tmdb_id": 2, "backdrop": "/b.jpg"}],
        },
    ]
    hero = _dashboard_hero_for_user(rows)

    assert hero["tmdb_id"] == 2
    assert hero["hero_source_name"] == "Top Picks For You", (
        "the eyebrow has to name the shelf the title came from, or it is a "
        "recommendation label on something that was never a recommendation"
    )


def test_the_hero_falls_back_to_trending_for_a_new_profile():
    from app.main import _dashboard_hero_for_user

    rows = [
        {
            "id": "personal-trending-movie",
            "name": "Trending Now",
            "media_type": "movie",
            "items": [{"tmdb_id": 1, "backdrop": "/a.jpg"}],
        }
    ]
    assert _dashboard_hero_for_user(rows)["tmdb_id"] == 1


# ---------------------------------------------------------------------------
# The shelf tagging pass
# ---------------------------------------------------------------------------
def test_the_shelf_pass_finds_untagged_shelf_members(db):
    """The nightly job tags what /discover returned for the last seven days.
    The feed rows are filled by a different fetch with different parameters, so
    a trending title released five years ago was never eligible for any
    scheduled tagging pass at all -- and reached the viewer with no chips and a
    TMDB fallback for More Like This."""
    from workers.shelf_pass import untagged_shelf_ids

    for tmdb_id in (90, 91, 92):
        _title(db, tmdb_id)
    _tag(db, 90, 1, 1.0)
    _feed_row(db, "trending-movie", [90, 91])
    _feed_row(db, "popular-movie", [91, 92])
    db.commit()

    found = untagged_shelf_ids(db)

    assert (90, "movie") not in found, "already tagged"
    assert set(found) == {(91, "movie"), (92, "movie")}
    assert found[0] == (91, "movie"), "the title on two shelves is tagged first"
