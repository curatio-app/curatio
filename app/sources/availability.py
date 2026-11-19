"""Answers "can this profile play this title?" for whole pages of cards at once.

Library ownership comes from one indexed query against ``media_availability``
(filled by the Plex/Jellyfin sync jobs). Searchable addons make every title
playable while they are enabled. Never touches the network.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import MediaAvailability
from app.sources import registry
from app.sources.models import Availability, AvailabilitySource

_IMDB_RE = re.compile(r"^tt\d{5,12}$")


def _norm_type(value: Any) -> Optional[str]:
    text = str(value or "").lower()
    if text in ("movie", "film"):
        return "movie"
    if text in ("series", "tv", "show"):
        return "series"
    return None


def _item_ids(item: dict) -> tuple[Optional[str], Optional[str], Optional[int]]:
    """(type, imdb_id, tmdb_id) from the many item shapes the API emits."""
    media_type = _norm_type(item.get("type") or item.get("media_type"))
    imdb = item.get("imdb_id") or item.get("imdbId")
    raw_id = str(item.get("id") or "")
    if not imdb and _IMDB_RE.match(raw_id.split(":")[0]):
        imdb = raw_id.split(":")[0]
    tmdb = item.get("tmdb_id") or item.get("tmdbId")
    if tmdb is None and raw_id.startswith("tmdb:"):
        tmdb = raw_id.split(":")[1]
    try:
        tmdb_int = int(tmdb) if tmdb not in (None, "") else None
    except (TypeError, ValueError):
        tmdb_int = None
    if imdb and not _IMDB_RE.match(str(imdb)):
        imdb = None
    return media_type, imdb, tmdb_int


class AvailabilityIndex:
    """Precomputed lookup for one request; build with ``for_items``."""

    def __init__(
        self,
        owned_imdb: dict[tuple[str, str], set[str]],
        owned_tmdb: dict[tuple[str, int], set[str]],
    ) -> None:
        self._imdb = owned_imdb
        self._tmdb = owned_tmdb
        enabled = registry.enabled_providers()
        self._labels = {p.id: p.label for p in enabled}
        self._library_ids = {p.id for p in enabled if p.kind == "library"}
        self._searchable = [
            AvailabilitySource(provider_id=p.id, label=p.label, kind="addon")
            for p in enabled
            if p.kind == "addon" and p.searchable
        ]

    def lookup(
        self, media_type: Optional[str], imdb_id: Optional[str], tmdb_id: Optional[int]
    ) -> Availability:
        owners: set[str] = set()
        types = [media_type] if media_type else ["movie", "series"]
        for kind in types:
            if imdb_id:
                owners |= self._imdb.get((kind, imdb_id), set())
            if tmdb_id is not None:
                owners |= self._tmdb.get((kind, tmdb_id), set())
        owners &= self._library_ids
        sources = [
            AvailabilitySource(
                provider_id=owner, label=self._labels.get(owner, owner), kind="library"
            )
            for owner in sorted(owners)
        ]
        sources.extend(self._searchable)
        return Availability(
            playable=bool(sources), in_library=bool(owners), sources=sources
        )

    def for_item(self, item: dict) -> Availability:
        return self.lookup(*_item_ids(item))

    @classmethod
    def build(
        cls, db: Session, keys: Iterable[tuple[Optional[str], Optional[int]]]
    ) -> "AvailabilityIndex":
        imdbs = {imdb for imdb, _ in keys if imdb}
        tmdbs = {tmdb for _, tmdb in keys if tmdb is not None}
        owned_imdb: dict[tuple[str, str], set[str]] = {}
        owned_tmdb: dict[tuple[str, int], set[str]] = {}
        if imdbs or tmdbs:
            clauses = []
            imdb_list, tmdb_list = sorted(imdbs), sorted(tmdbs)
            # Chunk to stay under SQLite's bound-parameter limit.
            for start in range(0, len(imdb_list), 500):
                clauses.append(
                    MediaAvailability.imdb_id.in_(imdb_list[start : start + 500])
                )
            for start in range(0, len(tmdb_list), 500):
                clauses.append(
                    MediaAvailability.tmdb_id.in_(tmdb_list[start : start + 500])
                )
            rows = (
                db.query(
                    MediaAvailability.provider,
                    MediaAvailability.media_type,
                    MediaAvailability.imdb_id,
                    MediaAvailability.tmdb_id,
                )
                .filter(or_(*clauses))
                .all()
            )
            for provider, media_type, imdb, tmdb in rows:
                if imdb:
                    owned_imdb.setdefault((media_type, imdb), set()).add(provider)
                if tmdb is not None:
                    owned_tmdb.setdefault((media_type, tmdb), set()).add(provider)
        return cls(owned_imdb, owned_tmdb)

    @classmethod
    def for_items(cls, db: Session, items: Iterable[dict]) -> "AvailabilityIndex":
        keys = []
        for item in items:
            if isinstance(item, dict):
                _, imdb, tmdb = _item_ids(item)
                keys.append((imdb, tmdb))
        return cls.build(db, keys)


def annotate(db: Session, items: list[dict], *, key: str = "availability") -> None:
    """Add ``availability`` to every dict in ``items`` (in place)."""
    dicts = [item for item in items if isinstance(item, dict)]
    if not dicts:
        return
    index = AvailabilityIndex.for_items(db, dicts)
    for item in dicts:
        item[key] = index.for_item(item).model_dump()


def annotate_tree(db: Session, payload: Any) -> Any:
    """Annotate every media-item-looking dict anywhere inside ``payload``.

    A media item is a dict with a ``type``/``media_type`` of movie/series/tv and
    an IMDb or TMDB identity. Used for nested payloads (bootstrap, home rows).
    """
    found: list[dict] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            media_type, imdb, tmdb = _item_ids(node)
            if media_type and (imdb or tmdb is not None) and "availability" not in node:
                found.append(node)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (dict, list)):
                    walk(value)

    walk(payload)
    if found:
        index = AvailabilityIndex.for_items(db, found)
        for item in found:
            item["availability"] = index.for_item(item).model_dump()
    return payload


def availability_revision(db: Session) -> str:
    """Changes whenever availability answers could change.

    Folded into the catalog revision/ETags so a cached home screen is
    revalidated after a library sync or an integration is switched on or off.
    """
    from sqlalchemy import func

    parts = [
        f"{p.id}:{int(bool(getattr(p, 'searchable', False)))}"
        for p in registry.enabled_providers()
    ]
    # Count and newest update as well as the generation: an incremental sync
    # adds titles without starting a new generation.
    for provider, generation, count, latest in (
        db.query(
            MediaAvailability.provider,
            func.max(MediaAvailability.sync_generation),
            func.count(MediaAvailability.id),
            func.max(MediaAvailability.updated_at),
        )
        .group_by(MediaAvailability.provider)
        .all()
    ):
        parts.append(f"{provider}@{generation}:{count}:{latest}")
    return "|".join(sorted(parts))
