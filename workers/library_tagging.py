"""Tag what the profiles own, not the whole catalog.

Recommendations need tags, and every install pays for tagging with its owner's
own AI key. So Curatio tags only:

* titles on the connected Plex/Jellyfin servers, and
* titles someone saved to their Watchlist (likely to be owned next).

Tagging the rest of the catalog is opt-in (``TAG_CATALOG_BEYOND_LIBRARY``).

With no AI key configured, titles still get the rule-derived tags (genres,
TMDB keywords and other facts mapped onto Curatio's vocabulary, ``source=
"derived"``). When a key is added later, those titles are re-tagged with the
model. Titles covered by the initial tag pack (``source="pack"``) are never
re-tagged here.

Spend is capped per calendar month by ``LIBRARY_TAGGING_MONTHLY_CAP_USD``
(0 = no cap).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session, defer

from app.config import settings
from app.database import get_db
from app.models import (
    AdminSetting,
    LibraryItem,
    MediaAvailability,
    MediaMetadata,
    MovieTag,
)
from app.time_utils import utc_now

#: Titles handed to the tagger per run. Small runs keep a first sync of a big
#: library from holding the provider for an hour; the next run continues.
BATCH_LIMIT = 400

# Lower-case keys are read directly, never applied to settings (admin.py).
_SPEND_KEY = "library_tagging_spend"

_lock = asyncio.Lock()
status: dict[str, Any] = {
    "running": False,
    "last_run_at": None,
    "last_result": None,
}


def _kind(media_type: str | None) -> str | None:
    if media_type == "movie":
        return "movie"
    if media_type in ("series", "tv", "show"):
        return "tv"
    return None


def wanted_titles(db: Session) -> dict[str, set[int]]:
    """Owned titles plus Watchlist titles, by "movie" / "tv"."""
    wanted: dict[str, set[int]] = {"movie": set(), "tv": set()}
    for media_type, tmdb_id in (
        db.query(MediaAvailability.media_type, MediaAvailability.tmdb_id)
        .filter(MediaAvailability.tmdb_id.isnot(None))
        .distinct()
    ):
        kind = _kind(media_type)
        if kind:
            wanted[kind].add(int(tmdb_id))
    for media_type, tmdb_id in (
        db.query(LibraryItem.media_type, LibraryItem.tmdb_id)
        .filter(LibraryItem.tmdb_id.isnot(None))
        .distinct()
    ):
        kind = _kind(media_type)
        if kind:
            wanted[kind].add(int(tmdb_id))
    return wanted


def _tag_sources(db: Session, media_type: str, ids: list[int], version: str):
    """{tmdb_id: set of tag sources at ``version``}."""
    found: dict[int, set[str]] = {}
    for start in range(0, len(ids), 1000):
        chunk = ids[start : start + 1000]
        for tmdb_id, source in (
            db.query(MovieTag.tmdb_id, MovieTag.source)
            .filter(
                MovieTag.media_type == media_type,
                MovieTag.tmdb_id.in_(chunk),
                MovieTag.tagger_version == version,
            )
            .distinct()
        ):
            found.setdefault(int(tmdb_id), set()).add(source or "")
    return found


def _pack_tagged(db: Session, media_type: str, ids: list[int]) -> set[int]:
    """Titles with pack tags (of any version: the pack may be older)."""
    found: set[int] = set()
    for start in range(0, len(ids), 1000):
        chunk = ids[start : start + 1000]
        found.update(
            int(row[0])
            for row in db.query(MovieTag.tmdb_id)
            .filter(MovieTag.media_type == media_type, MovieTag.tmdb_id.in_(chunk))
            .filter(MovieTag.source == "pack")
            .distinct()
        )
    return found


def _terminal(db: Session, media_type: str, ids: list[int], version: str) -> set[int]:
    found: set[int] = set()
    for start in range(0, len(ids), 1000):
        chunk = ids[start : start + 1000]
        found.update(
            int(row[0])
            for row in db.query(MediaMetadata.tmdb_id).filter(
                MediaMetadata.media_type == media_type,
                MediaMetadata.tmdb_id.in_(chunk),
                MediaMetadata.tag_attempt_version == version,
            )
        )
    return found


def pending(db: Session, ai_ready: bool) -> dict[str, dict[str, list[int]]]:
    """Titles to tag: ``{media_type: {"new": [...], "upgrade": [...]}}``.

    ``new`` have no current tags. ``upgrade`` have only rule-derived tags and
    are re-tagged with the model once an AI key is configured.
    """
    from workers.tagging_common import tagger_version

    version = tagger_version()
    result: dict[str, dict[str, list[int]]] = {}
    for media_type, ids in wanted_titles(db).items():
        ordered = sorted(ids)
        sources = _tag_sources(db, media_type, ordered, version)
        pack = _pack_tagged(db, media_type, ordered)
        terminal = _terminal(db, media_type, ordered, version)
        new = [
            i
            for i in ordered
            if i not in sources and i not in pack and i not in terminal
        ]
        upgrade = (
            [i for i in ordered if sources.get(i) == {"derived"} and i not in pack]
            if ai_ready
            else []
        )
        result[media_type] = {"new": new, "upgrade": upgrade}
    return result


def summary(db: Session) -> dict[str, Any]:
    """Counts for Admin Center: owned/Watchlist titles and how many are tagged."""
    from app.tagging_provider import provider_ready

    wanted = wanted_titles(db)
    total = sum(len(ids) for ids in wanted.values())
    todo = pending(db, provider_ready())
    waiting = sum(len(v["new"]) for v in todo.values())
    upgrades = sum(len(v["upgrade"]) for v in todo.values())
    return {
        "titles": total,
        "tagged": max(total - waiting, 0),
        "waiting": waiting,
        "rule_tagged_awaiting_ai": upgrades,
        "ai_ready": provider_ready(),
        "catalog_beyond_library": bool(settings.tag_catalog_beyond_library),
        "monthly_cap_usd": float(settings.library_tagging_monthly_cap_usd or 0),
        "spent_this_month_usd": round(month_spend(db), 4),
        "running": status["running"],
        "last_run_at": status["last_run_at"],
        "last_result": status["last_result"],
    }


# --- monthly spend --------------------------------------------------------------
def _month() -> str:
    return utc_now().strftime("%Y-%m")


def month_spend(db: Session) -> float:
    row = db.get(AdminSetting, _SPEND_KEY)
    try:
        data = json.loads(row.value) if row and row.value else {}
    except ValueError:
        data = {}
    return float(data.get(_month(), 0.0))


def _add_spend(db: Session, amount: float) -> None:
    if amount <= 0:
        return
    row = db.get(AdminSetting, _SPEND_KEY)
    try:
        data = json.loads(row.value) if row and row.value else {}
    except ValueError:
        data = {}
    month = _month()
    data = {month: float(data.get(month, 0.0)) + amount}  # keep this month only
    if row is None:
        db.add(AdminSetting(key=_SPEND_KEY, value=json.dumps(data)))
    else:
        row.value = json.dumps(data)
    db.commit()


def _budget_left(db: Session) -> float | None:
    cap = float(settings.library_tagging_monthly_cap_usd or 0)
    if cap <= 0:
        return None
    return max(cap - month_spend(db), 0.0)


# --- the run ---------------------------------------------------------------------
async def _tag(
    db: Session,
    media_type: str,
    ids: list[int],
    *,
    deterministic_only: bool,
    replace_all: bool,
) -> dict[str, Any]:
    from workers.tagging_common import build_features, tag_and_store

    if not ids:
        return {}
    if settings.tmdb_api_key:
        try:
            from workers.enrich import enrich

            await enrich(media_type=media_type, tmdb_ids=ids)
        except Exception as exc:  # noqa: BLE001 - thin tags beat none
            logger.warning("Could not enrich owned {} titles: {}", media_type, exc)
    rows = (
        db.query(MediaMetadata)
        .options(defer(MediaMetadata.raw_data))
        .filter(
            MediaMetadata.media_type == media_type,
            MediaMetadata.tmdb_id.in_(ids),
        )
        .all()
    )
    features = [build_features(row) for row in rows]
    if not features:
        return {}
    return await tag_and_store(
        db,
        features,
        deterministic_only=deterministic_only,
        replace_all=replace_all,
    )


async def tag_owned_titles(limit: int = BATCH_LIMIT) -> dict[str, Any]:
    """Tag up to ``limit`` owned/Watchlist titles. Safe to call often."""
    from app.tagging_provider import provider_ready

    if _lock.locked():
        return {"status": "busy"}
    async with _lock:
        status["running"] = True
        try:
            result = await _run(limit, provider_ready())
        finally:
            status["running"] = False
            status["last_run_at"] = utc_now().isoformat()
        status["last_result"] = result
        return result


async def _run(limit: int, ai_ready: bool) -> dict[str, Any]:
    written = processed = 0
    cost = 0.0
    capped = False
    with get_db() as db:
        todo = pending(db, ai_ready)
        budget = _budget_left(db)
        use_ai = ai_ready and (budget is None or budget > 0)
        capped = ai_ready and not use_ai
        remaining = limit
        for media_type, groups in todo.items():
            for group in ("new", "upgrade"):
                ids = groups[group][:remaining]
                if not ids or (group == "upgrade" and not use_ai):
                    continue
                stats = await _tag(
                    db,
                    media_type,
                    ids,
                    deterministic_only=not use_ai,
                    replace_all=group == "upgrade",
                )
                remaining -= len(ids)
                processed += int(stats.get("processed", 0))
                written += int(stats.get("tags_written", 0))
                spent = float(stats.get("cost_usd", 0.0) or 0.0)
                cost += spent
                _add_spend(db, spent)
                if budget is not None and month_spend(db) >= float(
                    settings.library_tagging_monthly_cap_usd
                ):
                    use_ai = False
                    capped = True
                if remaining <= 0:
                    break
            if remaining <= 0:
                break
    if processed:
        await asyncio.to_thread(_refresh_relevance)
    result = {
        "status": "ok",
        "processed": processed,
        "tags_written": written,
        "cost_usd": round(cost, 4),
        "mode": "ai" if ai_ready and not capped else "rules",
        "capped": capped,
    }
    if processed:
        logger.info("Library tagging: {}", result)
    return result


def _refresh_relevance() -> None:
    try:
        from workers.daily_update import refresh_relevance_threaded

        refresh_relevance_threaded("library_tagging")
    except Exception as exc:  # noqa: BLE001 - weights catch up on the next run
        logger.warning("Relevance refresh after library tagging failed: {}", exc)


def owned_count(db: Session) -> int:
    return int(
        db.query(func.count(func.distinct(MediaAvailability.tmdb_id)))
        .filter(MediaAvailability.tmdb_id.isnot(None))
        .scalar()
        or 0
    )
