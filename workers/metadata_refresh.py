"""Keep cached TMDB metadata younger than TMDB's six-month caching limit.

TMDB's API terms allow caching data for up to six months. ``media_metadata``
rows were written once and never refreshed, so this job re-fetches the oldest
rows a batch at a time, sized so every row is revisited well inside the limit.
Runs from the maintenance loop (every six hours); never raises.
"""

from __future__ import annotations

import asyncio
import math
from datetime import timedelta

import httpx
from loguru import logger
from sqlalchemy import func

from app.config import settings
from app.database import get_db
from app.models import MediaMetadata
from app.time_utils import utc_now

#: Refresh anything older than this (a margin under TMDB's six months).
STALE_AFTER = timedelta(days=165)
#: Revisit the whole catalog within this many days of maintenance runs.
CYCLE_DAYS = 120
RUNS_PER_DAY = 4  # the maintenance loop runs every six hours
MIN_BATCH, MAX_BATCH = 50, 1500
CONCURRENCY = 4


def _batch_size(total: int) -> int:
    per_run = math.ceil(total / (CYCLE_DAYS * RUNS_PER_DAY)) if total else 0
    return max(MIN_BATCH, min(MAX_BATCH, per_run))


async def refresh_stale_metadata(limit: int | None = None) -> dict:
    """Re-fetch the stalest TMDB rows. Returns counts; never raises."""
    if not settings.tmdb_api_key:
        return {"status": "skipped"}
    from app.tmdb_client import tmdb_client
    from workers.metadata_store import upsert_media_metadata

    cutoff = utc_now() - STALE_AFTER
    with get_db() as db:
        total = db.query(func.count(MediaMetadata.tmdb_id)).scalar() or 0
        stale = (
            db.query(MediaMetadata.tmdb_id, MediaMetadata.media_type)
            .filter(MediaMetadata.fetched_at < cutoff)
            .order_by(MediaMetadata.fetched_at)
            .limit(limit or _batch_size(total))
            .all()
        )
    if not stale:
        return {"status": "ok", "refreshed": 0, "failed": 0}

    gate = asyncio.Semaphore(CONCURRENCY)

    async def fetch(tmdb_id: int, media_type: str):
        async with gate:
            try:
                if media_type == "movie":
                    return tmdb_id, media_type, await tmdb_client.get_movie(tmdb_id)
                return tmdb_id, media_type, await tmdb_client.get_tv_show(tmdb_id)
            except httpx.HTTPStatusError as exc:
                logger.debug(f"TMDB refresh {media_type}/{tmdb_id}: {exc}")
            except Exception as exc:
                logger.debug(f"TMDB refresh {media_type}/{tmdb_id} failed: {exc}")
            return tmdb_id, media_type, None

    results = await asyncio.gather(*(fetch(t, m) for t, m in stale))
    refreshed = failed = 0
    with get_db() as db:
        for tmdb_id, media_type, details in results:
            if not details:
                failed += 1
                continue
            try:
                upsert_media_metadata(
                    db, tmdb_client.extract_metadata(details, media_type)  # type: ignore[arg-type]
                )
                db.query(MediaMetadata).filter(
                    MediaMetadata.tmdb_id == tmdb_id,
                    MediaMetadata.media_type == media_type,
                ).update(
                    {MediaMetadata.fetched_at: utc_now()}, synchronize_session=False
                )
                refreshed += 1
            except Exception as exc:
                failed += 1
                logger.debug(f"TMDB refresh store {media_type}/{tmdb_id} failed: {exc}")
        db.commit()
    logger.info(f"TMDB metadata refresh: {refreshed} refreshed, {failed} failed")
    return {"status": "ok", "refreshed": refreshed, "failed": failed}
