"""
Catalog generator - builds Netflix-style categories from tag database.

This module generates both universal and personalized catalogs using SQL queries.
No Gemini calls needed - everything is pre-computed from tags!
"""

from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta
from sqlalchemy import func, and_, case, or_, tuple_
from sqlalchemy.orm import Session, defer
from loguru import logger

from app.time_utils import utc_now  # noqa: E402
from app.models import (
    Tag,
    MovieTag,
    UniversalCategory,
    UniversalCatalogContent,
    UserCatalog,
    UserCatalogContent,
    MediaMetadata,
)
from app.config import settings
from app.catalog_policy import is_home_mockbuster

#: Bayesian prior for the TMDB rating fallback: pull every title toward 6.5
#: until it has ~250 votes, so a 10.0 from four people cannot top a row.
_PRIOR_VOTES = 250.0
_PRIOR_MEAN = 6.5

#: popularity / (popularity + this). Bounded 0-1 with no log, which SQLite
#: cannot be relied on to provide.
_BUZZ_HALF_LIFE = 60.0

#: Personal slots whose contents come from a TMDB feed endpoint rather than
#: from this install's own tag data.
#:
#: The distinction matters because these are the only rows where the candidate
#: SET is an editorial claim about the world ("these are trending") while the
#: ORDER is just TMDB's popularity ranking -- a number computed for everyone
#: and therefore for no one. Every other personal slot is already ordered by
#: the viewer's own taste vector, so re-ranking those a second time would
#: destroy the tier shuffle they are deliberately given.
TMDB_SOURCED_SLOT_PREFIXES: Tuple[str, ...] = (
    "trending-",
    "new-releases-",
    "popular-",
    "coming-soon-",
)


def is_tmdb_sourced_slot(slot_id: Optional[str]) -> bool:
    """True for a personal slot filled from a TMDB feed endpoint."""
    return bool(slot_id) and str(slot_id).startswith(TMDB_SOURCED_SLOT_PREFIXES)


#: How far a title may climb in a feed row on the strength of taste and quality.
#:
#: The number that decides whether "Trending Now" still reads as trending. Too
#: small and the row is TMDB's, unchanged; too large and it is a taste row
#: wearing a trending label. Eight places re-arranges what a viewer sees on one
#: screen without letting the far tail reach the front.
_MAX_FEED_CLIMB = 8

#: Lower bound of the *positional* multiplier used when personalising a row
#: whose order already means something.
#:
#: Deliberately no longer tied to ``catalog_quality_weight``. That setting
#: decides how much authority quality has when ranking a row from scratch;
#: this one decides how far taste may move a title inside an order that was
#: already correct, and the guarantee it buys -- at most ``_MAX_FEED_CLIMB``
#: places -- has to stay fixed for the row to keep meaning what its title says.
_FEED_MULT_FLOOR = 0.55

#: Per-position decay chosen so the full multiplier span is worth exactly
#: _MAX_FEED_CLIMB places: decay ** climb == floor.
_FEED_POSITION_DECAY = _FEED_MULT_FLOOR ** (1.0 / _MAX_FEED_CLIMB)


def quality_floor_clauses(
    min_vote_count: Optional[int] = None,
    *,
    min_rating_sources: Optional[int] = None,
    min_quality: Optional[float] = None,
    niche_min_quality: Optional[float] = None,
    niche_min_vote_count: Optional[int] = None,
    niche_min_age_days: Optional[int] = None,
    as_of: Optional[datetime] = None,
) -> list:
    """The "is this worth showing anyone" predicate, as SQLAlchemy clauses.

    Returns a list to be ANDed against a ``MediaMetadata`` query. Empty when
    every floor is disabled, so a caller can skip the join entirely.

    Two separate questions, and the floor has to answer both:

    **1. Has anyone heard of this?** Two signals, combined as OR rather than
    AND, because they cover different populations:

    * **Rating-source count** is the better signal. MDBList records how many of
      IMDb, Rotten Tomatoes, Metacritic, Trakt and Letterboxd carry a title; it
      is 0 for exactly the ~27,800-title tail MDBList has no record of, all
      sitting near 0.4 popularity. Requiring five sources says what an operator
      means by "mainstream" far more directly than a vote threshold; the
      production pilot showed that two still admitted the weak TV tail.
    * **TMDB vote count** is universal -- every acquired row has it -- and is
      the fallback for a title MDBList was never asked about.

    ``mdb_version`` is what separates "never looked" from "looked and found
    nothing", because ``rating_source_count`` defaults to 0 for both:

    * NULL -- never enriched. Must clear the **vote** arm; we know nothing else.
    * ``1.1.0-absent`` -- MDBList looked and has no record. The obscure tail by
      construction, so its only route in is the vote arm as well.
    * ``1.1.0`` -- enriched, so the count means what it says.

    This used to be an AND of two arms where the source arm passed *anything*
    with a NULL ``mdb_version``. On a library the per-title pass has not
    finished, that reduced the whole floor to the vote count -- which defaulted
    to 10. Structuring it as OR lets the vote fallback be raised to something
    that actually excludes without emptying a key-less install.

    **2. Is it any good?** ``catalog_min_quality`` against :func:`quality_expr`,
    the same 0-10 score the row is ordered by. Nothing in the product asked
    this before: quality could nudge a title's position and never keep it out,
    so a widely-rated 4.8/10 cleared every gate here. This is the c-tier floor.
    """
    floors = []
    votes = (
        settings.catalog_min_vote_count if min_vote_count is None else min_vote_count
    )
    sources = (
        settings.catalog_min_rating_sources
        if min_rating_sources is None
        else min_rating_sources
    )
    minimum_quality = (
        settings.catalog_min_quality if min_quality is None else min_quality
    )
    niche_quality = (
        settings.catalog_niche_min_quality
        if niche_min_quality is None
        else niche_min_quality
    )
    niche_votes = (
        settings.catalog_niche_min_vote_count
        if niche_min_vote_count is None
        else niche_min_vote_count
    )
    niche_age_days = (
        settings.catalog_niche_min_age_days
        if niche_min_age_days is None
        else niche_min_age_days
    )

    # Either arm at 0 disables the pair, not just itself. They are OR-ed, so an
    # arm that constrains nothing satisfies the whole test for every row --
    # spelling that out is cheaper than the alternative, which is an operator
    # setting the vote floor to 0 on a library MDBList has not covered and
    # silently emptying every row in the product. To keep *only* what MDBList
    # vouches for, raise the vote floor rather than zeroing it.
    evidence = None
    if votes > 0 and sources > 0:
        from workers.mdb_sync import MDB_ABSENT_VERSION

        evidence = or_(
            and_(
                MediaMetadata.mdb_version.isnot(None),
                MediaMetadata.mdb_version != MDB_ABSENT_VERSION,
                func.coalesce(MediaMetadata.rating_source_count, 0) >= sources,
            ),
            MediaMetadata.vote_count >= votes,
        )
    elif votes > 0:
        evidence = MediaMetadata.vote_count >= votes

    quality = quality_expr()
    standard_parts = []
    if evidence is not None:
        standard_parts.append(evidence)
    if minimum_quality > 0:
        standard_parts.append(quality >= minimum_quality)

    standard = and_(*standard_parts) if standard_parts else None

    # Preserve exceptional older/niche work without reopening the junk tail.
    # This is intentionally stricter on quality, still requires meaningful
    # votes, and excludes recent releases that simply have not accumulated
    # evidence yet. ISO YYYY-MM-DD strings retain chronological sort order.
    niche = None
    if (
        standard is not None
        and minimum_quality > 0
        and niche_quality > 0
        and niche_votes > 0
    ):
        cutoff = ((as_of or utc_now()) - timedelta(days=niche_age_days)).date()
        cutoff_iso = cutoff.isoformat()
        niche = and_(
            quality >= niche_quality,
            func.coalesce(MediaMetadata.vote_count, 0) >= niche_votes,
            MediaMetadata.release_date.isnot(None),
            MediaMetadata.release_date <= cutoff_iso,
        )

    if standard is not None and niche is not None:
        floors.append(or_(standard, niche))
    elif standard is not None:
        floors.append(standard)
    elif niche is not None:
        floors.append(niche)
    if floors:
        # A rating can establish quality and visibility, but it cannot give the
        # tagger evidence for mood, theme or plot. Blank/placeholder synopses
        # produced generic two-mood guesses in the production pilot and should
        # neither consume AI budget nor enter a generated recommendation row.
        from app.tag_evidence import catalog_evidence_clause

        floors.append(catalog_evidence_clause())
    return floors


def quality_floor_ids(db: Session, media_type: str):
    """``quality_floor_clauses`` as a membership sub-select of tmdb_ids.

    The tag-scored rows join ``movie_tags`` alone, so the floor has to arrive
    as an ``IN`` test rather than as a WHERE clause on a joined table. Same
    predicate the universal rows use, so every tier of the product agrees about
    what is too obscure or too poor to show.

    Lived in ``workers/personalized_sync`` while three of its callers were in
    that module and two were not; ``app/relevance`` cannot import a worker, so
    it moved here beside the clauses it wraps.
    """
    clauses = quality_floor_clauses()
    query = db.query(MediaMetadata.tmdb_id).filter(
        MediaMetadata.media_type == media_type
    )
    if clauses:
        query = query.filter(*clauses)
    return query.subquery().select()


def quality_expr():
    """0-10 quality signal: MDBList consensus where we have it, TMDB otherwise.

    ``rating_consensus`` is a votes-weighted mean across IMDb, RT, Metacritic,
    Trakt, Letterboxd and friends, and requires ``mdblist_min_rating_sources``
    before it is written at all -- so a COALESCE onto it is a strict upgrade
    over TMDB's ``vote_average``, which is compressed into 6.5-7.5 and does
    almost no ranking work. ~326,500 titles carry it and, until this function
    existed, **nothing read it**.
    """
    votes = func.coalesce(MediaMetadata.vote_count, 0)
    bayesian = (
        func.coalesce(MediaMetadata.vote_average, 0.0) * votes
        + _PRIOR_MEAN * _PRIOR_VOTES
    ) / (votes + _PRIOR_VOTES)
    return func.coalesce(MediaMetadata.rating_consensus, bayesian)


def _quality_value(metadata) -> float:
    """``quality_expr`` in Python, for re-ranking rows already in memory.

    Deliberately the same arithmetic as the SQL version rather than an
    approximation of it: a feed row and a curated row that disagree about how
    good a title is would reintroduce, one layer down, exactly the split this
    is meant to close. Keep the two in step.
    """
    if metadata is None:
        return _PRIOR_MEAN
    consensus = getattr(metadata, "rating_consensus", None)
    if consensus is not None:
        return float(consensus)
    votes = float(metadata.vote_count or 0)
    average = float(metadata.vote_average or 0.0)
    return (average * votes + _PRIOR_MEAN * _PRIOR_VOTES) / (votes + _PRIOR_VOTES)


def _recency_value(
    release_date: Optional[str], today: Optional[datetime] = None
) -> float:
    """``_recency_expr`` in Python. Same ladder, same string comparison.

    Keep in step with the SQL version: a personal row and a universal row that
    disagreed about how old a title is would reintroduce, one layer down, the
    same split these helpers exist to close.
    """
    year = (today or utc_now()).year
    value = release_date or ""
    for offset, weight in ((2, 1.0), (5, 0.85), (10, 0.70), (20, 0.55), (40, 0.40)):
        if value >= str(year - offset):
            return weight
    return 0.30


def blended_value(
    relevance: float, metadata, today: Optional[datetime] = None
) -> float:
    """``_blended_score_expr`` in Python, for candidates already in memory.

    The cosine path ("Because You Watched") produces its ranking inside
    :mod:`app.relevance` rather than as a SQL aggregate, so it cannot be scored
    by the expression the other rows share. Scoring it here with the identical
    arithmetic is what stops that row drifting back into being ordered by
    relevance alone, which is how it came to be the least quality-aware row in
    the product.
    """
    if metadata is None:
        return float(relevance)
    popularity = float(getattr(metadata, "popularity", 0.0) or 0.0)
    buzz = popularity / (popularity + _BUZZ_HALF_LIFE) if popularity > 0 else 0.0
    quality = min(max(_quality_value(metadata) / 10.0, 0.0), 1.0)
    recency = _recency_value(getattr(metadata, "release_date", None), today)
    floor = settings.catalog_quality_weight
    span = 1.0 - floor
    modulation = (
        floor
        + span * _QUALITY_SHARE * quality
        + span * (1.0 - _QUALITY_SHARE) * (0.7 * buzz + 0.3 * recency)
    )
    return float(relevance) * modulation


def _recency_expr(today: Optional[datetime] = None):
    """0.3-1.0 by age, from lexicographic comparison on the date string.

    ``release_date`` is a VARCHAR(20) holding 'YYYY-MM-DD', so the plan's
    ``cast(substr(release_date, 1, 4), Integer)`` would raise on Postgres for
    any row holding '' or a malformed value -- and 4,000+ rows have no release
    date at all. String comparison needs no cast, treats NULL and '' as "very
    old" via the else branch rather than erroring, and is identical on SQLite
    and Postgres. A six-step ladder is indistinguishable from a continuous
    curve once it is only used to *order* rows.
    """
    year = (today or utc_now()).year
    return case(
        (MediaMetadata.release_date >= str(year - 2), 1.0),
        (MediaMetadata.release_date >= str(year - 5), 0.85),
        (MediaMetadata.release_date >= str(year - 10), 0.70),
        (MediaMetadata.release_date >= str(year - 20), 0.55),
        (MediaMetadata.release_date >= str(year - 40), 0.40),
        else_=0.30,
    )


#: How the span above the multiplier floor is divided: two parts quality to one
#: part buzz-and-recency. At the historical floor of 0.55 this reproduces the
#: original 0.30 / 0.15 weights exactly, which is why it is expressed as a
#: ratio rather than as two more settings.
_QUALITY_SHARE = 2.0 / 3.0


def _blended_score_expr(relevance):
    """``relevance * (floor + (1-floor) * (⅔·quality + ⅓·buzz-and-recency))``.

    Multiplicative on relevance, and the multiplier is bounded to
    ``[catalog_quality_weight, 1.0]``. That is the whole point: popularity can
    *modulate* a well-matched title but can never float a badly-matched one, so
    the ordering stays monotone in how well a title actually fits the row.

    **The floor is what decides how much authority quality has.** It was a
    hardcoded 0.55, giving the multiplier a 1.82x span -- so quality could not
    reorder two titles whose relevance differed by more than that, and the
    ordering of any row with a spread of relevance scores was effectively
    quality-blind. Lowering it to the 0.30 default widens the span to 3.33x.
    The weights inside are derived from the floor rather than set separately so
    the multiplier still reaches exactly 1.0 at any floor; at 0.55 this is
    arithmetically identical to the original ``0.55 + 0.30q + 0.15b``.

    This replaces ``avg(confidence)``, which was actively perverse -- it
    averaged confidence over MATCHED tags only, so a title matching one optional
    tag at 0.95 outranked a title matching all four required tags at 0.85 mean.
    The old query computed ``tag_count`` and then discarded it.

    Emits only ``+ - * / CASE COALESCE``: no sqrt, no ln, no window functions,
    because SQLite's math functions are a compile-time option this project
    cannot assume (tests run on SQLite, production on Postgres).
    """
    popularity = func.coalesce(MediaMetadata.popularity, 0.0)
    buzz = popularity / (popularity + _BUZZ_HALF_LIFE)
    floor = settings.catalog_quality_weight
    span = 1.0 - floor
    modulation = (
        floor
        + span * _QUALITY_SHARE * (quality_expr() / 10.0)
        + span * (1.0 - _QUALITY_SHARE) * (0.7 * buzz + 0.3 * _recency_expr())
    )
    return relevance * modulation


class CatalogGenerator:
    """Generates catalogs from tag database using SQL queries."""

    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def _content_item(content, metadata: Optional[MediaMetadata]) -> Dict:
        """Serialize one materialized catalog membership without loading raw JSON."""
        if metadata:
            content_rating = None
            for facts in (metadata.mdb_facts, metadata.tag_facts):
                if isinstance(facts, dict) and facts.get("certification"):
                    content_rating = str(facts["certification"])
                    break
            year = metadata.release_date[:4] if metadata.release_date else ""
            return {
                "tmdb_id": metadata.tmdb_id,
                "media_type": metadata.media_type,
                "title": metadata.title,
                "poster": metadata.poster_path,
                "year": year,
                "genres": metadata.genres or [],
                "rating": (
                    metadata.rating_consensus
                    if metadata.rating_consensus is not None
                    else metadata.vote_average
                ),
                "rating_source": (
                    "consensus" if metadata.rating_consensus is not None else "tmdb"
                ),
                "content_rating": content_rating,
                "description": metadata.overview or "",
                "backdrop": metadata.backdrop_path,
                "imdb_id": metadata.imdb_id,
                "logo": metadata.logo_path,
                "digital_release_date": metadata.digital_release_date,
                "next_episode_air_date": metadata.next_episode_air_date,
                "rank": content.rank,
                "match_score": content.match_score,
                "_snapshot_revision": (
                    content.added_at.isoformat()
                    if hasattr(content, "added_at") and content.added_at
                    else (
                        content.last_updated.isoformat()
                        if hasattr(content, "last_updated") and content.last_updated
                        else None
                    )
                ),
                "_quality": _quality_value(metadata),
                "_votes": metadata.vote_count or 0,
            }
        return {
            "tmdb_id": content.tmdb_id,
            "media_type": content.media_type,
            "title": f"TMDB {content.tmdb_id}",
            "poster": None,
            "year": "",
            "genres": [],
            "rating": None,
            "rating_source": None,
            "content_rating": None,
            "description": "",
            "backdrop": None,
            "imdb_id": None,
            "logo": None,
            "digital_release_date": None,
            "next_episode_air_date": None,
            "rank": content.rank,
            "match_score": content.match_score,
            "_snapshot_revision": (
                content.added_at.isoformat()
                if hasattr(content, "added_at") and content.added_at
                else (
                    content.last_updated.isoformat()
                    if hasattr(content, "last_updated") and content.last_updated
                    else None
                )
            ),
        }

    def generate_universal_catalog_ids(
        self, category: UniversalCategory, limit: Optional[int] = None
    ) -> List[int]:
        """Ids only, for callers that do not want the scores.

        Kept because ``app/admin.py``'s category preview wraps the result in
        ``len()`` and would break on a list of tuples.
        """
        return [
            tmdb_id for tmdb_id, _ in self.generate_universal_catalog(category, limit)
        ]

    def generate_universal_catalog(
        self,
        category: UniversalCategory,
        limit: Optional[int] = None,
        tag_map: Optional[Dict[str, int]] = None,
    ) -> List[Tuple[int, float]]:
        """
        Generate content for a universal catalog based on tag formula.

        Args:
            category: UniversalCategory with tag_formula
            limit: Max items (defaults to settings.catalog_size)

        Returns:
            ``[(tmdb_id, score)]`` ranked by the blended score, best first. The
            scores are real -- they become ``UniversalCatalogContent.match_score``,
            which was written as a hardcoded 0.0 for the life of this module.
        """
        limit = limit or settings.catalog_size
        tag_formula = category.tag_formula

        mandatory_tags = tag_formula.get("mandatory", [])
        required_tags = tag_formula.get("required", [])
        optional_tags = tag_formula.get("optional", [])
        min_required = tag_formula.get("min_required", len(required_tags))
        min_weight_norm = max(0.0, float(tag_formula.get("min_weight_norm", 0.0)))
        formula_tagger_version = tag_formula.get("tagger_version")
        version_filters = (
            [MovieTag.tagger_version == str(formula_tagger_version)]
            if formula_tagger_version
            else []
        )
        # The operator's setting is the default; a per-category formula may
        # still override it. Nothing writes the key today, which is why the
        # floor was effectively a hardcoded 10 with no way to change it.
        min_vote_count = tag_formula.get(
            "min_vote_count", settings.catalog_min_vote_count
        )
        # A row with 12 genuine matches should return 12 items, not pad to 200
        # with titles that barely qualify. Off by default so existing formulas
        # are unchanged.
        min_relevance = tag_formula.get("min_relevance", 0.0)

        # Get tag IDs
        all_tag_names = mandatory_tags + required_tags + optional_tags
        if tag_map is None:
            tag_map = {
                tag.name: tag.id
                for tag in self.db.query(Tag).filter(Tag.name.in_(all_tag_names)).all()
            }

        mandatory_tag_ids = [
            tag_map[name] for name in mandatory_tags if name in tag_map
        ]
        required_tag_ids = [tag_map[name] for name in required_tags if name in tag_map]
        optional_tag_ids = [tag_map[name] for name in optional_tags if name in tag_map]

        all_match_ids = mandatory_tag_ids + required_tag_ids + optional_tag_ids

        # Relevance = SUM(weight_norm) over the matched tags, which rises with
        # BOTH the number of matches and their rarity. weight_norm is
        # confidence x IDF, L2-normalised per title (app/relevance.py), so a
        # shared "Cyberpunk" counts for far more than a shared "Drama" -- the
        # entire reason the IDF layer was built.
        relevance = func.sum(MovieTag.weight_norm).label("relevance")
        query = (
            self.db.query(
                MovieTag.tmdb_id,
                relevance,
                func.count(func.distinct(MovieTag.tag_id)).label("tag_count"),
            )
            .filter(
                MovieTag.media_type == category.media_type,
                MovieTag.tag_id.in_(all_match_ids),
                *version_filters,
                # Rows tagged before the relevance layer existed, and rows the
                # nightly job wrote without weights, have NULL weight_norm and
                # would contribute nothing to the sum. Excluding them here keeps
                # them out of the ranking rather than silently scoring them 0.
                MovieTag.weight_norm.isnot(None),
                *(
                    [MovieTag.weight_norm >= min_weight_norm]
                    if min_weight_norm > 0
                    else []
                ),
            )
            .group_by(MovieTag.tmdb_id)
        )

        # Mandatory tags: every single one must be present
        if mandatory_tag_ids:
            mandatory_sub = (
                self.db.query(MovieTag.tmdb_id)
                .filter(
                    MovieTag.tag_id.in_(mandatory_tag_ids),
                    MovieTag.media_type == category.media_type,
                    *version_filters,
                    *(
                        [MovieTag.weight_norm >= min_weight_norm]
                        if min_weight_norm > 0
                        else []
                    ),
                )
                .group_by(MovieTag.tmdb_id)
                .having(
                    func.count(func.distinct(MovieTag.tag_id)) == len(mandatory_tag_ids)
                )
                .subquery()
            )
            query = query.filter(MovieTag.tmdb_id.in_(mandatory_sub.select()))

        # Required tags: must have at least min_required of these
        if required_tag_ids:
            required_sub = (
                self.db.query(MovieTag.tmdb_id)
                .filter(
                    MovieTag.tag_id.in_(required_tag_ids),
                    MovieTag.media_type == category.media_type,
                    *version_filters,
                    *(
                        [MovieTag.weight_norm >= min_weight_norm]
                        if min_weight_norm > 0
                        else []
                    ),
                )
                .group_by(MovieTag.tmdb_id)
                .having(func.count(func.distinct(MovieTag.tag_id)) >= min_required)
                .subquery()
            )
            query = query.filter(MovieTag.tmdb_id.in_(required_sub.select()))

        # Quality floor: keep obscure and unheard-of titles out of every row.
        clauses = quality_floor_clauses(min_vote_count)
        if clauses:
            quality_sub = (
                self.db.query(MediaMetadata.tmdb_id)
                .filter(MediaMetadata.media_type == category.media_type, *clauses)
                .subquery()
            )
            query = query.filter(MovieTag.tmdb_id.in_(quality_sub.select()))

        if min_relevance > 0:
            query = query.having(func.sum(MovieTag.weight_norm) >= min_relevance)

        # The relevance aggregate is computed per title, then modulated by that
        # title's quality/buzz/recency -- so the join has to happen outside the
        # GROUP BY. A subquery keeps it portable; grouping by the metadata
        # columns as well would work on SQLite and be rejected by Postgres.
        rel_sub = query.subquery()
        score = _blended_score_expr(rel_sub.c.relevance).label("score")
        rows = (
            self.db.query(rel_sub.c.tmdb_id, score)
            .join(
                MediaMetadata,
                and_(
                    MediaMetadata.tmdb_id == rel_sub.c.tmdb_id,
                    MediaMetadata.media_type == category.media_type,
                ),
            )
            # tmdb_id breaks ties so pagination is stable across requests --
            # without it two titles on an identical score could swap places
            # between page 1 and page 2 and a client would see a duplicate.
            .order_by(score.desc(), rel_sub.c.tmdb_id)
            .limit(limit)
            .all()
        )

        logger.info(
            f"Generated {len(rows)} items for catalog '{category.name}' "
            f"(required: {required_tags}, optional: {optional_tags})"
        )

        return [(int(r[0]), float(r[1] or 0.0)) for r in rows]

    def save_universal_catalog_content(
        self,
        category_id: str,
        tmdb_ids: List[int],
        match_scores: Optional[List[float]] = None,
        media_type: Optional[str] = None,
    ):
        """
        Save universal catalog content to database.

        Args:
            category_id: Category ID
            tmdb_ids: List of TMDB IDs in rank order
            match_scores: Optional match scores for each item
        """
        # Delete existing content
        self.db.query(UniversalCatalogContent).filter(
            UniversalCatalogContent.category_id == category_id
        ).delete()

        # Get category for media_type
        if media_type is None:
            category = self.db.get(UniversalCategory, category_id)
            media_type = category.media_type if category else None
        if not media_type:
            logger.error(f"Category {category_id} not found")
            return

        # Insert new content
        updated_at = utc_now()
        self.db.bulk_insert_mappings(
            UniversalCatalogContent,
            [
                {
                    "category_id": category_id,
                    "tmdb_id": tmdb_id,
                    "rank": rank,
                    "match_score": match_scores[rank - 1] if match_scores else 0.0,
                    "media_type": media_type,
                    "last_updated": updated_at,
                }
                for rank, tmdb_id in enumerate(tmdb_ids, start=1)
            ],
        )

        self.db.flush()
        logger.info(f"Saved {len(tmdb_ids)} items to catalog '{category_id}'")
        from app.home_feed import invalidate_discovery

        invalidate_discovery(self.db)

    def regenerate_all_universal_catalogs(self, source: Optional[str] = None):
        """Regenerate active universal catalogs, optionally for one source."""
        query = self.db.query(UniversalCategory).filter(
            UniversalCategory.is_active.is_(True)
        )
        if source is not None:
            query = query.filter(UniversalCategory.source == source)
        categories = query.all()

        logger.info(f"Regenerating {len(categories)} universal catalogs...")

        tag_names = {
            name
            for category in categories
            for key in ("mandatory", "required", "optional")
            for name in (category.tag_formula or {}).get(key, [])
        }
        tag_map = {
            tag.name: tag.id
            for tag in self.db.query(Tag).filter(Tag.name.in_(tag_names or {""})).all()
        }

        for category in categories:
            try:
                ranked = self.generate_universal_catalog(category, tag_map=tag_map)
                self.save_universal_catalog_content(
                    category.id,
                    [tmdb_id for tmdb_id, _ in ranked],
                    match_scores=[score for _, score in ranked],
                    media_type=category.media_type,
                )
                # Commit each category independently so one failure rolls back only
                # its own (partial) work — previously it discarded every catalog
                # regenerated earlier in this run.
                self.db.commit()
            except Exception as e:
                self.db.rollback()
                logger.error(f"Failed to generate catalog '{category.id}': {e}")

        logger.info("Universal catalog regeneration complete")

    # The personalized dispatch tree that used to live here --
    # generate_personalized_catalog and its five private helpers -- has been
    # deleted. It had zero callers anywhere: workers/personalized_sync.py
    # computes its own ids and only ever calls save_user_catalog below, and the
    # "top_picks" / "because_you_watched" / "hidden_gems" strings that look
    # like a dispatch table are just generation_method labels stored on the
    # row. It mattered because it carried four authoritative-looking quality
    # thresholds (vote_average >= 7.0/7.5, vote_count >= 500/1000) that were
    # the first thing anyone found when asked "where is the quality filter" --
    # and none of them could execute. The live floor is quality_floor_clauses.

    def save_user_catalog(
        self,
        user_id: int,
        slot_id: str,
        name: str,
        media_type: str,
        tmdb_ids: List[int],
        generation_method: str,
        generation_params: Optional[Dict] = None,
        match_scores: Optional[List[float]] = None,
    ) -> UserCatalog:
        """
        Save personalized catalog for a user.

        Args:
            user_id: User ID
            slot_id: Slot identifier (e.g., 'personalized-1')
            name: Display name
            media_type: 'movie' or 'tv'
            tmdb_ids: List of TMDB IDs
            generation_method: How it was generated
            generation_params: Parameters used

        Returns:
            Created UserCatalog
        """
        generated_at = utc_now()
        generation_params = dict(generation_params or {})
        generation_params["generation_revision"] = generated_at.isoformat()

        # Check if catalog exists
        catalog = (
            self.db.query(UserCatalog)
            .filter(UserCatalog.user_id == user_id, UserCatalog.slot_id == slot_id)
            .first()
        )

        if catalog:
            # Update existing
            catalog.name = name  # type: ignore[assignment]
            catalog.generation_method = generation_method  # type: ignore[assignment]
            catalog.generation_params = generation_params  # type: ignore[assignment]
            catalog.last_generated = generated_at  # type: ignore[assignment]

            # Delete old content
            self.db.query(UserCatalogContent).filter(
                UserCatalogContent.catalog_id == catalog.id
            ).delete()
        else:
            # Create new
            catalog = UserCatalog(
                user_id=user_id,
                slot_id=slot_id,
                name=name,
                media_type=media_type,
                generation_method=generation_method,
                generation_params=generation_params,
                last_generated=generated_at,
            )
            self.db.add(catalog)
            self.db.flush()  # Get catalog.id

        # Most personal rows use rank as their score. Similarity rows also pass
        # the real per-title cosine so an operator can inspect why each BYW
        # recommendation survived the threshold.
        for rank, tmdb_id in enumerate(tmdb_ids, start=1):
            content = UserCatalogContent(
                catalog_id=catalog.id,
                tmdb_id=tmdb_id,
                rank=rank,
                match_score=(
                    float(match_scores[rank - 1])
                    if match_scores and rank <= len(match_scores)
                    else 0.0
                ),
                media_type=media_type,
                added_at=generated_at,
            )
            self.db.add(content)

        self.db.flush()
        logger.info(f"Saved user catalog '{name}' with {len(tmdb_ids)} items")
        # No invalidate_discovery here: callers save a whole set of rows in one
        # transaction and invalidate once just before committing. Doing it per
        # row took the profile's home_feed_states lock at the first save and
        # held it across the rest of the sync's provider I/O, stalling /api/home.
        return catalog

    def get_catalog_content(
        self,
        category_id: str,
        user_id: Optional[int] = None,
        hide_foreign: bool = False,
        hide_adult: bool = False,
        hide_unreleased: bool = False,
        user_taste_tag_ids: Optional[List[int]] = None,
        exclude_ids: Optional[set] = None,
    ) -> List[Dict]:
        """
        Get catalog content with metadata.

        Args:
            category_id: Category ID
            user_id: User ID (for personalized catalogs)
            hide_foreign: Exclude non-English content
            hide_adult: Exclude explicit/18+ content
            hide_unreleased: Exclude movies not yet available digitally
            user_taste_tag_ids: Tag IDs for taste-boosting (local users)
            exclude_ids: TMDB IDs to exclude (watched/imported items)

        Returns:
            List of catalog items with metadata
        """
        limit = settings.catalog_size

        if user_id:
            # Personalized catalog
            query = (
                self.db.query(UserCatalogContent, MediaMetadata)
                .outerjoin(
                    MediaMetadata,
                    and_(
                        UserCatalogContent.tmdb_id == MediaMetadata.tmdb_id,
                        UserCatalogContent.media_type == MediaMetadata.media_type,
                    ),
                )
                .join(UserCatalog)
                .filter(
                    UserCatalog.user_id == user_id, UserCatalog.slot_id == category_id
                )
            )
        else:
            # Universal catalog
            query = (
                self.db.query(UniversalCatalogContent, MediaMetadata)
                .outerjoin(
                    MediaMetadata,
                    and_(
                        UniversalCatalogContent.tmdb_id == MediaMetadata.tmdb_id,
                        UniversalCatalogContent.media_type == MediaMetadata.media_type,
                    ),
                )
                .filter(UniversalCatalogContent.category_id == category_id)
            )

        # The large MediaMetadata.raw_data JSON blob is not used on this hot path,
        # so don't SELECT it (defer to lazy-load if ever accessed).
        query = query.options(defer(MediaMetadata.raw_data))

        # Exclude watched/imported items
        if exclude_ids:
            if user_id:
                query = query.filter(UserCatalogContent.tmdb_id.notin_(exclude_ids))
            else:
                query = query.filter(
                    UniversalCatalogContent.tmdb_id.notin_(exclude_ids)
                )

        # Apply content filters (treat NULL metadata as "keep" so items
        # that haven't been backfilled yet aren't silently dropped)
        if hide_foreign:
            query = query.filter(
                or_(
                    MediaMetadata.original_language == "en",
                    MediaMetadata.original_language.is_(None),
                )
            )
        if hide_adult:
            query = query.filter(
                or_(
                    MediaMetadata.adult.isnot(True),
                    MediaMetadata.adult.is_(None),
                )
            )
        if hide_unreleased:
            cutoff = (utc_now() - timedelta(days=45)).strftime("%Y-%m-%d")
            query = query.filter(
                or_(
                    MediaMetadata.release_date <= cutoff,
                    MediaMetadata.release_date.is_(None),
                )
            )

        if user_id:
            results = query.order_by(UserCatalogContent.rank).all()
        else:
            results = query.order_by(UniversalCatalogContent.rank).all()

        # Catalog suppression happens before the visible limit so a removed
        # title is backfilled by the next ranked membership. Search and direct
        # metadata never call this loader and therefore remain complete.
        items = []
        for content, metadata in results:
            if is_home_mockbuster(
                metadata,
                tmdb_id=getattr(content, "tmdb_id", None),
                media_type=getattr(content, "media_type", None),
            ):
                continue
            items.append(self._content_item(content, metadata))
            if len(items) >= limit:
                break

        # Taste-boost: re-rank by how well each title matches the viewer.
        #
        # Summing weight_norm rather than counting matched tags. A raw count
        # treats a shared "Drama" as worth exactly as much as a shared
        # "Neo-Noir", which is how every profile ended up boosting the same
        # titles regardless of taste.
        #
        # **Bounded, not absolute.** This used to be a hard sort --
        # `items.sort(key=(-taste, rank))` -- for every universal row, which
        # discarded the blended relevance/quality score the row had just been
        # built with and left the row's own formula as a tie-break. A curated
        # row is an editorial claim ("these are Gritty Crime Thrillers"), and a
        # row whose first title is the 90th-best match to that claim because it
        # happens to hit two of your taste tags has quietly stopped being the
        # row its header promises. It is the same failure the feed rows were
        # given `_blend_feed_row` to avoid, one tier up -- so they now share it,
        # and a title can climb at most `_MAX_FEED_CLIMB` places here too.
        #
        # A side effect worth naming: `main._shuffle_items` tiers on
        # match_score, and the hard sort left the list no longer ordered by it,
        # so consecutive items almost never shared a tier and the three-hourly
        # reshuffle was inert for every profile with any history. Preserving
        # score order makes that feature work again.
        #
        # Applies to universal rows, and to the TMDB-sourced personal slots --
        # Trending Now, New Releases, Popular, Coming Soon. Those were the only
        # rows in the product that were not a function of the viewer: one TMDB
        # fetch per sync, copied verbatim into every profile, served in TMDB's
        # own popularity order. Every OTHER personal slot is already ordered by
        # this same signal at build time, so it is excluded here rather than
        # sorted twice.
        # Coming Soon is excluded: its order IS its content. The row is built
        # soonest-first because that is the only ordering an unreleased title
        # supports -- it has no reviews, effectively no votes, and is outside
        # the tagging window by construction. Blending it by taste let a title
        # climb eight places out of date order, which contradicted both the
        # shuffle exemption and the prefetch comment that claim date order is
        # load-bearing. Two mechanisms, opposite behaviour, same row.
        feed_slot = (
            bool(user_id)
            and is_tmdb_sourced_slot(category_id)
            and not str(category_id).startswith("coming-soon-")
        )
        if user_taste_tag_ids and items and (not user_id or feed_slot):
            taste_set = set(user_taste_tag_ids)
            item_pairs = [(item["tmdb_id"], item["media_type"]) for item in items]
            tag_hits_rows = (
                self.db.query(
                    MovieTag.tmdb_id,
                    MovieTag.media_type,
                    func.sum(MovieTag.weight_norm),
                )
                .filter(
                    tuple_(MovieTag.tmdb_id, MovieTag.media_type).in_(item_pairs),
                    MovieTag.tag_id.in_(taste_set),
                    MovieTag.weight_norm.isnot(None),
                )
                .group_by(MovieTag.tmdb_id, MovieTag.media_type)
                .all()
            )
            hits_map = {(r[0], r[1]): float(r[2] or 0.0) for r in tag_hits_rows}

            for item in items:
                item["_taste_score"] = hits_map.get(
                    (item["tmdb_id"], item["media_type"]), 0.0
                )

            self._blend_feed_row(items)
            for item in items:
                item.pop("_taste_score", None)
                item.pop("_quality", None)
                item.pop("_votes", None)

        return items

    def get_catalog_contents_batch(
        self,
        category_ids: List[str],
        *,
        user_id: Optional[int] = None,
        hide_foreign: bool = False,
        hide_adult: bool = False,
        hide_unreleased: bool = False,
        user_taste_tag_ids: Optional[List[int]] = None,
        exclude_ids: Optional[set] = None,
        membership_only: bool = False,
        catalog_size: Optional[int] = None,
    ) -> Dict[str, List[Dict]]:
        """Load many materialized shelves in a bounded number of queries.

        The single-row method remains for single-catalog reads. The
        browser dashboard uses this method so adding a shelf does not add a SQL
        round trip.
        """
        ordered_ids = list(dict.fromkeys(str(value) for value in category_ids))
        catalog_size = settings.catalog_size if catalog_size is None else catalog_size
        grouped: Dict[str, List[Dict]] = {
            category_id: [] for category_id in ordered_ids
        }
        if not ordered_ids:
            return grouped

        if user_id is not None:
            query = (
                self.db.query(UserCatalogContent, MediaMetadata, UserCatalog.slot_id)
                .join(UserCatalog, UserCatalog.id == UserCatalogContent.catalog_id)
                .outerjoin(
                    MediaMetadata,
                    and_(
                        UserCatalogContent.tmdb_id == MediaMetadata.tmdb_id,
                        UserCatalogContent.media_type == MediaMetadata.media_type,
                    ),
                )
                .filter(
                    UserCatalog.user_id == user_id,
                    UserCatalog.slot_id.in_(ordered_ids),
                )
            )
            content_model = UserCatalogContent
            order_columns = (UserCatalog.slot_id, UserCatalogContent.rank)
        else:
            query = (
                self.db.query(
                    UniversalCatalogContent,
                    MediaMetadata,
                    UniversalCatalogContent.category_id,
                )
                .outerjoin(
                    MediaMetadata,
                    and_(
                        UniversalCatalogContent.tmdb_id == MediaMetadata.tmdb_id,
                        UniversalCatalogContent.media_type == MediaMetadata.media_type,
                    ),
                )
                .filter(UniversalCatalogContent.category_id.in_(ordered_ids))
            )
            content_model = UniversalCatalogContent
            order_columns = (
                UniversalCatalogContent.category_id,
                UniversalCatalogContent.rank,
            )

        if membership_only:
            from sqlalchemy.orm import load_only

            # Background Home membership construction needs policy/ranking inputs,
            # not descriptions, images, credits or provider payloads for every card.
            query = query.options(
                load_only(
                    MediaMetadata.tmdb_id,
                    MediaMetadata.media_type,
                    MediaMetadata.tag_facts,
                    MediaMetadata.mdb_facts,
                    MediaMetadata.rating_consensus,
                    MediaMetadata.vote_count,
                    MediaMetadata.vote_average,
                    MediaMetadata.backdrop_path,
                    # The hero needs a title logo; without this each check is a
                    # lazy load per card.
                    MediaMetadata.logo_path,
                )
            )
        else:
            query = query.options(defer(MediaMetadata.raw_data))
        if exclude_ids:
            query = query.filter(content_model.tmdb_id.notin_(exclude_ids))
        if hide_foreign:
            query = query.filter(
                or_(
                    MediaMetadata.original_language == "en",
                    MediaMetadata.original_language.is_(None),
                )
            )
        if hide_adult:
            query = query.filter(
                or_(MediaMetadata.adult.isnot(True), MediaMetadata.adult.is_(None))
            )
        if hide_unreleased:
            cutoff = (utc_now() - timedelta(days=45)).strftime("%Y-%m-%d")
            query = query.filter(
                or_(
                    MediaMetadata.release_date <= cutoff,
                    MediaMetadata.release_date.is_(None),
                )
            )

        for content, metadata, category_id in query.order_by(*order_columns).yield_per(
            256
        ):
            bucket = grouped.get(str(category_id))
            if (
                bucket is not None
                and len(bucket) < catalog_size
                and not is_home_mockbuster(
                    metadata,
                    tmdb_id=getattr(content, "tmdb_id", None),
                    media_type=getattr(content, "media_type", None),
                )
            ):
                if membership_only:
                    timestamp = getattr(content, "added_at", None) or getattr(
                        content, "last_updated", None
                    )
                    bucket.append(
                        {
                            "tmdb_id": content.tmdb_id,
                            "media_type": content.media_type,
                            "rank": content.rank,
                            "match_score": content.match_score,
                            "_snapshot_revision": (
                                timestamp.isoformat() if timestamp else None
                            ),
                            "_quality": _quality_value(metadata),
                            "backdrop": bool(metadata and metadata.backdrop_path),
                        }
                    )
                else:
                    bucket.append(self._content_item(content, metadata))

        boosted_ids = {
            category_id
            for category_id, items in grouped.items()
            if items
            and user_taste_tag_ids
            and (
                user_id is None
                or (
                    is_tmdb_sourced_slot(category_id)
                    and not category_id.startswith("coming-soon-")
                )
            )
        }
        if boosted_ids:
            pairs = {
                (item["tmdb_id"], item["media_type"])
                for category_id in boosted_ids
                for item in grouped[category_id]
            }
            hit_rows = (
                self.db.query(
                    MovieTag.tmdb_id,
                    MovieTag.media_type,
                    func.sum(MovieTag.weight_norm),
                )
                .filter(
                    tuple_(MovieTag.tmdb_id, MovieTag.media_type).in_(pairs),
                    MovieTag.tag_id.in_(set(user_taste_tag_ids or [])),
                    MovieTag.weight_norm.isnot(None),
                )
                .group_by(MovieTag.tmdb_id, MovieTag.media_type)
                .all()
            )
            hits = {(row[0], row[1]): float(row[2] or 0.0) for row in hit_rows}
            for category_id in boosted_ids:
                items = grouped[category_id]
                for item in items:
                    item["_taste_score"] = hits.get(
                        (item["tmdb_id"], item["media_type"]), 0.0
                    )
                self._blend_feed_row(items)
                for item in items:
                    item.pop("_taste_score", None)
                    item.pop("_quality", None)
                    item.pop("_votes", None)

        return grouped

    @staticmethod
    def _blend_feed_row(items: List[Dict]) -> None:
        """Reorder a row in place: existing position, modulated by fit.

        Used by every row whose stored order already means something -- the
        TMDB feed rows, where position is "how trending is this", and the
        universal rows, where it is the blended relevance/quality score the
        catalog was generated with.

        The alternative, and what the universal rows used to do, is to sort
        purely by taste overlap. That throws the row's own ordering away. Both
        kinds of row make a claim -- "Trending Now" about the world, "Gritty
        Crime Thrillers" about its contents -- and a row whose top title is the
        47th-most-trending, or the 90th-best match to the formula, because it
        happens to hit two of your taste tags has quietly stopped being the row
        it says it is.

        So blend, in the same shape as ``_blended_score_expr``: multiplicative
        on the primary signal, with a multiplier bounded to
        ``[_FEED_MULT_FLOOR, 1.0]``. In ``_blended_score_expr`` the primary
        signal is relevance to a formula; here it is the position the row
        already has.

        **Position decays geometrically, not linearly**, and that is the whole
        design. Under a linear ramp the ratio between adjacent positions grows
        as you go down the row -- 1.05 near the top, 2.0 at the tail of a
        20-item row -- so a bounded multiplier reorders the head freely and the
        tail not at all. Exactly backwards: the head is what a viewer sees
        anyway, and a strong match buried at #18 is the one worth surfacing.
        A constant per-step ratio makes the multiplier worth the same fixed
        number of places wherever a title sits, so the guarantee is simple
        enough to state and to test: **a title can climb at most
        ``_MAX_FEED_CLIMB`` places, anywhere in the row.**

        Taste is normalised against the row's own maximum rather than an
        absolute scale, because ``weight_norm`` sums are not comparable across
        rows: a row of heavily-tagged prestige titles and a row of thin new
        releases would otherwise be modulated by wildly different amounts.
        A row where nothing matches degrades to TMDB's order exactly, which is
        the honest answer when we know nothing about the titles.
        """
        if not items:
            return
        max_taste = max(item.get("_taste_score", 0.0) for item in items)
        for index, item in enumerate(items):
            position = _FEED_POSITION_DECAY**index
            taste = (item.get("_taste_score", 0.0) / max_taste) if max_taste else 0.0
            quality = min(max(item.get("_quality", _PRIOR_MEAN) / 10.0, 0.0), 1.0)
            item["_feed_score"] = position * (
                _FEED_MULT_FLOOR + 0.30 * taste + 0.15 * quality
            )
        items.sort(key=lambda x: (-x["_feed_score"], x["rank"]))
        for item in items:
            item.pop("_feed_score", None)
