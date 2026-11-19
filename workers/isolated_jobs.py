"""Private-loop entry points for jobs launched by the web process.

The synchronous runners create owned asyncio loops; the async ``*_off_loop``
facades submit those runners to the job executor (:mod:`app.job_threads`, never
the request executor) and bridge cancellation back into the private loop.
SQLAlchemy sessions are opened only inside the worker thread, bound to the job
engine, and its HTTP pool is closed before the loop exits.
Only scalar identifiers and plain result dictionaries cross the boundary.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import threading
from time import monotonic
from typing import TypeVar

from loguru import logger

from app.database import get_db, job_db_scope
from app.job_threads import run_job_thread
from app.models import User

T = TypeVar("T")


class _PrivateLoopControl:
    """Thread-safe cancellation bridge from Uvicorn's loop to a job loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._cancel_requested = False

    def attach(self, loop: asyncio.AbstractEventLoop, task: asyncio.Task) -> None:
        with self._lock:
            self._loop = loop
            self._task = task
            cancel_requested = self._cancel_requested
        if cancel_requested:
            task.cancel()

    def detach(self) -> None:
        with self._lock:
            self._loop = None
            self._task = None

    def cancel(self) -> None:
        with self._lock:
            self._cancel_requested = True
            loop = self._loop
            task = self._task
        if loop is not None and task is not None:
            loop.call_soon_threadsafe(task.cancel)


def _run_private_loop(
    job_name: str,
    factory: Callable[[], Awaitable[T]],
    control: _PrivateLoopControl | None = None,
) -> T:
    """Run one job in an owned loop and close its loop-local HTTP clients."""
    started = monotonic()
    status = "succeeded"

    async def runner() -> T:
        task: asyncio.Task[T] = asyncio.create_task(
            factory(), name=f"curatio-{job_name}"
        )
        if control is not None:
            control.attach(asyncio.get_running_loop(), task)
        try:
            return await task
        finally:
            if control is not None:
                control.detach()
            # TMDBClient is a process singleton with one AsyncClient per loop.
            # Closing here is required before asyncio.run closes this job's loop.
            from app.tmdb_client import tmdb_client

            try:
                await tmdb_client.close()
            except Exception as exc:  # noqa: BLE001 - preserve the job result
                logger.warning(f"job={job_name} could not close TMDB client: {exc}")

    try:
        with job_db_scope():
            return asyncio.run(runner())
    except BaseException:
        status = "failed"
        raise
    finally:
        logger.info(
            f"job_phase job={job_name} phase=execute status={status} "
            f"duration_ms={(monotonic() - started) * 1000:.1f}"
        )


async def _offload_private_loop(
    job_name: str, factory: Callable[[], Awaitable[T]]
) -> T:
    """Submit a private-loop job and forward cancellation to that loop."""
    control = _PrivateLoopControl()
    try:
        return await run_job_thread(_run_private_loop, job_name, factory, control)
    except asyncio.CancelledError:
        control.cancel()
        raise


async def _daily_job(timeout_seconds: float | None) -> None:
    from workers.daily_update import run_daily_update

    await run_daily_update(timeout_seconds=timeout_seconds)


async def _all_user_job() -> dict:
    from workers.personalized_sync import sync_all_users

    with get_db() as db:
        return await sync_all_users(db)


async def _single_user_job(user_id: int) -> bool:
    from workers.personalized_sync import sync_user_catalogs

    with get_db() as db:
        user = db.query(User).filter(User.id == int(user_id)).first()
        if user is None:
            return False
        await sync_user_catalogs(user, db)
        return True


async def _shelf_job(limit: int) -> dict:
    from workers.shelf_pass import run_shelf_pass

    return await run_shelf_pass(limit=int(limit))


def run_daily_update_isolated(timeout_seconds: float | None = None) -> None:
    """Run the daily acquisition/publication cycle outside Uvicorn's loop."""

    _run_private_loop("daily_update", lambda: _daily_job(timeout_seconds))


async def run_daily_update_off_loop(timeout_seconds: float | None = None) -> None:
    """Async web-process facade for a cancellable isolated daily update."""
    await _offload_private_loop("daily_update", lambda: _daily_job(timeout_seconds))


def sync_all_users_isolated() -> dict:
    """Synchronize every profile with a worker-owned session and event loop."""

    return _run_private_loop("all_user_sync", _all_user_job)


async def sync_all_users_off_loop() -> dict:
    """Async web-process facade for a cancellable isolated all-user sync."""
    return await _offload_private_loop("all_user_sync", _all_user_job)


def sync_user_catalogs_isolated(user_id: int) -> bool:
    """Synchronize one profile, resolving its ORM object inside the worker."""

    return _run_private_loop(
        "foreground_profile_sync", lambda: _single_user_job(int(user_id))
    )


async def sync_user_catalogs_off_loop(user_id: int) -> bool:
    """Async web-process facade for a cancellable isolated profile sync."""
    return await _offload_private_loop(
        "foreground_profile_sync", lambda: _single_user_job(int(user_id))
    )


def run_shelf_pass_isolated(limit: int) -> dict:
    """Run shelf enrichment/tagging outside Uvicorn's event loop."""

    return _run_private_loop("shelf_pass", lambda: _shelf_job(int(limit)))


async def run_shelf_pass_off_loop(limit: int) -> dict:
    """Async web-process facade for a cancellable isolated shelf pass."""
    return await _offload_private_loop("shelf_pass", lambda: _shelf_job(int(limit)))
