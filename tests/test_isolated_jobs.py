"""Regression coverage for web-process job isolation."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_isolated_jobs.db")
os.environ.setdefault("TMDB_API_KEY", "test")
os.environ.setdefault("GEMINI_API_KEY", "test")
os.environ.setdefault("MASTER_PASSWORD", "test")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SECRET_KEY", "t" * 40)
os.environ.setdefault("SKIP_API_VALIDATION", "true")


@pytest.mark.asyncio
async def test_blocked_daily_job_does_not_stop_heartbeat_or_health(monkeypatch):
    """A synchronous relevance-shaped stall must stay off Uvicorn's loop."""
    from app import main
    from app.tmdb_client import tmdb_client
    from workers import isolated_jobs

    started = threading.Event()
    job_thread = None
    job_loop = None

    async def blocked_job(_timeout):
        nonlocal job_thread, job_loop
        job_thread = threading.get_ident()
        job_loop = asyncio.get_running_loop()
        started.set()
        time.sleep(0.25)

    close = AsyncMock()
    monkeypatch.setattr(isolated_jobs, "_daily_job", blocked_job)
    monkeypatch.setattr(tmdb_client, "close", close)
    monkeypatch.setattr(main, "check_database_connection", lambda: True)
    monkeypatch.setattr(main, "_SLOW_REQUEST_SECONDS", 0.0)
    warnings = []
    sink = logger.add(lambda message: warnings.append(str(message)), level="WARNING")

    web_loop = asyncio.get_running_loop()
    web_thread = threading.get_ident()
    task = asyncio.create_task(isolated_jobs.run_daily_update_off_loop())
    while not started.is_set():
        await asyncio.sleep(0.001)

    heartbeat_started = web_loop.time()
    await asyncio.sleep(0.02)
    assert web_loop.time() - heartbeat_started < 0.1
    assert not task.done()

    try:
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await asyncio.wait_for(client.get("/health"), timeout=0.2)
    finally:
        logger.remove(sink)

    await task
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert job_thread != web_thread
    assert job_loop is not web_loop
    assert any("slow_request" in row and "route=/health" in row for row in warnings)
    close.assert_awaited_once()


@pytest.mark.asyncio
async def test_profile_job_creates_session_and_resolves_orm_in_worker(monkeypatch):
    """Only the integer user id may cross from the request loop."""
    from app.tmdb_client import tmdb_client
    from workers import isolated_jobs, personalized_sync

    web_thread = threading.get_ident()
    seen = {}
    fake_user = SimpleNamespace(id=17)

    class FakeSession:
        def query(self, _model):
            seen["query_thread"] = threading.get_ident()
            return self

        def filter(self, *_criteria):
            return self

        def first(self):
            seen["orm_thread"] = threading.get_ident()
            return fake_user

    @contextmanager
    def worker_session():
        seen["session_thread"] = threading.get_ident()
        yield FakeSession()

    async def sync(user, db):
        seen["sync_thread"] = threading.get_ident()
        assert user is fake_user
        assert isinstance(db, FakeSession)

    monkeypatch.setattr(isolated_jobs, "get_db", worker_session)
    monkeypatch.setattr(personalized_sync, "sync_user_catalogs", sync)
    monkeypatch.setattr(tmdb_client, "close", AsyncMock())

    assert await isolated_jobs.sync_user_catalogs_off_loop(17) is True
    assert set(seen.values()) == {seen["session_thread"]}
    assert seen["session_thread"] != web_thread


@pytest.mark.asyncio
async def test_cancellation_reaches_private_job_loop_and_closes_client(monkeypatch):
    from app.tmdb_client import tmdb_client
    from workers import isolated_jobs

    started = threading.Event()
    cancelled = threading.Event()

    async def cancellable(_timeout):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    close = AsyncMock()
    monkeypatch.setattr(isolated_jobs, "_daily_job", cancellable)
    monkeypatch.setattr(tmdb_client, "close", close)

    task = asyncio.create_task(isolated_jobs.run_daily_update_off_loop())
    while not started.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    for _ in range(100):
        if cancelled.is_set() and close.await_count:
            break
        await asyncio.sleep(0.005)
    assert cancelled.is_set()
    close.assert_awaited_once()


@pytest.mark.asyncio
async def test_running_job_does_not_occupy_request_executor(monkeypatch):
    """Jobs run on their own executor; request-path to_thread work never queues."""
    from concurrent.futures import ThreadPoolExecutor

    from app.tmdb_client import tmdb_client
    from workers import isolated_jobs

    release = threading.Event()
    started = threading.Event()
    job_thread_name = None

    async def blocked_job(_timeout):
        nonlocal job_thread_name
        job_thread_name = threading.current_thread().name
        started.set()
        release.wait(5)

    monkeypatch.setattr(isolated_jobs, "_daily_job", blocked_job)
    monkeypatch.setattr(tmdb_client, "close", AsyncMock())

    loop = asyncio.get_running_loop()
    # A one-thread request executor: before the split, the job alone filled it.
    request_executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(request_executor)
    try:
        task = asyncio.create_task(isolated_jobs.run_daily_update_off_loop())
        while not started.is_set():
            await asyncio.sleep(0.001)
        result = await asyncio.wait_for(asyncio.to_thread(lambda: "served"), 1)
        assert result == "served"
        assert job_thread_name.startswith("curatio-job")
        release.set()
        await task
    finally:
        release.set()
        request_executor.shutdown(wait=False)


def test_job_scope_routes_new_sessions_to_job_engine(monkeypatch):
    """Sessions opened inside a job bind to the job engine; others do not."""
    from app import database

    sentinel = object()
    monkeypatch.setattr(database, "job_engine", sentinel)
    captured = []

    def fake_call(self, **local_kw):
        captured.append(local_kw.get("bind"))
        return SimpleNamespace()

    monkeypatch.setattr(database.sessionmaker, "__call__", fake_call)
    database.SessionLocal()
    with database.job_db_scope():
        database.SessionLocal()
    database.SessionLocal()
    assert captured == [None, sentinel, None]
