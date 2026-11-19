"""
IDF-weighted tag relevance: statistics, similarity, taste vectors, browse.

What is live, because the header used to say "nothing in this module is wired
into a serving path yet" long after most of it was:

* :func:`refresh_tag_stats` -- the relevance step of every pipeline.
* :func:`similar_by_vector` -- "Because You Watched" and "More Like This".
* :func:`build_taste_vector` / :func:`completion_weights` -- every personal row
  and the universal row ordering.
* :func:`browse_by_tags` -- the Browse screen and vibe search.

``store_taste_vector`` and ``taste_scored_candidates`` now drive Recommended
and Top Picks. Other display-only paths may still compute a small vector on the
fly, but the primary candidate ranking preserves the user's actual tag weights
instead of flattening the top tag IDs to equal votes.

Three stored layers on ``movie_tags``:

* ``confidence``  -- unchanged raw model output, kept for audit
* ``weight``      -- ``confidence * idf(tag, media_type)``
* ``weight_norm`` -- ``weight / ||doc||``

Because ``weight_norm`` is pre-divided, cosine similarity between two titles is
``SUM(a.weight_norm * b.weight_norm)`` over shared tags -- a multiply and a sum,
nothing else. That is the portability keystone: **every sqrt and log happens in
Python**, and the emitted SQL contains only ``+ - * / SUM CASE``. SQLite ships
``sqrt``/``ln`` only when compiled with SQLITE_ENABLE_MATH_FUNCTIONS, so a query
that used them would pass on one machine and fail on another.
``tests/test_relevance.py`` asserts the compiled SQL is free of them.

IDF is scoped by media type. The production corpora are not comparable --
~166.8k movies against ~37.2k TV titles, a 4.5:1 ratio -- so a single global IDF
would systematically mis-weight every TV tag.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

from loguru import logger
from sqlalchemy import and_, func, text
from sqlalchemy.orm import Session, aliased

from app.time_utils import utc_now  # noqa: E402
from app.models import (
    HiddenTitle,
    MediaTagVector,
    MovieTag,
    Tag,
    TagStats,
    UserTasteVector,
    WatchEvent,
    WatchProgress,
)

__all__ = [
    "refresh_tag_stats",
    "refresh_item_vectors",
    "similar_by_vector",
    "build_taste_vector",
    "store_taste_vector",
    "browse_by_tags",
    "completion_weights",
    "discriminating_tag_count",
    "reference_tag_ids",
    "IDF_MIN",
    "IDF_MAX",
    "SIMILARITY_MAX_CANDIDATE_TAGS",
    "SIMILARITY_UBIQUITOUS_DF_RATIO",
    "MIN_WEIGHT_NORM",
]

#: IDF is clamped so one freak-rare tag cannot hijack a whole row.
#:
#: Only the upper bound actually binds. With ``ln((N+1)/(df+1)) + 1`` and
#: ``df <= N``, the smoothed value is always >= 1.0, so IDF_MIN is a defensive
#: floor against a future formula change or a df/corpus_size race -- not
#: something reachable from valid inputs.
IDF_MIN = 0.5
IDF_MAX = 6.0

#: A title with fewer tags than this keeps all of them as similarity
#: candidates, however common they are.
SPARSE_TITLE_TAG_COUNT = 6

#: A reference title contributes at most this many tags to a similarity query.
SIMILARITY_MAX_CANDIDATE_TAGS = 12

#: Reference tags held by more than this share of the corpus are skipped --
#: "Drama" is not what makes two titles alike.
SIMILARITY_UBIQUITOUS_DF_RATIO = 0.25

#: Ignore near-zero contributions; also lets the index range-scan.
MIN_WEIGHT_NORM = 0.05

#: Rows per chunk for the population UPDATEs. Never one transaction: the table
#: holds ~2.2M rows today and ~5.3M after the re-tag.
_CHUNK = 50_000


# ---------------------------------------------------------------------------
# Statistics refresh
# ---------------------------------------------------------------------------
def _is_postgres(db: Session) -> bool:
    bind = db.get_bind()
    return bool(bind is not None and bind.dialect.name == "postgresql")


def _upsert_vectors(db: Session, rows: list[dict[str, Any]]) -> None:
    """Upsert media_tag_vectors, branching on dialect.

    SQLAlchemy's postgresql and sqlite dialects both expose on_conflict_do_update
    but they are separate constructs, so shared code has to choose. Doing this
    here rather than importing the postgresql insert unconditionally is what
    keeps the whole module exercisable by the SQLite test suite.
    """
    if not rows:
        return
    if _is_postgres(db):
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(MediaTagVector).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["tmdb_id", "media_type"],
            set_={
                "norm": stmt.excluded.norm,
                "tag_count": stmt.excluded.tag_count,
                "top_tag_id": stmt.excluded.top_tag_id,
                "updated_at": stmt.excluded.updated_at,
            },
        )
    else:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = sqlite_insert(MediaTagVector).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["tmdb_id", "media_type"],
            set_={
                "norm": stmt.excluded.norm,
                "tag_count": stmt.excluded.tag_count,
                "top_tag_id": stmt.excluded.top_tag_id,
                "updated_at": stmt.excluded.updated_at,
            },
        )
    db.execute(stmt)


def _id_chunks(db: Session, media_type: str | None) -> Iterable[tuple[int, int]]:
    """Yield [lo, hi) tmdb_id ranges covering the table."""
    query = db.query(func.min(MovieTag.tmdb_id), func.max(MovieTag.tmdb_id))
    if media_type:
        query = query.filter(MovieTag.media_type == media_type)
    low, high = query.one()
    if low is None or high is None:
        return
    lo = int(low)
    end = int(high) + 1
    while lo < end:
        yield lo, min(lo + _CHUNK, end)
        lo += _CHUNK


def compute_idf(doc_count: int, corpus_size: int) -> float:
    """Smoothed inverse document frequency, clamped.

    ``ln`` is evaluated here, in Python, and only the resulting float reaches
    SQL. That is deliberate -- see the module docstring.
    """
    raw = math.log((corpus_size + 1) / (doc_count + 1)) + 1.0
    return max(IDF_MIN, min(IDF_MAX, raw))


def refresh_tag_stats(
    db: Session,
    media_type: str | None = None,
    *,
    create_indexes: bool = True,
) -> dict[str, int]:
    """Recompute tag_stats, movie_tags.weight/weight_norm and media_tag_vectors.

    Must never be called from ``init_database()``: it rewrites every row of
    ``movie_tags`` and startup holds a pg_advisory_lock. Drive it from
    ``workers/refresh_relevance.py``.

    Args:
        media_type: Restrict to one corpus, or None for both.
        create_indexes: Build the weight_norm indexes when finished. They are
            created *after* the population UPDATEs on purpose -- both include
            weight_norm, so if they existed during the rewrite no update could
            be HOT and every row change would churn both index trees.

    Returns:
        Counts of tags, rows and vectors touched.
    """
    started = utc_now()
    media_types = [media_type] if media_type else ["movie", "tv"]
    touched = {"tags": 0, "rows": 0, "vectors": 0}

    # --- phase 1: document frequency -> idf (ln in Python) ---
    for mtype in media_types:
        corpus_size = (
            db.query(func.count(func.distinct(MovieTag.tmdb_id)))
            .filter(MovieTag.media_type == mtype)
            .scalar()
            or 0
        )
        if not corpus_size:
            logger.info(f"relevance: no {mtype} rows, skipping")
            continue

        df_rows = (
            db.query(
                MovieTag.tag_id,
                func.count(func.distinct(MovieTag.tmdb_id)),
                func.avg(MovieTag.confidence),
            )
            .filter(MovieTag.media_type == mtype)
            .group_by(MovieTag.tag_id)
            .all()
        )

        db.query(TagStats).filter(TagStats.media_type == mtype).delete(
            synchronize_session=False
        )
        db.bulk_insert_mappings(
            TagStats,
            [
                {
                    "tag_id": tag_id,
                    "media_type": mtype,
                    "doc_count": int(doc_count or 0),
                    "corpus_size": int(corpus_size),
                    "idf": compute_idf(int(doc_count or 0), int(corpus_size)),
                    "avg_confidence": float(avg_confidence or 0.0),
                    "computed_at": started,
                }
                for tag_id, doc_count, avg_confidence in df_rows
            ],
        )
        touched["tags"] += len(df_rows)
        db.commit()

        # Mirror onto tags for the admin dashboard; keep the authoritative
        # per-media-type values in tag_stats.
        db.bulk_update_mappings(
            Tag,
            [
                {
                    "id": tag_id,
                    "idf": compute_idf(int(doc_count or 0), int(corpus_size)),
                    "assignment_count": int(doc_count or 0),
                }
                for tag_id, doc_count, _avg in df_rows
            ],
        )
        db.commit()

        logger.info(
            f"relevance: {mtype} idf over {corpus_size} titles, {len(df_rows)} tags"
        )

    # --- phase 2: weight = confidence * idf, chunked ---
    weight_sql = text(
        "UPDATE movie_tags SET weight = confidence * COALESCE(("
        "  SELECT ts.idf FROM tag_stats ts"
        "  WHERE ts.tag_id = movie_tags.tag_id"
        "    AND ts.media_type = movie_tags.media_type"
        "), 1.0) WHERE tmdb_id >= :lo AND tmdb_id < :hi"
        + ("" if media_type is None else " AND media_type = :mt")
    )
    for lo, hi in _id_chunks(db, media_type):
        params: dict[str, Any] = {"lo": lo, "hi": hi}
        if media_type is not None:
            params["mt"] = media_type
        result = db.execute(weight_sql, params)
        touched["rows"] += result.rowcount or 0
        db.commit()

    # --- phase 3: norms (sqrt in Python) then weight_norm, chunked ---
    norm_sql = text(
        "UPDATE movie_tags SET weight_norm = weight / COALESCE(("
        "  SELECT v.norm FROM media_tag_vectors v"
        "  WHERE v.tmdb_id = movie_tags.tmdb_id"
        "    AND v.media_type = movie_tags.media_type"
        "), 1.0) WHERE tmdb_id >= :lo AND tmdb_id < :hi"
        + ("" if media_type is None else " AND media_type = :mt")
    )
    for lo, hi in _id_chunks(db, media_type):
        query = db.query(
            MovieTag.tmdb_id,
            MovieTag.media_type,
            func.sum(MovieTag.weight * MovieTag.weight),
            func.count(MovieTag.tag_id),
        ).filter(MovieTag.tmdb_id >= lo, MovieTag.tmdb_id < hi)
        if media_type is not None:
            query = query.filter(MovieTag.media_type == media_type)
        rows = query.group_by(MovieTag.tmdb_id, MovieTag.media_type).all()

        payload = []
        for tmdb_id, mtype, sum_squares, tag_count in rows:
            norm = math.sqrt(float(sum_squares or 0.0))
            payload.append(
                {
                    "tmdb_id": int(tmdb_id),
                    "media_type": mtype,
                    # A zero norm would divide by zero; 1.0 leaves the (all-zero)
                    # weights untouched instead.
                    "norm": norm if norm > 0 else 1.0,
                    "tag_count": int(tag_count or 0),
                    "top_tag_id": None,
                    "updated_at": started,
                }
            )
        _upsert_vectors(db, payload)
        touched["vectors"] += len(payload)
        db.commit()

        params = {"lo": lo, "hi": hi}
        if media_type is not None:
            params["mt"] = media_type
        db.execute(norm_sql, params)
        db.commit()

    if create_indexes:
        _create_weight_indexes(db)

    logger.info(
        "relevance: refreshed {tags} tag stats, {rows} rows, "
        "{vectors} vectors".format(**touched)
    )
    return touched


def refresh_item_vectors(
    db: Session, items: Iterable[tuple[int, str]]
) -> dict[str, int]:
    """Weight only the supplied titles using the current :class:`TagStats`.

    The shelf pass adds at most a few hundred titles. Recomputing corpus-wide
    document frequencies after each such pass rewrites millions of rows and was
    responsible for a second multi-minute event-loop stall every night. New
    shelf titles can safely use the most recently published IDF values; the next
    nightly full refresh reconciles the small amount of corpus drift.

    No corpus statistics or unrelated ``movie_tags`` rows are changed here.
    Tags that did not exist at the last full refresh use the same neutral 1.0
    fallback as :func:`refresh_tag_stats`.
    """
    targets: set[tuple[int, str]] = set()
    for tmdb_id, media_type in items:
        if media_type not in {"movie", "tv"}:
            raise ValueError(f"Unsupported media type: {media_type!r}")
        targets.add((int(tmdb_id), media_type))

    if not targets:
        return {"rows": 0, "vectors": 0}

    started = utc_now()
    touched = {"rows": 0, "vectors": 0}

    for media_type in ("movie", "tv"):
        ids = sorted(tmdb_id for tmdb_id, mtype in targets if mtype == media_type)
        if not ids:
            continue

        tag_rows = (
            db.query(MovieTag)
            .filter(
                MovieTag.media_type == media_type,
                MovieTag.tmdb_id.in_(ids),
            )
            .all()
        )
        if not tag_rows:
            continue

        tag_ids = {int(row.tag_id) for row in tag_rows}
        idf_by_tag = {
            int(tag_id): float(idf if idf is not None else 1.0)
            for tag_id, idf in db.query(TagStats.tag_id, TagStats.idf)
            .filter(
                TagStats.media_type == media_type,
                TagStats.tag_id.in_(tag_ids),
            )
            .all()
        }

        rows_by_item: dict[int, list[MovieTag]] = {}
        for row in tag_rows:
            row.weight = float(row.confidence) * idf_by_tag.get(int(row.tag_id), 1.0)
            rows_by_item.setdefault(int(row.tmdb_id), []).append(row)

        payload: list[dict[str, Any]] = []
        norms: dict[int, float] = {}
        for tmdb_id, rows in rows_by_item.items():
            norm = math.sqrt(sum(float(row.weight or 0.0) ** 2 for row in rows))
            safe_norm = norm if norm > 0 else 1.0
            norms[tmdb_id] = safe_norm
            payload.append(
                {
                    "tmdb_id": tmdb_id,
                    "media_type": media_type,
                    "norm": safe_norm,
                    "tag_count": len(rows),
                    "top_tag_id": None,
                    "updated_at": started,
                }
            )

        # Flush the per-tag weights before publishing their vector metadata,
        # then normalize only the selected rows in memory. This is intentionally
        # proportional to the shelf-pass target count, not corpus size.
        db.flush()
        _upsert_vectors(db, payload)
        for row in tag_rows:
            row.weight_norm = float(row.weight or 0.0) / norms[int(row.tmdb_id)]

        touched["rows"] += len(tag_rows)
        touched["vectors"] += len(payload)

    db.commit()
    logger.info(
        "relevance: targeted refresh weighted {rows} rows across "
        "{vectors} vectors".format(**touched)
    )
    return touched


def _create_weight_indexes(db: Session) -> None:
    """Build the weight_norm indexes -- only ever after population.

    Creating these before the population UPDATE would prevent HOT updates for
    every rewritten row and roughly double the size of both indexes. Building
    them on settled data is also faster.
    """
    ddl = [
        "CREATE INDEX IF NOT EXISTS idx_movie_tags_tag_media_wn "
        "ON movie_tags (tag_id, media_type, weight_norm)",
        "CREATE INDEX IF NOT EXISTS idx_movie_tags_item_wn "
        "ON movie_tags (tmdb_id, media_type, weight_norm)",
    ]
    for statement in ddl:
        try:
            db.execute(text(statement))
            db.commit()
        except Exception as exc:  # noqa: BLE001 - index creation must never abort a run
            db.rollback()
            logger.warning(f"Could not create relevance index: {exc}")


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------
def discriminating_tag_count(db: Session, tmdb_id: int, media_type: str) -> int:
    """How many of a title's tags say something specific about it.

    A tag held by more than :data:`SIMILARITY_UBIQUITOUS_DF_RATIO` of the corpus
    does not distinguish anything, and a title carrying only such tags has no
    similarity story to tell. :func:`reference_tag_ids` already computes this
    and then deliberately throws it away -- it falls back to the ubiquitous tags
    rather than returning nothing, because imprecise neighbours beat none on a
    detail page.

    On a *shelf* the tradeoff runs the other way, which is why the count is
    exposed separately: "Because You Watched X" is a claim that these titles are
    like X, and a seed whose only tags are Drama and Suspenseful cannot support
    it. Note that cosine alone will not catch this -- ``weight_norm`` is
    L2-normalised per title, so two titles with identical tag profiles score 1.0
    however common those tags are.
    """
    rows = (
        db.query(TagStats.doc_count, TagStats.corpus_size)
        .join(
            MovieTag,
            and_(
                MovieTag.tag_id == TagStats.tag_id,
                MovieTag.media_type == TagStats.media_type,
            ),
        )
        .filter(MovieTag.tmdb_id == tmdb_id, MovieTag.media_type == media_type)
        .all()
    )
    return sum(
        1
        for doc_count, corpus_size in rows
        if not (
            corpus_size
            and doc_count
            and doc_count > SIMILARITY_UBIQUITOUS_DF_RATIO * corpus_size
        )
    )


def reference_tag_ids(
    db: Session,
    tmdb_id: int,
    media_type: str,
    limit: int = SIMILARITY_MAX_CANDIDATE_TAGS,
) -> list[int]:
    """The reference title's most discriminating tags.

    Ubiquitous tags are dropped: a tag held by more than
    ``SIMILARITY_UBIQUITOUS_DF_RATIO`` of the corpus says nothing about
    similarity and makes the self-join fan out across the library.

    The escape hatch is keyed on how many tags the *title* has, not on how many
    survive filtering: a sparsely-tagged title keeps everything rather than
    being left with nothing to match on. A well-tagged title whose tags are all
    ubiquitous still falls back, because returning no candidates would be worse
    than returning imprecise ones.
    """
    rows = (
        db.query(
            MovieTag.tag_id, MovieTag.weight, TagStats.doc_count, TagStats.corpus_size
        )
        .outerjoin(
            TagStats,
            and_(
                TagStats.tag_id == MovieTag.tag_id,
                TagStats.media_type == MovieTag.media_type,
            ),
        )
        .filter(MovieTag.tmdb_id == tmdb_id, MovieTag.media_type == media_type)
        .all()
    )
    if not rows:
        return []

    ranked = sorted(rows, key=lambda r: -(r[1] or 0.0))
    if len(ranked) < SPARSE_TITLE_TAG_COUNT:
        return [int(r[0]) for r in ranked[:limit]]

    discriminating = [
        r
        for r in ranked
        if not (r[3] and r[2] and r[2] > SIMILARITY_UBIQUITOUS_DF_RATIO * r[3])
    ]
    chosen = discriminating or ranked
    return [int(r[0]) for r in chosen[:limit]]


def similar_by_vector(
    db: Session,
    tmdb_id: int,
    media_type: str,
    *,
    limit: int = 40,
    exclude_ids: Sequence[int] | None = None,
    min_shared_tags: int = 2,
    quality_floor: bool = True,
    only_ids: Sequence[int] | None = None,
) -> list[tuple[int, float]]:
    """Titles most similar to a reference, by cosine over IDF-weighted tags.

    ``only_ids`` limits the candidates to those titles (e.g. what a profile
    owns on its media servers).

    Returns ``[(tmdb_id, similarity)]`` ordered by similarity descending.

    Args:
        quality_floor: Apply the catalog quality floor to the *results*. On by
            default, and the reason this parameter exists at all: this function
            drives both "Because You Watched" and the detail page's "More Like
            This", and it was the one candidate generator in the product that
            never joined ``media_metadata`` -- no rating, no vote count, no
            popularity, nothing. Every other row family had a floor bolted on
            as it was noticed; these two were missed because they reach the
            engine through :mod:`app.relevance` rather than through the catalog
            generator.

            It matters more here than anywhere else, because cosine over
            L2-normalised vectors structurally favours *thinly-tagged* titles:
            a title with four tags concentrates its whole norm in them, while a
            richly-tagged one spreads it thin. Thin tagging tracks obscurity
            (no MDBList reviews means a smaller prompt means fewer tags), so
            without a floor the maths actively selects for the obscure tail.

            Pass ``False`` only for diagnostics that want the raw neighbour
            list.

    The emitted SQL is a self-join with ``SUM(a.weight_norm * b.weight_norm)``
    and contains no sqrt, log or date function, so the same statement runs on
    PostgreSQL and SQLite.
    """
    tag_ids = reference_tag_ids(db, tmdb_id, media_type)
    if not tag_ids:
        return []

    a = aliased(MovieTag)
    b = aliased(MovieTag)
    similarity = func.sum(a.weight_norm * b.weight_norm).label("sim")

    query = (
        db.query(b.tmdb_id, similarity)
        .select_from(a)
        .join(
            b,
            and_(
                b.tag_id == a.tag_id,
                b.media_type == media_type,
            ),
        )
        .filter(
            a.tmdb_id == tmdb_id,
            a.media_type == media_type,
            a.tag_id.in_(tag_ids),
            b.tmdb_id != tmdb_id,
            b.weight_norm >= MIN_WEIGHT_NORM,
        )
    )
    if quality_floor:
        from app.catalog_generator import quality_floor_ids

        query = query.filter(b.tmdb_id.in_(quality_floor_ids(db, media_type)))
    if only_ids is not None:
        query = query.filter(b.tmdb_id.in_(list(only_ids)))
    query = (
        query.group_by(b.tmdb_id)
        .having(func.count(b.tag_id) >= min_shared_tags)
        .order_by(similarity.desc(), b.tmdb_id)
        .limit(limit + len(exclude_ids or ()))
    )

    excluded = set(exclude_ids or ())
    results = [
        (int(row[0]), float(row[1] or 0.0))
        for row in query.all()
        if int(row[0]) not in excluded
    ]
    return results[:limit]


# ---------------------------------------------------------------------------
# Taste vectors
# ---------------------------------------------------------------------------
def build_taste_vector(
    db: Session,
    watched: Sequence[tuple[int, float]] | Sequence[int],
    media_type: str,
    *,
    top_n: int = 40,
) -> dict[int, float]:
    """Build a profile's sparse taste vector from watched titles.

    Args:
        watched: Either TMDB ids, or ``(tmdb_id, factor)`` pairs. The factor is
            a per-title multiplier; progress-derived weighting that can make it
            negative belongs to a later phase, so plain ids default to 1.0.

    Returns:
        ``{tag_id: weight}``, L2-normalized, truncated to ``top_n``.

    Because the inputs are IDF-weighted, a ubiquitous tag contributes far less
    than a rare one. That is the whole point: the previous top-15-by-raw-count
    profile converged on the same handful of tags for every user.
    """
    if not watched:
        return {}

    factors: dict[int, float] = {}
    for entry in watched:
        if isinstance(entry, (tuple, list)):
            factors[int(entry[0])] = float(entry[1])
        else:
            factors[int(entry)] = 1.0

    rows = (
        db.query(MovieTag.tmdb_id, MovieTag.tag_id, MovieTag.weight_norm)
        .filter(
            MovieTag.media_type == media_type,
            MovieTag.tmdb_id.in_(list(factors)),
        )
        .all()
    )
    if not rows:
        return {}

    accumulated: dict[int, float] = {}
    for tmdb_id, tag_id, weight_norm in rows:
        factor = factors.get(int(tmdb_id), 0.0)
        if not factor:
            continue
        accumulated[int(tag_id)] = accumulated.get(int(tag_id), 0.0) + (
            float(weight_norm or 0.0) * factor
        )

    # A tag can only be cancelled to zero or below by negative factors; drop it
    # rather than letting it invert the ranking.
    positive = {k: v for k, v in accumulated.items() if v > 0}
    if not positive:
        return {}

    norm = math.sqrt(sum(v * v for v in positive.values()))
    if norm <= 0:
        return {}

    ranked = sorted(positive.items(), key=lambda kv: -kv[1])[:top_n]
    return {tag_id: value / norm for tag_id, value in ranked}


#: Weight per completion-event kind. `imported` is a bulk library backfill --
#: real evidence, but far weaker than someone deliberately watching a thing to
#: the end.
# `server_watched` is a play on the profile's own Plex/Jellyfin account: a
# deliberate watch, so nearly as strong as one finished in Curatio.
_EVENT_WEIGHTS = {"complete": 1.0, "server_watched": 0.9, "imported": 0.6}

#: Taste factor for a title marked "Not for me".
HIDDEN_WEIGHT = -0.5

#: Progress rows shorter than this are noise: a five-second sample says nothing
#: about whether someone liked a title.
MIN_PROGRESS_SECONDS = 300

#: Watched fraction -> weight. The negative band is the entire point of this
#: function: today a title abandoned after ten minutes is indistinguishable
#: from one never started, so a profile can only ever accumulate evidence
#: *for* things.
_PROGRESS_BANDS: tuple[tuple[float, float], ...] = (
    (0.85, 1.0),  # finished, even without a scrobble
    (0.45, 0.5),  # watched most of it
    (0.10, -0.4),  # bailed -- a real signal, and a negative one
    (0.0, 0.0),  # mis-click
)

#: Bailing on episode one is abandonment; finishing season one is not. A series
#: needs at least this many started episodes before its mean ratio is read as
#: evidence either way.
MIN_SERIES_EPISODES = 2


def _band_weight(ratio: float) -> float:
    for floor, weight in _PROGRESS_BANDS:
        if ratio >= floor:
            return weight
    return 0.0


def completion_weights(
    db: Session,
    user_id: int,
    media_type: str | None = None,
    *,
    tmdb_id: int | None = None,
) -> dict[int, float]:
    """Per-title taste multipliers, including negative ones.

    Returns ``{tmdb_id: factor}`` suitable for :func:`build_taste_vector`.

    Only ``action IN ('complete', 'imported')`` has ever fed taste, so
    abandoning a title after ten minutes has been indistinguishable from never
    having started it. Rows can therefore only ever be evidence *for*
    something, and a profile that keeps recommending a subgenre the user
    repeatedly bails on has no mechanism for learning otherwise.

    Watch progress supplies the missing half. A title watched 10-45% through is
    scored **-0.4**: not merely absent from the profile but actively pushing
    its tags down. Negatives cancel positives inside the sum in
    ``build_taste_vector`` and the final vector is clamped at zero, so
    abandoning three Slow-Burns stops Slow-Burn rows appearing without ever
    inverting the ranking.

    A negative never overrides an explicit completion: someone who finished a
    title once and sampled it again later liked it. TV is aggregated per series
    over episodes with at least ``MIN_SERIES_EPISODES`` started, so bailing on
    episode one counts as abandonment while a finished season one does not.
    """
    weights: dict[int, float] = {}

    event_query = db.query(WatchEvent.tmdb_id, WatchEvent.action).filter(
        WatchEvent.user_id == user_id,
        WatchEvent.action.in_(tuple(_EVENT_WEIGHTS)),
        WatchEvent.tmdb_id.isnot(None),
    )
    if media_type:
        event_query = event_query.filter(WatchEvent.media_type == media_type)
    if tmdb_id is not None:
        event_query = event_query.filter(WatchEvent.tmdb_id == tmdb_id)
    events = event_query.all()
    completed: set[int] = set()
    for event_tmdb_id, action in events:
        tid = int(event_tmdb_id)
        weight = _EVENT_WEIGHTS.get(action, 0.0)
        weights[tid] = max(weights.get(tid, 0.0), weight)
        if action in ("complete", "server_watched"):
            completed.add(tid)

    progress_query = db.query(
        WatchProgress.tmdb_id,
        WatchProgress.media_type,
        WatchProgress.position,
        WatchProgress.duration,
    ).filter(
        WatchProgress.user_id == user_id,
        WatchProgress.tmdb_id.isnot(None),
        WatchProgress.duration >= MIN_PROGRESS_SECONDS,
    )
    if media_type:
        progress_query = progress_query.filter(WatchProgress.media_type == media_type)
    if tmdb_id is not None:
        progress_query = progress_query.filter(WatchProgress.tmdb_id == tmdb_id)
    progress_rows = progress_query.all()

    # Movies score off their single row; series average across episodes, so one
    # abandoned episode inside a watched season does not condemn the show.
    movie_ratios: dict[int, float] = {}
    series_ratios: dict[int, list[float]] = {}
    for progress_tmdb_id, media_type, position, duration in progress_rows:
        if not duration:
            continue
        tid = int(progress_tmdb_id)
        ratio = min(1.0, max(0.0, float(position or 0) / float(duration)))
        if media_type == "tv":
            series_ratios.setdefault(tid, []).append(ratio)
        else:
            movie_ratios[tid] = max(movie_ratios.get(tid, 0.0), ratio)

    derived: dict[int, float] = dict(movie_ratios)
    for tid, ratios in series_ratios.items():
        if len(ratios) < MIN_SERIES_EPISODES:
            # One episode is not enough to call it either way -- but a single
            # finished episode is still a completion signal, so keep the
            # positive band and drop the negative one.
            single = _band_weight(ratios[0])
            if single > 0:
                derived[tid] = ratios[0]
            continue
        derived[tid] = sum(ratios) / len(ratios)

    for tid, ratio in derived.items():
        weight = _band_weight(ratio)
        if weight < 0 and tid in completed:
            # They finished it once. A later partial re-watch is not a dislike.
            continue
        if weight == 0 and tid not in weights:
            continue
        existing = weights.get(tid)
        weights[tid] = weight if existing is None else max(existing, weight)

    # "Not for me" is the most explicit signal there is: it pushes the title's
    # tags down even when the profile once finished it.
    hidden_query = db.query(HiddenTitle.tmdb_id).filter(HiddenTitle.user_id == user_id)
    if media_type:
        hidden_query = hidden_query.filter(HiddenTitle.media_type == media_type)
    if tmdb_id is not None:
        hidden_query = hidden_query.filter(HiddenTitle.tmdb_id == tmdb_id)
    for (hidden_tmdb_id,) in hidden_query:
        weights[int(hidden_tmdb_id)] = HIDDEN_WEIGHT

    return {tid: w for tid, w in weights.items() if w != 0.0}


def store_taste_vector(
    db: Session, user_id: int, media_type: str, vector: dict[int, float]
) -> int:
    """Materialize a taste vector so scoring queries can join it."""
    db.query(UserTasteVector).filter(
        UserTasteVector.user_id == user_id,
        UserTasteVector.media_type == media_type,
    ).delete(synchronize_session=False)
    now = utc_now()
    for tag_id, weight in vector.items():
        db.add(
            UserTasteVector(
                user_id=user_id,
                media_type=media_type,
                tag_id=tag_id,
                weight=weight,
                updated_at=now,
            )
        )
    db.commit()
    return len(vector)


# ---------------------------------------------------------------------------
# Browse (built now, consumed by the faceted-browse phase)
# ---------------------------------------------------------------------------
def browse_by_tags(
    db: Session,
    tag_ids: Sequence[int],
    media_type: str,
    *,
    mode: str = "all",
    limit: int = 100,
    offset: int = 0,
    min_relevance: float = 0.0,
    quality_floor: bool = True,
) -> list[tuple[int, float]]:
    """Rank titles against an ad-hoc tag set.

    Args:
        mode: ``"all"`` requires every tag, ``"any"`` requires at least one.
        quality_floor: Apply the catalog quality floor. On by default so the
            Browse screen and the vibe-search path agree with the rows about
            what is worth showing; this was the third ungated candidate
            generator, alongside the two :func:`similar_by_vector` serves.

    Returns ``[(tmdb_id, relevance)]``. Relevance is ``SUM(weight_norm)`` over
    the matched tags, so it rises with both the number of matches and their
    rarity -- unlike the ``avg(confidence)`` ordering it replaces, which
    penalised titles for matching more of the query.
    """
    if not tag_ids:
        return []

    relevance = func.sum(MovieTag.weight_norm).label("relevance")
    query = (
        db.query(MovieTag.tmdb_id, relevance)
        .filter(
            MovieTag.media_type == media_type,
            MovieTag.tag_id.in_(list(tag_ids)),
        )
        .group_by(MovieTag.tmdb_id)
    )
    if quality_floor:
        from app.catalog_generator import quality_floor_ids

        query = query.filter(MovieTag.tmdb_id.in_(quality_floor_ids(db, media_type)))

    if mode == "all":
        query = query.having(
            func.count(func.distinct(MovieTag.tag_id)) >= len(set(tag_ids))
        )
    if min_relevance > 0:
        query = query.having(func.sum(MovieTag.weight_norm) >= min_relevance)

    rows = (
        query.order_by(relevance.desc(), MovieTag.tmdb_id)
        .limit(limit)
        .offset(offset)
        .all()
    )
    return [(int(r[0]), float(r[1] or 0.0)) for r in rows]


def taste_scored_candidates(
    db: Session,
    user_id: int,
    media_type: str,
    *,
    limit: int = 100,
    exclude_ids: Sequence[int] | None = None,
    quality_floor: bool = True,
    only_ids: Sequence[int] | None = None,
) -> list[tuple[int, float]]:
    """Rank unwatched titles against a stored taste vector.

    ``only_ids`` limits the candidates to those titles.

    Same multiply-and-sum shape as :func:`similar_by_vector`, with
    ``user_taste_vectors`` standing in for the reference title.

    This is the production scorer for Recommended and Top Picks. It remains
    quality-gated here (rather than relying on every caller) so no alternate
    serving path can accidentally reopen the junk tail.
    """
    score = func.sum(UserTasteVector.weight * MovieTag.weight_norm).label("score")
    query = (
        db.query(MovieTag.tmdb_id, score)
        .select_from(UserTasteVector)
        .join(
            MovieTag,
            and_(
                MovieTag.tag_id == UserTasteVector.tag_id,
                MovieTag.media_type == UserTasteVector.media_type,
            ),
        )
        .filter(
            UserTasteVector.user_id == user_id,
            UserTasteVector.media_type == media_type,
            MovieTag.weight_norm >= MIN_WEIGHT_NORM,
        )
    )
    if quality_floor:
        from app.catalog_generator import quality_floor_ids

        query = query.filter(MovieTag.tmdb_id.in_(quality_floor_ids(db, media_type)))
    if only_ids is not None:
        query = query.filter(MovieTag.tmdb_id.in_(list(only_ids)))
    query = (
        query.group_by(MovieTag.tmdb_id)
        .order_by(score.desc(), MovieTag.tmdb_id)
        .limit(limit + len(exclude_ids or ()))
    )
    excluded = set(exclude_ids or ())
    rows = [
        (int(r[0]), float(r[1] or 0.0))
        for r in query.all()
        if int(r[0]) not in excluded
    ]
    return rows[:limit]


def compiled_sql(query: Any) -> str:
    """Render a query's SQL as text. Used by the portability test."""
    statement = query.statement if hasattr(query, "statement") else query
    return str(statement.compile(compile_kwargs={"literal_binds": False}))
