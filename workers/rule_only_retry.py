"""Retry AI tagging for titles that only have rule-based tags.

Most such titles are complete: the tagger asks the model only when a title's
evidence supports an interpretive tag (mood, themes, style), and otherwise
writes the rule-derived tags on purpose. A smaller number got rule-based tags
because a provider call failed or the model left them out of its answer; the
title still counts as tagged at the current version, so no later run picks it
up again. On the reference server a retry of 10,778 such titles gave 748 AI
tags; the rest were complete.

This repair finds titles whose only tags are ``source="derived"`` and that
have a synopsis (the evidence the model needs), most-voted first, and runs
them through the AI tagger again, within ``RETAG_COST_CEILING_USD``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger
from sqlalchemy import and_, exists, func, or_
from sqlalchemy.orm import Session, defer

from app.config import settings
from app.database import get_db
from app.models import MediaMetadata, MovieTag
from app.time_utils import utc_now

CHUNK = 200

status: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "total": 0,
    "done": 0,
    "ai_tagged": 0,
    "still_rule_only": 0,
    "cost_usd": 0.0,
    "first_error": "",
    "stopped_reason": "",
}
_lock = asyncio.Lock()


def _rule_only_query(db: Session, min_votes: int):
    tagged = exists().where(
        and_(
            MovieTag.tmdb_id == MediaMetadata.tmdb_id,
            MovieTag.media_type == MediaMetadata.media_type,
        )
    )
    not_derived = exists().where(
        and_(
            MovieTag.tmdb_id == MediaMetadata.tmdb_id,
            MovieTag.media_type == MediaMetadata.media_type,
            or_(MovieTag.source.is_(None), MovieTag.source != "derived"),
        )
    )
    return db.query(MediaMetadata.media_type, MediaMetadata.tmdb_id).filter(
        MediaMetadata.vote_count >= min_votes,
        func.length(func.trim(func.coalesce(MediaMetadata.overview, ""))) > 0,
        tagged,
        ~not_derived,
    )


def count_rule_only(db: Session, min_votes: int = 5) -> dict[str, int]:
    counts = {"movie": 0, "tv": 0}
    for media_type, total in (
        _rule_only_query(db, min_votes)
        .with_entities(MediaMetadata.media_type, func.count())
        .group_by(MediaMetadata.media_type)
    ):
        counts[media_type] = int(total)
    return counts


def rule_only_titles(db: Session, min_votes: int = 5) -> list[tuple[str, int]]:
    """``[(media_type, tmdb_id)]``, most-voted first."""
    return [
        (media_type, int(tmdb_id))
        for media_type, tmdb_id in _rule_only_query(db, min_votes).order_by(
            MediaMetadata.vote_count.desc(), MediaMetadata.tmdb_id
        )
    ]


async def retry_rule_only(min_votes: int = 5, limit: int | None = None) -> dict:
    """Re-tag rule-only titles with the AI provider. One run at a time."""
    from app.tagging_provider import provider_ready
    from workers.tagging_common import build_features, tag_and_store

    if _lock.locked():
        return {"status": "busy"}
    if not provider_ready():
        return {"status": "no_ai_provider"}
    async with _lock:
        with get_db() as db:
            todo = rule_only_titles(db, min_votes)
        if limit is not None:
            todo = todo[:limit]
        status.update(
            running=True,
            started_at=utc_now().isoformat(),
            finished_at=None,
            total=len(todo),
            done=0,
            ai_tagged=0,
            still_rule_only=0,
            cost_usd=0.0,
            first_error="",
            stopped_reason="",
        )
        ceiling = float(settings.retag_cost_ceiling_usd)
        try:
            for start in range(0, len(todo), CHUNK):
                if status["cost_usd"] >= ceiling:
                    status["stopped_reason"] = f"Reached the ${ceiling:.2f} ceiling"
                    break
                chunk = todo[start : start + CHUNK]
                with get_db() as db:
                    for media_type in ("movie", "tv"):
                        ids = [t for kind, t in chunk if kind == media_type]
                        if not ids:
                            continue
                        rows = (
                            db.query(MediaMetadata)
                            .options(defer(MediaMetadata.raw_data))
                            .filter(
                                MediaMetadata.media_type == media_type,
                                MediaMetadata.tmdb_id.in_(ids),
                            )
                            .all()
                        )
                        stats = await tag_and_store(
                            db,
                            [build_features(row) for row in rows],
                            replace_all=True,
                            spent_usd=float(status["cost_usd"]),
                        )
                        status["cost_usd"] += float(stats.get("cost_usd") or 0.0)
                        if stats.get("first_error") and not status["first_error"]:
                            status["first_error"] = str(stats["first_error"])[:300]
                        if stats.get("halted"):
                            status["stopped_reason"] = str(
                                stats.get("halt_reason") or "The tagger stopped"
                            )
                    status["done"] += len(chunk)
                    remaining = {
                        (kind, tmdb)
                        for kind, tmdb in _rule_only_query(db, min_votes).filter(
                            MediaMetadata.tmdb_id.in_([t for _, t in chunk])
                        )
                    }
                    left = sum(1 for key in chunk if key in remaining)
                    status["still_rule_only"] += left
                    status["ai_tagged"] += len(chunk) - left
                if status["stopped_reason"]:
                    break
        finally:
            status["running"] = False
            status["finished_at"] = utc_now().isoformat()
        if status["ai_tagged"]:
            await asyncio.to_thread(_refresh_relevance)
        result = {"status": "ok", **{k: v for k, v in status.items() if k != "running"}}
        logger.info("Rule-only retry finished: {}", result)
        return result


def _refresh_relevance() -> None:
    try:
        from workers.daily_update import refresh_relevance_threaded

        refresh_relevance_threaded("rule_only_retry")
    except Exception as exc:  # noqa: BLE001 - weights catch up nightly
        logger.warning("Relevance refresh after rule-only retry failed: {}", exc)
