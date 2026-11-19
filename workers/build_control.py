"""
Cooperative pause/stop for long-running tagging jobs.

Extracted from ``workers/initial_build.py`` so the re-tag can share it.
``initial_build`` re-exports every name, because ``app/admin.py`` imports
``pause_event`` and ``request_build_stop`` from there in eight places and those
imports must keep working.

``threading.Event`` rather than an asyncio primitive: the build runs inside
``asyncio.to_thread`` and the admin endpoints that toggle it run on the event
loop, so the flag is set and read from different threads. Cancelling the asyncio
task alone does not stop the build thread -- it runs its own loop -- which is
what previously orphaned a thread and let a second build start alongside it.
"""

from __future__ import annotations

import threading

from loguru import logger

#: Set = running. Cleared = the job parks between batches.
pause_event = threading.Event()
pause_event.set()

#: Set = unwind cleanly at the next batch boundary. The real kill switch.
stop_event = threading.Event()


class BuildStopped(Exception):
    """Raised inside a job thread to unwind cleanly when a stop is requested."""


def request_build_stop() -> None:
    """Ask a running job to stop at its next batch boundary (thread-safe)."""
    stop_event.set()
    # If it is parked on the pause gate, wake it so it can observe the stop.
    pause_event.set()


def reset_build_stop() -> None:
    """Clear the stop flag before starting or resuming a job."""
    stop_event.clear()


def raise_if_stopped() -> None:
    if stop_event.is_set():
        raise BuildStopped()


async def check_pause() -> None:
    """Block until unpaused. No-op when running."""
    if not pause_event.is_set():
        logger.info("Job paused — waiting for resume...")
        # MUST run on a worker thread. Both callers put it there:
        # initial_build via _run_build_sync and the upgrade pipeline via
        # _run_upgrade_sync (app/admin.py). One blocking wait then parks that
        # thread until an admin endpoint sets the event, with no polling.
        #
        # Called from the main event loop instead, this deadlocks the whole
        # server: the loop parks here, so the Resume request that would set the
        # event can never be read off the socket. If you add a third caller,
        # put it on a thread.
        pause_event.wait()
        logger.info("Job resumed")


def clear_serving_cache(reason: str, user_id: int | None = None) -> None:
    """Drop the in-process catalog LRU so a completed job becomes visible.

    Serving reads go through an LRU with a ``cache_ttl`` (default 1 h)
    lifetime, so a job that rewrites catalog content without clearing it stays
    invisible for up to an hour -- indistinguishable, from the operator's side,
    from the job having done nothing. The nightly path did exactly that: both
    ``run_daily_update``'s regeneration and ``sync_all_users`` finished without
    touching it, so the daily re-roll every other part of the system works to
    produce could not be seen until the cache happened to expire.

    Imported lazily inside the function, as every other worker caller does:
    ``app.main`` imports from ``workers`` at module scope, so a top-level
    import here would be circular.

    Never raises. Failing to clear a cache must not fail the work that just
    succeeded.
    """
    try:
        from app.main import (
            _catalog_cache,
            clear_detail_response_cache,
            _invalidate_dashboard_cache,
        )

        _catalog_cache.clear()
        clear_detail_response_cache()
        _invalidate_dashboard_cache()
        from app.home_feed import invalidate_published_discovery

        invalidate_published_discovery(user_id)
        logger.info("catalog, detail, and dashboard caches cleared after " f"{reason}")
    except Exception as exc:  # noqa: BLE001 - a cache miss is not a failure
        logger.warning(f"could not clear catalog cache after {reason}: {exc}")
