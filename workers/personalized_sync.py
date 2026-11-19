"""
Personalized catalog sync worker backed by local Curatio watch history.

Uses the AI tag database and locally stored watch events to generate
Netflix/Prime/HBO-style catalog rows:
  1. "Trending Movies / Shows"         (TMDB daily/weekly trending)
  2. "New Movies / Shows"              (recent digital releases)
  3. "Because You Watched [Title]" x5  (tag-based similarity)
  4. "Top Picks / Recommended For You" (tag-based taste profile)
  5. "Genre/Mood For You" x4           (taste-specific catalogs)
  6. "Hidden Gems For You"             (high-quality low-popularity)

Trending and New Releases provide the timely movie/show shelves that the
synthesized layer cannot. Their candidates come from TMDB, then serving adjusts
the order for each viewer. Other discovery comes from synthesized universal
rows until a profile has enough history to produce genuinely personal rows.
Short activity collections belong to the dashboard's Continue Watching/history
fields, not to discovery shelves that promise a full row.
"""

import asyncio
import random
import time
from datetime import timedelta
from typing import List, Dict, Sequence, Set

import httpx
from loguru import logger
from sqlalchemy import and_, func, or_, tuple_
from sqlalchemy.orm import Session, defer

# Add parent directory to path
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.time_utils import utc_now  # noqa: E402
from app.catalog_policy import (  # noqa: E402
    HOME_MOVIE_WITHOUT_KEYWORDS,
    is_home_mockbuster,
    keyword_payload_is_mockbuster,
)
from app.models import User, MovieTag, MediaMetadata, WatchEvent, Tag  # noqa: E402
from app.server_history import SEED_ACTIONS  # noqa: E402
from app.models import UniversalCategory, UniversalCatalogContent  # noqa: E402
from app.tmdb_client import tmdb_client  # noqa: E402
from app.catalog_generator import CatalogGenerator  # noqa: E402
from app.row_names import TIMELY_FEED_ROWS, row_name  # noqa: E402
from workers.metadata_store import upsert_media_metadata  # noqa: E402
from workers.tagging_common import shuffle_within_tiers  # noqa: E402

# ---------------------------------------------------------------------------
# Sync status (in-memory, read by admin portal for live progress)
# ---------------------------------------------------------------------------
sync_status: dict = {
    "running": False,
    "started_at": None,
    "completed_at": None,
    "current_user": None,
    "users_done": 0,
    "users_total": 0,
    "prefetch_counts": {},
    "home_release_filtered": 0,
    "mockbusters_filtered": 0,
    "last_error": None,
}

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------
MAX_BYW_SEEDS = 5  # max "Because You Watched" seed items per user
TASTE_TOP_N_TAGS = 15  # top N tags used when building a taste profile
BYW_CANDIDATE_HISTORY = 50  # recent watch events to consider for BYW seeds
TMDB_DISCOVER_PAGES = 3  # pages fetched per discover/trending call
TASTE_CATALOG_POOL = 20  # genre/mood tags to fetch before rotating

#: Vote floor for the Coming Soon rows.
#:
#: Deliberately far below the catalog floor: an unreleased title has barely any
#: votes, so the general quality gate would empty this row entirely. It is high
#: enough to skip the announced-title placeholders TMDB carries with no
#: audience at all.
COMING_SOON_MIN_VOTES = 5

#: What makes a title a "hidden gem". Measured on the same 0-10 scale as
#: ``catalog_min_quality`` so the two are comparable at a glance: the gem row
#: asks for a title clearly *above* the floor the rest of the product applies,
#: not merely past it.
GEM_MIN_QUALITY = 7.2
GEM_MIN_VOTES = 100
GEM_MAX_POPULARITY = 30.0

#: How strongly a title must carry a tag before a row named after that tag will
#: claim it. Matches ``relevance.MIN_WEIGHT_NORM``, the threshold the rest of
#: the system already treats as "this tag is actually about this title" rather
#: than a trace assignment.
ANCHOR_MIN_WEIGHT_NORM = 0.05

#: A row nobody can fill is not a row.
#:
#: Every builder here already dropped an *empty* row; none of them dropped a
#: nearly-empty one, so a shelf could render with four posters under a header
#: promising a category. That reads as breakage, and it is the shape any honest
#: filter produces at the margin -- so the filters and this constant have to
#: ship together.
MIN_ROW_ITEMS = 24


def _taste_row_name(tag_name: str) -> str:
    """Facet-aware row title, falling back to the legacy form off-taxonomy.

    A tag row can hold a legacy name that is no longer in the taxonomy (nothing
    is ever deleted, only deactivated), so an unknown name must still produce a
    usable title rather than raising.
    """
    from app.taxonomy import BY_NAME, taste_row_name

    tag_def = BY_NAME.get(tag_name)
    return taste_row_name(tag_def) if tag_def else f"{tag_name} For You"


def _daily_rng(user_id: int = 0) -> random.Random:
    """Return an RNG seeded by today's date + user_id for daily rotation."""
    seed = utc_now().strftime("%Y-%m-%d") + str(user_id)
    return random.Random(seed)


def _quality_floor_ids(db: Session, media_type: str):
    """Sub-select of tmdb_ids clearing the catalog quality floor.

    Thin alias kept for the call sites in this module; the definition moved to
    ``app.catalog_generator`` so ``app.relevance`` can reach it too -- a worker
    module is not importable from ``app/`` without a cycle, which is part of
    why the two rows served out of ``relevance`` never got a floor.
    """
    from app.catalog_generator import quality_floor_ids

    return quality_floor_ids(db, media_type)


# ---------------------------------------------------------------------------
# Catalog slot ordering — controls home row order (lower = higher up)
# ---------------------------------------------------------------------------
# Timely TMDB feeds lead the genuinely personal rows. Popular, Coming Soon, and
# zero-history discovery rows remain retired: only Trending and New Releases
# add a time-sensitive signal that the synthesized universal layer cannot.
SLOT_ORDER = {
    "trending-movie": 1,
    "trending-series": 2,
    "new-releases-movie": 3,
    "new-releases-series": 4,
    # Slot ids carry their media type. A slot's type MUST be a property of its
    # id, never of whatever content happened to land in it this sync.
    #
    # These were `byw-1..5` and `taste-0..3`, typed from the seed title and the
    # winning tag respectively. Both change between syncs -- the taste rotation
    # is documented as changing daily -- while clients cached a catalog's type
    # from that same mutable field. Once a slot flipped type they kept
    # requesting the old one, every item was dropped as a type mismatch and the
    # row rendered empty. Observed in production with all five BYW rows
    # requested as `series` while all five held movies.
    "byw-movie-1": 10,
    "byw-movie-2": 11,
    "byw-movie-3": 12,
    "byw-series-1": 13,
    "byw-series-2": 14,
    "picks-movie": 15,
    "picks-series": 16,
    "rec-movie": 17,
    "rec-series": 18,
    "taste-movie-1": 19,
    "taste-movie-2": 20,
    "taste-series-1": 21,
    "taste-series-2": 22,
    "gems-movie": 23,
    "gems-series": 24,
}

#: How many slots each family reserves per media type. Fixed counts keep the
#: row set stable: a family with no eligible seed simply yields no row, rather
#: than borrowing another type's slot.
BYW_SLOTS = {"movie": 3, "tv": 2}
TASTE_SLOTS = {"movie": 2, "tv": 2}


def typed_slot_id(family: str, media_type: str, index: int) -> str:
    """``byw-series-2`` etc. — the id encodes the type, so it cannot drift."""
    return f"{family}-{'series' if media_type == 'tv' else 'movie'}-{index}"


def get_slot_sort_order(slot_id: str) -> int:
    """Return sort order for a catalog slot. Unknown slots go last."""
    return SLOT_ORDER.get(slot_id, 100)


# Valid slot IDs. The four timely feeds are user-scoped because serving blends
# their provider order with the viewer's taste and removes watched titles.
# Other legacy generic/bootstrap rows remain outside this contract.
VALID_SLOTS = set(SLOT_ORDER.keys())

# The user-visible common rows that must be present before a new profile is
# considered "synced". If the foreground TMDB fetch fails to populate these,
# we leave user.last_sync unset so the profile keeps re-triggering the sync
# until they land. "popular-*" is
# intentionally excluded — it can legitimately overlap/empty without indicating
# a failed fetch, and gating on it could cause permanent re-trigger loops.
CORE_COMMON_SLOTS = frozenset(spec.slot_id for spec in TIMELY_FEED_ROWS)

# Cached common (TMDB) catalog data shared with the single-user sync path. The
# nightly bulk run fetches this once for all users; a profile created shortly
# after can reuse it instead of re-hammering TMDB inline (which trips the rate
# limiter and produces the empty rows this whole change guards against). Only a
# result with all core rows populated is cached, so a partial/failed fetch is
# never served from cache.
_COMMON_DATA_TTL = 3600  # seconds
_common_data_cache: "tuple[float, dict[str, list[int]]] | None" = None


def _get_cached_common_data() -> "dict[str, list[int]] | None":
    """Return cached common catalog data if still fresh, else None."""
    if _common_data_cache is None:
        return None
    cached_at, data = _common_data_cache
    if time.monotonic() - cached_at > _COMMON_DATA_TTL:
        return None
    return data


def _store_common_data(data: "dict[str, list[int]] | None") -> None:
    """Cache common catalog data, but only when the core rows are populated."""
    global _common_data_cache
    if data and all(data.get(slot) for slot in CORE_COMMON_SLOTS):
        _common_data_cache = (time.monotonic(), data)


def _cleanup_stale_catalogs(user_id: int, db: Session) -> int:
    """Delete user catalogs with slot IDs that are no longer in SLOT_ORDER.

    Returns the number of catalogs removed.
    """
    from app.models import UserCatalog, UserCatalogContent

    stale = (
        db.query(UserCatalog)
        .filter(
            UserCatalog.user_id == user_id,
            UserCatalog.slot_id.notin_(VALID_SLOTS),
        )
        .all()
    )
    if not stale:
        return 0

    stale_ids = [c.id for c in stale]
    slot_names = [c.slot_id for c in stale]
    db.query(UserCatalogContent).filter(
        UserCatalogContent.catalog_id.in_(stale_ids)
    ).delete(synchronize_session=False)
    db.query(UserCatalog).filter(UserCatalog.id.in_(stale_ids)).delete(
        synchronize_session=False
    )
    db.flush()
    logger.info(
        f"  Cleaned up {len(stale)} stale catalogs for user {user_id}: {slot_names}"
    )
    return len(stale)


def _reset_user_catalogs(user_id: int, db: Session) -> int:
    """Clear the prior personal snapshot before atomically building the next.

    A valid slot that is not selected today must disappear rather than serving
    yesterday's seed or taste tag indefinitely. The delete and rebuild share
    the caller's transaction, so other sessions keep seeing the prior complete
    snapshot until the new one commits; an outer failure rolls everything back.
    """
    from app.models import UserCatalog, UserCatalogContent

    catalog_ids = [
        row.id
        for row in db.query(UserCatalog.id).filter(UserCatalog.user_id == user_id).all()
    ]
    if not catalog_ids:
        return 0
    db.query(UserCatalogContent).filter(
        UserCatalogContent.catalog_id.in_(catalog_ids)
    ).delete(synchronize_session=False)
    db.query(UserCatalog).filter(UserCatalog.id.in_(catalog_ids)).delete(
        synchronize_session=False
    )
    db.flush()
    logger.info(f"  Retired prior personal snapshot: {len(catalog_ids)} rows")
    return len(catalog_ids)


#: Rows built only for a profile with no watch history to draw on.
DISCOVERY_SLOTS = ("discover-movie", "discover-series", "discover-acclaimed")


def _retire_discovery_catalogs(db: Session, user: User, display: str) -> int:
    """Remove the zero-history rows once the profile has history.

    ``_generate_discovery_catalogs`` runs only under ``if not has_history``,
    but nothing ever removed what it wrote: ``_cleanup_stale_catalogs`` only
    deletes slots absent from ``VALID_SLOTS``, and these three are legitimate
    members. So a profile that watched a single title kept three frozen
    bootstrap rows forever, and they sat above every row built from the history
    that had since accumulated.
    """
    from app.models import UserCatalog, UserCatalogContent

    rows = (
        db.query(UserCatalog)
        .filter(
            UserCatalog.user_id == user.id,
            UserCatalog.slot_id.in_(DISCOVERY_SLOTS),
        )
        .all()
    )
    if not rows:
        return 0
    ids = [c.id for c in rows]
    db.query(UserCatalogContent).filter(UserCatalogContent.catalog_id.in_(ids)).delete(
        synchronize_session=False
    )
    db.query(UserCatalog).filter(UserCatalog.id.in_(ids)).delete(
        synchronize_session=False
    )
    db.flush()
    logger.info(f"  Retired {len(rows)} bootstrap discovery rows for {display}")
    return len(rows)


# ---------------------------------------------------------------------------
# Shared scoring — every personal row is ordered by the same expression the
# universal rows use.
#
# They were not. Each builder here ordered by raw ``SUM(weight_norm)``, so a
# personal row was ranked on tag fit alone while the universal row directly
# below it on the same screen had already been modulated by quality, buzz and
# recency. The quality floor kept the worst titles out of both; nothing made
# the two agree about which of the survivors belonged at the front.
# ---------------------------------------------------------------------------
def _blend_candidates(
    db: Session,
    candidates,
    media_type: str,
    limit: int,
) -> List[tuple]:
    """Rank a grouped ``(tmdb_id, relevance)`` query by the blended score.

    ``candidates`` is an un-ordered, un-limited query already grouped by
    ``tmdb_id``. The metadata join has to happen outside that GROUP BY, so this
    wraps it in a subquery exactly as ``generate_universal_catalog`` does --
    grouping by the metadata columns as well would work on SQLite and be
    rejected by PostgreSQL.

    Returns ``[(tmdb_id, score)]``, best first.
    """
    from app.catalog_generator import _blended_score_expr

    sub = candidates.subquery()
    score = _blended_score_expr(sub.c.relevance).label("score")
    rows = (
        db.query(sub.c.tmdb_id, score)
        .join(
            MediaMetadata,
            and_(
                MediaMetadata.tmdb_id == sub.c.tmdb_id,
                MediaMetadata.media_type == media_type,
            ),
        )
        .order_by(score.desc(), sub.c.tmdb_id)
        .limit(limit)
        .all()
    )
    return [(int(r[0]), float(r[1] or 0.0)) for r in rows]


def _blend_ids(db: Session, ranked: List[tuple], media_type: str) -> List[tuple]:
    """Re-score an in-memory ``[(tmdb_id, relevance)]`` list, best first.

    The subquery form above is preferable wherever the candidate set is still a
    query. Two callers have already materialised theirs -- the cosine path,
    whose ranking is produced inside :mod:`app.relevance`, and the taste rows,
    which have to inspect the movie/TV mix before they know which type the row
    is. Both go through ``blended_value``, which is the same arithmetic as the
    SQL expression rather than an approximation of it.
    """
    if not ranked:
        return []
    from app.catalog_generator import blended_value

    meta = {
        row.tmdb_id: row
        for row in db.query(MediaMetadata)
        .options(defer(MediaMetadata.raw_data))
        .filter(
            MediaMetadata.media_type == media_type,
            MediaMetadata.tmdb_id.in_([tid for tid, _ in ranked]),
        )
        .all()
    }
    return sorted(
        ((tid, blended_value(value, meta.get(tid))) for tid, value in ranked),
        key=lambda pair: (-pair[1], pair[0]),
    )


# ---------------------------------------------------------------------------
# Tag-based "Because You Watched" generation
# ---------------------------------------------------------------------------
#: Mean cosine similarity of the top few neighbours below which a seed does not
#: get a row.
#:
#: "Because You Watched X" is a claim that these titles are like X. When the
#: neighbour list is weak the row silently becomes "some titles that share two
#: tags with X", which is the shape a viewer reads as broken. The module already
#: makes this argument about seeds whose title cannot be resolved -- one fewer
#: row beats a row that misrepresents itself.
#:
#: Not sufficient on its own; see the discriminating-tag check in
#: :func:`find_similar_by_tags`, which catches the case cosine cannot.
MIN_BYW_SIMILARITY = 0.12

#: Neighbours averaged for the test above. Small on purpose: a strong row needs
#: a strong *head*, and averaging over the whole list would let a long tail of
#: weak matches veto a good one.
BYW_STRENGTH_SAMPLE = 5


def find_similar_by_tags(
    db: Session,
    reference_tmdb_id: int,
    media_type: str,
    exclude_ids: Set[int],
    limit: int = 40,
    user_id: int = 0,
    return_scores: bool = False,
) -> list:
    """
    Find items with similar tags to a reference item.

    A thin wrapper over :func:`app.relevance.similar_by_vector` -- real cosine
    over IDF-weighted tag vectors, now with the catalog quality floor applied
    inside that call.

    This used to count shared tags and tie-break on average confidence, which
    made "Because You Watched" mostly return other Dramas: a raw count is
    dominated by whichever tags are ubiquitous, and with 2.6 tags per title
    before the re-tag there was little else to go on. The vector query drops
    reference tags held by more than ``similarity_ubiquitous_df_ratio`` of the
    corpus, so similarity is driven by what is *distinctive* about a title.

    Returns ``[]`` when the neighbour list is too weak to justify the row --
    see :data:`MIN_BYW_SIMILARITY`. The caller treats that as "no row", not as
    an error.

    Args:
        db: Database session
        reference_tmdb_id: TMDB ID of the seed item
        media_type: 'movie' or 'tv' — results are filtered to this type
        exclude_ids: TMDB IDs to exclude (e.g. user's watched items)
        limit: Max results
        user_id: Seeds the daily tier shuffle. Defaulted to 0 for years, which
            meant every profile saw the identical permutation of a row that is
            supposed to be about them.

    Returns:
        TMDB IDs ranked by tag similarity, or ``(id, cosine)`` pairs when
        ``return_scores`` is true.
    """
    from app import relevance

    ranked = relevance.similar_by_vector(
        db,
        reference_tmdb_id,
        media_type,
        limit=max(limit * 3, limit),
        exclude_ids=list(exclude_ids),
    )
    if not ranked:
        logger.debug(f"No tag vector for {media_type} {reference_tmdb_id}")
        return []

    # Two independent ways a seed fails to justify a shelf, and cosine only
    # detects one of them.
    #
    # `weight_norm` is L2-normalised per title, so two titles with identical tag
    # profiles score 1.0 no matter how ubiquitous those tags are: a seed tagged
    # only Drama + Suspenseful produces a row of perfect 1.0 "matches" that are
    # alike in nothing anyone would notice. That is what the discriminating-tag
    # check catches. The similarity floor catches the other shape -- a seed with
    # good tags whose nearest neighbours barely overlap it.
    if relevance.discriminating_tag_count(db, reference_tmdb_id, media_type) < 2:
        logger.debug(
            f"BYW seed {media_type} {reference_tmdb_id}: fewer than two "
            "discriminating tags — no row"
        )
        return []

    # The threshold is a membership rule, not a shelf-level average. A strong
    # top five may not launder weak tail items into a row that claims every
    # title resembles the named seed.
    ranked = [pair for pair in ranked if pair[1] >= MIN_BYW_SIMILARITY]
    if not ranked:
        return []

    head = ranked[:BYW_STRENGTH_SAMPLE]
    strength = sum(score for _, score in head) / len(head)
    if strength < MIN_BYW_SIMILARITY:
        logger.debug(
            f"BYW seed {media_type} {reference_tmdb_id}: top-{len(head)} similarity "
            f"{strength:.3f} < {MIN_BYW_SIMILARITY} — no row"
        )
        return []

    # Similarity decides the candidate SET; the blend decides the order within
    # it, the same way it does for every other row. Cosine alone put a
    # thinly-tagged near-duplicate above a well-known title that matched almost
    # as closely, which is exactly what this row was being judged on.
    blended = _blend_ids(db, ranked, media_type)
    ordered = shuffle_within_tiers(blended, _daily_rng(user_id))[:limit]
    if return_scores:
        similarities = {tmdb_id: score for tmdb_id, score in ranked}
        return [(tmdb_id, similarities[tmdb_id]) for tmdb_id in ordered]
    return ordered


def build_taste_profile(
    db: Session,
    watched_tmdb_ids: Sequence[int] | Sequence[tuple[int, float]],
    media_type: str,
    top_n_tags: int = 15,
) -> List[int]:
    """
    Build a user's taste profile from their watched items' tags.

    Returns tag ids, highest taste weight first -- a shim over
    :func:`app.relevance.build_taste_vector` for the callers that only need the
    identities. Anything that needs the weights should call the vector directly;
    ``main.py::_reorder_universals_by_taste`` does.

    Accepts either plain ids or ``(tmdb_id, factor)`` pairs. Callers that know
    the user pass factors from :func:`app.relevance.completion_weights`, so an
    abandoned title pushes its tags *down* instead of being merely absent.

    Ordering by raw frequency is what this replaces, and it was the mechanism
    behind the taste-convergence problem: the most *frequent* tags in any
    library are the ubiquitous ones, so every profile ranked Drama first and the
    top-15 lists came out nearly identical across users. IDF weighting puts
    Drama near 0.04 and Kaiju near 0.9, and the profiles separate.

    Returns:
        List of tag IDs representing the user's taste profile
    """
    if not watched_tmdb_ids:
        return []

    from app import relevance

    vector = relevance.build_taste_vector(
        db, list(watched_tmdb_ids), media_type, top_n=top_n_tags
    )
    return [tag_id for tag_id, _ in sorted(vector.items(), key=lambda kv: -kv[1])]


def find_recommendations_by_taste(
    db: Session,
    watched_tmdb_ids: Set[int],
    media_type: str,
    top_n_tags: int = 15,
    limit: int = 40,
    user_id: int = 0,
) -> List[int]:
    """
    Generate tag-based recommendations from a user's taste profile.

    Aggregates the user's most frequent + highest-confidence tags across all
    watched items, then finds unwatched items that best match that profile.

    Args:
        db: Database session
        watched_tmdb_ids: TMDB IDs of items the user has watched
        media_type: 'movie' or 'tv'
        top_n_tags: Number of top tags to use from taste profile
        limit: Max results
        user_id: Seeds the daily tier shuffle, so two profiles with similar
            taste do not get the identical permutation of it.

    Returns:
        List of TMDB IDs ranked by the blended score over taste relevance
    """
    from app import relevance as _relevance

    # The production path materialises the complete weighted vector and scores
    # with SUM(user_weight * title_weight). The former query threw the user
    # weights away after selecting the top tag IDs, so a weak 15th preference
    # counted exactly as much as the defining first one and profiles converged.
    if user_id > 0:
        affinity = _relevance.completion_weights(db, user_id, media_type)
        weighted_history = [
            (tmdb_id, affinity.get(tmdb_id, 0.6)) for tmdb_id in watched_tmdb_ids
        ]
        vector = _relevance.build_taste_vector(
            db, weighted_history, media_type, top_n=top_n_tags
        )
        if not vector:
            logger.debug(f"No taste profile for {media_type} — skipping tag recs")
            return []
        _relevance.store_taste_vector(db, user_id, media_type, vector)
        ranked = _relevance.taste_scored_candidates(
            db,
            user_id,
            media_type,
            limit=limit * 2,
            exclude_ids=list(watched_tmdb_ids),
        )
        blended = _blend_ids(db, ranked, media_type)
        return shuffle_within_tiers(blended, _daily_rng(user_id))[:limit]

    # Compatibility path for tests/one-off callers that do not have a User row.
    taste_tags = build_taste_profile(
        db, list(watched_tmdb_ids), media_type, top_n_tags=top_n_tags
    )
    if not taste_tags:
        logger.debug(f"No taste profile for {media_type} — skipping tag recs")
        return []

    # SUM(weight_norm) over the taste tags, over-fetched so the tier shuffle has
    # something to work with. Same substitution as everywhere else in this
    # module: a count of matched tags cannot distinguish a title that shares one
    # rare tag from one that shares one ubiquitous tag.
    relevance_expr = func.sum(MovieTag.weight_norm).label("relevance")
    candidates = (
        db.query(MovieTag.tmdb_id, relevance_expr)
        .filter(
            MovieTag.tag_id.in_(taste_tags),
            MovieTag.media_type == media_type,
            ~MovieTag.tmdb_id.in_(watched_tmdb_ids),
            MovieTag.weight_norm.isnot(None),
            # This function builds four rows -- Recommended and Top Picks, both
            # types -- and had no quality condition of any kind, so a title
            # with two votes that happened to carry one rare taste tag could
            # sit at the top of a viewer's recommendations.
            MovieTag.tmdb_id.in_(_quality_floor_ids(db, media_type)),
        )
        .group_by(MovieTag.tmdb_id)
    )
    ranked = _blend_candidates(db, candidates, media_type, limit * 2)
    return shuffle_within_tiers(ranked, _daily_rng(user_id))[:limit]


# ---------------------------------------------------------------------------
# Metadata backfill — ensure TMDB metadata exists for catalog items
# ---------------------------------------------------------------------------
_BACKFILL_CONCURRENCY = 8
_BACKFILL_NOT_FOUND_TTL = 24 * 3600
#: (media_type, tmdb_id) -> monotonic time until which a 404 is trusted.
_backfill_not_found: Dict[tuple, float] = {}


async def _backfill_metadata(
    db: Session,
    tmdb_ids_by_type: Dict[str, Set[int]],
) -> int:
    """Fetch and store MediaMetadata for TMDB IDs that are missing from the DB.

    This is needed when imported/local TMDB IDs have not yet been
    processed by initial_build or daily_update.  Without metadata the
    INNER JOIN in get_catalog_content() silently drops those items.

    Returns the number of items backfilled.
    """
    backfilled = 0

    for media_type, all_ids in tmdb_ids_by_type.items():
        if not all_ids:
            continue

        # Find which IDs already have metadata
        existing: Set[int] = set()
        id_list = list(all_ids)
        batch_sz = 500
        for i in range(0, len(id_list), batch_sz):
            batch = id_list[i : i + batch_sz]
            rows = (
                db.query(MediaMetadata.tmdb_id)
                .filter(
                    MediaMetadata.media_type == media_type,
                    MediaMetadata.tmdb_id.in_(batch),
                )
                .all()
            )
            existing.update(r.tmdb_id for r in rows)

        now = time.monotonic()
        missing = {
            tmdb_id
            for tmdb_id in all_ids - existing
            if _backfill_not_found.get((media_type, tmdb_id), 0.0) <= now
        }
        if not missing:
            continue

        logger.info(
            f"Backfilling metadata for {len(missing)} {media_type} items "
            f"({len(existing)} already cached)"
        )

        # Concurrent, but bounded below the TMDB client's own admission gate:
        # these were fetched one at a time, which made a profile with a few
        # dozen uncached titles spend minutes here inside its transaction.
        gate = asyncio.Semaphore(_BACKFILL_CONCURRENCY)

        async def fetch(tmdb_id: int, media_type: str = media_type):
            async with gate:
                try:
                    if media_type == "movie":
                        return tmdb_id, await tmdb_client.get_movie(tmdb_id)
                    return tmdb_id, await tmdb_client.get_tv_show(tmdb_id)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 404:
                        # Dead ids were re-fetched for every profile every night.
                        _backfill_not_found[(media_type, tmdb_id)] = (
                            time.monotonic() + _BACKFILL_NOT_FOUND_TTL
                        )
                    logger.debug(
                        f"Could not fetch metadata for {media_type}/{tmdb_id}: {e}"
                    )
                except Exception as e:
                    logger.debug(
                        f"Could not fetch metadata for {media_type}/{tmdb_id}: {e}"
                    )
                return tmdb_id, None

        for tmdb_id, details in await asyncio.gather(
            *(fetch(tmdb_id) for tmdb_id in sorted(missing))
        ):
            if details is None:
                continue
            try:
                meta_dict = tmdb_client.extract_metadata(
                    details, media_type  # type: ignore[arg-type]
                )
                # This call site had the non-destructive pattern inline and the
                # other three did not, which is how the clobbering bug survived.
                # One definition now.
                upsert_media_metadata(db, meta_dict)
                backfilled += 1
            except Exception as e:
                logger.debug(
                    f"Could not store metadata for {media_type}/{tmdb_id}: {e}"
                )

        db.flush()

    if backfilled:
        logger.info(f"Backfilled metadata for {backfilled} items total")

    return backfilled


# ---------------------------------------------------------------------------
# Helper functions used by local sync logic
# ---------------------------------------------------------------------------


# ``_pick_byw_seeds`` used to live here: the Trakt-shaped seed picker, walking a
# ``recent_history`` list of ``{"movie": {...}} / {"show": {...}}`` dicts with
# nested ``ids.tmdb``. Nothing has produced that shape since Trakt was dropped
# (README: the TRAKT_* variables appear only in CI and are not read by the
# application), and it had no callers.
#
# Deleted rather than left in place because it was a near-duplicate of
# ``_pick_byw_seeds_local`` carrying the *old* seed ranking -- sort by tag count,
# pin the top -- so anyone reading this module to answer "how is a Because You
# Watched seed chosen?" found two answers, and the wrong one first.

# ---------------------------------------------------------------------------
# Local (scrobble) sync — builds catalogs from WatchEvent table
# ---------------------------------------------------------------------------


def _pick_byw_seeds_local(
    db: Session,
    user_id: int,
    all_watched_ids: Set[int],
    max_seeds: int = 5,
) -> List[Dict]:
    """Pick BYW seed items from real completed local playback only.

    Seeds are ranked by **how much evidence there is that the viewer liked the
    title**, not by how many tags it happens to carry.

    Tag count was the old ordering and it is close to meaningless here: it
    tracks enrichment coverage and popularity, so the pinned top seed was
    reliably whichever watched title TMDB and MDBList knew most about. Worse,
    Imported history continues to shape taste vectors and Top Picks, but it can
    never support the explicit product claim "Because You Watched X".

    :func:`app.relevance.completion_weights` already draws that distinction --
    1.0 for a completion, 0.6 for an import, negative for a title abandoned
    early -- and it already reads watch *progress*, so a series bailed on after
    two episodes is evidence against, not silence. Ranking on it means the row
    is seeded by something the viewer finished. Recency breaks ties, because
    between two equally-liked titles the more recent one explains the row
    better.

    A title still needs at least one tag to be usable as a similarity seed, so
    the tag count survives as a filter -- just not as the ranking.
    """

    recent = (
        db.query(
            WatchEvent.tmdb_id,
            WatchEvent.media_type,
            WatchEvent.title,
            func.max(WatchEvent.created_at).label("last_watched"),
        )
        .filter(
            WatchEvent.user_id == user_id,
            WatchEvent.action.in_(SEED_ACTIONS),
            WatchEvent.tmdb_id.isnot(None),
        )
        .group_by(WatchEvent.tmdb_id, WatchEvent.media_type, WatchEvent.title)
        .order_by(func.max(WatchEvent.created_at).desc())
        .limit(BYW_CANDIDATE_HISTORY)
        .all()
    )

    from app import relevance as _relevance

    affinity = {
        media_type: _relevance.completion_weights(db, user_id, media_type)
        for media_type in ("movie", "tv")
    }

    seen_ids: Set[tuple[str, int]] = set()
    seeds: List[Dict] = []
    candidates: List[Dict] = []

    for order, row in enumerate(recent):
        identity = (str(row.media_type), int(row.tmdb_id))
        if identity in seen_ids:
            continue
        seen_ids.add(identity)
        candidates.append(
            {
                "tmdb_id": row.tmdb_id,
                "media_type": row.media_type,
                "title": row.title,
                # `recent` is already newest-first, so position is recency.
                "recency_rank": order,
                # Absent from completion_weights means the title has an event
                # but no progress row to grade -- treat as a weak positive
                # rather than as a dislike.
                "affinity": affinity.get(str(row.media_type), {}).get(
                    int(row.tmdb_id), 0.5
                ),
            }
        )

    if candidates:
        # Batch tag-count query
        candidate_pairs = [(c["tmdb_id"], c["media_type"]) for c in candidates]
        tag_counts_rows = (
            db.query(MovieTag.tmdb_id, MovieTag.media_type, func.count(MovieTag.tag_id))
            .filter(tuple_(MovieTag.tmdb_id, MovieTag.media_type).in_(candidate_pairs))
            .group_by(MovieTag.tmdb_id, MovieTag.media_type)
            .all()
        )
        tag_counts = {(r[0], r[1]): r[2] for r in tag_counts_rows}

        # Batch title lookup for items missing WatchEvent.title
        missing_title_pairs = [
            (c["tmdb_id"], c["media_type"])
            for c in candidates
            if not c["title"] and tag_counts.get((c["tmdb_id"], c["media_type"]), 0) > 0
        ]
        title_map: dict = {}
        if missing_title_pairs:
            title_rows = (
                db.query(
                    MediaMetadata.tmdb_id, MediaMetadata.media_type, MediaMetadata.title
                )
                .filter(
                    tuple_(MediaMetadata.tmdb_id, MediaMetadata.media_type).in_(
                        missing_title_pairs
                    )
                )
                .all()
            )
            title_map = {(r.tmdb_id, r.media_type): r.title for r in title_rows}

        # A seed with no resolvable title is dropped rather than named
        # "Unknown": the row's whole claim is "because you watched X", and
        # "Because You Watched Unknown" is worse than one fewer row -- two such
        # seeds in the same media family even produced two identically-named
        # rows.
        tagged = [
            {
                "title": title,
                "tmdb_id": c["tmdb_id"],
                "media_type": c["media_type"],
                "tag_count": tag_counts.get((c["tmdb_id"], c["media_type"]), 0),
                "affinity": c["affinity"],
                "recency_rank": c["recency_rank"],
            }
            for c in candidates
            if tag_counts.get((c["tmdb_id"], c["media_type"]), 0) > 0
            # A title the viewer demonstrably bailed on is not something to
            # build a shelf around, however well tagged it is.
            and c["affinity"] > 0
            and (
                title := (
                    c["title"] or title_map.get((c["tmdb_id"], c["media_type"]), "")
                ).strip()
            )
        ]

        if len(tagged) > max_seeds:
            tagged.sort(key=lambda x: (-x["affinity"], x["recency_rank"]))
            rng = _daily_rng(user_id)
            rest = tagged[1:]
            rng.shuffle(rest)
            seeds = [tagged[0]] + rest[: max_seeds - 1]
        else:
            seeds = tagged

    logger.info(f"Selected {len(seeds)} local BYW seeds: {[s['title'] for s in seeds]}")
    return seeds


def _find_hidden_gems_by_taste(
    db: Session,
    taste_tag_ids: List[int],
    media_type: str,
    exclude_ids: Set[int],
    limit: int = 100,
    user_id: int = 0,
) -> List[int]:
    """Find high-quality, low-popularity items matching the user's taste.

    "Gem" is two claims: good, and overlooked. The second is
    ``popularity < GEM_MAX_POPULARITY`` and is what makes this row different
    from Recommended.

    The first used to be a hardcoded ``vote_average >= 7.0`` -- a fourth
    definition of "good" in a product that already had three, and the weakest
    of them: TMDB's average is compressed into roughly 6.5-7.5 and does almost
    no discriminating. It now uses :func:`app.catalog_generator.quality_expr`,
    the same MDBList-consensus-where-we-have-it score every other row is gated
    and ranked by, so a gem is measurably better than the floor rather than
    merely above a number that most of the library clears.
    """
    if not taste_tag_ids:
        return []

    from app.catalog_generator import quality_expr

    relevance_expr = func.sum(MovieTag.weight_norm).label("relevance")
    candidates = (
        db.query(MovieTag.tmdb_id, relevance_expr)
        .join(
            MediaMetadata,
            (MovieTag.tmdb_id == MediaMetadata.tmdb_id)
            & (MovieTag.media_type == MediaMetadata.media_type),
        )
        .filter(
            MovieTag.tag_id.in_(taste_tag_ids),
            MovieTag.media_type == media_type,
            MovieTag.weight_norm.isnot(None),
            quality_expr() >= GEM_MIN_QUALITY,
            MediaMetadata.vote_count >= GEM_MIN_VOTES,
            MediaMetadata.popularity < GEM_MAX_POPULARITY,
        )
        .group_by(MovieTag.tmdb_id)
    )
    if exclude_ids:
        candidates = candidates.filter(~MovieTag.tmdb_id.in_(exclude_ids))
    ranked = _blend_candidates(db, candidates, media_type, limit * 2)
    return shuffle_within_tiers(ranked, _daily_rng(user_id))[:limit]


def _typed_id_filter(watched_movies: Set[int], watched_shows: Set[int]):
    """``(media_type, tmdb_id)`` membership, as two indexed IN lists.

    TMDB movie and series ids are separate namespaces -- 1396 is Breaking Bad
    as a series and an unrelated film as a movie. Filtering ``MovieTag.tmdb_id``
    against a union of both sets therefore lets a watched movie suppress, or
    boost, a completely unrelated show across a 316k-title corpus.

    Two ``AND``ed IN lists rather than ``tuple_(...).in_(pairs)``: the tuple
    form is not indexable on either backend, and the two lists together are the
    same cardinality as the union that was being passed before.
    """
    clauses = []
    if watched_movies:
        clauses.append(
            and_(MovieTag.media_type == "movie", MovieTag.tmdb_id.in_(watched_movies))
        )
    if watched_shows:
        clauses.append(
            and_(MovieTag.media_type == "tv", MovieTag.tmdb_id.in_(watched_shows))
        )
    if not clauses:
        return None
    return or_(*clauses)


def _find_genre_taste_catalogs(
    db: Session,
    watched_movie_ids: Set[int],
    watched_show_ids: Set[int],
    taste_tag_ids: List[int],
    max_catalogs: int = 4,
    limit: int = 100,
    user_id: int = 0,
) -> List[Dict]:
    """Generate taste-specific catalog rows (genre, mood, plot, character, etc.).

    Fetches a pool of candidate tags across all categories and rotates the
    selection daily so users see different rows each sync.

    Takes the watched sets separately rather than as one union: they are the
    profile's history AND its exclusion set, and both uses need the media type
    to avoid the id-namespace collision described in ``_typed_id_filter``.

    Returns a list of dicts with keys: slot_id, name, media_type, tmdb_ids.
    """
    watched_filter = _typed_id_filter(watched_movie_ids, watched_show_ids)
    if not taste_tag_ids or watched_filter is None:
        return []

    # Fetch top tags across ALL categories, weighted by how often the user
    # has watched items with that tag — includes genre, mood, plot, style,
    # character, era, region
    top_tags = (
        db.query(
            Tag.id,
            Tag.name,
            Tag.category,
            func.count().label("cnt"),
        )
        .join(MovieTag, MovieTag.tag_id == Tag.id)
        .filter(watched_filter)
        .group_by(Tag.id, Tag.name, Tag.category)
        .order_by(func.count().desc())
        .limit(TASTE_CATALOG_POOL)
        .all()
    )

    if not top_tags:
        return []

    rng = _daily_rng(user_id)

    # Always show the user's #1 tag; rotate the remaining slots from the pool
    # Cycle through all top tags — advance the window by max_catalogs each day
    # so every tag gets its turn and no two consecutive days show the same set
    day_of_year = utc_now().timetuple().tm_yday
    pool_size = len(top_tags)
    start = (day_of_year * max_catalogs) % pool_size
    # Index-wrapping repeats a tag once the pool is smaller than max_catalogs,
    # and nothing downstream deduped, so a two-tag profile got two rows with
    # the same name and near-identical contents. A thin pool now yields fewer
    # rows, which the caller already handles for a tag that returns no results.
    selected = list(
        dict.fromkeys(top_tags[(start + i) % pool_size] for i in range(max_catalogs))
    )

    catalogs: List[Dict] = []
    taste_set = set(taste_tag_ids)
    # Same reasoning as BYW: the winning tag's dominant media type decides the
    # row's type, and the tag selection deliberately rotates daily, so a
    # positionally-numbered slot changes type on its own schedule. Numbering per
    # type makes the id's type permanent.
    taste_used = {"movie": 0, "tv": 0}

    for tag_row in selected:
        if sum(taste_used.values()) >= max_catalogs:
            break
        # The row is NAMED after this tag, so its members must carry it.
        #
        # The filter used to be `tag_id IN (taste_set | {anchor})` with
        # `HAVING count >= 2`, which any two tags from the fifteen-tag taste
        # profile satisfied. The anchor was one of sixteen ways to qualify, not
        # a requirement -- so a row headed "Because You Like It Dark" could
        # legitimately contain nothing tagged Dark, and routinely did. That is
        # the whole of the "categories don't line up" complaint in one clause.
        anchored = (
            db.query(MovieTag.tmdb_id, MovieTag.media_type)
            .filter(
                MovieTag.tag_id == tag_row.id,
                MovieTag.weight_norm >= ANCHOR_MIN_WEIGHT_NORM,
            )
            .subquery()
        )
        relevance_expr = func.sum(MovieTag.weight_norm).label("relevance")
        results = (
            db.query(MovieTag.tmdb_id, MovieTag.media_type, relevance_expr)
            .join(
                anchored,
                and_(
                    anchored.c.tmdb_id == MovieTag.tmdb_id,
                    anchored.c.media_type == MovieTag.media_type,
                ),
            )
            .filter(
                MovieTag.tag_id.in_(taste_set | {tag_row.id}),
                ~watched_filter,
                MovieTag.weight_norm.isnot(None),
                # Same hole as find_recommendations_by_taste: these four rows
                # had no quality condition at all. Both media types, because
                # this query decides the row's type from the result mix.
                or_(
                    and_(
                        MovieTag.media_type == "movie",
                        MovieTag.tmdb_id.in_(_quality_floor_ids(db, "movie")),
                    ),
                    and_(
                        MovieTag.media_type == "tv",
                        MovieTag.tmdb_id.in_(_quality_floor_ids(db, "tv")),
                    ),
                ),
            )
            .group_by(MovieTag.tmdb_id, MovieTag.media_type)
            .having(func.count(func.distinct(MovieTag.tag_id)) >= 2)
            .order_by(relevance_expr.desc(), MovieTag.tmdb_id)
            .limit(limit * 2)
            .all()
        )

        if len(results) < MIN_ROW_ITEMS:
            logger.debug(
                f"  taste row '{tag_row.name}': {len(results)} matches "
                f"< {MIN_ROW_ITEMS} — skipped"
            )
            continue

        movie_count = sum(1 for r in results if r[1] == "movie")
        tv_count = sum(1 for r in results if r[1] == "tv")
        media_type = "movie" if movie_count >= tv_count else "tv"
        if taste_used.get(media_type, 0) >= TASTE_SLOTS.get(media_type, 0):
            continue

        typed = [r for r in results if r[1] == media_type] or results
        if len(typed) < MIN_ROW_ITEMS:
            # The mix can be dominated by one type while the winning type is
            # still thin -- checking only the combined count would let a
            # 30-match row render as 6 movies.
            logger.debug(
                f"  taste row '{tag_row.name}': {len(typed)} {media_type} matches "
                f"< {MIN_ROW_ITEMS} — skipped"
            )
            continue

        blended = _blend_ids(
            db, [(int(r[0]), float(r[2] or 0.0)) for r in typed], media_type
        )
        tiered = shuffle_within_tiers(blended, rng)

        taste_used[media_type] += 1
        catalogs.append(
            {
                "slot_id": typed_slot_id("taste", media_type, taste_used[media_type]),
                # Facet-aware naming instead of "{tag} For You", which produced
                # "Dark For You" and "Slow-Burn For You". taste_row_name has been
                # written and tested since the taxonomy landed and was never
                # called; it yields "Because You Like It Dark", "More Slasher",
                # "Stories About Grief" depending on the tag's facet.
                "name": _taste_row_name(tag_row.name),
                "media_type": media_type,
                "tmdb_ids": tiered[:limit],
            }
        )

    return catalogs


async def _save_metadata_from_results(
    db: Session, results: list, media_type: str
) -> list[int]:
    """Extract IDs and save metadata from TMDB list results.

    Filters to English-language only. Uses selective non-null updates so
    richer detail-level fields (imdb_id, logo) from prior backfills aren't
    overwritten with None from list responses.
    """
    tmdb_ids: list[int] = []
    for r in results:
        if not r.get("id"):
            continue
        tid = r["id"]
        if tid in tmdb_ids:
            continue
        if r.get("original_language") != "en":
            continue
        tmdb_ids.append(tid)
        try:
            meta_dict = tmdb_client.extract_metadata(
                r, media_type  # type: ignore[arg-type]
            )
            upsert_media_metadata(db, meta_dict)
        except Exception as e:
            logger.debug(f"Metadata save failed for {media_type}/{tid}: {e}")
    db.flush()
    return tmdb_ids


def _store_digital_release_dates(db: Session, dates: dict[int, str | None]) -> None:
    """Record digital release dates gathered during the home-release filter.

    That filter fetches the ``release_dates`` payload anyway, so keeping the date
    costs no extra TMDB traffic. IDs without a metadata row yet are skipped — the
    later backfill picks those up through ``extract_metadata``. A successful
    authoritative lookup may also clear a stale date that the corrected extractor
    now recognizes as a late catalog entry.
    """
    if not dates:
        return
    rows = (
        db.query(MediaMetadata)
        .options(defer(MediaMetadata.raw_data))
        .filter(
            MediaMetadata.media_type == "movie",
            MediaMetadata.tmdb_id.in_(list(dates)),
        )
        .all()
    )
    for row in rows:
        row.digital_release_date = dates[int(row.tmdb_id)]  # type: ignore[assignment]
    db.flush()


async def _filter_home_released_movies(
    db: Session,
    tmdb_ids: list[int],
    *,
    digital_window: tuple[str, str] | None = None,
) -> list[int]:
    """Post-filter movies to those with confirmed US digital or physical release.

    Calls /movie/{id}/release_dates concurrently (max 10 in-flight) and keeps
    only IDs that have release type 4 (digital) or 5 (physical) for the US. When
    ``digital_window`` is supplied, the corrected type-4 date must fall inside
    that inclusive range; this is the stricter contract used by New Releases.
    General shelves fail open on a per-item lookup error to avoid over-filtering,
    while a date-bounded shelf fails closed because it cannot verify its promise.

    The digital (type 4) date from the same payload is persisted for the catalog
    badges. The extractor also corrects late storefront/rerelease entries before
    either persistence or the optional window check.
    """
    if not tmdb_ids:
        return tmdb_ids

    sem = asyncio.Semaphore(10)

    async def check(tmdb_id: int) -> tuple[int, bool, str | None, bool]:
        async with sem:
            try:
                data = await tmdb_client.get_movie_release_dates(tmdb_id)
                digital_date = tmdb_client.digital_release_date(data)
                in_window = digital_window is None or (
                    digital_date is not None
                    and digital_window[0] <= digital_date <= digital_window[1]
                )
                return (
                    tmdb_id,
                    tmdb_client.has_home_release(data) and in_window,
                    digital_date,
                    True,
                )
            except Exception as e:
                logger.debug(f"Release date check skipped for movie/{tmdb_id}: {e}")
                return tmdb_id, digital_window is None, None, False

    checks = await asyncio.gather(*[check(tid) for tid in tmdb_ids])
    valid = {tid for tid, ok, _, _ in checks if ok}
    # Written after the gather, not inside it: the Session is not safe to touch
    # from concurrent tasks. Failed lookups are omitted so an outage cannot clear
    # good stored dates; successful None results deliberately clear stale ones.
    _store_digital_release_dates(
        db, {tid: date for tid, _, date, fetched in checks if fetched}
    )
    filtered = [tid for tid in tmdb_ids if tid in valid]
    excluded = len(tmdb_ids) - len(filtered)
    if excluded:
        sync_status["home_release_filtered"] += excluded
        logger.info(
            f"Home release/date post-filter removed {excluded}/{len(tmdb_ids)} movies"
        )
    return filtered


async def _filter_home_mockbusters(tmdb_ids: list[int]) -> list[int]:
    """Remove known or TMDB-tagged mockbusters from movie Home feeds.

    TMDB's discover endpoint can exclude a keyword directly, but its trending
    endpoint cannot.  This lightweight post-filter closes that gap. Provider
    failures fail open so an outage cannot empty Trending; the two confirmed
    incidents remain fail-closed through the central known-id policy.
    """
    if not tmdb_ids:
        return tmdb_ids

    sem = asyncio.Semaphore(10)

    async def keep(tmdb_id: int) -> tuple[int, bool]:
        if is_home_mockbuster(None, tmdb_id=tmdb_id, media_type="movie"):
            return tmdb_id, False
        async with sem:
            try:
                payload = await tmdb_client.get_movie_keywords(tmdb_id)
                return tmdb_id, not keyword_payload_is_mockbuster(payload)
            except Exception as e:
                logger.debug(
                    f"Mockbuster keyword check skipped for movie/{tmdb_id}: {e}"
                )
                return tmdb_id, True

    checks = await asyncio.gather(*[keep(tmdb_id) for tmdb_id in tmdb_ids])
    allowed = {tmdb_id for tmdb_id, should_keep in checks if should_keep}
    filtered = [tmdb_id for tmdb_id in tmdb_ids if tmdb_id in allowed]
    excluded = len(tmdb_ids) - len(filtered)
    if excluded:
        sync_status["mockbusters_filtered"] += excluded
        logger.info(f"Home mockbuster filter removed {excluded}/{len(tmdb_ids)} movies")
    return filtered


async def _prefetch_common_catalog_data(db: Session) -> dict[str, list[int]]:
    """Fetch Trending and New Releases from TMDB once for all users.

    Called once at the start of a bulk sync and shared across all users so
    we make ~18 TMDB requests total instead of 18 × N (one set per user).
    Returns a dict mapping slot_id → ordered list of tmdb_ids, with
    MediaMetadata already saved/updated in the database.
    """
    result: dict[str, list[int]] = {}
    cutoff_90d = (utc_now() - timedelta(days=90)).strftime("%Y-%m-%d")
    today = utc_now().strftime("%Y-%m-%d")

    # Trending (day + week windows, 3 pages each)
    for media_type, slot in [("movie", "trending-movie"), ("tv", "trending-series")]:
        try:
            all_results: list = []
            for time_window in ["day", "week"]:
                for page in range(1, 4):
                    data = (
                        await tmdb_client.get_trending_movies(time_window, page=page)
                        if media_type == "movie"
                        else await tmdb_client.get_trending_tv_shows(
                            time_window, page=page
                        )
                    )
                    all_results.extend(data.get("results", []))
            ids = await _save_metadata_from_results(db, all_results, media_type)
            if media_type == "movie":
                ids = await _filter_home_mockbusters(ids)
                ids = await _filter_home_released_movies(db, ids)
            result[slot] = ids
            logger.info(f"Prefetched {slot}: {len(result[slot])} items")
        except Exception as e:
            logger.warning(f"Prefetch {slot} failed: {e}")
            result[slot] = []

    # New Releases — last 90 days, US digital release. TMDB's discover filter can
    # surface an old movie when a retailer adds a fresh regional type-4 record, so
    # the detail-level date is checked against the same window below.
    for media_type, slot in [
        ("movie", "new-releases-movie"),
        ("tv", "new-releases-series"),
    ]:
        try:
            all_results = []
            for page in range(1, 4):
                if media_type == "movie":
                    data = await tmdb_client._request(
                        "/discover/movie",
                        params={
                            "page": page,
                            "sort_by": "popularity.desc",
                            "release_date.gte": cutoff_90d,
                            "release_date.lte": today,
                            "region": "US",
                            "with_release_type": "4",
                            "with_original_language": "en",
                            "without_keywords": HOME_MOVIE_WITHOUT_KEYWORDS,
                            "include_adult": False,
                        },
                    )
                else:
                    data = await tmdb_client._request(
                        "/discover/tv",
                        params={
                            "page": page,
                            "sort_by": "popularity.desc",
                            "first_air_date.gte": cutoff_90d,
                            "first_air_date.lte": today,
                            "with_original_language": "en",
                            "without_keywords": "210024",
                            "include_adult": False,
                        },
                    )
                all_results.extend(data.get("results", []))
            ids = await _save_metadata_from_results(db, all_results, media_type)
            if media_type == "movie":
                ids = await _filter_home_released_movies(
                    db, ids, digital_window=(cutoff_90d, today)
                )
            result[slot] = ids
            logger.info(f"Prefetched {slot}: {len(result[slot])} items")
        except Exception as e:
            logger.warning(f"Prefetch {slot} failed: {e}")
            result[slot] = []

    # Popular
    for media_type, slot in [("movie", "popular-movie"), ("tv", "popular-series")]:
        if slot not in CORE_COMMON_SLOTS:
            continue
        try:
            all_results = []
            for page in range(1, 4):
                if media_type == "movie":
                    data = await tmdb_client._request(
                        "/discover/movie",
                        params={
                            "page": page,
                            "sort_by": "popularity.desc",
                            "region": "US",
                            "with_release_type": "4|5",
                            "with_original_language": "en",
                            "without_keywords": HOME_MOVIE_WITHOUT_KEYWORDS,
                            "include_adult": False,
                        },
                    )
                else:
                    data = await tmdb_client.get_popular_tv_shows(page)
                all_results.extend(data.get("results", []))
            ids = await _save_metadata_from_results(db, all_results, media_type)
            if media_type == "movie":
                ids = await _filter_home_released_movies(db, ids)
            result[slot] = ids
            logger.info(f"Prefetched {slot}: {len(result[slot])} items")
        except Exception as e:
            logger.warning(f"Prefetch {slot} failed: {e}")
            result[slot] = []

    # Coming Soon — announced but not yet out.
    #
    # Deliberately the one row with no tag or quality signal in it, ordered by
    # date alone. An unreleased title cannot be ranked by the tag layer and it
    # is honest to say so: the nightly tagger's discover window is capped at
    # today (app/tmdb_client.py::get_new_releases_this_week), the universal
    # vote floor is 10 and a pre-release title has ~0 votes, and reviews --
    # which is where mood, theme and style actually come from -- do not exist
    # for a film nobody has seen. Soonest-first is the only ordering the data
    # supports, and it is also the one a viewer wants.
    for media_type, slot in [
        ("movie", "coming-soon-movie"),
        ("tv", "coming-soon-series"),
    ]:
        if slot not in CORE_COMMON_SLOTS:
            continue
        try:
            all_results = []
            for page in range(1, 3):
                if media_type == "movie":
                    data = await tmdb_client._request(
                        "/discover/movie",
                        params={
                            "page": page,
                            "sort_by": "primary_release_date.asc",
                            "primary_release_date.gte": today,
                            "region": "US",
                            "with_original_language": "en",
                            "without_keywords": HOME_MOVIE_WITHOUT_KEYWORDS,
                            "include_adult": False,
                            # A real floor, not the 0 that was here under this
                            # same comment. Unreleased titles have few votes by
                            # definition, so this has to be low enough to admit
                            # a genuine upcoming release and high enough to
                            # exclude the placeholder rows TMDB carries for
                            # films with an announced date and nothing else.
                            "vote_count.gte": COMING_SOON_MIN_VOTES,
                            "with_release_type": "2|3",
                        },
                    )
                else:
                    data = await tmdb_client._request(
                        "/discover/tv",
                        params={
                            "page": page,
                            "sort_by": "first_air_date.asc",
                            "first_air_date.gte": today,
                            "with_original_language": "en",
                            "without_keywords": "210024",
                            "include_adult": False,
                        },
                    )
                all_results.extend(data.get("results", []))
            # NOT passed through _filter_home_released_movies: that helper keeps
            # only titles with a US digital or physical release entry, which is
            # exactly what an unreleased title does not have yet.
            result[slot] = await _save_metadata_from_results(
                db, all_results, media_type
            )
            logger.info(f"Prefetched {slot}: {len(result[slot])} items")
        except Exception as e:
            logger.warning(f"Prefetch {slot} failed: {e}")
            result[slot] = []

    _dedupe_common_slots(result)
    db.commit()
    return result


#: Later slots yield ids already claimed by an earlier one.
#:
#: popular-* is the same /discover query as new-releases-* without the date
#: bound, so it is a strict superset by construction and the two rows largely
#: repeated each other. Trending keeps its ids because it is the row a viewer
#: reads first; Popular is the one that can afford to lose them.
_DEDUPE_PRIORITY: tuple[str, ...] = (
    "coming-soon-movie",
    "coming-soon-series",
    "trending-movie",
    "trending-series",
    "new-releases-movie",
    "new-releases-series",
    "popular-movie",
    "popular-series",
)


def _dedupe_common_slots(result: "dict[str, list[int]]") -> None:
    """Drop ids from later slots that an earlier slot already claimed.

    Per media type -- a movie id and a series id are separate namespaces, so
    they must not suppress each other.
    """
    claimed: dict[str, set[int]] = {"movie": set(), "series": set()}
    for slot in _DEDUPE_PRIORITY:
        ids = result.get(slot)
        if not ids:
            continue
        bucket = claimed["series" if slot.endswith("-series") else "movie"]
        kept = [tid for tid in ids if tid not in bucket]
        dropped = len(ids) - len(kept)
        if dropped:
            logger.info(
                f"{slot}: dropped {dropped} ids already shown in an earlier row"
            )
        bucket.update(kept)
        result[slot] = kept


async def _generate_common_catalogs(
    user: User,
    db: Session,
    catalog_gen: CatalogGenerator,
    catalogs_created: int,
    display: str,
    common_data: "dict[str, list[int]] | None" = None,
) -> int:
    """Write pre-fetched common catalog data to a user's rows, then backfill and commit.

    common_data comes from _prefetch_common_catalog_data() which is called
    once per bulk sync run. If None (e.g. single-user refresh), fetches inline.
    """
    from app.models import UserCatalog, UserCatalogContent

    if common_data is None:
        # Single-user path (e.g. a freshly created profile). Reuse recently
        # fetched shared data when available so we don't re-hammer TMDB; only
        # fetch inline on a cache miss, and cache a healthy result for the next
        # new profile.
        common_data = _get_cached_common_data()
        if common_data is None:
            # Its own session: the prefetch commits the metadata it stores, and
            # on this session that commit would publish the half-rebuilt rows
            # (prior snapshot already deleted) before the sync finished.
            from app.database import get_db

            with get_db() as prefetch_db:
                common_data = await _prefetch_common_catalog_data(prefetch_db)
            _store_common_data(common_data)

    core_persisted: set[str] = set()
    for slot, name, media_type, gen_method in TIMELY_FEED_ROWS:
        tmdb_ids = common_data.get(slot, [])
        if tmdb_ids:
            catalog_gen.save_user_catalog(
                user_id=user.id,
                slot_id=slot,
                name=name,
                media_type=media_type,
                tmdb_ids=tmdb_ids,
                generation_method=gen_method,
            )
            catalogs_created += 1
            if slot in CORE_COMMON_SLOTS:
                core_persisted.add(slot)
            logger.info(f"  {name}: {len(tmdb_ids)} items")

    # --- Backfill metadata for other user catalog items (BYW, recs, etc.) ---
    user_catalog_ids = [
        c.id
        for c in db.query(UserCatalog.id).filter(UserCatalog.user_id == user.id).all()
    ]
    if user_catalog_ids:
        rows = (
            db.query(UserCatalogContent.tmdb_id, UserCatalogContent.media_type)
            .filter(UserCatalogContent.catalog_id.in_(user_catalog_ids))
            .distinct()
            .all()
        )
        ids_by_type: Dict[str, Set[int]] = {"movie": set(), "tv": set()}
        for r in rows:
            ids_by_type.setdefault(r.media_type, set()).add(r.tmdb_id)
        try:
            await _backfill_metadata(db, ids_by_type)
        except Exception as e:
            logger.warning(f"Metadata backfill had errors: {e}")

    # Only mark the profile "synced" once the user-visible core rows are present.
    # Otherwise leave last_sync unset: the profile keeps re-triggering this
    # sync on its next requests until a healthy TMDB fetch lands the rows — instead of baking
    # in permanently-missing rows that only the nightly bulk sync would recover.
    if core_persisted >= CORE_COMMON_SLOTS:
        user.last_sync = utc_now()
    else:
        user.last_sync = None
        logger.warning(
            f"Common catalog sync incomplete for {display}: "
            f"{len(core_persisted)}/{len(CORE_COMMON_SLOTS)} core rows persisted — "
            f"leaving last_sync unset for retry"
        )
    from app.home_feed import invalidate_discovery

    invalidate_discovery(db, user.id)
    db.commit()

    logger.info(
        f"Local sync complete for {display}: {catalogs_created} catalogs generated"
    )
    return catalogs_created


async def _finalize_personal_catalogs(
    user: User,
    db: Session,
    catalogs_created: int,
    display: str,
) -> int:
    """Backfill, commit, and mark a personal-only catalog sync complete."""
    from app.models import UserCatalog, UserCatalogContent

    user_catalog_ids = [
        c.id
        for c in db.query(UserCatalog.id)
        .filter(
            UserCatalog.user_id == user.id,
            UserCatalog.slot_id.in_(VALID_SLOTS),
        )
        .all()
    ]
    if user_catalog_ids:
        rows = (
            db.query(UserCatalogContent.tmdb_id, UserCatalogContent.media_type)
            .filter(UserCatalogContent.catalog_id.in_(user_catalog_ids))
            .distinct()
            .all()
        )
        ids_by_type: Dict[str, Set[int]] = {"movie": set(), "tv": set()}
        for row in rows:
            ids_by_type.setdefault(row.media_type, set()).add(row.tmdb_id)
        try:
            await _backfill_metadata(db, ids_by_type)
        except Exception as exc:
            logger.warning(f"Metadata backfill had errors: {exc}")

    # A profile with no history legitimately has no personal rows. Marking it
    # synced prevents its requests from scheduling the same empty rebuild every
    # 30 seconds; synthesized universal rows remain its discovery experience.
    user.last_sync = utc_now()
    from app.home_feed import invalidate_discovery

    invalidate_discovery(db, user.id)
    db.commit()
    logger.info(
        f"Personal-only sync complete for {display}: "
        f"{catalogs_created} catalogs generated"
    )
    return catalogs_created


async def _generate_discovery_catalogs(
    user: User,
    db: Session,
    catalog_gen: CatalogGenerator,
    display: str,
) -> int:
    """Generate discovery catalogs for users with no watch history.

    Creates daily-rotating catalogs from universal content + quality picks.
    """
    import hashlib
    import random as _random
    from datetime import date

    catalogs_created = 0
    daily_seed = int(hashlib.md5(date.today().isoformat().encode()).hexdigest()[:8], 16)

    # "Discover Today" — sample from random universal categories, rotated daily
    for media_type, slot in [("movie", "discover-movie"), ("tv", "discover-series")]:
        try:
            categories = (
                db.query(UniversalCategory.id)
                .filter(
                    UniversalCategory.is_active.is_(True),
                    UniversalCategory.source == "generated",
                    UniversalCategory.media_type == media_type,
                )
                .all()
            )
            cat_ids = [c.id for c in categories]
            if cat_ids:
                rng = _random.Random(daily_seed + hash(media_type))
                picked = rng.sample(cat_ids, min(5, len(cat_ids)))
                ranked_lists: list[list[int]] = []
                for cid in picked:
                    rows = (
                        db.query(UniversalCatalogContent.tmdb_id)
                        .filter(UniversalCatalogContent.category_id == cid)
                        .order_by(UniversalCatalogContent.rank)
                        .limit(40)
                        .all()
                    )
                    ranked_lists.append([r.tmdb_id for r in rows])

                # Round-robin by rank, not set-union-then-shuffle.
                #
                # Every source row arrives ORDER BY rank -- the blended score
                # the whole relevance layer exists to compute. Pouring them
                # into a set and shuffling threw that away entirely and made
                # Discover Today a uniform sample of five categories' first 40
                # titles. Interleaving keeps each category's best at the front
                # while still mixing the categories, which is what the row is
                # for.
                seen: set[int] = set()
                tmdb_ids = []
                for position in range(
                    max((len(lst) for lst in ranked_lists), default=0)
                ):
                    for lst in ranked_lists:
                        if position < len(lst) and lst[position] not in seen:
                            seen.add(lst[position])
                            tmdb_ids.append(lst[position])
                tmdb_ids = tmdb_ids[:100]

                if tmdb_ids:
                    catalog_gen.save_user_catalog(
                        user_id=user.id,
                        slot_id=slot,
                        name=row_name(slot),
                        media_type=media_type,
                        tmdb_ids=tmdb_ids,
                        generation_method="discovery_daily",
                    )
                    catalogs_created += 1
                    logger.info(
                        f"  Discover Today ({media_type}): {len(tmdb_ids)} items"
                    )
        except Exception as e:
            logger.warning(f"  Discover Today ({media_type}) failed: {e}")

    # "Critically Acclaimed" — high-rated English films, shuffled daily.
    #
    # The media_type filter is load-bearing and was missing: the query selected
    # across both types and the result was saved as media_type='movie', so
    # every TV id landed in a movie row. get_catalog_content's outer join is on
    # (tmdb_id, media_type), so those rows missed and rendered as a card titled
    # "TMDB 1396" with no poster.
    #
    # Ranked by rating_consensus where it exists. This row's entire premise is
    # "critically acclaimed", and it was gating on TMDB's vote_average -- a
    # number compressed into 6.5-7.5 that does almost no ranking work -- while
    # a votes-weighted mean across IMDb, RT and Metacritic sat unread on the
    # same row for ~326,500 titles.
    try:
        from app.catalog_generator import quality_expr

        consensus = quality_expr()
        acclaimed = (
            db.query(MediaMetadata.tmdb_id)
            .filter(
                MediaMetadata.media_type == "movie",
                MediaMetadata.vote_count >= 1000,
                MediaMetadata.original_language == "en",
                consensus >= 7.5,
            )
            .order_by(consensus.desc())
            .limit(200)
            .all()
        )
        tmdb_ids = [r.tmdb_id for r in acclaimed]
        _random.Random(daily_seed).shuffle(tmdb_ids)
        tmdb_ids = tmdb_ids[:100]
        if tmdb_ids:
            catalog_gen.save_user_catalog(
                user_id=user.id,
                slot_id="discover-acclaimed",
                name=row_name("discover-acclaimed"),
                media_type="movie",
                tmdb_ids=tmdb_ids,
                generation_method="discovery_acclaimed",
            )
            catalogs_created += 1
            logger.info(f"  Critically Acclaimed: {len(tmdb_ids)} items")
    except Exception as e:
        logger.warning(f"  Critically Acclaimed failed: {e}")

    logger.info(f"  Generated {catalogs_created} discovery catalogs for {display}")
    return catalogs_created


async def sync_user_catalogs(
    user: User,
    db: Session,
    common_data: "dict[str, list[int]] | None" = None,
) -> int:
    """Build personalized catalogs from local watch events."""

    catalog_gen = CatalogGenerator(db)
    catalogs_created = 0
    display = user.display_name or user.user_key[:8]

    # User-scoped rows are a rotating snapshot, not an append-only collection.
    # The timely feeds are rebuilt into the same transaction below; legacy
    # Popular/Coming Soon/bootstrap rows remain retired.
    _reset_user_catalogs(user.id, db)

    logger.info(f"Syncing local catalogs for user {display}...")

    # ------------------------------------------------------------------
    # Step 1: Derive watched items from WatchEvent (action='complete')
    # ------------------------------------------------------------------
    # imdb-only events (tmdb_id NULL) can't feed TMDB-tag recommendations or the
    # TMDB-keyed catalogs, so they're excluded from every catalog-building query.
    completed = (
        db.query(WatchEvent.tmdb_id, WatchEvent.media_type)
        .filter(
            WatchEvent.user_id == user.id,
            WatchEvent.action.in_(SEED_ACTIONS),
            WatchEvent.tmdb_id.isnot(None),
        )
        .distinct()
        .all()
    )

    watched_movie_ids: Set[int] = {
        r.tmdb_id for r in completed if r.media_type == "movie"
    }
    watched_show_ids: Set[int] = {r.tmdb_id for r in completed if r.media_type == "tv"}

    # Include imported history for recommendation building. Imported history
    # can shape taste, but cannot create a Because You Watched claim.
    imported = (
        db.query(WatchEvent.tmdb_id, WatchEvent.media_type)
        .filter(
            WatchEvent.user_id == user.id,
            WatchEvent.action == "imported",
            WatchEvent.tmdb_id.isnot(None),
        )
        .distinct()
        .all()
    )
    all_movie_ids: Set[int] = watched_movie_ids | {
        r.tmdb_id for r in imported if r.media_type == "movie"
    }
    all_show_ids: Set[int] = watched_show_ids | {
        r.tmdb_id for r in imported if r.media_type == "tv"
    }

    logger.info(
        f"User {display}: "
        f"{len(all_movie_ids)} movies ({len(watched_movie_ids)} watched + "
        f"{len(all_movie_ids) - len(watched_movie_ids)} imported), "
        f"{len(all_show_ids)} shows ({len(watched_show_ids)} watched + "
        f"{len(all_show_ids) - len(watched_show_ids)} imported)"
    )

    has_history = bool(all_movie_ids or all_show_ids)

    if not has_history:
        return await _generate_common_catalogs(
            user, db, catalog_gen, catalogs_created, display, common_data=common_data
        )

    # ------------------------------------------------------------------
    # Step 2: "Because You Watched [Title]" — tag-based similarity
    # ------------------------------------------------------------------
    byw_seeds = _pick_byw_seeds_local(
        db, user.id, all_movie_ids | all_show_ids, max_seeds=8
    )

    # Fill each type's slots independently. Previously the seeds were numbered
    # in whatever order they arrived, so slot byw-1 was a series one night and a
    # movie the next -- flipping the row type under clients that had
    # already cached it. Seeds beyond a type's allocation are simply unused.
    byw_used = {"movie": 0, "tv": 0}
    for seed in byw_seeds:
        seed_type = seed["media_type"]
        if byw_used.get(seed_type, 0) >= BYW_SLOTS.get(seed_type, 0):
            continue
        exclude = all_movie_ids if seed_type == "movie" else all_show_ids
        similar = find_similar_by_tags(
            db,
            reference_tmdb_id=seed["tmdb_id"],
            media_type=seed_type,
            exclude_ids=exclude,
            limit=100,
            user_id=user.id,
            return_scores=True,
        )
        similar_ids = [int(tmdb_id) for tmdb_id, _ in similar]
        similarity_scores = [float(score) for _, score in similar]

        # `find_similar_by_tags` returns [] for a seed whose neighbour list is
        # too weak to justify the claim; MIN_ROW_ITEMS covers the separate case
        # of a list that is strong but short. Either way the seed is skipped and
        # the next one gets the slot, which is why more seeds are requested than
        # there are slots to fill.
        if len(similar_ids) >= MIN_ROW_ITEMS:
            from app import relevance

            basis_ids = relevance.reference_tag_ids(
                db, int(seed["tmdb_id"]), str(seed_type)
            )
            basis_rows = db.query(Tag.id, Tag.name).filter(Tag.id.in_(basis_ids)).all()
            basis_by_id = {int(tag_id): str(name) for tag_id, name in basis_rows}
            basis_tags = [
                basis_by_id[tag_id] for tag_id in basis_ids if tag_id in basis_by_id
            ]
            revision = utc_now().isoformat()
            byw_used[seed_type] += 1
            catalog_gen.save_user_catalog(
                user_id=user.id,
                slot_id=typed_slot_id("byw", seed_type, byw_used[seed_type]),
                name=f"Because You Watched {seed['title']}",
                media_type=seed_type,
                tmdb_ids=similar_ids,
                generation_method="because_you_watched",
                generation_params={
                    "seed": {
                        "tmdb_id": int(seed["tmdb_id"]),
                        "media_type": seed_type,
                        "title": seed["title"],
                    },
                    # Legacy scalar keys remain for old admin tooling.
                    "seed_title": seed["title"],
                    "seed_tmdb_id": int(seed["tmdb_id"]),
                    "basis_tags": basis_tags,
                    "algorithm_version": "idf-cosine-v2",
                    "generation_revision": revision,
                },
                match_scores=similarity_scores,
            )
            catalogs_created += 1
            logger.info(f"  BYW '{seed['title']}': {len(similar_ids)} items")

    # ------------------------------------------------------------------
    # Step 4: "Recommended For You" — tag-based taste profile
    # ------------------------------------------------------------------
    for media_label, watched_ids, media_type, slot in [
        ("movies", all_movie_ids, "movie", "rec-movie"),
        ("shows", all_show_ids, "tv", "rec-series"),
    ]:
        try:
            tmdb_ids = find_recommendations_by_taste(
                db,
                watched_tmdb_ids=watched_ids,
                media_type=media_type,
                limit=100,
                user_id=user.id,
            )
            if len(tmdb_ids) >= MIN_ROW_ITEMS:
                catalog_gen.save_user_catalog(
                    user_id=user.id,
                    slot_id=slot,
                    name=row_name(slot),
                    media_type=media_type,
                    tmdb_ids=tmdb_ids,
                    generation_method="tag_recommendations",
                )
                catalogs_created += 1
                logger.info(f"  Tag recs ({media_label}): {len(tmdb_ids)} items")
        except Exception as e:
            logger.warning(f"  Tag recs ({media_label}) failed: {e}")

    # ------------------------------------------------------------------
    # Step 4a: "Top Picks For You" — best taste matches (broader tags)
    # ------------------------------------------------------------------
    for media_label, watched_ids, media_type, slot in [
        ("movies", all_movie_ids, "movie", "picks-movie"),
        ("shows", all_show_ids, "tv", "picks-series"),
    ]:
        try:
            tmdb_ids = find_recommendations_by_taste(
                db,
                watched_tmdb_ids=watched_ids,
                media_type=media_type,
                top_n_tags=20,
                limit=100,
                user_id=user.id,
            )
            if len(tmdb_ids) >= MIN_ROW_ITEMS:
                catalog_gen.save_user_catalog(
                    user_id=user.id,
                    slot_id=slot,
                    name=row_name(slot),
                    media_type=media_type,
                    tmdb_ids=tmdb_ids,
                    generation_method="top_picks",
                )
                catalogs_created += 1
                logger.info(f"  Top Picks ({media_label}): {len(tmdb_ids)} items")
        except Exception as e:
            logger.warning(f"  Top Picks ({media_label}) failed: {e}")

    # ------------------------------------------------------------------
    # Step 4b: Genre/mood taste catalogs — "Sci-Fi For You", etc.
    # ------------------------------------------------------------------
    # Weighted by completion, so a series abandoned after two episodes pushes
    # its tags down rather than merely not lifting them.
    from app import relevance as _relevance

    movie_completion = _relevance.completion_weights(db, user.id, "movie")
    show_completion = _relevance.completion_weights(db, user.id, "tv")
    watched_movies = [
        (tmdb_id, movie_completion.get(tmdb_id, 1.0)) for tmdb_id in all_movie_ids
    ]
    watched_shows = [
        (tmdb_id, show_completion.get(tmdb_id, 1.0)) for tmdb_id in all_show_ids
    ]
    taste_tag_ids = list(
        dict.fromkeys(
            build_taste_profile(
                db, watched_movies, "movie", top_n_tags=TASTE_TOP_N_TAGS
            )
            + build_taste_profile(db, watched_shows, "tv", top_n_tags=TASTE_TOP_N_TAGS)
        )
    )

    try:
        taste_catalogs = _find_genre_taste_catalogs(
            db,
            watched_movie_ids=all_movie_ids,
            watched_show_ids=all_show_ids,
            taste_tag_ids=taste_tag_ids,
            max_catalogs=4,
            limit=100,
            user_id=user.id,
        )
        for tc in taste_catalogs:
            catalog_gen.save_user_catalog(
                user_id=user.id,
                slot_id=tc["slot_id"],
                name=tc["name"],
                media_type=tc["media_type"],
                tmdb_ids=tc["tmdb_ids"],
                generation_method="taste_genre",
            )
            catalogs_created += 1
            logger.info(f"  Taste '{tc['name']}': {len(tc['tmdb_ids'])} items")
    except Exception as e:
        logger.warning(f"  Taste catalogs failed: {e}")

    # ------------------------------------------------------------------
    # Step 4c: "Hidden Gems For You" — high quality, low popularity
    # ------------------------------------------------------------------
    for media_label, media_type, slot in [
        ("movies", "movie", "gems-movie"),
        ("shows", "tv", "gems-series"),
    ]:
        try:
            tmdb_ids = _find_hidden_gems_by_taste(
                db,
                taste_tag_ids=taste_tag_ids,
                media_type=media_type,
                exclude_ids=all_movie_ids | all_show_ids,
                limit=100,
                user_id=user.id,
            )
            if len(tmdb_ids) >= MIN_ROW_ITEMS:
                catalog_gen.save_user_catalog(
                    user_id=user.id,
                    slot_id=slot,
                    name=row_name(slot),
                    media_type=media_type,
                    tmdb_ids=tmdb_ids,
                    generation_method="hidden_gems",
                )
                catalogs_created += 1
                logger.info(f"  Hidden Gems ({media_label}): {len(tmdb_ids)} items")
        except Exception as e:
            logger.warning(f"  Hidden Gems ({media_label}) failed: {e}")

    return await _generate_common_catalogs(
        user, db, catalog_gen, catalogs_created, display, common_data=common_data
    )


# ---------------------------------------------------------------------------
# Bulk sync for all users (called by scheduler)
# ---------------------------------------------------------------------------
async def sync_all_users(db: Session) -> Dict:
    """Sync personalized catalogs for all active local users."""
    users = db.query(User).filter(User.is_active.is_(True)).all()

    logger.info(f"Starting personalized catalog sync for {len(users)} active users...")

    sync_status["running"] = True
    sync_status["started_at"] = utc_now().isoformat()
    sync_status["completed_at"] = None
    sync_status["users_done"] = 0
    sync_status["users_total"] = len(users)
    sync_status["current_user"] = None
    sync_status["home_release_filtered"] = 0
    sync_status["mockbusters_filtered"] = 0
    sync_status["last_error"] = None

    logger.info("Pre-fetching timely TMDB catalogs (Trending and New Releases)...")
    common_data = await _prefetch_common_catalog_data(db)
    _store_common_data(common_data)
    sync_status["prefetch_counts"] = {k: len(v) for k, v in common_data.items()}
    logger.info(
        "Pre-fetch complete: "
        + ", ".join(f"{k}={len(v)}" for k, v in common_data.items())
    )

    stats = {"total": len(users), "synced": 0, "failed": 0, "catalogs": 0}

    for user in users:
        display = (
            getattr(user, "display_name", None)
            or getattr(user, "email", None)
            or user.user_key[:8]
        )
        sync_status["current_user"] = display
        try:
            count = await sync_user_catalogs(user, db, common_data=common_data)
            stats["synced"] += 1
            stats["catalogs"] += count
        except Exception as e:
            logger.error(f"Sync failed for user {display}: {e}")
            sync_status["last_error"] = f"{display}: {str(e)[:100]}"
            stats["failed"] += 1
            db.rollback()

        sync_status["users_done"] = stats["synced"] + stats["failed"]
        await asyncio.sleep(1)

    sync_status["running"] = False
    sync_status["completed_at"] = utc_now().isoformat()
    sync_status["current_user"] = None

    logger.info(
        f"Personalized catalog sync complete: {stats['synced']}/{stats['total']} users synced, "
        f"{stats['catalogs']} total catalogs, {stats['failed']} failures"
    )
    from workers.build_control import clear_serving_cache

    clear_serving_cache("scheduled personal catalog rebuild")
    return stats
