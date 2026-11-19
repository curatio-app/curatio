"""
MDBList client — multi-source ratings, per-title facts, and curated lists.

MDBList aggregates IMDb, TMDb, Trakt, Letterboxd, Metacritic, Rotten Tomatoes,
Roger Ebert and MyAnimeList, and exposes per-title keywords, certification and
age-rating data. That is a materially better quality signal than TMDB's
``vote_average`` alone, which is compressed around 6.5-7.5 and does almost no
ranking work.

**Optional by construction.** The API key lives in ``admin_settings`` under
``mdblist_api_key``, like other operator secrets -- not in the environment,
so no existing install breaks on upgrade and ``app/config.py``'s required-settings
list is untouched. With no key every method here returns empty and the callers
fall back to TMDB. This client never raises into a caller: an MDBList outage must
degrade the library, not fail a build.

**Quota is discovered, never assumed.** The ceiling comes from response headers
and ``/user``, persisted in ``mdb_quota``. The same code runs on a free
1,000/day key and a paid 500,000/day one, which is what makes "pay for one month,
then downgrade" a safe plan rather than a cliff.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import httpx
from loguru import logger
from sqlalchemy.orm import Session

from app.time_utils import utc_now  # noqa: E402
from app.config import settings
from app.models import AdminSetting, MdbQuota
from app.rate_limit import RateLimiter

BASE_URL = "https://api.mdblist.com"

#: Consecutive 429s before the client gives up for the day.
#:
#: A 429 is not proof of quota exhaustion. The first full run tripped one after
#: ~8,184 requests against a 500,000/day key -- 1.6% of quota -- because the
#: enricher fired ``mdblist_concurrency`` requests wide with no pacing at all.
#: Treating that as terminal ended the run at 2.4% of the library and reported
#: it as a clean finish. A rate limit is transient by definition, so back off
#: and retry; only a run of them, or a genuinely spent quota, stops the day.
MAX_RATE_LIMIT_RETRIES = 4

#: Optional blocks the media endpoints will append when asked. ``review`` is the
#: one this integration exists for -- critic and user prose is by far the richest
#: source for mood, theme and style, the facets a plot synopsis supports worst.
#: ``extra`` carries certification and age-rating fields; ``keyword`` feeds the
#: tagging prompt directly. ``recommendations`` is deliberately omitted: nothing
#: reads it and it is pure payload weight.
APPEND_FIELDS_LIST = ("extra", "keyword", "review")
APPEND_FIELDS = ",".join(APPEND_FIELDS_LIST)

#: Hard ceiling on ids per batch request: the API documents 10 for a regular key
#: and 100 for a supporter, so no request should ever exceed the higher of the
#: two whatever the setting says.
MAX_BATCH_IDS = 100

#: What a regular (non-supporter) key accepts. A batch at or below this size can
#: never have been rejected *for its size*, so an empty result at this size means
#: MDBList has no record of the ids -- not that the request was too big.
REGULAR_KEY_BATCH_IDS = 10


def _as_item_list(payload: Any) -> list[dict]:
    """Normalise a batch response to a list of dicts."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        # Tolerate an envelope rather than a bare array.
        for key in ("results", "items", "data"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [item for item in inner if isinstance(item, dict)]
    return []


def _MEDIA_TYPE(media_type: str) -> str:
    """Our ``movie``/``tv`` to MDBList's ``movie``/``show``."""
    return "show" if media_type == "tv" else "movie"


API_KEY_SETTING = "mdblist_api_key"

#: Floor under the daily reserve. A flat reserve is fine against 500,000/day and
#: badly wrong against 1,000, so the reserve is a percentage with this floor.
RESERVE_FLOOR = 50

#: Per-source scales for ``value``, used ONLY when ``score`` is absent.
#:
#: These were guessed, and a live probe showed four of nine were wrong. MDBList
#: reports ``trakt`` and ``tmdb`` as percentages (87, 82), not out of ten, so the
#: old mapping clamped both to a perfect 10.0. ``letterboxd`` arrives already
#: doubled onto 0-10 (8.4 for a 4.2/5 film), so treating it as 0-5 clamped that
#: to 10.0 as well. ``rogerebert`` is out of four stars (3.0), not ten.
#: Consensus therefore skewed high across the library -- which would have made
#: the whole "better quality signal than TMDB" premise false.
_PERCENT_SOURCES = {
    "tomatoes",
    "tomatoesaudience",
    "popcorn",
    "metacritic",
    "trakt",
    "tmdb",
}
#: Sources reported out of ten.
_TEN_SOURCES = {"imdb", "metacriticuser", "myanimelist", "letterboxd"}
#: Roger Ebert is out of four stars.
_FOUR_SOURCES = {"rogerebert"}


def normalize_rating(source: str, value: Any) -> Optional[float]:
    """Map one source's ``value`` onto a common 0-10 scale.

    Fallback only -- prefer :func:`normalize_score`, which needs no per-source
    knowledge at all. This exists for the sources that report ``score: null``
    while still carrying a usable ``value``.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number < 0:  # NaN or negative
        return None

    key = (source or "").strip().lower()
    if key in _PERCENT_SOURCES:
        scaled = number / 10.0
    elif key in _FOUR_SOURCES:
        scaled = number * 2.5
    elif key in _TEN_SOURCES:
        scaled = number
    else:
        # Unknown source: infer from magnitude rather than dropping the signal.
        scaled = number / 10.0 if number > 10 else number
    return max(0.0, min(10.0, scaled))


def normalize_score(score: Any) -> Optional[float]:
    """Map MDBList's ``score`` onto 0-10. No per-source knowledge required.

    Every rating entry carries a ``score`` already normalised to 0-100 by
    MDBList itself, whatever scale that source publishes in ``value``::

        imdb  value 8.7  score 87      trakt      value 93  score 93
        metacritic  73         73      letterboxd value 8.4 score 84

    Deriving from this deletes an entire class of bug rather than patching it:
    the per-source table above only has to be right for the rare entry where
    ``score`` is null.
    """
    try:
        number = float(score)
    except (TypeError, ValueError):
        return None
    if number != number or number < 0:  # NaN or negative
        return None
    return max(0.0, min(10.0, number / 10.0))


def consensus(
    normalized: list[tuple[float, Optional[int]]],
    min_sources: Optional[int] = None,
) -> tuple[Optional[float], int]:
    """Votes-weighted mean of normalized ratings.

    Returns ``(consensus_or_None, source_count)``. None below the source floor,
    so a single stray rating never overrides the TMDB fallback.
    """
    floor = settings.mdblist_min_rating_sources if min_sources is None else min_sources
    usable = [(v, w) for v, w in normalized if v is not None]
    if len(usable) < floor:
        return None, len(usable)
    # Vote counts span orders of magnitude (IMDb millions vs Metacritic dozens),
    # so weight by a dampened count rather than the raw number, which would let
    # IMDb alone decide every score.
    total_w = 0.0
    total = 0.0
    for value, votes in usable:
        weight = 1.0 + min(4.0, (max(0, votes or 0)) ** 0.25)
        total += value * weight
        total_w += weight
    return (round(total / total_w, 3) if total_w else None), len(usable)


def get_api_key(db: Session) -> Optional[str]:
    """Read the MDBList key from admin_settings, or None if unset."""
    row = db.query(AdminSetting).filter(AdminSetting.key == API_KEY_SETTING).first()
    value = (row.value or "").strip() if row else ""
    return value or None


def set_api_key(db: Session, key: Optional[str]) -> None:
    """Store or clear the MDBList key."""
    row = db.query(AdminSetting).filter(AdminSetting.key == API_KEY_SETTING).first()
    if row is None:
        row = AdminSetting(key=API_KEY_SETTING)
        db.add(row)
    row.value = (key or "").strip()
    row.updated_at = utc_now()
    db.commit()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class QuotaExhausted(Exception):
    """Raised internally when the discovered daily ceiling is reached."""


class MDBListClient:
    """Thin, never-raising MDBList client with persisted quota accounting."""

    def __init__(self, api_key: Optional[str], db: Optional[Session] = None):
        self.api_key = api_key
        self.db = db
        self._client: Optional[httpx.AsyncClient] = None
        self._limit_seen: Optional[int] = None
        self._used_today = 0
        self._exhausted = False
        self._batch_cap_warned = False
        # Concurrency bounds requests in flight; only this bounds the *rate*.
        self._limiter = RateLimiter(settings.mdblist_rps)
        self._shape_logged = False
        #: Batch id cap, tightened from /user. 10 is the documented regular-key
        #: limit and the safe assumption until a supporter key proves otherwise.
        self.max_batch_ids = 10
        self.is_supporter = False

    # -- lifecycle ---------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(settings.mdblist_enabled and self.api_key)

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=BASE_URL,
                timeout=httpx.Timeout(30.0),
                headers={"Accept": "application/json"},
                # The documented paths end in a trailing slash. Requesting one
                # without it draws a 307, which is not >= 400 so it slips past
                # the error check and then dies on .json() -- surfacing as a
                # silent None indistinguishable from "no such title".
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "MDBListClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # -- quota -------------------------------------------------------------
    def load_quota(self) -> None:
        """Restore today's usage so a restart does not walk into a 429."""
        if self.db is None:
            return
        row = self.db.query(MdbQuota).filter(MdbQuota.day == _today()).first()
        if row:
            self._used_today = int(row.used or 0)
            self._limit_seen = row.limit_seen

    def _persist_quota(self) -> None:
        if self.db is None:
            return
        try:
            day = _today()
            row = self.db.query(MdbQuota).filter(MdbQuota.day == day).first()
            if row is None:
                row = MdbQuota(day=day)
                self.db.add(row)
            row.used = self._used_today
            row.limit_seen = self._limit_seen
            row.updated_at = utc_now()
            self.db.commit()
        except Exception as exc:  # noqa: BLE001 - accounting must never break a run
            logger.debug(f"mdblist: could not persist quota: {exc}")
            self.db.rollback()

    @property
    def remaining(self) -> Optional[int]:
        """Requests left today after the reserve, or None if the limit is unknown."""
        if self._limit_seen is None:
            return None
        reserve = max(
            RESERVE_FLOOR, int(self._limit_seen * settings.mdblist_daily_reserve_pct)
        )
        return max(0, self._limit_seen - reserve - self._used_today)

    def _note_headers(self, response: httpx.Response) -> None:
        for header in ("X-RateLimit-Limit", "x-ratelimit-limit", "X-RateLimit-Daily"):
            raw = response.headers.get(header)
            if raw:
                try:
                    self._limit_seen = int(raw)
                    break
                except ValueError:
                    pass

    # -- requests ----------------------------------------------------------
    def _blocked(self) -> bool:
        """True when no request should be attempted."""
        if not self.enabled or self._exhausted:
            return True
        if self.remaining is not None and self.remaining <= 0:
            self._exhausted = True
            logger.warning("mdblist: daily quota reserve reached, stopping cleanly")
            return True
        return False

    def _stop_for_today(self, reason: str) -> None:
        self._exhausted = True
        self._persist_quota()
        logger.warning(f"mdblist: {reason}")

    async def _send(self, label: str, call) -> Optional[Any]:
        """Pace one request, retry rate limits, and never raise into a caller.

        ``call`` is a zero-argument coroutine function returning the response,
        so :meth:`_get` and :meth:`_post` keep their own httpx call shapes while
        sharing the pacing, quota accounting and 429 policy.
        """
        if self._blocked():
            return None

        delay = 2.0
        for attempt in range(1, MAX_RATE_LIMIT_RETRIES + 1):
            await self._limiter.wait()
            try:
                response = await call()
            except Exception as exc:  # noqa: BLE001 - degrade, never fail the build
                logger.warning(f"mdblist {label}: {type(exc).__name__}: {exc}")
                return None

            self._used_today += 1
            self._note_headers(response)

            if response.status_code == 429:
                spent = self.remaining is not None and self.remaining <= 0
                if spent:
                    self._stop_for_today(
                        "429 with the daily quota spent — stopping cleanly, "
                        "resume tomorrow"
                    )
                    return None
                if attempt == MAX_RATE_LIMIT_RETRIES:
                    seen = f" of {self._limit_seen:,}" if self._limit_seen else ""
                    self._stop_for_today(
                        f"{attempt} consecutive 429s on {label} after "
                        f"{self._used_today:,} requests today{seen} — stopping. "
                        "Well below the key's limit means a RATE limit rather "
                        "than a quota: lower mdblist_rps."
                    )
                    return None
                wait = delay + random.uniform(0, delay / 2)
                logger.warning(
                    f"mdblist: rate limited on {label} (attempt {attempt}/"
                    f"{MAX_RATE_LIMIT_RETRIES}); retrying in {wait:.1f}s"
                )
                await asyncio.sleep(wait)
                delay *= 2
                continue

            if response.status_code >= 400:
                # WARNING, not DEBUG. Both of this client's original request
                # shapes were wrong and every failure landed here, so the
                # dashboard showed "0 titles rated" with no way to find out why.
                # An HTTP status is exactly what an operator needs to diagnose
                # that.
                logger.warning(
                    f"mdblist {label}: HTTP {response.status_code} "
                    f"{response.text[:200]}"
                )
                return None

            if self._used_today % 50 == 0:
                self._persist_quota()

            try:
                return response.json()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"mdblist {label}: unparseable response: {exc}")
                return None
        return None

    async def _get(self, path: str, **params: Any) -> Optional[Any]:
        """One GET. Returns None on any failure -- never raises into a caller."""
        params["apikey"] = self.api_key

        async def _call():
            client = await self._http()
            return await client.get(path, params=params)

        return await self._send(path, _call)

    async def _post(self, path: str, body: dict) -> Optional[Any]:
        """One POST. Same contract as :meth:`_get` -- degrades, never raises."""

        async def _call():
            client = await self._http()
            return await client.post(path, params={"apikey": self.api_key}, json=body)

        return await self._send(f"POST {path}", _call)

    async def user_info(self) -> Optional[dict]:
        """``/user`` — tier, daily ceiling, usage, and the batch cap.

        A live probe pins the shape, so nothing here is guessed::

            {"api_requests": 500000,        # the daily CEILING
             "api_requests_count": 14485,   # requests USED today
             "rate_limit": 500000, "rate_limit_remaining": 485515,
             "plan": "VIP 10", "is_supporter": true}

        The previous version looped over
        ``("api_requests", "limit", "daily_limit", "api_requests_count")`` taking
        the max, which resolved correctly only because ``api_requests`` happens
        to be larger than the usage count. Had the account been near its limit,
        or the tuple ordered differently, the ceiling would have been seeded from
        *usage*. The mapping is now explicit -- ceiling and usage are different
        quantities and conflating them is exactly the class of bug that made this
        integration store 0.7% of what it fetched.

        There are **no ``X-RateLimit-*`` response headers** (the header block is
        entirely Cloudflare boilerplate), so this call is the only way the quota
        is ever discovered. Call it before a long run.
        """
        data = await self._get("/user")
        if not isinstance(data, dict):
            return data

        ceiling = data.get("api_requests")
        if not isinstance(ceiling, int) or ceiling <= 0:
            ceiling = data.get("rate_limit")
        if isinstance(ceiling, int) and ceiling > 0:
            self._limit_seen = ceiling

        # MDBList's own count is authoritative: it survives restarts and counts
        # what MDBList counts, where the local tally increments per attempt
        # (retries included) and resets with the process.
        used = data.get("api_requests_count")
        if isinstance(used, int) and used >= 0:
            self._used_today = used
            self._persist_quota()

        self.is_supporter = bool(data.get("is_supporter"))
        # A supporter key accepts 100 ids per batch instead of 10 -- a tenfold
        # reduction in requests for the same work (354k titles: 3,543 requests
        # rather than 35,428). Derived rather than configured so it stays correct
        # if the plan is ever downgraded.
        self.max_batch_ids = 100 if self.is_supporter else 10
        logger.info(
            f"mdblist: plan={data.get('plan') or 'unknown'} "
            f"supporter={self.is_supporter} batch_cap={self.max_batch_ids} "
            f"used={self._used_today:,}/{self._limit_seen or '?'}"
        )
        return data

    async def title_info(self, tmdb_id: int, media_type: str) -> Optional[dict]:
        """Per-title record: ratings, keywords, certification, reviews.

        ``append_to_response`` is what makes the optional blocks appear. Without
        it the response carries ratings and basic metadata only -- which is why
        ``mdb_facts`` never held a single review despite the extraction and the
        length caps working correctly. They were never given anything to cap.
        """
        return await self._get(
            f"/tmdb/{_MEDIA_TYPE(media_type)}/{tmdb_id}/",
            append_to_response=APPEND_FIELDS,
        )

    def _log_shape_once(self, items: list[dict]) -> None:
        """Log the first payload's key names, once per client.

        Every expensive MDBList bug so far has been a silent assumption about
        someone else's JSON: matching on ``id`` when the TMDB id lives at
        ``ids.tmdb``, guessing per-source rating scales, guessing quota field
        names. Each cost a multi-hour run that reported success. One INFO line
        makes the next shape change a ten-second diagnosis instead.
        """
        if self._shape_logged or not items:
            return
        self._shape_logged = True
        first = items[0]
        if not isinstance(first, dict):
            return
        ids = first.get("ids")
        logger.info(
            f"mdblist payload keys: {sorted(first.keys())}"
            + (f" · ids: {sorted(ids.keys())}" if isinstance(ids, dict) else "")
        )

    async def title_info_batch(
        self, tmdb_ids: Sequence[int], media_type: str
    ) -> list[dict]:
        """Resolve many titles in one request.

        The batch companion to :meth:`title_info`, and the difference between a
        ~10 hour pass over the library and a few minutes: at
        ``mdblist_batch_size`` per call, 354k titles is a few thousand requests
        rather than 354k. Note the body takes ``append_to_response`` as a list,
        where the single-title route takes a comma-joined string.

        Returns ``[]`` rather than None on failure, so a caller can iterate
        unconditionally.
        """
        ids = [int(i) for i in tmdb_ids]
        if not ids:
            return []

        # The cap is 10 ids for a regular key and 100 for a supporter, learned
        # from /user rather than configured. Rather than make the caller know
        # which it holds, an over-large batch is split -- so a wrong setting
        # costs extra requests instead of losing the titles entirely.
        cap = min(self.max_batch_ids, MAX_BATCH_IDS)
        if len(ids) > cap:
            out: list[dict] = []
            for start in range(0, len(ids), cap):
                out.extend(
                    await self.title_info_batch(ids[start : start + cap], media_type)
                )
            return out

        payload = await self._post(
            f"/tmdb/{_MEDIA_TYPE(media_type)}/",
            {
                "ids": ids,
                "append_to_response": list(APPEND_FIELDS_LIST),
            },
        )
        items = _as_item_list(payload)
        self._log_shape_once(items)
        if items or len(ids) == 1 or self._exhausted:
            return items

        # An empty result has two very different causes, and only one is worth
        # retrying:
        #
        #   the tier rejected the batch SIZE  -> smaller batches will work
        #   MDBList has no record of these ids -> smaller batches change nothing
        #
        # Halving used to run for both. Against the obscure tail of the library
        # -- 27,776 titles averaging 0.4 popularity, which MDBList genuinely
        # does not carry -- a batch of 10 descended 10 -> 5+5 -> 2+3 -> ... ->
        # singles, about 19 requests to learn nothing. At mdblist_rps=5 that
        # saturated the limiter completely: 152 requests per 30s window, every
        # one wasted, ~52,700 to enrich zero rows.
        #
        # A size rejection is only plausible above the regular-key cap, so that
        # is the only case that halves now. At or below it, empty means absent,
        # and the caller records it as such (see workers/mdb_sync.py) so the
        # title is not asked for again.
        if len(ids) <= REGULAR_KEY_BATCH_IDS:
            return []

        if not self._batch_cap_warned:
            self._batch_cap_warned = True
            logger.warning(
                f"mdblist: batch of {len(ids)} returned nothing and is above "
                f"the {REGULAR_KEY_BATCH_IDS}-id regular-key cap — retrying in "
                "halves. If this repeats, the key does not accept batches this "
                "large; lower mdblist_batch_size."
            )
        mid = len(ids) // 2
        first = await self.title_info_batch(ids[:mid], media_type)
        second = await self.title_info_batch(ids[mid:], media_type)
        return first + second

    async def list_items(
        self, slug: str, limit: int = 1000, offset: int = 0
    ) -> Optional[dict]:
        """Page of a curated list, ratings appended where the tier allows."""
        return await self._get(
            f"/lists/{slug}/items",
            limit=limit,
            offset=offset,
            append_to_response="genres,ratings",
        )

    # -- extraction --------------------------------------------------------
    def extract_ratings(self, payload: dict) -> list[dict]:
        """Pull ``[{source, value, normalized, votes}]`` out of a title record."""
        out: list[dict] = []
        for entry in (payload or {}).get("ratings") or []:
            if not isinstance(entry, dict):
                continue
            source = (entry.get("source") or "").strip().lower()
            if not source:
                continue
            # `score` first: MDBList has already normalised it to 0-100 for every
            # source, so it needs no per-source scale knowledge and cannot go
            # stale when MDBList adds a source we have never heard of.
            normalized = normalize_score(entry.get("score"))
            if normalized is None:
                normalized = normalize_rating(source, entry.get("value"))
            if normalized is None:
                continue
            votes = entry.get("votes")
            out.append(
                {
                    "source": source[:24],
                    "value": entry.get("value"),
                    "normalized": normalized,
                    "votes": int(votes) if isinstance(votes, int) else None,
                }
            )
        return out

    def extract_facts(self, payload: dict) -> dict:
        """Distil a title record into the fields the tagging prompt uses.

        Reviews are capped hard. MDBList can return many long reviews and
        storing them whole would be 10-50KB per title -- 3.5-18GB across the
        library, for prose no query ever reads back. They are stored at all only
        because a title must be re-taggable without re-fetching it.
        """
        payload = payload or {}
        facts: dict[str, Any] = {}

        keywords = [
            str(k.get("name") if isinstance(k, dict) else k)
            for k in (payload.get("keywords") or [])
        ]
        keywords = [k for k in keywords if k][:15]
        if keywords:
            facts["keywords"] = keywords

        for source_key, target in (
            ("certification", "certification"),
            ("age_rating", "age_rating"),
            ("commonsense", "commonsense"),
            ("language", "language"),
            ("country", "country"),
            ("type", "type"),
        ):
            value = payload.get(source_key)
            if value not in (None, "", []):
                facts[target] = value

        # `runtime` means two different things depending on the type.
        #
        # For a movie it is the film's length (The Matrix: 136). For a show it is
        # the TOTAL runtime of every episode (Breaking Bad: 2995 minutes -- 62
        # episodes at ~47 each), which is not comparable to TMDB's per-episode
        # figure and would read as a fifty-hour title.
        #
        # Nothing consumes it today -- app/tagging_prompt.py:71 and
        # taxonomy.derive_tags both take runtime from tag_facts (TMDB) and never
        # from mdb_facts, and the runtime-derived pacing tags are movie-gated
        # anyway. So this is a latent hazard rather than a live bug: the moment
        # anyone merges mdb_facts over tag_facts, or reads mdb runtime for a
        # show, they get a number that is wrong by two orders of magnitude.
        # Storing shows under a distinct key makes the two quantities
        # impossible to confuse instead of relying on every future reader
        # knowing the difference.
        runtime = payload.get("runtime")
        if runtime not in (None, "", []):
            is_show = str(payload.get("type") or "").strip().lower() == "show"
            facts["total_runtime" if is_show else "runtime"] = runtime

        reviews: list[str] = []
        for review in (payload.get("reviews") or [])[
            : settings.mdblist_reviews_per_item
        ]:
            text = ""
            if isinstance(review, dict):
                text = str(review.get("content") or review.get("summary") or "")
            elif isinstance(review, str):
                text = review
            text = " ".join(text.split())
            if text:
                reviews.append(text[: settings.mdblist_review_chars])
        if reviews:
            facts["reviews"] = reviews

        return facts
