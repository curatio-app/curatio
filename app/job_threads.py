"""Thread pool for background work, kept apart from the request executor.

Request handlers offload synchronous DB calls with ``asyncio.to_thread``, which
uses the loop's default executor (sized to the request connection pool in
``startup_event``). Background jobs hold their thread for minutes; on the same
executor they leave interactive requests queued behind them. Everything that
runs for the lifetime of a job goes through :func:`run_job_thread` instead, and
its sessions bind to the job engine via :func:`app.database.job_db_scope`.
"""

from __future__ import annotations

import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypeVar

from app.config import settings
from app.database import job_db_scope

T = TypeVar("T")

_JOB_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(1, settings.job_executor_workers),
    thread_name_prefix="curatio-job",
)


async def run_job_thread(fn: Callable[..., T], /, *args, **kwargs) -> T:
    """``asyncio.to_thread`` for background work, on the job executor."""
    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()

    def call() -> T:
        with job_db_scope():
            return fn(*args, **kwargs)

    return await loop.run_in_executor(_JOB_EXECUTOR, context.run, call)
