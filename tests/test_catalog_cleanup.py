# ruff: noqa: E402
"""Row names, the quality floor, and the reshuffle — the operator-visible cleanup.

Three complaints, one file:

* "two coming soon pages, two trending" -- seven pairs of rows shared a name,
  and on the web home page every row flattens into one list where the shelf
  header renders the name alone.
* "some sort of setting to weed out the bad movies or non-mainstream" -- there
  was no such control anywhere. The ``min_votes`` box in admin scopes which
  titles get *tagged*; the row-level floor was a hardcoded ``vote_count >= 10``
  with no endpoint and no UI, and eight of the fourteen personalized rows had
  no quality condition at all.
* "we need to shuffle the catalogs" -- the setting labelled "Randomize catalog
  order every N hours" did neither: row order never varied anywhere, and the
  item shuffle ran on one legacy endpoint only, where it discarded the ranking.
"""

import os
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_catalog_cleanup.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.config import settings  # noqa: E402
from app.models import Base, MediaMetadata  # noqa: E402
from app.row_names import (  # noqa: E402
    DISCOVERY_ROWS,
    FEED_ROWS,
    TASTE_ROWS,
)

TEST_DATABASE_URL = "sqlite:///./test_catalog_cleanup.db"
engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def db():
    # Other modules exercise the process-global app SessionLocal during full
    # suite collection. Start from a known-empty file instead of assuming a
    # prior test's teardown owned every connection to this SQLite database.
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def restore_quality_settings():
    keys = (
        "catalog_min_rating_sources",
        "catalog_min_vote_count",
        "catalog_min_quality",
        "catalog_quality_weight",
        "catalog_niche_min_quality",
        "catalog_niche_min_vote_count",
        "catalog_niche_min_age_days",
    )
    before = {key: getattr(settings, key) for key in keys}
    yield
    for key, value in before.items():
        object.__setattr__(settings, key, value)


# ---------------------------------------------------------------------------
# Row names
# ---------------------------------------------------------------------------
def test_no_two_rows_share_a_name():
    """The complaint, as one assertion."""
    specs = [*FEED_ROWS, *DISCOVERY_ROWS, *TASTE_ROWS]
    names = [spec.name for spec in specs]
    duplicates = {n for n in names if names.count(n) > 1}
    assert not duplicates, f"rows sharing a name: {sorted(duplicates)}"


def test_every_feed_family_names_its_two_halves_differently():
    """A movie row and a series row are siblings, not the same shelf twice."""
    by_family: dict[str, list[str]] = {}
    for spec in FEED_ROWS:
        family = spec.slot_id.rsplit("-", 1)[0]
        by_family.setdefault(family, []).append(spec.name)
    for family, names in by_family.items():
        assert len(names) == 2, family
        assert names[0] != names[1], family


# ---------------------------------------------------------------------------
# The quality floor
# ---------------------------------------------------------------------------
def _known(db, tmdb_id, title, **kw):
    """A metadata row that clears the QUALITY arm, so only the "known" arm is
    under test. vote_average has to be set explicitly: the Bayesian fallback
    pulls a NULL average toward 6.5 only while vote_count is small, so a title
    with many votes and no average scores near 0 and is filtered for a reason
    the test is not about."""
    db.add(
        MediaMetadata(
            tmdb_id=tmdb_id,
            media_type="movie",
            title=title,
            overview=kw.pop(
                "overview",
                "A substantive synopsis with enough evidence to classify this title.",
            ),
            vote_average=kw.pop("vote_average", 7.5),
            **kw,
        )
    )


def test_a_title_no_rating_service_carries_is_filtered(db, restore_quality_settings):
    """The two arms are OR-ed: MDBList vouching for a title, or enough TMDB
    votes. Either is enough; neither is required of every title.

    They used to be AND-ed, with the source arm passing anything whose
    ``mdb_version`` was NULL. On a library the per-title MDBList pass had not
    finished that reduced the whole floor to the vote count -- which defaulted
    to 10, i.e. to nothing.
    """
    from app.catalog_generator import quality_floor_clauses

    object.__setattr__(settings, "catalog_min_rating_sources", 2)
    object.__setattr__(settings, "catalog_min_vote_count", 300)
    object.__setattr__(settings, "catalog_min_quality", 0.0)

    _known(
        db,
        1,
        "Known to MDBList, few TMDB votes",
        mdb_version="1.1.0",
        rating_source_count=4,
        vote_count=20,
    )
    _known(
        db,
        2,
        "Thin: one source, few votes",
        mdb_version="1.1.0",
        rating_source_count=1,
        vote_count=20,
    )
    _known(db, 3, "MDBList has no record", mdb_version="1.1.0-absent", vote_count=20)
    _known(db, 4, "Un-enriched but widely rated", vote_count=5000)
    db.commit()

    kept = {
        r[0]
        for r in db.query(MediaMetadata.tmdb_id).filter(*quality_floor_clauses()).all()
    }
    assert kept == {1, 4}, (
        "MDBList consensus and TMDB votes are independent routes in; a title "
        "MDBList looked for and could not find is the obscure tail by "
        "construction and needs the vote route"
    )


def test_a_title_mdblist_has_never_seen_is_kept(db, restore_quality_settings):
    """The trap this default has to survive, and the one that caught me.

    ``rating_source_count`` defaults to 0 in both the model and the migration
    DDL, so "MDBList has no record" and "we never looked" are the same value.
    Gating on the count alone empties every row in the product on a key-less
    install. ``mdb_version`` is what separates them.
    """
    from app.catalog_generator import quality_floor_clauses

    object.__setattr__(settings, "catalog_min_rating_sources", 2)
    object.__setattr__(settings, "catalog_min_vote_count", 0)
    object.__setattr__(settings, "catalog_min_quality", 0.0)

    # Exactly what a key-less install looks like: enrichment never ran, so
    # mdb_version is NULL and the count sits at its column default of 0.
    db.add(MediaMetadata(tmdb_id=7, media_type="movie", title="Un-enriched"))
    db.commit()

    kept = [
        r[0]
        for r in db.query(MediaMetadata.tmdb_id).filter(*quality_floor_clauses()).all()
    ]
    assert kept == [7], "a library with no MDBList data must not filter to nothing"


def test_the_floor_can_be_turned_off(restore_quality_settings):
    from app.catalog_generator import quality_floor_clauses

    object.__setattr__(settings, "catalog_min_rating_sources", 0)
    object.__setattr__(settings, "catalog_min_vote_count", 0)
    object.__setattr__(settings, "catalog_min_quality", 0.0)
    assert quality_floor_clauses() == []


def test_zeroing_the_vote_arm_does_not_empty_an_un_enriched_library(
    db, restore_quality_settings
):
    """The trap in OR-ing the two arms, and the reason 0 disables the pair.

    An operator reading "0 disables the filter" sets the vote floor to 0. If
    that left the source arm standing alone, every title MDBList has not been
    asked about -- the whole library on a key-less install -- would vanish from
    every row, with no error and nothing in the logs. Disabling either arm of
    an OR disables the OR.
    """
    from app.catalog_generator import quality_floor_clauses

    object.__setattr__(settings, "catalog_min_rating_sources", 2)
    object.__setattr__(settings, "catalog_min_vote_count", 0)
    object.__setattr__(settings, "catalog_min_quality", 0.0)

    _known(db, 7, "Un-enriched", vote_count=3)
    db.commit()

    kept = [
        r[0]
        for r in db.query(MediaMetadata.tmdb_id).filter(*quality_floor_clauses()).all()
    ]
    assert kept == [7]


def test_the_quality_floor_excludes_a_well_matched_dud(db, restore_quality_settings):
    """The c-tier floor.

    Nothing in the product asked "is this any good?" before this. ``quality_expr``
    existed, was computed on every row, and was used only to nudge ordering
    inside a bounded multiplier -- so a widely-rated 4.8/10 cleared every gate
    and could lead a row on tag relevance alone.
    """
    from app.catalog_generator import quality_floor_clauses

    object.__setattr__(settings, "catalog_min_rating_sources", 0)
    object.__setattr__(settings, "catalog_min_vote_count", 0)
    object.__setattr__(settings, "catalog_min_quality", 6.2)

    _known(db, 1, "Widely rated and bad", vote_average=4.8, vote_count=8000)
    _known(db, 2, "Widely rated and good", vote_average=7.6, vote_count=8000)
    _known(
        db,
        3,
        "Poor TMDB average, strong consensus",
        vote_average=4.8,
        vote_count=8000,
        rating_consensus=7.9,
    )
    db.commit()

    kept = {
        r[0]
        for r in db.query(MediaMetadata.tmdb_id).filter(*quality_floor_clauses()).all()
    }
    assert kept == {2, 3}, (
        "the floor reads the same score the rows are ordered by -- MDBList "
        "consensus where it exists, Bayesian TMDB otherwise"
    )


def test_the_quality_defaults_are_the_strict_ones():
    """Deliberate behaviour change, recorded here so it cannot drift back.

    The vote floor was 10, which excluded essentially nothing, and there was no
    rating floor at all.
    """
    from app.config import NUMERIC_BOUNDS, Settings

    assert Settings.model_fields["catalog_min_vote_count"].default == 1000
    assert Settings.model_fields["catalog_min_quality"].default == 6.8
    assert Settings.model_fields["catalog_min_rating_sources"].default == 5
    assert Settings.model_fields["tag_min_confidence"].default == 0.60
    assert Settings.model_fields["catalog_niche_min_quality"].default == 7.4
    assert Settings.model_fields["catalog_niche_min_vote_count"].default == 250
    assert NUMERIC_BOUNDS["catalog_min_quality"][0] == 6.8
    assert Settings(catalog_min_quality=6.2).catalog_min_quality == 6.8
    assert Settings(catalog_min_rating_sources=2).catalog_min_rating_sources == 5
    assert Settings(catalog_min_rating_sources=4).catalog_min_rating_sources == 5
    assert Settings(tag_min_confidence=0.45).tag_min_confidence == 0.60


def test_the_quality_floor_requires_a_substantive_synopsis(
    db, restore_quality_settings
):
    from app.catalog_generator import quality_floor_clauses

    object.__setattr__(settings, "catalog_min_rating_sources", 2)
    object.__setattr__(settings, "catalog_min_vote_count", 1000)
    object.__setattr__(settings, "catalog_min_quality", 6.8)

    _known(db, 1, "Described", vote_average=8.0, vote_count=5000)
    _known(db, 2, "Blank", overview="", vote_average=8.0, vote_count=5000)
    _known(
        db, 3, "Placeholder", overview="Coming soon.", vote_average=8.0, vote_count=5000
    )
    db.commit()

    kept = {
        row[0]
        for row in db.query(MediaMetadata.tmdb_id)
        .filter(*quality_floor_clauses())
        .all()
    }
    assert kept == {1}


def test_exceptional_older_niche_titles_survive_without_admitting_recent_noise(
    db, restore_quality_settings
):
    from app.catalog_generator import quality_floor_clauses

    object.__setattr__(settings, "catalog_min_rating_sources", 2)
    object.__setattr__(settings, "catalog_min_vote_count", 1000)
    object.__setattr__(settings, "catalog_min_quality", 6.8)
    object.__setattr__(settings, "catalog_niche_min_quality", 7.4)
    object.__setattr__(settings, "catalog_niche_min_vote_count", 250)
    object.__setattr__(settings, "catalog_niche_min_age_days", 180)

    _known(
        db,
        1,
        "Older acclaimed niche film",
        vote_average=8.0,
        vote_count=500,
        release_date="2020-01-01",
    )
    _known(
        db,
        2,
        "Recent thin signal",
        vote_average=8.0,
        vote_count=500,
        release_date=datetime.utcnow().strftime("%Y-%m-%d"),
    )
    _known(
        db,
        3,
        "Older mediocre obscurity",
        vote_average=6.0,
        vote_count=500,
        release_date="2020-01-01",
    )
    db.commit()

    kept = {
        row[0]
        for row in db.query(MediaMetadata.tmdb_id)
        .filter(*quality_floor_clauses())
        .all()
    }
    assert kept == {1}


def test_the_recommendation_rows_have_a_quality_condition(db):
    """find_recommendations_by_taste builds four rows -- Recommended and Top
    Picks, both types -- and joined movie_tags alone with no vote, rating or
    popularity gate of any kind."""
    from workers.personalized_sync import _quality_floor_ids

    compiled = str(_quality_floor_ids(db, "movie"))
    assert "media_metadata" in compiled


# ---------------------------------------------------------------------------
# The reshuffle
# ---------------------------------------------------------------------------
def _row(tmdb_id, score):
    return {"tmdb_id": tmdb_id, "media_type": "movie", "match_score": score}


def test_the_reshuffle_never_moves_a_title_past_a_better_match():
    """The old version called random.shuffle on the whole 200-item list,
    discarding the blended score and the taste sort computed moments earlier in
    the same request."""
    from app.main import _shuffle_items

    items = [_row(i, 10.0 - i) for i in range(10)]
    shuffled = _shuffle_items(items, "universal-dark-crime-dramas")

    assert [i["tmdb_id"] for i in shuffled] == [
        i["tmdb_id"] for i in items
    ], "distinct scores form single-item tiers, so nothing may move"


def test_equally_scored_titles_do_trade_places():
    from app.main import _shuffle_items

    items = [_row(i, 5.0) for i in range(40)]
    shuffled = _shuffle_items(items, "universal-dark-crime-dramas")

    assert {i["tmdb_id"] for i in shuffled} == {i["tmdb_id"] for i in items}
    assert [i["tmdb_id"] for i in shuffled] != [i["tmdb_id"] for i in items]


def test_personal_rows_are_never_reshuffled_at_serve_time():
    """They are already tier-shuffled per day at build time; doing it again
    here would undo that, and for Coming Soon the order is the content."""
    from app.main import _shuffle_items

    items = [_row(i, 0.0) for i in range(20)]
    for slot in (
        "personal-byw-movie-1",
        "personal-coming-soon-movie",
        "personal-trending-movie",
        "personal-gems-series",
    ):
        assert _shuffle_items(items, slot) == items, slot


def test_the_web_dashboard_shuffles_catalog_rows():
    """_shuffle_items once had a single caller on a legacy endpoint, so on the
    web home page nothing moved between nightly jobs."""
    import inspect

    from app import main

    source = inspect.getsource(main._catalog_row_from_items)
    assert "_shuffle_items" in source


# ---------------------------------------------------------------------------
# Coming Soon
# ---------------------------------------------------------------------------
def test_coming_soon_keeps_its_date_order_for_a_profile_with_taste():
    """Two mechanisms disagreed about this row. _shuffle_items exempted it with
    a docstring saying the order IS the content, while _blend_feed_row -- which
    matched it via is_tmdb_sourced_slot -- reordered it by taste up to eight
    places."""
    from app.catalog_generator import is_tmdb_sourced_slot

    assert is_tmdb_sourced_slot("coming-soon-movie")

    import inspect

    from app.catalog_generator import CatalogGenerator

    source = inspect.getsource(CatalogGenerator.get_catalog_content)
    assert 'startswith("coming-soon-")' in source, (
        "the blend must exclude Coming Soon, or its soonest-first ordering is "
        "a claim the code does not honour"
    )


def test_coming_soon_asks_tmdb_for_a_real_vote_floor():
    """The value was 0, directly under a comment explaining why a floor was
    needed."""
    from workers.personalized_sync import COMING_SOON_MIN_VOTES

    assert COMING_SOON_MIN_VOTES > 0


# ---------------------------------------------------------------------------
# The generated-row rotation
# ---------------------------------------------------------------------------
def test_the_rotation_steps_by_a_whole_window():
    """It advanced by one row per period against a ~140-row pool showing 20 at
    a time, so a full pass took 140 periods -- 980 days at the default week."""
    from datetime import date

    from app.main import _rotation_offset

    pool, window, period = 140, 20, settings.synth_rotation_days
    start = date(2026, 1, 5)
    seen = set()
    for step in range(pool // window):
        day = date.fromordinal(start.toordinal() + step * period)
        seen.add(_rotation_offset(pool, day, window=window))
    assert (
        len(seen) == pool // window
    ), f"only {len(seen)} distinct windows in {pool // window} periods"


def test_generated_rows_interleave_the_two_media_types():
    """apply turns list position into manifest_priority, so emitting all 70
    movie rows before all 70 TV rows made every generated TV shelf need ~51
    periods to reach the home screen."""
    from app.category_synth import RowSpec, _interleave

    def spec(slug, media_type):
        return RowSpec(
            slug=slug,
            name=slug,
            media_type=media_type,
            tag_a="A",
            tag_b="B",
            facet_a="mood",
            facet_b="subgenre",
            support=100,
            lift=2.0,
        )

    out = _interleave(
        {
            "movie": [spec(f"m{i}", "movie") for i in range(5)],
            "tv": [spec(f"t{i}", "tv") for i in range(5)],
        }
    )
    assert [s.media_type for s in out[:4]] == ["movie", "tv", "movie", "tv"]


def test_a_generated_row_can_reuse_an_inactive_curated_name_but_not_repeat_itself():
    """Inactive compatibility names do not suppress a corpus-discovered row."""
    from app.categories import UNIVERSAL_CATEGORIES
    from app.category_synth import RowSpec, _dedupe_names

    curated = str(UNIVERSAL_CATEGORIES[0]["name"])

    def spec(name, media_type="movie"):
        return RowSpec(
            slug=f"gen-{media_type}-{name.lower().replace(' ', '-')}",
            name=name,
            media_type=media_type,
            tag_a="A",
            tag_b="B",
            facet_a="mood",
            facet_b="subgenre",
            support=100,
            lift=2.0,
        )

    kept = _dedupe_names(
        [spec(curated), spec(curated, media_type="tv"), spec("Something Else")]
    )
    assert [s.name for s in kept] == [curated, "Something Else"]


def test_a_generated_name_is_not_shipped_for_both_media_types():
    """The slug embeds the media type and the per-propose guard is scoped to
    one type, so an identical name could ship as both a movie and a TV row."""
    from app.category_synth import RowSpec, _dedupe_names

    def spec(name, media_type):
        return RowSpec(
            slug=f"gen-{media_type}-x",
            name=name,
            media_type=media_type,
            tag_a="A",
            tag_b="B",
            facet_a="mood",
            facet_b="subgenre",
            support=100,
            lift=2.0,
        )

    kept = _dedupe_names(
        [spec("Neon Thrillers", "movie"), spec("Neon Thrillers", "tv")]
    )
    assert len(kept) == 1


# ---------------------------------------------------------------------------
# What was deleted
# ---------------------------------------------------------------------------
def test_the_unreachable_personalized_dispatch_tree_is_gone():
    """~190 lines with four authoritative-looking quality thresholds that could
    not execute -- and were the first thing anyone found when asked where the
    quality filter lived."""
    from app.catalog_generator import CatalogGenerator

    for name in (
        "generate_personalized_catalog",
        "_generate_top_picks",
        "_fallback_top_picks",
        "_generate_because_you_watched",
        "_generate_from_tmdb_ids",
        "_generate_hidden_gems",
    ):
        assert not hasattr(CatalogGenerator, name), f"{name} is still here"


# ---------------------------------------------------------------------------
# Upgrading a running install
#
# `admin_settings` rows are replayed over the config object on every boot, so a
# change to what `_apply_setting` does is also a change to what an EXISTING
# database means. Nothing else in the suite starts from a populated
# `admin_settings`, which is how both of these got through.
# ---------------------------------------------------------------------------
def _setting(db, key, value):
    from app.models import AdminSetting

    db.add(AdminSetting(key=key, value=value, updated_at=datetime.utcnow()))
    db.commit()


def _keys(db):
    from app.models import AdminSetting

    return {row.key for row in db.query(AdminSetting).all()}


def test_a_stale_catalog_toggle_does_not_switch_off_every_row(db):
    """The upgrade hazard, as one assertion.

    Both keys were offered by the settings tab while being fields on nothing,
    and `saveSettings` sends every toggle on every save, so any install where
    Save Settings was ever pressed holds `"False"` for them -- inert, because
    `_apply_setting`'s hasattr check dropped it. Making them real settings makes
    that row live: the first boot after the upgrade would serve no catalog rows
    at all, on either surface, with nothing in the logs to explain it.
    """
    from app.database import CATALOG_TOGGLE_RESET_MARKER, _reset_stale_catalog_toggles

    _setting(db, "ENABLE_UNIVERSAL_CATALOGS", "False")
    _setting(db, "ENABLE_PERSONALIZED_CATALOGS", "False")

    _reset_stale_catalog_toggles(db)

    assert "ENABLE_UNIVERSAL_CATALOGS" not in _keys(db)
    assert "ENABLE_PERSONALIZED_CATALOGS" not in _keys(db)
    assert CATALOG_TOGGLE_RESET_MARKER in _keys(db)
    # Nothing left to replay, so the code defaults stand.
    assert settings.enable_universal_catalogs is True
    assert settings.enable_personalized_catalogs is True


def test_a_toggle_set_deliberately_after_the_upgrade_survives(db):
    """Guarded by a marker rather than run unconditionally, so this reconciles
    the pre-upgrade artifact exactly once. A value stored afterwards is a real
    setting and must not be silently reset on the next restart."""
    from app.database import _reset_stale_catalog_toggles

    _setting(db, "ENABLE_UNIVERSAL_CATALOGS", "False")
    _reset_stale_catalog_toggles(db)
    assert "ENABLE_UNIVERSAL_CATALOGS" not in _keys(db)

    _setting(db, "ENABLE_UNIVERSAL_CATALOGS", "false")
    _reset_stale_catalog_toggles(db)
    assert "ENABLE_UNIVERSAL_CATALOGS" in _keys(db)


@pytest.mark.parametrize("former_default", ["2", "4"])
def test_the_former_persisted_source_default_is_upgraded_once(db, former_default):
    from app.database import (
        CATALOG_SOURCE_FLOOR_UPGRADE_MARKER,
        _upgrade_catalog_source_floor,
    )
    from app.models import AdminSetting

    _setting(db, "CATALOG_MIN_RATING_SOURCES", former_default)
    _upgrade_catalog_source_floor(db)

    row = (
        db.query(AdminSetting)
        .filter(AdminSetting.key == "CATALOG_MIN_RATING_SOURCES")
        .one()
    )
    assert row.value == "5"
    assert CATALOG_SOURCE_FLOOR_UPGRADE_MARKER in _keys(db)

    # Once deployed, an operator can deliberately choose any allowed value.
    row.value = "2"
    db.commit()
    _upgrade_catalog_source_floor(db)
    assert row.value == "2"


def test_a_custom_source_floor_survives_the_upgrade(db):
    from app.database import _upgrade_catalog_source_floor
    from app.models import AdminSetting

    _setting(db, "CATALOG_MIN_RATING_SOURCES", "3")
    _upgrade_catalog_source_floor(db)

    row = (
        db.query(AdminSetting)
        .filter(AdminSetting.key == "CATALOG_MIN_RATING_SOURCES")
        .one()
    )
    assert row.value == "3"


def test_the_reset_is_idempotent_on_a_fresh_install(db):
    from app.database import CATALOG_TOGGLE_RESET_MARKER, _reset_stale_catalog_toggles

    _reset_stale_catalog_toggles(db)
    _reset_stale_catalog_toggles(db)
    assert _keys(db) == {CATALOG_TOGGLE_RESET_MARKER}


@contextmanager
def _captured_warnings():
    """Collect loguru WARNINGs.

    Not pytest's `caplog`: the app logs through loguru, which does not propagate
    to the stdlib root logger, so a caplog-based assertion here passes whatever
    happens -- including the negative one, which would then be asserting
    nothing at all.
    """
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(
        lambda m: messages.append(m.record["message"]), level="WARNING"
    )
    try:
        yield messages
    finally:
        logger.remove(sink_id)


def test_settings_that_live_only_in_the_database_do_not_warn():
    """Six keys are read straight out of `admin_settings` and never through the
    config object. The "not a setting" warning exists to catch a control wired
    to nothing -- a real bug that shipped -- and firing it on every boot for
    correctly-designed keys teaches the reader to ignore it."""
    from app.admin import _apply_setting

    # Named, not iterated over the allowlist itself -- a test that walks the
    # very set it is checking passes just as happily when a key is dropped
    # from it, which is the regression worth catching.
    db_only = (
        "BUILD_PAUSED",  # app/admin.py, run state rather than configuration
        "FINGERPRINT_PRESET",  # workers/fingerprint_worker.py
        "mdblist_api_key",  # app/mdblist_client.py
        "integration_plex_v1",  # app/sources/, lowercase storage key
        "some_addon_setting_v1",  # installed addons keep lowercase keys
    )
    with _captured_warnings() as warnings:
        for key in db_only:
            _apply_setting(key, "some-value")
    assert not warnings, warnings


def test_a_key_backed_by_nothing_still_warns():
    """The case the warning is for: a control writing a row no code reads."""
    from app.admin import _apply_setting

    with _captured_warnings() as warnings:
        _apply_setting("ENABLE_SOMETHING_INVENTED", "true")
    assert any("is not a setting" in message for message in warnings), warnings
