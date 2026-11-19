"""
Recompute IDF weights, document norms and tag statistics.

Run after a build, a daily update or a re-tag, and whenever the taxonomy
changes materially:

    docker compose --profile workers run --rm worker \\
        python workers/refresh_relevance.py

    # one corpus only, e.g. after a TV-only update
    python workers/refresh_relevance.py --media-type tv

    # size the job before running it
    python workers/refresh_relevance.py --dry-run

This is a worker entrypoint rather than an admin endpoint on purpose. The
population rewrites every row of ``movie_tags`` -- ~2.2M today, ~5.3M after the
re-tag -- and that work has no business running inside the web process, which
serves catalog requests from a small connection pool. An admin trigger can be
added later for convenience; the heavy path should stay here.

It is likewise never called from ``init_database()``: startup holds a
pg_advisory_lock, so a multi-minute UPDATE there would block every other
instance from starting.
"""

import argparse
import sys
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import get_db  # noqa: E402
from app.models import MovieTag  # noqa: E402
from app.relevance import refresh_tag_stats  # noqa: E402


def _dry_run(db) -> None:
    """Report the size of the job without writing anything."""
    from sqlalchemy import func

    total = db.query(func.count(MovieTag.tag_id)).scalar() or 0
    logger.info(f"movie_tags rows: {total:,}")
    for media_type in ("movie", "tv"):
        titles = (
            db.query(func.count(func.distinct(MovieTag.tmdb_id)))
            .filter(MovieTag.media_type == media_type)
            .scalar()
            or 0
        )
        tags = (
            db.query(func.count(func.distinct(MovieTag.tag_id)))
            .filter(MovieTag.media_type == media_type)
            .scalar()
            or 0
        )
        logger.info(f"  {media_type}: {titles:,} titles, {tags:,} distinct tags")
    unweighted = (
        db.query(func.count(MovieTag.tag_id)).filter(MovieTag.weight.is_(None)).scalar()
        or 0
    )
    logger.info(f"rows with no weight yet: {unweighted:,}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--media-type",
        choices=("movie", "tv"),
        default=None,
        help="Restrict to one corpus (default: both)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the size of the job and exit without writing",
    )
    parser.add_argument(
        "--no-indexes",
        action="store_true",
        help="Skip building the weight_norm indexes afterwards",
    )
    args = parser.parse_args()

    with get_db() as db:
        if args.dry_run:
            _dry_run(db)
            return 0

        logger.info("Refreshing tag relevance statistics...")
        counts = refresh_tag_stats(
            db,
            media_type=args.media_type,
            create_indexes=not args.no_indexes,
        )
        logger.info(
            f"Done: {counts['tags']:,} tag stats, {counts['rows']:,} rows weighted, "
            f"{counts['vectors']:,} document vectors"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
