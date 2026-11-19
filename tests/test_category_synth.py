# ruff: noqa: E402
"""Synthesized catalog rows from tag co-occurrence.

The 40 curated categories are editorial — a human decided "Dark & Gritty Crime
Dramas" is worth a row. That cannot know what *this* library actually contains.
Synthesis finds the combinations that are over-represented relative to chance
and turns them into rows.

The property that matters is that **lift, not frequency, decides**. The most
frequent pair in any library is whatever pairs with Drama, and a row of "Drama
Thrillers" is exactly the uninformative shelf this exists to avoid.
"""

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_category_synth.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app import category_synth  # noqa: E402
from app.categories import (  # noqa: E402
    CURATED_PRIORITY,
    UNIVERSAL_CATEGORIES,
    seed_categories,
)
from app.models import (  # noqa: E402
    Base,
    MediaMetadata,
    MovieTag,
    Tag,
    UniversalCategory,
)
from app.relevance import refresh_tag_stats  # noqa: E402

TEST_DATABASE_URL = "sqlite:///./test_category_synth.db"
engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def db():
    # A previously interrupted test run can leave this file-backed SQLite
    # database populated. Always begin from an empty schema as well as cleaning
    # up afterward so the suite remains deterministic across reruns.
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def _tag(db, name, facet):
    tag = Tag(name=name, category=facet)
    db.add(tag)
    db.commit()
    return tag


def _seed_library(db, size=400):
    """A corpus with one genuinely over-represented pair.

    * ``Drama`` is on 60% of titles and ``Thriller`` on 30% — independently, so
      their co-occurrence is high but their lift is ~1.0.
    * ``Neo-Noir`` and ``Bleak`` are each on ~15%, and they *always* appear
      together — high lift, moderate support.

    Frequency ranking picks Drama+Thriller. Lift ranking picks Neo-Noir+Bleak,
    which is the row a person would actually want.
    """
    from workers.tagging_common import tagger_version

    version = tagger_version()
    tags = {
        "Drama": _tag(db, "Drama", "subgenre"),
        "Thriller": _tag(db, "Thriller", "subgenre"),
        "Neo-Noir": _tag(db, "Neo-Noir", "subgenre"),
        "Bleak": _tag(db, "Bleak", "mood"),
        "Feel-Good": _tag(db, "Feel-Good", "mood"),
    }
    for i in range(size):
        db.add(
            MediaMetadata(
                tmdb_id=i + 1,
                media_type="movie",
                title=f"Movie {i + 1}",
                overview="A complete synopsis with enough evidence for semantic tagging.",
                popularity=float(size - i),
                vote_average=7.0,
                vote_count=5000,
                release_date="2015-01-01",
            )
        )
        assigned = []
        if i % 10 < 6:
            assigned.append("Drama")
        # Interleaved so Thriller lands on Drama titles at the corpus rate --
        # co-occurrence without association.
        if i % 10 in (0, 3, 6):
            assigned.append("Thriller")
        if 60 <= i < 120:
            assigned.extend(["Neo-Noir", "Bleak"])
        if i % 10 == 9:
            assigned.append("Feel-Good")
        for name in assigned:
            db.add(
                MovieTag(
                    tmdb_id=i + 1,
                    media_type="movie",
                    tag_id=tags[name].id,
                    confidence=0.85,
                    tagger_version=version,
                )
            )
    db.commit()
    refresh_tag_stats(db, create_indexes=False)
    return tags


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def test_lift_not_frequency_decides_which_rows_exist(db):
    """The headline property.

    Drama+Thriller co-occurs on far more titles than Neo-Noir+Bleak, and it
    still must not win: two common tags landing together at the corpus rate is
    arithmetic, not a genre.
    """
    _seed_library(db)

    specs = category_synth.propose(db, "movie", limit=20)

    names = [(s.tag_a, s.tag_b) for s in specs]
    assert any(set(pair) == {"Neo-Noir", "Bleak"} for pair in names)
    assert not any(set(pair) == {"Drama", "Thriller"} for pair in names)


def test_a_pair_below_the_support_floor_is_noise(db):
    """Three titles sharing two rare tags gives enormous lift and no shelf."""
    _seed_library(db)
    rare_a = _tag(db, "Kaiju", "subgenre")
    rare_b = _tag(db, "Absurdist", "mood")
    from workers.tagging_common import tagger_version

    for tmdb_id in (1, 2, 3):
        db.add(
            MovieTag(
                tmdb_id=tmdb_id,
                media_type="movie",
                tag_id=rare_a.id,
                confidence=0.9,
                tagger_version=tagger_version(),
            )
        )
        db.add(
            MovieTag(
                tmdb_id=tmdb_id,
                media_type="movie",
                tag_id=rare_b.id,
                confidence=0.9,
                tagger_version=tagger_version(),
            )
        )
    db.commit()
    refresh_tag_stats(db, create_indexes=False)

    specs = category_synth.propose(db, "movie", limit=20)

    assert not any({s.tag_a, s.tag_b} == {"Kaiju", "Absurdist"} for s in specs)


def test_same_facet_pairs_are_rejected(db):
    """ "Neo-Noir Slashers" is two subgenres arguing, not a row."""
    _seed_library(db)

    specs = category_synth.propose(db, "movie", limit=40)

    assert all(s.facet_a != s.facet_b for s in specs)


def test_a_ubiquitous_tag_never_anchors_a_row(db):
    """A tag on 60% of the library says nothing about any title carrying it."""
    _seed_library(db)

    specs = category_synth.propose(db, "movie", limit=40)

    assert all("Drama" not in (s.tag_a, s.tag_b) for s in specs)


def test_no_tag_anchors_more_rows_than_the_cap(db):
    _seed_library(db)

    specs = category_synth.propose(db, "movie", limit=60)

    counts: dict[str, int] = {}
    for spec in specs:
        counts[spec.tag_a] = counts.get(spec.tag_a, 0) + 1
        counts[spec.tag_b] = counts.get(spec.tag_b, 0) + 1
    assert all(n <= category_synth.MAX_ROWS_PER_TAG for n in counts.values())


def test_facet_diversity_cap_treats_pair_order_as_irrelevant():
    specs = [
        category_synth.RowSpec(
            slug=f"row-{index}",
            name=f"Row {index}",
            media_type="movie",
            tag_a=f"Origin {index}" if index % 2 == 0 else f"Place {index}",
            tag_b=f"Place {index}" if index % 2 == 0 else f"Origin {index}",
            facet_a="origin" if index % 2 == 0 else "place",
            facet_b="place" if index % 2 == 0 else "origin",
            support=100 - index,
            lift=3.0 - index / 10,
        )
        for index in range(4)
    ]

    chosen = category_synth._select(specs, limit=12)

    assert len(chosen) == 2


def test_proposals_are_deterministic(db):
    """Two runs over the same corpus must agree, or every weekly run churns
    the home rows and every installed client sees rows appear and vanish."""
    _seed_library(db)

    first = category_synth.propose(db, "movie", limit=20)
    second = category_synth.propose(db, "movie", limit=20)

    assert [s.slug for s in first] == [s.slug for s in second]


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------
def test_names_come_from_templates_with_no_api_call(db):
    _seed_library(db)

    specs = category_synth.propose(db, "movie", limit=20)

    spec = next(s for s in specs if {s.tag_a, s.tag_b} == {"Neo-Noir", "Bleak"})
    # ("mood", "subgenre") -> "{a} {b}s". The pair is reordered to match the
    # template's direction, so it reads "Bleak Neo-Noirs" not "Neo-Noir Bleaks".
    assert spec.name == "Bleak Neo-Noirs"


def test_generated_formula_requires_current_meaningful_assignments(db):
    from workers.tagging_common import tagger_version

    _seed_library(db)
    spec = category_synth.propose(db, "movie", limit=20)[0]

    assert spec.formula["tagger_version"] == tagger_version()
    assert spec.formula["min_weight_norm"] == category_synth.MIN_PAIR_WEIGHT_NORM


def test_every_row_gets_a_name_even_without_a_template():
    """A row that cannot be named is a row that cannot ship."""
    name, a, b = category_synth.name_for("Kaiju", "subgenre", "Kids", "audience")

    assert name and name.strip()
    assert {a, b} == {"Kaiju", "Kids"}


def test_names_are_unique_and_short_enough_to_render(db):
    _seed_library(db)

    specs = category_synth.propose(db, "movie", limit=40)

    names = [s.name for s in specs]
    assert len(names) == len(set(names))
    assert all(len(n) <= 60 for n in names)


def test_the_spec_hash_tracks_the_specification_not_the_row(db):
    """A name is only regenerated when the underlying pair changes.

    Without this an optional LLM polish pass would re-run on every synthesis,
    paying for names it already has.
    """
    _seed_library(db)

    first = category_synth.propose(db, "movie", limit=20)
    second = category_synth.propose(db, "movie", limit=20)

    assert [s.spec_hash for s in first] == [s.spec_hash for s in second]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def test_synthesis_never_mutates_curated_rows(db):
    """seed_categories owns the curated tier and re-runs on every startup."""
    _seed_library(db)
    seed_categories(db)
    before = {
        c.id: (c.name, c.tag_formula, c.sort_order)
        for c in db.query(UniversalCategory).filter(
            UniversalCategory.source == "curated"
        )
    }

    category_synth.synthesize(db, limit_per_type=20)

    after = {
        c.id: (c.name, c.tag_formula, c.sort_order)
        for c in db.query(UniversalCategory).filter(
            UniversalCategory.source == "curated"
        )
    }
    assert before == after
    assert len(before) == len(UNIVERSAL_CATEGORIES)


def test_generated_ids_fit_the_production_varchar_and_stay_collision_safe():
    """Long template names previously rolled back every generated row."""
    from app.category_synth import CATEGORY_ID_MAX_LENGTH, _generated_slug

    first = _generated_slug(
        "movie", "Political Thrillers About Corruption of Power and Surveillance"
    )
    second = _generated_slug(
        "movie", "Political Thrillers About Corruption of Power and Revolution"
    )

    assert len(first) <= CATEGORY_ID_MAX_LENGTH
    assert len(second) <= CATEGORY_ID_MAX_LENGTH
    assert first != second
    assert first == _generated_slug(
        "movie", "Political Thrillers About Corruption of Power and Surveillance"
    )


def test_the_seeder_does_not_fight_the_synthesizer(db):
    """seed_categories runs on every boot. It must leave generated rows alone."""
    _seed_library(db)
    seed_categories(db)
    category_synth.synthesize(db, limit_per_type=20)
    generated_before = {
        c.id: c.name
        for c in db.query(UniversalCategory).filter(
            UniversalCategory.source == "generated"
        )
    }
    assert generated_before, "fixture must actually produce generated rows"

    seed_categories(db)  # simulate a restart

    generated_after = {
        c.id: c.name
        for c in db.query(UniversalCategory).filter(
            UniversalCategory.source == "generated"
        )
    }
    assert generated_after == generated_before


def test_generated_rows_have_their_own_full_priority_tier(db):
    _seed_library(db)
    seed_categories(db)
    category_synth.synthesize(db, limit_per_type=20)

    generated = (
        db.query(UniversalCategory)
        .filter(UniversalCategory.source == "generated")
        .all()
    )

    assert generated
    assert min(g.sort_order for g in generated) == 1
    assert max(g.manifest_priority for g in generated) == CURATED_PRIORITY
    assert all(g.source == "generated" for g in generated)


def test_a_dropped_row_is_deactivated_not_deleted(db):
    """Its catalog content is still being served until the next regeneration,
    and deleting the category cascades that content away mid-flight."""
    _seed_library(db)
    category_synth.synthesize(db, limit_per_type=20)
    stale = (
        db.query(UniversalCategory)
        .filter(UniversalCategory.source == "generated")
        .first()
    )
    assert stale is not None
    stale_id = stale.id

    # A second pass proposing nothing: every previously-written row falls out.
    category_synth.apply(db, [])

    row = db.query(UniversalCategory).filter(UniversalCategory.id == stale_id).first()
    assert row is not None, "the row must survive so its content is not cascaded away"
    assert row.is_active is False


def test_applying_twice_is_idempotent(db):
    _seed_library(db)

    first = category_synth.synthesize(db, limit_per_type=20)
    second = category_synth.synthesize(db, limit_per_type=20)

    assert first["created"] > 0
    assert second["created"] == 0
    assert second["deactivated"] == 0
    assert second["updated"] == first["created"]


def test_an_untagged_library_proposes_nothing_rather_than_raising(db):
    """A fresh install has no tag_stats at all. Synthesis must no-op."""
    assert category_synth.propose(db, "movie", limit=20) == []
    assert category_synth.synthesize(db, limit_per_type=20)["proposed"] == 0


def test_preview_writes_nothing(db):
    _seed_library(db)

    rows = category_synth.preview(db, limit_per_type=20)

    assert rows
    assert db.query(UniversalCategory).count() == 0


# ---------------------------------------------------------------------------
# Naming, after the first production preview exposed real defects
# ---------------------------------------------------------------------------
def test_plurals_are_english_not_a_bare_s():
    """Live output contained *Documentarys* and *Mockumentarys*."""
    from app.taxonomy import pluralise

    assert pluralise("Documentary") == "Documentaries"
    assert pluralise("Mockumentary") == "Mockumentaries"
    assert pluralise("Comedy") == "Comedies"
    assert pluralise("Box") == "Boxes"
    assert pluralise("Neo-Noir") == "Neo-Noirs"
    assert pluralise("Nature Documentary") == "Nature Documentaries"


def test_an_already_plural_name_is_left_alone():
    from app.taxonomy import pluralise

    assert pluralise("Series") == "Series"
    assert pluralise("Limited Series") == "Limited Series"


def test_roman_numerals_are_never_pluralised():
    """ "World War IIs" is the failure this prevents."""
    from app.taxonomy import pluralise

    assert pluralise("World War II") == "World War II"


def test_generated_names_use_real_english_irregular_plurals():
    from app.taxonomy import pluralise

    assert pluralise("Anti-Hero") == "Anti-Heroes"
    assert pluralise("Hero") == "Heroes"
    assert pluralise("Rise and Fall") == "Rise-and-Fall Stories"


def test_full_database_bare_joins_have_editorial_templates():
    assert (
        category_synth.name_for("Far Future", "setting_period", "Outer Space", "place")[
            0
        ]
        == "Outer Space Stories from the Far Future"
    )
    assert (
        category_synth.name_for("High School", "place", "Teen", "audience")[0]
        == "Teen Stories Set in High School"
    )
    assert (
        category_synth.name_for(
            "Ghostly Presence", "plot_device", "Supernatural Horror", "subgenre"
        )[0]
        == "Supernatural Horrors with Ghostly Presence"
    )


def test_a_contained_tag_does_not_repeat_itself():
    """Live output contained *Christmas Christmas Movies*."""
    name, _, _ = category_synth.name_for(
        "Christmas", "seasonal", "Christmas Movie", "subgenre"
    )
    assert name == "Christmas Movies"

    media_name, _, _ = category_synth.name_for(
        "Christmas", "seasonal", "Christmas Movie", "subgenre", "movie"
    )
    assert media_name == "Christmas Movies"


def test_periods_get_an_article_only_when_they_need_one():
    """Live output contained *Set in the World War II*."""
    from app.taxonomy import period_phrase

    assert period_phrase("Cold War") == "the Cold War"
    assert period_phrase("World War II") == "World War II"

    name, _, _ = category_synth.name_for(
        "World War II", "setting_period", "War Drama", "subgenre"
    )
    assert name == "War Dramas Set in World War II"


def test_the_pairs_the_preview_could_not_name_now_have_templates():
    """Each of these fell through to a bare "A B" join in production."""
    from app.taxonomy import NAME_TEMPLATES

    for pair in (
        ("audience", "subgenre"),
        ("theme", "setting_period"),
        ("character", "mood"),
        ("audience", "mood"),
    ):
        assert pair in NAME_TEMPLATES, pair


@pytest.mark.parametrize(
    ("tag_a", "facet_a", "tag_b", "facet_b", "media_type", "expected"),
    [
        (
            "Superhero",
            "subgenre",
            "Comic Book Adaptation",
            "source_franchise",
            "movie",
            "Comic Book Superhero Movies",
        ),
        (
            "Hong Kong",
            "origin",
            "Martial Arts",
            "subgenre",
            "movie",
            "Hong Kong Martial Arts Movies",
        ),
        (
            "Family Movie Night",
            "audience",
            "Family",
            "subgenre",
            "tv",
            "Family Movie Night",
        ),
        ("Dark", "mood", "Comedy", "subgenre", "movie", "Dark Comedies"),
        (
            "1940s",
            "release_decade",
            "Black and White",
            "style",
            "movie",
            "Black-and-White Movies from the 1940s",
        ),
        (
            "Outer Space",
            "place",
            "Sci-Fi",
            "subgenre",
            "tv",
            "Sci-Fi Shows Set in Outer Space",
        ),
        (
            "World War II",
            "setting_period",
            "War",
            "subgenre",
            "movie",
            "World War II Movies",
        ),
        (
            "Family",
            "subgenre",
            "Kids",
            "audience",
            "tv",
            "Family Shows for Kids",
        ),
        (
            "Slice of Life",
            "subgenre",
            "Manga Adaptation",
            "source_franchise",
            "tv",
            "Slice-of-Life Manga Adaptations",
        ),
        (
            "British",
            "origin",
            "London",
            "place",
            "tv",
            "British Shows Set in London",
        ),
        (
            "Japanese",
            "origin",
            "Manga Adaptation",
            "source_franchise",
            "movie",
            "Japanese Manga Adaptations",
        ),
        (
            "High School",
            "place",
            "Slice of Life",
            "subgenre",
            "tv",
            "Slice-of-Life Shows Set in High School",
        ),
        (
            "Time Travel",
            "plot_device",
            "Sci-Fi",
            "subgenre",
            "movie",
            "Time-Travel Sci-Fi Movies",
        ),
        (
            "Japanese",
            "origin",
            "Mecha",
            "subgenre",
            "tv",
            "Japanese Mecha Shows",
        ),
        (
            "CG Animation",
            "style",
            "Family Movie Night",
            "audience",
            "movie",
            "CG-Animated Family Movies",
        ),
        (
            "Detective",
            "plot_device",
            "Crime",
            "subgenre",
            "tv",
            "Detective Crime Shows",
        ),
        (
            "CG Animation",
            "style",
            "Family",
            "subgenre",
            "movie",
            "CG-Animated Family Movies",
        ),
        (
            "Detective",
            "plot_device",
            "Mystery",
            "subgenre",
            "tv",
            "Detective Mysteries",
        ),
        (
            "Japanese",
            "origin",
            "Slice of Life",
            "subgenre",
            "movie",
            "Japanese Slice-of-Life Movies",
        ),
        (
            "Corruption of Power",
            "theme",
            "Crime",
            "subgenre",
            "tv",
            "Crime Shows About Corruption of Power",
        ),
        (
            "Adult Animation",
            "audience",
            "Animation",
            "subgenre",
            "movie",
            "Adult Animated Movies",
        ),
        (
            "British",
            "origin",
            "Historical Drama",
            "subgenre",
            "tv",
            "British Historical Dramas",
        ),
        (
            "Stop-Motion",
            "style",
            "Animation",
            "subgenre",
            "movie",
            "Stop-Motion Animated Movies",
        ),
        (
            "Multi-Season Saga",
            "pacing_format",
            "Sitcom",
            "subgenre",
            "tv",
            "Long-Running Sitcoms",
        ),
        (
            "Serial Killer",
            "plot_device",
            "Horror",
            "subgenre",
            "movie",
            "Serial-Killer Horror Movies",
        ),
        (
            "1980s",
            "release_decade",
            "Multi-Season Saga",
            "pacing_format",
            "tv",
            "Long-Running Shows from the 1980s",
        ),
        (
            "Christmas",
            "seasonal",
            "Made for Television",
            "subgenre",
            "movie",
            "Made-for-TV Christmas Movies",
        ),
        (
            "Based on a Book",
            "source_franchise",
            "Mystery",
            "subgenre",
            "tv",
            "Mysteries Based on Books",
        ),
        (
            "2000s",
            "release_decade",
            "Sitcom",
            "subgenre",
            "tv",
            "Sitcoms from the 2000s",
        ),
        (
            "French",
            "origin",
            "Paris",
            "place",
            "movie",
            "French Movies Set in Paris",
        ),
        ("Dark", "mood", "Fantasy", "subgenre", "tv", "Dark Fantasy Shows"),
        ("Heist", "plot_device", "Crime", "subgenre", "movie", "Heist Movies"),
        (
            "2000s",
            "release_decade",
            "Sci-Fi",
            "subgenre",
            "tv",
            "Sci-Fi Shows from the 2000s",
        ),
        (
            "Indian",
            "origin",
            "Epic Runtime",
            "pacing_format",
            "movie",
            "Epic-Length Indian Movies",
        ),
        (
            "Comic Book Adaptation",
            "source_franchise",
            "Action",
            "subgenre",
            "movie",
            "Comic Book Action Movies",
        ),
        (
            "1990s",
            "release_decade",
            "Hong Kong",
            "origin",
            "movie",
            "Hong Kong Movies from the 1990s",
        ),
        (
            "Undercover",
            "plot_device",
            "Crime",
            "subgenre",
            "movie",
            "Undercover Crime Movies",
        ),
        (
            "World War II",
            "setting_period",
            "History",
            "subgenre",
            "movie",
            "World War II Historical Movies",
        ),
        (
            "Love Triangle",
            "plot_device",
            "Romance",
            "subgenre",
            "movie",
            "Love-Triangle Romance Movies",
        ),
        (
            "Serial Killer",
            "plot_device",
            "Thriller",
            "subgenre",
            "movie",
            "Serial-Killer Thrillers",
        ),
        (
            "Outer Space",
            "place",
            "Adventure",
            "subgenre",
            "movie",
            "Outer-Space Adventure Movies",
        ),
        (
            "Hostage Crisis",
            "plot_device",
            "Thriller",
            "subgenre",
            "movie",
            "Hostage Thrillers",
        ),
        (
            "Coming of Age",
            "theme",
            "High School",
            "place",
            "movie",
            "High-School Coming-of-Age Movies",
        ),
        (
            "Time Travel",
            "plot_device",
            "Fantasy",
            "subgenre",
            "movie",
            "Time-Travel Fantasy Movies",
        ),
        (
            "Fame",
            "theme",
            "Music",
            "subgenre",
            "movie",
            "Music Movies About Fame",
        ),
        (
            "Sequel",
            "source_franchise",
            "Adventure",
            "subgenre",
            "movie",
            "Adventure Sequels",
        ),
        (
            "Manga Adaptation",
            "source_franchise",
            "Fantasy",
            "subgenre",
            "movie",
            "Manga Fantasy Movies",
        ),
        ("Dark", "mood", "Horror", "subgenre", "movie", "Dark Horror Movies"),
        (
            "Undercover",
            "plot_device",
            "Action",
            "subgenre",
            "movie",
            "Undercover Action Movies",
        ),
        (
            "Canadian",
            "origin",
            "Made for Television",
            "subgenre",
            "movie",
            "Canadian Made-for-TV Movies",
        ),
        (
            "Based on a True Story",
            "source_franchise",
            "History",
            "subgenre",
            "movie",
            "True-Story Historical Movies",
        ),
        (
            "1960s",
            "release_decade",
            "Based on a Book",
            "source_franchise",
            "movie",
            "Book Adaptations from the 1960s",
        ),
        (
            "1950s",
            "release_decade",
            "Based on a Book",
            "source_franchise",
            "movie",
            "Book Adaptations from the 1950s",
        ),
        (
            "1960s",
            "release_decade",
            "French",
            "origin",
            "movie",
            "French Movies from the 1960s",
        ),
        (
            "Friendship",
            "theme",
            "High School",
            "place",
            "movie",
            "High-School Friendship Movies",
        ),
        (
            "1990s",
            "release_decade",
            "Detective",
            "plot_device",
            "movie",
            "Detective Movies from the 1990s",
        ),
        (
            "Epic Runtime",
            "pacing_format",
            "War",
            "subgenre",
            "movie",
            "Epic-Length War Movies",
        ),
        (
            "British",
            "origin",
            "Music",
            "subgenre",
            "movie",
            "British Music Movies",
        ),
    ],
)
def test_production_pairs_receive_editorial_media_aware_names(
    tag_a, facet_a, tag_b, facet_b, media_type, expected
):
    name, _, _ = category_synth.name_for(tag_a, facet_a, tag_b, facet_b, media_type)

    assert name == expected


@pytest.mark.parametrize(
    ("tag_a", "facet_a", "tag_b", "facet_b"),
    [
        ("Romantic", "mood", "Romance", "subgenre"),
        ("Under 90 Minutes", "pacing_format", "Short Film", "subgenre"),
    ],
)
def test_semantically_redundant_pairs_are_not_rows(tag_a, facet_a, tag_b, facet_b):
    candidates = {
        1: (tag_a, facet_a, 100),
        2: (tag_b, facet_b, 100),
    }

    specs = category_synth._score_pairs(
        [(1, 2, 60)], candidates, corpus=400, media_type="movie"
    )

    assert specs == []


def test_an_unsupported_facet_pair_cannot_fill_the_row_quota():
    """Correlation alone does not make an intelligible browse category."""
    candidates = {
        1: ("Festival Favorite", "pedigree", 100),
        2: ("Christmas", "seasonal", 100),
    }

    specs = category_synth._score_pairs(
        [(1, 2, 60)], candidates, corpus=400, media_type="movie"
    )

    assert specs == []


# ---------------------------------------------------------------------------
# Editorial exclusions
# ---------------------------------------------------------------------------
def test_an_excluded_tag_cannot_anchor_a_row(db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "synth_excluded_tags", "Bleak", raising=False)
    _seed_library(db)

    specs = category_synth.propose(db, "movie", limit=40)

    assert all("Bleak" not in (s.tag_a, s.tag_b) for s in specs)


def test_an_excluded_word_cannot_appear_in_a_row_name(db, monkeypatch):
    """The safety net: a template could reintroduce a word whose tag is gone."""
    from app.config import settings

    monkeypatch.setattr(settings, "synth_excluded_words", "neo,bleak", raising=False)
    _seed_library(db)

    specs = category_synth.propose(db, "movie", limit=40)

    for spec in specs:
        lowered = spec.name.lower().replace("-", " ").split()
        assert "neo" not in lowered and "bleak" not in lowered, spec.name


def test_exclusions_are_configurable_rather_than_hardcoded(monkeypatch):
    """An operator's editorial choice must not need a redeploy to change."""
    from app.config import settings

    monkeypatch.setattr(settings, "synth_excluded_words", "", raising=False)
    assert category_synth._name_is_allowed("Anything At All") is True

    monkeypatch.setattr(settings, "synth_excluded_words", "anything", raising=False)
    assert category_synth._name_is_allowed("Anything At All") is False


def test_no_tag_anchors_more_than_two_rows_now(db):
    """Three Cyberpunk rows read as repetition, not variety."""
    _seed_library(db)

    specs = category_synth.propose(db, "movie", limit=60)

    counts: dict[str, int] = {}
    for spec in specs:
        counts[spec.tag_a] = counts.get(spec.tag_a, 0) + 1
        counts[spec.tag_b] = counts.get(spec.tag_b, 0) + 1
    assert all(n <= 2 for n in counts.values())
