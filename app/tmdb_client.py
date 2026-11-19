"""
TMDB (The Movie Database) API client.

Handles all interactions with TMDB API for movie and TV show metadata.
"""

import asyncio
import weakref
from typing import List, Dict, Optional, Literal
from datetime import datetime, timedelta
import httpx
from loguru import logger
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.time_utils import utc_now  # noqa: E402
from app.config import settings


class TMDBServerError(Exception):
    """TMDB returned a 5xx error (not worth retrying, especially at deep pages)."""

    pass


def _is_retryable(exc: BaseException) -> bool:
    """Retry on transient/connection errors only."""
    if isinstance(exc, TMDBServerError):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        # Rate limiting is transient — back off and retry. Other 4xx are not.
        return exc.response.status_code == 429
    # A pool timeout means this process already has more TMDB requests in flight
    # than the pool can carry. Retrying re-queues the request behind the same
    # saturated pool and deepens the jam that caused it.
    if isinstance(exc, httpx.PoolTimeout):
        return False
    # A read timeout already waited the full read budget. Three of them plus
    # backoff held a caller (and its admission slot) for ~100s; one is enough.
    if isinstance(exc, httpx.ReadTimeout):
        return False
    if isinstance(exc, (httpx.ConnectError, httpx.RemoteProtocolError, OSError)):
        return True
    return True


_exponential_wait = wait_exponential(multiplier=1, min=2, max=10)


def _retry_wait(retry_state: RetryCallState) -> float:
    """Honour TMDB's ``Retry-After`` on 429, otherwise back off exponentially."""
    outcome = retry_state.outcome
    exc = outcome.exception() if outcome is not None else None
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        try:
            return min(max(float(exc.response.headers["Retry-After"]), 0.0), 10.0)
        except (KeyError, ValueError):
            pass
    return _exponential_wait(retry_state)


MediaType = Literal["movie", "tv"]

# Standard TMDB genre IDs — avoids an extra API call to /genre/movie/list
# Combined movie + TV genres (TMDB uses the same IDs across both)
_GENRE_MAP: dict[int, str] = {
    28: "Action",
    12: "Adventure",
    16: "Animation",
    35: "Comedy",
    80: "Crime",
    99: "Documentary",
    18: "Drama",
    10751: "Family",
    14: "Fantasy",
    36: "History",
    27: "Horror",
    10402: "Music",
    9648: "Mystery",
    10749: "Romance",
    878: "Science Fiction",
    10770: "TV Movie",
    53: "Thriller",
    10752: "War",
    37: "Western",
    10759: "Action & Adventure",
    10762: "Kids",
    10763: "News",
    10764: "Reality",
    10765: "Sci-Fi & Fantasy",
    10766: "Soap",
    10767: "Talk",
    10768: "War & Politics",
}


def parse_find_result(
    data: Dict, prefer_media_type: Optional[str] = None
) -> Optional[Dict]:
    """Collapse a TMDB ``/find`` payload into one canonical resolution.

    An IMDb id usually maps to exactly one entity, so the result lists are
    normally mutually exclusive. Resolution precedence:

    - ``movie_results[0]``      -> ``{tmdb_id, media_type='movie'}``
    - ``tv_results[0]``         -> ``{tmdb_id, media_type='tv'}`` (series)
    - ``tv_episode_results[0]`` -> ``{tmdb_id=show_id, media_type='tv',
      season, episode}`` (the series tmdb_id plus the episode's season/episode)

    TMDB does list some works under both a movie and a TV record, typically an
    unreleased series with a stub movie entry. ``prefer_media_type='tv'`` puts
    the TV results ahead of the movie one for a caller that knows it is showing
    a series; the default order is unchanged.

    Returns ``None`` when nothing matched (caller stores an imdb-only fallback).
    """
    if not isinstance(data, dict):
        return None

    movie_hit = None
    movie = data.get("movie_results") or []
    if movie and movie[0].get("id") is not None:
        movie_hit = {
            "tmdb_id": movie[0]["id"],
            "media_type": "movie",
            "season": None,
            "episode": None,
        }

    tv_hit = None
    tv = data.get("tv_results") or []
    if tv and tv[0].get("id") is not None:
        tv_hit = {
            "tmdb_id": tv[0]["id"],
            "media_type": "tv",
            "season": None,
            "episode": None,
        }

    episode_hit = None
    episode = data.get("tv_episode_results") or []
    if episode and episode[0].get("show_id") is not None:
        ep = episode[0]
        episode_hit = {
            "tmdb_id": ep["show_id"],
            "media_type": "tv",
            "season": ep.get("season_number"),
            "episode": ep.get("episode_number"),
        }

    if prefer_media_type == "tv":
        return tv_hit or episode_hit or movie_hit
    return movie_hit or tv_hit or episode_hit


# TMDB fan-out arrives from several independent places at once: detail builds,
# the season fan-out, catalog sync. Each was bounded on its own,
# but nothing bounded their sum, so bursts pushed well past the pool's
# `max_connections`. httpx then parks the excess on the pool and raises
# PoolTimeout after `pool` seconds — which surfaced as `TMDB request failed:`
# with an empty message, since httpx timeouts carry no message text. Admission
# is gated here instead: callers wait on a semaphore that never times out, so a
# burst queues rather than failing and re-queueing.
_TMDB_MAX_CONNECTIONS = 20
_TMDB_MAX_INFLIGHT = 16

# TMDB sometimes adds a current storefront date as a movie's first regional
# type-4 entry even though the movie has been available at home for years. That
# is a catalog/rerelease date, not the original digital release. Allow a long
# theatrical-to-digital path for genuinely delayed films, but do not turn an
# old title into a new release because one retailer added it recently.
_DIGITAL_RERELEASE_GAP_DAYS = 2 * 365


class TMDBClient:
    """Client for TMDB API."""

    BASE_URL = "https://api.themoviedb.org/3"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.tmdb_api_key
        # One connection pool PER EVENT LOOP, not one per client.
        #
        # This is a module-level singleton, and since the upgrade pipeline moved
        # onto a worker thread with its own loop (app/admin.py::_run_upgrade_sync)
        # there are two loops using it: the uvicorn loop serving clients and the
        # web player, and the pipeline's. httpx pools connections, and a pooled
        # connection carries asyncio primitives bound to the loop that created
        # it -- so whichever loop picked up the other's connection failed with
        # "<asyncio.locks.Event object ...> is bound to a different event loop",
        # surfacing as 502s on details requests while an upgrade ran.
        #
        # Keyed weakly on the loop itself rather than id(loop): ids are recycled
        # after a loop is collected, which would hand a fresh loop a dead pool.
        self._clients: (
            "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]"
        ) = weakref.WeakKeyDictionary()  # noqa: E501
        # Bound to the loop for the same reason the pool is.
        self._gates: (
            "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]"
        ) = weakref.WeakKeyDictionary()  # noqa: E501
        self._stats: Dict = {
            "total_calls": 0,
            "errors_4xx": 0,
            "errors_5xx": 0,
            "errors_network": 0,
            "last_call_at": None,
            "last_error": None,
        }

    def _loop_client(self) -> httpx.AsyncClient:
        """The pool belonging to the running loop, created on first use."""
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=15.0),
                limits=httpx.Limits(
                    max_keepalive_connections=10,
                    max_connections=_TMDB_MAX_CONNECTIONS,
                ),
            )
            self._clients[loop] = client
        return client

    def _loop_gate(self) -> asyncio.Semaphore:
        """The admission gate belonging to the running loop."""
        loop = asyncio.get_running_loop()
        gate = self._gates.get(loop)
        if gate is None:
            gate = asyncio.Semaphore(_TMDB_MAX_INFLIGHT)
            self._gates[loop] = gate
        return gate

    async def close(self):
        """Close this loop's pool. Other loops' pools are left to their loops.

        Calling ``aclose()`` on a pool belonging to a different loop raises the
        very cross-loop error this split exists to prevent, so the others are
        dropped rather than closed -- they are reclaimed with their loop.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._clients.clear()
            return
        client = self._clients.pop(loop, None)
        if client is not None:
            await client.aclose()

    @retry(
        stop=stop_after_attempt(3),
        wait=_retry_wait,
        retry=retry_if_exception(_is_retryable),
    )
    async def _request(self, endpoint: str, params: Optional[Dict] = None) -> Dict:
        """
        Make request to TMDB API with retry logic.

        Args:
            endpoint: API endpoint (e.g., '/movie/550')
            params: Query parameters

        Returns:
            JSON response as dictionary
        """
        if params is None:
            params = {}

        params["api_key"] = self.api_key

        url = f"{self.BASE_URL}{endpoint}"

        self._stats["total_calls"] += 1
        self._stats["last_call_at"] = utc_now().isoformat()
        try:
            async with self._loop_gate():
                response = await self._loop_client().get(url, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status >= 500:
                self._stats["errors_5xx"] += 1
                self._stats["last_error"] = f"{status} on {endpoint}"
                raise TMDBServerError(
                    f"TMDB server error {status} on {endpoint}"
                ) from e
            self._stats["errors_4xx"] += 1
            self._stats["last_error"] = f"{status} on {endpoint}"
            if status == 404:
                # Stale or invalid ids are routine (old cards, removed titles);
                # callers already treat them as absent.
                logger.info(f"TMDB 404 on {endpoint}")
            else:
                logger.error(f"TMDB API error: {status} - {e.response.text}")
            raise
        except Exception as e:
            # httpx timeout classes carry no message, so name the type: without it
            # every transport failure logged as "TMDB request failed:" and nothing else.
            description = f"{type(e).__name__}: {e}".rstrip(": ")
            self._stats["errors_network"] += 1
            self._stats["last_error"] = description[:120]
            logger.error(f"TMDB request failed on {endpoint} - {description}")
            raise

    async def find_by_imdb_id(self, imdb_id: str) -> Dict:
        """Resolve an external IMDb id (``tt...``) to TMDB results via ``/find``.

        Returns the raw TMDB payload with ``movie_results``, ``tv_results`` and
        ``tv_episode_results`` lists. Use :func:`parse_find_result` to collapse
        it into a single canonical resolution.
        """
        return await self._request(
            f"/find/{imdb_id}",
            params={"external_source": "imdb_id"},
        )

    async def get_movie(self, tmdb_id: int) -> Dict:
        """Get detailed movie information (includes external_ids for IMDB).

        ``release_dates`` is appended so callers can surface the digital (type 4)
        home-release date without a second request (see :func:`digital_release_date`).
        """
        return await self._request(
            f"/movie/{tmdb_id}",
            params={
                "append_to_response": (
                    "credits,keywords,external_ids,images,videos,release_dates,reviews"
                ),
                "include_image_language": "en,null",
            },
        )

    async def get_tv_show(self, tmdb_id: int) -> Dict:
        """Get detailed TV show information (includes external_ids for IMDB)."""
        return await self._request(
            f"/tv/{tmdb_id}",
            params={
                "append_to_response": (
                    "credits,keywords,external_ids,images,videos,reviews,content_ratings"
                ),
                "include_image_language": "en,null",
            },
        )

    async def get_tv_show_summary(self, tmdb_id: int) -> Dict:
        """Bare ``/tv/{id}``: top-level fields such as ``next_episode_to_air``.

        For checks that need nothing appended -- the full :meth:`get_tv_show`
        response carries credits, images, videos and reviews as well.
        """
        return await self._request(f"/tv/{tmdb_id}")

    async def get_tv_season(self, tmdb_id: int, season_number: int) -> Dict:
        """Get detailed information for a specific TV season."""
        return await self._request(f"/tv/{tmdb_id}/season/{season_number}")

    async def get_trending_movies(
        self, time_window: str = "day", page: int = 1
    ) -> Dict:
        """Get trending movies (day or week)."""
        return await self._request(
            f"/trending/movie/{time_window}", params={"page": page}
        )

    async def get_trending_tv_shows(
        self, time_window: str = "day", page: int = 1
    ) -> Dict:
        """Get trending TV shows (day or week)."""
        return await self._request(f"/trending/tv/{time_window}", params={"page": page})

    async def get_movie_release_dates(self, tmdb_id: int) -> Dict:
        """Get release dates for a movie by region and type."""
        return await self._request(f"/movie/{tmdb_id}/release_dates")

    async def get_movie_keywords(self, tmdb_id: int) -> Dict:
        """Get the lightweight keyword payload used to screen Home feeds."""
        return await self._request(f"/movie/{tmdb_id}/keywords")

    @staticmethod
    def has_home_release(release_dates_data: Dict, region: str = "US") -> bool:
        """Return True if the movie has a digital (type 4) or physical (type 5) release."""
        for r in release_dates_data.get("results", []):
            if r.get("iso_3166_1") == region:
                for rd in r.get("release_dates", []):
                    if rd.get("type") in (4, 5):
                        return True
        return False

    @staticmethod
    def digital_release_date(
        release_dates_data: Dict, region: str = "US"
    ) -> Optional[str]:
        """Return the digital (type 4) release date as ``yyyy-MM-dd``, or None.

        Prefers ``region`` (earliest of its type-4 dates); if that region lists no
        digital release, falls back to the earliest type-4 date across any region.
        A regional type-4 date more than two years after that movie's earlier
        regional theatrical/home history is treated as a late catalog or rerelease
        entry: an older worldwide digital date wins, or the field is suppressed if
        TMDB has no original digital date. Future-dated first releases are kept (that
        is what powers the "upcoming" countdown client-side). Accepts the payload
        from either ``/movie/{id}/release_dates`` or the ``release_dates`` appended
        to a movie detail response.
        """
        regional: list[str] = []
        everywhere: list[str] = []
        regional_history: list[str] = []
        for r in release_dates_data.get("results", []):
            is_region = r.get("iso_3166_1") == region
            for rd in r.get("release_dates", []):
                if not rd.get("release_date"):
                    continue
                release_type = rd.get("type")
                release_date = str(rd["release_date"])[:10]
                if is_region and release_type in (2, 3, 4, 5):
                    regional_history.append(release_date)
                if release_type == 4:
                    everywhere.append(release_date)
                    if is_region:
                        regional.append(release_date)

        if not regional:
            return min(everywhere) if everywhere else None

        candidate = min(regional)
        if regional_history:
            try:
                candidate_day = datetime.strptime(candidate, "%Y-%m-%d")
                first_regional_day = datetime.strptime(
                    min(regional_history), "%Y-%m-%d"
                )
                is_late_catalog_entry = (
                    candidate_day - first_regional_day
                ).days > _DIGITAL_RERELEASE_GAP_DAYS
            except ValueError:
                is_late_catalog_entry = False

            if is_late_catalog_entry:
                earlier_worldwide = [date for date in everywhere if date < candidate]
                return min(earlier_worldwide) if earlier_worldwide else None

        return candidate

    @staticmethod
    def next_episode_air_date(detail: Dict) -> Optional[str]:
        """Return the next episode's air date as ``yyyy-MM-dd``, or None.

        Reads TMDB's top-level ``next_episode_to_air``, which ``/tv/{id}`` returns
        natively (no ``append_to_response`` needed) — so this needs no episode-list
        walk. Absent on list results and on series that have finished airing.
        """
        upcoming = detail.get("next_episode_to_air")
        if not isinstance(upcoming, dict):
            return None
        air_date = upcoming.get("air_date")
        return str(air_date)[:10] if air_date else None

    async def get_similar_movies(self, tmdb_id: int, page: int = 1) -> Dict:
        """Get movies similar to the given movie."""
        return await self._request(f"/movie/{tmdb_id}/similar", params={"page": page})

    async def get_similar_tv_shows(self, tmdb_id: int, page: int = 1) -> Dict:
        """Get TV shows similar to the given show."""
        return await self._request(f"/tv/{tmdb_id}/similar", params={"page": page})

    async def get_popular_movies(self, page: int = 1) -> Dict:
        """Get popular movies."""
        return await self._request("/movie/popular", params={"page": page})

    async def get_popular_tv_shows(self, page: int = 1) -> Dict:
        """Get popular TV shows."""
        return await self._request("/tv/popular", params={"page": page})

    async def get_top_rated_movies(self, page: int = 1) -> Dict:
        """Get top rated movies."""
        return await self._request("/movie/top_rated", params={"page": page})

    async def get_top_rated_tv_shows(self, page: int = 1) -> Dict:
        """Get top rated TV shows."""
        return await self._request("/tv/top_rated", params={"page": page})

    async def discover_movies(
        self,
        page: int = 1,
        sort_by: str = "popularity.desc",
        year: Optional[int] = None,
        with_genres: Optional[str] = None,
        vote_average_gte: Optional[float] = None,
    ) -> Dict:
        """
        Discover movies with filters.

        Args:
            page: Page number
            sort_by: Sort order (popularity.desc, vote_average.desc, etc.)
            year: Filter by release year
            with_genres: Comma-separated genre IDs
            vote_average_gte: Minimum vote average
        """
        params = {
            "page": page,
            "sort_by": sort_by,
            "include_adult": False,
            "include_video": False,
        }

        if year:
            params["primary_release_year"] = year
        if with_genres:
            params["with_genres"] = with_genres
        if vote_average_gte:
            params["vote_average.gte"] = vote_average_gte

        return await self._request("/discover/movie", params=params)

    async def discover_tv_shows(
        self,
        page: int = 1,
        sort_by: str = "popularity.desc",
        first_air_date_year: Optional[int] = None,
        with_genres: Optional[str] = None,
        vote_average_gte: Optional[float] = None,
    ) -> Dict:
        """
        Discover TV shows with filters.

        Args:
            page: Page number
            sort_by: Sort order
            first_air_date_year: Filter by first air date year
            with_genres: Comma-separated genre IDs
            vote_average_gte: Minimum vote average
        """
        params = {
            "page": page,
            "sort_by": sort_by,
            "include_adult": False,
        }

        if first_air_date_year:
            params["first_air_date_year"] = first_air_date_year
        if with_genres:
            params["with_genres"] = with_genres
        if vote_average_gte:
            params["vote_average.gte"] = vote_average_gte

        return await self._request("/discover/tv", params=params)

    async def get_movies_released_in_date_range(
        self, start_date: datetime, end_date: datetime, page: int = 1
    ) -> Dict:
        """
        Get movies released between two dates.

        Args:
            start_date: Start date
            end_date: End date
            page: Page number
        """
        params = {
            "page": page,
            "sort_by": "popularity.desc",
            "primary_release_date.gte": start_date.strftime("%Y-%m-%d"),
            "primary_release_date.lte": end_date.strftime("%Y-%m-%d"),
        }

        return await self._request("/discover/movie", params=params)

    async def get_tv_shows_aired_in_date_range(
        self, start_date: datetime, end_date: datetime, page: int = 1
    ) -> Dict:
        """
        Get TV shows that first aired between two dates.

        Args:
            start_date: Start date
            end_date: End date
            page: Page number
        """
        params = {
            "page": page,
            "sort_by": "popularity.desc",
            "first_air_date.gte": start_date.strftime("%Y-%m-%d"),
            "first_air_date.lte": end_date.strftime("%Y-%m-%d"),
        }

        return await self._request("/discover/tv", params=params)

    async def get_new_releases_this_week(self, media_type: MediaType) -> List[Dict]:
        """
        Get movies or TV shows released in the past week.

        Args:
            media_type: 'movie' or 'tv'

        Returns:
            List of media items
        """
        end_date = datetime.now()
        start_date = end_date - timedelta(days=7)

        all_results = []
        page = 1

        while page <= 5:  # Limit to 5 pages (100 items)
            if media_type == "movie":
                response = await self.get_movies_released_in_date_range(
                    start_date, end_date, page
                )
            else:
                response = await self.get_tv_shows_aired_in_date_range(
                    start_date, end_date, page
                )

            results = response.get("results", [])
            if not results:
                break

            all_results.extend(results)

            if page >= response.get("total_pages", 1):
                break

            page += 1

        # Deduplicate before returning. TMDB's discover endpoints page over an
        # unstable sort, so the same title can appear on two pages of one walk --
        # and a duplicated id here reached db.merge() twice in daily_update.
        # SessionLocal runs with autoflush=False, so the second merge could not
        # see the first one's pending INSERT and both were flushed, which is what
        # produced the recurring
        # `UniqueViolation: duplicate key value violates "media_metadata_pkey"`
        # that killed whole daily runs.
        seen: set = set()
        unique: List[Dict] = []
        for item in all_results:
            tmdb_id = item.get("id")
            if tmdb_id is None or tmdb_id in seen:
                continue
            seen.add(tmdb_id)
            unique.append(item)

        if len(unique) < len(all_results):
            logger.info(
                f"[{media_type}] new releases: deduplicated "
                f"{len(all_results)} -> {len(unique)}"
            )

        logger.info(f"Found {len(unique)} new {media_type} releases this week")
        return unique

    async def get_popular_items(
        self, media_type: MediaType, limit: int = 10000
    ) -> List[Dict]:
        """
        Get popular movies or TV shows.

        Args:
            media_type: 'movie' or 'tv'
            limit: Maximum number of items to fetch

        Returns:
            List of media items
        """
        all_results = []
        page = 1
        max_pages = min(500, (limit // 20) + 1)  # TMDB limits to 500 pages

        while page <= max_pages:
            try:
                if media_type == "movie":
                    response = await self.get_popular_movies(page)
                else:
                    response = await self.get_popular_tv_shows(page)
            except TMDBServerError:
                logger.debug(f"Popular {media_type} reached TMDB limit at page {page}")
                break

            results = response.get("results", [])
            if not results:
                break

            all_results.extend(results)

            if len(all_results) >= limit:
                break

            if page >= response.get("total_pages", 1):
                break

            page += 1

        logger.info(f"Fetched {len(all_results)} popular {media_type} items")
        return all_results[:limit]

    async def _paginate_discover(
        self,
        media_type: MediaType,
        params: Dict,
        seen_ids: set,
        max_pages: int = 500,
        should_stop=None,
    ) -> List[Dict]:
        """Paginate through a discover query, skipping already-seen IDs.

        ``should_stop`` is polled per page. Passed as a callable rather than
        imported, so this module stays free of any dependency on the worker
        layer's stop flag -- it is an HTTP client and knows nothing about
        builds.
        """
        results = []
        endpoint = "/discover/movie" if media_type == "movie" else "/discover/tv"

        for page in range(1, max_pages + 1):
            if should_stop and should_stop():
                logger.info(f"Discover {media_type} stopped by request at page {page}")
                break
            params["page"] = page
            try:
                response = await self._request(endpoint, params=params.copy())
            except TMDBServerError:
                logger.debug(f"Discover {media_type} reached TMDB limit at page {page}")
                break
            except Exception as e:
                logger.warning(f"Discover page {page} failed: {e}")
                break

            items = response.get("results", [])
            if not items:
                break

            for item in items:
                tmdb_id = item.get("id")
                if tmdb_id and tmdb_id not in seen_ids:
                    seen_ids.add(tmdb_id)
                    results.append(item)

            if page >= response.get("total_pages", 1):
                break

        return results

    async def fetch_all_items(
        self, media_type: MediaType, limit: int = 100000, should_stop=None
    ) -> List[Dict]:
        """
        Fetch items using multiple strategies to maximize catalog coverage.

        Strategies applied in order:
        1. Popular endpoint (baseline)
        2. Top rated
        3. Discover by year (each year gets its own 500-page window)
        4. Discover by vote average (hidden gems with good ratings)
        5. Discover by language (international cinema)

        Args:
            media_type: 'movie' or 'tv'
            limit: Maximum number of unique items to fetch
            should_stop: Polled per page and between strategies. Without it a
                crawl could not be halted at all -- the build's stop check ran
                only *before* each fetch, so pressing Stop during a multi-hour
                acquisition did nothing until the whole media type finished.
                Whatever has been collected when the stop lands is returned, so
                the caller keeps its partial work.

        Returns:
            Deduplicated list of media items
        """
        seen_ids: set = set()
        all_items: List[Dict] = []

        def _add_items(items: List[Dict], label: str):
            new = 0
            for item in items:
                tmdb_id = item.get("id")
                if tmdb_id and tmdb_id not in seen_ids:
                    seen_ids.add(tmdb_id)
                    all_items.append(item)
                    new += 1
            logger.info(
                f"[{media_type}] {label}: +{new} new items "
                f"(total: {len(all_items)})"
            )

        # --- Strategy 1: Popular endpoint ---
        popular = await self.get_popular_items(media_type, limit=10000)
        _add_items(popular, "Popular")

        if len(all_items) >= limit or (should_stop and should_stop()):
            return all_items[:limit]

        # --- Strategy 2: Top rated ---
        logger.info(f"[{media_type}] Fetching top rated...")
        top_rated = []
        for page in range(1, 501):
            if should_stop and should_stop():
                logger.info(f"[{media_type}] Top rated stopped at page {page}")
                break
            try:
                if media_type == "movie":
                    resp = await self.get_top_rated_movies(page)
                else:
                    resp = await self.get_top_rated_tv_shows(page)
            except TMDBServerError:
                logger.debug(
                    f"[{media_type}] Top rated reached TMDB limit at page {page}"
                )
                break
            except Exception:
                break
            items = resp.get("results", [])
            if not items:
                break
            top_rated.extend(items)
            if page >= resp.get("total_pages", 1):
                break
        _add_items(top_rated, "Top Rated")

        if len(all_items) >= limit or (should_stop and should_stop()):
            return all_items[:limit]

        # --- Strategy 3: Discover by year (most popular per year) ---
        current_year = datetime.now().year
        year_key = (
            "primary_release_year" if media_type == "movie" else "first_air_date_year"
        )

        for year in range(current_year, 1969, -1):
            if len(all_items) >= limit or (should_stop and should_stop()):
                break
            logger.info(f"[{media_type}] Discovering year {year}...")
            params = {
                "sort_by": "popularity.desc",
                "include_adult": False,
                year_key: year,
            }
            if media_type == "movie":
                params["include_video"] = False
            items = await self._paginate_discover(
                media_type, params, seen_ids, max_pages=500, should_stop=should_stop
            )
            all_items.extend(items)
            logger.info(
                f"[{media_type}] Year {year}: +{len(items)} new "
                f"(total: {len(all_items)})"
            )

        if len(all_items) >= limit or (should_stop and should_stop()):
            return all_items[:limit]

        # --- Strategy 4: Hidden gems (high vote avg, lower popularity) ---
        logger.info(f"[{media_type}] Fetching hidden gems...")
        for min_votes in [100, 50, 20]:
            if len(all_items) >= limit or (should_stop and should_stop()):
                break
            params = {
                "sort_by": "vote_average.desc",
                "vote_count.gte": min_votes,
                "include_adult": False,
            }
            if media_type == "movie":
                params["include_video"] = False
            items = await self._paginate_discover(
                media_type, params, seen_ids, max_pages=500, should_stop=should_stop
            )
            all_items.extend(items)
            logger.info(
                f"[{media_type}] Hidden gems (votes>={min_votes}): "
                f"+{len(items)} new (total: {len(all_items)})"
            )

        if len(all_items) >= limit or (should_stop and should_stop()):
            return all_items[:limit]

        # --- Strategy 5: International cinema by language ---
        languages = [
            "ko",
            "ja",
            "fr",
            "es",
            "de",
            "it",
            "hi",
            "zh",
            "pt",
            "sv",
            "da",
            "no",
            "th",
            "tr",
            "pl",
        ]
        logger.info(f"[{media_type}] Fetching international content...")
        for lang in languages:
            if len(all_items) >= limit or (should_stop and should_stop()):
                break
            params = {
                "sort_by": "popularity.desc",
                "with_original_language": lang,
                "include_adult": False,
            }
            if media_type == "movie":
                params["include_video"] = False
            items = await self._paginate_discover(
                media_type, params, seen_ids, max_pages=500, should_stop=should_stop
            )
            all_items.extend(items)
            logger.info(
                f"[{media_type}] Language '{lang}': +{len(items)} new "
                f"(total: {len(all_items)})"
            )

        logger.info(f"[{media_type}] Fetch complete: {len(all_items)} unique items")
        return all_items[:limit]

    def extract_metadata(self, item: Dict, media_type: MediaType) -> Dict:
        """
        Extract relevant metadata from TMDB response.

        Args:
            item: TMDB API response
            media_type: 'movie' or 'tv'

        Returns:
            Cleaned metadata dictionary
        """
        if media_type == "movie":
            title = item.get("title", "Unknown")
            original_title = item.get("original_title")
            release_date = item.get("release_date", "")
        else:
            title = item.get("name", "Unknown")
            original_title = item.get("original_name")
            release_date = item.get("first_air_date", "")

        # Extract imdb_id from external_ids (only present in detail responses)
        imdb_id = (
            item.get("external_ids", {}).get("imdb_id")
            if isinstance(item.get("external_ids"), dict)
            else None
        )

        # Extract logo path (prefer English logos)
        logo_path = None
        logos = (
            item.get("images", {}).get("logos", [])
            if isinstance(item.get("images"), dict)
            else []
        )
        if logos:
            en_logos = [lg for lg in logos if lg.get("iso_639_1") in ("en", None)]
            chosen = en_logos[0] if en_logos else logos[0]
            logo_path = chosen.get("file_path")

        return {
            "tmdb_id": item["id"],
            "media_type": media_type,
            "title": title,
            "original_title": original_title,
            "original_language": item.get("original_language"),
            "adult": item.get("adult", False),
            "overview": item.get("overview", ""),
            "release_date": release_date,
            "genres": (
                [g["name"] for g in item["genres"]]
                if "genres" in item
                and item["genres"]
                and isinstance(item["genres"][0], dict)
                else [
                    _GENRE_MAP[gid]
                    for gid in item.get("genre_ids", [])
                    if gid in _GENRE_MAP
                ]
            ),
            # Both resolve to None for list payloads (which carry neither key). The
            # callers' selective non-null updates then leave any previously stored
            # detail-level date intact rather than clobbering it.
            "digital_release_date": (
                self.digital_release_date(item.get("release_dates") or {})
                if media_type == "movie"
                else None
            ),
            "next_episode_air_date": (
                self.next_episode_air_date(item) if media_type == "tv" else None
            ),
            "poster_path": item.get("poster_path"),
            "backdrop_path": item.get("backdrop_path"),
            "imdb_id": imdb_id,
            "logo_path": logo_path,
            "vote_average": item.get("vote_average"),
            "vote_count": item.get("vote_count"),
            "popularity": item.get("popularity"),
            "number_of_seasons": (
                item.get("number_of_seasons") if media_type == "tv" else None
            ),
            "number_of_episodes": (
                item.get("number_of_episodes") if media_type == "tv" else None
            ),
            "raw_data": item,
        }


# Global client instance
tmdb_client = TMDBClient()
