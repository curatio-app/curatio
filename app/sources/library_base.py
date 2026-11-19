"""Shared plumbing for media-server providers (Plex, Jellyfin).

Each library provider stores one encrypted JSON config under
``integration_{id}_v1``, mirrors the server's titles into
``media_availability`` with a generation-swap sync, and serves playback through
signed ``/media/`` URLs.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, Iterable, Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy import func, or_

from app.database import get_db
from app.models import MediaAvailability
from app.sources.base import SourceProvider
from app.sources.models import MediaRef, PlaybackHints, PlaybackSource, TitleRef
from app.sources.sdk import (
    identity,
    read_secret_json,
    require_admin,
    write_secret_json,
)
from app.time_utils import utc_now

_CONFIG_TTL = 5.0
#: The full scan: finds removals and anything an incremental pass missed.
SYNC_INTERVAL_SECONDS = 6 * 3600
#: The incremental pass: only titles added since the newest one stored, so a
#: new download shows up on Home within minutes.
QUICK_SYNC_INTERVAL_SECONDS = 120
#: Look back this far past the newest stored addition (clock skew between the
#: media server and this one, items saved slightly out of order).
QUICK_SYNC_OVERLAP = timedelta(minutes=30)


class ServerTitle:
    """One title as reported by a media server during sync."""

    __slots__ = (
        "item_key",
        "media_type",
        "imdb_id",
        "tmdb_id",
        "tvdb_id",
        "title",
        "year",
        "added_at",
        "resolution",
        "thumb",
        "play_count",
        "watched_at",
    )

    def __init__(
        self,
        item_key: str,
        media_type: str,
        imdb_id: Optional[str] = None,
        tmdb_id: Optional[int] = None,
        tvdb_id: Optional[int] = None,
        title: Optional[str] = None,
        year: Optional[int] = None,
        added_at: Optional[datetime] = None,
        resolution: Optional[str] = None,
        thumb: Optional[str] = None,
        play_count: Optional[int] = None,
        watched_at: Optional[datetime] = None,
    ) -> None:
        self.item_key = item_key
        self.media_type = media_type
        self.imdb_id = imdb_id
        self.tmdb_id = tmdb_id
        self.tvdb_id = tvdb_id
        self.title = title
        self.year = year
        self.added_at = added_at
        self.resolution = resolution
        self.thumb = thumb
        self.play_count = play_count
        self.watched_at = watched_at


class TranscodeRung:
    """One transcode the media server is asked for (H.264 + AAC over HLS)."""

    __slots__ = ("resolution", "width", "height", "bitrate_kbps", "audio_channels")

    def __init__(self, resolution, width, height, bitrate_kbps, audio_channels):
        self.resolution = resolution
        self.width = width
        self.height = height
        self.bitrate_kbps = bitrate_kbps
        self.audio_channels = audio_channels

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TranscodeRung({self.resolution}, {self.bitrate_kbps}kbps, {self.audio_channels}ch)"


#: Transcode rungs, best first: (label, width, height, kbps). Transcodes stop
#: at 1080p: a 4K source plays as the original or not at all, because a 4K
#: software transcode would stall most home servers.
_LADDER = (
    ("1080p", 1920, 1080, 20_000),
    ("1080p", 1920, 1080, 12_000),
    ("1080p", 1920, 1080, 8_000),
    ("720p", 1280, 720, 4_000),
    ("720p", 1280, 720, 2_000),
)
#: Used when the device sent no bitrate: the old fixed rendition.
DEFAULT_TRANSCODE_KBPS = 8_000
_RES_HEIGHT = {
    "2160p": 2160,
    "1440p": 1440,
    "1080p": 1080,
    "720p": 720,
    "576p": 576,
    "480p": 480,
}


def transcode_ladder(
    hints: Optional[PlaybackHints], source_height: Optional[int]
) -> list[TranscodeRung]:
    """The transcodes to offer for one file: a primary fitted to the device's
    budget, plus one lighter rung for the player's lower-bitrate failover.

    The media server still decides per stream: with a budget above the file's
    own bitrate and resolution, Plex and Jellyfin copy the video (direct
    stream) and only convert what the device can't take.
    """
    hints = hints or PlaybackHints()
    budget = hints.max_bitrate_kbps or DEFAULT_TRANSCODE_KBPS
    cap = min(
        _RES_HEIGHT.get(hints.max_resolution or "", 1080),
        max(source_height or 1080, 480),
    )
    channels = 6 if (hints.audio_channels or 2) >= 6 else 2
    fitting = [
        rung
        for rung in _LADDER
        if rung[3] <= max(budget, _LADDER[-1][3]) and rung[2] <= max(cap, 720)
    ] or [_LADDER[-1]]
    primary = fitting[0]
    rungs = [primary]
    lighter = next(
        (r for r in fitting[1:] if r[2] < primary[2] or r[3] <= primary[3] // 2), None
    )
    if lighter:
        rungs.append(lighter)
    out = []
    for label, width, height, kbps in rungs:
        # Never ask for a frame larger than the file's own.
        if source_height and source_height < height:
            label = resolution_from_height(source_height) or label
            width, height = int(width * source_height / height), source_height
        out.append(TranscodeRung(label, width, height, kbps, channels))
    return out


class LibraryError(Exception):
    """A user-facing integration error (bad URL, bad credentials, offline)."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def epoch_to_datetime(value: Any) -> Optional[datetime]:
    """Plex ``addedAt`` (Unix seconds) as a naive UTC datetime."""
    seconds = int_or_none(value)
    if not seconds or seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)


def iso_to_datetime(value: Any) -> Optional[datetime]:
    """Jellyfin ``DateCreated`` (ISO 8601, up to 7 fractional digits)."""
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(ch for ch in tail if ch.isdigit())
        zone = tail[len(digits) :]
        text = f"{head}.{digits[:6]}{zone}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


#: A show counts as watched for taste once this many episodes were played.
SHOW_WATCHED_EPISODES = 3


def counts_as_watched(media_type: Optional[str], play_count: Optional[int]) -> bool:
    """Whether a server title's play count is strong enough to shape taste."""
    if not play_count:
        return False
    return play_count >= (1 if media_type == "movie" else SHOW_WATCHED_EPISODES)


def _apply_watch_state(row: Any, item: "ServerTitle") -> bool:
    """Copy reported watch state onto ``row``; True when it newly counts.

    A provider that did not report watch state (``play_count`` None) leaves
    the stored state alone.
    """
    if item.play_count is None:
        return False
    before = counts_as_watched(row.media_type, row.play_count)
    row.play_count = item.play_count
    row.watched_at = item.watched_at
    return not before and counts_as_watched(row.media_type, row.play_count)


def int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


_RES_LABEL = {"2160p": "4K", "1440p": "1440p", "1080p": "1080p", "720p": "720p"}
_HDR_LABEL = {
    "dv": "Dolby Vision",
    "hdr10plus": "HDR10+",
    "hdr10": "HDR10",
    "hlg": "HLG",
}
_CODEC_LABEL = {
    "hevc": "HEVC",
    "h264": "H.264",
    "av1": "AV1",
    "vp9": "VP9",
    "mpeg2": "MPEG-2",
}


def resolution_from_height(
    height: Optional[int], width: Optional[int] = None
) -> Optional[str]:
    if not height and not width:
        return None
    h, w = height or 0, width or 0
    if h >= 1600 or w >= 3200:
        return "2160p"
    if h >= 1000 or w >= 1800:
        return "1080p"
    if h >= 700 or w >= 1200:
        return "720p"
    if h >= 540:
        return "576p"
    return "480p"


def channels_label(channels: Optional[int]) -> Optional[str]:
    if not channels:
        return None
    return {1: "Mono", 2: "Stereo", 6: "5.1", 7: "6.1", 8: "7.1"}.get(
        channels, f"{channels}ch"
    )


def describe(
    *,
    resolution: Optional[str],
    video_codec: Optional[str],
    hdr: Optional[str],
    audio_channels: Optional[int],
    optimized: bool = False,
) -> str:
    parts = []
    if optimized:
        parts.append("Optimized")
    if resolution:
        parts.append(_RES_LABEL.get(resolution, resolution))
    if video_codec and not optimized:
        parts.append(_CODEC_LABEL.get(video_codec, video_codec.upper()))
    if hdr and not optimized:
        parts.append(_HDR_LABEL.get(hdr, hdr.upper()))
    ch = channels_label(audio_channels)
    if ch:
        parts.append(ch)
    return " · ".join(parts)


class LibraryProvider(SourceProvider):
    """Base for Plex/Jellyfin. Subclasses implement the server-specific calls."""

    kind = "library"
    #: query-string parameter names that carry credentials and must never be
    #: handed to a client (stripped from rewritten HLS playlists).
    media_secret_params: ClassVar[tuple[str, ...]] = ()

    def __init__(self) -> None:
        self._config_cache: tuple[float, Optional[dict]] = (0.0, None)
        self._config_lock = threading.Lock()
        self._sync_lock = asyncio.Lock()
        self._health = "ok"

    # --- config -------------------------------------------------------------
    @property
    def setting_key(self) -> str:
        return f"integration_{self.id}_v1"

    def config(self) -> Optional[dict]:
        now = time.monotonic()
        with self._config_lock:
            stamp, cached = self._config_cache
            if now - stamp < _CONFIG_TTL:
                return cached
        with get_db() as db:
            value = read_secret_json(db, self.setting_key)
        with self._config_lock:
            self._config_cache = (now, value)
        return value

    def save_config(self, value: Optional[dict]) -> None:
        with get_db() as db:
            write_secret_json(db, self.setting_key, value)
        with self._config_lock:
            self._config_cache = (0.0, None)

    def update_config(self, **changes: Any) -> dict:
        current = dict(self.config() or {})
        current.update(changes)
        self.save_config(current)
        return current

    def enabled(self) -> bool:
        cfg = self.config()
        return bool(
            cfg and cfg.get("enabled") and cfg.get("server_url") and cfg.get("token")
        )

    def health(self) -> str:
        if not self.enabled():
            return "offline"
        return self._health

    def clear_caches(self) -> None:
        with self._config_lock:
            self._config_cache = (0.0, None)

    # --- server-specific hooks (subclasses) ------------------------------------
    async def list_items(self, cfg: dict) -> Iterable[ServerTitle]:
        raise NotImplementedError

    async def sources_for(
        self,
        cfg: dict,
        row: MediaAvailability,
        media: MediaRef,
        quality: str,
        hints: Optional[PlaybackHints] = None,
    ) -> list[PlaybackSource]:
        raise NotImplementedError

    async def list_recent(
        self, cfg: dict, since: datetime
    ) -> Optional[list[ServerTitle]]:
        """Titles added at or after ``since`` (newest first), or None when the
        server cannot answer that cheaply (then only the full scan runs)."""
        return None

    async def list_recently_watched(
        self, cfg: dict, since: datetime
    ) -> Optional[list[ServerTitle]]:
        """Titles the signed-in account played at or after ``since``, with
        their watch state, or None when unsupported."""
        return None

    async def media_request(self, path: str) -> tuple[str, dict[str, str], str]:
        """``(absolute upstream URL, auth headers, server base URL)`` for ``path``."""
        cfg = self.config()
        if not cfg or not self.enabled():
            raise HTTPException(status_code=404, detail="Not found")
        if not path.startswith("/") or path.startswith("//"):
            raise HTTPException(status_code=404, detail="Not found")
        base = cfg["server_url"].rstrip("/")
        return base + path, self.auth_headers(cfg), base

    def auth_headers(self, cfg: dict) -> dict[str, str]:
        raise NotImplementedError

    # --- availability ---------------------------------------------------------
    def availability(self, refs: list[TitleRef]) -> dict[str, bool]:
        if not self.enabled() or not refs:
            return {}
        imdbs = [r.imdb_id for r in refs if r.imdb_id]
        tmdbs = [r.tmdb_id for r in refs if r.tmdb_id is not None]
        with get_db() as db:
            rows = (
                db.query(
                    MediaAvailability.media_type,
                    MediaAvailability.imdb_id,
                    MediaAvailability.tmdb_id,
                )
                .filter(MediaAvailability.provider == self.id)
                .filter(
                    or_(
                        MediaAvailability.imdb_id.in_(imdbs),
                        MediaAvailability.tmdb_id.in_(tmdbs),
                    )
                )
                .all()
            )
        owned = {(t, i) for t, i, _ in rows if i} | {(t, d) for t, _, d in rows if d}
        return {
            r.key(): (r.type, r.imdb_id) in owned or (r.type, r.tmdb_id) in owned
            for r in refs
        }

    def _find_row(self, media: MediaRef) -> Optional[MediaAvailability]:
        cfg = self.config() or {}
        with get_db() as db:
            query = db.query(MediaAvailability).filter(
                MediaAvailability.provider == self.id,
                MediaAvailability.server_id == cfg.get("server_id", ""),
                MediaAvailability.media_type == media.type,
            )
            clauses = []
            if media.imdb_id:
                clauses.append(MediaAvailability.imdb_id == media.imdb_id)
            if media.tmdb_id is not None:
                clauses.append(MediaAvailability.tmdb_id == media.tmdb_id)
            if not clauses:
                return None
            row = query.filter(or_(*clauses)).first()
            if row is not None:
                db.expunge(row)
            return row

    # --- playback -------------------------------------------------------------
    def _find_item(self, item_key: str) -> Optional[MediaAvailability]:
        cfg = self.config() or {}
        with get_db() as db:
            row = (
                db.query(MediaAvailability)
                .filter(
                    MediaAvailability.provider == self.id,
                    MediaAvailability.server_id == cfg.get("server_id", ""),
                    MediaAvailability.item_key == item_key,
                )
                .first()
            )
            if row is not None:
                db.expunge(row)
            return row

    async def resolve_item(
        self, item_key: str, quality: str, hints: Optional[PlaybackHints] = None
    ) -> list[PlaybackSource]:
        """Sources for one stored item by its server key (unmatched videos).

        Only single-file items (movie-type) are playable this way; a show has
        no one file to play.
        """
        cfg = self.config()
        if not cfg or not self.enabled():
            return []
        row = await asyncio.to_thread(self._find_item, item_key)
        if row is None or row.media_type != "movie":
            return []
        # Providers read only ``row.item_key`` for a movie; the ref is a
        # placeholder that satisfies the signature.
        placeholder = MediaRef(type="movie", tmdb_id=0)
        try:
            sources = await self.sources_for(cfg, row, placeholder, quality, hints)
            self._health = "ok"
            return sources
        except LibraryError:
            self._health = "degraded"
            raise

    async def resolve(self, media, ctx) -> list[PlaybackSource]:
        cfg = self.config()
        if not cfg or not self.enabled():
            return []
        row = await asyncio.to_thread(self._find_row, media)
        if row is None:
            return []
        try:
            sources = await self.sources_for(
                cfg, row, media, ctx.quality, getattr(ctx, "hints", None)
            )
            self._health = "ok"
            return sources
        except LibraryError:
            self._health = "degraded"
            raise

    def route_id(self, *parts: object) -> str:
        cfg = self.config() or {}
        return identity("route", self.id, cfg.get("server_id"), *parts)

    def file_id(self, *parts: object) -> str:
        cfg = self.config() or {}
        return identity("file", self.id, cfg.get("server_id"), *parts)

    def source_id(self, *parts: object) -> str:
        return "src_" + identity("source", self.id, *parts)[:24]

    # --- sync -----------------------------------------------------------------
    async def sync(self) -> dict:
        """Mirror the server's titles into ``media_availability``."""
        cfg = self.config()
        if not cfg or not self.enabled():
            return {"status": "skipped"}
        async with self._sync_lock:
            started = time.monotonic()
            try:
                items = list(await self.list_items(cfg))
            except Exception as exc:
                message = (
                    str(exc) if isinstance(exc, LibraryError) else type(exc).__name__
                )
                logger.warning("{} sync failed: {}", self.label, message)
                self._health = "degraded"
                self.update_config(
                    last_sync_error=message, last_sync_attempt=utc_now().isoformat()
                )
                return {"status": "failed", "error": message}
            counts = await asyncio.to_thread(self._store_items, cfg, items)
            self._health = "ok"
            self.update_config(
                last_sync_at=utc_now().isoformat(),
                last_sync_error=None,
                last_sync_counts=counts,
                last_sync_seconds=round(time.monotonic() - started, 1),
            )
            logger.info("{} sync stored {}", self.label, counts)
        if counts.get("added") or counts.get("removed"):
            await self.after_library_change()
        elif counts.get("newly_watched"):
            await self.after_watch_change()
        return {"status": "ok", **counts}

    async def after_watch_change(self) -> None:
        """Feed newly watched server titles into the mapped profiles' history."""
        from app import server_history

        try:
            await asyncio.to_thread(server_history.import_all)
        except Exception as exc:  # never fail a sync over history
            logger.warning("{} history import failed: {}", self.label, exc)

    async def after_library_change(self) -> None:
        """Make new arrivals presentable and show them on Home promptly."""
        from app import library_catalog

        try:
            await library_catalog.prepare_owned_metadata()
        except Exception as exc:  # never fail a sync over card metadata
            logger.warning("{} metadata backfill failed: {}", self.label, exc)
        from app.sources import resolver

        resolver.clear_cache()
        await asyncio.to_thread(library_catalog.invalidate_home)
        await self.after_watch_change()
        await library_catalog.queue_owned_tagging()

    def _store_items(self, cfg: dict, items: list[ServerTitle]) -> dict:
        server_id = cfg.get("server_id", "")
        with get_db() as db:
            current = (
                db.query(func.max(MediaAvailability.sync_generation))
                .filter(MediaAvailability.provider == self.id)
                .scalar()
            )
            generation = int(current or 0) + 1
            existing = {
                (r.server_id, r.item_key): r
                for r in db.query(MediaAvailability).filter(
                    MediaAvailability.provider == self.id,
                    MediaAvailability.server_id == server_id,
                )
            }
            seen: set[str] = set()
            added = movies = unmatched = newly_watched = 0
            for item in items:
                if item.item_key in seen or not item.item_key:
                    continue
                seen.add(item.item_key)
                if not (item.imdb_id or item.tmdb_id):
                    # Home videos, unmatched files: kept for the Library tab's
                    # "Other Videos" (played by item key, never by identity).
                    unmatched += 1
                elif item.media_type == "movie":
                    movies += 1
                row = existing.get((server_id, item.item_key))
                if row is None:
                    row = MediaAvailability(
                        provider=self.id, server_id=server_id, item_key=item.item_key
                    )
                    db.add(row)
                    added += 1
                row.media_type = item.media_type
                row.imdb_id = item.imdb_id
                row.tmdb_id = item.tmdb_id
                row.tvdb_id = item.tvdb_id
                row.title = (item.title or "")[:500] or None
                row.year = item.year
                row.added_at = item.added_at
                row.resolution = item.resolution
                row.thumb = (item.thumb or "")[:500] or None
                if _apply_watch_state(row, item):
                    newly_watched += 1
                row.sync_generation = generation
            db.flush()
            removed = (
                db.query(MediaAvailability)
                .filter(
                    MediaAvailability.provider == self.id,
                    MediaAvailability.sync_generation != generation,
                )
                .delete(synchronize_session=False)
            )
            db.commit()
        matched = len(seen) - unmatched
        return {
            "titles": matched,
            "movies": movies,
            "shows": matched - movies,
            "unmatched": unmatched,
            "added": added,
            "removed": removed,
            "newly_watched": newly_watched,
        }

    def background_jobs(self):
        return [
            (f"{self.id}-sync", SYNC_INTERVAL_SECONDS, self.sync),
            (f"{self.id}-quick-sync", QUICK_SYNC_INTERVAL_SECONDS, self.quick_sync),
        ]

    # --- incremental sync ---------------------------------------------------------
    def _newest_added(self) -> Optional[datetime]:
        with get_db() as db:
            return (
                db.query(func.max(MediaAvailability.added_at))
                .filter(MediaAvailability.provider == self.id)
                .scalar()
            )

    async def quick_sync(self) -> dict:
        """Store titles added since the newest one we know; never removes any.

        Runs every couple of minutes. Each web worker runs its own job loop,
        so a run recorded moments ago by another process is skipped.
        """
        cfg = self.config()
        if not cfg or not self.enabled() or not cfg.get("last_sync_at"):
            return {"status": "skipped"}  # the first full scan has not run yet
        if self._sync_lock.locked():
            return {"status": "skipped"}
        last = iso_to_datetime(cfg.get("last_quick_sync_at"))
        now = utc_now().replace(tzinfo=None)
        if last and (now - last).total_seconds() < QUICK_SYNC_INTERVAL_SECONDS / 2:
            return {"status": "skipped"}
        async with self._sync_lock:
            newest = await asyncio.to_thread(self._newest_added)
            since = (newest or now - timedelta(days=1)) - QUICK_SYNC_OVERLAP
            try:
                items = await self.list_recent(cfg, since)
            except Exception as exc:
                logger.info("{} quick sync failed: {}", self.label, exc)
                return {"status": "failed"}
            watched_since = (last or now - timedelta(days=1)) - QUICK_SYNC_OVERLAP
            try:
                watched = await self.list_recently_watched(cfg, watched_since)
            except Exception as exc:
                logger.info("{} watched-state check failed: {}", self.label, exc)
                watched = None
            self.update_config(last_quick_sync_at=now.isoformat())
            items = list(items or []) + list(watched or [])
            if not items:
                return {"status": "ok", "added": 0}
            counts = await asyncio.to_thread(self._upsert_items, cfg, items)
        if counts["added"]:
            logger.info("{} quick sync added {}", self.label, counts["added"])
            await self.after_library_change()
        elif counts["newly_watched"]:
            await self.after_watch_change()
        return {"status": "ok", **counts}

    def _upsert_items(self, cfg: dict, items: list[ServerTitle]) -> dict:
        """Add or refresh titles in the current generation (no deletions)."""
        server_id = cfg.get("server_id", "")
        added = newly_watched = 0
        with get_db() as db:
            generation = int(
                db.query(func.max(MediaAvailability.sync_generation))
                .filter(MediaAvailability.provider == self.id)
                .scalar()
                or 0
            )
            keys = [item.item_key for item in items]
            existing = {
                r.item_key: r
                for r in db.query(MediaAvailability).filter(
                    MediaAvailability.provider == self.id,
                    MediaAvailability.server_id == server_id,
                    MediaAvailability.item_key.in_(keys),
                )
            }
            seen: set[str] = set()
            for item in items:
                if item.item_key in seen or not item.item_key:
                    continue
                seen.add(item.item_key)
                row = existing.get(item.item_key)
                if row is None:
                    row = MediaAvailability(
                        provider=self.id,
                        server_id=server_id,
                        item_key=item.item_key,
                        sync_generation=generation,
                    )
                    db.add(row)
                    added += 1
                row.media_type = item.media_type
                row.imdb_id = item.imdb_id
                row.tmdb_id = item.tmdb_id
                row.tvdb_id = item.tvdb_id
                row.title = (item.title or "")[:500] or None
                row.year = item.year
                row.added_at = item.added_at
                row.resolution = item.resolution
                row.thumb = (item.thumb or "")[:500] or None
                if _apply_watch_state(row, item):
                    newly_watched += 1
            db.commit()
        return {"titles": len(seen), "added": added, "newly_watched": newly_watched}

    def forget_library(self) -> None:
        with get_db() as db:
            db.query(MediaAvailability).filter(
                MediaAvailability.provider == self.id
            ).delete(synchronize_session=False)
            db.commit()

    # --- admin ----------------------------------------------------------------
    def public_config(self) -> dict:
        cfg = self.config() or {}
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "connected": bool(cfg.get("server_url") and cfg.get("token")),
            "enabled": self.enabled(),
            "health": self.health(),
            "server_name": cfg.get("server_name"),
            "server_url": cfg.get("server_url"),
            "last_sync_at": cfg.get("last_sync_at"),
            "last_sync_error": cfg.get("last_sync_error"),
            "last_sync_counts": cfg.get("last_sync_counts"),
        }

    def admin_summary(self) -> dict:
        return self.public_config()

    def base_admin_router(self) -> APIRouter:
        router = APIRouter(dependencies=[Depends(require_admin)])

        @router.get("")
        async def status():
            return self.public_config()

        @router.post("/sync")
        async def sync_now():
            if not self.enabled():
                raise HTTPException(
                    status_code=400, detail=f"{self.label} is not connected"
                )
            result = await self.sync()
            if result.get("status") == "failed":
                raise HTTPException(status_code=502, detail=result.get("error"))
            return {**self.public_config(), "result": result}

        @router.post("/enabled")
        async def set_enabled(body: dict):
            if not (self.config() or {}).get("token"):
                raise HTTPException(
                    status_code=400, detail=f"{self.label} is not connected"
                )
            self.update_config(enabled=bool(body.get("enabled")))
            from app.sources import resolver

            resolver.clear_cache()
            return self.public_config()

        @router.post("/disconnect")
        async def disconnect():
            self.save_config(None)
            await asyncio.to_thread(self.forget_library)
            from app.sources import resolver

            resolver.clear_cache()
            return self.public_config()

        return router
