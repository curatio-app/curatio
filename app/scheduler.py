"""
Asyncio-based daily scheduler for content updates.

Runs as a background task inside the FastAPI app.
Controlled by DAILY_UPDATE_ENABLED and DAILY_UPDATE_TIME env vars.
"""

import asyncio
from datetime import timedelta

from loguru import logger

from app.time_utils import utc_now  # noqa: E402
from app.config import settings


def parse_time(time_str: str) -> tuple[int, int]:
    """
    Parse HH:MM time string into (hour, minute) tuple.

    Args:
        time_str: Time in HH:MM format (e.g., "03:00")

    Returns:
        Tuple of (hour, minute)

    Raises:
        ValueError: If format is invalid
    """
    parts = time_str.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time format '{time_str}', expected HH:MM")

    hour = int(parts[0])
    minute = int(parts[1])

    if not (0 <= hour <= 23):
        raise ValueError(f"Hour must be 0-23, got {hour}")
    if not (0 <= minute <= 59):
        raise ValueError(f"Minute must be 0-59, got {minute}")

    return hour, minute


def seconds_until(hour: int, minute: int) -> float:
    """
    Calculate seconds from now until the next occurrence of HH:MM UTC.

    If the target time has already passed today, returns seconds until
    that time tomorrow.

    Args:
        hour: Target hour (0-23)
        minute: Target minute (0-59)

    Returns:
        Seconds until next occurrence
    """
    now = utc_now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if target <= now:
        # Already passed today, schedule for tomorrow
        target += timedelta(days=1)

    return (target - now).total_seconds()


def _run_maintenance_once() -> None:
    """Reap expired ephemeral rows and reset stale fingerprint jobs (blocking)."""
    from app.auth import cleanup_expired_sessions
    from app.database import get_db, _reset_stale_fingerprint_jobs

    try:
        with get_db() as db:
            cleanup_expired_sessions(db)
            from app.playback_activity import prune_activity
            from app.time_utils import utc_now

            prune_activity(db, utc_now())
            from app.home_feed import prune_snapshots

            prune_snapshots(db)
    except Exception as e:
        logger.error(f"Session/ephemeral cleanup failed: {e}")
    try:
        _reset_stale_fingerprint_jobs()
    except Exception as e:
        logger.error(f"Stale fingerprint reset failed: {e}")


async def run_maintenance() -> None:
    """Periodic lightweight maintenance loop.

    Reaps expired sessions/challenges/auth-codes and requeues stale fingerprint
    jobs every few hours. Runs independently of the daily-update scheduler (so it
    still runs when DAILY_UPDATE_ENABLED is false). Never raises — logs and
    continues; the blocking DB work runs off the event loop.
    """
    interval_seconds = 6 * 3600
    logger.info("Maintenance loop started (reaps expired rows every 6h)")
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            from app.job_threads import run_job_thread

            await run_job_thread(_run_maintenance_once)
            # TMDB terms: cached data may be kept for at most six months.
            from workers.metadata_refresh import refresh_stale_metadata

            await refresh_stale_metadata()
            # A newer tag pack, when the owner set TAG_PACK_URL.
            try:
                from app.tag_pack import update_from_url

                await update_from_url()
            except Exception as exc:  # noqa: BLE001 - retried next pass
                logger.warning(f"Tag pack update failed: {exc}")
        except asyncio.CancelledError:
            logger.info("Maintenance loop cancelled, shutting down")
            break
        except Exception as e:
            logger.error(f"Maintenance loop error: {e}")


async def run_detail_snapshot_scheduler() -> None:
    """Continuously prepare complete native details outside the tap path.

    The pass is resumable and skips fresh snapshots, so an hourly cadence is a
    cheap inventory check after the initial deployment warm. Provider work stays
    behind the warmer's four-call hard cap and the stricter background TMDB
    limiter in ``app.main``.
    """
    from workers.detail_snapshot_warm import warm_active_inventory

    interval_seconds = max(300, settings.detail_snapshot_warm_interval_seconds)
    startup_delay = max(0, settings.detail_snapshot_warm_startup_delay_seconds)
    limit = settings.detail_snapshot_warm_limit or None
    concurrency = min(max(settings.detail_snapshot_warm_concurrency, 1), 4)
    logger.info(
        "Detail snapshot scheduler started "
        f"(delay={startup_delay}s interval={interval_seconds}s "
        f"limit={limit or 'unlimited'} concurrency={concurrency})"
    )

    try:
        if startup_delay:
            await asyncio.sleep(startup_delay)
        while True:
            try:
                counts = await warm_active_inventory(limit, concurrency)
                logger.info(f"Scheduled detail snapshot warm finished: {counts}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # A provider outage must not kill future passes or the app.
                logger.error(f"Scheduled detail snapshot warm failed: {e}")
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        logger.info("Detail snapshot scheduler cancelled, shutting down")


async def run_personalized_scheduler() -> None:
    """Refresh personal rows daily without running acquisition or Gemini.

    ``DAILY_UPDATE_ENABLED`` defaults off because its acquisition and shelf
    tagging work can cost money. Personal rows are local recommendations and
    should not freeze merely because that separate job is disabled. This loop
    is started only when the full daily scheduler is off; when it is on, the
    existing post-update sync preserves dependency order.
    """
    from workers.isolated_jobs import sync_all_users_off_loop

    hour, minute = parse_time(settings.daily_update_time)
    logger.info(
        "Personal catalog scheduler started. Local rows will refresh at "
        f"{hour:02d}:{minute:02d} UTC (no Gemini calls)"
    )
    while True:
        try:
            await asyncio.sleep(seconds_until(hour, minute))
            logger.info("Starting scheduled local personalized catalog refresh...")
            await sync_all_users_off_loop()
        except asyncio.CancelledError:
            logger.info("Personal catalog scheduler cancelled, shutting down")
            break
        except Exception as e:
            logger.error(f"Personal catalog refresh failed: {e}")
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                logger.info("Personal catalog scheduler cancelled during retry wait")
                break


async def run_scheduler() -> None:
    """
    Main scheduler loop. Runs daily at the configured time.

    Sleeps until DAILY_UPDATE_TIME, runs the update, then repeats.
    Never raises — logs errors and continues.
    """
    # Import here to avoid circular imports
    from workers.isolated_jobs import (
        run_daily_update_off_loop,
        run_shelf_pass_off_loop,
        sync_all_users_off_loop,
    )

    hour, minute = parse_time(settings.daily_update_time)

    logger.info(
        f"Daily update scheduler started. "
        f"Updates will run at {hour:02d}:{minute:02d} UTC"
    )

    while True:
        try:
            wait_seconds = seconds_until(hour, minute)
            next_run = utc_now() + timedelta(seconds=wait_seconds)

            logger.info(
                f"Next daily update scheduled for {next_run.isoformat()} UTC "
                f"({wait_seconds / 3600:.1f} hours from now)"
            )

            await asyncio.sleep(wait_seconds)

            # Retry with backoff for transient failures (e.g. DNS resolution).
            # A persistent daily-update failure is logged but does NOT abort the
            # loop: the per-user catalog sync below is independent and new
            # profiles rely on it to backfill their rows.
            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                try:
                    logger.info("Starting scheduled daily update...")
                    await run_daily_update_off_loop()
                    break  # success
                except asyncio.TimeoutError:
                    # The run hit its overall deadline and was killed. Retrying
                    # would likely hang the same way (up to 3x the deadline), so
                    # treat it as terminal for today and move on to the sync.
                    logger.error(
                        "Daily update hit its overall deadline and was killed "
                        "— skipping remaining retries"
                    )
                    break
                except Exception as e:
                    if attempt < max_attempts:
                        wait = 30 * attempt  # 30s, 60s
                        logger.warning(
                            f"Daily update attempt {attempt}/{max_attempts} "
                            f"failed: {e} — retrying in {wait}s"
                        )
                        await asyncio.sleep(wait)
                    else:
                        logger.error(
                            f"Daily update failed after {max_attempts} attempts: "
                            f"{e} — continuing to personalized catalog sync"
                        )

            # Sync personalized catalogs for all users from local watch history.
            # Runs regardless of the daily-update outcome above.
            if settings.enable_personalized_sync:
                logger.info("Starting personalized catalog sync for all users...")
                try:
                    await sync_all_users_off_loop()
                except Exception as e:
                    logger.error(f"Personalized catalog sync failed: {e}")

                # Deliberately last: it asks what is on a shelf *now*, so it has
                # to run after the shelves for this cycle exist. The feed rows
                # are filled by a fetch the daily update never sees, which is
                # why a trending title could sit in front of a viewer with no
                # tags, no chips and a TMDB fallback for More Like This.
                if settings.shelf_pass_enabled:
                    try:
                        await run_shelf_pass_off_loop(settings.shelf_pass_limit)
                    except Exception as e:
                        logger.error(f"Shelf tagging pass failed: {e}")

        except asyncio.CancelledError:
            logger.info("Scheduler cancelled, shutting down")
            break
        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")
            # Wait 1 hour before retrying on persistent failure
            logger.info("Retrying in 1 hour...")
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                logger.info("Scheduler cancelled during retry wait")
                break
