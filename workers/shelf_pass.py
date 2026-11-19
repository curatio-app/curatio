"""Tag whatever is on a shelf and is not yet tagged.

The nightly job tags what TMDB's ``/discover`` returned for the last seven
days. That is not the same set as what a viewer actually sees. The feed rows --
Trending Now, New Releases, Popular, Coming Soon -- are filled by a *different*
fetch inside the personalized sync, with different parameters (region=US,
release types 4|5, English only, a 90-day window), so the two id sets barely
overlap by construction. A title that is trending today but came out five years
ago entered ``media_metadata`` through the feed prefetch and was never eligible
for any scheduled tagging pass at all.

The user-visible consequence is that clicking a title in Trending Now dropped
you out of the tag world: no chips on the detail page, and "More Like This"
silently fell back to TMDB's ``/similar``. The rest of the library had 9.19
tags per title; those rows had none.

This pass closes it by asking a better question than "what is new?". It asks
**what is on a shelf right now and still has no tags**, which also catches the
Because-You-Watched and taste rows, and needs no second TMDB feed fetch. Run it
after the personalized sync, when the shelves for this cycle exist.

Two properties worth stating, because both were wrong on the path this
replaces:

* **Enrich before tagging.** ``daily_update`` hardcodes ``tag_facts={}`` and
  ``mdb_facts={}``, so every title it tags is tagged from title, overview and
  genre names alone -- while the 316k-title library was tagged with TMDB
  keywords, cast, certification and MDBList review text. Those nightly rows
  were permanently lower-quality than everything around them. Here the detail
  fetch runs first and the features are read back off the row.
* **Refresh the weights.** ``tag_and_store`` writes ``weight``/``weight_norm``
  as NULL, and every ranked query filters ``weight_norm IS NOT NULL``. Shelf
  additions are weighted from the current ``TagStats`` only; the next nightly
  full refresh reconciles global IDF without rewriting the corpus twice here.
"""

from __future__ import annotations

import asyncio
from time import monotonic

from loguru import logger
from sqlalchemy import func, tuple_
from sqlalchemy.orm import Session, defer

from app.config import settings
from app.models import MediaMetadata, MovieTag, UserCatalogContent
from workers.tagging_common import build_features, tag_and_store

#: Ceiling on titles tagged per run. A shelf holds ~200 items and there are
#: ~25 slots per profile, so an install whose library is already tagged finds a
#: handful; this only binds after a taxonomy change or a fresh install, where
#: the right answer is "make progress tonight, finish tomorrow" rather than
#: "spend an unbounded amount in a scheduled job nobody is watching".
DEFAULT_LIMIT = 400


def untagged_shelf_ids(
    db: Session, limit: int = DEFAULT_LIMIT
) -> list[tuple[int, str]]:
    """``(tmdb_id, media_type)`` for shelf members carrying no tags.

    Keyed on ``movie_tags`` rather than on ``media_metadata``: a row that exists
    but was never tagged is exactly the population this pass is for, and the
    ``media_metadata``-keyed predicate is what let the gap persist.

    Ordered by how many shelves a title appears on, so the most-seen titles are
    tagged first when the limit binds.
    """
    tagged = db.query(MovieTag.tmdb_id, MovieTag.media_type).distinct().subquery()
    rows = (
        db.query(
            UserCatalogContent.tmdb_id,
            UserCatalogContent.media_type,
            func.count().label("shelves"),
        )
        .outerjoin(
            tagged,
            (UserCatalogContent.tmdb_id == tagged.c.tmdb_id)
            & (UserCatalogContent.media_type == tagged.c.media_type),
        )
        .filter(tagged.c.tmdb_id.is_(None))
        .group_by(UserCatalogContent.tmdb_id, UserCatalogContent.media_type)
        .order_by(func.count().desc(), UserCatalogContent.tmdb_id)
        .limit(limit)
        .all()
    )
    return [(int(r[0]), r[1]) for r in rows]


async def run_shelf_pass(limit: int = DEFAULT_LIMIT) -> dict:
    """Enrich then tag the untagged titles currently sitting on a shelf.

    Never raises: this runs inside the nightly scheduler behind the personalized
    sync, and a TMDB or Gemini hiccup must not take down a job whose earlier
    steps already committed their work.
    """
    from app.database import get_db

    stats: dict = {"targets": 0, "enriched": 0, "tagged": 0, "failed": 0}

    with get_db() as db:
        targets = untagged_shelf_ids(db, limit=limit)
    stats["targets"] = len(targets)
    if not targets:
        logger.info("shelf pass: every title on a shelf is already tagged")
        return stats

    logger.info(f"shelf pass: {len(targets):,} untagged titles on shelves")

    # Enrich per media type -- enrich() filters on one type at a time, and the
    # two id namespaces are disjoint, so a movie id must never be handed to the
    # TV endpoint.
    for media_type in ("movie", "tv"):
        ids = [tid for tid, mtype in targets if mtype == media_type]
        if not ids:
            continue
        phase_started = monotonic()
        try:
            from workers.enrich import enrich

            result = await enrich(media_type=media_type, tmdb_ids=ids)
            stats["enriched"] += int(result.get("fetched", 0)) + int(
                result.get("reused", 0)
            )
        except Exception as exc:  # noqa: BLE001 - tagging blind beats not tagging
            logger.warning(f"shelf pass: enrich failed for {media_type}: {exc}")
        finally:
            logger.info(
                "job_phase job=shelf_pass "
                f"phase=enrich_{media_type} "
                f"duration_ms={(monotonic() - phase_started) * 1000:.1f} "
                f"targets={len(ids)}"
            )

    for media_type in ("movie", "tv"):
        pairs = [(tid, mtype) for tid, mtype in targets if mtype == media_type]
        if not pairs:
            continue
        phase_started = monotonic()
        try:
            with get_db() as db:
                rows = (
                    db.query(MediaMetadata)
                    .options(defer(MediaMetadata.raw_data))
                    .filter(
                        tuple_(MediaMetadata.tmdb_id, MediaMetadata.media_type).in_(
                            pairs
                        )
                    )
                    .all()
                )
                if not rows:
                    continue
                features = [build_features(row) for row in rows]
                result = await tag_and_store(
                    db, features, batch_size=settings.tagging_batch_size
                )
                stats["tagged"] += int(result.get("processed", 0))
                stats["failed"] += int(result.get("failed", 0))

                # NULL weights are worse than no tags: every ranked query
                # filters weight_norm IS NOT NULL, so an unweighted title is
                # excluded rather than merely scored low.
                if result.get("processed"):
                    from app.relevance import refresh_item_vectors

                    refresh_item_vectors(
                        db,
                        [(int(row.tmdb_id), media_type) for row in rows],
                    )
        except Exception as exc:  # noqa: BLE001 - never fail the nightly run
            logger.warning(f"shelf pass: tagging failed for {media_type}: {exc}")
        finally:
            logger.info(
                "job_phase job=shelf_pass "
                f"phase=tag_{media_type} "
                f"duration_ms={(monotonic() - phase_started) * 1000:.1f} "
                f"targets={len(pairs)}"
            )

    logger.info(
        f"shelf pass: tagged {stats['tagged']:,} of {stats['targets']:,} "
        f"({stats['failed']:,} failed)"
    )
    return stats


if __name__ == "__main__":
    from app.database import init_database

    init_database()
    asyncio.run(run_shelf_pass())
