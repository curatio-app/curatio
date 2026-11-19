"""
Database build worker.

Tags all movies and TV shows using the Gemini API.
Run this once, then use the free tier for weekly updates.

Usage:
    python workers/initial_build.py --movies 100000 --shows 50000
"""

import asyncio
import argparse
from loguru import logger
from sqlalchemy.orm import Session

# Add parent directory to path
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.time_utils import utc_now  # noqa: E402
from app.database import get_db, init_database  # noqa: E402
from app.models import MediaMetadata, MovieTag, TaggingJob  # noqa: E402
from app.tmdb_client import tmdb_client, MediaType  # noqa: E402

# Pause/stop control now lives in workers/build_control.py so the re-tag worker
# can share it. Re-exported here because app/admin.py imports pause_event and
# request_build_stop from this module in eight places.
from workers.build_control import (  # noqa: E402,F401
    BuildStopped,
    check_pause,
    pause_event,
    raise_if_stopped as _raise_if_stopped,
    request_build_stop,
    reset_build_stop,
    stop_event,
)
from workers.metadata_store import upsert_many  # noqa: E402


async def fetch_items(media_type: MediaType, limit: int):
    """Fetch movies or TV shows from TMDB using multi-strategy discovery.

    The stop flag is passed *into* the crawl. Previously it was only checked
    before each fetch, so pressing Stop during a multi-hour acquisition did
    nothing until that whole media type finished -- the one long stage of the
    build that could not be interrupted.
    """
    logger.info(f"Fetching up to {limit} {media_type} items from TMDB...")

    items = await tmdb_client.fetch_all_items(
        media_type, limit=limit, should_stop=stop_event.is_set
    )

    logger.info(f"Fetched {len(items)} unique {media_type} items")
    return items


async def store_metadata(db: Session, items: list, media_type: MediaType):
    """Store TMDB metadata in database."""
    logger.info(f"Storing metadata for {len(items)} {media_type} items...")

    # Deduplicate items by tmdb_id (TMDB can return same item on multiple pages)
    seen = set()
    unique_items = []
    for item in items:
        tmdb_id = item.get("id")
        if tmdb_id not in seen:
            seen.add(tmdb_id)
            unique_items.append(item)

    if len(unique_items) < len(items):
        logger.info(f"Deduplicated {len(items)} -> {len(unique_items)} items")

    # Non-destructive upsert, NOT db.merge(). Acquisition works with list
    # payloads, which carry no imdb_id / logo_path / release_dates / season
    # counts -- so extract_metadata returns None for those, and merge() would
    # write those Nones over whatever enrichment had already learned. Running
    # acquisition a second time on an enriched library would have stripped
    # imdb_id from every re-fetched title, breaking MDBList matching and stream
    # resolution. See workers/metadata_store.py.
    stats = upsert_many(
        db,
        (tmdb_client.extract_metadata(item, media_type) for item in unique_items),
    )

    logger.info(
        f"Stored metadata for {len(unique_items)} items "
        f"({stats['inserted']:,} new, {stats['updated']:,} updated)"
    )


async def tag_items_batch(
    db: Session, items: list, media_type: MediaType, batch_size: int = 100
):
    """Tag items in batches through the shared v2 tagging path."""
    logger.info(
        f"Tagging {len(items)} {media_type} items in batches of {batch_size}..."
    )

    # Deduplicate items by TMDB ID to avoid processing duplicates
    seen_ids = set()
    unique_items = []
    for item in items:
        tmdb_id = item.get("id")
        if tmdb_id not in seen_ids:
            seen_ids.add(tmdb_id)
            unique_items.append(item)

    if len(unique_items) < len(items):
        logger.info(
            f"Deduplicated {len(items)} -> {len(unique_items)} items for tagging"
        )

    # Skip items that are already tagged or have a title-specific terminal
    # disposition at the current tagger version. Sparse/model-blocked titles
    # must not become a paid retry loop on every initial-build restart.
    from workers.tagging_common import tagger_version

    unique_ids = [item.get("id") for item in unique_items if item.get("id") is not None]
    already_tagged = {
        row[0]
        for row in db.query(MovieTag.tmdb_id)
        .filter(
            MovieTag.media_type == media_type,
            MovieTag.tmdb_id.in_(unique_ids),
        )
        .distinct()
        .all()
    }
    version = tagger_version()
    terminal_ids = {
        row[0]
        for row in db.query(MediaMetadata.tmdb_id)
        .filter(
            MediaMetadata.media_type == media_type,
            MediaMetadata.tmdb_id.in_(unique_ids),
            MediaMetadata.tag_attempt_version == version,
        )
        .all()
    }
    items_to_tag = [
        item
        for item in unique_items
        if item.get("id") not in already_tagged and item.get("id") not in terminal_ids
    ]

    if len(items_to_tag) < len(unique_items):
        logger.info(
            f"Skipping {len(already_tagged)} already-tagged and "
            f"{len(terminal_ids)} terminal items at {version}; "
            f"{len(items_to_tag)} remaining"
        )

    # The shared v2 tagging path, same as the re-tag and the daily update.
    #
    # This used to call gemini_engine.tag_items(), whose prompt hardcodes the
    # ORIGINAL 75 tag names as literal prose against a taxonomy that now has
    # hundreds of versioned tags -- so a fresh install tagged its library against the
    # vocabulary this overhaul replaced, dropped anything off that list without
    # recording it, and stamped no tagger_version or weights. tag_and_store
    # fixes all of that and is dialect-branched, so this path is finally
    # reachable from the SQLite test suite.
    from workers.tagging_common import tag_and_store

    if not items_to_tag:
        return 0, 0, ""

    features = [
        {
            "tmdb_id": item["id"],
            "media_type": media_type,
            "title": item.get("title") or item.get("name", "Unknown"),
            "overview": item.get("overview", ""),
            "genres": [
                g["name"] if isinstance(g, dict) else str(g)
                for g in item.get("genres", [])
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
        for item in items_to_tag
    ]

    stats = await tag_and_store(
        db,
        features,
        batch_size=batch_size,
        should_stop=stop_event.is_set,
        check_pause=check_pause,
    )

    total_processed = int(stats.get("processed", 0))
    total_failed = int(stats.get("failed", 0))
    first_error = str(stats.get("first_error") or "")

    logger.info(
        f"Tagging complete: {total_processed} succeeded, {total_failed} failed, "
        f"{stats.get('tags_written', 0)} tags, "
        f"{stats.get('off_vocabulary', 0)} off-vocabulary"
    )
    if total_failed and not total_processed:
        # Every batch failing is almost always one systemic cause (a retired
        # model ID, a revoked key, an exhausted quota) rather than N unlucky
        # batches. Say so loudly instead of returning a bare count.
        logger.error(
            f"Every {media_type} batch failed. First error: {first_error or 'unknown'}"
        )

    if total_processed:
        try:
            from app.relevance import refresh_tag_stats

            refresh_tag_stats(db, media_type=media_type)
        except Exception as exc:  # noqa: BLE001 - weights must not fail the build
            logger.warning(f"Could not refresh relevance weights: {exc}")

    return total_processed, total_failed, first_error


async def create_universal_categories(db: Session):
    """Retain inactive compatibility definitions for legacy category ids."""
    from app.categories import seed_categories

    logger.info("Syncing legacy category compatibility definitions...")
    seed_categories(db)


async def generate_all_catalogs(db: Session):
    """Synthesize and materialize the universal catalogs from current tags."""
    from app.category_synth import rebuild_synthesized_shelves

    logger.info("Synthesizing universal catalogs from the current corpus...")
    rebuild_synthesized_shelves(db)

    logger.info("All catalogs generated successfully")


async def main(movies_limit: int, shows_limit: int, fetch_only: bool = False):
    """Main build process.

    ``fetch_only`` stops after metadata is stored, skipping tagging and catalog
    regeneration. Acquisition and tagging were welded together -- ``main()``
    tagged unconditionally -- so there was no way to widen what the library
    collects without also paying Gemini for it and rebuilding every row. Making
    the acquisition question answerable on its own is what lets the pre-1970
    gap be measured before it is acted on.
    """
    start_time = utc_now()

    logger.info("=" * 80)
    logger.info("DATABASE BUILD")
    logger.info("=" * 80)
    logger.info(f"Target: {movies_limit} movies + {shows_limit} TV shows")
    logger.info("Uses Gemini API calls")
    logger.info("Estimated time: ~3 hours")
    logger.info("=" * 80)

    # Initialize database
    init_database()

    with get_db() as db:
        # Create tagging job record (save params upfront for auto-resume)
        job = TaggingJob(
            job_type="database_build",
            started_at=start_time,
            status="running",
            job_metadata={
                "movies_target": movies_limit,
                "shows_target": shows_limit,
            },
        )
        db.add(job)
        db.commit()

        try:
            # The tag vocabulary is reconciled from app/taxonomy.py by
            # sync_taxonomy(), which init_database() above already ran. Builds no
            # longer seed tags of their own -- that was the third divergent copy
            # of the vocabulary.

            # Step 1: Create universal categories
            await create_universal_categories(db)

            # Step 2: Fetch movies
            _raise_if_stopped()
            movies = await fetch_items("movie", movies_limit)
            await store_metadata(db, movies, "movie")

            # Step 3: Tag movies
            movies_processed = movies_failed = 0
            movies_error = None
            if not fetch_only:
                movies_processed, movies_failed, movies_error = await tag_items_batch(
                    db, movies, "movie"
                )

            # Step 4: Fetch TV shows
            _raise_if_stopped()
            shows = await fetch_items("tv", shows_limit)
            await store_metadata(db, shows, "tv")

            # Step 5: Tag TV shows
            shows_processed = shows_failed = 0
            shows_error = None
            if not fetch_only:
                shows_processed, shows_failed, shows_error = await tag_items_batch(
                    db, shows, "tv"
                )

            # Step 6: Generate catalogs
            if not fetch_only:
                _raise_if_stopped()
                await generate_all_catalogs(db)
            else:
                logger.info(
                    "fetch-only: skipping tagging and catalog regeneration. "
                    f"Stored {len(movies):,} movies and {len(shows):,} shows."
                )

            # Update job record
            job.completed_at = utc_now()
            job.items_processed = movies_processed + shows_processed
            job.items_failed = movies_failed + shows_failed
            build_error = movies_error or shows_error
            if job.items_failed and not job.items_processed:
                job.status = "failed"
                job.error_message = (
                    f"All {job.items_failed} items failed to tag. "
                    f"First error: {build_error or 'unknown'}"
                )[:2000]
            else:
                job.status = "completed"
                if build_error:
                    job.error_message = (
                        f"{job.items_failed} item(s) failed. "
                        f"First error: {build_error}"
                    )[:2000]
            db.commit()

            # Summary
            duration = (utc_now() - start_time).total_seconds() / 3600

            logger.info("=" * 80)
            logger.info("BUILD COMPLETE!")
            logger.info("=" * 80)
            logger.info(f"Movies fetched: {len(movies)}, tagged: {movies_processed}")
            logger.info(f"Shows fetched: {len(shows)}, tagged: {shows_processed}")
            logger.info(f"Total items: {movies_processed + shows_processed}")
            logger.info(f"Failed: {movies_failed + shows_failed}")
            logger.info(f"Duration: {duration:.1f} hours")
            logger.info("=" * 80)
            logger.info("Next steps:")
            logger.info("1. Set GEMINI_PAID_TIER=false in .env")
            logger.info("2. Start the server: docker-compose up -d")
            logger.info("3. Pair a device from /account/pair")
            logger.info("=" * 80)

        except BuildStopped:
            logger.warning("Build stopped by user — unwinding cleanly")
            db.rollback()
            job.status = "cancelled"
            job.error_message = "Cancelled by user"
            job.completed_at = utc_now()
            db.commit()
            # Return (do NOT raise) so the build thread exits without falling
            # through to the 'completed' finalization above.
            return
        except Exception as e:
            logger.error(f"Build failed: {e}")
            db.rollback()
            job.status = "failed"
            job.error_message = str(e)[:2000]
            db.commit()
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initial database build")
    parser.add_argument(
        "--movies", type=int, default=100000, help="Number of movies to tag"
    )
    parser.add_argument(
        "--shows", type=int, default=50000, help="Number of TV shows to tag"
    )
    parser.add_argument(
        "--fetch-only",
        action="store_true",
        help="Acquire metadata and stop: no tagging, no catalog regeneration, "
        "no Gemini spend. Use this to measure or widen what the library "
        "collects without paying to tag it in the same run.",
    )

    args = parser.parse_args()

    asyncio.run(main(args.movies, args.shows, fetch_only=args.fetch_only))
