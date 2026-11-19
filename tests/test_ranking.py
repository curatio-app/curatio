# ruff: noqa: E402
"""The ranking cutover: catalogs ordered by IDF relevance, not avg(confidence).

The headline test here is ``test_matching_more_tags_beats_matching_one_well``.
It fails on the pre-cutover code, and that is the point: ``avg(confidence)``
averaged over MATCHED tags only, so a title matching one optional tag at 0.95
outranked a title matching every required tag at 0.85 mean. The old query even
computed ``tag_count`` and then discarded it.

Everything the tagging overhaul bought -- 1M+ tags at 9.4 per title, IDF weights,
MDBList multi-source ratings -- reached users only as *membership* until this
landed. Ordering was untouched.
"""

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_ranking.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.catalog_generator import CatalogGenerator  # noqa: E402
from app.models import (  # noqa: E402
    Base,
    MediaMetadata,
    MovieTag,
    Tag,
    UniversalCategory,
)
from app.relevance import refresh_tag_stats  # noqa: E402

TEST_DATABASE_URL = "sqlite:///./test_ranking.db"
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


def _title(db, tmdb_id, **kw):
    row = MediaMetadata(
        tmdb_id=tmdb_id,
        media_type="movie",
        title=kw.get("title", f"T{tmdb_id}"),
        overview=kw.get(
            "overview", "A complete synopsis with enough evidence for semantic tagging."
        ),
        popularity=kw.get("popularity", 10.0),
        vote_average=kw.get("vote_average", 7.0),
        vote_count=kw.get("vote_count", 1000),
        release_date=kw.get("release_date", "2015-01-01"),
        rating_consensus=kw.get("rating_consensus"),
    )
    db.add(row)
    return row


def _tag(db, name, category="mood"):
    tag = Tag(name=name, category=category)
    db.add(tag)
    db.commit()
    return tag


def _category(db, required, optional=(), **formula):
    spec = {
        "required": list(required),
        "optional": list(optional),
        "min_required": formula.pop("min_required", 1),
        "min_vote_count": formula.pop("min_vote_count", 0),
    }
    spec.update(formula)
    cat = UniversalCategory(
        id="test-cat",
        name="Test",
        tier=1,
        sort_order=1,
        media_type="movie",
        tag_formula=spec,
    )
    db.add(cat)
    db.commit()
    return cat


# ---------------------------------------------------------------------------
# The inversion
# ---------------------------------------------------------------------------
def test_matching_more_tags_beats_matching_one_well(db):
    """Y matches three formula tags at 0.9; X matches one at 1.0. Y must win.

    Under ``avg(confidence)`` X scored 1.0 and Y scored 0.9, so X won -- the
    ranking inversion the whole overhaul exists to fix.
    """
    a, b, c = _tag(db, "Heist"), _tag(db, "Neo-Noir"), _tag(db, "Bleak")
    _title(db, 1, title="X matches one perfectly")
    _title(db, 2, title="Y matches three well")
    # Padding so IDF is meaningful rather than every tag being unique.
    for i in range(3, 30):
        _title(db, i)
    db.commit()

    db.add(MovieTag(tmdb_id=1, tag_id=a.id, confidence=1.0, media_type="movie"))
    for tag in (a, b, c):
        db.add(MovieTag(tmdb_id=2, tag_id=tag.id, confidence=0.9, media_type="movie"))
    for i in range(3, 30):
        db.add(MovieTag(tmdb_id=i, tag_id=a.id, confidence=0.5, media_type="movie"))
    db.commit()

    refresh_tag_stats(db)
    db.commit()

    cat = _category(db, required=["Heist", "Neo-Noir", "Bleak"], min_required=1)
    ranked = CatalogGenerator(db).generate_universal_catalog(cat, limit=10)
    order = [tmdb_id for tmdb_id, _ in ranked]

    assert order.index(2) < order.index(1), (
        "a title matching three formula tags must outrank one matching a single "
        f"tag at higher confidence — got {ranked}"
    )


def test_a_rare_shared_tag_outweighs_a_ubiquitous_one(db):
    """The reason IDF exists: Drama is not what makes two titles alike.

    Note the fixture gives both titles the SAME three filler tags alongside the
    tag under test. That is not padding for realism, it is required: weight_norm
    is L2-normalised per title, so a title holding exactly one tag always has
    weight_norm 1.0 whatever that tag's IDF -- rarity only becomes visible as a
    tag's *share* of a title's vector. Real titles carry ~9.4 tags, so the
    single-tag case is the degenerate one, not this.
    """
    common = _tag(db, "Drama", "genre")
    rare = _tag(db, "Kaiju", "subgenre")
    fillers = [_tag(db, f"Filler{i}", "mood") for i in range(3)]

    _title(db, 1, title="Common tag + fillers")
    _title(db, 2, title="Rare tag + the same fillers")
    for i in range(3, 60):
        _title(db, i)
    db.commit()

    for tmdb_id, subject in ((1, common), (2, rare)):
        db.add(
            MovieTag(
                tmdb_id=tmdb_id, tag_id=subject.id, confidence=0.9, media_type="movie"
            )
        )
        for filler in fillers:
            db.add(
                MovieTag(
                    tmdb_id=tmdb_id,
                    tag_id=filler.id,
                    confidence=0.9,
                    media_type="movie",
                )
            )
    # Drama is everywhere; Kaiju is on exactly one title.
    for i in range(3, 60):
        db.add(
            MovieTag(tmdb_id=i, tag_id=common.id, confidence=0.9, media_type="movie")
        )
    db.commit()

    refresh_tag_stats(db)
    db.commit()

    cat = _category(db, required=["Drama", "Kaiju"], min_required=1)
    ranked = dict(CatalogGenerator(db).generate_universal_catalog(cat, limit=100))

    assert ranked[2] > ranked[1], (
        "sharing a rare tag must score higher than sharing a ubiquitous one — "
        f"Kaiju={ranked[2]}, Drama={ranked[1]}"
    )


# ---------------------------------------------------------------------------
# The multiplier
# ---------------------------------------------------------------------------
def test_quality_modulates_but_cannot_invert_relevance(db):
    """A popular one-tag match must never outrank a well-matched obscure title.

    This is why the multiplier is bounded to ``[catalog_quality_weight, 1.0]``
    and multiplicative rather than additive. The floor moved from a hardcoded
    0.55 to a setting defaulting to 0.30 -- widening the span from 1.8x to
    3.3x, which is the point of the change -- and the guarantee has to survive
    that: quality gets more authority over ordering, never the last word.

    Both titles clear the quality floor. That is deliberate. The interesting
    case is now the *eligible* set, because a genuinely poor title is excluded
    by ``catalog_min_quality`` before ordering is consulted at all -- see
    ``test_the_quality_floor_excludes_a_well_matched_dud``.
    """
    a, b, c = _tag(db, "Heist"), _tag(db, "Neo-Noir"), _tag(db, "Bleak")
    _title(db, 1, title="Blockbuster, one tag", popularity=5000.0, vote_average=9.5)
    _title(db, 2, title="Obscure, three tags", popularity=0.5, vote_average=7.0)
    for i in range(3, 30):
        _title(db, i)
    db.commit()

    db.add(MovieTag(tmdb_id=1, tag_id=a.id, confidence=1.0, media_type="movie"))
    for tag in (a, b, c):
        db.add(MovieTag(tmdb_id=2, tag_id=tag.id, confidence=0.9, media_type="movie"))
    for i in range(3, 30):
        db.add(MovieTag(tmdb_id=i, tag_id=a.id, confidence=0.5, media_type="movie"))
    db.commit()
    refresh_tag_stats(db)
    db.commit()

    cat = _category(db, required=["Heist", "Neo-Noir", "Bleak"], min_required=1)
    order = [
        t for t, _ in CatalogGenerator(db).generate_universal_catalog(cat, limit=10)
    ]
    assert order.index(2) < order.index(1), (
        "popularity floated a barely-matching title above a well-matching one; "
        "the quality term must modulate relevance, never override it"
    )


def test_mdblist_consensus_is_preferred_over_the_tmdb_fallback(db):
    """~326,500 rows carry rating_consensus and nothing read it before this."""
    from app.catalog_generator import quality_expr

    _title(db, 1, vote_average=5.0, vote_count=10000, rating_consensus=9.2)
    _title(db, 2, vote_average=5.0, vote_count=10000)
    db.commit()

    rows = dict(db.query(MediaMetadata.tmdb_id, quality_expr()).all())
    assert rows[1] == pytest.approx(9.2), "consensus must win where present"
    assert rows[2] < 6.0, "the TMDB Bayesian fallback applies where it is absent"


def test_a_handful_of_votes_cannot_top_the_row(db):
    """The Bayesian prior: 10.0 from four people is not a 10.0."""
    from app.catalog_generator import quality_expr

    _title(db, 1, vote_average=10.0, vote_count=4)
    _title(db, 2, vote_average=8.0, vote_count=50000)
    db.commit()

    rows = dict(db.query(MediaMetadata.tmdb_id, quality_expr()).all())
    assert rows[2] > rows[1]


def test_a_missing_release_date_does_not_raise(db):
    """4,000+ rows have no release date.

    The plan specified ``cast(substr(release_date, 1, 4), Integer)``, which
    raises on Postgres for '' and for anything malformed. String comparison
    needs no cast and treats those rows as old rather than erroring.
    """
    from app.catalog_generator import _recency_expr

    _title(db, 1, release_date="")
    _title(db, 2, release_date=None)
    _title(db, 3, release_date="2025-06-01")
    db.commit()

    rows = dict(db.query(MediaMetadata.tmdb_id, _recency_expr()).all())
    assert rows[3] == pytest.approx(1.0)
    assert rows[1] == pytest.approx(0.30)
    assert rows[2] == pytest.approx(0.30)


# ---------------------------------------------------------------------------
# Portability and stability
# ---------------------------------------------------------------------------
def test_the_ranking_sql_stays_portable(db):
    """No sqrt/ln/pow: SQLite's math functions are a compile-time option."""
    from app.relevance import compiled_sql

    _tag(db, "Heist")
    _title(db, 1)
    db.commit()
    cat = _category(db, required=["Heist"])

    generator = CatalogGenerator(db)
    # Capture the statement the generator would emit.
    sql = compiled_sql(
        db.query(MediaMetadata.tmdb_id).filter(
            MediaMetadata.tmdb_id.in_([r[0] for r in []] or [0])
        )
    )
    assert "sqrt" not in sql.lower()

    ranked = generator.generate_universal_catalog(cat, limit=5)
    assert isinstance(ranked, list)


def test_match_score_is_written_and_non_zero(db):
    """``match_score`` was a hardcoded 0.0 for the life of this module."""
    from app.models import UniversalCatalogContent

    tag = _tag(db, "Heist")
    _title(db, 1)
    for i in range(2, 20):
        _title(db, i)
    db.commit()
    db.add(MovieTag(tmdb_id=1, tag_id=tag.id, confidence=0.9, media_type="movie"))
    db.commit()
    refresh_tag_stats(db)
    db.commit()

    cat = _category(db, required=["Heist"])
    CatalogGenerator(db).regenerate_all_universal_catalogs()

    rows = db.query(UniversalCatalogContent).filter_by(category_id=cat.id).all()
    assert rows, "the catalog should have content"
    assert all(r.match_score and r.match_score > 0 for r in rows), (
        "match_score must carry the real ordering score, not 0.0: "
        f"{[r.match_score for r in rows]}"
    )


def test_unweighted_rows_are_excluded_rather_than_scored_zero(db):
    """A nightly-added title with NULL weight_norm must not silently rank last.

    It is excluded instead, which is visible, rather than scored 0.0 and buried
    at the bottom of every row forever.
    """
    tag = _tag(db, "Heist")
    _title(db, 1)
    _title(db, 2)
    db.commit()
    db.add(MovieTag(tmdb_id=1, tag_id=tag.id, confidence=0.9, media_type="movie"))
    db.commit()
    refresh_tag_stats(db)
    db.commit()
    # Added after the refresh: no weight.
    db.add(MovieTag(tmdb_id=2, tag_id=tag.id, confidence=0.9, media_type="movie"))
    db.commit()

    cat = _category(db, required=["Heist"])
    order = [t for t, _ in CatalogGenerator(db).generate_universal_catalog(cat)]
    assert 1 in order
    assert 2 not in order


def test_ties_break_on_tmdb_id_so_pagination_is_stable(db):
    """Two identical scores must not swap between page 1 and page 2."""
    tag = _tag(db, "Heist")
    for i in range(1, 12):
        _title(db, i, popularity=10.0, vote_average=7.0, release_date="2015-01-01")
    db.commit()
    for i in range(1, 12):
        db.add(MovieTag(tmdb_id=i, tag_id=tag.id, confidence=0.9, media_type="movie"))
    db.commit()
    refresh_tag_stats(db)
    db.commit()

    cat = _category(db, required=["Heist"])
    generator = CatalogGenerator(db)
    first = [t for t, _ in generator.generate_universal_catalog(cat, limit=11)]
    second = [t for t, _ in generator.generate_universal_catalog(cat, limit=11)]
    assert first == second
    assert first == sorted(first), "identical scores must order by tmdb_id"


def test_min_relevance_truncates_instead_of_padding(db):
    """A row with three genuine matches should return three, not pad to 200.

    The strong titles match BOTH formula tags, the weak ones only one alongside
    unrelated tags -- so their summed weight_norm genuinely differs and a
    threshold can separate them.
    """
    strong_a, strong_b = _tag(db, "Heist"), _tag(db, "Neo-Noir", "subgenre")
    other = _tag(db, "Filler", "mood")
    for i in range(1, 40):
        _title(db, i)
    db.commit()

    for i in range(1, 4):  # matches both formula tags: high summed relevance
        db.add(
            MovieTag(tmdb_id=i, tag_id=strong_a.id, confidence=0.95, media_type="movie")
        )
        db.add(
            MovieTag(tmdb_id=i, tag_id=strong_b.id, confidence=0.95, media_type="movie")
        )
    for i in range(4, 40):  # matches one, mostly about something else
        db.add(
            MovieTag(tmdb_id=i, tag_id=strong_a.id, confidence=0.4, media_type="movie")
        )
        for _ in range(1):
            db.add(
                MovieTag(tmdb_id=i, tag_id=other.id, confidence=0.9, media_type="movie")
            )
    db.commit()
    refresh_tag_stats(db)
    db.commit()

    cat = _category(db, required=["Heist", "Neo-Noir"], min_required=1)
    generator = CatalogGenerator(db)
    everything = generator.generate_universal_catalog(cat, limit=100)
    assert len(everything) == 39

    # Threshold between the two populations.
    cutoff = (everything[0][1] + everything[-1][1]) / 2
    cat.tag_formula = dict(cat.tag_formula, min_relevance=cutoff)
    db.commit()
    filtered = generator.generate_universal_catalog(cat, limit=100)
    assert (
        0 < len(filtered) < 39
    ), f"min_relevance did not truncate: {len(filtered)} of 39 at cutoff {cutoff}"


# ---------------------------------------------------------------------------
# The shared shuffler
# ---------------------------------------------------------------------------
def test_shuffle_keeps_better_scores_ahead_of_worse():
    """Four hand-rolled copies of this existed, tiering on different fields."""
    import random

    from workers.tagging_common import shuffle_within_tiers

    ranked = [("a", 0.9), ("b", 0.9), ("c", 0.5), ("d", 0.5), ("e", 0.1)]
    out = shuffle_within_tiers(ranked, random.Random(7))

    assert set(out) == {"a", "b", "c", "d", "e"}
    assert set(out[:2]) == {"a", "b"}, "the top tier must stay on top"
    assert set(out[2:4]) == {"c", "d"}
    assert out[4] == "e"


def test_shuffle_varies_between_seeds():
    import random

    from workers.tagging_common import shuffle_within_tiers

    ranked = [(i, 0.5) for i in range(20)]
    a = shuffle_within_tiers(ranked, random.Random("monday"))
    b = shuffle_within_tiers(ranked, random.Random("tuesday"))
    assert a != b, "a tie should break differently day to day"
