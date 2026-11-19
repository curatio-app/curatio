"""Single entry point for "how can this profile play this title right now?".

Fans a ``MediaRef`` out to every enabled provider, orders the results (library
sources first, then addons, degraded providers last), caches briefly, and
records titles nobody could supply.
"""

from __future__ import annotations

import asyncio
import copy
import time
from typing import Optional, cast

from loguru import logger
from sqlalchemy.orm import Session

from app import provider_health
from app.config import settings
from app.database import get_db
from app.models import MediaMetadata, SourceLookupMiss
from app.sources import registry
from app.sources.base import SourceProvider
from app.sources.models import (
    FailureReport,
    MediaRef,
    PlaybackContext,
    PlaybackSource,
)
from app.sources.sdk import http_client

PROVIDER_TIMEOUT_SECONDS = 20.0
_CACHE_TTL = 300
_CACHE_PARTIAL_TTL = 30
_CACHE_MAX = 500
_cache: dict[tuple, tuple[float, list[PlaybackSource], bool]] = {}


class ProvidersUnavailable(Exception):
    """Every enabled provider failed (as opposed to none having the title)."""


async def _db(fn, *args):
    def run():
        with get_db() as db:
            return fn(db, *args)

    return await asyncio.to_thread(run)


# --- identity completion ----------------------------------------------------
def _lookup_ids(db: Session, media: MediaRef) -> tuple[Optional[str], Optional[int]]:
    media_type = media.tmdb_media_type
    query = db.query(MediaMetadata.imdb_id, MediaMetadata.tmdb_id).filter(
        MediaMetadata.media_type == media_type
    )
    if media.imdb_id:
        row = query.filter(MediaMetadata.imdb_id == media.imdb_id).first()
    else:
        row = query.filter(MediaMetadata.tmdb_id == media.tmdb_id).first()
    if not row:
        return media.imdb_id, media.tmdb_id
    return media.imdb_id or row[0], media.tmdb_id or row[1]


async def _imdb_from_tmdb_api(tmdb_id: int, media_type: str) -> Optional[str]:
    if not settings.tmdb_api_key:
        return None
    url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/external_ids"
    try:
        resp = await http_client().get(
            url, params={"api_key": settings.tmdb_api_key}, timeout=10
        )
        if resp.status_code == 200:
            return cast(Optional[str], resp.json().get("imdb_id")) or None
    except Exception as exc:
        logger.warning("TMDB external id lookup failed for {}: {}", tmdb_id, exc)
    return None


async def complete_ids(media: MediaRef) -> MediaRef:
    """Fill in whichever of IMDb/TMDB id is missing, from the catalog first."""
    if media.imdb_id and media.tmdb_id:
        return media
    imdb_id, tmdb_id = await _db(_lookup_ids, media)
    if not imdb_id and tmdb_id:
        imdb_id = await _imdb_from_tmdb_api(tmdb_id, media.tmdb_media_type)
    return MediaRef(
        type=media.type,
        imdb_id=imdb_id,
        tmdb_id=tmdb_id,
        season=media.season,
        episode=media.episode,
    )


# --- ordering ----------------------------------------------------------------
_RES_RANK = {"2160p": 4, "1440p": 3, "1080p": 3, "720p": 2, "576p": 1, "480p": 1}


def _order(
    results: list[tuple[int, SourceProvider, list[PlaybackSource]]],
    degraded: set[str],
    quality: str,
) -> list[PlaybackSource]:
    ordered: list[tuple[tuple, PlaybackSource]] = []
    for provider_index, provider, sources in results:
        for source_index, source in enumerate(sources):
            preferred_variant = 0 if source.variant == quality else 1
            is_library = source.kind == "library"
            key = (
                0 if is_library else 1,
                1 if source.provider_id in degraded else 0,
                preferred_variant if is_library else 0,
                # The best copy wins across library servers (a 4K Jellyfin file
                # beats a 1080p Plex one); server order only breaks ties.
                # Addons keep their own ranking.
                -_RES_RANK.get(source.resolution or "", 0) if is_library else 0,
                provider_index,
                source_index,
            )
            ordered.append((key, source))
    ordered.sort(key=lambda item: item[0])
    return [source for _, source in ordered]


# --- bookkeeping ---------------------------------------------------------------
def _record_miss(db: Session, media: MediaRef, title: Optional[str]) -> None:
    try:
        key = media.key()[:80]
        row = db.get(SourceLookupMiss, key)
        if row is None:
            row = SourceLookupMiss(
                media_key=key,
                media_type=media.type,
                imdb_id=media.imdb_id,
                tmdb_id=media.tmdb_id,
                season=media.season,
                episode=media.episode,
                title=(title or None) and title[:500],
                miss_count=0,
            )
            db.add(row)
        row.miss_count = int(row.miss_count or 0) + 1
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("source lookup miss bookkeeping failed: {}", exc)


def clear_cache() -> None:
    _cache.clear()
    for provider in registry.all_providers():
        try:
            provider.clear_caches()
        except Exception:
            pass


def cache_stats() -> dict:
    return {"entries": len(_cache), "max": _CACHE_MAX}


# --- resolve ----------------------------------------------------------------
async def resolve(media: MediaRef, ctx: PlaybackContext) -> list[PlaybackSource]:
    """Ordered sources for ``media``. Raises ``ProvidersUnavailable`` only when
    every enabled provider errored."""
    diagnostics = ctx.diagnostics
    diagnostics.update(status="searching", cache_hit=False, errors=[])
    providers = registry.enabled_providers()
    if not providers:
        diagnostics["status"] = "not_configured"
        return []

    media = await complete_ids(media)
    hints = ctx.hints
    cache_key = (
        media.key(),
        ctx.quality,
        tuple(p.id for p in providers),
        # Transcode rungs are fitted to the device, so devices don't share them.
        (hints.max_bitrate_kbps, hints.max_resolution, hints.audio_channels),
    )
    now = time.time()
    hit = _cache.get(cache_key)
    if hit and not ctx.refresh:
        ttl = _CACHE_PARTIAL_TTL if hit[2] else _CACHE_TTL
        if now - hit[0] < ttl:
            diagnostics.update(
                status="partial" if hit[2] else ("ready" if hit[1] else "empty"),
                cache_hit=True,
            )
            return copy.deepcopy(hit[1])

    async def run(provider: SourceProvider) -> list[PlaybackSource]:
        sources = await asyncio.wait_for(
            provider.resolve(media, ctx), timeout=PROVIDER_TIMEOUT_SECONDS
        )
        clean: list[PlaybackSource] = []
        for source in sources or []:
            if not isinstance(source, PlaybackSource):
                continue
            if source.provider_id != provider.id and not source.provider_id.startswith(
                f"{provider.id}:"
            ):
                # Providers may sub-key (``addon:service``) but never impersonate.
                source = source.model_copy(update={"provider_id": provider.id})
            if provider.kind == "addon" and source.kind != "addon":
                source = source.model_copy(update={"kind": "addon"})
            clean.append(source)
        return clean

    raw = await asyncio.gather(*(run(p) for p in providers), return_exceptions=True)
    results: list[tuple[int, SourceProvider, list[PlaybackSource]]] = []
    errors: list[dict] = []
    for index, (provider, outcome) in enumerate(zip(providers, raw)):
        if isinstance(outcome, BaseException):
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            code = "timeout" if isinstance(outcome, asyncio.TimeoutError) else "error"
            errors.append({"provider_id": provider.id, "code": code})
            logger.warning(
                "Provider {} failed for {}: {}",
                provider.id,
                media.key(),
                type(outcome).__name__,
            )
            continue
        results.append((index, provider, outcome))

    if not results:
        diagnostics.update(status="failed", errors=errors)
        raise ProvidersUnavailable()

    degraded = await _db(provider_health.get_degraded_providers)
    sources = _order(results, degraded, ctx.quality)
    if not sources:
        await _db(_record_miss, media, ctx.title)

    if len(_cache) >= _CACHE_MAX:
        for stale in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
            _cache.pop(stale, None)
    _cache[cache_key] = (now, copy.deepcopy(sources), bool(errors))
    diagnostics.update(
        status="partial" if errors else ("ready" if sources else "empty"),
        errors=errors,
    )
    return sources


async def report_failure(report: FailureReport) -> None:
    provider_id = (report.provider_id or "").split(":", 1)[0]
    provider = registry.get(provider_id) if provider_id else None
    if report.provider_id:
        await _db(
            provider_health.record_failure,
            report.provider_id[:64],
            report.failure_category or (report.reason or "")[:64] or None,
        )
    if provider is not None:
        try:
            await provider.on_failure(report)
        except Exception as exc:
            logger.warning("Provider {} on_failure hook failed: {}", provider_id, exc)


async def resolve_library_item(
    library_item: str, ctx: PlaybackContext
) -> list[PlaybackSource]:
    """Sources for ``"<provider>:<item_key>"`` (an unmatched server video).

    Only library providers serve these; an addon never sees the request.
    """
    provider_id, _, item_key = (library_item or "").partition(":")
    provider = registry.get(provider_id) if item_key else None
    if provider is None or provider.kind != "library" or not provider.enabled():
        return []
    resolve_item = getattr(provider, "resolve_item", None)
    if resolve_item is None:
        return []
    ctx.diagnostics.update(status="searching", cache_hit=False, errors=[])
    sources = await resolve_item(item_key, ctx.quality, ctx.hints)
    ctx.diagnostics["status"] = "ready" if sources else "empty"
    return sources
