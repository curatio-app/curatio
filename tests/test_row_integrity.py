# ruff: noqa: E402
"""Every row applies the quality bar, and every row keeps its own promise.

The tag layer was finished long before the row layer was connected to it. Each
row family acquired its quality gate and its ordering separately, as somebody
noticed the gap, so the families disagreed with each other — and two of them
were never reached at all.

What these tests pin down, in the order the complaints arrived:

* **C-tier titles.** There was no rating floor anywhere. ``quality_expr`` was
  computed on every row and used only to nudge ordering inside a bounded
  multiplier, so a widely-rated 4.8/10 cleared every gate in the product. Worse,
  ``similar_by_vector`` — the engine behind both "Because You Watched" and the
  detail page's "More Like This" — never joined ``media_metadata`` at all.
* **Rows whose contents contradict their name.** Eighteen curated formulas made
  the row's own subject optional, taste rows never required the tag they were
  named after, and the serving layer hard-sorted every universal row by taste,
  discarding the ranking the row was built with.
* **The same handful of films, six times.** Rows were built independently and
  never reconciled against each other.
"""

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_row_integrity.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.models import (  # noqa: E402
    Base,
    MediaMetadata,
    MovieTag,
    Tag,
    UniversalCategory,
    User,
    WatchEvent,
)
from app.relevance import refresh_tag_stats  # noqa: E402

TEST_DATABASE_URL = "sqlite:///./test_row_integrity.db"
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


def _title(db, tmdb_id, media_type="movie", **kw):
    """A title that clears the floor unless a kwarg says otherwise."""
    db.add(
        MediaMetadata(
            tmdb_id=tmdb_id,
            media_type=media_type,
            title=kw.pop("title", f"T{tmdb_id}"),
            overview=kw.pop(
                "overview",
                "A complete synopsis with enough evidence for semantic tagging.",
            ),
            popularity=kw.pop("popularity", 20.0),
            vote_average=kw.pop("vote_average", 7.4),
            vote_count=kw.pop("vote_count", 5000),
            release_date=kw.pop("release_date", "2018-01-01"),
            **kw,
        )
    )


def _tag(db, name, category="mood"):
    tag = Tag(name=name, category=category)
    db.add(tag)
    db.commit()
    return tag


def _assign(db, tmdb_id, tag, media_type="movie", confidence=0.9):
    db.add(
        MovieTag(
            tmdb_id=tmdb_id,
            tag_id=tag.id,
            media_type=media_type,
            confidence=confidence,
        )
    )


# ---------------------------------------------------------------------------
# The c-tier floor, on every surface
# ---------------------------------------------------------------------------
def test_because_you_watched_and_more_like_this_apply_the_floor(db):
    """The two surfaces that had no quality gate of any kind.

    Both are served by ``similar_by_vector``, which filtered on
    ``weight_norm`` and a shared-tag count and never looked at
    ``media_metadata`` — no rating, no votes, no popularity. It is also the
    place a floor matters most: cosine over L2-normalised vectors favours
    *thinly*-tagged titles, and thin tagging tracks obscurity, so the maths
    actively selects for the tail it should be excluding.
    """
    from app.relevance import similar_by_vector
    from workers.personalized_sync import find_similar_by_tags

    rare, other = _tag(db, "Kaiju", "subgenre"), _tag(db, "Bleak")
    _title(db, 1, title="The seed")
    _title(db, 2, title="A good neighbour")
    _title(db, 3, title="Widely rated and terrible", vote_average=4.2)
    for i in range(4, 60):
        _title(db, i)
    db.commit()

    for tmdb_id in (1, 2, 3):
        _assign(db, tmdb_id, rare)
        _assign(db, tmdb_id, other)
    for i in range(4, 60):
        _assign(db, i, other)
    db.commit()
    refresh_tag_stats(db)
    db.commit()

    gated = {tid for tid, _ in similar_by_vector(db, 1, "movie", limit=50)}
    assert 2 in gated, "a good neighbour must survive the floor"
    assert 3 not in gated, "a 4.2/10 title must not be 'more like this'"

    ungated = {
        tid
        for tid, _ in similar_by_vector(db, 1, "movie", limit=50, quality_floor=False)
    }
    assert 3 in ungated, "the fixture must actually be a strong tag match"

    # The BYW wrapper reaches the same engine, so it inherits the gate.
    assert 3 not in find_similar_by_tags(db, 1, "movie", exclude_ids=set(), limit=50)


def test_browse_applies_the_floor(db):
    """The third ungated candidate generator: the Browse screen and the
    vibe-search path that turns "cyberpunk" into a tag query."""
    from app.relevance import browse_by_tags

    tag = _tag(db, "Neo-Noir", "subgenre")
    _title(db, 1, title="Good")
    _title(db, 2, title="Bad", vote_average=4.0)
    db.commit()
    _assign(db, 1, tag)
    _assign(db, 2, tag)
    db.commit()
    refresh_tag_stats(db)
    db.commit()

    assert {t for t, _ in browse_by_tags(db, [tag.id], "movie", mode="any")} == {1}
    assert {
        t
        for t, _ in browse_by_tags(
            db, [tag.id], "movie", mode="any", quality_floor=False
        )
    } == {1, 2}


def test_a_well_matched_dud_reaches_no_row_family(db):
    """One fixture, every surface. The floor is a property of the product now,
    not of whichever builder happened to have one bolted on."""
    from app.catalog_generator import CatalogGenerator
    from app.relevance import browse_by_tags, similar_by_vector
    from workers.personalized_sync import find_recommendations_by_taste

    a, b = _tag(db, "Heist", "subgenre"), _tag(db, "Gritty")
    _title(db, 1, title="Seed the viewer watched")
    _title(db, 2, title="Perfect match, 4.5/10", vote_average=4.5)
    for i in range(3, 40):
        _title(db, i)
    db.commit()
    for tmdb_id in (1, 2):
        _assign(db, tmdb_id, a, confidence=0.95)
        _assign(db, tmdb_id, b, confidence=0.95)
    for i in range(3, 40):
        _assign(db, i, b, confidence=0.4)
    db.commit()
    refresh_tag_stats(db)
    db.commit()

    category = UniversalCategory(
        id="heists",
        name="Heist Films",
        tier=1,
        sort_order=1,
        media_type="movie",
        tag_formula={"mandatory": ["Heist"], "required": ["Gritty"], "min_required": 1},
    )
    db.add(category)
    db.commit()

    universal = [
        t for t, _ in CatalogGenerator(db).generate_universal_catalog(category)
    ]
    similar = [t for t, _ in similar_by_vector(db, 1, "movie", limit=50)]
    browse = [t for t, _ in browse_by_tags(db, [a.id, b.id], "movie", mode="any")]
    recommended = find_recommendations_by_taste(db, {1}, "movie", limit=50)

    for surface, ids in (
        ("universal", universal),
        ("more like this", similar),
        ("browse", browse),
        ("recommended", recommended),
    ):
        assert 2 not in ids, f"the dud reached the {surface} row"


def test_hidden_gems_uses_the_shared_quality_score(db):
    """It had a hardcoded ``vote_average >= 7.0`` — a fourth definition of
    "good" in a product that already had three, and the weakest of them: TMDB's
    average is compressed into roughly 6.5-7.5 and discriminates almost
    nothing."""
    from workers.personalized_sync import (
        GEM_MAX_POPULARITY,
        GEM_MIN_QUALITY,
        _find_hidden_gems_by_taste,
    )

    tag = _tag(db, "Slow-Burn")
    # Both clear the old 7.0 bar on TMDB alone; only one is actually good once
    # the multi-source consensus is read.
    _title(
        db,
        1,
        title="Real gem",
        vote_average=7.1,
        popularity=5.0,
        vote_count=900,
        rating_consensus=8.4,
    )
    _title(
        db,
        2,
        title="Looks fine on TMDB",
        vote_average=7.1,
        popularity=5.0,
        vote_count=900,
        rating_consensus=6.1,
    )
    db.commit()
    _assign(db, 1, tag)
    _assign(db, 2, tag)
    db.commit()
    refresh_tag_stats(db)
    db.commit()

    assert GEM_MIN_QUALITY > 7.0
    gems = _find_hidden_gems_by_taste(db, [tag.id], "movie", exclude_ids=set())
    assert gems == [1]
    assert GEM_MAX_POPULARITY, "a gem is also overlooked, not merely good"


# ---------------------------------------------------------------------------
# Rows keep their promise
# ---------------------------------------------------------------------------
def test_no_curated_row_makes_its_own_subject_optional():
    """Eighteen formulas were ``min_required: 2`` over three tags, so the row's
    head noun was one of three interchangeable ways to qualify: "Mind-Bending
    Sci-Fi Thrillers" was satisfied by Cerebral + Suspenseful with no Sci-Fi
    tag, and "Deeply Disturbing Horror" by Dark + Intense with no Horror."""
    from app.categories import UNIVERSAL_CATEGORIES

    loose = []
    for category in UNIVERSAL_CATEGORIES:
        formula = category["formula"]
        required = formula.get("required", [])
        minimum = formula.get("min_required", len(required))
        if len(required) > minimum and not formula.get("mandatory"):
            loose.append(category["id"])
    assert not loose, (
        "these rows can be filled by titles carrying none of the tags their "
        f"name asserts: {loose}"
    )


def test_a_curated_row_only_contains_titles_carrying_its_subject(db):
    a, b, c = _tag(db, "Horror", "subgenre"), _tag(db, "Dark"), _tag(db, "Intense")
    _title(db, 1, title="Dark intense horror")
    _title(db, 2, title="Dark and intense, but not horror")
    db.commit()
    for tag in (a, b, c):
        _assign(db, 1, tag)
    for tag in (b, c):
        _assign(db, 2, tag)
    db.commit()
    refresh_tag_stats(db)
    db.commit()

    from app.catalog_generator import CatalogGenerator
    from app.categories import UNIVERSAL_CATEGORIES

    spec = next(c for c in UNIVERSAL_CATEGORIES if c["id"] == "dark-horror")
    category = UniversalCategory(
        id=spec["id"],
        name=spec["name"],
        tier=1,
        sort_order=1,
        media_type="movie",
        tag_formula=spec["formula"],
    )
    db.add(category)
    db.commit()

    ids = [t for t, _ in CatalogGenerator(db).generate_universal_catalog(category)]
    assert ids == [1], "'Deeply Disturbing Horror' must contain horror"


def test_a_taste_row_only_contains_titles_carrying_its_anchor(db):
    """The row is named from a tag it did not require. The filter was
    ``tag_id IN (taste_profile | {anchor})`` with ``HAVING count >= 2``, so the
    anchor was one of sixteen ways to qualify — "Because You Like It Dark"
    could hold nothing tagged Dark, and routinely did."""
    from workers.personalized_sync import MIN_ROW_ITEMS, _find_genre_taste_catalogs

    anchor = _tag(db, "Dark")
    other, third = _tag(db, "Gritty"), _tag(db, "Suspenseful")
    watched = 1
    # The anchor pool is drawn from tags on WATCHED titles, so giving the
    # watched title only this tag pins which tag the row can be named after.
    # Without that the rotation is free to pick Gritty, which the decoys
    # legitimately carry, and the test would be asserting nothing.
    _title(db, watched, title="What they watched")
    _assign(db, watched, anchor)

    # Enough anchored titles to clear MIN_ROW_ITEMS, plus decoys that carry the
    # taste tags but not the anchor.
    anchored = list(range(10, 10 + MIN_ROW_ITEMS + 5))
    decoys = list(range(200, 210))
    for tmdb_id in anchored:
        _title(db, tmdb_id)
    for tmdb_id in decoys:
        _title(db, tmdb_id)
    db.commit()
    for tmdb_id in anchored:
        _assign(db, tmdb_id, anchor)
        _assign(db, tmdb_id, other)
    for tmdb_id in decoys:
        _assign(db, tmdb_id, other)
        _assign(db, tmdb_id, third)
    db.commit()
    refresh_tag_stats(db)
    db.commit()

    rows = _find_genre_taste_catalogs(
        db,
        watched_movie_ids={watched},
        watched_show_ids=set(),
        taste_tag_ids=[anchor.id, other.id, third.id],
        max_catalogs=4,
    )
    assert rows, "the anchored population is large enough to make a row"
    for row in rows:
        assert not (
            set(row["tmdb_ids"]) & set(decoys)
        ), f"row {row['name']!r} contains titles that do not carry its anchor tag"


def test_a_row_that_cannot_fill_is_not_built(db):
    """Honest rows are shorter, and some stop existing. A shelf of four posters
    under a category header reads as breakage, so the filter and this rule ship
    together."""
    from workers.personalized_sync import MIN_ROW_ITEMS, _find_genre_taste_catalogs

    anchor = _tag(db, "Dark")
    other = _tag(db, "Gritty")
    _title(db, 1, title="Watched")
    _assign(db, 1, anchor)
    _assign(db, 1, other)
    for tmdb_id in range(10, 10 + MIN_ROW_ITEMS - 5):
        _title(db, tmdb_id)
    db.commit()
    for tmdb_id in range(10, 10 + MIN_ROW_ITEMS - 5):
        _assign(db, tmdb_id, anchor)
        _assign(db, tmdb_id, other)
    db.commit()
    refresh_tag_stats(db)
    db.commit()

    rows = _find_genre_taste_catalogs(
        db,
        watched_movie_ids={1},
        watched_show_ids=set(),
        taste_tag_ids=[anchor.id, other.id],
        max_catalogs=4,
    )
    assert rows == []


def test_taste_moves_a_universal_row_a_bounded_number_of_places(db):
    """It used to replace the ordering outright.

    ``get_catalog_content`` hard-sorted every universal row by taste overlap,
    discarding the blended relevance/quality score the row had just been built
    with and leaving the row's own formula as nothing but a tie-break. A curated
    row is an editorial claim; a row whose first title is the 90th-best match to
    that claim has stopped being the row its header promises.
    """
    from app.catalog_generator import CatalogGenerator, _MAX_FEED_CLIMB
    from app.models import UniversalCatalogContent

    tag = _tag(db, "Heist", "subgenre")
    taste_tag = _tag(db, "Kaiju", "subgenre")
    for i in range(1, 31):
        _title(db, i)
    db.commit()
    for i in range(1, 31):
        _assign(db, i, tag)
    # The viewer's taste tag sits on the LAST title in the row.
    _assign(db, 30, taste_tag)
    db.commit()
    refresh_tag_stats(db)

    category = UniversalCategory(
        id="heists",
        name="Heist Films",
        tier=1,
        sort_order=1,
        media_type="movie",
        tag_formula={"required": ["Heist"], "min_required": 1},
    )
    db.add(category)
    db.commit()
    for rank, tmdb_id in enumerate(range(1, 31), start=1):
        db.add(
            UniversalCatalogContent(
                category_id="heists",
                tmdb_id=tmdb_id,
                rank=rank,
                match_score=1.0 - rank * 0.01,
                media_type="movie",
            )
        )
    db.commit()

    items = CatalogGenerator(db).get_catalog_content(
        "heists", user_taste_tag_ids=[taste_tag.id]
    )
    order = [i["tmdb_id"] for i in items]
    assert order[0] != 30, "taste must not hand the top slot to the last-ranked title"
    climbed = 29 - order.index(30)
    assert (
        0 < climbed <= _MAX_FEED_CLIMB
    ), f"the taste match climbed {climbed} places; the bound is {_MAX_FEED_CLIMB}"


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------
def _row(name, ids, count=None):
    return {
        "id": name,
        "name": name,
        "media_type": "movie",
        "items": [{"tmdb_id": i, "media_type": "movie"} for i in ids],
    }


def test_a_title_appears_in_at_most_one_row_on_a_page():
    """Rows are generated independently — a curated formula, a taste vector and
    a TMDB feed can all legitimately pick the same film — and nothing ever
    reconciled them, so one title could occupy six shelves on one screen."""
    from app.main import MAX_ROW_APPEARANCES, _assemble_page

    shared = list(range(1, 41))
    rows = [_row(f"row-{n}", shared) for n in range(6)]
    kept = _assemble_page(rows)

    appearances: dict[int, int] = {}
    for row in kept:
        for item in row["items"]:
            appearances[item["tmdb_id"]] = appearances.get(item["tmdb_id"], 0) + 1
    assert appearances, "the page must not be emptied"
    assert max(appearances.values()) == MAX_ROW_APPEARANCES == 1


def test_page_assembly_keeps_up_to_eighty_items_per_catalog_row():
    from app.main import NATIVE_DASHBOARD_ROW_ITEM_LIMIT, _assemble_page

    row = _row("deep-catalog", list(range(1, NATIVE_DASHBOARD_ROW_ITEM_LIMIT + 41)))
    kept = _assemble_page([row], item_limit=NATIVE_DASHBOARD_ROW_ITEM_LIMIT)

    assert NATIVE_DASHBOARD_ROW_ITEM_LIMIT == 80
    assert len(kept) == 1
    assert len(kept[0]["items"]) == NATIVE_DASHBOARD_ROW_ITEM_LIMIT


def test_page_assembly_prefers_the_earlier_row():
    """Greedy in row order, and rows arrive most-personal first, so the generic
    row is the one that gives a title up."""
    from app.main import _assemble_page

    personal = _row("because-you-watched", list(range(1, 41)))
    generic = _row("popular", list(range(1, 41)))
    kept = _assemble_page([personal, generic, _row("also-popular", list(range(1, 41)))])
    assert [r["name"] for r in kept] == ["because-you-watched"]


def test_page_assembly_drops_short_activity_rows_from_discovery():
    """Activity is served separately; it must not bypass the shelf floor."""
    from app.main import _assemble_page

    tiny = _row("up-next", [900])
    assert _assemble_page([tiny]) == []


def test_page_assembly_drops_duplicate_names_and_near_duplicate_heads():
    from app.main import _assemble_page

    first = _row("Crime Thrillers", list(range(1, 41)))
    same_name = _row("CRIME---THRILLERS", list(range(100, 140)))
    # 34/46 = 73.9% Jaccard overlap in the untouched visible head.
    same_head = _row("Dark Suspense", list(range(1, 35)) + list(range(200, 206)))
    distinct = _row("Space Adventures", list(range(300, 340)))

    kept = _assemble_page([first, same_name, same_head, distinct])

    assert [row["name"] for row in kept] == ["Crime Thrillers", "Space Adventures"]


def test_page_assembly_uses_oversized_pool_or_drops_the_shelf():
    from app.main import _assemble_page

    shared = list(range(1, 41))
    rows = [
        _row("first", shared),
        _row("second", list(range(41, 81))),
        # Some shared head titles, but not enough to make the row itself a
        # duplicate concept. Its oversized tail restores a full shelf.
        _row("restored", shared[:20] + list(range(100, 130))),
        _row("too-thin", shared[:20] + list(range(200, 223))),
    ]
    kept = _assemble_page(rows)
    assert [row["name"] for row in kept] == ["first", "second", "restored"]
    assert len(kept[-1]["items"]) == 30


def test_page_assembly_returns_at_most_native_discovery_limit():
    from app.main import MAX_DISCOVERY_ROWS, _assemble_page

    rows = [
        _row(f"row-{index}", list(range(index * 100, index * 100 + 40)))
        for index in range(MAX_DISCOVERY_ROWS + 6)
    ]
    assert len(_assemble_page(rows)) == MAX_DISCOVERY_ROWS


def test_a_row_thinned_below_usable_size_is_dropped_without_spending_claims():
    """A row dropped for being too thin must release the titles it took, or it
    silently thins the next row on behalf of a shelf nobody ever sees."""
    from app.main import MIN_VISIBLE_ROW_ITEMS, _assemble_page

    shared = list(range(1, 41))
    thin_extra = [999]
    rows = [
        _row("first", shared),
        _row("second", shared),
        # Would be left with one unique title after the first row spends
        # their claims, so it is dropped rather than rendered as a stub.
        _row("third", shared + thin_extra),
        _row("fourth", thin_extra * 1 + list(range(41, 41 + MIN_VISIBLE_ROW_ITEMS))),
    ]
    kept = _assemble_page(rows)
    assert [r["name"] for r in kept] == ["first", "fourth"]
    fourth = next(r for r in kept if r["name"] == "fourth")
    assert 999 in [
        i["tmdb_id"] for i in fourth["items"]
    ], "the dropped row must not have consumed 999 on its way out"


# ---------------------------------------------------------------------------
# Because You Watched: the seed
# ---------------------------------------------------------------------------
def test_a_weak_neighbour_list_gets_no_row(db):
    """ "Because You Watched X" is a claim that these titles are like X. When the
    neighbour list is weak the row silently becomes "some titles that share two
    tags with X", which is the shape a viewer reads as broken."""
    from workers.personalized_sync import MIN_BYW_SIMILARITY, find_similar_by_tags

    ubiquitous, second = _tag(db, "Drama", "subgenre"), _tag(db, "Suspenseful")
    for i in range(1, 60):
        _title(db, i)
    db.commit()
    for i in range(1, 60):
        _assign(db, i, ubiquitous, confidence=0.5)
        _assign(db, i, second, confidence=0.5)
    db.commit()
    refresh_tag_stats(db)
    db.commit()

    assert MIN_BYW_SIMILARITY > 0
    assert (
        find_similar_by_tags(db, 1, "movie", exclude_ids=set(), limit=50) == []
    ), "a seed whose only neighbours share two ubiquitous tags explains nothing"


def test_imported_history_never_becomes_a_byw_seed(db):
    from workers.personalized_sync import _pick_byw_seeds_local

    user = User(user_key="seed-test", auth_source="local")
    db.add(user)
    db.flush()
    tag = _tag(db, "Robots", "theme")
    for tmdb_id, title in ((1, "Imported"), (2, "Completed")):
        _title(db, tmdb_id, title=title)
        _assign(db, tmdb_id, tag)
    db.add_all(
        [
            WatchEvent(
                user_id=user.id,
                tmdb_id=1,
                media_type="movie",
                title="Imported",
                action="imported",
            ),
            WatchEvent(
                user_id=user.id,
                tmdb_id=2,
                media_type="movie",
                title="Completed",
                action="complete",
            ),
        ]
    )
    db.commit()

    seeds = _pick_byw_seeds_local(db, user.id, {1, 2})
    assert [seed["tmdb_id"] for seed in seeds] == [2]


def test_because_you_watched_applies_similarity_floor_to_every_item(db, monkeypatch):
    from app import relevance
    from workers import personalized_sync

    monkeypatch.setattr(
        relevance,
        "similar_by_vector",
        lambda *_args, **_kwargs: [(2, 0.5), (3, 0.119), (4, 0.12)],
    )
    monkeypatch.setattr(relevance, "discriminating_tag_count", lambda *_args: 2)
    monkeypatch.setattr(
        personalized_sync,
        "_blend_ids",
        lambda _db, ranked, _media_type: ranked,
    )
    monkeypatch.setattr(
        personalized_sync,
        "shuffle_within_tiers",
        lambda ranked, _rng: [tmdb_id for tmdb_id, _ in ranked],
    )

    scored = personalized_sync.find_similar_by_tags(
        db,
        1,
        "movie",
        exclude_ids=set(),
        limit=40,
        return_scores=True,
    )
    assert scored == [(2, 0.5), (4, 0.12)]
