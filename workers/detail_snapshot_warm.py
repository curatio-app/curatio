"""Resumable warmer for Curatio-native detail snapshots.

The warmer intentionally uses the same detail assembly path as interactive and
batch requests.  Existing fresh snapshots are skipped, so the process can be
stopped and restarted without repeating completed work.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from app.main import (
    _DETAIL_MOVIE_FRESH_SECONDS,
    _DETAIL_SNAPSHOT_SCHEMA_VERSION,
    _DETAIL_TV_FRESH_SECONDS,
    _detail_snapshot_is_complete,
    _discovery_details_payload,
    _singleflight_detail_build,
    purge_expired_detail_snapshots,
)
from app.database import get_db
from app.job_threads import run_job_thread
from app.models import (
    DetailSnapshot,
    LibraryItem,
    UniversalCategory,
    UniversalCatalogContent,
    UserCatalog,
    UserCatalogContent,
    WatchProgress,
)

logger = logging.getLogger("curatio.detail_snapshot_warm")


@dataclass(frozen=True)
class Candidate:
    tmdb_id: int
    media_type: str


def _candidate(tmdb_id: object, media_type: object) -> Candidate | None:
    try:
        parsed_id = int(tmdb_id)
    except (TypeError, ValueError):
        return None
    parsed_type = "tv" if str(media_type or "").lower() in {"tv", "series"} else "movie"
    return Candidate(parsed_id, parsed_type)


def _append_unique(
    destination: list[Candidate],
    seen: set[tuple[int, str]],
    rows: Iterable[tuple[object, object]],
    limit: int | None,
) -> None:
    for tmdb_id, media_type in rows:
        value = _candidate(tmdb_id, media_type)
        if value is None or (value.tmdb_id, value.media_type) in seen:
            continue
        seen.add((value.tmdb_id, value.media_type))
        destination.append(value)
        if limit is not None and len(destination) >= limit:
            return


def load_candidates(limit: int | None) -> list[Candidate]:
    """Load active heroes/shelves, libraries, and recent history in priority order."""

    candidates: list[Candidate] = []
    seen: set[tuple[int, str]] = set()
    with get_db() as db:
        active_rows = db.execute(
            select(UniversalCatalogContent.tmdb_id, UniversalCatalogContent.media_type)
            .join(
                UniversalCategory,
                UniversalCategory.id == UniversalCatalogContent.category_id,
            )
            .where(UniversalCategory.is_active.is_(True))
            # The native Home rail renders up to 40 cards. Warming only its first
            # 14 made the beginning instant and the scrolled tail upstream-bound.
            .where(UniversalCatalogContent.rank < 40)
            .order_by(
                UniversalCategory.manifest_priority.desc(),
                UniversalCatalogContent.rank.asc(),
            )
        ).all()
        _append_unique(candidates, seen, active_rows, limit)

        if limit is None or len(candidates) < limit:
            shelf_rows = db.execute(
                select(UserCatalogContent.tmdb_id, UserCatalogContent.media_type)
                .join(UserCatalog, UserCatalog.id == UserCatalogContent.catalog_id)
                .where(UserCatalog.is_active.is_(True))
                .where(UserCatalogContent.rank < 40)
                .order_by(UserCatalogContent.rank.asc())
            ).all()
            _append_unique(candidates, seen, shelf_rows, limit)

        if limit is None or len(candidates) < limit:
            library_rows = db.execute(
                select(LibraryItem.tmdb_id, LibraryItem.media_type)
                .where(LibraryItem.tmdb_id.is_not(None))
                .order_by(LibraryItem.added_at.desc())
            ).all()
            _append_unique(candidates, seen, library_rows, limit)

        if limit is None or len(candidates) < limit:
            history_rows = db.execute(
                select(WatchProgress.tmdb_id, WatchProgress.media_type)
                .where(WatchProgress.tmdb_id.is_not(None))
                .order_by(WatchProgress.updated_at.desc())
            ).all()
            _append_unique(candidates, seen, history_rows, limit)

    return candidates[:limit] if limit is not None else candidates


def _snapshot_is_fresh(snapshot: DetailSnapshot, now: datetime) -> bool:
    if snapshot.schema_version != _DETAIL_SNAPSHOT_SCHEMA_VERSION:
        return False
    if not _detail_snapshot_is_complete(snapshot.payload, snapshot.media_type):
        return False
    refreshed_at = snapshot.refreshed_at
    if refreshed_at.tzinfo is None:
        refreshed_at = refreshed_at.replace(tzinfo=timezone.utc)
    ttl = (
        _DETAIL_TV_FRESH_SECONDS
        if snapshot.media_type == "tv"
        else _DETAIL_MOVIE_FRESH_SECONDS
    )
    return (now - refreshed_at).total_seconds() < ttl


def _snapshot_state(candidate: Candidate) -> tuple[bool, bool]:
    """(fresh, complete) for a stored snapshot; decodes its payload off the loop."""
    with get_db() as db:
        snapshot = db.get(DetailSnapshot, (candidate.tmdb_id, candidate.media_type))
        snapshot_is_fresh = snapshot is not None and _snapshot_is_fresh(
            snapshot, datetime.now(timezone.utc)
        )
        snapshot_is_complete = (
            snapshot is not None
            and isinstance(snapshot.payload, dict)
            and _detail_snapshot_is_complete(snapshot.payload, candidate.media_type)
        )
    return snapshot_is_fresh, snapshot_is_complete


async def warm_candidate(candidate: Candidate) -> str:
    snapshot_is_fresh, snapshot_is_complete = await run_job_thread(
        _snapshot_state, candidate
    )

    if snapshot_is_fresh:
        # The snapshot contains the last complete overlay and can seed the memory
        # cache on demand in milliseconds. Do not spend the initial warm pass
        # synchronously rebuilding vector similarity for every fresh title.
        return "reused"

    if snapshot_is_complete:
        # A stale snapshot is served stale-while-revalidate to interactive callers,
        # but the inventory warmer must await the shared provider refresh so its
        # progress reflects genuinely completed work and concurrency stays bounded.
        payload = await _singleflight_detail_build(
            candidate.tmdb_id,
            candidate.media_type,
            background=True,
        )
        return (
            "warmed"
            if _detail_snapshot_is_complete(payload, candidate.media_type)
            else "partial"
        )

    payload, _ = await _discovery_details_payload(
        candidate.tmdb_id,
        candidate.media_type,
        None,
        background=True,
    )
    if not _detail_snapshot_is_complete(payload, candidate.media_type):
        return "partial"
    return "warmed"


async def run_warmer(candidates: list[Candidate], concurrency: int) -> dict[str, int]:
    limiter = asyncio.Semaphore(max(1, min(concurrency, 4)))
    counts = {"warmed": 0, "reused": 0, "partial": 0, "failed": 0}
    counts_lock = asyncio.Lock()

    async def run_one(index: int, candidate: Candidate) -> None:
        async with limiter:
            try:
                outcome = await warm_candidate(candidate)
            except Exception:
                logger.exception(
                    "Detail warmer failed index=%s tmdb_id=%s media_type=%s",
                    index,
                    candidate.tmdb_id,
                    candidate.media_type,
                )
                outcome = "failed"
            async with counts_lock:
                counts[outcome] += 1
                completed = sum(counts.values())
                if completed % 25 == 0 or completed == len(candidates):
                    logger.info(
                        "Detail warmer progress %s/%s %s",
                        completed,
                        len(candidates),
                        counts,
                    )

    await asyncio.gather(
        *(run_one(index, candidate) for index, candidate in enumerate(candidates, 1))
    )
    return counts


def _purge_expired() -> int:
    with get_db() as db:
        return purge_expired_detail_snapshots(db)


async def warm_active_inventory(limit: int | None, concurrency: int) -> dict[str, int]:
    """Warm the prioritized inventory for both CLI and in-process scheduling."""
    purged = await run_job_thread(_purge_expired)
    candidates = await run_job_thread(load_candidates, limit)
    logger.info(
        "Starting detail warmer candidates=%s purged=%s", len(candidates), purged
    )
    counts = await run_warmer(candidates, concurrency)
    logger.info("Finished detail warmer %s", counts)
    return counts


async def async_main(limit: int | None, concurrency: int) -> int:
    counts = await warm_active_inventory(limit, concurrency)
    return 1 if counts["failed"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=None, help="Stop after this many unique titles"
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="Concurrent background builds (maximum 4)",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    return asyncio.run(async_main(args.limit, args.concurrency))


if __name__ == "__main__":
    raise SystemExit(main())
