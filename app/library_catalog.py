"""What the connected Plex/Jellyfin servers own, shaped as catalog cards.

Feeds the "On Your Servers" Home row, the "My Servers" content scope and the
Library tab. Reads only ``media_availability`` (filled by the library sync
jobs) joined to ``media_metadata``; never touches the network on a request.

Ownership is about library providers only. A searchable source addon
makes every title *playable*, but never *owned*, so nothing here changes when
an addon is enabled.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Iterable, Literal, Optional

from loguru import logger
from sqlalchemy import func, tuple_
from sqlalchemy.orm import Session

from app.catalog_generator import CatalogGenerator
from app.database import get_db
from app.models import MediaAvailability, MediaMetadata, WatchEvent
from app.server_history import TASTE_ACTIONS

Kind = Literal["movie", "tv"]
Sort = Literal["added", "title", "year", "popular"]

CONTENT_SCOPES = ("everything", "my_servers")
DEFAULT_SCOPE = "everything"

RECENT_ROW_ID = "servers-recent"
MOVIES_ROW_ID = "servers-movies"
SHOWS_ROW_ID = "servers-shows"
RECENT_ROW_TITLE = "On Your Servers"
#: Titles in the Home row (newest first); the Library tab pages the rest.
ROW_LIMIT = 80
#: In "My Servers" scope a filtered discovery row needs at least this many
#: owned titles to stay on Home (the normal floor is far higher).
MY_SERVERS_MIN_ROW_ITEMS = 6
#: Bound the IMDb -> TMDB lookups one sync may spend.
MAX_IMDB_LOOKUPS_PER_RUN = 200

_RES_RANK = {"2160p": 4, "1440p": 3, "1080p": 3, "720p": 2, "576p": 1, "480p": 0}


def _kind(media_type: Optional[str]) -> Optional[Kind]:
    value = str(media_type or "").lower()
    if value in ("movie", "film"):
        return "movie"
    if value in ("series", "tv", "show"):
        return "tv"
    return None


def normalize_scope(value: Optional[str]) -> str:
    return value if value in CONTENT_SCOPES else DEFAULT_SCOPE


def _library_provider_ids() -> list[str]:
    from app.sources import registry

    return [p.id for p in registry.enabled_providers() if p.kind == "library"]


def owned_entries(db: Session) -> dict[tuple[str, int], dict]:
    """``{(kind, tmdb_id): {added_at, resolution, providers}}`` for owned titles.

    Rows known only by IMDb id are matched through ``media_metadata.imdb_id``;
    ones that still have no TMDB id are left for :func:`prepare_owned_metadata`.
    """
    providers = _library_provider_ids()
    if not providers:
        return {}
    rows = (
        db.query(
            MediaAvailability.provider,
            MediaAvailability.media_type,
            MediaAvailability.imdb_id,
            MediaAvailability.tmdb_id,
            MediaAvailability.added_at,
            MediaAvailability.resolution,
        )
        .filter(MediaAvailability.provider.in_(providers))
        .filter(
            (MediaAvailability.tmdb_id.isnot(None))
            | (MediaAvailability.imdb_id.isnot(None))
        )
        .all()
    )
    imdb_only = {
        (_kind(media_type), imdb)
        for _, media_type, imdb, tmdb, _, _ in rows
        if tmdb is None and imdb
    }
    imdb_map: dict[tuple[str, str], int] = {}
    if imdb_only:
        imdbs = sorted({imdb for _, imdb in imdb_only})
        for start in range(0, len(imdbs), 500):
            for media_type, imdb, tmdb in db.query(
                MediaMetadata.media_type, MediaMetadata.imdb_id, MediaMetadata.tmdb_id
            ).filter(MediaMetadata.imdb_id.in_(imdbs[start : start + 500])):
                imdb_map[(media_type, imdb)] = tmdb

    owned: dict[tuple[str, int], dict] = {}
    for provider, media_type, imdb, tmdb, added_at, resolution in rows:
        kind = _kind(media_type)
        if kind is None:
            continue
        tmdb_id = tmdb if tmdb is not None else imdb_map.get((kind, imdb))
        if tmdb_id is None:
            continue
        entry = owned.setdefault(
            (kind, int(tmdb_id)),
            {"added_at": None, "resolution": None, "providers": set()},
        )
        entry["providers"].add(provider)
        if added_at and (entry["added_at"] is None or added_at > entry["added_at"]):
            entry["added_at"] = added_at
        if resolution and _RES_RANK.get(resolution, -1) > _RES_RANK.get(
            entry["resolution"] or "", -1
        ):
            entry["resolution"] = resolution
    return owned


def library_fingerprint(db: Session) -> tuple:
    """Changes whenever what the servers own could change (for Home snapshots)."""
    providers = _library_provider_ids()
    if not providers:
        return ()
    count, latest = (
        db.query(
            func.count(MediaAvailability.id), func.max(MediaAvailability.updated_at)
        )
        .filter(MediaAvailability.provider.in_(providers))
        .first()
    ) or (0, None)
    return (tuple(sorted(providers)), int(count or 0), latest)


def _watched_keys(db: Session, user_id: int) -> set[tuple[str, int]]:
    return {
        (kind, tmdb)
        for kind, tmdb in db.query(WatchEvent.media_type, WatchEvent.tmdb_id)
        .filter(
            WatchEvent.user_id == user_id,
            WatchEvent.action.in_(TASTE_ACTIONS),
            WatchEvent.tmdb_id.isnot(None),
        )
        .distinct()
    }


def _sort_key(sort: Sort, entry: dict, meta: MediaMetadata):
    if sort == "title":
        return ((meta.title or "").casefold(),)
    if sort == "year":
        return (
            -(int((meta.release_date or "0")[:4] or 0)),
            (meta.title or "").casefold(),
        )
    if sort == "popular":
        return (-(meta.vote_count or 0), (meta.title or "").casefold())
    added = entry.get("added_at")
    # Newest first; titles the server gave no date for go last.
    return (
        0 if added else 1,
        -(added - datetime(1970, 1, 1)).total_seconds() if added else 0,
        (meta.title or "").casefold(),
    )


def owned_items(
    db: Session,
    *,
    kind: Optional[Kind] = None,
    sort: Sort = "added",
    offset: int = 0,
    limit: int = 40,
    unwatched_for: Optional[int] = None,
    min_resolution: Optional[str] = None,
    owned: Optional[dict] = None,
) -> tuple[list[dict], int]:
    """Owned titles as catalog cards, sorted and paged. Returns ``(items, total)``.

    Titles whose metadata has not been fetched yet are skipped; the sync's
    backfill adds them shortly after they arrive.
    """
    owned = owned if owned is not None else owned_entries(db)
    keys = [key for key in owned if kind is None or key[0] == kind]
    if unwatched_for is not None and keys:
        watched = _watched_keys(db, unwatched_for)
        keys = [key for key in keys if key not in watched]
    if min_resolution and keys:
        floor = _RES_RANK.get(min_resolution, 0)
        keys = [
            key
            for key in keys
            if _RES_RANK.get(owned[key].get("resolution") or "", -1) >= floor
        ]
    metadata: dict[tuple[str, int], MediaMetadata] = {}
    for start in range(0, len(keys), 500):
        for meta in db.query(MediaMetadata).filter(
            tuple_(MediaMetadata.media_type, MediaMetadata.tmdb_id).in_(
                keys[start : start + 500]
            )
        ):
            metadata[(meta.media_type, meta.tmdb_id)] = meta
    ready = [key for key in keys if key in metadata]
    ready.sort(key=lambda key: _sort_key(sort, owned[key], metadata[key]))
    page = ready[offset : offset + limit]
    items = []
    for key in page:
        content = SimpleNamespace(
            tmdb_id=key[1],
            media_type=key[0],
            rank=1,
            match_score=0,
            added_at=owned[key].get("added_at"),
        )
        item = CatalogGenerator._content_item(content, metadata[key])
        item["server_resolution"] = owned[key].get("resolution")
        items.append(item)
    return items, len(ready)


def other_items(
    db: Session, *, sort: Sort = "added", offset: int = 0, limit: int = 40
) -> tuple[list[dict], int]:
    """Server videos with no IMDb/TMDB match (home videos, unmatched files).

    Movie-type items only: each is one file, played by its server item key
    (``library_item``). Artwork comes from the media server through the
    signed ``/media/`` proxy, so ``poster`` is an absolute URL.
    """
    from app import media_proxy
    from app.sources import registry

    providers = {p.id: p for p in registry.enabled_providers() if p.kind == "library"}
    if not providers:
        return [], 0
    query = db.query(MediaAvailability).filter(
        MediaAvailability.provider.in_(list(providers)),
        MediaAvailability.media_type == "movie",
        MediaAvailability.tmdb_id.is_(None),
        MediaAvailability.imdb_id.is_(None),
    )
    total = query.count()
    if sort == "title":
        query = query.order_by(
            func.lower(MediaAvailability.title), MediaAvailability.id
        )
    elif sort == "year":
        query = query.order_by(
            MediaAvailability.year.desc().nullslast(), MediaAvailability.id
        )
    else:
        query = query.order_by(
            MediaAvailability.added_at.desc().nullslast(), MediaAvailability.id
        )
    items = []
    for row in query.offset(offset).limit(limit):
        provider = providers[row.provider]
        key = f"{row.provider}:{row.item_key}"
        poster = media_proxy.mint_url(row.provider, row.thumb) if row.thumb else None
        items.append(
            {
                "id": f"library:{key}",
                "library_item": key,
                "type": "movie",
                "media_type": "movie",
                "name": row.title or "Untitled",
                "title": row.title or "Untitled",
                "year": str(row.year) if row.year else "",
                "releaseInfo": str(row.year) if row.year else None,
                "poster": poster,
                "background": None,
                "description": None,
                "availability": {
                    "playable": True,
                    "in_library": True,
                    "sources": [
                        {
                            "provider_id": provider.id,
                            "label": provider.label,
                            "kind": "library",
                        }
                    ],
                },
            }
        )
    return items, total


def home_rows(db: Session, scope: str, owned: Optional[dict] = None) -> list[dict]:
    """Library rows for Home, in the shape ``build_membership`` uses.

    "Everything" gets one "On Your Servers" row (newest first). "My Servers"
    also gets movie and show rows, so a small library still fills Home.
    """
    owned = owned if owned is not None else owned_entries(db)
    if not owned:
        return []
    rows = []
    recent, _ = owned_items(db, sort="added", limit=ROW_LIMIT, owned=owned)
    if recent:
        rows.append(
            {
                "id": RECENT_ROW_ID,
                "name": RECENT_ROW_TITLE,
                "media_type": "mixed",
                "items": recent,
            }
        )
    if normalize_scope(scope) == "my_servers":
        for row_id, title, kind in (
            (MOVIES_ROW_ID, "Your Movies", "movie"),
            (SHOWS_ROW_ID, "Your Shows", "tv"),
        ):
            items, _ = owned_items(
                db, kind=kind, sort="popular", limit=ROW_LIMIT, owned=owned
            )
            if len(items) >= MY_SERVERS_MIN_ROW_ITEMS:
                rows.append(
                    {"id": row_id, "name": title, "media_type": kind, "items": items}
                )
    return rows


def compose_rows(db: Session, user, scope: str, owned: dict) -> list[dict]:
    """Every library-led row for Home, in display order.

    "On Your Servers" first, then rows picked from owned titles (Top Picks on
    Your Servers, Unwatched...), then "Your Movies" / "Your Shows".
    """
    from app import owned_rows

    library = home_rows(db, scope, owned)
    picked = owned_rows.build(db, user, owned, normalize_scope(scope))
    return library[:1] + picked + library[1:]


def filter_rows_to_owned(rows: Iterable[dict], owned: dict) -> list[dict]:
    """Keep only owned titles in each row (row order and item order kept)."""
    kept = []
    for row in rows:
        items = [
            item
            for item in row.get("items") or []
            if (_kind(item.get("media_type")), item.get("tmdb_id")) in owned
        ]
        if len(items) >= MY_SERVERS_MIN_ROW_ITEMS:
            kept.append({**row, "items": items})
    return kept


# --- after a sync ----------------------------------------------------------------
async def _resolve_imdb_only() -> int:
    """Give IMDb-only availability rows a TMDB id via TMDB ``/find``."""
    from app.config import settings
    from app.tmdb_client import tmdb_client

    if not settings.tmdb_api_key:
        return 0
    providers = _library_provider_ids()
    with get_db() as db:
        pending = (
            db.query(MediaAvailability.imdb_id, MediaAvailability.media_type)
            .filter(
                MediaAvailability.provider.in_(providers),
                MediaAvailability.tmdb_id.is_(None),
                MediaAvailability.imdb_id.isnot(None),
            )
            .distinct()
            .limit(MAX_IMDB_LOOKUPS_PER_RUN)
            .all()
        )
    resolved = 0
    for imdb_id, media_type in pending:
        try:
            found = await tmdb_client.find_by_imdb_id(imdb_id)
        except Exception as exc:
            logger.debug("TMDB find failed for {}: {}", imdb_id, exc)
            continue
        key = "movie_results" if _kind(media_type) == "movie" else "tv_results"
        results = (found or {}).get(key) or []
        if not results or not results[0].get("id"):
            continue
        with get_db() as db:
            db.query(MediaAvailability).filter(
                MediaAvailability.imdb_id == imdb_id,
                MediaAvailability.tmdb_id.is_(None),
            ).update(
                {MediaAvailability.tmdb_id: int(results[0]["id"])},
                synchronize_session=False,
            )
            db.commit()
        resolved += 1
    return resolved


async def prepare_owned_metadata() -> int:
    """Fetch card metadata for owned titles Curatio has never catalogued."""
    from app.config import settings

    if not settings.tmdb_api_key:
        return 0
    await _resolve_imdb_only()
    from workers.personalized_sync import _backfill_metadata

    with get_db() as db:
        providers = _library_provider_ids()
        ids: dict[str, set[int]] = {"movie": set(), "tv": set()}
        for media_type, tmdb in db.query(
            MediaAvailability.media_type, MediaAvailability.tmdb_id
        ).filter(
            MediaAvailability.provider.in_(providers),
            MediaAvailability.tmdb_id.isnot(None),
        ):
            kind = _kind(media_type)
            if kind:
                ids[kind].add(int(tmdb))
        filled = await _backfill_metadata(db, ids)
        db.commit()
    if filled:
        logger.info("Backfilled metadata for {} owned titles", filled)
    return filled


_tagging_task: "asyncio.Task | None" = None


async def queue_owned_tagging() -> None:
    """Start tagging new owned titles in the background (one run at a time)."""
    global _tagging_task
    if _tagging_task is not None and not _tagging_task.done():
        return
    from workers.library_tagging import tag_owned_titles

    async def run() -> None:
        try:
            result = await tag_owned_titles()
        except Exception as exc:  # never surfaces into a sync
            logger.warning("Owned-title tagging failed: {}", exc)
            return
        if result.get("processed"):
            invalidate_home()

    _tagging_task = asyncio.create_task(run())


def invalidate_home() -> None:
    """Rebuild every profile's Home snapshot and drop cached web dashboards."""
    from app.home_feed import invalidate_published_discovery

    invalidate_published_discovery()
    try:
        from app import main

        main._invalidate_dashboard_cache()
    except Exception as exc:  # cache eviction is best-effort
        logger.debug("dashboard cache eviction skipped: {}", exc)
