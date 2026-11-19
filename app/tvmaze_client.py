"""
TVmaze client -- the release world's season numbering, without an API key.

Our catalog numbers seasons the way TMDB does. Release groups number them the way
TheTVDB does. Where the two disagree the upstream stream lookup returns an empty list
rather than an error, so the failure is invisible: Kitchen Nightmares' 2023 revival is
seasons 1-3 of ``tt32436778`` to us and seasons 8-10 of ``tt0983514`` to every scraper,
and asking for our own numbering can never match anything.

TVmaze is the practical bridge. It is free and keyless, it carries TVDB-style numbering,
and it exposes the IMDb id that indexers key on -- so "which season does the rest of the
world think this is" becomes a lookup instead of a guess. Nothing here interprets the
answer; ``app.series_alias`` does the reconciliation.

Synchronous (``httpx.Client``), since the reconciliation sweep runs in a worker
thread. Async callers must offload, e.g.
``await asyncio.to_thread(tvmaze_client.get_episodes, show_id)``. Never raises: every
entry point returns an empty result on any error, because a missing alias is only a
missed optimisation while an exception here would break a stream lookup that was
already working.
"""

import threading
import time
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import quote

import httpx
from loguru import logger

TVMAZE_BASE = "https://api.tvmaze.com"
TVMAZE_TIMEOUT = 8.0

# TVmaze asks for at most ~20 calls per 10 seconds from anonymous clients. The sweep
# walks a whole catalog, so pace every call through one process-wide gate rather than
# relying on each caller to be polite.
_MIN_CALL_INTERVAL = 0.55
_rate_lock = threading.Lock()
_last_call_at = 0.0


@dataclass(frozen=True)
class TvmazeShow:
    """A TVmaze series with the external ids indexers care about."""

    id: int
    name: str
    premiered: str
    imdb_id: Optional[str]
    tvdb_id: Optional[int]


@dataclass(frozen=True)
class TvmazeEpisode:
    """One episode in TVmaze's (TVDB-style) numbering."""

    season: int
    number: int
    name: str
    airdate: str


def _throttle() -> None:
    global _last_call_at
    with _rate_lock:
        wait = _last_call_at + _MIN_CALL_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


def _get(path: str) -> Optional[object]:
    """GET a TVmaze path, returning parsed JSON or None on any failure."""
    _throttle()
    try:
        with httpx.Client(timeout=TVMAZE_TIMEOUT) as client:
            resp = client.get(f"{TVMAZE_BASE}{path}")
        if resp.status_code == 404:
            # TVmaze uses 404 for "no such show", which is an answer, not an error.
            return None
        if resp.status_code != 200:
            logger.warning(f"tvmaze {path} returned {resp.status_code}")
            return None
        return resp.json()
    except Exception as exc:
        logger.warning(f"tvmaze {path} failed: {exc}")
        return None


def _to_show(raw: dict) -> Optional[TvmazeShow]:
    show_id = raw.get("id")
    if not isinstance(show_id, int):
        return None
    externals = raw.get("externals") or {}
    tvdb = externals.get("thetvdb")
    return TvmazeShow(
        id=show_id,
        name=str(raw.get("name") or ""),
        premiered=str(raw.get("premiered") or ""),
        imdb_id=externals.get("imdb") or None,
        tvdb_id=tvdb if isinstance(tvdb, int) else None,
    )


def lookup_by_imdb(imdb_id: str) -> Optional[TvmazeShow]:
    """Find a show by IMDb id. Often ``None`` for the very titles we care about.

    A split revival is exactly the case TVmaze has not indexed under its new IMDb id --
    the episodes live under the original show -- so callers must fall back to
    ``search_shows`` rather than treating a miss here as "no mapping exists".
    """
    raw = _get(f"/lookup/shows?imdb={quote(imdb_id)}")
    return _to_show(raw) if isinstance(raw, dict) else None


def search_shows(title: str, limit: int = 5) -> List[TvmazeShow]:
    """Search shows by title, best match first."""
    raw = _get(f"/search/shows?q={quote(title)}")
    if not isinstance(raw, list):
        return []
    shows = []
    for entry in raw[:limit]:
        if not isinstance(entry, dict):
            continue
        show = _to_show(entry.get("show") or {})
        if show:
            shows.append(show)
    return shows


def get_episodes(show_id: int) -> List[TvmazeEpisode]:
    """Every regular episode of a show, in TVmaze's season/number numbering."""
    raw = _get(f"/shows/{show_id}/episodes")
    if not isinstance(raw, list):
        return []
    episodes = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        season = entry.get("season")
        number = entry.get("number")
        # ``number`` is null for specials; they have no stable numbering to map onto.
        if not isinstance(season, int) or not isinstance(number, int):
            continue
        episodes.append(
            TvmazeEpisode(
                season=season,
                number=number,
                name=str(entry.get("name") or ""),
                airdate=str(entry.get("airdate") or ""),
            )
        )
    return episodes
