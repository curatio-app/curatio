"""Discovery rows picked from what a profile owns on its media servers.

Global discovery rows are built from the whole catalog, so filtering them down
to owned titles leaves almost nothing for a typical library. These rows rank
the library itself: the same taste vector, similarity and quality signals,
with the candidate set limited to owned titles before ranking.

Every item carries a short ``reason`` ("Because you watched Heat",
"Slow-Burn · Heist", "On your server · unwatched") that clients show on the
card and in Details.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Iterable, Optional

from loguru import logger
from sqlalchemy import func, tuple_
from sqlalchemy.orm import Session

from app.catalog_generator import CatalogGenerator
from app.models import (
    HiddenTitle,
    MediaMetadata,
    MovieTag,
    Tag,
    User,
    UserTasteVector,
    WatchEvent,
)
from app.server_history import SEED_ACTIONS, TASTE_ACTIONS
from app.time_utils import utc_now

#: Owned rows are smaller than catalog rows: a library is not a catalog.
OWNED_ROW_MIN_ITEMS = 8
OWNED_ROW_LIMIT = 60
#: "Because You Watched" rows built from the most recent watches.
MAX_BYW_ROWS = 2
#: Taste rows ("Slow-Burn Thrillers on Your Servers").
MAX_TASTE_ROWS = 3
SHORT_RUNTIME_MINUTES = 100
REWATCH_AFTER = timedelta(days=3 * 365)
REWATCH_MIN_RATING = 7.0
GEM_MIN_RATING = 7.0
GEM_MAX_VOTES = 3000
GEM_MIN_VOTES = 50
TASTE_ROW_ANCHOR_WEIGHT = 0.05

PICKS_ROW = "servers-picks"
UNWATCHED_ROW = "servers-unwatched"
SHORT_ROW = "servers-short"
REWATCH_ROW = "servers-rewatch"
GEMS_ROW = "servers-gems"
BYW_ROW = "servers-byw"
TASTE_ROW = "servers-taste"

#: Rows "Everything" scope adds after "On Your Servers".
EVERYTHING_ROWS = (f"{PICKS_ROW}-movie", UNWATCHED_ROW)

Key = tuple[str, int]


# --- shared lookups ---------------------------------------------------------------
def hidden_keys(db: Session, user_id: int) -> set[Key]:
    return {
        (media_type, int(tmdb_id))
        for media_type, tmdb_id in db.query(
            HiddenTitle.media_type, HiddenTitle.tmdb_id
        ).filter(HiddenTitle.user_id == user_id)
    }


def _watched(db: Session, user_id: int) -> dict[Key, object]:
    """{key: last watched} from every event kind that shapes taste."""
    watched: dict[Key, object] = {}
    for media_type, tmdb_id, last in (
        db.query(
            WatchEvent.media_type,
            WatchEvent.tmdb_id,
            func.max(WatchEvent.created_at),
        )
        .filter(
            WatchEvent.user_id == user_id,
            WatchEvent.action.in_(TASTE_ACTIONS),
            WatchEvent.tmdb_id.isnot(None),
        )
        .group_by(WatchEvent.media_type, WatchEvent.tmdb_id)
    ):
        watched[(media_type, int(tmdb_id))] = last
    return watched


def _metadata(db: Session, keys: Iterable[Key]) -> dict[Key, MediaMetadata]:
    keys = list(keys)
    found: dict[Key, MediaMetadata] = {}
    for start in range(0, len(keys), 500):
        for meta in db.query(MediaMetadata).filter(
            tuple_(MediaMetadata.media_type, MediaMetadata.tmdb_id).in_(
                keys[start : start + 500]
            )
        ):
            found[(meta.media_type, int(meta.tmdb_id))] = meta
    return found


def _taste_scores(db: Session, user_id: int, owned_ids: dict[str, list[int]]):
    """{key: score} for owned titles against the profile's taste vectors."""
    from app.relevance import taste_scored_candidates

    scores: dict[Key, float] = {}
    for media_type, ids in owned_ids.items():
        if not ids:
            continue
        for tmdb_id, score in taste_scored_candidates(
            db,
            user_id,
            media_type,
            limit=len(ids),
            quality_floor=False,
            only_ids=ids,
        ):
            scores[(media_type, tmdb_id)] = score
    return scores


def _top_tags(db: Session, user_id: int, key: Key, limit: int = 2) -> list[str]:
    """The title's strongest tags that are also in the profile's taste."""
    rows = (
        db.query(Tag.name)
        .join(MovieTag, MovieTag.tag_id == Tag.id)
        .join(
            UserTasteVector,
            (UserTasteVector.tag_id == MovieTag.tag_id)
            & (UserTasteVector.media_type == MovieTag.media_type),
        )
        .filter(
            UserTasteVector.user_id == user_id,
            MovieTag.media_type == key[0],
            MovieTag.tmdb_id == key[1],
            Tag.display.is_(True),
        )
        .order_by((UserTasteVector.weight * MovieTag.weight_norm).desc())
        .limit(limit)
        .all()
    )
    return [name for (name,) in rows]


def _card(key: Key, meta: MediaMetadata, owned: dict, reason: str | None) -> dict:
    content = SimpleNamespace(
        tmdb_id=key[1],
        media_type=key[0],
        rank=1,
        match_score=0,
        added_at=(owned.get(key) or {}).get("added_at"),
    )
    item = CatalogGenerator._content_item(content, meta)
    item["server_resolution"] = (owned.get(key) or {}).get("resolution")
    if reason:
        item["reason"] = reason
    return item


def _row(row_id: str, name: str, media_type: str, items: list[dict]) -> dict | None:
    if len(items) < OWNED_ROW_MIN_ITEMS:
        return None
    return {
        "id": row_id,
        "name": name,
        "media_type": media_type,
        "items": items[:OWNED_ROW_LIMIT],
    }


# --- rows --------------------------------------------------------------------------
class _Context:
    """Everything the row builders share, computed once per Home build."""

    def __init__(self, db: Session, user: User, owned: dict) -> None:
        self.db = db
        self.user = user
        self.owned = owned
        hidden = hidden_keys(db, user.id)
        self.keys: list[Key] = [key for key in owned if key not in hidden]
        self.meta = _metadata(db, self.keys)
        self.keys = [key for key in self.keys if key in self.meta]
        self.watched = _watched(db, user.id)
        self.unwatched = [key for key in self.keys if key not in self.watched]
        ids: dict[str, list[int]] = {"movie": [], "tv": []}
        for media_type, tmdb_id in self.keys:
            ids.setdefault(media_type, []).append(tmdb_id)
        self.ids = ids
        self.scores = _taste_scores(db, user.id, ids)
        self._reasons: dict[Key, str] = {}

    def taste_reason(self, key: Key) -> str | None:
        if key not in self._reasons:
            tags = _top_tags(self.db, self.user.id, key) if key in self.scores else []
            self._reasons[key] = " · ".join(tags)
        return self._reasons[key] or None

    def by_taste(self, keys: Iterable[Key]) -> list[Key]:
        """Taste score first, then rating and vote count (no taste yet)."""

        def order(key: Key):
            meta = self.meta[key]
            return (
                -self.scores.get(key, 0.0),
                -(meta.vote_average or 0.0),
                -(meta.vote_count or 0),
            )

        return sorted(keys, key=order)

    def cards(self, keys: Iterable[Key], reason) -> list[dict]:
        return [
            _card(key, self.meta[key], self.owned, reason(key) if reason else None)
            for key in keys
        ]


def _picks(ctx: _Context) -> list[dict]:
    rows = []
    names = {
        "movie": "Top Picks on Your Servers",
        "tv": "Shows For You on Your Servers",
    }
    for media_type in ("movie", "tv"):
        keys = [
            key
            for key in ctx.unwatched
            if key[0] == media_type and ctx.scores.get(key, 0) > 0
        ]
        row = _row(
            f"{PICKS_ROW}-{media_type}",
            names[media_type],
            media_type,
            ctx.cards(ctx.by_taste(keys)[:OWNED_ROW_LIMIT], ctx.taste_reason),
        )
        if row:
            rows.append(row)
    return rows


def _because_you_watched(ctx: _Context) -> list[dict]:
    from app.relevance import similar_by_vector

    seeds = (
        ctx.db.query(
            WatchEvent.tmdb_id,
            WatchEvent.media_type,
            func.max(WatchEvent.created_at).label("last"),
        )
        .filter(
            WatchEvent.user_id == ctx.user.id,
            WatchEvent.action.in_(SEED_ACTIONS),
            WatchEvent.tmdb_id.isnot(None),
        )
        .group_by(WatchEvent.tmdb_id, WatchEvent.media_type)
        .order_by(func.max(WatchEvent.created_at).desc())
        .limit(10)
        .all()
    )
    seed_meta = _metadata(ctx.db, [(m, int(t)) for t, m, _ in seeds])
    unwatched = set(ctx.unwatched)
    rows: list[dict] = []
    for tmdb_id, media_type, _ in seeds:
        if len(rows) >= MAX_BYW_ROWS:
            break
        seed = seed_meta.get((media_type, int(tmdb_id)))
        title = seed.title if seed else None
        if not title or not ctx.ids.get(media_type):
            continue
        similar = similar_by_vector(
            ctx.db,
            int(tmdb_id),
            media_type,
            limit=OWNED_ROW_LIMIT,
            quality_floor=False,
            only_ids=ctx.ids[media_type],
        )
        keys = [
            (media_type, other)
            for other, _ in similar
            if (media_type, other) in unwatched
        ]
        row = _row(
            f"{BYW_ROW}-{media_type}-{tmdb_id}",
            f"Because You Watched {title}",
            media_type,
            ctx.cards(keys, lambda _key, t=title: f"Because you watched {t}"),
        )
        if row:
            rows.append(row)
    return rows


def _unwatched(ctx: _Context) -> Optional[dict]:
    keys = ctx.by_taste(ctx.unwatched)
    return _row(
        UNWATCHED_ROW,
        "Unwatched on Your Servers",
        "mixed",
        ctx.cards(keys[:OWNED_ROW_LIMIT], lambda _key: "On your server · unwatched"),
    )


def _runtime(meta: MediaMetadata) -> Optional[int]:
    facts = meta.tag_facts if isinstance(meta.tag_facts, dict) else {}
    value = facts.get("runtime")
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _short_tonight(ctx: _Context) -> Optional[dict]:
    keys = []
    for key in ctx.unwatched:
        if key[0] != "movie":
            continue
        minutes = _runtime(ctx.meta[key])
        if minutes and minutes < SHORT_RUNTIME_MINUTES:
            keys.append(key)
    return _row(
        SHORT_ROW,
        "Short Tonight",
        "movie",
        ctx.cards(
            ctx.by_taste(keys)[:OWNED_ROW_LIMIT],
            lambda key: f"{_runtime(ctx.meta[key])} min · on your server",
        ),
    )


def _rewatch(ctx: _Context) -> Optional[dict]:
    cutoff = utc_now() - REWATCH_AFTER

    def last(key: Key):
        value = ctx.watched.get(key)
        return value.replace(tzinfo=None) if value is not None else None  # type: ignore[union-attr]

    keys = [
        key
        for key in ctx.keys
        if last(key) is not None
        and last(key) < cutoff
        and (ctx.meta[key].vote_average or 0) >= REWATCH_MIN_RATING
    ]
    keys.sort(key=lambda key: -(ctx.meta[key].vote_average or 0))
    return _row(
        REWATCH_ROW,
        "Haven't Watched in Years",
        "mixed",
        ctx.cards(
            keys[:OWNED_ROW_LIMIT],
            lambda key: f"Last watched {last(key).year}",  # type: ignore[union-attr]
        ),
    )


def _hidden_gems(ctx: _Context) -> Optional[dict]:
    keys = [
        key
        for key in ctx.unwatched
        if (ctx.meta[key].vote_average or 0) >= GEM_MIN_RATING
        and GEM_MIN_VOTES <= (ctx.meta[key].vote_count or 0) <= GEM_MAX_VOTES
    ]
    return _row(
        GEMS_ROW,
        "Hidden Gems You Own",
        "mixed",
        ctx.cards(
            ctx.by_taste(keys)[:OWNED_ROW_LIMIT],
            lambda key: f"Rated {ctx.meta[key].vote_average:.1f} · rarely seen",
        ),
    )


def _taste_rows(ctx: _Context) -> list[dict]:
    """Rows for the profile's strongest taste tags, from owned titles."""
    tags = (
        ctx.db.query(Tag.id, Tag.name, UserTasteVector.media_type)
        .join(UserTasteVector, UserTasteVector.tag_id == Tag.id)
        .filter(UserTasteVector.user_id == ctx.user.id, Tag.display.is_(True))
        .order_by(UserTasteVector.weight.desc())
        .limit(MAX_TASTE_ROWS * 4)
        .all()
    )
    unwatched = set(ctx.unwatched)
    rows: list[dict] = []
    used: set[Key] = set()
    for tag_id, name, media_type in tags:
        if len(rows) >= MAX_TASTE_ROWS or not ctx.ids.get(media_type):
            continue
        members = [
            (media_type, int(tmdb_id))
            for (tmdb_id,) in ctx.db.query(MovieTag.tmdb_id).filter(
                MovieTag.tag_id == tag_id,
                MovieTag.media_type == media_type,
                MovieTag.tmdb_id.in_(ctx.ids[media_type]),
                MovieTag.weight_norm >= TASTE_ROW_ANCHOR_WEIGHT,
            )
        ]
        keys = [key for key in ctx.by_taste(members) if key in unwatched]
        # Rows that mostly repeat an earlier taste row add nothing.
        if len(set(keys) - used) < OWNED_ROW_MIN_ITEMS:
            continue
        kind = "Movies" if media_type == "movie" else "Shows"
        row = _row(
            f"{TASTE_ROW}-{media_type}-{tag_id}",
            f"{name} {kind} on Your Servers",
            media_type,
            ctx.cards(keys[:OWNED_ROW_LIMIT], lambda _key, n=name: n),
        )
        if row:
            rows.append(row)
            used.update(keys)
    return rows


def build(db: Session, user: User, owned: dict, scope: str) -> list[dict]:
    """Owned discovery rows for Home, in display order.

    "my_servers" gets the full set; "everything" gets only Top Picks on Your
    Servers and Unwatched on Your Servers.
    """
    if not owned:
        return []
    try:
        ctx = _Context(db, user, owned)
        if not ctx.keys:
            return []
        rows: list[Optional[dict]] = []
        rows.extend(_picks(ctx))
        if scope == "my_servers":
            rows.extend(_because_you_watched(ctx))
            rows.append(_unwatched(ctx))
            rows.append(_short_tonight(ctx))
            rows.extend(_taste_rows(ctx))
            rows.append(_hidden_gems(ctx))
            rows.append(_rewatch(ctx))
        else:
            rows.append(_unwatched(ctx))
        built = [row for row in rows if row]
        if scope != "my_servers":
            built = [row for row in built if row["id"] in EVERYTHING_ROWS]
        return built
    except Exception as exc:  # a broken row must never take Home down
        logger.warning("Owned rows failed for profile {}: {}", user.id, exc)
        return []
