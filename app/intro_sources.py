"""
Community intro and credits timestamps.

Two crowd-sourced databases cover far more episodes than the fingerprint learner,
which needs two decoded episodes of a season before it can say anything:

- IntroDB (introdb.app): GET /segments?imdb_id&season&episode. Seconds; a null
  segment with `no_<type>` set means the community confirmed there is none.
- TheIntroDB (theintrodb.org): GET /v4/media?imdb_id&season&episode&details=true.
  Milliseconds; a type with nothing known is left out, a null intro start means
  the episode's start and a null credits end means its end.

Both are read anonymously (an optional TheIntroDB key raises its limits). Each
source's answer is cached per episode so a replay costs nothing and usage stays
"reasonable" under both sites' terms.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Optional

import httpx
from loguru import logger
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models import ExternalSegmentCache
from app.time_utils import utc_now

INTRODB_URL = "https://api.introdb.app/segments"
THEINTRODB_URL = "https://api.theintrodb.org/v4/media"
SOURCES = ("introdb", "theintrodb")
REQUEST_TIMEOUT_SEC = 3.0
USER_AGENT = "Curatio"

HIT_TTL = timedelta(days=7)
NONE_TTL = timedelta(hours=24)
ERROR_TTL = timedelta(minutes=15)

INTRO_MIN_SEC = 5.0
INTRO_MAX_SEC = 300.0
INTRO_LATEST_START_SEC = 900.0
CREDITS_MIN_SEC = 5.0
CREDITS_MAX_SEC = 900.0

_IMDB_ID = re.compile(r"^tt\d{7,8}$")


def _seconds(value, scale: float = 1.0) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value) / scale
    except (TypeError, ValueError):
        return None


def _plausible(seg_type: str, start: float, end: float) -> bool:
    length = end - start
    if seg_type == "intro":
        return (
            start >= 0
            and start <= INTRO_LATEST_START_SEC
            and INTRO_MIN_SEC <= length <= INTRO_MAX_SEC
        )
    return start >= 0 and CREDITS_MIN_SEC <= length <= CREDITS_MAX_SEC


def _segment(seg_type, start, end, confidence, submissions, source) -> Optional[dict]:
    if start is None or end is None or not _plausible(seg_type, start, end):
        return None
    return {
        "type": seg_type,
        "start": round(start, 3),
        "end": round(end, 3),
        "confidence": _seconds(confidence) or 1.0,
        "submission_count": int(submissions or 0),
        "source": source,
    }


def parse_introdb(body: dict) -> list[dict]:
    segments = []
    for seg_type, key in (("intro", "intro"), ("credits", "outro")):
        entry = body.get(key)
        if not isinstance(entry, dict) or body.get(f"no_{key}"):
            continue
        segment = _segment(
            seg_type,
            _seconds(entry.get("start_sec")),
            _seconds(entry.get("end_sec")),
            entry.get("confidence"),
            entry.get("submission_count"),
            "introdb",
        )
        if segment:
            segments.append(segment)
    return segments


def parse_theintrodb(body: dict) -> list[dict]:
    duration = _seconds(body.get("group_duration_ms"), 1000.0)
    segments = []
    for seg_type in ("intro", "credits"):
        entries = [e for e in body.get(seg_type) or [] if isinstance(e, dict)]
        if not entries:
            continue
        entry = max(entries, key=lambda e: int(e.get("submission_count") or 0))
        start = _seconds(entry.get("start_ms"), 1000.0)
        end = _seconds(entry.get("end_ms"), 1000.0)
        # 0 marks "no segment"; null runs to the episode's start or end.
        if entry.get("start_ms") == 0 and entry.get("end_ms") == 0:
            continue
        if seg_type == "intro" and start is None:
            start = 0.0
        if seg_type == "credits" and end is None:
            end = duration
        segment = _segment(
            seg_type,
            start,
            end,
            entry.get("confidence"),
            entry.get("submission_count"),
            "theintrodb",
        )
        if segment:
            segments.append(segment)
    return segments


def _fetch(
    source: str, imdb_id: str, season: int, episode: int, client: httpx.Client
) -> tuple[str, list[dict]]:
    """Returns (status, segments) with status hit, none or error."""
    params = {"imdb_id": imdb_id, "season": season, "episode": episode}
    headers = {"User-Agent": USER_AGENT}
    if source == "introdb":
        url, parse = INTRODB_URL, parse_introdb
    else:
        url, parse = THEINTRODB_URL, parse_theintrodb
        params["details"] = "true"
        if settings.theintrodb_api_key:
            headers["Authorization"] = f"Bearer {settings.theintrodb_api_key}"
    try:
        response = client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        logger.info(
            f"community_segments_error source={source} error={type(exc).__name__}"
        )
        return "error", []
    if response.status_code == 404:
        return "none", []
    if response.status_code != 200:
        logger.info(
            f"community_segments_error source={source} status={response.status_code}"
        )
        return "error", []
    try:
        segments = parse(response.json())
    except ValueError:
        return "error", []
    return ("hit" if segments else "none"), segments


def _fresh(row: ExternalSegmentCache) -> bool:
    ttl = {"hit": HIT_TTL, "none": NONE_TTL}.get(row.status, ERROR_TTL)
    return row.fetched_at is not None and utc_now() - row.fetched_at < ttl


def _store(db, imdb_id, season, episode, source, status, segments) -> None:
    row = (
        db.query(ExternalSegmentCache)
        .filter_by(imdb_id=imdb_id, season=season, episode=episode, source=source)
        .first()
    )
    if row is None:
        row = ExternalSegmentCache(
            imdb_id=imdb_id, season=season, episode=episode, source=source
        )
        db.add(row)
    row.status = status
    row.segments = segments
    row.fetched_at = utc_now()
    try:
        db.commit()
    except IntegrityError:
        # Another request for the same episode stored it first.
        db.rollback()


def enabled_sources() -> tuple[str, ...]:
    flags = {
        "introdb": settings.introdb_enabled,
        "theintrodb": settings.theintrodb_enabled,
    }
    return tuple(source for source in SOURCES if flags[source])


def lookup(
    db, imdb_id: str, season: int, episode: int, client: Optional[httpx.Client] = None
) -> tuple[list[dict], bool]:
    """Every source's segments for the episode, from cache or fetched now, and
    whether all of them came from the cache."""
    sources = enabled_sources()
    if not sources or not _IMDB_ID.match(imdb_id or ""):
        return [], True
    rows = {
        row.source: row
        for row in db.query(ExternalSegmentCache)
        .filter_by(imdb_id=imdb_id, season=season, episode=episode)
        .all()
    }
    segments: list[dict] = []
    stale = []
    for source in sources:
        row = rows.get(source)
        if row is not None and _fresh(row):
            segments.extend(row.segments or [])
        else:
            stale.append(source)
    if not stale:
        return segments, True

    owns_client = client is None
    client = client or httpx.Client(timeout=REQUEST_TIMEOUT_SEC)
    try:
        with ThreadPoolExecutor(max_workers=len(stale)) as pool:
            results = list(
                pool.map(
                    lambda source: (
                        source,
                        *_fetch(source, imdb_id, season, episode, client),
                    ),
                    stale,
                )
            )
    finally:
        if owns_client:
            client.close()
    for source, status, fetched in results:
        _store(db, imdb_id, season, episode, source, status, fetched)
        segments.extend(fetched)
    return segments, False


def best_by_type(segments: list[dict]) -> dict[str, dict]:
    """The segment with the most submissions for each type; IntroDB wins a tie."""
    best: dict[str, dict] = {}
    for segment in segments:
        current = best.get(segment["type"])
        rank = (segment["submission_count"], segment["source"] == "introdb")
        if current is None or rank > (
            current["submission_count"],
            current["source"] == "introdb",
        ):
            best[segment["type"]] = segment
    return best
