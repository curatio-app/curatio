"""
Tests for the IDF relevance engine.

Two assertions carry most of the weight here:

* :func:`test_rare_tag_outranks_ubiquitous_tag` -- sharing a rare tag must beat
  sharing a common one. Under the old count/avg scoring a shared "Drama" counted
  exactly as much as a shared "Cosmic Horror", which is why every profile's
  top-15 taste tags converged on the same handful.
* :func:`test_similarity_sql_has_no_math_functions` -- the emitted SQL must not
  contain sqrt, ln, pow or EXTRACT. SQLite compiles those in only when built
  with SQLITE_ENABLE_MATH_FUNCTIONS, so a query using them would pass here and
  fail on a self-hoster's machine.
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_relevance.db")
os.environ.setdefault("TMDB_API_KEY", "test")
os.environ.setdefault("GEMINI_API_KEY", "test")
os.environ.setdefault("MASTER_PASSWORD", "test")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SECRET_KEY", "t" * 40)
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app import relevance as rel  # noqa: E402
from app.models import (  # noqa: E402
    Base,
    MediaMetadata,
    MediaTagVector,
    MovieTag,
    Tag,
    TagStats,
)

TEST_DATABASE_URL = "sqlite:///./test_relevance.db"
engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

COMMON = "CommonTag"
RARE = "RareTag"
OTHER = "OtherTag"


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def _corpus(db, common_docs: int = 190, rare_docs: int = 5, total: int = 200):
    """200 movies: COMMON on 190, RARE on 5, OTHER on everything.

    OTHER exists so every document has at least two tags and a non-degenerate
    norm.

    Every title also gets a ``media_metadata`` row clearing the catalog quality
    floor. That is not scaffolding for the sake of it: ``similar_by_vector`` and
    ``browse_by_tags`` both apply the floor now, and in production a title
    cannot be tagged without metadata anyway -- ``tagging_common.build_features``
    reads it. A tag-only fixture would be testing a state the system cannot
    reach, and would go green if the floor were accidentally removed.
    """
    tags = {}
    for name in (COMMON, RARE, OTHER):
        tag = Tag(name=name, category="mood")
        db.add(tag)
        db.flush()
        tags[name] = tag.id

    for tmdb_id in range(1, total + 1):
        db.add(
            MediaMetadata(
                tmdb_id=tmdb_id,
                media_type="movie",
                title=f"T{tmdb_id}",
                overview="A complete synopsis with enough evidence for semantic tagging.",
                popularity=10.0,
                vote_average=7.0,
                vote_count=1000,
                release_date="2015-01-01",
            )
        )
        db.add(
            MovieTag(
                tmdb_id=tmdb_id,
                media_type="movie",
                tag_id=tags[OTHER],
                confidence=0.9,
            )
        )
        if tmdb_id <= common_docs:
            db.add(
                MovieTag(
                    tmdb_id=tmdb_id,
                    media_type="movie",
                    tag_id=tags[COMMON],
                    confidence=0.9,
                )
            )
        if tmdb_id <= rare_docs:
            db.add(
                MovieTag(
                    tmdb_id=tmdb_id,
                    media_type="movie",
                    tag_id=tags[RARE],
                    confidence=0.9,
                )
            )
    db.commit()
    return tags


# ---------------------------------------------------------------------------
# IDF
# ---------------------------------------------------------------------------
def test_compute_idf_rewards_rarity():
    assert rel.compute_idf(5, 200) > rel.compute_idf(190, 200)


def test_compute_idf_is_clamped():
    assert rel.compute_idf(1, 10**9) == rel.IDF_MAX
    assert rel.IDF_MIN <= rel.compute_idf(0, 0) <= rel.IDF_MAX


def test_idf_lower_clamp_is_unreachable_from_valid_inputs():
    """ln((N+1)/(df+1)) + 1 is >= 1.0 whenever df <= N.

    IDF_MIN is therefore a defensive floor, not an operating bound. Pinning it
    here so a future formula change that starts producing sub-1.0 values is a
    deliberate decision rather than a silent one.
    """
    assert rel.compute_idf(1000, 1000) == 1.0
    for corpus in (1, 10, 1000, 200_000):
        for docs in (0, 1, corpus // 2, corpus):
            assert rel.compute_idf(docs, corpus) >= 1.0


def test_idf_is_higher_for_the_rare_tag(db):
    tags = _corpus(db)
    rel.refresh_tag_stats(db)

    stats = {
        row.tag_id: row.idf
        for row in db.query(TagStats).filter(TagStats.media_type == "movie").all()
    }
    assert stats[tags[RARE]] > stats[tags[COMMON]]


def test_idf_is_media_type_scoped(db):
    """A 4.5:1 corpus ratio is why this is not a single global statistic."""
    movie_tag = Tag(name="Shared", category="mood")
    db.add(movie_tag)
    db.flush()
    # Common in movies (18/20), rare in TV (1/20).
    for tmdb_id in range(1, 21):
        db.add(
            MovieTag(
                tmdb_id=tmdb_id, media_type="movie", tag_id=movie_tag.id, confidence=0.9
            )
            if tmdb_id <= 18
            else MovieTag(
                tmdb_id=tmdb_id,
                media_type="movie",
                tag_id=movie_tag.id,
                confidence=0.9,
            )
        )
    for tmdb_id in range(1, 21):
        if tmdb_id == 1:
            db.add(
                MovieTag(
                    tmdb_id=tmdb_id,
                    media_type="tv",
                    tag_id=movie_tag.id,
                    confidence=0.9,
                )
            )
    # Give TV a filler tag so its corpus is 20 titles, not 1.
    filler = Tag(name="Filler", category="mood")
    db.add(filler)
    db.flush()
    for tmdb_id in range(1, 21):
        db.add(
            MovieTag(tmdb_id=tmdb_id, media_type="tv", tag_id=filler.id, confidence=0.9)
        )
    db.commit()

    rel.refresh_tag_stats(db)

    movie_idf = (
        db.query(TagStats.idf)
        .filter(TagStats.tag_id == movie_tag.id, TagStats.media_type == "movie")
        .scalar()
    )
    tv_idf = (
        db.query(TagStats.idf)
        .filter(TagStats.tag_id == movie_tag.id, TagStats.media_type == "tv")
        .scalar()
    )
    assert (
        tv_idf > movie_idf
    ), "the same tag must be rarer in the corpus where it is rare"


# ---------------------------------------------------------------------------
# Weights and norms
# ---------------------------------------------------------------------------
def test_weights_are_populated(db):
    _corpus(db)
    rel.refresh_tag_stats(db)
    assert db.query(MovieTag).filter(MovieTag.weight.is_(None)).count() == 0
    assert db.query(MovieTag).filter(MovieTag.weight_norm.is_(None)).count() == 0


def test_document_vectors_are_unit_length(db):
    """SUM(weight_norm^2) == 1 for every document."""
    _corpus(db)
    rel.refresh_tag_stats(db)

    rows = (
        db.query(
            MovieTag.tmdb_id,
            func.sum(MovieTag.weight_norm * MovieTag.weight_norm),
        )
        .filter(MovieTag.media_type == "movie")
        .group_by(MovieTag.tmdb_id)
        .all()
    )
    assert rows
    for _tmdb_id, sum_squares in rows:
        assert math.isclose(float(sum_squares), 1.0, rel_tol=1e-6)


def test_norms_are_stored(db):
    _corpus(db)
    rel.refresh_tag_stats(db)
    vectors = db.query(MediaTagVector).all()
    assert len(vectors) == 200
    assert all(v.norm > 0 for v in vectors)
    assert all(v.tag_count >= 1 for v in vectors)


def test_refresh_is_idempotent(db):
    _corpus(db)
    rel.refresh_tag_stats(db)
    first = {(r.tmdb_id, r.tag_id): r.weight_norm for r in db.query(MovieTag).all()}
    rel.refresh_tag_stats(db)
    second = {(r.tmdb_id, r.tag_id): r.weight_norm for r in db.query(MovieTag).all()}
    assert first.keys() == second.keys()
    for key in first:
        assert math.isclose(first[key], second[key], rel_tol=1e-9)


def test_refresh_on_empty_database_is_a_noop(db):
    counts = rel.refresh_tag_stats(db)
    assert counts["tags"] == 0
    assert db.query(TagStats).count() == 0


def test_refresh_scoped_to_one_media_type(db):
    _corpus(db)
    counts = rel.refresh_tag_stats(db, media_type="movie")
    assert counts["tags"] > 0
    assert db.query(TagStats).filter(TagStats.media_type == "tv").count() == 0


def test_targeted_refresh_weights_only_selected_titles(db):
    """Shelf weighting must be proportional to new titles, not corpus size."""
    first = Tag(name="Targeted First", category="mood")
    second = Tag(name="Targeted Second", category="theme")
    db.add_all([first, second])
    db.flush()
    db.add_all(
        [
            TagStats(
                tag_id=first.id,
                media_type="movie",
                doc_count=10,
                corpus_size=100,
                idf=2.0,
            ),
            TagStats(
                tag_id=second.id,
                media_type="movie",
                doc_count=5,
                corpus_size=100,
                idf=3.0,
            ),
            MovieTag(
                tmdb_id=1,
                media_type="movie",
                tag_id=first.id,
                confidence=0.5,
            ),
            MovieTag(
                tmdb_id=1,
                media_type="movie",
                tag_id=second.id,
                confidence=0.5,
            ),
            MovieTag(
                tmdb_id=2,
                media_type="movie",
                tag_id=first.id,
                confidence=0.9,
                weight=7.0,
                weight_norm=0.25,
            ),
            MediaTagVector(
                tmdb_id=2,
                media_type="movie",
                norm=9.0,
                tag_count=1,
            ),
        ]
    )
    db.commit()

    counts = rel.refresh_item_vectors(db, [(1, "movie")])
    db.expire_all()

    target = (
        db.query(MovieTag)
        .filter(MovieTag.tmdb_id == 1, MovieTag.media_type == "movie")
        .order_by(MovieTag.tag_id)
        .all()
    )
    assert counts == {"rows": 2, "vectors": 1}
    assert [row.weight for row in target] == [1.0, 1.5]
    assert math.isclose(
        sum(float(row.weight_norm) ** 2 for row in target), 1.0, rel_tol=1e-9
    )
    target_vector = (
        db.query(MediaTagVector).filter_by(tmdb_id=1, media_type="movie").one()
    )
    assert math.isclose(target_vector.norm, math.sqrt(3.25), rel_tol=1e-9)

    unrelated = (
        db.query(MovieTag)
        .filter_by(tmdb_id=2, media_type="movie", tag_id=first.id)
        .one()
    )
    unrelated_vector = (
        db.query(MediaTagVector).filter_by(tmdb_id=2, media_type="movie").one()
    )
    assert (unrelated.weight, unrelated.weight_norm) == (7.0, 0.25)
    assert unrelated_vector.norm == 9.0


# ---------------------------------------------------------------------------
# Similarity -- the point of the exercise
# ---------------------------------------------------------------------------
def test_rare_tag_outranks_ubiquitous_tag(db):
    """Sharing only RARE must beat sharing only COMMON.

    Title 1 holds OTHER + COMMON + RARE.
    Title 2 holds OTHER + COMMON + RARE  -> shares the rare tag.
    Title 100 holds OTHER + COMMON       -> shares only the common one.
    """
    _corpus(db)
    rel.refresh_tag_stats(db)

    results = dict(rel.similar_by_vector(db, 1, "movie", limit=200, min_shared_tags=1))

    assert 2 in results and 100 in results
    assert (
        results[2] > results[100]
    ), "a shared rare tag must outweigh a shared ubiquitous tag"


def test_similarity_is_bounded(db):
    _corpus(db)
    rel.refresh_tag_stats(db)
    for _tmdb_id, score in rel.similar_by_vector(
        db, 1, "movie", limit=200, min_shared_tags=1
    ):
        assert 0.0 <= score <= 1.0 + 1e-9


def test_similarity_excludes_the_reference(db):
    _corpus(db)
    rel.refresh_tag_stats(db)
    assert 1 not in dict(
        rel.similar_by_vector(db, 1, "movie", limit=200, min_shared_tags=1)
    )


def test_similarity_honours_exclude_ids(db):
    _corpus(db)
    rel.refresh_tag_stats(db)
    results = dict(
        rel.similar_by_vector(
            db, 1, "movie", limit=200, min_shared_tags=1, exclude_ids=[2, 3]
        )
    )
    assert 2 not in results and 3 not in results


def test_similarity_respects_media_type(db):
    _corpus(db)
    tag = db.query(Tag).filter(Tag.name == RARE).one()
    db.add(MovieTag(tmdb_id=999, media_type="tv", tag_id=tag.id, confidence=0.9))
    db.commit()
    rel.refresh_tag_stats(db)

    assert 999 not in dict(
        rel.similar_by_vector(db, 1, "movie", limit=200, min_shared_tags=1)
    )


def test_similarity_of_an_untagged_title_is_empty(db):
    _corpus(db)
    rel.refresh_tag_stats(db)
    assert rel.similar_by_vector(db, 99999, "movie") == []


def test_reference_tags_drop_ubiquitous_ones(db):
    """COMMON is on 95% of the corpus, so it must not drive similarity.

    Title 1 is given enough tags to clear SPARSE_TITLE_TAG_COUNT, since a
    sparsely-tagged title deliberately keeps everything.
    """
    tags = _corpus(db)
    fillers = []
    for index in range(5):
        tag = Tag(name=f"Filler{index}", category="mood")
        db.add(tag)
        db.flush()
        fillers.append(tag.id)
        # 20/200 = 10% of the corpus, comfortably under the 25% threshold.
        for tmdb_id in range(1, 21):
            db.add(
                MovieTag(
                    tmdb_id=tmdb_id,
                    media_type="movie",
                    tag_id=tag.id,
                    confidence=0.9,
                )
            )
    db.commit()
    rel.refresh_tag_stats(db)

    chosen = rel.reference_tag_ids(db, 1, "movie")

    assert len(chosen) >= rel.SPARSE_TITLE_TAG_COUNT - 1
    assert tags[RARE] in chosen
    assert tags[COMMON] not in chosen, "a tag on 95% of the corpus is not a signal"
    assert tags[OTHER] not in chosen, "a tag on 100% of the corpus is not a signal"


def test_reference_tags_keep_everything_for_a_sparse_title(db):
    """A title with few tags must not end up with none to match on."""
    _corpus(db)
    rel.refresh_tag_stats(db)
    common = db.query(Tag).filter(Tag.name == COMMON).one()
    db.add(MovieTag(tmdb_id=555, media_type="movie", tag_id=common.id, confidence=0.9))
    db.commit()
    assert rel.reference_tag_ids(db, 555, "movie") == [common.id]


def test_reference_tags_fall_back_when_every_tag_is_ubiquitous(db):
    """Imprecise candidates beat no candidates."""
    _corpus(db)
    for index in range(6):
        tag = Tag(name=f"Everywhere{index}", category="mood")
        db.add(tag)
        db.flush()
        for tmdb_id in range(1, 201):
            db.add(
                MovieTag(
                    tmdb_id=tmdb_id,
                    media_type="movie",
                    tag_id=tag.id,
                    confidence=0.9,
                )
            )
    db.commit()
    rel.refresh_tag_stats(db)

    # Title 100 holds only tags that are on the whole corpus.
    assert rel.reference_tag_ids(db, 100, "movie")


# ---------------------------------------------------------------------------
# Portability -- R7
# ---------------------------------------------------------------------------
FORBIDDEN = ("sqrt(", "ln(", "pow(", "power(", "extract(", "log(")


def _assert_portable(sql: str):
    lowered = sql.lower()
    found = [token for token in FORBIDDEN if token in lowered]
    assert not found, f"non-portable SQL functions in emitted query: {found}\n{sql}"


def test_similarity_sql_has_no_math_functions(db):
    """SQLite's sqrt/ln are a compile-time option -- never emit them."""
    _corpus(db)
    rel.refresh_tag_stats(db)

    from sqlalchemy.orm import aliased

    a = aliased(MovieTag)
    b = aliased(MovieTag)
    similarity = func.sum(a.weight_norm * b.weight_norm)
    query = (
        db.query(b.tmdb_id, similarity)
        .select_from(a)
        .join(b, b.tag_id == a.tag_id)
        .group_by(b.tmdb_id)
        .having(func.count(b.tag_id) >= 2)
        .order_by(similarity.desc())
    )
    _assert_portable(rel.compiled_sql(query))


def test_browse_sql_has_no_math_functions(db):
    query = (
        db.query(MovieTag.tmdb_id, func.sum(MovieTag.weight_norm))
        .filter(MovieTag.media_type == "movie", MovieTag.tag_id.in_([1, 2]))
        .group_by(MovieTag.tmdb_id)
        .having(func.count(func.distinct(MovieTag.tag_id)) >= 2)
    )
    _assert_portable(rel.compiled_sql(query))


def test_population_sql_has_no_math_functions(db):
    """The chunked UPDATEs use only * and / with a correlated subquery."""
    _corpus(db)
    rel.refresh_tag_stats(db)
    # The statements are built inline in refresh_tag_stats; assert the shape by
    # re-deriving the sum-of-squares aggregate that feeds the Python sqrt.
    query = db.query(
        MovieTag.tmdb_id, func.sum(MovieTag.weight * MovieTag.weight)
    ).group_by(MovieTag.tmdb_id)
    _assert_portable(rel.compiled_sql(query))


# ---------------------------------------------------------------------------
# Taste vectors
# ---------------------------------------------------------------------------
def test_taste_vector_is_normalized(db):
    _corpus(db)
    rel.refresh_tag_stats(db)
    vector = rel.build_taste_vector(db, [1, 2, 3], "movie")
    assert vector
    assert math.isclose(
        math.sqrt(sum(v * v for v in vector.values())), 1.0, rel_tol=1e-9
    )


def test_taste_vector_favours_rare_tags(db):
    """The convergence fix: a rare tag must dominate a ubiquitous one."""
    tags = _corpus(db)
    rel.refresh_tag_stats(db)
    vector = rel.build_taste_vector(db, [1, 2, 3, 4, 5], "movie")
    assert vector[tags[RARE]] > vector[tags[COMMON]]


def test_taste_vector_is_empty_without_history(db):
    _corpus(db)
    rel.refresh_tag_stats(db)
    assert rel.build_taste_vector(db, [], "movie") == {}


def test_taste_vector_accepts_weighted_pairs(db):
    _corpus(db)
    rel.refresh_tag_stats(db)
    plain = rel.build_taste_vector(db, [1, 2], "movie")
    weighted = rel.build_taste_vector(db, [(1, 1.0), (2, 1.0)], "movie")
    assert plain.keys() == weighted.keys()


def test_taste_vector_drops_tags_cancelled_by_negative_factors(db):
    """Negative factors may remove a tag but must never invert the ranking."""
    _corpus(db)
    rel.refresh_tag_stats(db)
    vector = rel.build_taste_vector(db, [(1, 1.0), (2, -1.0)], "movie")
    assert all(v > 0 for v in vector.values())


def test_taste_vector_is_truncated(db):
    _corpus(db)
    rel.refresh_tag_stats(db)
    assert len(rel.build_taste_vector(db, [1, 2, 3], "movie", top_n=2)) <= 2


def test_store_and_score_taste_vector(db):
    _corpus(db)
    rel.refresh_tag_stats(db)
    vector = rel.build_taste_vector(db, [1, 2, 3], "movie")

    from app.models import Account, User

    account = Account(email="t@example.com")
    db.add(account)
    db.flush()
    user = User(user_key="k", account_id=account.id)
    db.add(user)
    db.flush()

    stored = rel.store_taste_vector(db, user.id, "movie", vector)
    assert stored == len(vector)

    scored = rel.taste_scored_candidates(db, user.id, "movie", limit=10)
    assert scored
    assert all(score > 0 for _tmdb_id, score in scored)


# ---------------------------------------------------------------------------
# Browse
# ---------------------------------------------------------------------------
def test_browse_all_requires_every_tag(db):
    tags = _corpus(db)
    rel.refresh_tag_stats(db)
    results = rel.browse_by_tags(db, [tags[COMMON], tags[RARE]], "movie", mode="all")
    assert {tmdb_id for tmdb_id, _ in results} <= set(range(1, 6))


def test_browse_any_matches_either_tag(db):
    tags = _corpus(db)
    rel.refresh_tag_stats(db)
    results = rel.browse_by_tags(
        db, [tags[COMMON], tags[RARE]], "movie", mode="any", limit=500
    )
    assert len(results) >= 190


def test_browse_rewards_matching_more_tags(db):
    """The inversion fix, in miniature: more matches must score higher."""
    tags = _corpus(db)
    rel.refresh_tag_stats(db)
    scores = dict(
        rel.browse_by_tags(
            db, [tags[COMMON], tags[RARE]], "movie", mode="any", limit=500
        )
    )
    assert scores[1] > scores[100]


def test_browse_with_no_tags_returns_nothing(db):
    assert rel.browse_by_tags(db, [], "movie") == []


def test_browse_paginates(db):
    tags = _corpus(db)
    rel.refresh_tag_stats(db)
    page1 = rel.browse_by_tags(db, [tags[OTHER]], "movie", mode="any", limit=5)
    page2 = rel.browse_by_tags(
        db, [tags[OTHER]], "movie", mode="any", limit=5, offset=5
    )
    assert len(page1) == 5 and len(page2) == 5
    assert not ({i for i, _ in page1} & {i for i, _ in page2})
