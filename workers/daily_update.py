"""
Daily content update worker.

Fetches new releases from TMDB, tags them with Gemini, and regenerates catalogs.
Designed to run daily via the in-app scheduler or manually.

Usage (manual):
    python workers/daily_update.py
"""

import asyncio
from time import monotonic
from typing import Tuple

from loguru import logger
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session, defer

# Add parent directory to path
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.time_utils import utc_now  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import get_db, init_database  # noqa: E402
from app.models import MediaMetadata, MovieTag, TaggingJob  # noqa: E402
from app.models import UniversalCatalogContent, UserCatalogContent  # noqa: E402
from app.tmdb_client import tmdb_client  # noqa: E402

# Compatibility export for integrations/tests that patch the former generator
# entry point. Production regeneration now uses the staged compiler below.
from app.catalog_generator import CatalogGenerator  # noqa: E402,F401
from workers.build_control import clear_serving_cache  # noqa: E402
from workers.metadata_store import upsert_many  # noqa: E402


def filter_new_items(db: Session, items: list, media_type: str) -> list:
    """
    Filter out items already tagged or terminally attempted at this version.

    Keyed on ``movie_tags``, deliberately NOT on ``media_metadata``.

    This used to test ``media_metadata``, which made the untagged backlog
    self-perpetuating: ``personalized_sync._backfill_metadata`` and
    ``_save_metadata_from_results`` both insert MediaMetadata rows for
    trending/popular/imported titles without ever tagging them, and this filter
    then treated those rows as "already exists" and skipped them forever. Only a
    full ``initial_build`` -- which correctly keys off ``movie_tags`` -- could
    ever reach them. In production that backlog reached ~150k rows, 42% of the
    library.

    Uses batched SQL IN queries instead of loading all existing IDs into memory.

    Args:
        db: Database session
        items: List of TMDB items
        media_type: 'movie' or 'tv'

    Returns:
        List of items that carry no tags and no current terminal disposition
    """
    if not items:
        return []

    # Check existence in batches via SQL to avoid loading entire table
    all_ids = [item["id"] for item in items]
    from workers.tagging_common import tagger_version

    tagged_ids: set = set()
    terminal_ids: set = set()
    version = tagger_version()
    batch_size = 1000
    for i in range(0, len(all_ids), batch_size):
        batch = all_ids[i : i + batch_size]
        rows = (
            db.query(MovieTag.tmdb_id)
            .filter(
                MovieTag.media_type == media_type,
                MovieTag.tmdb_id.in_(batch),
            )
            .distinct()
            .all()
        )
        tagged_ids.update(row.tmdb_id for row in rows)

        rows = (
            db.query(MediaMetadata.tmdb_id)
            .filter(
                MediaMetadata.media_type == media_type,
                MediaMetadata.tmdb_id.in_(batch),
                MediaMetadata.tag_attempt_version == version,
            )
            .all()
        )
        terminal_ids.update(row.tmdb_id for row in rows)

    new_items = [
        item
        for item in items
        if item["id"] not in tagged_ids and item["id"] not in terminal_ids
    ]
    logger.info(
        f"Filtered {media_type}: {len(items)} fetched, "
        f"{len(tagged_ids)} tagged, "
        f"{len(terminal_ids)} terminal at {version}, "
        f"{len(new_items)} to tag"
    )
    return new_items


async def store_metadata(db: Session, items: list, media_type: str) -> None:
    """Store TMDB metadata in database.

    Non-destructive: these are list payloads, so a blind ``merge`` would null
    ``imdb_id`` and friends on any title the enricher had already filled in.
    See ``workers/metadata_store.py``.
    """
    logger.info(f"Storing metadata for {len(items)} new {media_type} items...")

    upsert_many(
        db,
        (tmdb_client.extract_metadata(item, media_type) for item in items),  # type: ignore[arg-type]
    )
    logger.info(f"Stored metadata for {len(items)} items")


def _features_from_payload(item: dict, media_type: str) -> dict:
    """Feature dict straight from a TMDB list payload.

    The thin fallback: a list payload carries no keywords, cast, certification
    or MDBList reviews, so a title tagged from it gets noticeably worse tags
    than the rest of the library. Used only when the enriched row is missing.
    """
    return {
        "tmdb_id": item["id"],
        "media_type": media_type,
        "title": item.get("title") or item.get("name", "Unknown"),
        "overview": item.get("overview", ""),
        "genres": [
            g["name"] if isinstance(g, dict) else str(g) for g in item.get("genres", [])
        ],
        "release_date": item.get("release_date") or item.get("first_air_date", ""),
        "original_language": item.get("original_language"),
        "tag_facts": {},
        "mdb_facts": {},
        "countries": None,
        "runtime": item.get("runtime"),
        "number_of_seasons": item.get("number_of_seasons"),
        "number_of_episodes": item.get("number_of_episodes"),
    }


async def tag_items_batch(
    db: Session, items: list, media_type: str, batch_size: int = 50
) -> Tuple[int, int, str]:
    """
    Tag items in batches through the shared v2 tagging path.

    Returns:
        ``(processed_count, failed_count, first_error)``.

    ``first_error`` retains the first hard or optional-provider anomaly for
    diagnostics. A model outage no longer fails a title: taxonomy v5 writes the
    deterministic factual tags and reports the loss of optional nuance.
    """
    from workers.tagging_common import build_features, tag_and_store

    if not items:
        return 0, 0, ""

    # The v2 tagging path, same as the re-tag. This used to call
    # gemini_engine.tag_items(), whose prompt hardcodes the ORIGINAL 75 tag
    # names as literal prose (app/gemini_client.py) against a taxonomy that now
    # has hundreds of versioned tags. Four consequences, all live every night:
    #
    #   * titles were tagged from the vocabulary this overhaul replaced;
    #   * anything off that list was dropped by a bare `if tag_name in tag_map`
    #     instead of going through app/tag_validation.py's alias/normalise/fuzzy
    #     resolution, and never reached tag_observations;
    #   * rows carried no tagger_version, so the next upgrade re-selected and
    #     RE-PAID for every one of them;
    #   * rows carried no weight/weight_norm, which after the ranking cutover
    #     means invisible to every ranked row.
    #
    # tag_and_store fixes all four, and its dialect-branched upsert is why this
    # path can finally be exercised by the SQLite test suite.
    # Enrich first, then read the features back off the row.
    #
    # This used to build the feature dicts straight from the /discover list
    # payload with `tag_facts: {}` and `mdb_facts: {}` hardcoded -- so every
    # title the nightly job tagged was tagged from its title, overview and
    # genre names alone, while the 316k-title library behind it had been tagged
    # with TMDB keywords, cast, certification and MDBList review text. Those
    # are precisely the inputs mood, theme and style come from, so the newest
    # titles carried the worst tags in the library, permanently, until an
    # operator ran a re-tag by hand.
    #
    # store_metadata has already committed these ids, so a detail fetch is all
    # that stands between the list payload and the real features.
    ids = [item["id"] for item in items if item.get("id") is not None]
    try:
        from workers.enrich import enrich

        await enrich(media_type=media_type, tmdb_ids=ids)
    except Exception as exc:  # noqa: BLE001 - thin tags beat no tags
        logger.warning(f"Could not enrich {media_type} items before tagging: {exc}")

    stored = {
        row.tmdb_id: row
        for row in db.query(MediaMetadata)
        .options(defer(MediaMetadata.raw_data))
        .filter(
            MediaMetadata.media_type == media_type,
            MediaMetadata.tmdb_id.in_(ids),
        )
        .all()
    }

    # Prefer the stored row; fall back to the list payload for anything not
    # persisted. The caller does run store_metadata first, but making the
    # enriched path a hard requirement would mean a caller that forgot it tags
    # nothing at all and says so only at WARNING -- the same silent-zero shape
    # this module has been bitten by twice.
    features = [
        (
            build_features(stored[item["id"]])
            if item.get("id") in stored
            else _features_from_payload(item, media_type)
        )
        for item in items
        if item.get("id") is not None
    ]
    thin = sum(1 for item in items if item.get("id") not in stored)
    if thin:
        logger.warning(
            f"{thin}/{len(features)} {media_type} items have no stored row — "
            "tagging those from the list payload alone (no keywords or cast)"
        )

    logger.info(f"Tagging {len(features)} {media_type} items at the current taxonomy")
    stats = await tag_and_store(db, features, batch_size=batch_size)

    total_processed = int(stats.get("processed", 0))
    total_failed = int(stats.get("failed", 0))
    first_error = str(stats.get("first_error") or "")

    logger.info(
        f"Tagging complete: {total_processed} succeeded, {total_failed} failed, "
        f"{stats.get('tags_written', 0)} tags, "
        f"{stats.get('off_vocabulary', 0)} off-vocabulary"
    )
    if total_failed and not total_processed:
        logger.error(
            f"Every {media_type} batch failed. First error: {first_error or 'unknown'}"
        )

    return total_processed, total_failed, first_error


# Bound how long any single query inside an offloaded worker session may run, so a
# lock-stuck query aborts instead of hanging a thread (and its pooled connection)
# indefinitely. Applied per worker session — not globally — so it can't abort the
# startup CREATE INDEX migrations or the pg_advisory_lock startup wait. Well under
# the daily-update deadline. Postgres only.
_WORKER_STATEMENT_TIMEOUT_MS = 600_000


def _apply_statement_timeout(db: Session) -> None:
    try:
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            db.execute(text(f"SET statement_timeout = {_WORKER_STATEMENT_TIMEOUT_MS}"))
    except Exception as e:  # never let this block the run
        logger.warning(f"Could not set statement_timeout: {e}")


# The web process now puts the whole daily update on a private worker loop. Keep
# these heavy, await-free DB stages off that loop too so the job deadline remains
# enforceable at await points. Each stage owns a short-lived session because
# SQLAlchemy sessions are not thread-safe.
def _filter_new_items_threaded(items: list, media_type: str) -> list:
    with get_db() as db:
        _apply_statement_timeout(db)
        return filter_new_items(db, items, media_type)


def _store_metadata_threaded(items: list, media_type: str) -> None:
    """Synchronous mirror of store_metadata() on a fresh, short-lived session.

    Deduplicates by tmdb_id first. ``SessionLocal`` uses ``autoflush=False``, so
    a second ``merge()`` for a primary key already pending in this session cannot
    see the first one and both get flushed as INSERTs -- a UniqueViolation on
    ``media_metadata_pkey`` that aborts the entire daily run. The source is now
    deduplicated in ``get_new_releases_this_week`` too; this is the belt to that
    braces, because every caller of this function is exposed to the same trap.
    """
    with get_db() as db:
        _apply_statement_timeout(db)
        seen: set = set()
        unique = []
        for item in items:
            tmdb_id = item.get("id")
            if tmdb_id is None or tmdb_id in seen:
                continue
            seen.add(tmdb_id)
            unique.append(item)
        # Non-destructive, for the same reason as store_metadata above.
        upsert_many(
            db,
            (tmdb_client.extract_metadata(item, media_type) for item in unique),  # type: ignore[arg-type]
        )


def _regen_universal_catalogs_threaded() -> None:
    from app.category_synth import rebuild_synthesized_shelves

    with get_db() as db:
        _apply_statement_timeout(db)
        rebuild_synthesized_shelves(db)
    clear_serving_cache("nightly catalog regeneration")


def refresh_relevance_threaded(
    job_name: str = "daily_update",
) -> dict[str, int]:
    """Run the single nightly full-corpus refresh on a fresh worker session."""
    from app.relevance import refresh_tag_stats

    started = monotonic()
    with get_db() as db:
        _apply_statement_timeout(db)
        counts = refresh_tag_stats(db)
    logger.info(
        f"job_phase job={job_name} phase=full_relevance "
        f"duration_ms={(monotonic() - started) * 1000:.1f} "
        f"rows={counts.get('rows', 0)} vectors={counts.get('vectors', 0)}"
    )
    return counts


async def _refresh_tv_next_air_dates(db: Session, limit: int) -> int:
    """Re-check next-episode air dates for series that appear on a shelf.

    A series' ``next_episode_to_air`` goes stale as episodes air, so unlike a movie's
    digital date it needs periodic refreshing. Only catalog members are checked, least
    recently checked first, capped at ``limit`` per run — that round-robins the pool on
    roughly a weekly cycle, which is as often as a next-air date can actually change.
    No fan-out on any request path.

    Returns the number of series whose date was successfully refreshed.
    """
    if limit <= 0:
        return 0

    universal_tv = select(UniversalCatalogContent.tmdb_id).where(
        UniversalCatalogContent.media_type == "tv"
    )
    user_tv = select(UserCatalogContent.tmdb_id).where(
        UserCatalogContent.media_type == "tv"
    )
    rows = (
        db.query(MediaMetadata)
        .options(defer(MediaMetadata.raw_data))
        .filter(
            MediaMetadata.media_type == "tv",
            or_(
                MediaMetadata.tmdb_id.in_(universal_tv),
                MediaMetadata.tmdb_id.in_(user_tv),
            ),
        )
        .order_by(MediaMetadata.next_episode_checked_at.asc().nullsfirst())
        .limit(limit)
        .all()
    )
    if not rows:
        return 0

    sem = asyncio.Semaphore(10)

    async def fetch(tmdb_id: int) -> "tuple[int, str | None, bool]":
        async with sem:
            try:
                detail = await tmdb_client.get_tv_show_summary(tmdb_id)
                return tmdb_id, tmdb_client.next_episode_air_date(detail), True
            except Exception as e:
                logger.debug(f"Next-air-date refresh skipped for tv/{tmdb_id}: {e}")
                return tmdb_id, None, False

    results = await asyncio.gather(*[fetch(row.tmdb_id) for row in rows])
    # Successful fetches only; a None value here means "checked, no next episode".
    fetched = {tid: date for tid, date, ok in results if ok}

    now = utc_now()
    for row in rows:
        # Stamped even when the fetch failed, so a persistently failing id can't
        # wedge the front of the round-robin queue.
        row.next_episode_checked_at = now  # type: ignore[assignment]
        tmdb_id = int(row.tmdb_id)
        if tmdb_id in fetched:
            # Assigned directly rather than through extract_metadata's non-null
            # guard, so a series that has finished airing gets its date cleared.
            row.next_episode_air_date = fetched[tmdb_id]  # type: ignore[assignment]
    db.commit()

    logger.info(
        f"Refreshed next-episode air dates for {len(fetched)}/{len(rows)} catalog series"
    )
    return len(fetched)


async def _run_daily_update(job_ref: dict) -> None:
    """
    Run the daily content update.

    Fetches new releases from TMDB, tags them, and regenerates catalogs.
    Can be called from the scheduler or run manually.

    ``job_ref`` is an out-param: the created TaggingJob's id is written to
    ``job_ref["job_id"]`` so the timeout wrapper can finalize the row if the
    run is killed for exceeding its deadline.
    """
    start_time = utc_now()

    logger.info("=" * 60)
    logger.info("DAILY CONTENT UPDATE")
    logger.info(f"Started at {start_time.isoformat()}")
    logger.info("=" * 60)

    with get_db() as db:
        # Create job record
        job = TaggingJob(
            job_type="daily_update",
            started_at=start_time,
            status="running",
        )
        db.add(job)
        db.commit()
        job_ref["job_id"] = job.id

        try:
            total_processed = 0
            total_failed = 0
            first_error = ""

            # Fetch and process new movies
            phase_started = monotonic()
            logger.info("Fetching new movie releases...")
            new_movies_raw = await tmdb_client.get_new_releases_this_week("movie")
            new_movies = await asyncio.to_thread(
                _filter_new_items_threaded, new_movies_raw, "movie"
            )

            if new_movies:
                await asyncio.to_thread(_store_metadata_threaded, new_movies, "movie")
                # Library-first: titles nobody owns are only AI-tagged when
                # the owner opts in. Owned ones are tagged below.
                if settings.tag_catalog_beyond_library:
                    processed, failed, err = await tag_items_batch(
                        db, new_movies, "movie"
                    )
                    total_processed += processed
                    total_failed += failed
                    first_error = first_error or err
            logger.info(
                "job_phase job=daily_update phase=movies "
                f"duration_ms={(monotonic() - phase_started) * 1000:.1f} "
                f"fetched={len(new_movies_raw)} selected={len(new_movies)}"
            )

            # Fetch and process new TV shows
            phase_started = monotonic()
            logger.info("Fetching new TV releases...")
            new_shows_raw = await tmdb_client.get_new_releases_this_week("tv")
            new_shows = await asyncio.to_thread(
                _filter_new_items_threaded, new_shows_raw, "tv"
            )

            if new_shows:
                await asyncio.to_thread(_store_metadata_threaded, new_shows, "tv")
                # Library-first: titles nobody owns are only AI-tagged when
                # the owner opts in. Owned ones are tagged below.
                if settings.tag_catalog_beyond_library:
                    processed, failed, err = await tag_items_batch(db, new_shows, "tv")
                    total_processed += processed
                    total_failed += failed
                    first_error = first_error or err
            logger.info(
                "job_phase job=daily_update phase=tv "
                f"duration_ms={(monotonic() - phase_started) * 1000:.1f} "
                f"fetched={len(new_shows_raw)} selected={len(new_shows)}"
            )

            # Owned and Watchlist titles that are still untagged (a backstop
            # for the per-sync runs). Its own relevance refresh runs inside.
            try:
                from workers.library_tagging import tag_owned_titles

                owned = await tag_owned_titles()
                logger.info(f"Library tagging during daily update: {owned}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never fail the update
                logger.warning(f"Library tagging failed: {exc}")

            # Both media types are tagged before the one full-corpus relevance
            # publication. The old per-media refresh rewrote the movie corpus,
            # then the TV corpus, and the shelf pass repeated both later in the
            # same cycle. The worker helper owns its SQLAlchemy session and the
            # statement timeout; no ORM instance crosses the thread boundary.
            relevance_refreshed = False
            if total_processed:
                db.commit()
                try:
                    await asyncio.to_thread(refresh_relevance_threaded)
                    relevance_refreshed = True
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - preserve prior behavior
                    logger.warning(f"Could not refresh relevance weights: {exc}")

            # Keep the catalog "coming soon" badges honest. Bounded and independent
            # of whether anything new was tagged, since dates age on their own.
            phase_started = monotonic()
            try:
                series_refreshed = await _refresh_tv_next_air_dates(
                    db, settings.coming_soon_tv_refresh_limit
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # A cosmetic badge must never fail the whole daily update; the next
                # run retries the same rows, since their cursor was not stamped.
                logger.warning(f"Next-episode air date refresh failed: {e}")
                db.rollback()
                series_refreshed = 0
            logger.info(
                "job_phase job=daily_update phase=next_air_dates "
                f"duration_ms={(monotonic() - phase_started) * 1000:.1f} "
                f"refreshed={series_refreshed}"
            )

            catalogs_regenerated = total_processed > 0
            if catalogs_regenerated:
                # Regenerate only when fresh tags were written. Rebuilding every
                # catalog scans the full tag table, so no-op daily runs should
                # finish quickly instead of spending time on unchanged SQL work.
                # Offloaded to a thread with its own session so the ~40-category
                # rebuild doesn't freeze the event loop (and the deadline stays
                # enforceable at await boundaries).
                logger.info("Regenerating universal catalogs...")
                phase_started = monotonic()
                await asyncio.to_thread(_regen_universal_catalogs_threaded)
                logger.info(
                    "job_phase job=daily_update phase=catalog_publication "
                    f"duration_ms={(monotonic() - phase_started) * 1000:.1f}"
                )
            else:
                logger.info("No new items were tagged; skipping catalog regeneration")

            # Update job record.
            #
            # A run that failed every item is not "completed". Reporting it as
            # such is how a two-month tagging outage stayed invisible: the model
            # ID had been retired, every batch 404'd, and the dashboard showed a
            # green "completed" with a failure count nobody had reason to read.
            # Record the reason and mark the status honestly.
            job.completed_at = utc_now()  # type: ignore[assignment]
            if total_failed and not total_processed:
                job.status = "failed"  # type: ignore[assignment]
                job.error_message = (  # type: ignore[assignment]
                    f"All {total_failed} items failed to tag. "
                    f"First error: {first_error or 'unknown'}"
                )[:2000]
            else:
                job.status = "completed"  # type: ignore[assignment]
                if first_error:
                    # Partial failure still deserves a recorded reason.
                    job.error_message = (  # type: ignore[assignment]
                        f"{total_failed} item(s) failed. First error: {first_error}"
                    )[:2000]
            job.items_processed = total_processed  # type: ignore[assignment]
            job.items_failed = total_failed  # type: ignore[assignment]
            job.job_metadata = {  # type: ignore[assignment]
                "new_movies_found": len(new_movies_raw),
                "new_movies_added": len(new_movies),
                "new_shows_found": len(new_shows_raw),
                "new_shows_added": len(new_shows),
                "catalogs_regenerated": catalogs_regenerated,
                "relevance_refreshed": relevance_refreshed,
                "series_next_air_refreshed": series_refreshed,
            }
            db.commit()

            duration = (utc_now() - start_time).total_seconds()

            logger.info("=" * 60)
            logger.info("DAILY UPDATE COMPLETE")
            logger.info(f"New movies: {len(new_movies)} tagged")
            logger.info(f"New TV shows: {len(new_shows)} tagged")
            logger.info(f"Total processed: {total_processed}")
            logger.info(f"Failed: {total_failed}")
            logger.info(f"Duration: {duration:.1f}s")
            logger.info("=" * 60)

        except asyncio.CancelledError:
            logger.warning("Daily update was cancelled")
            job.status = "cancelled"  # type: ignore[assignment]
            job.error_message = "Cancelled by user"  # type: ignore[assignment]
            job.completed_at = utc_now()  # type: ignore[assignment]
            db.commit()
            raise
        except Exception as e:
            logger.error(f"Daily update failed: {e}")
            job.status = "failed"  # type: ignore[assignment]
            job.error_message = str(e)  # type: ignore[assignment]
            job.completed_at = utc_now()  # type: ignore[assignment]
            db.commit()
            raise


def _mark_job_timed_out(job_id, timeout_seconds: float) -> None:
    """Mark a timed-out job's record with a distinct 'timed_out' status.

    Opens a fresh session because the run's own session is already closed by
    the time the timeout propagates. Best-effort: never raises.
    """
    if job_id is None:
        return
    try:
        with get_db() as db:
            job = db.query(TaggingJob).filter(TaggingJob.id == job_id).first()
            if job is not None:
                job.status = "timed_out"
                job.error_message = f"Exceeded {timeout_seconds:.0f}s deadline"
                job.completed_at = utc_now()
                db.commit()
    except Exception as e:
        logger.error(f"Failed to mark job {job_id} as timed out: {e}")


async def run_daily_update(timeout_seconds: float | None = None) -> None:
    """
    Run the daily content update with an overall deadline.

    Wraps the actual update in ``asyncio.wait_for`` so a hung or pathologically
    slow run (e.g. a stalled AI request) is killed instead of running forever.
    On timeout the job record is marked ``timed_out`` and TimeoutError is
    re-raised for the caller (scheduler / admin trigger) to handle.
    """
    if timeout_seconds is None:
        timeout_seconds = settings.daily_update_timeout

    job_ref: dict = {}
    try:
        await asyncio.wait_for(_run_daily_update(job_ref), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        logger.error(f"Daily update exceeded {timeout_seconds}s deadline — killing run")
        _mark_job_timed_out(job_ref.get("job_id"), timeout_seconds)
        raise


if __name__ == "__main__":
    init_database()
    asyncio.run(run_daily_update())
