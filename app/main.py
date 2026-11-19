"""
Main FastAPI application for Curatio.

Serves the native client API (``/{user_key}/api/...`` and ``/account/api/...``).
"""

from collections import OrderedDict
import asyncio
import collections
import copy
import hashlib
import json
import math
import mimetypes
import os
import random
import re
import threading
import weakref
from uuid import UUID
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic, time
from urllib.parse import (
    parse_qsl,
    quote,
    unquote,
    urlencode,
    urlparse,
    urlsplit,
    urlunsplit,
)

from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

from fastapi import FastAPI, Depends, HTTPException, Query, Request
import httpx
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    HTMLResponse,
    RedirectResponse,
    StreamingResponse,
    Response,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import case, func, literal, tuple_
from sqlalchemy.orm import Session, load_only
from datetime import date, datetime, timedelta
import secrets
from types import SimpleNamespace
from loguru import logger

from app.server_history import TASTE_ACTIONS  # noqa: E402
from app.time_utils import utc_now  # noqa: E402
from app import playback_activity
from app.playback_activity import PresenceReport
from app.config import settings, validate_api_keys
from app.http_middleware import SelectiveGZipMiddleware
from app.database import (
    get_db_dependency,
    get_db,
    init_database,
    check_database_connection,
    pool_occupancy,
)
from app.models import (
    Account,
    User,
    UniversalCategory,
    UniversalCatalogContent,
    UserCatalog,
    UserCatalogContent,
    InviteCode,
    DetailSnapshot,
    MediaMetadata,
    MovieTag,
    WatchEvent,
    Tag,
    TagStats,
    TitleRating,
    WatchProgress,
    HiddenTitle,
    LibraryItem,
    ImdbResolution,
    SegmentMarker,
    SegmentFeedback,
    EpisodeFingerprint,
    PasskeyChallenge,
    PasskeyCredential,
    MobileAuthCode,
)
from app.catalog_generator import (
    CatalogGenerator,
    _quality_value,
    is_tmdb_sourced_slot,
    quality_expr,
)
from workers.tagging_common import shuffle_within_tiers
from pydantic import BaseModel, Field
from typing import Optional, Literal, cast

from app.landing import welcome_page_html
from app.admin import router as admin_router, load_settings_from_db
from app.account import router as account_router
from app.home_feed import router as home_feed_router
from app.auth import (
    BearerUserKey,
    invalid_profile_credential,
    account_passkey_count,
    auth_requirement,
    hash_password,
    verify_password,
    dummy_verify_password,
    generate_totp_secret,
    get_totp_provisioning_uri,
    verify_account_totp,
    generate_totp_qr_data_url,
    encrypt_totp_secret,
    create_user_session,
    verify_user_session,
    get_user_session,
    set_session_active_profile,
    delete_user_session,
    create_pairing_session,
    claim_pairing_session,
    create_device_pairing_session,
    claim_device_pairing_session,
    poll_device_pairing_session,
    create_mobile_auth_code,
    exchange_mobile_auth_code,
    exchange_desktop_auth_code,
    is_loopback_redirect_uri,
    renew_desktop_session,
    DESKTOP_SESSION_LIFETIME,
    get_client_ip,
)
from app.net_safety import is_safe_source_url, public_only_transport
from app.sources import registry as source_registry
from app.sources import resolver as source_resolver
from app.sources.availability import (
    AvailabilityIndex,
    annotate_tree,
    availability_revision,
)
from app.sources.models import FailureReport, MediaRef, PlaybackContext, PlaybackHints
from app.media_probe import MediaProbeError, MediaProbeTimeout, media_inspector
from app.remux import (
    RemuxCapacityError,
    RemuxPrepareError,
    RemuxSessionFailed,
    UnsupportedRemuxVideoError,
    create_proxy_token,
    remux_manager,
    verify_proxy_token,
)
from app.passkeys import (
    b64url_decode,
    b64url_encode,
    cookie_secure as _cookie_secure,
    credential_from_dict,
    get_webauthn_config,
    load_webauthn,
    options_to_dict,
)


def _api_media_type(media_type: str) -> str:
    """Map an internal media type to the client-facing content type.

    Storage and TMDB use ``tv``; the client API's ``content_type`` (and the
    details ``addon_type`` field) spell it ``series``. Movies are unchanged.
    """
    return "series" if media_type == "tv" else media_type


def _entry_content_type(entry: dict) -> str | None:
    """The client-facing content type ("movie"/"series") for one details entry.

    Emitted as ``addon_type`` on the details ``item`` and ``similar[]`` entries,
    the field the Apple client decodes. Deliberately not ``media_type``'s
    "movie"/"tv" spelling -- that field keeps its own values. Copied from the
    entry's ``content_type`` so the two agree by construction, with the
    ``media_type`` mapping as a fallback.
    """
    existing = entry.get("content_type")
    if existing:
        return str(existing)
    media_type = entry.get("media_type")
    return _api_media_type(str(media_type)) if media_type else None


class ScrobbleRequest(BaseModel):
    """Payload sent by a Curatio client on playback completion.

    At least one of ``tmdb_id`` / ``imdb_id`` must be present (validated in the
    handler). ``imdb_id`` lets the client sync items that have no TMDB id.
    """

    tmdb_id: Optional[int] = None
    imdb_id: Optional[str] = None  # 'tt1234567'
    media_type: Literal["movie", "tv"]
    action: Literal["complete"] = "complete"
    season: Optional[int] = None
    episode: Optional[int] = None
    title: Optional[str] = None


class SegmentFeedbackRequest(BaseModel):
    segment_type: Literal["intro", "credits"]
    feedback: Literal["correct", "wrong"]
    adjusted_start: Optional[float] = None
    adjusted_end: Optional[float] = None


# Debounce tracking for local catalog rebuilds
_last_local_sync: dict[int, float] = {}
_LOCAL_SYNC_DEBOUNCE = 1800  # 30 minutes
# Strong references to in-flight sync tasks. asyncio only holds weak references
# to tasks, so without this the fire-and-forget task can be garbage-collected
# mid-execution and the sync silently never completes.
_local_sync_tasks: set[asyncio.Task] = set()

# Cap concurrent background per-user syncs. Each holds a pooled DB connection
# across awaited TMDB/Gemini I/O, so an unbounded burst (e.g. many profile
# requests at once) could otherwise exhaust the small pool. Bounding concurrency
# caps how many connections the sync path can pin at any moment.
_LOCAL_SYNC_CONCURRENCY = 3
_local_sync_semaphore = asyncio.Semaphore(_LOCAL_SYNC_CONCURRENCY)


def _schedule_local_sync(user_id: int):
    """Fire-and-forget background task to rebuild a local user's catalogs."""
    now = time()
    last = _last_local_sync.get(user_id, 0)
    if now - last < _LOCAL_SYNC_DEBOUNCE:
        return  # debounce — too soon since last sync

    _last_local_sync[user_id] = now

    def _publish_sync() -> None:
        # Clear this process after the worker-owned transaction commits so the
        # foreground rebuild becomes visible immediately.
        from workers.build_control import clear_serving_cache

        clear_serving_cache(f"profile {user_id} foreground sync", user_id=user_id)
        _invalidate_dashboard_cache(user_id)

    def _sync_failed(exc: Exception) -> None:
        # Release the debounce so the next profile request retries promptly
        # instead of being blocked for the full debounce window.
        _last_local_sync.pop(user_id, None)
        logger.error(f"Background local sync failed for user {user_id}: {exc}")

    async def _do_sync():
        from workers.isolated_jobs import sync_user_catalogs_off_loop

        try:
            if await sync_user_catalogs_off_loop(user_id):
                _publish_sync()
        except Exception as e:
            _sync_failed(e)

    def _do_sync_without_web_loop() -> None:
        from workers.isolated_jobs import sync_user_catalogs_isolated

        try:
            if sync_user_catalogs_isolated(user_id):
                _publish_sync()
        except Exception as exc:  # noqa: BLE001 - fire-and-forget fallback
            _sync_failed(exc)

    async def _guarded_sync():
        # Bound concurrency so a burst of syncs can't pin every pooled connection
        # at once. Each admitted job owns a private loop and database session.
        async with _local_sync_semaphore:
            await _do_sync()

    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(_guarded_sync())
        _local_sync_tasks.add(task)
        task.add_done_callback(_local_sync_tasks.discard)
        logger.info(f"Scheduled background local sync for user {user_id}")
    except RuntimeError:
        threading.Thread(
            target=_do_sync_without_web_loop,
            name=f"curatio-local-sync-{user_id}",
            daemon=True,
        ).start()
        logger.info(f"Scheduled threaded local sync for user {user_id}")


# Rate limiter
limiter = Limiter(key_func=get_client_ip)


# Initialize FastAPI app
@asynccontextmanager
async def app_lifespan(application: FastAPI):
    """Run application services and release every shared resource cleanly."""
    await startup_event()
    try:
        yield
    finally:
        await shutdown_event()


app = FastAPI(
    title="Curatio",
    description="AI-curated recommendations for your Plex and Jellyfin libraries",
    version="1.0.0",
    lifespan=app_lifespan,
)

app.state.limiter = limiter
BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
for _suffix, _mime in {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".m4s": "video/mp4",
    ".mp4": "video/mp4",
    ".vtt": "text/vtt",
}.items():
    mimetypes.add_type(_mime, _suffix)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_SLOW_REQUEST_SECONDS = 0.5
_EVENT_LOOP_WATCHDOG_INTERVAL = 0.5
_EVENT_LOOP_LAG_WARNING_SECONDS = 0.5


def _request_route_template(request: Request) -> str:
    """Resolve a stable route label without logging secret URL segments."""
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    if route_path:
        return str(route_path)

    endpoint = request.scope.get("endpoint")
    if endpoint is not None:
        for candidate in app.router.routes:
            if getattr(candidate, "endpoint", None) is endpoint:
                candidate_path = getattr(candidate, "path", None)
                if candidate_path:
                    return str(candidate_path)

    if request.url.path.startswith("/static/"):
        return "/static/{path:path}"
    return "<unmatched>"


@app.middleware("http")
async def slow_request_middleware(request: Request, call_next):
    """Log slow requests by route template with current DB pool pressure."""
    started = monotonic()
    response = None
    try:
        response = await call_next(request)
        return response
    finally:
        duration = monotonic() - started
        if duration >= _SLOW_REQUEST_SECONDS:
            pool = pool_occupancy()
            logger.warning(
                "slow_request "
                f"method={request.method} route={_request_route_template(request)} "
                f"status={getattr(response, 'status_code', 500)} "
                f"duration_ms={duration * 1000:.1f} "
                f"db_checked_out={pool['checked_out']} db_size={pool['size']} "
                f"db_overflow={pool['overflow']}"
            )


@app.middleware("http")
async def csrf_origin_guard(request: Request, call_next):
    """Reject cross-origin browser writes that rely on ambient cookies."""
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.cookies.get("user_session") or request.cookies.get("admin_token"):
            origin = request.headers.get("origin")
            if origin and origin not in _allowed_web_origins():
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": "Cross-origin credentialed requests are not allowed"
                    },
                )
    return await call_next(request)


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many requests. Please try again later."},
    )


# Compress documents, but never transform byte-range or streaming media responses.
app.add_middleware(SelectiveGZipMiddleware, minimum_size=500)


def _allowed_web_origins() -> list[str]:
    origin = (
        f"{urlparse(settings.base_url).scheme}://{urlparse(settings.base_url).netloc}"
    )
    return [origin] if origin != "://" else []


def _html_security_headers(request: Request) -> dict[str, str]:
    """Return baseline response headers for browser-facing HTML endpoints."""
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "X-Frame-Options": "DENY",
    }
    if _cookie_secure(request):
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    csp_header = (
        "Content-Security-Policy-Report-Only"
        if settings.security_csp_report_only
        else "Content-Security-Policy"
    )
    headers[csp_header] = settings.security_csp
    if request.url.path.startswith("/admin/addons/"):
        # Addon settings UIs are framed by the admin shell (same origin only).
        headers["X-Frame-Options"] = "SAMEORIGIN"
        headers[csp_header] = settings.security_csp.replace(
            "frame-ancestors 'none'", "frame-ancestors 'self'"
        )
    return headers


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    if "text/html" in response.headers.get("content-type", ""):
        for key, value in _html_security_headers(request).items():
            response.headers.setdefault(key, value)
        if request.url.path.startswith(("/account/", "/admin")):
            response.headers["Cache-Control"] = "private, no-store"
    if request.url.path.startswith("/static/") and "/assets/" in request.url.path:
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif request.url.path == "/static/auth.css":
        response.headers["Cache-Control"] = "public, max-age=86400"
    return response


#: Failed profile-API authentications allowed per client IP per window before
#: answering 429. A profile key is 32 random bytes, so this only stops noise.
_V2_AUTH_FAILURE_LIMIT = 30
_V2_AUTH_FAILURE_WINDOW = 60.0
_v2_auth_failures: dict[str, collections.deque[float]] = {}


def _v2_auth_failures_recent(ip: str, now: float) -> collections.deque[float]:
    window = _v2_auth_failures.setdefault(ip, collections.deque())
    while window and now - window[0] > _V2_AUTH_FAILURE_WINDOW:
        window.popleft()
    if not window:
        _v2_auth_failures.pop(ip, None)
    return window


@app.middleware("http")
async def profile_api_middleware(request: Request, call_next):
    """Bearer-authenticated ``/api/v2`` responses: throttle and keep private.

    The profile key arrives in ``Authorization``, so any cache must keep one
    profile's response away from another's: always ``Vary: Authorization`` and
    never ``public``.
    """
    if not request.url.path.startswith("/api/v2/"):
        return await call_next(request)
    ip = get_client_ip(request)
    now = monotonic()
    if len(_v2_auth_failures_recent(ip, now)) >= _V2_AUTH_FAILURE_LIMIT:
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests. Please try again later."},
        )
    response = await call_next(request)
    if response.status_code == 401:
        _v2_auth_failures.setdefault(ip, collections.deque()).append(now)
        if len(_v2_auth_failures) > 4096:
            _v2_auth_failures.pop(next(iter(_v2_auth_failures)))
    cache_control = response.headers.get("cache-control", "")
    if not cache_control:
        response.headers["Cache-Control"] = "private, no-cache"
    elif "public" in cache_control:
        response.headers["Cache-Control"] = cache_control.replace("public", "private")
    vary = response.headers.get("vary", "")
    if "authorization" not in vary.lower():
        response.headers["Vary"] = f"{vary}, Authorization" if vary else "Authorization"
    return response


# CORS middleware (native and desktop clients). Do not allow credentials with the
# wildcard; cookie-authenticated browser writes are protected by origin checks.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Playback: signed media URLs, Integrations admin API, installed addons.
# Registered before the admin portal, whose /admin/{path} SPA fallback would
# otherwise answer /admin/api/integrations/* and /admin/addons/*.
from app import media_proxy  # noqa: E402
from app.sources import wiring as source_wiring  # noqa: E402

app.include_router(media_proxy.router)
source_wiring.install(app)

# Mount admin portal
app.include_router(admin_router)

# Mount account pages
app.include_router(account_router)

app.include_router(home_feed_router)


async def _event_loop_watchdog() -> None:
    """Report Uvicorn loop stalls with a simultaneous DB-pool snapshot."""
    loop = asyncio.get_running_loop()
    expected = loop.time() + _EVENT_LOOP_WATCHDOG_INTERVAL
    logger.info(
        "Event-loop watchdog started "
        f"(warning={_EVENT_LOOP_LAG_WARNING_SECONDS:.1f}s)"
    )
    try:
        while True:
            await asyncio.sleep(_EVENT_LOOP_WATCHDOG_INTERVAL)
            now = loop.time()
            lag = max(0.0, now - expected)
            if lag >= _EVENT_LOOP_LAG_WARNING_SECONDS:
                pool = pool_occupancy()
                logger.warning(
                    "event_loop_lag "
                    f"lag_ms={lag * 1000:.1f} "
                    f"db_checked_out={pool['checked_out']} db_size={pool['size']} "
                    f"db_overflow={pool['overflow']}"
                )
            # Reset from the actual wake time so one stall produces one useful
            # measurement instead of a long series of cumulative warnings.
            expected = now + _EVENT_LOOP_WATCHDOG_INTERVAL
    except asyncio.CancelledError:
        logger.info("Event-loop watchdog cancelled, shutting down")
        raise


async def startup_event():
    """Initialize application on startup."""
    logger.info("Starting Curatio...")
    logger.info(
        "source_revision {}",
        os.getenv("CURATIO_GIT_REVISION", "unknown").strip() or "unknown",
    )

    # Synchronous DB work is offloaded with asyncio.to_thread, whose default
    # executor is sized from the CPU count (8 here) — far below the connection
    # pool. Left at the default, offloaded queries queue on threads instead of
    # on the database, which is the stall the offloading exists to avoid. This
    # executor is for request-path work only: background jobs hold a thread for
    # minutes and run on app.job_threads' executor and the job engine instead.
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(
            max_workers=settings.db_pool_size,
            thread_name_prefix="curatio-sync",
        )
    )

    # Validate configuration
    try:
        validate_api_keys()
        logger.info("Configuration validated")
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        if not settings.skip_api_validation:
            raise

    # Initialize database
    try:
        init_database()
        logger.info("Database initialized")
        logger.info(f"database_pool {pool_occupancy()}")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise

    # Load admin settings from database (overrides env vars)
    load_settings_from_db()

    # Name the provider/model actually in use. Between .env, admin_settings and the
    # code default there are three places this can come from, and a retired id
    # 404s on every call -- which is how tagging died silently for two months.
    # One log line makes the effective value checkable without guessing.
    try:
        from app.tagging_provider import (
            get_tagging_engine,
            normalize_provider,
            provider_label,
        )

        tagging_engine = get_tagging_engine()
        provider = normalize_provider()
        logger.info(
            f"Tagging provider in use: {provider_label(provider)} / "
            f"{tagging_engine.model_name}"
        )
    except Exception as exc:  # noqa: BLE001 - never block startup on this
        logger.warning(f"Could not confirm the tagging provider/model: {exc}")

    # Auto-resume interrupted builds (container restart recovery)
    from app.admin import auto_resume_build

    await auto_resume_build()

    def _log_task_exit(name: str):
        def _cb(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.error(f"{name} exited unexpectedly: {exc!r}")

        return _cb

    # Start daily update scheduler if enabled
    if settings.daily_update_enabled:
        from app.scheduler import run_scheduler

        app.state.scheduler_task = asyncio.create_task(run_scheduler())
        app.state.scheduler_task.add_done_callback(
            _log_task_exit("Daily update scheduler")
        )
        logger.info("Daily update scheduler enabled")
    else:
        app.state.scheduler_task = None
        logger.info("Daily update scheduler disabled (set DAILY_UPDATE_ENABLED=true)")

    # Personal rows are a local, zero-AI computation and must keep cycling even
    # when the paid acquisition/tagging scheduler is intentionally disabled.
    # The full scheduler already performs this sync after its update, so only
    # start the independent loop when that scheduler is off.
    if settings.enable_personalized_sync and not settings.daily_update_enabled:
        from app.scheduler import run_personalized_scheduler

        app.state.personal_scheduler_task = asyncio.create_task(
            run_personalized_scheduler()
        )
        app.state.personal_scheduler_task.add_done_callback(
            _log_task_exit("Personal catalog scheduler")
        )
    else:
        app.state.personal_scheduler_task = None

    # Periodic maintenance (reap expired sessions/challenges/auth-codes, requeue
    # stale fingerprint jobs) runs regardless of the daily-update setting.
    from app.scheduler import run_maintenance

    app.state.maintenance_task = asyncio.create_task(run_maintenance())
    app.state.maintenance_task.add_done_callback(_log_task_exit("Maintenance loop"))

    from app.home_feed import run_home_feed_scheduler

    app.state.home_feed_task = asyncio.create_task(run_home_feed_scheduler())
    app.state.home_feed_task.add_done_callback(
        _log_task_exit("Home membership scheduler")
    )

    app.state.event_loop_watchdog_task = asyncio.create_task(_event_loop_watchdog())
    app.state.event_loop_watchdog_task.add_done_callback(
        _log_task_exit("Event-loop watchdog")
    )

    # Complete native details are a serving read model, not part of the paid
    # tagging pipeline. Warm them even when the daily content scheduler is off.
    if settings.detail_snapshot_warm_enabled:
        from app.scheduler import run_detail_snapshot_scheduler

        app.state.detail_snapshot_warmer_task = asyncio.create_task(
            run_detail_snapshot_scheduler()
        )
        app.state.detail_snapshot_warmer_task.add_done_callback(
            _log_task_exit("Detail snapshot scheduler")
        )
    else:
        app.state.detail_snapshot_warmer_task = None
        logger.info("Detail snapshot scheduler disabled")

    await source_wiring.startup()

    logger.info(f"Curatio ready at {settings.base_url}")


async def shutdown_event():
    """Cleanup on shutdown."""
    logger.info("Shutting down Curatio...")

    if getattr(app.state, "home_feed_task", None) is not None:
        app.state.home_feed_task.cancel()
        try:
            await app.state.home_feed_task
        except asyncio.CancelledError:
            pass

    # Foreground profile jobs use the same cancellation bridge as scheduled
    # jobs. Drain them before closing shared clients so every private loop gets
    # a chance to close its own TMDB pool first.
    if _local_sync_tasks:
        active_local_syncs = list(_local_sync_tasks)
        for task in active_local_syncs:
            task.cancel()
        await asyncio.gather(*active_local_syncs, return_exceptions=True)
        logger.info(f"Stopped {len(active_local_syncs)} foreground profile sync(s)")

    # Cancel scheduler if running
    if getattr(app.state, "scheduler_task", None) is not None:
        app.state.scheduler_task.cancel()
        try:
            await app.state.scheduler_task
        except asyncio.CancelledError:
            pass
        logger.info("Scheduler stopped")

    if getattr(app.state, "personal_scheduler_task", None) is not None:
        app.state.personal_scheduler_task.cancel()
        try:
            await app.state.personal_scheduler_task
        except asyncio.CancelledError:
            pass
        logger.info("Personal catalog scheduler stopped")

    # Cancel the maintenance loop
    if getattr(app.state, "maintenance_task", None) is not None:
        app.state.maintenance_task.cancel()
        try:
            await app.state.maintenance_task
        except asyncio.CancelledError:
            pass
        logger.info("Maintenance loop stopped")

    if getattr(app.state, "event_loop_watchdog_task", None) is not None:
        app.state.event_loop_watchdog_task.cancel()
        try:
            await app.state.event_loop_watchdog_task
        except asyncio.CancelledError:
            pass
        logger.info("Event-loop watchdog stopped")

    if getattr(app.state, "detail_snapshot_warmer_task", None) is not None:
        app.state.detail_snapshot_warmer_task.cancel()
        try:
            await app.state.detail_snapshot_warmer_task
        except asyncio.CancelledError:
            pass
        logger.info("Detail snapshot scheduler stopped")

    # Close HTTP clients so connections don't leak
    from app.tmdb_client import tmdb_client

    await tmdb_client.close()
    await source_wiring.shutdown()
    await remux_manager.shutdown()
    try:
        if _proxy_client is not None and not _proxy_client.is_closed:
            await _proxy_client.aclose()
    except Exception:
        pass
    logger.info("HTTP clients closed")


@app.get("/", response_class=HTMLResponse)
async def root(request: Request, db: Session = Depends(get_db_dependency)):
    """Public welcome page; signed-in viewers go directly to their dashboard."""
    token = request.cookies.get("user_session")
    if token:
        account = verify_user_session(token, db)
        if account and account.is_active:
            return RedirectResponse(url="/account/dashboard", status_code=303)
    response = HTMLResponse(content=welcome_page_html())
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _web_dashboard_html() -> str:
    """Return the built authenticated React dashboard shell."""
    index_path = STATIC_DIR / "dashboard" / "index.html"
    if index_path.exists():
        return index_path.read_text(encoding="utf-8")
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Curatio Dashboard</title>
<style>
body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #020203; color: #fff; font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif; }
main { width: min(32rem, calc(100vw - 2rem)); padding: 1.5rem; border: 1px solid rgba(255,255,255,.14); border-radius: 12px; background: rgba(255,255,255,.07); }
h1 { margin: 0 0 .7rem; font-size: 1.6rem; }
p { margin: 0; color: rgba(255,255,255,.68); line-height: 1.5; }
</style>
</head>
<body>
<main>
<h1>Dashboard build missing</h1>
<p>Run <code>npm run build</code> to create the authenticated Curatio dashboard bundle.</p>
</main>
</body>
</html>"""


def _dashboard_csp() -> str:
    # Browser-native sources keep correct color and audio; no client remux path is wired here.
    return (
        "default-src 'self'; "
        "base-uri 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "img-src 'self' https: data: blob:; "
        "media-src 'self' https: blob: data:; "
        "connect-src 'self' https: wss: blob:; "
        # 'wasm-unsafe-eval' lets libav.js compile its WebAssembly build — without
        # it the loader's wasm feature-detect fails on the main thread and it
        # silently falls back to the far slower asm.js variant.
        "script-src 'self' 'wasm-unsafe-eval' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; "
        "worker-src 'self' blob:;"
    )


@app.get("/account/dashboard", response_class=HTMLResponse)
def account_dashboard(request: Request, db: Session = Depends(get_db_dependency)):
    """Cookie-authenticated web dashboard with browser-native playback.

    Plain ``def`` so the synchronous session lookup runs in the threadpool and
    does not block the event loop.
    """
    try:
        account = _get_account_from_session(request, db, allow_pending_enrollment=True)
    except HTTPException:
        return RedirectResponse(
            url="/account/login?next=/account/dashboard", status_code=303
        )
    # A browser deserves a redirect to the setup page rather than a bare 403.
    requirement = auth_requirement(db, account)
    if requirement != "none":
        return RedirectResponse(
            url=_enrollment_url(requirement, "/account/dashboard"), status_code=303
        )
    response = HTMLResponse(content=_web_dashboard_html())
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Content-Security-Policy"] = _dashboard_csp()
    return response


@app.get("/account", response_class=HTMLResponse)
def account_home(request: Request, db: Session = Depends(get_db_dependency)):
    """Send signed-in users to the web app and guests to login.

    Plain ``def`` so the synchronous session lookup runs in the threadpool.
    """
    try:
        account = _get_account_from_session(request, db, allow_pending_enrollment=True)
    except HTTPException:
        return RedirectResponse(
            url="/account/login?next=/account/dashboard", status_code=303
        )
    requirement = auth_requirement(db, account)
    if requirement != "none":
        return RedirectResponse(
            url=_enrollment_url(requirement, "/account/dashboard"), status_code=303
        )
    return RedirectResponse(url="/account/dashboard", status_code=303)


@app.get("/health")
def health_check():
    """Health check endpoint.

    Uses ``def`` so the synchronous DB ping runs in a threadpool and
    does not block the event loop.
    """
    db_ok = check_database_connection()

    return {
        "status": "healthy" if db_ok else "degraded",
        "database": "connected" if db_ok else "disconnected",
        "version": "1.0.0",
    }


def _reorder_universals_by_taste(categories: list, user_id: int, db: Session) -> None:
    """Sort universal categories in-place by overlap with user's taste profile.

    Only called for local-auth users with local watch history.

    Scored as ``SUM(taste weight of the formula's tags) / sqrt(len(formula))``.
    Both halves matter and neither was here before:

    * **Weighted, not counted.** ``len(formula_tag_ids & taste_tag_ids)`` treated
      a shared "Drama" exactly like a shared "Neo-Noir", so with a Drama-heavy
      library every profile produced nearly the same ordering.
    * **Length-normalised.** An unnormalised sum lets a 4-tag formula outrank a
      2-tag one purely for having more tags to match, independent of taste.
    """
    import math

    # Read the weights from the same fingerprinted cache ``_get_user_taste_data``
    # fills. Both used to derive their own: the identical WatchEvent scan, the
    # identical ``completion_weights`` call and the identical pair of
    # ``build_taste_vector`` passes, run twice per dashboard build with only one
    # of the two cached. ``_dashboard_payload`` calls both.
    taste_weights = _get_user_taste_weights(user_id, db)
    if not taste_weights:
        return  # no history, or no tags on it — keep default order

    # Map tag names → IDs for scoring
    all_tag_names: set[str] = set()
    for cat in categories:
        formula = cat.tag_formula or {}
        for key in ("mandatory", "required", "optional"):
            all_tag_names.update(formula.get(key, []))

    tag_name_to_id = {
        t.name: t.id for t in db.query(Tag).filter(Tag.name.in_(all_tag_names)).all()
    }

    # Score each category by weighted taste overlap
    scores: dict[str, float] = {}
    for cat in categories:
        formula = cat.tag_formula or {}
        formula_tag_ids: set[int] = set()
        for key in ("mandatory", "required", "optional"):
            for name in formula.get(key, []):
                tid = tag_name_to_id.get(name)
                if tid is not None:
                    formula_tag_ids.add(tid)
        if not formula_tag_ids:
            scores[cat.id] = 0.0
            continue
        overlap = sum(taste_weights.get(tid, 0.0) for tid in formula_tag_ids)
        scores[cat.id] = overlap / math.sqrt(len(formula_tag_ids))

    categories.sort(key=lambda c: (-scores.get(c.id, 0.0), c.sort_order))


def _get_user_taste_data(user_id: int, db: Session) -> tuple[list[int], set[int]]:
    """Get taste tag IDs and watched TMDB IDs for a local user.

    Returns (taste_tag_ids, watched_tmdb_ids).
    """
    taste_tags, watched_set, _weights = _compute_user_taste(user_id, db)
    return list(taste_tags), set(watched_set)


def _get_user_taste_weights(user_id: int, db: Session) -> dict[int, float]:
    """The full ``{tag_id: weight}`` taste vector, merged across both media types.

    Same cache and same computation as :func:`_get_user_taste_data`; that one only
    needs the tag identities, ``_reorder_universals_by_taste`` needs the weights.
    """
    _tags, _watched, weights = _compute_user_taste(user_id, db)
    return dict(weights)


def _compute_user_taste(
    user_id: int, db: Session
) -> tuple[list[int], set[int], dict[int, float]]:
    """Build (and cache) a profile's taste identities, watched set, and weights.

    The two taste vectors are the expensive part -- each is an ``IN`` over every
    watched title against ``movie_tags`` -- so they are computed once here and
    both shapes are derived from them.

    ``build_taste_vector`` normalizes over *all* positive tags before truncating to
    ``top_n``, so a tag's weight does not depend on where the cut falls. Building at
    the vector's default 40 and slicing to 15 therefore yields byte-identical
    identities to the old ``build_taste_profile(..., top_n_tags=15)`` calls, while
    also leaving the full-width vector available.
    """
    watch_meta = (
        db.query(func.count(WatchEvent.id), func.max(WatchEvent.created_at))
        .filter(
            WatchEvent.user_id == user_id,
            WatchEvent.action.in_(TASTE_ACTIONS),
        )
        .first()
    )
    count = int(watch_meta[0] or 0) if watch_meta else 0
    max_created = watch_meta[1].isoformat() if watch_meta and watch_meta[1] else ""

    # WatchProgress has to be part of the fingerprint, not just WatchEvent.
    # Progress rows already move independently of completion events (a resumed
    # episode updates progress with no new event), so a cache keyed on
    # WatchEvent alone can serve a stale taste profile for the full TTL. This
    # also makes the cache correct ahead of progress-derived signal feeding the
    # taste vector, where it would otherwise never invalidate at all.
    progress_max = (
        db.query(func.max(WatchProgress.updated_at))
        .filter(WatchProgress.user_id == user_id)
        .scalar()
    )
    max_progress = progress_max.isoformat() if progress_max else ""
    fingerprint = (count, max_created, max_progress)
    now = time()
    cached = _taste_cache.get(user_id)
    if cached and now - cached[0] < _TASTE_CACHE_TTL and cached[1] == fingerprint:
        return cached[2]
    # Watched and "Not for me" ids in one round trip. Hiding a title drops this
    # profile's cache entry (_hidden_titles_changed), so the hidden set needs
    # no place in the fingerprint.
    watched_query = (
        db.query(WatchEvent.tmdb_id.label("tmdb_id"), literal("w").label("kind"))
        .filter(
            WatchEvent.user_id == user_id,
            WatchEvent.action.in_(TASTE_ACTIONS),
            WatchEvent.tmdb_id.isnot(None),
        )
        .distinct()
    )
    hidden_query = db.query(
        HiddenTitle.tmdb_id.label("tmdb_id"), literal("h").label("kind")
    ).filter(HiddenTitle.user_id == user_id)
    watched_ids: list[int] = []
    hidden_ids: list[int] = []
    for tmdb_id, kind in watched_query.union_all(hidden_query).all():
        (hidden_ids if kind == "h" else watched_ids).append(int(tmdb_id))
    watched_ids = list(dict.fromkeys(watched_ids))
    # "Not for me" titles are excluded like watched ones and, below, count
    # against their tags.
    watched_set = set(watched_ids) | set(hidden_ids)

    if not watched_ids:
        empty_result: tuple[list[int], set[int], dict[int, float]] = (
            [],
            watched_set,
            {},
        )
        _taste_cache[user_id] = (now, fingerprint, empty_result)
        return empty_result

    # The cache fingerprint above already tracks WatchProgress, which is what
    # makes these factors safe to read here -- abandoning a title changes the
    # profile, and the cache now notices.
    from app import relevance as _relevance

    factors = _relevance.completion_weights(db, user_id)
    weighted = [(tid, factors.get(tid, 1.0)) for tid in watched_ids]
    weighted += [
        (tid, factors.get(tid, -0.5)) for tid in hidden_ids if tid not in watched_ids
    ]

    vectors = {
        media_type: _relevance.build_taste_vector(db, weighted, media_type)
        for media_type in ("movie", "tv")
    }

    # Merged weights keep the max rather than the sum where a tag appears in both,
    # so a tag that is strong for one media type is not diluted by being weak in
    # the other.
    taste_weights: dict[int, float] = {}
    for vector in vectors.values():
        for tag_id, weight in vector.items():
            taste_weights[tag_id] = max(taste_weights.get(tag_id, 0.0), weight)

    def _top_ids(vector: dict[int, float], count: int) -> list[int]:
        return [
            tag_id
            for tag_id, _ in sorted(vector.items(), key=lambda kv: -kv[1])[:count]
        ]

    seen: set[int] = set()
    taste_tags: list[int] = []
    for tid in _top_ids(vectors["movie"], 15) + _top_ids(vectors["tv"], 15):
        if tid not in seen:
            seen.add(tid)
            taste_tags.append(tid)

    result = (taste_tags, watched_set, taste_weights)
    _taste_cache[user_id] = (now, fingerprint, result)
    return result


def _rotation_offset(
    pool_size: int,
    today: date | None = None,
    window: int = 1,
    *,
    rotation_days: int | None = None,
) -> int:
    """How far to rotate the generated pool for the current period.

    Derived from the date, so every request on a given day agrees without any
    stored state or scheduled job, and the lineup changes on its own.

    **Steps by a whole window, not by one row.** The first version advanced the
    offset by 1 per period, which sounds like rotation and is not: against a
    ~140-row pool showing 20 at a time, one period swapped a single shelf and a
    full pass took 140 periods -- 980 days at the default week. Stepping by the
    window size means each period shows a genuinely different set and the pool
    is exhausted in ``ceil(pool / window)`` periods, which is what "the lineup
    changes" was always supposed to mean.
    """
    period = max(
        0, settings.synth_rotation_days if rotation_days is None else rotation_days
    )
    if period == 0 or pool_size <= 0:
        return 0
    day = (today or utc_now().date()).toordinal()
    return ((day // period) * max(1, window)) % pool_size


def _universal_row_categories(
    db: Session,
    today: date | None = None,
    *,
    limit: int | None = None,
    enabled: bool | None = None,
    rotation_days: int | None = None,
) -> list:
    """Active synthesized categories, capped and rotated for the home screen.

    The static curated rows remain stored but are not shown; only generated
    rows are returned. Profile-specific rows are added separately.

    **The surplus rotates rather than sitting unseen.** Synthesis produces far
    more rows than a home screen can carry, so pinning the highest-lift ones
    permanently would mean most of what it found is never surfaced. Every
    ``synth_rotation_days`` the generated pool is rotated, so the lineup changes.

    Two properties this deliberately preserves:

    * **Nothing is deactivated.** A row that rotates out of the window is still
      stored and can rotate back in; rotation is purely a selection.
    * **Ordering inside the cut is still ``sort_order``**, so synthesis rank is
      preserved inside each rotating selection.
    """
    if not (settings.enable_universal_catalogs if enabled is None else enabled):
        # The toggle exists in the README, in .env.example and in the admin
        # dashboard; until now it was not a field on Settings at all, so it
        # wrote a row nothing read and rendered OFF while the feature ran.
        # Gating here covers every surface: the web dashboard and the native
        # home feed both cut their universal rows from this function.
        return []
    limit = (
        MAX_NATIVE_DISCOVERY_ROWS
        if limit is None
        else min(MAX_NATIVE_DISCOVERY_ROWS, max(1, limit))
    )
    dense_categories = (
        db.query(UniversalCatalogContent.category_id)
        .group_by(UniversalCatalogContent.category_id)
        .having(func.count(UniversalCatalogContent.tmdb_id) >= MIN_VISIBLE_ROW_ITEMS)
        .subquery()
    )
    active = (
        db.query(UniversalCategory)
        .join(
            dense_categories,
            dense_categories.c.category_id == UniversalCategory.id,
        )
        .filter(
            UniversalCategory.is_active.is_(True),
            UniversalCategory.source == "generated",
        )
        .order_by(
            # Highest synthesis priority first for the rotating cut.
            UniversalCategory.manifest_priority.desc(),
            UniversalCategory.sort_order,
        )
        .all()
    )

    if active:
        offset = _rotation_offset(
            len(active), today, window=limit, rotation_days=rotation_days
        )
        # Rotate the pool, then take the window. Ordering within the pool is
        # still best-lift-first, so a rotation shows a different slice of good
        # rows rather than a random one.
        rotated = active[offset:] + active[:offset]
        active = rotated[:limit]
    else:
        active = active[:limit]

    active.sort(key=lambda c: c.sort_order)
    return active


def _coming_soon_date(
    item: dict, media_type: str | None = None, today: str | None = None
) -> str | None:
    """Return the date a catalog item becomes watchable, or None.

    Movies use the TMDB digital (type 4) release date; series use the next episode's
    air date. One scalar covers both types so the client never has to derive anything
    from a full ``videos[]`` list.

    Dates already past are suppressed, so a client never has to decide whether one is
    still upcoming — and a series date that went stale between refreshes degrades to
    silence rather than a wrong badge.

    Today (UTC) still counts as upcoming, and that boundary is load-bearing: a client
    treats a title as released at 12:00am *in the viewer's own zone*, which for
    anywhere west of UTC falls partway through this UTC day. Tightening this to ``>``
    would strip the date out from under those clients while they are still counting
    down. Clients re-gate against their own midnight, so keeping it a day longer than
    strictly needed costs nothing; dropping it early cannot be recovered.
    """
    if (media_type or item.get("media_type")) == "tv":
        date = item.get("next_episode_air_date")
    else:
        date = item.get("digital_release_date")
    if not date:
        return None
    today = today or utc_now().strftime("%Y-%m-%d")
    return str(date)[:10] if str(date)[:10] >= today else None


# ---- Meta cache (avoids repeated TMDB calls for popular items) ----
class _WriteOrderedCache(OrderedDict):
    """Dict whose iteration order is last-write order, so eviction is O(1)."""

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.move_to_end(key)


_meta_cache: "OrderedDict[str, tuple[float, dict | list]]" = _WriteOrderedCache()
_META_CACHE_TTL = 3600  # 1 hour
# Up to four keys per title (web-detail, detail-core, detail-hero, meta); 512
# kept only ~130 titles warm and sent the rest back to snapshot decodes.
_META_CACHE_MAX = settings.meta_cache_max_entries

# Provider snapshots stay stable for hours/days, while the Curatio overlay below
# (tags, ratings, reviews, and vector similarity) is database-derived. Snapshots
# retain the last complete overlay so a process restart can still serve one complete
# payload immediately. This compact memory cache is refreshed in the background;
# keeping only overlay fields avoids duplicating large TV season/episode bodies.
_detail_response_cache: dict[tuple[str, int], tuple[float, dict]] = {}
_DETAIL_RESPONSE_CACHE_TTL = settings.detail_dynamic_overlay_ttl_seconds
_DETAIL_RESPONSE_CACHE_MAX = 512
_detail_response_cache_lock = threading.Lock()
_detail_force_refresh_claims: dict[tuple[str, int], float] = {}
_detail_force_refresh_lock = threading.Lock()


def _detail_response_cache_get(tmdb_id: int, media_type: str) -> dict | None:
    key = (media_type, tmdb_id)
    now = monotonic()
    with _detail_response_cache_lock:
        cached = _detail_response_cache.get(key)
        if cached is None:
            return None
        if now - cached[0] >= _DETAIL_RESPONSE_CACHE_TTL:
            _detail_response_cache.pop(key, None)
            return None
        return copy.deepcopy(cached[1])


def _detail_response_cache_store(tmdb_id: int, media_type: str, payload: dict) -> None:
    key = (media_type, tmdb_id)
    with _detail_response_cache_lock:
        _detail_response_cache[key] = (monotonic(), copy.deepcopy(payload))
        while len(_detail_response_cache) > _DETAIL_RESPONSE_CACHE_MAX:
            oldest_key = min(
                _detail_response_cache, key=lambda item: _detail_response_cache[item][0]
            )
            _detail_response_cache.pop(oldest_key, None)


def clear_detail_response_cache() -> None:
    """Invalidate composed details after metadata/catalog mutations."""
    with _detail_response_cache_lock:
        _detail_response_cache.clear()
    with _detail_force_refresh_lock:
        _detail_force_refresh_claims.clear()
    # `_detail_response_cache` holds only the dynamic overlay (tags, ratings,
    # similar…). The episode bodies live in `_meta_cache` for an hour under these
    # separate namespaces, so clearing the overlay alone left a stale — possibly
    # episode-less — payload serving for the rest of its TTL.
    for key in [
        key
        for key in _meta_cache
        if key.startswith(
            ("web-detail:", "meta:", "detail-core:", "detail-hero:", "detail-type:")
        )
    ]:
        _meta_cache.pop(key, None)


def _detail_response_cache_remove(tmdb_id: int, media_type: str) -> None:
    with _detail_response_cache_lock:
        _detail_response_cache.pop((media_type, tmdb_id), None)


def _claim_detail_force_refresh(tmdb_id: int, media_type: str) -> bool:
    """Bound explicit retries so one client cannot hammer the provider."""
    key = (media_type, tmdb_id)
    now = monotonic()
    with _detail_force_refresh_lock:
        previous = _detail_force_refresh_claims.get(key)
        if (
            previous is not None
            and now - previous < _DETAIL_FORCE_REFRESH_COOLDOWN_SECONDS
        ):
            return False
        _detail_force_refresh_claims[key] = now
        if len(_detail_force_refresh_claims) > _DETAIL_RESPONSE_CACHE_MAX:
            oldest_key = min(
                _detail_force_refresh_claims,
                key=_detail_force_refresh_claims.__getitem__,
            )
            _detail_force_refresh_claims.pop(oldest_key, None)
        return True


def _release_detail_force_refresh(tmdb_id: int, media_type: str) -> None:
    """Drop a claim whose rebuild did not produce a usable payload.

    The cooldown exists to stop one client hammering the provider, but it is
    global, so a claim that produced nothing would silently downgrade the *next*
    user's Retry to a cached read of the same broken payload. A rebuild only
    earns the cooldown by succeeding.
    """
    with _detail_force_refresh_lock:
        _detail_force_refresh_claims.pop((media_type, tmdb_id), None)


_DETAIL_DYNAMIC_FIELDS = (
    "tags",
    "ratings",
    "similar",
    "similar_source",
    "reviews",
    "review_summary",
    "tagline",
    "status",
    "original_language",
    "homepage",
    "content_rating",
    "content_advisory",
    "keywords",
    "trailers",
    "studios",
    "networks",
    "countries",
)


def _dated_movie_release_block(detail: dict) -> dict | None:
    """Return a release_dates block only when it contains dated entries.

    Sparse fixtures and provider fallbacks can carry certifications in this block
    without carrying release history. Those are not authoritative enough to clear
    a stored date.
    """
    block = detail.get("release_dates")
    if not isinstance(block, dict):
        return None
    for region in block.get("results") or []:
        if not isinstance(region, dict):
            continue
        if any(
            isinstance(entry, dict) and entry.get("release_date")
            for entry in region.get("release_dates") or []
        ):
            return block
    return None


def _apply_detail_dynamic_overlay(payload: dict, overlay: dict) -> dict:
    # Shallow by design: deep-copying a full series payload (every episode, the
    # whole provider detail) cost tens to hundreds of milliseconds of loop time
    # per hit. Cached payloads are never mutated below the top level, overlays
    # arrive already deep-copied from their caches, and `item` -- the one nested
    # dict edited here -- is copied before it is touched.
    refreshed = dict(payload)
    for key in _DETAIL_DYNAMIC_FIELDS:
        if key in overlay:
            refreshed[key] = overlay[key]
    item = refreshed.get("item")
    item_overlay = overlay.get("_item")
    if isinstance(item, dict) and isinstance(item_overlay, dict):
        item = refreshed["item"] = dict(item)
        item["content_rating"] = item_overlay.get("content_rating")
        if item_overlay.get("comingSoonDate"):
            item["comingSoonDate"] = item_overlay["comingSoonDate"]
        else:
            item.pop("comingSoonDate", None)
        if "digitalReleaseInfo" in item_overlay:
            if item_overlay["digitalReleaseInfo"]:
                item["digitalReleaseInfo"] = item_overlay["digitalReleaseInfo"]
            else:
                item.pop("digitalReleaseInfo", None)
    return refreshed


def _detail_dynamic_overlay(payload: dict) -> dict:
    overlay = {
        key: copy.deepcopy(payload[key])
        for key in _DETAIL_DYNAMIC_FIELDS
        if key in payload
    }
    item = payload.get("item")
    if isinstance(item, dict):
        item_overlay = {
            "content_rating": item.get("content_rating"),
            "comingSoonDate": item.get("comingSoonDate"),
        }
        provider_detail = payload.get("_provider_detail")
        if (
            item.get("media_type") == "movie"
            and isinstance(provider_detail, dict)
            and (release_block := _dated_movie_release_block(provider_detail))
        ):
            from app.tmdb_client import tmdb_client

            item_overlay["digitalReleaseInfo"] = tmdb_client.digital_release_date(
                release_block
            )
        overlay["_item"] = item_overlay
    return overlay


def _detail_payload_from_cached_overlay(
    payload: dict, tmdb_id: int, media_type: str
) -> tuple[dict, bool]:
    """Return a complete payload immediately and flag a cold overlay for refresh.

    The persistent snapshot already contains the last complete Curatio overlay.
    On a process-local cache miss, seed from that snapshot instead of synchronously
    re-running vector similarity. The caller schedules a stale-while-revalidate
    refresh without delaying this response.
    """
    overlay = _detail_response_cache_get(tmdb_id, media_type)
    if overlay is not None:
        return _apply_detail_dynamic_overlay(payload, overlay), False

    # Do not mark an overlay with unknown/expired persisted freshness as fresh
    # merely because it was served. This lets a later interactive request retry
    # after a failed refresh, while batch prefetch can return the stored overlay
    # without creating database work.
    overlay = _detail_dynamic_overlay(payload)
    return _apply_detail_dynamic_overlay(payload, overlay), True


def _meta_cache_evict():
    """Evict the oldest-written entries when meta cache exceeds max size."""
    while len(_meta_cache) > _META_CACHE_MAX:
        _meta_cache.popitem(last=False)


# ---- LRU catalog cache with TTL ----
_CACHE_MAX_ENTRIES = 256
_catalog_cache: dict[str, tuple[float, list]] = {}

# ---- User-key → active user id lookup cache (avoids DB hit on every request) ----
_user_cache: dict[str, tuple[float, "int | None"]] = {}
_USER_CACHE_TTL = 300  # 5 minutes
_USER_CACHE_MAX = 512

# ---- Discovery response caching ----
#
# Facets and browse were the only native endpoints with no cache directive at
# all, so a client could not reuse either even for the back-button. Both are
# authenticated per profile but their *results* are not profile-scoped -- both
# read only the shared tag/relevance tables and use the caller solely for the
# auth check.
#
# Client-side only, deliberately: a server-side memo would have to invalidate on
# a relevance rebuild, and keying it on a `tag_stats` fingerprint costs a probe
# query per request -- most of what the memo would save, on an endpoint that is
# already a few hundred pre-aggregated rows. `private` because the URL's first
# path segment is the profile credential, and these windows are short enough
# that a rebuild surfaces within a minute or two.
_FACETS_CACHE_CONTROL = "private, max-age=300"
_BROWSE_CACHE_CONTROL = "private, max-age=60"

# Local taste-profile cache, keyed by user_id alone so its size is bounded by the
# number of profiles (never grows per watch-count transition). The stored
# (count, max_created) fingerprint invalidates the slot when imports/scrobbles land,
# so a changed count overwrites the same entry instead of leaking a new one.
_TASTE_CACHE_TTL = 300
_taste_cache: dict[
    int,
    tuple[float, tuple, tuple[list[int], set[int], dict[int, float]]],
] = {}

# Short-lived assembled dashboard cache. Authentication is still resolved on
# every bootstrap request, but a visible tab polling/reloading within this
# window does not rebuild every shelf. Mutating viewer endpoints evict the
# affected profile immediately, and last_sync is part of the key so a completed
# catalog rebuild cannot return a pre-sync page.
_DASHBOARD_CACHE_TTL = 10.0
_DASHBOARD_CACHE_MAX = 1024
_dashboard_cache: dict[tuple, tuple[float, dict]] = {}


def _invalidate_dashboard_cache(user_id: int | None = None) -> None:
    if user_id is None:
        _dashboard_cache.clear()
        return
    for key in [key for key in _dashboard_cache if key[0] == user_id]:
        _dashboard_cache.pop(key, None)


WEB_DASHBOARD_ROW_ITEM_LIMIT = 40
# Native clients mirror this value with `CatalogRowLimits.maximumItems`.
NATIVE_DASHBOARD_ROW_ITEM_LIMIT = 80
WEB_DASHBOARD_CANDIDATE_LIMIT = 120

# Native iOS/tvOS uses the paged bootstrap endpoint and can carry a deep home
# in a couple of bounded pages.
MAX_NATIVE_DISCOVERY_ROWS = 40
NATIVE_BOOTSTRAP_PAGE_SIZE = 24

# Name used by page-assembly callers and focused tests.
MAX_DISCOVERY_ROWS = MAX_NATIVE_DISCOVERY_ROWS

#: How many rows on one page may show the same title.
#:
#: Rows are generated independently -- a curated formula, a taste vector and a
#: TMDB feed can all legitimately select the same film -- and nothing ever
#: reconciled them, so a single title could occupy Trending, Top Picks,
#: Recommended, a taste row and two universal rows on one screen. The page then
#: reads as a handful of films shown six ways, which is the complaint even when
#: every individual row is correct.
#:
#: One appearance makes cross-row deduplication literal. The oversized candidate
#: pools let a later row replace repeated titles before it is judged on density.
MAX_ROW_APPEARANCES = 1

#: Suppress rows whose leading contents are effectively the same concept.
#:
#: Exact title deduplication alone can leave two shelves alive by filling the
#: later one with its long tail. The viewer still sees two names for the same
#: idea. Comparing the untouched row heads before title claims are spent
#: catches that case while retaining rows with genuinely different heads.
MAX_ROW_HEAD_JACCARD = 0.70

#: A shelf this assembly pass has thinned below this is not worth rendering.
#:
#: Applied after actual request-time filtering and cross-row deduplication. A
#: row that arrived short is still weak and is suppressed. Short activity data
#: is returned in the separate Continue Watching/history collections instead.
MIN_VISIBLE_ROW_ITEMS = 24


def _row_item_keys(items: list[dict], limit: int = WEB_DASHBOARD_ROW_ITEM_LIMIT) -> set:
    """Stable title identities for duplicate-row comparison."""
    return {
        (item.get("media_type"), item.get("tmdb_id"))
        for item in items[:limit]
        if item.get("tmdb_id") is not None
    }


def _normalized_row_name(row: dict) -> str:
    """Case/punctuation-insensitive row name used at the final boundary."""
    return re.sub(r"[^a-z0-9]+", " ", str(row.get("name") or "").lower()).strip()


def _row_content_is_duplicate(sample: set, prior_samples: list[set]) -> bool:
    """Whether a row's original visible head is over 70% the same as a kept row."""
    if not sample:
        return False
    for prior in prior_samples:
        union = sample | prior
        if union and len(sample & prior) / len(union) > MAX_ROW_HEAD_JACCARD:
            return True
    return False


def _assemble_page(
    rows: list[dict],
    item_limit: int = WEB_DASHBOARD_ROW_ITEM_LIMIT,
    min_items: int | None = None,
) -> list[dict]:
    """Drop over-repeated titles across a page of rows, in row order.

    This is the stage the row layer never had. Retrieval and ranking both exist
    per row; nothing looked at the page as a whole, so diversity was whatever
    the independently-built rows happened to produce.

    Greedy and order-dependent, which is the point: rows arrive most-personal
    first (``SLOT_ORDER``, then the taste-ordered universal rows), so an earlier
    row keeps its claim on a title and the later, more generic row gives it up.
    A row below :data:`MIN_VISIBLE_ROW_ITEMS` is dropped entirely rather than
    rendered as a stub. Exact-name duplicates and rows with over 70% Jaccard
    overlap in their original visible heads are also dropped before they can consume
    title claims.

    Mutates each row's ``items`` in place and returns the surviving rows;
    callers holding the same row dicts in other lists (the movie/series splits)
    see the same edit, which is why those lists are rebuilt from the result
    rather than filtered separately.
    """
    floor = MIN_VISIBLE_ROW_ITEMS if min_items is None else min_items
    seen: dict[tuple, int] = {}
    kept: list[dict] = []
    kept_names: set[str] = set()
    kept_samples: list[set] = []
    discovery_rows = 0
    for row in rows:
        original = row.get("items") or []
        if len(original) < floor:
            continue
        name_key = _normalized_row_name(row)
        sample = _row_item_keys(original, limit=item_limit)
        if (name_key and name_key in kept_names) or _row_content_is_duplicate(
            sample, kept_samples
        ):
            continue
        surviving = []
        for item in original:
            key = (item.get("media_type"), item.get("tmdb_id"))
            if key[1] is None:
                surviving.append(item)
                continue
            count = seen.get(key, 0)
            if count >= MAX_ROW_APPEARANCES:
                continue
            seen[key] = count + 1
            surviving.append(item)
            if len(surviving) >= item_limit:
                break

        if len(surviving) < floor:
            # Only rows that survive spend their claims. A row dropped here must
            # release the titles it had taken, or it would thin the next row on
            # behalf of a shelf nobody ever sees.
            for item in surviving:
                key = (item.get("media_type"), item.get("tmdb_id"))
                if key[1] is not None and key in seen:
                    seen[key] -= 1
                    if seen[key] <= 0:
                        del seen[key]
            continue
        if discovery_rows >= MAX_DISCOVERY_ROWS:
            for item in surviving:
                key = (item.get("media_type"), item.get("tmdb_id"))
                if key[1] is not None and key in seen:
                    seen[key] -= 1
                    if seen[key] <= 0:
                        del seen[key]
            continue
        row["items"] = surviving
        row["item_count"] = len(surviving)
        kept.append(row)
        if name_key:
            kept_names.add(name_key)
        kept_samples.append(sample)
        discovery_rows += 1
    return kept


def _user_cache_evict():
    while len(_user_cache) > _USER_CACHE_MAX:
        oldest_key = min(_user_cache, key=lambda k: _user_cache[k][0])
        del _user_cache[oldest_key]


def _cache_evict():
    """Evict oldest entries when cache exceeds max size."""
    while len(_catalog_cache) > _CACHE_MAX_ENTRIES:
        oldest_key = min(_catalog_cache, key=lambda k: _catalog_cache[k][0])
        del _catalog_cache[oldest_key]


def _ttl_cache_evict(cache: dict, max_entries: int) -> None:
    """Trim a ``{key: (inserted_at, value)}`` cache back under its cap.

    Drops a batch rather than one entry per insert: the single-eviction loops
    elsewhere in this file re-scan the whole dict on *every* write once the cache
    is full, which is O(n) per request forever. Taking 10% at a time amortizes
    that to one scan per n/10 writes.
    """
    if len(cache) <= max_entries:
        return
    overflow = len(cache) - max_entries
    batch = max(overflow, max_entries // 10)
    for key in sorted(cache, key=lambda k: cache[k][0])[:batch]:
        cache.pop(key, None)


def _cache_ids_sig(ids) -> str:
    """Stable short signature of a set/list of ids for use in a cache key.

    Order-independent (sorted) so the same logical set always maps to the same
    key. Returns "0" for empty/None.
    """
    if not ids:
        return "0"
    joined = ",".join(str(i) for i in sorted(ids))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]


def _get_shuffle_seed(catalog_id: str, hours: int | None = None) -> int:
    """Deterministic shuffle seed based on catalog ID and current time window."""
    hours = settings.catalog_shuffle_hours if hours is None else hours
    if hours <= 0:
        return 0  # shuffle disabled
    window = int(time()) // (hours * 3600)
    raw = f"{catalog_id}:{window}"
    return int(hashlib.md5(raw.encode()).hexdigest()[:8], 16)


def _shuffle_items(
    items: list, catalog_id: str, *, shuffle_hours: int | None = None
) -> list:
    """Vary a row's order over time without discarding its ranking.

    **Only equally-scored titles trade places.** ``shuffle_within_tiers`` groups
    items whose ``match_score`` matches to two decimal places and shuffles
    within each group, leaving the groups in order -- so a better-matched title
    never falls below a worse one, while ties break differently each window.
    The previous version called ``random.shuffle`` on the whole 200-item list,
    throwing away the blended score and the taste sort that
    ``get_catalog_content`` had computed moments earlier in the same request.

    **Personal rows are left alone entirely.** Their variation already happens
    at build time: the nightly sync tier-shuffles byw/rec/picks/taste/gems with
    a per-day RNG, and the feed rows are position-blended per viewer. Shuffling
    them again here would undo both. For Coming Soon in particular the order
    *is* the content -- soonest first is the only signal an unreleased title
    has.

    Called with the *prefixed* id (``personal-trending-movie``), so the prefix
    is stripped here rather than teaching the slot predicate to accept two id
    shapes.
    """
    if catalog_id.startswith("personal-") or is_tmdb_sourced_slot(catalog_id):
        return items
    seed = _get_shuffle_seed(catalog_id, shuffle_hours)
    if seed == 0:
        return items
    return shuffle_within_tiers(
        [(item, float(item.get("match_score") or 0.0)) for item in items],
        random.Random(seed),
    )


def _search_media_multi(
    db: Session, media_types: list[str], query: str, limit: int = 50
) -> list:
    """Search metadata across one or more media types without hiding known titles.

    Returns internal item dicts with the card fields plus ``media_type``, the
    raw ``popularity`` (ranking fallback), the same blended quality signal used by
    catalog generation, and a hidden ``_row`` (the ORM object, used by the web
    serializer to reproduce today's exact output).
    """
    query = unquote(query).strip()
    if len(query) < 2 or len(query) > 100:
        return []

    # A year disambiguates ranking but must not be part of the SQL title match.
    # Previously "Dune 2021" searched for that literal string and missed both
    # Dune records before the year-aware ranker ever got a chance to run.
    title_query, _ = _extract_query_year(query)
    title_query = title_query.strip()
    if len(title_query) < 2:
        return []

    search_pattern = f"%{title_query}%"
    normalized_query = title_query.lower()
    lowered_title = func.lower(MediaMetadata.title)
    # Search is an intent lookup, not a recommendation shelf. An exact or prefix
    # match must enter the candidate pool even when its ratings/vote evidence is
    # sparse; the shared quality signal orders results *within* each relevance
    # band instead of acting as an admission gate.
    title_relevance = case(
        (
            lowered_title.in_([normalized_query, f"the {normalized_query}"]),
            0,
        ),
        (
            lowered_title.like(f"{normalized_query}%")
            | lowered_title.like(f"the {normalized_query}%"),
            1,
        ),
        else_=2,
    )
    stored_type_relevance = case(
        (MediaMetadata.media_type.in_(media_types), 0),
        else_=1,
    )
    search_query = (
        db.query(MediaMetadata)
        # Not `raw_data`. It is the stored TMDB payload -- a few KB per row once
        # `workers/enrich.py` has pruned it, 50-250KB before -- and the only thing
        # search wants from it is whether three keys are present. Selecting it for
        # up to 200 rows to answer that was the largest blob read left on any
        # request path; `_search_row_media_type` now gets it separately, for the
        # handful of rows where it can still change the answer.
        .options(load_only(*_SEARCH_ROW_COLUMNS)).filter(
            # Query both namespaces, then retain the reconciled type below. This
            # is what lets a row whose stored composite key is stale reappear in
            # the TV search once its IMDb/raw-data evidence proves it is a series.
            MediaMetadata.media_type.in_(["movie", "tv"]),
            MediaMetadata.title.ilike(search_pattern),
            # The one hard exclusion in search. TMDB sets `adult` for pornography
            # specifically, not for mature themes -- an R-rated drama is not
            # flagged -- so this removes junk without costing a single findable
            # title. Everything else that is merely obscure, thin or badly rated
            # is DEMOTED by the ranker instead, so typing its name still finds it.
            MediaMetadata.adult.isnot(True),
        )
    )
    results = (
        search_query.order_by(
            title_relevance.asc(),
            # Preserve the requested namespace's recall before considering a
            # stale opposite-type row for repair.
            stored_type_relevance.asc(),
            # Recognition first, matching what the ranker orders on. A broad query
            # matches far more rows than the cap, so this decides which ones the
            # ranker ever sees -- selecting the pool by rating while ranking it by
            # recognition meant a well-known title could be cut before ranking.
            MediaMetadata.vote_count.desc().nullslast(),
            quality_expr().desc(),
            MediaMetadata.popularity.desc().nullslast(),
        )
        .limit(min(max(limit * 2, limit), 200))
        .all()
    )

    imdb_ids = {r.imdb_id for r in results if r.imdb_id}
    resolved_types = {
        (imdb_id, tmdb_id): media_type
        for imdb_id, tmdb_id, media_type in (
            db.query(
                ImdbResolution.imdb_id,
                ImdbResolution.tmdb_id,
                ImdbResolution.media_type,
            )
            .filter(
                ImdbResolution.imdb_id.in_(imdb_ids),
                ImdbResolution.status == "found",
                ImdbResolution.tmdb_id.isnot(None),
                ImdbResolution.media_type.in_(["movie", "tv"]),
            )
            .all()
            if imdb_ids
            else []
        )
    }

    # Fetch `raw_data` only where the sniff can still flip the answer. It never
    # can for a row already stored as `tv` (the fallback returns `tv` anyway), nor
    # where an IMDb resolution or an episode column has already settled it -- so
    # this is the stale-classification repair case only, and usually empty.
    ambiguous_ids = [
        r.tmdb_id
        for r in results
        if r.media_type == "movie"
        and resolved_types.get((r.imdb_id, r.tmdb_id)) not in ("movie", "tv")
        and r.number_of_seasons is None
        and r.number_of_episodes is None
        and r.next_episode_air_date is None
    ]
    raw_by_id: dict[int, dict] = {}
    if ambiguous_ids:
        raw_by_id = {
            tmdb_id: payload
            for tmdb_id, payload in db.query(
                MediaMetadata.tmdb_id, MediaMetadata.raw_data
            ).filter(
                MediaMetadata.tmdb_id.in_(ambiguous_ids),
                MediaMetadata.media_type == "movie",
            )
            if isinstance(payload, dict)
        }

    items = []
    for r in results:
        media_type = _search_row_media_type(
            r,
            resolved_types.get((r.imdb_id, r.tmdb_id)),
            raw=raw_by_id.get(r.tmdb_id, {}),
        )
        if media_type not in media_types:
            continue
        if media_type != r.media_type:
            logger.info(
                f"search media type repaired tmdb_id={r.tmdb_id} "
                f"stored={r.media_type} resolved={media_type}"
            )
        year = ""
        if r.release_date and len(r.release_date) >= 4:
            year = r.release_date[:4]
        items.append(
            {
                "tmdb_id": r.tmdb_id,
                "media_type": media_type,
                "title": r.title,
                "poster": r.poster_path,
                "backdrop": r.backdrop_path,
                "year": year,
                "description": r.overview,
                "rating": r.vote_average,
                "genres": r.genres if isinstance(r.genres, list) else [],
                "adult": r.adult is True,
                "imdb_id": r.imdb_id,
                "logo": r.logo_path,
                "digital_release_date": r.digital_release_date,
                "next_episode_air_date": r.next_episode_air_date,
                "popularity": r.popularity,
                # Rating evidence, not a rating. The ranker demotes a title
                # nobody has voted on rather than excluding it, so a genuinely
                # obscure film stays findable by name while the junk that shares
                # a word with a popular query sinks below it.
                "vote_count": r.vote_count,
                # Rating alone, 0..1 -- MDBList consensus where we have it, the
                # Bayesian TMDB average otherwise. Deliberately NOT
                # ``blended_value``: that multiplies in a buzz term
                # (popularity/(popularity+60)), which for a title trending on
                # TMDB is ~0.94. Feeding it in here double-counted popularity --
                # once inside "quality" and once as the popularity signal -- and
                # is how this week's TMDB churn outranked established films.
                "quality_score": min(max(_quality_value(r) / 10.0, 0.0), 1.0),
                "collection_id": r.collection_id,
                "collection_name": r.collection_name,
                "_row": r,
            }
        )

    # IMDb identifies one work independently of TMDB's movie/TV namespaces. If
    # stale and corrected rows coexist, retain one card and prefer the row whose
    # stored type already agrees with the canonical result.
    deduped: list[dict] = []
    index_by_identity: dict[tuple[str, object], int] = {}
    for item in items:
        identity = (
            ("imdb", item["imdb_id"])
            if item.get("imdb_id")
            else (f"tmdb:{item['media_type']}", item["tmdb_id"])
        )
        existing_index = index_by_identity.get(identity)
        if existing_index is None:
            index_by_identity[identity] = len(deduped)
            deduped.append(item)
            continue
        existing = deduped[existing_index]
        existing_row = existing.get("_row")
        candidate_row = item.get("_row")
        existing_agrees = (
            existing_row is not None
            and existing_row.media_type == existing["media_type"]
        )
        candidate_agrees = (
            candidate_row is not None and candidate_row.media_type == item["media_type"]
        )
        if candidate_agrees and not existing_agrees:
            deduped[existing_index] = item
    return deduped


def _search_row_media_type(
    row: MediaMetadata,
    resolved_type: str | None = None,
    *,
    raw: dict | None = None,
) -> str:
    """Return the strongest locally available movie/TV classification.

    Positive TV evidence is authoritative because downgrading a series to a movie
    drops its episode list. A scoped TMDB IMDb resolution may upgrade a legacy
    movie row to TV, but neither it nor a federated provider may downgrade a row
    already stored or shaped as TV. Absence of episode fields is never used to
    turn TV into a movie.

    ``raw`` lets a caller that loaded the row without ``raw_data`` supply the
    payload itself -- or supply ``{}`` to say it has already established there is
    no raw evidence to consider. Left unset, the column is read off the row, which
    is what callers holding a fully-loaded row do.
    """
    # A stored TV namespace is positive evidence on its own. It also protects a
    # good row from a stale or mismatched IMDb resolution saying "movie".
    if row.media_type == "tv" or (
        row.number_of_seasons is not None
        or row.number_of_episodes is not None
        or row.next_episode_air_date is not None
    ):
        return "tv"
    if raw is None:
        raw = row.raw_data if isinstance(row.raw_data, dict) else {}
    if (
        "seasons" in raw
        or "first_air_date" in raw
        or ("name" in raw and "title" not in raw)
    ):
        return "tv"
    if resolved_type == "tv":
        return "tv"
    return row.media_type


# ---------------------------------------------------------------------------
# Search: normalization and ranking.
# ---------------------------------------------------------------------------

_LEADING_THE_RE = re.compile(r"^the\s+")
_QUERY_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _normalize_search_text(s: str) -> str:
    """Normalize a title/query for comparison.

    Lowercase, replace punctuation with spaces, collapse whitespace, and strip a single
    leading article ("the"). Applied to BOTH sides so "The Office" == "office".
    """
    if not s:
        return ""
    s = unquote(s).lower().strip()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return _LEADING_THE_RE.sub("", s)


def _extract_query_year(raw: str) -> tuple[str, int | None]:
    """Pull a 4-digit year from the query; return (text_without_year, year|None)."""
    if not raw:
        return "", None
    text = unquote(raw)
    match = _QUERY_YEAR_RE.search(text)
    if not match:
        return text, None
    year = int(match.group(0))
    text_without = (text[: match.start()] + " " + text[match.end() :]).strip()
    return text_without, year


#: Genres that identify pornography rather than mature themes. Matched as a
#: substring so compound labels ("Softcore Porn") are caught too. Neither TMDB
#: nor IMDb uses these words in a legitimate genre, so the substring rule is
#: safe here -- unlike "adult", which needs the exact rule below.
_EXPLICIT_GENRE_SUBSTRINGS = (
    "erotic",
    "softcore",
    "hardcore",
    "pornographic",
    "sexploitation",
    "porn",
    "hentai",
    "에로",  # Korean: "ero(tic)"
    "성인",  # Korean: "adult"
)

#: IMDb's own genre is the bare word "Adult" -- a high-value signal for rows
#: whose genres were sourced from IMDb. It must match exactly rather than as a
#: substring: "Adult Animation"
#: is a legitimate audience label in our taxonomy, and a substring rule would
#: silently remove every adult-animation title from search.
_EXPLICIT_GENRES_EXACT = ("adult",)

#: Unambiguous tokens matched against the TITLE only. Deliberately NOT matched
#: against the synopsis: legitimate films routinely describe adult themes in
#: their plot (the 2004 "The Girl Next Door" is about a former adult-film star),
#: so synopsis matching would hide exactly the mainstream titles people want.
_EXPLICIT_TITLE_SUBSTRINGS = (
    "porn",
    "sexploitation",
    "softcore",
    "hentai",
    "onlyfans",
    "erotic",
    "emmanuelle",
)

_XXX_FRANCHISE_TITLES = (
    "xxx",
    "xxx state of the union",
    "xxx return of xander cage",
)


def _is_explicit_result(item: dict) -> bool:
    """True when a search item is pornography rather than mature drama.

    The SQL ``adult`` filter covers our rows only barely: the library is
    ingested with TMDB's ``include_adult=False``, so almost nothing carries the
    flag. Genre and title evidence catches what the flag misses.

    Genre and title only. Nothing here keys off popularity, ratings or artwork,
    so no legitimate obscure title is caught by it.
    """
    if item.get("adult") is True:
        return True

    genres = item.get("genres")
    if isinstance(genres, list):
        for genre in genres:
            if not isinstance(genre, str):
                continue
            normalized = genre.strip().casefold()
            if normalized in _EXPLICIT_GENRES_EXACT:
                return True
            if any(token in normalized for token in _EXPLICIT_GENRE_SUBSTRINGS):
                return True

    title = _normalize_search_text(item.get("title") or "")
    if "xxx" in title and title not in _XXX_FRANCHISE_TITLES:
        return True
    return any(token in title for token in _EXPLICIT_TITLE_SUBSTRINGS)


def _drop_explicit_results(items: list) -> list:
    """Remove pornographic entries from a merged result set."""
    return [item for item in items if not _is_explicit_result(item)]


# Ranking weights (tunable server-side).
#
# Tier gaps are 2000 and the whole within-tier budget spans roughly -180..+850, so
# relevance is now *strictly* dominant: no combination of popularity, quality and
# recency can lift a weaker match over a stronger one. That was previously untrue
# -- the gaps were 100 while the within-tier signals summed to 115 -- which is one
# of the two reasons results looked arbitrary.
_TIER_EXACT = 10_000
_TIER_PREFIX = 8_000
_TIER_ALL_TOKENS = 6_000
_TIER_SUBSTRING = 4_000
_TIER_OTHER = 2_000

# Within a tier, RECOGNITION leads: how many people have rated a title at all.
#
# The signal to use for "popular" is `vote_count`, not TMDB's `popularity`. They
# measure different things. `popularity` is a volatile, recency-weighted activity
# score -- whatever is churning on TMDB this week -- so a low-budget new release
# can sit at 900 while The Godfather sits at 35. Ranking on it surfaced exactly
# that churn. `vote_count` measures accumulated audience over a title's lifetime,
# which is what a person means by "popular", and it barely moves week to week.
#
# Recognition also has to carry this on its own, because QUALITY CANNOT
# DISCRIMINATE JUNK: `_quality_value` is Bayesian against a 6.5/250 prior, so a
# title with 3 votes scores ~6.4 -- indistinguishable from a decent film. That is
# correct behaviour for "we have no evidence", and it means vote_count is the only
# signal that separates an established film from an unknown one.
_W_RECOGNITION = 420.0  # log-normalized vote_count (0..1) * this
_W_QUALITY = 300.0  # rating only, 0..1 -- no buzz, no recency
_W_BUZZ = 60.0  # log-normalized TMDB popularity; a nudge, nothing more
_VOTES_REF = 20_000.0
_DB_POP_REF = 500.0
_YEAR_BOOST = 150.0

# A broad one-word search is usually discovery intent ("star", "office"), not a
# demand that an unknown work with that exact one-word title beat every familiar
# title containing the word. Exact/prefix/whole-token matches therefore share one
# relevance band and get only a bounded specificity nudge. Recognition can
# overcome the nudge; for specific multi-word or year-qualified queries the normal
# absolute relevance tiers still apply.
_BROAD_EXACT_BOOST = 180.0
_BROAD_PREFIX_BOOST = 50.0

# Demotion, never exclusion. A title with no artwork still appears -- and tier
# dominance means typing its name exactly still puts it first. This is the whole
# difference between "less junk" and "can't find my film". There is deliberately
# no separate obscurity penalty: the recognition curve already separates a
# 3-vote title from a 20,000-vote one smoothly, without a cliff at an arbitrary
# vote count that a legitimate niche film could fall off.
_P_NO_ARTWORK = 120.0

#: Below this many genuine matches we keep even non-matching (`_TIER_OTHER`)
#: candidates, because a sparse or misspelled query needs every scrap of recall.
#: Above it they are dropped: they are, by definition, results that do not match
#: what was typed, and they were filling the tail of every populated search.
_MIN_REAL_MATCHES = 5

#: A collection is rendered as a block from two members up -- a duology is a
#: franchise for ordering purposes, and Apple TV groups them the same way.
_MIN_FRANCHISE_MEMBERS = 2


def _item_year(item: dict) -> int | None:
    """The item's release year as an int, or None when it is absent/malformed."""
    try:
        return int(item.get("year"))
    except (TypeError, ValueError):
        return None


def _rank_search_results(items: list, raw_query: str) -> list:
    """Sort merged items most-relevant-first; the returned order IS the ranking.

    Three rules, in order of authority:

    1. **Specific intent dominates absolutely.** Multi-word and year-qualified
       queries use exact > prefix > all-tokens > substring > other, with gaps far
       wider than every other signal. Broad one-word queries instead place exact,
       prefix and whole-word matches in one strong band so recognition can surface
       the title most people likely mean.
    2. **Recognition leads inside a tier** -- `vote_count`, i.e. how many people
       have engaged with a title over its lifetime -- with rating second, current
       TMDB activity a distant third, and a year match disambiguating same-named
       titles. Artwork-less rows are demoted here, never dropped.
    3. **A franchise is emitted as one release-ordered block** (``_group_franchises``).

    The only candidates removed are ones that do not match the query at all, and
    only once the query already has ``_MIN_REAL_MATCHES`` genuine matches.
    """
    text, qyear = _extract_query_year(raw_query)
    nq = _normalize_search_text(text)
    q_tokens = set(nq.split())
    broad_query = qyear is None and len(q_tokens) == 1

    def match_for(nt: str, t_tokens: set) -> tuple[int, float]:
        if broad_query:
            if nq and nt == nq:
                return _TIER_ALL_TOKENS, _BROAD_EXACT_BOOST
            if nq and nt.startswith(nq):
                return _TIER_ALL_TOKENS, _BROAD_PREFIX_BOOST
            if q_tokens and q_tokens <= t_tokens:
                return _TIER_ALL_TOKENS, 0.0
            if nq and nq in nt:
                return _TIER_SUBSTRING, 0.0
            return _TIER_OTHER, 0.0
        if nq and nt == nq:
            return _TIER_EXACT, 0.0
        if nq and nt.startswith(nq):
            return _TIER_PREFIX, 0.0
        if q_tokens and q_tokens <= t_tokens:
            return _TIER_ALL_TOKENS, 0.0
        if nq and nq in nt:
            return _TIER_SUBSTRING, 0.0
        return _TIER_OTHER, 0.0

    scored = []
    for item in items:
        nt = _normalize_search_text(item.get("title", ""))
        tier, specificity = match_for(nt, set(nt.split()))

        db_pop = item.get("popularity") or 0.0
        votes = item.get("vote_count") or 0

        recognition = 0.0
        if votes > 0:
            recognition = _W_RECOGNITION * min(
                1.0, math.log10(1 + votes) / math.log10(1 + _VOTES_REF)
            )

        # Current activity, as a nudge. Deliberately small: it is the signal that
        # promotes churn, and it is already the reason the previous weighting
        # ranked this week's noise above established films.
        buzz = 0.0
        if db_pop > 0:
            buzz = _W_BUZZ * min(
                1.0, math.log10(1 + db_pop) / math.log10(1 + _DB_POP_REF)
            )

        quality = item.get("quality_score")
        if quality is None:
            # Rows without a stored quality score fall back to their rating,
            # on the same 0..1 scale.
            quality = min(max(float(item.get("rating") or 0.0) / 10.0, 0.0), 1.0)
        quality_score = _W_QUALITY * float(quality)

        year_boost = 0.0
        item_year = _item_year(item)
        if qyear and item_year is not None and item_year == qyear:
            year_boost = _YEAR_BOOST

        penalty = 0.0
        if not item.get("poster"):
            # A card with no artwork reads as broken in the client's poster row.
            penalty += _P_NO_ARTWORK

        score = (
            tier
            + specificity
            + recognition
            + quality_score
            + buzz
            + year_boost
            - penalty
        )
        scored.append((tier, (-score, -db_pop, nt), item))

    real_matches = sum(1 for tier, _, _ in scored if tier > _TIER_OTHER)
    if real_matches >= _MIN_REAL_MATCHES:
        scored = [entry for entry in scored if entry[0] > _TIER_OTHER]

    ranked = [item for _, _, item in sorted(scored, key=lambda entry: entry[1])]
    return _group_franchises(ranked, nq)


def _group_franchises(ranked: list, nq: str) -> list:
    """Rewrite a ranked list so each franchise appears as one contiguous block.

    The block sits where the collection's *best-ranked* member earned its place,
    and inside it the films run in release order -- so "harry potter" returns the
    eight films 1..8 from wherever the strongest of them ranked, instead of
    interleaving them by rating with unrelated titles in between. Searching one
    specific film still puts that film first and lets the rest of its series
    follow, which is the behaviour Apple TV has.

    Rows with no ``collection_id`` (TV, standalone films, and anything not yet
    backfilled) are returned untouched and in rank order, so this degrades to a
    no-op rather than to a wrong order.
    """
    indexes_by_collection: dict[int, list[int]] = {}
    for index, item in enumerate(ranked):
        collection_id = item.get("collection_id")
        if collection_id:
            indexes_by_collection.setdefault(int(collection_id), []).append(index)

    groups = {
        collection_id: indexes
        for collection_id, indexes in indexes_by_collection.items()
        if len(indexes) >= _MIN_FRANCHISE_MEMBERS
    }
    if not groups:
        return ranked

    collection_of_index = {
        index: collection_id
        for collection_id, indexes in groups.items()
        for index in indexes
    }

    def block_order(index: int) -> tuple:
        item = ranked[index]
        # An exactly-named film leads its own block; everything else is
        # chronological, with rank as the tie-break for unknown years.
        is_exact = bool(nq) and _normalize_search_text(item.get("title", "")) == nq
        year = _item_year(item)
        return (0 if is_exact else 1, year if year is not None else 9999, index)

    ordered = {
        collection_id: sorted(indexes, key=block_order)
        for collection_id, indexes in groups.items()
    }

    output: list = []
    emitted: set[int] = set()
    for index, item in enumerate(ranked):
        collection_id = collection_of_index.get(index)
        if collection_id is None:
            output.append(item)
            continue
        if collection_id in emitted:
            continue
        emitted.add(collection_id)
        output.extend(ranked[member] for member in ordered[collection_id])
    return output


async def _db_call(fn, *args, **kwargs):
    """Run one synchronous unit of DB work off the loop, on a session the thread owns.

    FastAPI runs ``def`` handlers in a threadpool but ``async def`` handlers on the
    event loop, so a synchronous ``with get_db()`` block inside an ``async def`` blocks
    every other in-flight request for its duration. The deployment is deliberately a
    single Uvicorn process (in-memory stream caches and the playback admission counter
    are per-process), so there is no second worker to absorb that — one profile's
    library write would stall the byte proxy mid-playback for somebody else.

    A handler that has no ``await`` at all should just drop ``async`` instead; this is
    for the ones that genuinely await something and still need a DB block.

    The thread gets its own session for the same reason ``_detail_db_call`` does: a
    cancelled request closes its session as it unwinds, and a thread already inside
    that session cannot be cancelled with it.
    """

    def run():
        with get_db() as db:
            return fn(db, *args, **kwargs)

    return await asyncio.to_thread(run)


async def _request_db_call(db: Session, fn, *args, release: bool = True):
    """Run synchronous work on the request's own session off the loop.

    For ``async def`` handlers that receive ``db`` from ``get_db_dependency``:
    the query runs on a worker thread instead of blocking the loop, and with
    ``release`` the session hands its connection back to the pool as soon as
    the work is done, so the handler never holds a checkout across provider
    I/O. If the request is cancelled mid-call, the thread is allowed to finish
    before the dependency's cleanup closes the same session underneath it.
    """

    def run():
        try:
            return fn(db, *args)
        finally:
            if release and db is not None:
                db.close()

    work = asyncio.ensure_future(asyncio.to_thread(run))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        await asyncio.wait([work])
        raise


def _get_cached_user(user_key: str, db: Session) -> "User | None":
    """Look up an active user by key with in-memory caching.

    Cache only the scalar user id, not a request-bound ORM instance. A failed
    commit rolls back and expires objects in that request session; caching the
    ORM object would leave later requests with an expired, detached instance.
    """
    now = time()
    if user_key in _user_cache:
        cached_at, user_id = _user_cache[user_key]
        if now - cached_at < _USER_CACHE_TTL:
            if user_id is None:
                return None
            user = db.get(User, user_id)
            if user and user.is_active:
                return user
            _user_cache.pop(user_key, None)
            return None

    user = (
        db.query(User)
        .filter(User.user_key == user_key, User.is_active.is_(True))
        .first()
    )
    _user_cache[user_key] = (now, user.id if user else None)
    _user_cache_evict()
    return user


def _require_active_user_by_key(user_key: str, db: Session) -> User:
    user = _get_cached_user(user_key, db)
    if not user:
        raise invalid_profile_credential()
    return user


def _get_cached_catalog(
    catalog_id: str,
    db: Session | None = None,
    user_id: int | None = None,
    hide_foreign: bool = False,
    hide_adult: bool = False,
    hide_unreleased: bool = False,
    user_taste_tag_ids: list[int] | None = None,
    exclude_ids: set[int] | None = None,
    cache_empty: bool = True,
    revision: str = "",
) -> list:
    """Get catalog items with TTL + LRU caching.

    Only opens a DB session on cache miss. Serves stale cached data when
    the database is temporarily unreachable (e.g. transient DNS failures).
    """
    # The cached result depends on the exact taste ordering AND the exclude set
    # (watched items are filtered out of the result), so both must be in the key.
    # Callers pass user_id=None for the universal paths, so keying on user_id alone
    # let one local user's personalized ordering — and their hidden watched items —
    # be served to every other user. Fold a stable signature of both into the key.
    taste_sig = _cache_ids_sig(user_taste_tag_ids)
    exclude_sig = _cache_ids_sig(exclude_ids)
    cache_key = (
        f"{catalog_id}:user={user_id}:f={hide_foreign}:a={hide_adult}:"
        f"u={hide_unreleased}:t={taste_sig}:x={exclude_sig}:r={revision}"
    )
    now = time()
    ttl = settings.cache_ttl

    if cache_key in _catalog_cache:
        cached_at, items = _catalog_cache[cache_key]
        if now - cached_at < ttl:
            _catalog_cache[cache_key] = (now, items)
            return items

    # Cache miss or expired — try refreshing from DB
    try:
        if db is None:
            from app.database import get_db_session

            db = get_db_session()
            _close_db = True
        else:
            _close_db = False

        try:
            generator = CatalogGenerator(db)
            items = generator.get_catalog_content(
                catalog_id,
                user_id=user_id,
                hide_foreign=hide_foreign,
                hide_adult=hide_adult,
                hide_unreleased=hide_unreleased,
                user_taste_tag_ids=user_taste_tag_ids,
                exclude_ids=exclude_ids,
            )
            if items or cache_empty:
                _catalog_cache[cache_key] = (now, items)
                _cache_evict()
            return items
        finally:
            if _close_db:
                db.close()
    except Exception as e:
        # DB unreachable — serve stale cache if available
        if cache_key in _catalog_cache:
            _, items = _catalog_cache[cache_key]
            logger.warning(
                f"DB error for catalog '{catalog_id}', serving stale cache: {e}"
            )
            return items
        logger.error(f"DB error for catalog '{catalog_id}', no cached data: {e}")
        return []


# ---------------------------------------------------------------------------
# Scrobble registration and event endpoints
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    """Account registration request."""

    invite: str
    email: str
    password: str = ""
    name: str = ""


class PasskeyBeginRegistrationRequest(BaseModel):
    """Begin passkey registration for a new or existing local account."""

    invite: str = ""
    email: str = ""
    name: str = ""
    nickname: str = ""


class PasskeyFinishRegistrationRequest(BaseModel):
    """Finish passkey registration with the browser credential response."""

    token: str
    credential: dict
    nickname: str = ""


class PasskeyBeginLoginRequest(BaseModel):
    """Begin passkey login for an existing local account."""

    email: str = ""


class PasskeyFinishLoginRequest(BaseModel):
    """Finish passkey login with the browser assertion response."""

    token: str
    credential: dict


class LoginRequest(BaseModel):
    """Login request."""

    email: str
    password: str
    totp_code: str = ""


class TOTPConfirmRequest(BaseModel):
    """2FA confirmation request."""

    code: str


class DeviceClaimRequest(BaseModel):
    """Device pairing claim request (authenticated user claims a device code)."""

    short_code: str


class MobileAuthExchangeRequest(BaseModel):
    """Exchange a mobile authorization code using the PKCE verifier."""

    code: str
    code_verifier: str


def _get_account_from_session(
    request: Request, db: Session, *, allow_pending_enrollment: bool = False
) -> Account:
    """Extract and verify the authenticated account from the session cookie.

    Also enforces the passkey/TOTP enrollment requirement. Because every
    authenticated route resolves its caller through here, this is what makes the
    requirement real rather than a client-side suggestion — a hand-typed URL or a
    raw API call is refused just like the dashboard is. Endpoints that exist to
    *satisfy* the requirement pass ``allow_pending_enrollment=True``.

    Deliberately a dependency rather than middleware: the native ``user_key``
    routes never call this, so a TV already paired to an account keeps
    streaming while its owner is mid-enrollment.
    """
    token = request.cookies.get("user_session")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    account = verify_user_session(token, db)
    if not account:
        raise HTTPException(status_code=401, detail="Session expired")
    if not account.is_active:
        raise HTTPException(status_code=403, detail="Account is deactivated")
    if not allow_pending_enrollment:
        requirement = auth_requirement(db, account)
        if requirement != "none":
            raise _enrollment_required_error(requirement)
    return account


def _account_profiles(db: Session, account: Account) -> list[User]:
    """Return an account's viewing profiles, oldest first."""
    return (
        db.query(User)
        .filter(User.account_id == account.id)
        .order_by(User.created_at.asc(), User.id.asc())
        .all()
    )


def _create_profile(
    db: Session,
    account: Account,
    *,
    display_name: str | None = None,
    avatar: str | None = None,
    bandwidth_tier: str = "high",
    content_scope: str = "everything",
) -> User:
    """Create a new viewing profile with its own ``user_key``."""
    profile = User(
        user_key=secrets.token_urlsafe(32),
        account_id=account.id,
        auth_source="local",
        display_name=display_name or None,
        avatar=avatar or None,
        bandwidth_tier=bandwidth_tier,
        content_scope=content_scope,
    )
    db.add(profile)
    db.flush()
    from app.home_feed import ensure_state

    ensure_state(db, profile.id)
    db.commit()
    db.refresh(profile)
    _schedule_local_sync(profile.id)
    return profile


def _get_active_profile(
    request: Request, db: Session, *, allow_pending_enrollment: bool = False
) -> User:
    """Resolve the viewing profile selected for the current web session.

    Falls back to the account's default (oldest) profile and self-heals an
    account that somehow has no profiles. Enforces the passkey/TOTP enrollment
    requirement exactly like ``_get_account_from_session``.
    """
    token = request.cookies.get("user_session")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return _get_active_profile_for_token(
        token, db, allow_pending_enrollment=allow_pending_enrollment
    )


def _enrollment_required_error(requirement: str) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail=(
            "Set up a passkey to continue"
            if requirement == "passkey_required"
            else "Set up two-factor authentication to continue"
        ),
        headers={"X-Auth-Requirement": requirement},
    )


def _get_active_profile_for_token(
    token: str, db: Session, *, allow_pending_enrollment: bool = False
) -> User:
    """Token-based half of profile resolution, usable in an off-loop DB unit."""
    session = get_user_session(token, db)
    if not session:
        raise HTTPException(status_code=401, detail="Session expired")
    account = verify_user_session(token, db)
    if not account:
        raise HTTPException(status_code=401, detail="Session expired")
    if not account.is_active:
        raise HTTPException(status_code=403, detail="Account is deactivated")
    if not allow_pending_enrollment:
        requirement = auth_requirement(db, account)
        if requirement != "none":
            raise _enrollment_required_error(requirement)

    profiles = _account_profiles(db, account)
    if not profiles:
        profiles = [_create_profile(db, account)]

    profile = None
    if session.active_profile_id:
        profile = next((p for p in profiles if p.id == session.active_profile_id), None)
    if profile is None:
        profile = profiles[0]
        if session.active_profile_id != profile.id:
            session.active_profile_id = profile.id
            db.commit()
    if not profile.is_active:
        raise HTTPException(status_code=403, detail="Profile is deactivated")
    return profile


def _profile_summary(profile: User) -> dict:
    """Profile descriptor with the credential native clients send as Bearer."""
    return {
        "id": profile.id,
        "display_name": profile.display_name or "Profile",
        "avatar": profile.avatar,
        "bandwidth_tier": profile.bandwidth_tier,
        "content_scope": getattr(profile, "content_scope", None) or "everything",
        # "auto" (on for the server owner's first profile), "on" or "off".
        "server_history": getattr(profile, "server_history", None) or "auto",
        "user_key": profile.user_key,
        "api_base": settings.base_url,
        "created_at": profile.created_at.isoformat() if profile.created_at else None,
    }


def _issue_account_session_response(
    request: Request,
    account: Account,
    db: Session,
    content: dict,
    active_profile_id: int | None = None,
) -> JSONResponse:
    """Create an account web session and attach it to a JSON response."""
    account.last_login = utc_now()  # type: ignore[assignment]
    token = create_user_session(account.id, db, active_profile_id=active_profile_id)
    response = JSONResponse(content=content)
    response.set_cookie(
        key="user_session",
        value=token,
        httponly=True,
        secure=_cookie_secure(request),
        max_age=86400,
        samesite="lax",
    )
    return response


def _enrollment_url(requirement: str, next_url: str = "/account/dashboard") -> str:
    """Return the page that satisfies a blocking requirement."""
    page = (
        "/account/setup-passkey"
        if requirement == "passkey_required"
        else "/account/setup-2fa"
    )
    return f"{page}?next={quote(next_url, safe='')}"


def _passkey_requirement(account: Account) -> str:
    """Return the legacy ``totp_requirement`` value for native clients.

    Superseded by :func:`_auth_requirement` for anything that gates access; kept
    so older iOS/Electron builds parsing this field keep behaving as they did.
    """
    if account.is_admin and not account.totp_enabled:
        return "totp_required"
    if not account.totp_enabled:
        return "totp_encouraged"
    return "none"


def _consume_valid_invite(db: Session, invite_code: str, email: str) -> None:
    invite_code = (invite_code or "").strip()
    if not invite_code:
        raise HTTPException(status_code=403, detail="Invite code required")
    invite_row = (
        db.query(InviteCode)
        .filter(InviteCode.code == invite_code, InviteCode.is_used.is_(False))
        .first()
    )
    if not invite_row:
        raise HTTPException(status_code=403, detail="Invalid invite code")
    if invite_row.expires_at and utc_now() > invite_row.expires_at:
        raise HTTPException(status_code=403, detail="Invite code has expired")
    invite_row.is_used = True
    invite_row.used_at = utc_now()
    invite_row.used_by = email


def _store_passkey_challenge(
    db: Session,
    *,
    challenge: bytes,
    intent: str,
    account_id: int | None = None,
    email: str | None = None,
    invite_code: str | None = None,
    display_name: str | None = None,
) -> str:
    token = secrets.token_urlsafe(32)
    db.add(
        PasskeyChallenge(
            token=token,
            challenge=b64url_encode(challenge),
            intent=intent,
            account_id=account_id,
            email=email,
            invite_code=invite_code,
            display_name=display_name,
            expires_at=utc_now() + timedelta(minutes=5),
        )
    )
    db.commit()
    return token


def _get_passkey_challenge(db: Session, token: str, intent: str) -> PasskeyChallenge:
    challenge = (
        db.query(PasskeyChallenge)
        .filter(PasskeyChallenge.token == token, PasskeyChallenge.intent == intent)
        .first()
    )
    if not challenge or challenge.expires_at < utc_now():
        raise HTTPException(status_code=400, detail="Passkey challenge expired")
    return challenge


def _delete_passkey_challenge(db: Session, challenge: PasskeyChallenge) -> None:
    db.delete(challenge)
    db.commit()


@app.get("/auth/capabilities")
async def auth_capabilities():
    """Advertise supported client authentication flows."""
    return {
        "mobileAuth": {
            "supported": True,
            "startURL": f"{settings.base_url}/auth/mobile/start",
            "exchangeURL": f"{settings.base_url}/auth/mobile/exchange",
            "pkceMethods": ["S256"],
            "redirectSchemes": [
                scheme.strip()
                for scheme in settings.mobile_redirect_schemes.split(",")
                if scheme.strip()
            ],
        },
        "deviceCode": {
            "supported": True,
            "codeURL": f"{settings.base_url}/auth/device/code",
        },
        "playback_providers": [
            status.model_dump() for status in source_registry.provider_statuses()
        ],
    }


API_VERSION = 2
MIN_CLIENT_VERSIONS = {"roku": "2.0.0", "tvos": "2.0.0", "web": "2.0.0"}


@app.get("/api/version")
async def api_version():
    """Unauthenticated compatibility handshake for clients (docs/playback-api.md)."""
    return {
        "api_version": API_VERSION,
        "server_version": app.version,
        "min_clients": MIN_CLIENT_VERSIONS,
        # Profile endpoints live under api_prefix and take the profile key as
        # ``Authorization: Bearer <user_key>``, never in the URL.
        "auth": "bearer",
        "api_prefix": "/api/v2",
    }


@app.get("/auth/me")
def auth_me(request: Request, db: Session = Depends(get_db_dependency)):
    """Return the signed-in account, its profiles, and the active profile.

    Plain ``def`` so the synchronous DB queries run in the threadpool and don't
    block the event loop (this is polled by the web client).
    """
    # Readable mid-enrollment: this is what the setup pages poll to learn which
    # step they still owe.
    account = _get_account_from_session(request, db, allow_pending_enrollment=True)
    profiles = _account_profiles(db, account)
    active = _get_active_profile(request, db, allow_pending_enrollment=True)
    return {
        "id": account.id,
        "email": account.email,
        "display_name": active.display_name or account.email,
        "totp_enabled": account.totp_enabled,
        "passkey_count": account_passkey_count(db, account),
        "auth_requirement": auth_requirement(db, account),
        "totp_requirement": _passkey_requirement(account),
        "active_profile_id": active.id,
        "profiles": [_profile_summary(p) for p in profiles],
    }


@app.post("/auth/register")
@limiter.limit("5/minute")
async def register_account(request: Request, payload: RegisterRequest):
    """Register a new account with email, password, and invite code.

    Creates the user and immediately signs them in so they can continue to
    optional 2FA/passkey setup and app pairing without returning to login.
    """
    if not payload.invite:
        raise HTTPException(status_code=403, detail="Invite code required")
    if not payload.email or "@" not in payload.email:
        raise HTTPException(status_code=400, detail="Valid email required")
    if len(payload.password) < 8:
        raise HTTPException(
            status_code=400, detail="Password must be at least 8 characters"
        )
    email = payload.email.lower().strip()

    with get_db() as db_session:
        # Check email uniqueness across accounts
        existing = db_session.query(Account).filter(Account.email == email).first()
        if existing:
            raise HTTPException(status_code=409, detail="Email already registered")

        _consume_valid_invite(db_session, payload.invite, email)

        try:
            # bcrypt is CPU-bound and holds the GIL ~250ms; run it off the event
            # loop so a burst of registrations can't serialize the whole server.
            password_hash = await asyncio.to_thread(hash_password, payload.password)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        account = Account(
            auth_source="local",
            email=email,
            password_hash=password_hash,
        )
        db_session.add(account)
        db_session.commit()
        db_session.refresh(account)
        # Every account starts with one default viewing profile.
        profile = _create_profile(
            db_session, account, display_name=payload.name or None
        )
        return _issue_account_session_response(
            request,
            account,
            db_session,
            {
                "status": "ok",
                "message": "Account created. Set up your passkey to continue.",
                "totp_enabled": account.totp_enabled,
                "auth_requirement": auth_requirement(db_session, account),
                "totp_requirement": _passkey_requirement(account),
                "passkey_count": 0,
                "display_name": profile.display_name or account.email,
            },
            active_profile_id=profile.id,
        )


@app.post("/auth/login")
@limiter.limit("10/minute")
async def login(
    request: Request, payload: LoginRequest, db: Session = Depends(get_db_dependency)
):
    """Log in with email + password + optional TOTP code.

    Returns a session cookie for web access.
    """
    account = (
        db.query(Account).filter(Account.email == payload.email.lower().strip()).first()
    )
    if not account or not account.password_hash:
        # Offload bcrypt off the event loop (see register); the dummy verify keeps
        # login timing constant against user enumeration.
        await asyncio.to_thread(dummy_verify_password, payload.password)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not account.is_active:
        raise HTTPException(status_code=403, detail="Account is deactivated")
    if not await asyncio.to_thread(
        verify_password, payload.password, account.password_hash
    ):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Check 2FA if enabled
    if account.totp_enabled:
        if not payload.totp_code:
            raise HTTPException(status_code=401, detail="2FA code required")
        if not verify_account_totp(account, payload.totp_code):
            raise HTTPException(status_code=401, detail="Invalid 2FA code")

    passkey_count = account_passkey_count(db, account)
    profiles = _account_profiles(db, account)
    default_profile = profiles[0] if profiles else _create_profile(db, account)
    return _issue_account_session_response(
        request,
        account,
        db,
        {
            "status": "ok",
            "totp_enabled": account.totp_enabled,
            "auth_requirement": auth_requirement(db, account),
            "totp_requirement": _passkey_requirement(account),
            "passkey_count": passkey_count,
            "display_name": default_profile.display_name or account.email,
        },
        active_profile_id=default_profile.id,
    )


@app.post("/auth/passkeys/register/begin")
@limiter.limit("10/minute")
async def begin_passkey_registration(
    request: Request,
    payload: PasskeyBeginRegistrationRequest,
    db: Session = Depends(get_db_dependency),
):
    """Start passkey registration for invite onboarding or an existing account."""
    webauthn = load_webauthn()
    cfg = get_webauthn_config(request)

    existing_account: Account | None = None
    email = (payload.email or "").lower().strip()
    display_name = payload.name.strip()

    session_token = request.cookies.get("user_session")
    if session_token:
        existing_account = verify_user_session(session_token, db)
        if existing_account:
            email = existing_account.email or email
            display_name = display_name or existing_account.email or email
    else:
        if not email or "@" not in email:
            raise HTTPException(status_code=400, detail="Valid email required")
        if not payload.invite:
            raise HTTPException(status_code=403, detail="Invite code required")
        if db.query(Account).filter(Account.email == email).first():
            raise HTTPException(status_code=409, detail="Email already registered")
        inv = (
            db.query(InviteCode)
            .filter(InviteCode.code == payload.invite, InviteCode.is_used.is_(False))
            .first()
        )
        if not inv:
            raise HTTPException(status_code=403, detail="Invalid invite code")
        if inv.expires_at and utc_now() > inv.expires_at:
            raise HTTPException(status_code=403, detail="Invite code has expired")

    descriptors = []
    if existing_account:
        for cred in (
            db.query(PasskeyCredential)
            .filter(PasskeyCredential.account_id == existing_account.id)
            .all()
        ):
            descriptors.append(
                webauthn["PublicKeyCredentialDescriptor"](
                    id=b64url_decode(cred.credential_id)
                )
            )

    user_handle = (
        str(existing_account.id).encode("utf-8")
        if existing_account
        else email.encode("utf-8")
    )
    options = webauthn["generate_registration_options"](
        rp_id=cfg.rp_id,
        rp_name=cfg.rp_name,
        user_id=user_handle,
        user_name=email,
        user_display_name=display_name or email,
        exclude_credentials=descriptors,
        authenticator_selection=webauthn["AuthenticatorSelectionCriteria"](
            resident_key=webauthn["ResidentKeyRequirement"].PREFERRED,
            user_verification=webauthn["UserVerificationRequirement"].PREFERRED,
        ),
    )
    token = _store_passkey_challenge(
        db,
        challenge=options.challenge,
        intent="register",
        account_id=existing_account.id if existing_account else None,
        email=email,
        invite_code=payload.invite.strip() if not existing_account else None,
        display_name=display_name,
    )
    return {
        "token": token,
        "options": options_to_dict(options, webauthn["options_to_json"]),
    }


@app.post("/auth/passkeys/register/finish")
@limiter.limit("10/minute")
async def finish_passkey_registration(
    request: Request,
    payload: PasskeyFinishRegistrationRequest,
    db: Session = Depends(get_db_dependency),
):
    """Verify and store a passkey, creating and signing in an invite user if needed."""
    webauthn = load_webauthn()
    cfg = get_webauthn_config(request)
    challenge_row = _get_passkey_challenge(db, payload.token, "register")
    if challenge_row.intent not in ("register", "add"):
        raise HTTPException(status_code=400, detail="Invalid passkey challenge")

    try:
        verification = webauthn["verify_registration_response"](
            credential=credential_from_dict(
                webauthn["RegistrationCredential"], payload.credential
            ),
            expected_challenge=b64url_decode(challenge_row.challenge),
            expected_origin=cfg.origin,
            expected_rp_id=cfg.rp_id,
            require_user_verification=False,
        )
    except Exception as exc:
        logger.warning(
            "Passkey registration verification failed for origin={} rp_id={}: {}",
            cfg.origin,
            cfg.rp_id,
            exc,
        )
        raise HTTPException(
            status_code=400, detail="Passkey registration failed"
        ) from exc

    account = (
        db.query(Account).filter(Account.id == challenge_row.account_id).first()
        if challenge_row.account_id
        else None
    )
    profile: User | None = None
    if not account:
        email = (challenge_row.email or "").lower().strip()
        if not email or db.query(Account).filter(Account.email == email).first():
            raise HTTPException(status_code=409, detail="Email already registered")
        _consume_valid_invite(db, challenge_row.invite_code or "", email)
        account = Account(
            auth_source="local",
            email=email,
            password_hash=None,
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        profile = _create_profile(
            db, account, display_name=challenge_row.display_name or None
        )

    transports = payload.credential.get("response", {}).get(
        "transports"
    ) or payload.credential.get("transports")
    credential_id = b64url_encode(verification.credential_id)
    if (
        db.query(PasskeyCredential)
        .filter(PasskeyCredential.credential_id == credential_id)
        .first()
    ):
        raise HTTPException(status_code=409, detail="Passkey is already registered")
    db.add(
        PasskeyCredential(
            account_id=account.id,
            credential_id=credential_id,
            public_key=b64url_encode(verification.credential_public_key),
            sign_count=verification.sign_count or 0,
            transports=transports,
            nickname=payload.nickname.strip() or "Passkey",
        )
    )
    db.delete(challenge_row)
    db.commit()

    profiles = _account_profiles(db, account)
    if not profiles:
        profile = _create_profile(db, account)
        profiles = [profile]
    if profile is None:
        profile = profiles[0]
    passkey_count = account_passkey_count(db, account)
    return _issue_account_session_response(
        request,
        account,
        db,
        {
            "status": "ok",
            "message": "Passkey saved. You are signed in.",
            "totp_enabled": account.totp_enabled,
            "auth_requirement": auth_requirement(db, account),
            "totp_requirement": _passkey_requirement(account),
            "passkey_count": passkey_count,
            "display_name": profile.display_name or account.email,
        },
        active_profile_id=profile.id,
    )


@app.post("/auth/passkeys/login/begin")
@limiter.limit("10/minute")
async def begin_passkey_login(
    request: Request,
    payload: PasskeyBeginLoginRequest,
    db: Session = Depends(get_db_dependency),
):
    """Start passkey login for an existing account."""
    webauthn = load_webauthn()
    cfg = get_webauthn_config(request)
    email = (payload.email or "").lower().strip()
    account = (
        db.query(Account).filter(Account.email == email).first() if email else None
    )
    if email and (not account or not account.is_active):
        raise HTTPException(
            status_code=404, detail="No active account found for that email"
        )

    allow_credentials = None
    account_id = None
    if account:
        credentials = (
            db.query(PasskeyCredential)
            .filter(PasskeyCredential.account_id == account.id)
            .all()
        )
        if not credentials:
            raise HTTPException(
                status_code=404, detail="No passkeys registered for that account"
            )
        allow_credentials = [
            webauthn["PublicKeyCredentialDescriptor"](
                id=b64url_decode(cred.credential_id)
            )
            for cred in credentials
        ]
        account_id = account.id

    options_kwargs = {
        "rp_id": cfg.rp_id,
        "user_verification": webauthn["UserVerificationRequirement"].PREFERRED,
    }
    if allow_credentials is not None:
        options_kwargs["allow_credentials"] = allow_credentials
    options = webauthn["generate_authentication_options"](**options_kwargs)
    token = _store_passkey_challenge(
        db,
        challenge=options.challenge,
        intent="login",
        account_id=account_id,
        email=email or None,
    )
    return {
        "token": token,
        "options": options_to_dict(options, webauthn["options_to_json"]),
    }


@app.post("/auth/passkeys/login/finish")
@limiter.limit("10/minute")
async def finish_passkey_login(
    request: Request,
    payload: PasskeyFinishLoginRequest,
    db: Session = Depends(get_db_dependency),
):
    """Verify a passkey assertion and issue a web session."""
    webauthn = load_webauthn()
    cfg = get_webauthn_config(request)
    challenge_row = _get_passkey_challenge(db, payload.token, "login")
    credential_id = payload.credential.get("id") or payload.credential.get("rawId")
    if not credential_id:
        raise HTTPException(status_code=400, detail="Missing passkey credential id")
    try:
        normalized_credential_id = b64url_encode(b64url_decode(credential_id))
    except Exception:
        normalized_credential_id = credential_id
    stored = (
        db.query(PasskeyCredential)
        .filter(PasskeyCredential.credential_id == normalized_credential_id)
        .first()
    )
    if not stored or (
        challenge_row.account_id and stored.account_id != challenge_row.account_id
    ):
        raise HTTPException(status_code=401, detail="Unknown passkey")
    account = db.query(Account).filter(Account.id == stored.account_id).first()
    if not account or not account.is_active:
        raise HTTPException(status_code=403, detail="Account is deactivated")
    try:
        verification = webauthn["verify_authentication_response"](
            credential=credential_from_dict(
                webauthn["AuthenticationCredential"], payload.credential
            ),
            expected_challenge=b64url_decode(challenge_row.challenge),
            expected_origin=cfg.origin,
            expected_rp_id=cfg.rp_id,
            credential_public_key=b64url_decode(stored.public_key),
            credential_current_sign_count=stored.sign_count,
            require_user_verification=False,
        )
    except Exception as exc:
        logger.warning(
            "Passkey login verification failed for account_id={} origin={} rp_id={}: {}",
            account.id,
            cfg.origin,
            cfg.rp_id,
            exc,
        )
        raise HTTPException(status_code=401, detail="Passkey login failed") from exc
    stored.sign_count = verification.new_sign_count or stored.sign_count
    stored.last_used_at = utc_now()
    db.delete(challenge_row)
    db.commit()
    passkey_count = account_passkey_count(db, account)
    profiles = _account_profiles(db, account)
    default_profile = profiles[0] if profiles else _create_profile(db, account)
    return _issue_account_session_response(
        request,
        account,
        db,
        {
            "status": "ok",
            "totp_enabled": account.totp_enabled,
            "auth_requirement": auth_requirement(db, account),
            "totp_requirement": _passkey_requirement(account),
            "passkey_count": passkey_count,
            "display_name": default_profile.display_name or account.email,
        },
        active_profile_id=default_profile.id,
    )


@app.get("/auth/passkeys")
async def list_passkeys(request: Request, db: Session = Depends(get_db_dependency)):
    """List passkeys for the current account."""
    # Readable mid-enrollment so the setup page can show what is already saved.
    account = _get_account_from_session(request, db, allow_pending_enrollment=True)
    credentials = (
        db.query(PasskeyCredential)
        .filter(PasskeyCredential.account_id == account.id)
        .order_by(PasskeyCredential.created_at.desc())
        .all()
    )
    return {
        "passkeys": [
            {
                "id": c.id,
                "nickname": c.nickname or "Passkey",
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "last_used_at": c.last_used_at.isoformat() if c.last_used_at else None,
            }
            for c in credentials
        ]
    }


@app.delete("/auth/passkeys/{credential_id}")
async def delete_passkey(
    credential_id: int, request: Request, db: Session = Depends(get_db_dependency)
):
    """Remove one of the account's passkeys, keeping at least one on file.

    A password is no longer an acceptable substitute, so the last passkey cannot
    be removed on any account — otherwise a single click would drop the account
    straight back behind the enrollment gate.
    """
    account = _get_account_from_session(request, db)
    credential = (
        db.query(PasskeyCredential)
        .filter(
            PasskeyCredential.id == credential_id,
            PasskeyCredential.account_id == account.id,
        )
        .first()
    )
    if not credential:
        raise HTTPException(status_code=404, detail="Passkey not found")
    if account_passkey_count(db, account) <= 1:
        raise HTTPException(
            status_code=400,
            detail="Add another passkey before removing your only one",
        )
    db.delete(credential)
    db.commit()
    return {"status": "ok"}


@app.post("/auth/setup-2fa")
async def setup_2fa(
    request: Request,
    db: Session = Depends(get_db_dependency),
):
    """Generate a TOTP secret and QR code for authenticator app enrollment.

    Requires an active user session. Reachable mid-enrollment — this endpoint is
    how an account clears its own ``totp_required`` gate.
    """
    account = _get_account_from_session(request, db, allow_pending_enrollment=True)

    if account.totp_enabled:
        raise HTTPException(status_code=400, detail="2FA is already enabled")

    secret = generate_totp_secret()
    provisioning_uri = get_totp_provisioning_uri(secret, account.email or "user")
    qr_data_url = generate_totp_qr_data_url(provisioning_uri)

    # Store encrypted secret (not yet enabled until confirmed)
    account.totp_secret = encrypt_totp_secret(secret)
    db.commit()

    return {
        "secret": secret,
        "provisioning_uri": provisioning_uri,
        "qr_data_url": qr_data_url,
    }


@app.post("/auth/confirm-2fa")
@limiter.limit("10/minute")
async def confirm_2fa(
    payload: TOTPConfirmRequest,
    request: Request,
    db: Session = Depends(get_db_dependency),
):
    """Verify a TOTP code to confirm 2FA enrollment.

    Requires an active user session with a pending TOTP secret. Reachable
    mid-enrollment — this is the call that clears a ``totp_required`` gate.
    """
    account = _get_account_from_session(request, db, allow_pending_enrollment=True)

    if account.totp_enabled:
        raise HTTPException(status_code=400, detail="2FA is already enabled")
    if not account.totp_secret:
        raise HTTPException(status_code=400, detail="Call /auth/setup-2fa first")

    if not verify_account_totp(account, payload.code):
        raise HTTPException(status_code=401, detail="Invalid 2FA code")

    account.totp_enabled = True
    db.commit()

    return {"status": "ok", "message": "2FA enabled successfully"}


def _validate_mobile_redirect_uri(redirect_uri: str) -> None:
    """Validate a native-app redirect URI for mobile auth callbacks."""
    if not redirect_uri or len(redirect_uri) > 2048:
        raise HTTPException(status_code=400, detail="Invalid redirect_uri")
    parsed = urlsplit(redirect_uri)
    if not parsed.scheme or parsed.fragment:
        raise HTTPException(status_code=400, detail="Invalid redirect_uri")
    allowed_schemes = {
        scheme.strip().lower()
        for scheme in settings.mobile_redirect_schemes.split(",")
        if scheme.strip()
    }
    if parsed.scheme.lower() not in allowed_schemes:
        raise HTTPException(status_code=400, detail="Unsupported redirect_uri scheme")


def _validate_pkce_challenge(code_challenge: str, method: str) -> None:
    if method != "S256":
        raise HTTPException(status_code=400, detail="Only S256 PKCE is supported")
    if not 43 <= len(code_challenge) <= 128:
        raise HTTPException(status_code=400, detail="Invalid code_challenge")
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    if any(ch not in allowed for ch in code_challenge):
        raise HTTPException(status_code=400, detail="Invalid code_challenge")


def _append_query_params(url: str, params: dict[str, str]) -> str:
    parts = urlsplit(url)
    query_items = parse_qsl(parts.query, keep_blank_values=True)
    query_items.extend((key, value) for key, value in params.items() if value != "")
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query_items), parts.fragment)
    )


def _relative_url_for_login_next(request: Request) -> str:
    """Return a same-origin relative URL safe for the account login next param."""
    path = request.url.path or "/"
    query = request.url.query
    return f"{path}?{query}" if query else path


@app.get("/auth/mobile/start")
@limiter.limit("20/minute")
async def mobile_auth_start(
    request: Request,
    redirect_uri: str,
    code_challenge: str,
    state: str = "",
    code_challenge_method: str = "S256",
    db: Session = Depends(get_db_dependency),
):
    """Start native iOS/macOS auth using authorization-code + PKCE.

    The mobile app opens this URL in ASWebAuthenticationSession. If the user is
    not signed in, they are sent through the normal web login page. After login,
    this endpoint redirects back to the app with only a short-lived one-time code.
    """
    _validate_mobile_redirect_uri(redirect_uri)
    return _native_auth_redirect(
        request, db, redirect_uri, code_challenge, state, code_challenge_method
    )


def _native_auth_redirect(
    request: Request,
    db: Session,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    code_challenge_method: str,
) -> RedirectResponse:
    """Send a signed-in browser back to a native app with a one-time code."""
    _validate_pkce_challenge(code_challenge, code_challenge_method)
    if len(state) > 512:
        raise HTTPException(status_code=400, detail="Invalid state")

    try:
        profile = _get_active_profile(request, db, allow_pending_enrollment=True)
    except HTTPException:
        login_next = quote(_relative_url_for_login_next(request), safe="")
        return RedirectResponse(
            url=f"/account/login?next={login_next}", status_code=303
        )
    # The app's code is as good as a password-plus-second-factor sign-in, so a
    # required passkey/2FA setup is finished here in the browser first, then the
    # browser comes back to this same URL.
    account = verify_user_session(request.cookies.get("user_session", ""), db)
    requirement = auth_requirement(db, account) if account else "none"
    if requirement != "none":
        return RedirectResponse(
            url=_enrollment_url(requirement, _relative_url_for_login_next(request)),
            status_code=303,
        )

    db.query(MobileAuthCode).filter(
        MobileAuthCode.user_id == profile.id,
        MobileAuthCode.consumed_at.is_(None),
        MobileAuthCode.expires_at <= utc_now(),
    ).delete(synchronize_session=False)
    db.commit()

    # The mobile/native client pairs to a specific profile's user_key.
    code = create_mobile_auth_code(
        db,
        user_id=profile.id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
    )
    callback_url = _append_query_params(redirect_uri, {"code": code, "state": state})
    return RedirectResponse(url=callback_url, status_code=303)


@app.post("/auth/mobile/exchange")
@limiter.limit("20/minute")
async def mobile_auth_exchange(
    request: Request,
    payload: MobileAuthExchangeRequest,
    db: Session = Depends(get_db_dependency),
):
    """Exchange a one-time mobile authorization code for Curatio credentials."""
    verifier_allowed = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    )
    if not 43 <= len(payload.code_verifier) <= 128 or any(
        ch not in verifier_allowed for ch in payload.code_verifier
    ):
        raise HTTPException(status_code=400, detail="Invalid code_verifier")
    result = exchange_mobile_auth_code(
        db, code=payload.code, code_verifier=payload.code_verifier
    )
    if not result:
        raise HTTPException(status_code=401, detail="Invalid or expired code")
    return result


@app.get("/auth/desktop/start")
@limiter.limit("20/minute")
async def desktop_auth_start(
    request: Request,
    redirect_uri: str,
    code_challenge: str,
    state: str = "",
    code_challenge_method: str = "S256",
    db: Session = Depends(get_db_dependency),
):
    """Start desktop-app sign-in in the system browser (RFC 8252 loopback).

    The Curatio desktop app opens this in the user's browser, where passkeys and
    password managers work, and listens on ``http://127.0.0.1:<port>`` for the
    one-time code. PKCE binds the code to the app instance that asked for it.
    """
    if len(redirect_uri) > 2048 or not is_loopback_redirect_uri(redirect_uri):
        raise HTTPException(status_code=400, detail="Invalid redirect_uri")
    return _native_auth_redirect(
        request,
        db,
        redirect_uri,
        code_challenge,
        state,
        code_challenge_method,
    )


@app.post("/auth/desktop/exchange")
@limiter.limit("20/minute")
async def desktop_auth_exchange(
    request: Request,
    payload: MobileAuthExchangeRequest,
    db: Session = Depends(get_db_dependency),
):
    """Exchange a desktop sign-in code for a long-lived web session token."""
    verifier_allowed = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    )
    if not 43 <= len(payload.code_verifier) <= 128 or any(
        ch not in verifier_allowed for ch in payload.code_verifier
    ):
        raise HTTPException(status_code=400, detail="Invalid code_verifier")
    result = exchange_desktop_auth_code(
        db, code=payload.code, code_verifier=payload.code_verifier
    )
    if not result:
        raise HTTPException(status_code=401, detail="Invalid or expired code")
    return result


@app.post("/auth/desktop/renew")
@limiter.limit("30/minute")
async def desktop_auth_renew(
    request: Request,
    db: Session = Depends(get_db_dependency),
):
    """Keep a desktop-app session alive while the app is in use."""
    token = request.cookies.get("user_session")
    session = renew_desktop_session(token, db) if token else None
    if not session:
        raise HTTPException(status_code=401, detail="Not a desktop session")
    return {"max_age": int(DESKTOP_SESSION_LIFETIME.total_seconds())}


@app.post("/auth/pair/create")
async def create_pair(
    request: Request,
    db: Session = Depends(get_db_dependency),
):
    """Create a pairing session for signing into the app.

    Returns a QR code and short code. The app scans the QR or user types
    the short code to receive the active profile's user_key.
    """
    profile = _get_active_profile(request, db)
    result = create_pairing_session(profile.id, db)
    return result


@app.get("/auth/pair/{token}/status")
@limiter.limit("30/minute")
async def pair_status(
    request: Request, token: str, db: Session = Depends(get_db_dependency)
):
    """Poll pairing status without claiming or leaking account credentials."""
    from app.models import AppPairingSession

    from app.auth import _token_digest

    session = (
        db.query(AppPairingSession)
        .filter(AppPairingSession.token == _token_digest(token))
        .first()
    )
    if not session or utc_now() > session.expires_at:
        return {"status": "expired"}
    return {"status": "claimed" if session.claimed else "pending"}


@app.post("/auth/pair/{token}/claim")
@limiter.limit("10/minute")
async def claim_pair_by_token(
    request: Request, token: str, db: Session = Depends(get_db_dependency)
):
    """Claim a QR/token pairing session via POST only."""
    result = claim_pairing_session(db, token=token)
    if not result:
        raise HTTPException(status_code=404, detail="Invalid or expired token")
    return {"status": "ready", **result}


@app.post("/auth/pair/code/{short_code}/claim")
@limiter.limit("10/minute")
async def pair_by_code(
    request: Request, short_code: str, db: Session = Depends(get_db_dependency)
):
    """Claim a short-code pairing session via POST only."""
    result = claim_pairing_session(db, short_code=short_code)
    if not result:
        raise HTTPException(status_code=404, detail="Invalid or expired code")
    return {"status": "ready", **result}


# ---------------------------------------------------------------------------
# Device pairing (tvOS / device-initiated flow)
# ---------------------------------------------------------------------------


@app.post("/auth/device/code")
@limiter.limit("10/minute")
async def device_code(request: Request, db: Session = Depends(get_db_dependency)):
    """Create a device pairing session (no auth required).

    The device (e.g. Apple TV) calls this to get a short code to display
    on screen. The user then enters the code on the web or mobile app
    to link their account.

    Returns device_token (for polling), short_code (to display), and expires_at.
    """
    result = create_device_pairing_session(db)
    return result


@app.get("/auth/device/{device_token}/status")
@limiter.limit("30/minute")
async def device_status(
    request: Request,
    device_token: str,
    db: Session = Depends(get_db_dependency),
):
    """Poll this from the device to check if a user has claimed the code.

    Returns {"status": "pending"} while waiting, or
    {"status": "ready", "user_key": "...", "server_url": "..."} once claimed.
    """
    result = poll_device_pairing_session(db, device_token)
    if not result:
        return {"status": "pending"}
    return {"status": "ready", **result}


@app.post("/auth/device/claim")
@limiter.limit("10/minute")
async def device_claim(
    request: Request,
    payload: DeviceClaimRequest,
    db: Session = Depends(get_db_dependency),
):
    """Authenticated user claims a device pairing code.

    The user enters the short code displayed on the TV into the web/mobile app.
    Requires a valid user_session cookie. The TV is linked to the active profile.
    """
    profile = _get_active_profile(request, db)
    success = claim_device_pairing_session(db, payload.short_code, profile)
    if not success:
        raise HTTPException(status_code=404, detail="Invalid or expired code")
    return {"status": "ok", "message": "Device paired successfully"}


@app.post("/auth/logout")
async def user_logout(
    request: Request,
    db: Session = Depends(get_db_dependency),
):
    """Log out by clearing the user session."""
    token = request.cookies.get("user_session")
    if token:
        delete_user_session(token, db)
    response = JSONResponse(content={"status": "ok"})
    response.delete_cookie("user_session")
    return response


@app.post("/api/v2/scrobble")
async def scrobble(
    user_key: BearerUserKey,
    payload: ScrobbleRequest,
):
    """Record a completed watch event from a Curatio client.

    Only ``action='complete'`` events are accepted (client sends this when
    <= 15 minutes of content remain).  Duplicate events for the same item
    within 60 seconds are silently ignored. Items may be keyed by ``tmdb_id``
    and/or ``imdb_id``; imdb-only items are canonicalized to a tmdb_id when
    TMDB knows them and stored imdb-only otherwise.
    """
    req_tmdb_id, req_imdb_id = _require_sync_ids(payload.tmdb_id, payload.imdb_id)
    tmdb_id, imdb_id, media_type, season, episode = await _canonicalize_write_ids(
        req_tmdb_id, req_imdb_id, payload.media_type, payload.season, payload.episode
    )

    response, sync_user_id = await _db_call(
        _write_scrobble_event,
        user_key,
        payload,
        tmdb_id,
        imdb_id,
        media_type,
        season,
        episode,
    )
    # Deliberately scheduled here rather than inside the threaded half.
    # ``_schedule_local_sync`` falls back to a private thread + private loop when
    # no loop is running, which skips ``_local_sync_semaphore`` — the bound that
    # stops a burst of scrobbles from pinning every pooled connection at once.
    if sync_user_id is not None:
        _schedule_local_sync(sync_user_id)
    return response


def _write_scrobble_event(
    db: Session,
    user_key: str,
    payload: ScrobbleRequest,
    tmdb_id: int | None,
    imdb_id: str | None,
    media_type: str,
    season: int | None,
    episode: int | None,
) -> tuple[dict, int | None]:
    """The DB half of ``scrobble``, run off the loop by ``_db_call``.

    Returns the response and, when a catalog rebuild is warranted, the profile id
    for the caller to schedule on the event loop.
    """
    user = db.query(User).filter(User.user_key == user_key).first()
    if not user:
        raise invalid_profile_credential()
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User is inactive")

    # Deduplicate: ignore if the same item completed within 60 seconds. Match
    # on the canonical tmdb_id when present, otherwise on the imdb_id, and on
    # the exact season/episode so a burst of "mark season watched" scrobbles
    # for distinct episodes is not collapsed into one.
    cutoff = utc_now() - timedelta(seconds=60)
    dedup_query = db.query(WatchEvent.id).filter(
        WatchEvent.user_id == user.id,
        WatchEvent.media_type == media_type,
        WatchEvent.action == "complete",
        WatchEvent.created_at >= cutoff,
        func.coalesce(WatchEvent.season, 0) == (season or 0),
        func.coalesce(WatchEvent.episode, 0) == (episode or 0),
    )
    if tmdb_id is not None:
        dedup_query = dedup_query.filter(WatchEvent.tmdb_id == tmdb_id)
    else:
        dedup_query = dedup_query.filter(WatchEvent.imdb_id == imdb_id)
    if dedup_query.first():
        return {"status": "ok", "deduplicated": True}, None

    event = WatchEvent(
        user_id=user.id,
        tmdb_id=tmdb_id,
        imdb_id=imdb_id,
        media_type=media_type,
        season=season,
        episode=episode,
        action="complete",
        title=payload.title,
    )
    db.add(event)
    from app.home_feed import invalidate_discovery

    invalidate_discovery(db, user.id)
    db.commit()
    _invalidate_dashboard_cache(user.id)

    # Debounced catalog rebuild + metadata backfill is scheduled by the caller.
    return {"status": "ok"}, user.id


@app.post("/api/import-history")
@limiter.limit("10/minute")
async def import_history(request: Request):
    """Import a CSV of TMDB IDs (series) to seed recommendations.

    Accepts a JSON body with a ``csv_text`` field containing one TMDB ID per
    line (or comma-separated).  Items are stored as WatchEvent with
    action='imported' so they influence recommendations without appearing in
    Up Next or counting as fully watched.
    """
    with get_db() as db:
        user = _get_active_profile(request, db)

        body = await request.json()
        csv_text: str = body.get("csv_text", "")
        if not csv_text.strip():
            raise HTTPException(status_code=400, detail="No data provided")

        # Parse TMDB IDs — support one-per-line, comma-separated, or mixed
        raw_tokens = csv_text.replace(",", "\n").splitlines()
        tmdb_ids: list[int] = []
        for token in raw_tokens:
            token = token.strip()
            if not token:
                continue
            try:
                tmdb_ids.append(int(token))
            except ValueError:
                continue  # skip header rows or non-numeric lines

        if not tmdb_ids:
            raise HTTPException(status_code=400, detail="No valid TMDB IDs found")

        # Deduplicate against already-imported IDs for this user
        existing = set(
            r.tmdb_id
            for r in db.query(WatchEvent.tmdb_id)
            .filter(
                WatchEvent.user_id == user.id,
                WatchEvent.media_type == "tv",
                WatchEvent.action == "imported",
            )
            .all()
        )

        added = 0
        for tid in tmdb_ids:
            if tid in existing:
                continue
            existing.add(tid)
            db.add(
                WatchEvent(
                    user_id=user.id,
                    tmdb_id=tid,
                    media_type="tv",
                    action="imported",
                )
            )
            added += 1

        from app.home_feed import invalidate_discovery

        invalidate_discovery(db, user.id)
        db.commit()
        _invalidate_dashboard_cache(user.id)

    # Trigger a catalog rebuild so new imports take effect
    _schedule_local_sync(user.id)

    return {"status": "ok", "imported": added, "skipped": len(tmdb_ids) - added}


# ---------------------------------------------------------------------------
# Watch progress / history endpoints (cross-device Continue Watching)
# ---------------------------------------------------------------------------


class ProgressRequest(BaseModel):
    """Payload sent by the client to report playback position.

    At least one of ``tmdb_id`` / ``imdb_id`` must be present (validated in the
    handler). ``imdb_id`` lets the client report progress for items that have no
    TMDB id; the server canonicalizes it to a tmdb_id when possible.
    """

    tmdb_id: Optional[int] = None
    imdb_id: Optional[str] = None  # 'tt1234567'
    media_type: Literal["movie", "tv"]
    position: int  # seconds into playback
    duration: int  # total runtime in seconds
    season: Optional[int] = None
    episode: Optional[int] = None
    title: Optional[str] = None


# ---------------------------------------------------------------------------
# IMDb id handling: validation, canonicalization (imdb -> tmdb) and caching.
# ---------------------------------------------------------------------------

_IMDB_ID_RE = re.compile(r"^tt\d+$")
# Never block a write longer than this on the TMDB /find network call. On
# timeout we store the imdb-only fallback and upgrade on a later write.
_IMDB_RESOLVE_TIMEOUT = 4.0
# How long to trust a cached "not found" before re-attempting resolution.
_IMDB_NEG_RETRY = timedelta(hours=6)


def _normalize_imdb_id(imdb_id: str | None) -> str | None:
    """Return a normalized ``tt...`` id, or ``None`` if missing/malformed."""
    if not imdb_id:
        return None
    candidate = imdb_id.strip()
    return candidate if _IMDB_ID_RE.match(candidate) else None


def _require_sync_ids(
    tmdb_id: int | None, imdb_id: str | None
) -> tuple[int | None, str | None]:
    """Validate that a write carries at least one usable id.

    Returns ``(tmdb_id, normalized_imdb_id)``. Raises 400 when neither a
    tmdb_id nor a valid imdb_id is present.
    """
    norm_imdb = _normalize_imdb_id(imdb_id)
    if tmdb_id is None and norm_imdb is None:
        raise HTTPException(status_code=400, detail="A tmdb_id or imdb_id is required")
    return tmdb_id, norm_imdb


async def _resolve_imdb_to_tmdb(imdb_id: str) -> dict | None:
    """Resolve an IMDb id to a canonical TMDB id, cached persistently.

    Returns ``{"tmdb_id", "media_type", "season", "episode"}`` on success, or
    ``None`` when unresolved/not-found (the caller then stores an imdb-only
    fallback). The TMDB call is bounded by ``_IMDB_RESOLVE_TIMEOUT`` so a slow
    TMDB never stalls a progress write, and every outcome is cached so each
    imdb id is resolved at most once (negatives retried after a cooldown).
    """
    imdb_id = imdb_id.strip()

    # 1. Persistent cache lookup. This function is called from async request
    # handlers, so the synchronous checkout/query must happen off the event loop.
    # Returning only plain values also keeps an ORM instance from escaping the
    # worker-owned session.
    cached, cache_is_authoritative = await _db_call(
        _read_imdb_resolution_cache, imdb_id
    )
    if cache_is_authoritative:
        return cached

    # 2. Ask TMDB (bounded; best-effort).
    from app.tmdb_client import tmdb_client, parse_find_result

    try:
        data = await asyncio.wait_for(
            tmdb_client.find_by_imdb_id(imdb_id), timeout=_IMDB_RESOLVE_TIMEOUT
        )
    except Exception as e:
        # Timeout / network / TMDB error — transient. Do NOT cache a negative
        # so the next write retries; store the imdb-only fallback for now.
        logger.warning(f"IMDb resolution failed for {imdb_id}: {e}")
        return None

    resolved = parse_find_result(data)

    # 3. Upsert the cache with the outcome in another short, off-loop session.
    try:
        await _db_call(_write_imdb_resolution_cache, imdb_id, resolved)
    except Exception as e:
        logger.warning(f"Could not cache IMDb resolution for {imdb_id}: {e}")

    return resolved


async def _resolve_imdb_to_tmdb_as(
    imdb_id: str, media_type: Literal["movie", "tv"]
) -> dict | None:
    """Resolve an IMDb id to the TMDB record of one media type, if TMDB has one.

    The canonical resolution keeps a single record per IMDb id, movie first.
    TMDB lists some works under both a movie and a TV record, so a series can
    resolve to a movie stub with no episodes. A caller that knows which type it
    is showing asks here for that record. Memory-cached only: the persistent row
    stays the canonical answer for callers that carry no type.
    """
    cache_key = f"imdb-find:{media_type}:{imdb_id}"
    now = time()
    cached = _meta_cache.get(cache_key)
    if cached and (now - cached[0]) < _META_CACHE_TTL:
        return cast(dict, cached[1]) or None

    from app.tmdb_client import tmdb_client, parse_find_result

    try:
        data = await asyncio.wait_for(
            tmdb_client.find_by_imdb_id(imdb_id), timeout=_IMDB_RESOLVE_TIMEOUT
        )
    except Exception as e:
        # Transient: the caller keeps the canonical resolution and may retry.
        logger.warning(f"Typed IMDb resolution failed for {imdb_id}: {e}")
        return None

    resolved = parse_find_result(data, prefer_media_type=media_type)
    if not resolved or resolved.get("media_type") != media_type:
        resolved = None
    _meta_cache[cache_key] = (now, resolved or {})
    _meta_cache_evict()
    return resolved


def _read_imdb_resolution_cache(db: Session, imdb_id: str) -> tuple[dict | None, bool]:
    """Return ``(value, authoritative)`` without leaking an ORM instance."""
    row = db.query(ImdbResolution).filter(ImdbResolution.imdb_id == imdb_id).first()
    if row is None:
        return None, False
    if row.status == "found" and row.tmdb_id is not None:
        return (
            {
                "tmdb_id": row.tmdb_id,
                "media_type": row.media_type,
                "season": row.season,
                "episode": row.episode,
            },
            True,
        )
    # A fresh negative is authoritative; an expired one asks the caller to retry.
    is_fresh_negative = bool(
        row.updated_at and (utc_now() - row.updated_at) < _IMDB_NEG_RETRY
    )
    return None, is_fresh_negative


def _write_imdb_resolution_cache(
    db: Session, imdb_id: str, resolved: dict | None
) -> None:
    """Persist one TMDB find result in a short transaction."""
    row = db.query(ImdbResolution).filter(ImdbResolution.imdb_id == imdb_id).first()
    if row is None:
        row = ImdbResolution(imdb_id=imdb_id)
        db.add(row)
    row.attempts = (row.attempts or 0) + 1
    row.updated_at = utc_now()
    if resolved:
        row.status = "found"
        row.tmdb_id = resolved["tmdb_id"]
        row.media_type = resolved["media_type"]
        row.season = resolved.get("season")
        row.episode = resolved.get("episode")
    else:
        row.status = "not_found"


async def _canonicalize_write_ids(
    tmdb_id: int | None,
    imdb_id: str | None,
    media_type: str,
    season: int | None,
    episode: int | None,
) -> tuple[int | None, str | None, str, int | None, int | None]:
    """Resolve the canonical ids for a write.

    A tmdb_id (when supplied) is authoritative and kept as-is, with any imdb_id
    retained alongside it. An imdb-only write is resolved to its canonical
    tmdb_id (and, for episode imdb ids, its season/episode) when TMDB knows it;
    otherwise it stays imdb-only.

    Returns ``(tmdb_id, imdb_id, media_type, season, episode)``.
    """
    if tmdb_id is not None:
        return tmdb_id, imdb_id, media_type, season, episode

    resolved = await _resolve_imdb_to_tmdb(imdb_id) if imdb_id else None
    if resolved and resolved.get("tmdb_id") is not None:
        r_media = resolved.get("media_type") or media_type
        r_season = season
        r_episode = episode
        if resolved.get("season") is not None:
            r_season = resolved["season"]
        if resolved.get("episode") is not None:
            r_episode = resolved["episode"]
        return resolved["tmdb_id"], imdb_id, r_media, r_season, r_episode

    # Fallback: keep it imdb-only. It still syncs cross-device.
    return None, imdb_id, media_type, season, episode


def _media_year(
    meta: MediaMetadata | None, fallback_year: str | None = None
) -> str | None:
    if meta and meta.release_date and len(meta.release_date) >= 4:
        return meta.release_date[:4]
    return fallback_year


def _image_url(path: str | None, size: str = "w780") -> str | None:
    if not path:
        return None
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"https://image.tmdb.org/t/p/{size}{path}"


def _logo_path_from_detail(detail: dict) -> str | None:
    """Choose the best title logo path from a TMDB detail payload."""
    images = detail.get("images")
    logos = images.get("logos", []) if isinstance(images, dict) else []
    if not logos:
        return None
    en_logos = [lg for lg in logos if lg.get("iso_639_1") in ("en", None)]
    chosen = en_logos[0] if en_logos else logos[0]
    return chosen.get("file_path")


def _playback_id_for_item(item: dict) -> str:
    season = item.get("season") or 1
    episode = item.get("episode") or 1
    if item.get("media_type") == "tv":
        if item.get("tmdb_id"):
            return f"tmdb:{item['tmdb_id']}:{season}:{episode}"
        if item.get("imdb_id"):
            return f"{item['imdb_id']}:{season}:{episode}"
        return f"tmdb:{item.get('tmdb_id')}:{season}:{episode}"
    return item.get("imdb_id") or f"tmdb:{item.get('tmdb_id')}"


def _stored_content_rating(meta: MediaMetadata | None) -> str | None:
    """Return the best certification already persisted with title metadata."""
    if meta is None:
        return None
    for facts in (meta.mdb_facts, meta.tag_facts):
        if not isinstance(facts, dict):
            continue
        value = str(facts.get("certification") or "").strip()
        if value:
            return value
    return None


#: Exactly the ``MediaMetadata`` columns :func:`_serialize_media_item` reads.
#:
#: An unqualified ``db.query(MediaMetadata)`` also selects ``raw_data`` — the stored
#: TMDB payload, ~5KB per row once ``workers/enrich.py`` has pruned it and 50-250KB
#: before that. No card path touches it, so every such query was fetching and
#: ``json.loads``-ing a few hundred KB per request to throw it away. Eight other
#: queries in this codebase already defer it for exactly that reason, and
#: ``tests/test_upgrade_safety.py`` guards one of them.
#:
#: ``load_only`` rather than ``defer(raw_data)`` on purpose: a whitelist does not
#: silently start shipping the next large JSON column somebody adds to the model.
_MEDIA_CARD_COLUMNS = (
    MediaMetadata.tmdb_id,
    MediaMetadata.media_type,
    MediaMetadata.title,
    MediaMetadata.overview,
    MediaMetadata.release_date,
    MediaMetadata.genres,
    MediaMetadata.digital_release_date,
    MediaMetadata.next_episode_air_date,
    MediaMetadata.poster_path,
    MediaMetadata.backdrop_path,
    MediaMetadata.imdb_id,
    MediaMetadata.logo_path,
    MediaMetadata.vote_average,
    MediaMetadata.mdb_facts,
    MediaMetadata.tag_facts,
    MediaMetadata.adult,
)


#: The card columns plus what search additionally ranks and classifies on.
#: Everything :func:`_search_media_multi` reads except ``raw_data``.
_SEARCH_ROW_COLUMNS = _MEDIA_CARD_COLUMNS + (
    MediaMetadata.popularity,
    MediaMetadata.vote_count,
    MediaMetadata.rating_consensus,
    MediaMetadata.number_of_seasons,
    MediaMetadata.number_of_episodes,
    MediaMetadata.collection_id,
    MediaMetadata.collection_name,
)


def _serialize_media_item(
    *,
    tmdb_id: int | None,
    media_type: str,
    title: str | None = None,
    season: int | None = None,
    episode: int | None = None,
    position: int | None = None,
    duration: int | None = None,
    updated_at: datetime | None = None,
    completed_at: datetime | None = None,
    action: str | None = None,
    meta: MediaMetadata | None = None,
    poster_path: str | None = None,
    year: str | None = None,
    imdb_id: str | None = None,
) -> dict:
    display_title = title or (meta.title if meta else None) or "Untitled"
    genres: list[str] = (
        cast(list[str], meta.genres) if meta and isinstance(meta.genres, list) else []
    )
    item = {
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "title": display_title,
        "season": season,
        "episode": episode,
        "position": position,
        "duration": duration,
        "updated_at": updated_at.isoformat() + "Z" if updated_at else None,
        "completed_at": completed_at.isoformat() + "Z" if completed_at else None,
        "action": action,
        "year": _media_year(meta, year),
        "overview": meta.overview if meta else None,
        "genres": genres,
        "poster_path": poster_path or (meta.poster_path if meta else None),
        "backdrop_path": meta.backdrop_path if meta else None,
        "poster": _image_url(
            poster_path or (meta.poster_path if meta else None), "w500"
        ),
        "backdrop": _image_url(meta.backdrop_path if meta else None, "w780"),
        "logo_path": meta.logo_path if meta else None,
        "logo": _image_url(meta.logo_path if meta else None, "original"),
        # Prefer the id stored on the row; fall back to the TMDB metadata cache.
        "imdb_id": imdb_id or (meta.imdb_id if meta else None),
        "rating": meta.vote_average if meta else None,
        # MPAA/TV certification (PG-13, R, TV-MA, ...), sourced from the
        # persisted TMDB/MDBList enrichment so cards do not need a detail call.
        "content_rating": _stored_content_rating(meta),
        # Additive safety metadata for native clients. Search filters server-side,
        # but the client also searches cached catalog cards and needs this signal
        # to keep an adult-flagged item from being reintroduced locally.
        "adult": getattr(meta, "adult", False) is True if meta else False,
    }
    if meta is not None:
        coming_soon = _coming_soon_date(
            {
                "digital_release_date": meta.digital_release_date,
                "next_episode_air_date": meta.next_episode_air_date,
            },
            media_type,
        )
        if coming_soon:
            item["comingSoonDate"] = coming_soon
    if duration:
        item["progress_percent"] = min(
            100, max(0, round(((position or 0) / duration) * 100))
        )
    if media_type == "tv" and season is not None and episode is not None:
        item["subtitle"] = f"S{season}:E{episode}"
    else:
        item["subtitle"] = item["year"] or ""
    item["video_id"] = _playback_id_for_item(item)
    item["content_type"] = _api_media_type(media_type)
    return item


def _serialize_search_result(it: dict) -> dict:
    """Serialize a ranked search item into the web dashboard shape.

    Items backed by an ORM row reproduce ``_serialize_media_item`` output exactly,
    then fill any gaps from the search item's own fields.
    """
    row = it.get("_row")
    if row is not None:
        base = _serialize_media_item(
            tmdb_id=row.tmdb_id,
            # Search may have repaired a stale composite-key classification
            # using IMDb or TV-specific detail evidence. Serialize that result,
            # not the stale ORM key.
            media_type=it.get("media_type") or row.media_type,
            title=row.title,
            meta=row,
        )
    else:
        base = _serialize_media_item(
            tmdb_id=it.get("tmdb_id"),
            media_type=it["media_type"],
            title=it.get("title"),
            imdb_id=it.get("imdb_id"),
            poster_path=it.get("poster"),
            year=it.get("year"),
            meta=None,
        )

    # Fill gaps from the search item; never overwrite values we already have.
    if base.get("rating") is None and it.get("rating") is not None:
        base["rating"] = it["rating"]
    if not base.get("genres") and it.get("genres"):
        base["genres"] = it["genres"]
    if not base.get("overview") and it.get("description"):
        base["overview"] = it["description"]
    if not base.get("year") and it.get("year"):
        base["year"] = it["year"]
        if not base.get("subtitle"):
            base["subtitle"] = it["year"]
    if not base.get("poster") and it.get("poster"):
        base["poster"] = _image_url(it["poster"], "w500")
    if it.get("adult") is True:
        base["adult"] = True
    return base


def _metadata_map(
    db: Session, rows: list[object]
) -> dict[tuple[int, str], MediaMetadata]:
    keys = {
        (int(getattr(row, "tmdb_id")), str(getattr(row, "media_type")))
        for row in rows
        if getattr(row, "tmdb_id", None) is not None
        and getattr(row, "media_type", None)
    }
    if not keys:
        return {}
    # Match the (tmdb_id, media_type) pairs, not their cross product. Two
    # independent IN clauses also return the *other* media type's row for any
    # tmdb_id shared by a film and a show, which the loop below then discards --
    # up to twice the rows for nothing.
    rows = cast(
        list[MediaMetadata],
        db.query(MediaMetadata)
        .options(load_only(*_MEDIA_CARD_COLUMNS))
        .filter(
            tuple_(MediaMetadata.tmdb_id, MediaMetadata.media_type).in_(
                [(k[0], k[1]) for k in keys]
            )
        )
        .all(),
    )
    metadata: dict[tuple[int, str], MediaMetadata] = {}
    for row in rows:
        key = (int(row.tmdb_id), str(row.media_type))
        if key in keys:
            metadata[key] = row
    return metadata


def _imdb_id_fallback_map(
    db: Session, rows: list[object]
) -> dict[tuple[int, str], str]:
    """Map (tmdb_id, media_type) -> imdb_id from the cached TMDB metadata.

    Used to echo an imdb_id on rows that don't carry one of their own (e.g.
    legacy tmdb-only rows), via a single cheap DB lookup — no TMDB call.
    """
    keys = {
        (int(getattr(row, "tmdb_id")), str(getattr(row, "media_type")))
        for row in rows
        if getattr(row, "tmdb_id", None) is not None
        and getattr(row, "media_type", None)
        and not getattr(row, "imdb_id", None)
    }
    if not keys:
        return {}
    meta_rows = (
        db.query(MediaMetadata.tmdb_id, MediaMetadata.media_type, MediaMetadata.imdb_id)
        .filter(
            MediaMetadata.tmdb_id.in_([k[0] for k in keys]),
            MediaMetadata.media_type.in_([k[1] for k in keys]),
            MediaMetadata.imdb_id.isnot(None),
        )
        .all()
    )
    return {
        (int(m.tmdb_id), str(m.media_type)): m.imdb_id
        for m in meta_rows
        if (int(m.tmdb_id), str(m.media_type)) in keys
    }


def _progress_items(
    db: Session,
    user: User,
    limit: int = 24,
    *,
    loaded_rows: list | None = None,
    loaded_metas: dict[tuple[int, str], MediaMetadata] | None = None,
) -> list[dict]:
    rows = (
        loaded_rows
        if loaded_rows is not None
        else (
            db.query(WatchProgress)
            .filter(WatchProgress.user_id == user.id)
            .order_by(WatchProgress.updated_at.desc())
            .limit(limit)
            .all()
        )
    )
    metas = loaded_metas if loaded_metas is not None else _metadata_map(db, rows)
    return [
        _serialize_media_item(
            tmdb_id=r.tmdb_id,
            media_type=r.media_type,
            title=r.title,
            season=r.season if r.media_type == "tv" else None,
            episode=r.episode if r.media_type == "tv" else None,
            position=r.position,
            duration=r.duration,
            updated_at=r.updated_at,
            meta=metas.get((r.tmdb_id, r.media_type)),
            imdb_id=r.imdb_id,
        )
        for r in rows
    ]


def _history_items(
    db: Session,
    user: User,
    limit: int = 36,
    *,
    loaded_rows: list | None = None,
    loaded_metas: dict[tuple[int, str], MediaMetadata] | None = None,
) -> list[dict]:
    rows = (
        loaded_rows
        if loaded_rows is not None
        else (
            db.query(WatchEvent)
            .filter(WatchEvent.user_id == user.id)
            .order_by(WatchEvent.created_at.desc())
            .limit(limit)
            .all()
        )
    )
    metas = loaded_metas if loaded_metas is not None else _metadata_map(db, rows)
    return [
        _serialize_media_item(
            tmdb_id=r.tmdb_id,
            media_type=r.media_type,
            title=r.title,
            season=r.season,
            episode=r.episode,
            completed_at=r.created_at,
            action=r.action,
            meta=metas.get((r.tmdb_id, r.media_type)),
            imdb_id=r.imdb_id,
        )
        for r in rows
    ]


def _library_items_for_user(
    db: Session,
    user: User,
    limit: int = 36,
    *,
    loaded_rows: list | None = None,
    loaded_metas: dict[tuple[int, str], MediaMetadata] | None = None,
) -> list[dict]:
    rows = (
        loaded_rows
        if loaded_rows is not None
        else (
            db.query(LibraryItem)
            .filter(LibraryItem.user_id == user.id)
            .order_by(LibraryItem.added_at.desc())
            .limit(limit)
            .all()
        )
    )
    metas = loaded_metas if loaded_metas is not None else _metadata_map(db, rows)
    return [
        _serialize_media_item(
            tmdb_id=r.tmdb_id,
            media_type=r.media_type,
            title=r.title,
            poster_path=r.poster_path,
            year=r.year,
            updated_at=r.added_at,
            meta=metas.get((r.tmdb_id, r.media_type)),
            imdb_id=r.imdb_id,
        )
        for r in rows
    ]


def _dashboard_activity_items(
    db: Session, user: User
) -> tuple[list[dict], list[dict], list[dict]]:
    """Load progress, history, and library with one shared metadata lookup."""
    progress_rows = (
        db.query(WatchProgress)
        .filter(WatchProgress.user_id == user.id)
        .order_by(WatchProgress.updated_at.desc())
        .limit(24)
        .all()
    )
    history_rows = (
        db.query(WatchEvent)
        .filter(WatchEvent.user_id == user.id)
        .order_by(WatchEvent.created_at.desc())
        .limit(36)
        .all()
    )
    library_rows = (
        db.query(LibraryItem)
        .filter(LibraryItem.user_id == user.id)
        .order_by(LibraryItem.added_at.desc())
        .limit(36)
        .all()
    )
    metas = _metadata_map(db, [*progress_rows, *history_rows, *library_rows])
    return (
        _progress_items(db, user, loaded_rows=progress_rows, loaded_metas=metas),
        _history_items(db, user, loaded_rows=history_rows, loaded_metas=metas),
        _library_items_for_user(db, user, loaded_rows=library_rows, loaded_metas=metas),
    )


#: Playback fields that only ever apply to the activity collections, never to a
#: catalog row item. See the strip in :func:`_catalog_row_from_items`.
_CATALOG_ITEM_DEAD_KEYS = (
    "season",
    "episode",
    "position",
    "duration",
    "updated_at",
    "completed_at",
    "action",
)


def _catalog_row_from_items(
    name: str,
    catalog_id: str,
    catalog_type: str,
    items: list[dict],
    *,
    limit: int = 16,
    source: str | None = None,
    generation_method: str | None = None,
    generation_params: dict | None = None,
    revision: str | None = None,
    shuffle: bool = True,
) -> dict:
    """Serialize already-loaded catalog content into a dashboard shelf."""
    params = generation_params if isinstance(generation_params, dict) else {}
    reason = None
    if generation_method == "because_you_watched":
        seed = params.get("seed") if isinstance(params.get("seed"), dict) else {}
        if not seed:
            seed = {
                "tmdb_id": params.get("seed_tmdb_id"),
                "media_type": "tv" if catalog_type == "series" else "movie",
                "title": params.get("seed_title"),
            }
        seed = {
            "tmdb_id": seed.get("tmdb_id"),
            "media_type": seed.get("media_type"),
            "title": seed.get("title"),
        }
        if seed.get("title"):
            # The product copy and reason are projections of one stored seed,
            # never independently-computed values that can drift.
            name = f"Because You Watched {seed['title']}"
        reason = {
            "kind": "because_you_watched",
            "seed": seed,
            "basis_tags": list(params.get("basis_tags") or []),
        }
    media_type = "tv" if catalog_type == "series" else "movie"
    valid_items = [
        item
        for item in (_shuffle_items(items, catalog_id) if shuffle else items)
        if item.get("tmdb_id")
    ]
    content_revisions = {
        str(item.get("_snapshot_revision"))
        for item in valid_items
        if item.get("_snapshot_revision")
    }
    if revision and content_revisions and content_revisions != {revision}:
        # Publication is atomic, but under READ COMMITTED a request can read
        # the old row label immediately before the commit and the new contents
        # immediately after it. Suppress that one inconsistent read; the next
        # request uses the new revision and can never pair the two snapshots.
        logger.warning(
            f"catalog {catalog_id} crossed snapshot revisions; suppressing stale read"
        )
        valid_items = []
    serialized = [
        _serialize_media_item(
            tmdb_id=i.get("tmdb_id"),
            media_type=media_type,
            title=i.get("title"),
            meta=None,
        )
        | {
            "overview": i.get("description") or i.get("overview"),
            "genres": i.get("genres") or [],
            "poster_path": i.get("poster"),
            "backdrop_path": i.get("backdrop"),
            "poster": _image_url(i.get("poster"), "w500"),
            "backdrop": _image_url(i.get("backdrop"), "w780"),
            # Same URL under the `background` spelling. The Apple client's
            # card model declares `background` (it decodes these rows with
            # its card mapper), and this shape only ever emitted `backdrop` -- so
            # `backdropURL` was nil on every native-bootstrap card and both the
            # hero billboard and the card preview art silently fell back to the
            # poster. The web dashboard reads `backdrop_path`/`backdrop`
            # (src/api/mediaUtils.ts), so all three stay.
            "background": _image_url(i.get("backdrop"), "w780"),
            "logo_path": i.get("logo"),
            "logo": _image_url(i.get("logo"), "original"),
            "imdb_id": i.get("imdb_id"),
            "year": i.get("year"),
            "rating": i.get("rating"),
            "rating_source": i.get("rating_source"),
            "content_rating": i.get("content_rating"),
            "digitalReleaseInfo": i.get("digital_release_date"),
            "digital_release_date": i.get("digital_release_date"),
            "next_episode_air_date": i.get("next_episode_air_date"),
        }
        for i in valid_items[:limit]
    ]
    for item in serialized:
        # `_serialize_media_item` is shared with the progress/history/library
        # collections, where these carry an episode and a resume position. A
        # catalog row is built with `meta=None` and no playback arguments, so all
        # seven are structurally `None` here -- ~7 dead keys on every one of the
        # up-to-960 items in a bootstrap page, which on tvOS is thousands of
        # keyed decodes through JSONDecoder for nothing.
        #
        # Safe to omit rather than send as null: the native contract states that
        # "Missing, null, and empty collections are normal states", Swift decodes
        # a missing key and an explicit null to the same `nil`, and the web
        # dashboard reads these through `??`.
        for dead_key in _CATALOG_ITEM_DEAD_KEYS:
            item.pop(dead_key, None)

    for item, source_item in zip(serialized, valid_items[:limit]):
        coming_soon = _coming_soon_date(source_item, media_type)
        if coming_soon:
            item["comingSoonDate"] = coming_soon
        # Native bootstrap rows intentionally carry the camelCase card aliases
        # as well as the richer dashboard fields. Apple clients can therefore
        # reuse their battle-tested card -> MediaItem mapper while
        # migrating Home from N catalog requests to one paged response.
        # Catalog identity is the title, not the default playback target.  TV
        # playback ids carry a season/episode suffix (for example
        # ``tmdb:615:1:1``); exposing that value as ``id`` makes clients address
        # metadata and persistence with an episode-shaped key.  Keep ``video_id``
        # for playback while matching the canonical ``tmdb:<id>`` title id.
        item["id"] = f"tmdb:{item['tmdb_id']}"
        item["type"] = catalog_type
        item["name"] = item.get("title")
        item["description"] = item.get("overview")
        item["releaseInfo"] = item.get("year")
        item["imdbId"] = item.get("imdb_id")
        item["tmdbId"] = item.get("tmdb_id")
        item["contentRating"] = item.get("content_rating")
    return {
        "id": catalog_id,
        "name": name,
        "type": media_type,
        "media_type": media_type,
        "catalog_type": catalog_type,
        "items": serialized,
        "item_count": len(serialized),
        "source": source,
        "generation_method": generation_method,
        "revision": revision,
        "reason": reason,
        "poster": False,
    }


def _catalog_row_for_web(
    name: str,
    catalog_id: str,
    catalog_type: str,
    user: User,
    db: Session,
    limit: int = 16,
    taste_tags: list[int] | None = None,
    exclude_ids: set[int] | None = None,
) -> dict:
    if catalog_id.startswith("personal-"):
        actual_id = catalog_id.replace("personal-", "", 1)
        tmdb_sourced = is_tmdb_sourced_slot(actual_id)
        if tmdb_sourced and taste_tags is None:
            taste_tags, exclude_ids = _get_user_taste_data(user.id, db)
        items = _get_cached_catalog(
            actual_id,
            db,
            user_id=user.id,
            hide_foreign=settings.hide_foreign,
            hide_adult=settings.hide_adult,
            hide_unreleased=False,
            cache_empty=user.last_sync is not None,
            revision=user.last_sync.isoformat() if user.last_sync else "pending",
            # Taste must reach the TMDB-sourced rows too, not only the
            # universal rows.
            user_taste_tag_ids=taste_tags if tmdb_sourced else None,
            exclude_ids=exclude_ids if tmdb_sourced else None,
        )
    else:
        actual_id = catalog_id.replace("universal-", "", 1)
        # Taste data is identical for every universal row in a single request, so
        # callers should precompute it once and pass it in. Fall back to a lazy
        # lookup (still TTL-cached) when called outside the dashboard loop.
        if taste_tags is None:
            taste_tags, exclude_ids = _get_user_taste_data(user.id, db)
        items = _get_cached_catalog(
            actual_id,
            db,
            hide_foreign=settings.hide_foreign,
            hide_adult=settings.hide_adult,
            hide_unreleased=settings.hide_unreleased,
            user_taste_tag_ids=taste_tags,
            exclude_ids=exclude_ids,
        )
    return _catalog_row_from_items(name, catalog_id, catalog_type, items, limit=limit)


#: Slot families the hero may be drawn from, best first.
#:
#: The hero is the largest thing on the page, and it used to be sourced
#: unconditionally from ``trending-movie`` -- i.e. TMDB's top trending title,
#: identical for every profile, presented under a hardcoded "Trending Now"
#: eyebrow as though it were a recommendation. Prefer a row that is actually
#: about the viewer, and fall back to the feed only when none exists (a brand
#: new profile with no history).
_HERO_ROW_PREFERENCE: tuple[str, ...] = (
    "picks-movie",
    "byw-movie-",
    "rec-movie",
    "taste-movie-",
    "trending-movie",
)


def _hero_row_rank(row: dict) -> int:
    """Index into _HERO_ROW_PREFERENCE, or a sentinel meaning "not a hero row"."""
    row_id = str(row.get("id") or "").lower()
    row_type = str(row.get("media_type") or row.get("type") or "").lower()
    if row_type != "movie":
        return len(_HERO_ROW_PREFERENCE)
    for index, prefix in enumerate(_HERO_ROW_PREFERENCE):
        if prefix in row_id:
            return index
    return len(_HERO_ROW_PREFERENCE)


def _dashboard_hero_for_user(catalogs: list[dict]) -> dict | None:
    """Pick the hero from the most personal movie row available.

    Returns the row's own name alongside the item as ``hero_source_name`` so
    the client can label the hero with the shelf it came from instead of the
    fixed "Trending Now" string it has always used.
    """
    ranked = sorted(
        (row for row in catalogs if _hero_row_rank(row) < len(_HERO_ROW_PREFERENCE)),
        key=_hero_row_rank,
    )
    for row in ranked:
        items = [item for item in row.get("items", []) if item]
        chosen = next(
            (i for i in items if i.get("backdrop_path") or i.get("backdrop")),
            None,
        ) or next(iter(items), None)
        if chosen:
            return {**chosen, "hero_source_name": row.get("name")}
    return None


@app.get("/account/api/me")
def account_api_me(request: Request, db: Session = Depends(get_db_dependency)):
    """Return signed-in account details, its profiles, and the active profile."""
    account = _get_account_from_session(request, db)
    profiles = _account_profiles(db, account)
    active = _get_active_profile(request, db)
    return {
        "id": account.id,
        "email": account.email,
        "display_name": active.display_name or account.email,
        "totp_enabled": account.totp_enabled,
        "active_profile_id": active.id,
        "profiles": [_profile_summary(p) for p in profiles],
    }


class ProfileCreateRequest(BaseModel):
    """Payload to create a new viewing profile under the current account."""

    display_name: str = ""
    avatar: Optional[str] = None
    # "auto": the client decides per request from measured speed (native BandwidthRequestPolicy).
    bandwidth_tier: Optional[Literal["low", "high", "auto"]] = None
    # What Home and search show: the whole catalog or only the media servers.
    content_scope: Optional[Literal["everything", "my_servers"]] = None


class ProfileUpdateRequest(BaseModel):
    """Payload to update a viewing profile. Omitted fields are left unchanged."""

    display_name: Optional[str] = None
    avatar: Optional[str] = None
    # "auto": the client decides per request from measured speed (native BandwidthRequestPolicy).
    bandwidth_tier: Optional[Literal["low", "high", "auto"]] = None
    content_scope: Optional[Literal["everything", "my_servers"]] = None
    # Use watch history from the connected Plex/Jellyfin account.
    server_history: Optional[Literal["auto", "on", "off"]] = None


# Soft cap on profiles per account to prevent runaway provisioning.
MAX_PROFILES_PER_ACCOUNT = 10


# --- Shared profile CRUD core (used by both cookie- and user_key-authed routes) ---


def _create_profile_for_account(
    account: Account, payload: ProfileCreateRequest, db: Session
) -> User:
    """Create a profile under an account, enforcing the per-account cap."""
    if len(_account_profiles(db, account)) >= MAX_PROFILES_PER_ACCOUNT:
        raise HTTPException(
            status_code=400,
            detail=f"Profile limit reached (max {MAX_PROFILES_PER_ACCOUNT})",
        )
    return _create_profile(
        db,
        account,
        display_name=payload.display_name.strip() or None,
        avatar=payload.avatar,
        bandwidth_tier=payload.bandwidth_tier or "high",
        content_scope=payload.content_scope or "everything",
    )


def _apply_profile_update(
    account: Account, profile_id: int, payload: ProfileUpdateRequest, db: Session
) -> User:
    """Update a profile owned by the account. Omitted fields are left as-is."""
    profile = (
        db.query(User)
        .filter(User.id == profile_id, User.account_id == account.id)
        .first()
    )
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    if payload.display_name is not None:
        profile.display_name = payload.display_name.strip() or None
    if payload.avatar is not None:
        profile.avatar = payload.avatar or None
    if payload.bandwidth_tier is not None:
        profile.bandwidth_tier = payload.bandwidth_tier
    scope_changed = (
        payload.content_scope is not None
        and payload.content_scope != getattr(profile, "content_scope", None)
    )
    if payload.content_scope is not None:
        profile.content_scope = payload.content_scope
    history_changed = False
    if payload.server_history is not None:
        from app import server_history

        wanted = server_history.normalize_setting(payload.server_history)
        if wanted != server_history.normalize_setting(profile.server_history):
            profile.server_history = wanted
            history_changed = True
            if server_history.uses_server_history(db, profile):
                server_history.import_for(db, profile)
            else:
                server_history.forget_for(db, profile.id)
    db.commit()
    db.refresh(profile)
    if history_changed:
        _invalidate_dashboard_cache(profile.id)
        _schedule_local_sync(profile.id)
    if scope_changed:
        # Home is a published snapshot; rebuild it for the new scope now.
        from app.home_feed import invalidate_published_discovery

        invalidate_published_discovery(profile.id)
    return profile


def _delete_profile_for_account(account: Account, profile_id: int, db: Session) -> None:
    """Delete a profile (cascade) owned by the account; keeps at least one."""
    profiles = _account_profiles(db, account)
    if len(profiles) <= 1:
        raise HTTPException(status_code=400, detail="Cannot delete your only profile")
    profile = next((p for p in profiles if p.id == profile_id), None)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    user_key = profile.user_key
    db.delete(profile)
    db.commit()
    # Drop the deleted profile from the user_key lookup cache. Sessions whose
    # active profile was deleted fall back to the default (active_profile_id is
    # SET NULL) on their next request.
    _user_cache.pop(user_key, None)


def _active_profile_or_403(db: Session, request: Request) -> User:
    """Cookie-authenticated active profile, or 403 when the account is off."""
    user = _get_active_profile(request, db)
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is deactivated")
    return user


def _account_for_user_key(user_key: str, db: Session) -> tuple[Account, User]:
    """Resolve the owning Account (and calling profile) from a profile user_key.

    Possessing any profile's user_key authorizes account-level profile
    management — the same household trust boundary used everywhere else a
    user_key is the credential.
    """
    profile = _require_active_user_by_key(user_key, db)
    account = (
        db.query(Account).filter(Account.id == profile.account_id).first()
        if profile.account_id
        else None
    )
    if not account or not account.is_active:
        raise invalid_profile_credential()
    return account, profile


@app.get("/account/api/profiles")
def list_profiles(request: Request, db: Session = Depends(get_db_dependency)):
    """List the viewing profiles for the signed-in account."""
    account = _get_account_from_session(request, db)
    active = _get_active_profile(request, db)
    return {
        "active_profile_id": active.id,
        "profiles": [_profile_summary(p) for p in _account_profiles(db, account)],
    }


@app.post("/account/api/profiles")
def create_profile(
    request: Request,
    payload: ProfileCreateRequest,
    db: Session = Depends(get_db_dependency),
):
    """Create a new profile (with its own native API ``user_key``) for the account."""
    account = _get_account_from_session(request, db)
    profile = _create_profile_for_account(account, payload, db)
    return {"status": "ok", "profile": _profile_summary(profile)}


@app.patch("/account/api/profiles/{profile_id}")
def update_profile(
    profile_id: int,
    request: Request,
    payload: ProfileUpdateRequest,
    db: Session = Depends(get_db_dependency),
):
    """Rename a profile or change its avatar / bandwidth tier."""
    account = _get_account_from_session(request, db)
    profile = _apply_profile_update(account, profile_id, payload, db)
    return {"status": "ok", "profile": _profile_summary(profile)}


@app.delete("/account/api/profiles/{profile_id}")
def delete_profile(
    profile_id: int, request: Request, db: Session = Depends(get_db_dependency)
):
    """Delete a profile and all its watch history (cascade). Keeps at least one."""
    account = _get_account_from_session(request, db)
    _delete_profile_for_account(account, profile_id, db)
    return {"status": "ok"}


@app.post("/account/api/profiles/{profile_id}/select")
def select_profile(
    profile_id: int, request: Request, db: Session = Depends(get_db_dependency)
):
    """Switch the active profile for the current web session."""
    account = _get_account_from_session(request, db)
    profile = (
        db.query(User)
        .filter(
            User.id == profile_id,
            User.account_id == account.id,
            User.is_active.is_(True),
        )
        .first()
    )
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    token = request.cookies.get("user_session")
    if not token or not set_session_active_profile(token, db, profile.id):
        raise HTTPException(status_code=401, detail="Session expired")
    return {"status": "ok", "profile": _profile_summary(profile)}


# --- Native (user_key-authed) profile API ---
# These mirror the cookie-authed CRUD above for native clients (iOS/tvOS) that
# hold a profile's user_key rather than a web session cookie. Switching profiles
# on a native client is purely local (use the target profile's user_key), so
# there is intentionally no `/select` here. See docs/multi-profile-ios.md.


@app.get("/api/v2/profiles")
def list_profiles_for_key(
    user_key: BearerUserKey, db: Session = Depends(get_db_dependency)
):
    """List sibling profiles for the account that owns this profile user_key.

    ``active_profile_id`` is the profile whose key was used to call this.
    """
    account, caller = _account_for_user_key(user_key, db)
    return {
        "active_profile_id": caller.id,
        "profiles": [_profile_summary(p) for p in _account_profiles(db, account)],
    }


@app.post("/api/v2/profiles")
def create_profile_for_key(
    user_key: BearerUserKey,
    payload: ProfileCreateRequest,
    db: Session = Depends(get_db_dependency),
):
    """Create a profile under the account that owns this user_key."""
    account, _ = _account_for_user_key(user_key, db)
    profile = _create_profile_for_account(account, payload, db)
    return {"status": "ok", "profile": _profile_summary(profile)}


def _rotate_profile_key(account: Account, profile_id: int, db: Session) -> User:
    """Give an account's profile a fresh user_key; the old one stops at once."""
    profile = next(
        (p for p in _account_profiles(db, account) if p.id == profile_id), None
    )
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    old_key = profile.user_key
    profile.user_key = secrets.token_urlsafe(32)
    db.commit()
    db.refresh(profile)
    _user_cache.pop(old_key, None)
    logger.info(f"Rotated user_key for profile {profile.id}")
    return profile


@app.post("/account/api/profiles/{profile_id}/rotate-key")
def rotate_profile_key(
    request: Request, profile_id: int, db: Session = Depends(get_db_dependency)
):
    """Signed-in account: issue a new key for one of its profiles.

    Every app paired to that profile has to pair again.
    """
    account = _get_account_from_session(request, db)
    profile = _rotate_profile_key(account, profile_id, db)
    return {"status": "ok", "profile": _profile_summary(profile)}


@app.post("/api/v2/profiles/{profile_id}/rotate-key")
def rotate_profile_key_for_key(
    user_key: BearerUserKey,
    profile_id: int,
    db: Session = Depends(get_db_dependency),
):
    """Native clients: issue a new key for a profile in the caller's account.

    Same household trust boundary as renaming or deleting a profile. When the
    caller rotates its own profile, it must switch to the returned key.
    """
    account, _ = _account_for_user_key(user_key, db)
    profile = _rotate_profile_key(account, profile_id, db)
    return {
        "status": "ok",
        "user_key": profile.user_key,
        "profile": _profile_summary(profile),
    }


@app.patch("/api/v2/profiles/{profile_id}")
def update_profile_for_key(
    user_key: BearerUserKey,
    profile_id: int,
    payload: ProfileUpdateRequest,
    db: Session = Depends(get_db_dependency),
):
    """Rename/avatar/bandwidth a profile under this user_key's account."""
    account, _ = _account_for_user_key(user_key, db)
    profile = _apply_profile_update(account, profile_id, payload, db)
    return {"status": "ok", "profile": _profile_summary(profile)}


@app.delete("/api/v2/profiles/{profile_id}")
def delete_profile_for_key(
    user_key: BearerUserKey, profile_id: int, db: Session = Depends(get_db_dependency)
):
    """Delete a profile (cascade) under this user_key's account; keeps one."""
    account, _ = _account_for_user_key(user_key, db)
    _delete_profile_for_account(account, profile_id, db)
    return {"status": "ok"}


def _catalog_state_revision(user: User, db: Session) -> str:
    """Cheap, cross-worker revision for one profile's discovery surface.

    In-process cache eviction only reaches the worker that performed a rebuild.
    Including persisted category/content timestamps makes every worker miss its
    stale dashboard cache after publication, and gives native clients a small
    value they can compare when returning to the foreground.

    The profile's *activity* timestamps are part of this too, because
    ``/api/bootstrap`` ships ``continue_watching``, ``recent_history`` and
    ``library`` in the same document as the discovery rows. Without them:

    * the bootstrap ETag (``"{revision}:{cursor}:{limit}"``) could be identical
      across two responses whose activity collections differ, so it was unsafe to
      answer a conditional request with it; and
    * a title started on *another device* changed nothing this value could see, so
      the client's documented foreground rule -- probe the revision, refetch only
      when it moves -- never fired, and Continue Watching stayed stale until some
      unrelated catalog change happened to bump the revision.
    """
    universal_category_at, universal_content_at = (
        db.query(
            func.max(UniversalCategory.updated_at),
            func.max(UniversalCatalogContent.last_updated),
        )
        .select_from(UniversalCategory)
        .outerjoin(
            UniversalCatalogContent,
            UniversalCatalogContent.category_id == UniversalCategory.id,
        )
        .filter(
            UniversalCategory.is_active.is_(True),
            UniversalCategory.source == "generated",
        )
        .first()
    ) or (None, None)
    # The three activity high-water marks ride along as scalar subqueries on the
    # personal-catalog aggregate rather than costing their own round trip. Each is
    # filtered by user_id, which leads the (user_id, imdb_id) index on all three
    # tables, and none of them correlates with this query's FROM.
    (
        personal_catalog_at,
        personal_content_at,
        progress_at,
        event_at,
        library_at,
    ) = (
        db.query(
            func.max(UserCatalog.last_generated),
            func.max(UserCatalogContent.added_at),
            db.query(func.max(WatchProgress.updated_at))
            .filter(WatchProgress.user_id == user.id)
            .scalar_subquery(),
            db.query(func.max(WatchEvent.created_at))
            .filter(WatchEvent.user_id == user.id)
            .scalar_subquery(),
            db.query(func.max(LibraryItem.added_at))
            .filter(LibraryItem.user_id == user.id)
            .scalar_subquery(),
        )
        .select_from(UserCatalog)
        .outerjoin(
            UserCatalogContent,
            UserCatalogContent.catalog_id == UserCatalog.id,
        )
        .filter(
            UserCatalog.user_id == user.id,
            UserCatalog.is_active.is_(True),
        )
        .first()
    ) or (None, None, None, None, None)

    rotation_days = max(0, settings.synth_rotation_days)
    rotation_bucket = (
        utc_now().date().toordinal() // rotation_days if rotation_days else 0
    )
    state = "|".join(
        [
            str(user.id),
            user.last_sync.isoformat() if user.last_sync else "pending",
            universal_category_at.isoformat() if universal_category_at else "",
            universal_content_at.isoformat() if universal_content_at else "",
            personal_catalog_at.isoformat() if personal_catalog_at else "",
            personal_content_at.isoformat() if personal_content_at else "",
            progress_at.isoformat() if progress_at else "",
            event_at.isoformat() if event_at else "",
            library_at.isoformat() if library_at else "",
            str(rotation_bucket),
            str(settings.enable_personalized_catalogs),
            str(settings.enable_universal_catalogs),
            str(settings.hide_foreign),
            str(settings.hide_adult),
            str(settings.hide_unreleased),
            availability_revision(db),
        ]
    )
    return hashlib.sha256(state.encode()).hexdigest()[:24]


def _library_dashboard_row(row: dict, limit: int) -> dict:
    """A library row for the web dashboard; it may mix movies and shows, so
    each card is serialized with its own media type."""
    items = []
    for item in row["items"][:limit]:
        card = _catalog_row_from_items(
            "",
            row["id"],
            _api_media_type(item.get("media_type") or "movie"),
            [item],
            limit=1,
            shuffle=False,
        )["items"]
        if card:
            if item.get("reason"):
                card[0]["reason"] = item["reason"]
            items.append(card[0])
    shelf = _catalog_row_from_items(
        row["name"],
        row["id"],
        _api_media_type(row["media_type"]) if row["media_type"] != "mixed" else "movie",
        [],
        limit=limit,
        source="library",
        shuffle=False,
    )
    shelf["items"] = items
    shelf["item_count"] = len(items)
    if row["media_type"] == "mixed":
        shelf["type"] = shelf["media_type"] = shelf["catalog_type"] = "mixed"
    return shelf


def _dashboard_payload(
    user: User,
    db: Session,
    *,
    catalog_revision: str | None = None,
    row_item_limit: int = WEB_DASHBOARD_ROW_ITEM_LIMIT,
) -> dict:
    """Assemble a dashboard with shelf queries batched by content source.

    ``catalog_revision`` lets a caller that has already resolved it -- the native
    bootstrap does, to answer conditional requests -- pass it in rather than pay
    for the aggregate queries a second time.
    """
    if catalog_revision is None:
        catalog_revision = _catalog_state_revision(user, db)
    cache_key = (
        user.id,
        user.last_sync.isoformat() if user.last_sync else "",
        catalog_revision,
        settings.enable_personalized_catalogs,
        settings.enable_universal_catalogs,
        settings.hide_foreign,
        settings.hide_adult,
        settings.hide_unreleased,
        row_item_limit,
        getattr(user, "content_scope", None),
    )
    now = time()
    cached = _dashboard_cache.get(cache_key)
    if cached and now - cached[0] < _DASHBOARD_CACHE_TTL:
        return cached[1]

    is_local = getattr(user, "auth_source", "local") == "local"
    catalog_sync_pending = is_local and user.last_sync is None
    if catalog_sync_pending:
        _schedule_local_sync(user.id)

    continue_watching, recent_history, library = _dashboard_activity_items(db, user)

    catalogs: list[dict] = []
    movie_catalogs: list[dict] = []
    series_catalogs: list[dict] = []
    from workers.personalized_sync import get_slot_sort_order, VALID_SLOTS

    user_catalogs = (
        db.query(UserCatalog)
        .filter(
            UserCatalog.user_id == user.id,
            UserCatalog.is_active.is_(True),
            UserCatalog.slot_id.in_(VALID_SLOTS),
        )
        .all()
        if settings.enable_personalized_catalogs
        else []
    )
    user_catalogs.sort(key=lambda c: get_slot_sort_order(c.slot_id))
    # Taste data is the same for every row in this request, so compute it once
    # here instead of paying the WatchEvent count/max probe query inside each
    # call. The TMDB-sourced personal rows need it too now, not just the
    # universal ones.
    taste_tags, exclude_ids = _get_user_taste_data(user.id, db)
    generator = CatalogGenerator(db)
    personal_content = generator.get_catalog_contents_batch(
        [catalog.slot_id for catalog in user_catalogs],
        user_id=user.id,
        hide_foreign=settings.hide_foreign,
        hide_adult=settings.hide_adult,
        hide_unreleased=False,
        user_taste_tag_ids=taste_tags,
        exclude_ids=exclude_ids,
    )
    for catalog in user_catalogs:
        row = _catalog_row_from_items(
            catalog.name,
            f"personal-{catalog.slot_id}",
            _api_media_type(catalog.media_type),
            personal_content.get(catalog.slot_id, []),
            limit=WEB_DASHBOARD_CANDIDATE_LIMIT,
            source="personal",
            generation_method=catalog.generation_method,
            generation_params=catalog.generation_params,
            revision=(
                (catalog.generation_params or {}).get("generation_revision")
                if isinstance(catalog.generation_params, dict)
                else catalog.last_generated.isoformat()
            )
            or catalog.last_generated.isoformat(),
        )
        if row["items"]:
            catalogs.append(row)
            (series_catalogs if catalog.media_type == "tv" else movie_catalogs).append(
                row
            )

    # Universal rows are appended for every profile, not only for a profile
    # whose personal sync came up short.
    #
    # This used to be gated on `len(movie_catalogs) < 2 or len(series_catalogs)
    # < 2`. A successfully synced profile has three personal rows per type
    # before its taste rows are even built, so the condition was false for
    # everyone it mattered for -- and the entire 491-tag taxonomy, every
    # curated row and every synthesized one, rendered on the web player for
    # nobody.
    #
    # Native/web Home draws from a larger rotating window. The final page
    # assembler still enforces 40 rows and all quality/duplicate
    # rules, while the extra universal candidates allow personal rows to lead
    # without reducing the synthesized selection to nine shelves.
    universal_rows = (
        _universal_row_categories(db, limit=MAX_NATIVE_DISCOVERY_ROWS)
        if settings.enable_universal_catalogs
        else []
    )
    _reorder_universals_by_taste(universal_rows, user.id, db)
    universal_content = generator.get_catalog_contents_batch(
        [category.id for category in universal_rows],
        hide_foreign=settings.hide_foreign,
        hide_adult=settings.hide_adult,
        hide_unreleased=settings.hide_unreleased,
        user_taste_tag_ids=taste_tags,
        exclude_ids=exclude_ids,
    )
    for category in universal_rows:
        row = _catalog_row_from_items(
            category.name,
            f"universal-{category.id}",
            _api_media_type(category.media_type),
            universal_content.get(category.id, []),
            limit=WEB_DASHBOARD_CANDIDATE_LIMIT,
            source="synthesized",
            generation_method="synthesized",
            generation_params=category.tag_formula,
            revision=category.updated_at.isoformat(),
        )
        if row["items"]:
            catalogs.append(row)
            (series_catalogs if category.media_type == "tv" else movie_catalogs).append(
                row
            )

    # Page assembly. Runs after every row exists and before the hero is chosen,
    # so the hero is picked from what the viewer will actually see.
    from app import library_catalog

    # "Not for me" titles are already gone: they are in the taste exclusions.
    scope = library_catalog.normalize_scope(getattr(user, "content_scope", None))
    owned = library_catalog.owned_entries(db)
    if scope == "my_servers":
        # Filter in place so the movie/series splits see the same rows.
        for row in catalogs:
            row["items"] = [
                item
                for item in row.get("items") or []
                if (
                    "tv" if item.get("media_type") in ("tv", "series") else "movie",
                    item.get("tmdb_id"),
                )
                in owned
            ]
    catalogs = _assemble_page(
        catalogs,
        item_limit=row_item_limit,
        min_items=(
            library_catalog.MY_SERVERS_MIN_ROW_ITEMS if scope == "my_servers" else None
        ),
    )
    kept = {id(row) for row in catalogs}
    movie_catalogs = [row for row in movie_catalogs if id(row) in kept]
    series_catalogs = [row for row in series_catalogs if id(row) in kept]
    # Library rows lead the page and sit outside its de-duplication.
    library_rows = [
        _library_dashboard_row(row, row_item_limit)
        for row in library_catalog.compose_rows(db, user, scope, owned)
    ]
    catalogs = library_rows + catalogs

    hero = _dashboard_hero_for_user(catalogs) or next(
        (
            item
            for group in (recent_history, library)
            for item in group
            if item.get("backdrop_path") or item.get("backdrop")
        ),
        None,
    )
    if not hero:
        hero = next((i for c in catalogs for i in c.get("items", []) if i), None)

    payload = {
        "hero": hero,
        "continue_watching": continue_watching,
        "recent_history": recent_history,
        "library": library,
        "catalogs": catalogs,
        "movie_catalogs": movie_catalogs,
        "series_catalogs": series_catalogs,
        "catalog_sync_pending": catalog_sync_pending,
        "catalog_revision": catalog_revision,
    }
    _dashboard_cache[cache_key] = (now, payload)
    _ttl_cache_evict(_dashboard_cache, _DASHBOARD_CACHE_MAX)
    return payload


def _account_payload(account: Account, profiles: list[User], active: User) -> dict:
    return {
        "id": account.id,
        "email": account.email,
        "display_name": active.display_name or account.email,
        "totp_enabled": account.totp_enabled,
        "active_profile_id": active.id,
        "profiles": [_profile_summary(profile) for profile in profiles],
    }


#: Row-item keys an Apple client cannot read, dropped when `fields=card`.
#:
#: The Apple client decodes `dashboard.rows[].items` with its card model
#: (`NativeCatalogRow.items`, and nothing else consumes them) with
#: `.convertFromSnakeCase` and no `CodingKeys`. These 15 have no matching
#: property after that conversion, and the 3 in the second group decode to the
#: same property as a camelCase twin that is also on the wire.
#:
#: All 18 are still live for the browser dashboard, which reads the snake_case
#: spellings (`src/api/mediaUtils.ts` takes `poster_path ?? poster`), so this is
#: opt-in per request rather than a change to the shape everyone gets.
#:
#: This substantially reduces keyed decodes on a full bootstrap page. The decode
#: count is the point; the bytes are comparatively cheap on a home network.
_CARD_PROJECTION_DROP = frozenset(
    {
        # No matching property on the Apple client's card model.
        "title",
        "media_type",
        "overview",
        "year",
        "rating",
        "subtitle",
        "video_id",
        "content_type",
        "rating_source",
        "digital_release_date",
        "next_episode_air_date",
        "poster_path",
        "backdrop_path",
        "logo_path",
        "backdrop",
        # Decode to the same property as a camelCase twin already on the wire.
        "content_rating",
        "imdb_id",
        "tmdb_id",
    }
)


def _card_projected_rows(rows: list[dict]) -> list[dict]:
    """Copy `rows` with each item reduced to the keys an Apple client decodes.

    Copies rather than mutates: these row dicts belong to the `_dashboard_cache`
    entry and are shared with the browser dashboard, which needs the full shape.
    """
    return [
        {
            **row,
            "items": [
                {
                    key: value
                    for key, value in item.items()
                    if key not in _CARD_PROJECTION_DROP
                }
                for item in row.get("items") or []
            ],
        }
        for row in rows
    ]


def _bootstrap_dashboard_page(
    legacy: dict, cursor: int, limit: int, *, card_fields: bool = False
) -> dict:
    """Project the assembled native dashboard into a stable bounded row page."""
    rows = legacy["catalogs"]
    start = min(max(0, cursor), len(rows))
    page_limit = min(NATIVE_BOOTSTRAP_PAGE_SIZE, max(1, limit))
    end = min(start + page_limit, len(rows))
    page_rows = rows[start:end]
    if card_fields:
        page_rows = _card_projected_rows(page_rows)
    return {
        "hero": legacy["hero"],
        # The activity collections keep the full shape whatever `fields` says:
        # their items carry real season/episode/position/duration values, which
        # is exactly what the projection drops.
        "continue_watching": legacy["continue_watching"],
        "recent_history": legacy["recent_history"],
        "library": legacy["library"],
        "rows": page_rows,
        "catalog_sync_pending": legacy["catalog_sync_pending"],
        "catalog_revision": legacy["catalog_revision"],
        "row_page": {
            "cursor": start,
            "limit": page_limit,
            "total": len(rows),
            "next_cursor": end if end < len(rows) else None,
        },
    }


def _bootstrap_context(
    request: Request, db: Session
) -> tuple[Account, list[User], User]:
    """Resolve session, account, profiles, and active profile without duplicate reads."""
    token = request.cookies.get("user_session")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    session = get_user_session(token, db)
    if not session:
        raise HTTPException(status_code=401, detail="Session expired")
    account_id = session.account_id
    if account_id is None and session.user_id is not None:
        legacy_profile = db.get(User, session.user_id)
        account_id = legacy_profile.account_id if legacy_profile else None
    account = db.get(Account, account_id) if account_id is not None else None
    if not account:
        raise HTTPException(status_code=401, detail="Session expired")
    if not account.is_active:
        raise HTTPException(status_code=403, detail="Account is deactivated")
    requirement = auth_requirement(db, account)
    if requirement != "none":
        raise HTTPException(
            status_code=403,
            detail=(
                "Set up a passkey to continue"
                if requirement == "passkey_required"
                else "Set up two-factor authentication to continue"
            ),
            headers={"X-Auth-Requirement": requirement},
        )
    profiles = _account_profiles(db, account)
    if not profiles:
        profiles = [_create_profile(db, account)]
    active = next(
        (profile for profile in profiles if profile.id == session.active_profile_id),
        profiles[0],
    )
    if session.active_profile_id != active.id:
        session.active_profile_id = active.id
        db.commit()
    if not active.is_active:
        raise HTTPException(status_code=403, detail="Profile is deactivated")
    return account, profiles, active


@app.get("/account/api/dashboard")
def account_api_dashboard(request: Request, db: Session = Depends(get_db_dependency)):
    """Return the legacy dashboard shape for existing clients."""
    return annotate_tree(db, _dashboard_payload(_get_active_profile(request, db), db))


@app.get("/account/api/bootstrap")
def account_api_bootstrap(request: Request, db: Session = Depends(get_db_dependency)):
    """Return account context and a normalized dashboard in one request."""
    account, profiles, active = _bootstrap_context(request, db)
    legacy = _dashboard_payload(active, db)
    dashboard = {
        "hero": legacy["hero"],
        "continue_watching": legacy["continue_watching"],
        "recent_history": legacy["recent_history"],
        "library": legacy["library"],
        "rows": legacy["catalogs"],
        "catalog_sync_pending": legacy["catalog_sync_pending"],
    }
    return {
        "account": _account_payload(account, profiles, active),
        "dashboard": annotate_tree(db, dashboard),
        "playback_providers": [
            status.model_dump() for status in source_registry.provider_statuses()
        ],
    }


@app.get("/api/v2/catalog-revision")
def native_api_catalog_revision(
    user_key: BearerUserKey, db: Session = Depends(get_db_dependency)
):
    """Return the small profile-scoped revision used for foreground revalidation."""
    _account, active = _account_for_user_key(user_key, db)
    revision = _catalog_state_revision(active, db)
    response = JSONResponse(
        content={
            "catalog_revision": revision,
            "max_rows": MAX_NATIVE_DISCOVERY_ROWS,
            "page_size": NATIVE_BOOTSTRAP_PAGE_SIZE,
        }
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["ETag"] = f'"{revision}"'
    return response


def _if_none_match_hit(request: Request, etag: str) -> bool:
    """Does the request's ``If-None-Match`` list contain this entity tag?

    Handles the list form (``"a", "b"``) and the weak prefix, both of which a
    caching client or intermediary is allowed to send.
    """
    header = request.headers.get("if-none-match")
    if not header:
        return False
    if header.strip() == "*":
        return True
    bare = etag.lstrip("W/")
    return any(
        candidate.strip().lstrip("W/") == bare for candidate in header.split(",")
    )


@app.get("/api/v2/bootstrap")
def native_api_bootstrap(
    request: Request,
    user_key: BearerUserKey,
    cursor: int = Query(0, ge=0, le=MAX_NATIVE_DISCOVERY_ROWS),
    limit: int = Query(
        NATIVE_BOOTSTRAP_PAGE_SIZE,
        ge=1,
        le=NATIVE_BOOTSTRAP_PAGE_SIZE,
    ),
    fields: Literal["card"] | None = Query(
        None,
        description=(
            "`card` reduces dashboard.rows[].items to the keys an Apple client "
            "decodes. Omit for the full shape."
        ),
    ),
    db: Session = Depends(get_db_dependency),
):
    """Native home-screen bootstrap authenticated by a profile user_key."""
    account, active = _account_for_user_key(user_key, db)

    # Resolve the revision before assembling anything. `Cache-Control: no-cache`
    # asks the client to revalidate rather than to skip caching, so an unchanged
    # home can be answered with an empty 304 -- which skips the entire shelf
    # build, not just the response body. The ETag only became safe to honour once
    # the revision started covering the activity collections this document also
    # carries; see `_catalog_state_revision`.
    revision = _catalog_state_revision(active, db)
    # `fields` is part of the tag: the two shapes are different entities, so a
    # client that changes mode must not be answered 304 from the other one.
    etag = f'"{revision}:{cursor}:{limit}:{fields or "full"}"'
    if _if_none_match_hit(request, etag):
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": "private, no-cache"},
        )

    profiles = _account_profiles(db, account)
    legacy = _dashboard_payload(
        active,
        db,
        catalog_revision=revision,
        row_item_limit=NATIVE_DASHBOARD_ROW_ITEM_LIMIT,
    )
    response = JSONResponse(
        content={
            "account": _account_payload(account, profiles, active),
            "dashboard": annotate_tree(
                db,
                _bootstrap_dashboard_page(
                    legacy, cursor, limit, card_fields=fields == "card"
                ),
            ),
            "playback_providers": [
                status.model_dump() for status in source_registry.provider_statuses()
            ],
        }
    )
    # Bootstrap is profile-scoped and revision-aware. Keep it out of shared or
    # persistent HTTP caches; HomeFeedCacheStore remains the explicit offline
    # fallback on Apple devices.
    response.headers["Cache-Control"] = "private, no-cache"
    response.headers["ETag"] = etag
    return response


#: A title query that matches fewer titles than this, and exactly matches a tag
#: name, is read as a vibe rather than a title. Five is deliberately low: the
#: point is to catch "cyberpunk" (no film is called that) without hijacking
#: "Heist", which is both a tag and several actual movies.
_TAG_SEARCH_TITLE_CEILING = 5


def _search_with_tags(db: Session, q: str, tag_ids: list) -> dict:
    """Title search intersected with a tag set, in one query."""
    title_query, _ = _extract_query_year(q)
    term = f"%{title_query.strip()}%"
    matching = (
        db.query(MovieTag.tmdb_id, MovieTag.media_type)
        .filter(MovieTag.tag_id.in_(tag_ids))
        .group_by(MovieTag.tmdb_id, MovieTag.media_type)
        .having(func.count(func.distinct(MovieTag.tag_id)) >= len(set(tag_ids)))
        .subquery()
    )
    rows_query = (
        db.query(MediaMetadata)
        .join(
            matching,
            (MediaMetadata.tmdb_id == matching.c.tmdb_id)
            & (MediaMetadata.media_type == matching.c.media_type),
        )
        .filter(MediaMetadata.title.ilike(term))
    )
    rows = (
        rows_query.order_by(
            quality_expr().desc(), MediaMetadata.popularity.desc().nullslast()
        )
        .limit(40)
        .all()
    )
    return {
        "items": _drop_explicit_results(
            [
                _serialize_media_item(
                    tmdb_id=r.tmdb_id, media_type=r.media_type, title=r.title, meta=r
                )
                for r in rows
            ]
        ),
        "result_type": "filtered",
    }


def _reads_as_vibe(db: Session, q: str) -> bool:
    """Whether a query naming a narrow tag means the tag rather than a title.

    Broad genres ("horror", "action movies") are always read as genres; see
    :func:`app.genre_search.parse_genre_query`. A narrower tag name only counts
    when no title is called exactly that and barely any contain it --
    searching "cyberpunk" should work like Netflix, searching "Heist" should
    still find the films called Heist.
    """
    term = q.strip()
    if len(term) < 3:
        return False

    # Title intent wins over a coincidentally named tag. The previous count-only
    # heuristic redirected a real but uncommon exact title into a vibe shelf.
    exact_title = (
        db.query(MediaMetadata.tmdb_id)
        .filter(func.lower(MediaMetadata.title) == term.lower())
        .first()
    )
    if exact_title is not None:
        return False

    title_matches = (
        db.query(func.count(MediaMetadata.tmdb_id))
        .filter(MediaMetadata.title.ilike(f"%{term}%"))
        .scalar()
        or 0
    )
    return title_matches < _TAG_SEARCH_TITLE_CEILING


def _ranked_items(db: Session, ranked: list) -> list:
    """Serialize ``RankedTitle``s of mixed media types, keeping their order."""
    by_type: dict[str, list[int]] = {}
    for r in ranked:
        by_type.setdefault(r.media_type, []).append(r.tmdb_id)
    serialized = {
        (item["media_type"], item["tmdb_id"]): item
        for media_type, ids in by_type.items()
        for item in _items_from_ids(db, ids, media_type)
    }
    items = []
    for r in ranked:
        item = serialized.get((r.media_type, r.tmdb_id))
        if item is None:
            continue
        if r.reasons:
            item = {**item, "reason": " · ".join(r.reasons)}
        items.append(item)
    return _drop_explicit_results(items)


def _genre_search(db: Session, genre, user: User | None, q: str) -> dict | None:
    """A genre's titles ranked by the profile's taste, grouped into sections.

    ``items`` stays a flat, taste-ordered list so clients that predate
    ``sections`` improve without a change. A title literally named after the
    query ("Horror", "Drama") leads ``items``: genre intent no longer hides it.
    """
    from app.genre_search import favourite_subgenres, taste_ranked

    user_id = int(user.id) if user is not None else None
    ranked = taste_ranked(db, genre, user_id)
    if not ranked:
        return None

    label = genre.label
    has_taste = any(r.reasons for r in ranked)
    unseen = [r for r in ranked if not r.watched]
    sections = [
        {
            "id": "top",
            "title": f"Top {label} picks for you" if has_taste else f"Top {label}",
            "items": _ranked_items(db, unseen[:20]),
        }
    ]
    for sub in favourite_subgenres(db, genre, user_id):
        rows = [r for r in unseen if int(sub.id) in r.tag_ids][:20]
        if len(rows) >= 3:
            sections.append(
                {
                    "id": f"because-{sub.id}",
                    "title": f"Because you like {sub.name}",
                    "items": _ranked_items(db, rows),
                }
            )
    acclaimed = sorted(unseen, key=lambda r: (-r.quality, r.tmdb_id))[:20]
    sections.append(
        {
            "id": "acclaimed",
            "title": f"Highly rated {label}",
            "items": _ranked_items(db, acclaimed),
        }
    )
    if len(genre.media_types) > 1:
        for media_type, noun in (("movie", "movies"), ("tv", "series")):
            rows = [r for r in unseen if r.media_type == media_type][:20]
            if rows:
                sections.append(
                    {
                        "id": media_type,
                        "title": f"{label} {noun}",
                        "items": _ranked_items(db, rows),
                    }
                )
    sections = [s for s in sections if s["items"]]

    exact = (
        db.query(MediaMetadata)
        .options(load_only(*_MEDIA_CARD_COLUMNS))
        .filter(func.lower(MediaMetadata.title) == q.strip().lower())
        .order_by(MediaMetadata.vote_count.desc().nullslast())
        .limit(3)
        .all()
    )
    exact_items = _drop_explicit_results(
        [
            _serialize_media_item(
                tmdb_id=r.tmdb_id, media_type=r.media_type, title=r.title, meta=r
            )
            for r in exact
        ]
    )
    exact_keys = {(i["media_type"], i["tmdb_id"]) for i in exact_items}
    items = exact_items + [
        i
        for i in _ranked_items(db, ranked[:40])
        if (i["media_type"], i["tmdb_id"]) not in exact_keys
    ]
    first = genre.tags[0]
    return {
        "items": items[:40],
        "result_type": "tag",
        "tag": {"id": int(first.id), "name": first.name, "facet": first.category},
        "genre": {
            "tag_ids": [int(t.id) for t in genre.tags],
            "name": label,
            "media_type": genre.media_types[0] if len(genre.media_types) == 1 else None,
        },
        "sections": sections,
    }


@app.get("/account/api/search")
def account_api_search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=80),
    tags: str = Query("", max_length=400),
    db: Session = Depends(get_db_dependency),
):
    """Search cached TMDB metadata for the authenticated web dashboard.

    Results come from our own database and are ranked server-side.

    ``tags`` intersects the results with a tag set **in SQL**, so a filter is
    applied to the whole library rather than to the 40 rows a page happened to
    return.
    """
    user = _get_active_profile(request, db)
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is deactivated")

    return _scoped_search(user, annotate_tree(db, _discovery_search(db, q, tags, user)))


def _scoped_search(user: User, payload: dict) -> dict:
    """In "My Servers" scope, search shows only titles on the media servers.

    Uses ``in_library`` (set by ``annotate_tree``), never ``playable``: a
    searchable addon makes every title playable but none of them owned.
    """
    if getattr(user, "content_scope", None) != "my_servers":
        return payload

    def owned(items):
        return [
            item
            for item in items or []
            if (item.get("availability") or {}).get("in_library")
        ]

    payload["items"] = owned(payload.get("items"))
    if payload.get("sections"):
        payload["sections"] = [
            {**section, "items": owned(section.get("items"))}
            for section in payload["sections"]
            if owned(section.get("items"))
        ]
    return payload


# --- Library tab: what the connected Plex/Jellyfin servers own -------------------
_OWNED_SORTS = ("added", "title", "year", "popular")
_OWNED_FILTERS = ("unwatched", "4k")


def _owned_library_page(
    db: Session,
    user: User,
    *,
    kind: str | None,
    sort: str,
    filter_: str | None,
    offset: int,
    limit: int,
) -> dict:
    from app import library_catalog

    owned = library_catalog.owned_entries(db)
    if kind == "other":
        others, other_total = library_catalog.other_items(
            db,
            sort=sort if sort in ("added", "title", "year") else "added",  # type: ignore[arg-type]
            offset=offset,
            limit=limit,
        )
        return _owned_library_response(db, others, other_total, offset, owned)
    media_kind = {"movie": "movie", "tv": "tv", "series": "tv"}.get(kind or "")
    items, total = library_catalog.owned_items(
        db,
        kind=media_kind,  # type: ignore[arg-type]
        sort=sort if sort in _OWNED_SORTS else "added",  # type: ignore[arg-type]
        offset=offset,
        limit=limit,
        unwatched_for=user.id if filter_ == "unwatched" else None,
        min_resolution="2160p" if filter_ == "4k" else None,
        owned=owned,
    )
    row = _library_dashboard_row(
        {"id": "library", "name": "", "media_type": "mixed", "items": items}, limit
    )
    for card, item in zip(row["items"], items):
        if item.get("server_resolution"):
            card["server_resolution"] = item["server_resolution"]
    return _owned_library_response(db, row["items"], total, offset, owned)


def _owned_library_response(
    db: Session, items: list[dict], total: int, offset: int, owned: dict
) -> dict:
    from app import library_catalog

    counts = {"movie": 0, "tv": 0}
    for key in owned:
        counts[key[0]] += 1
    next_offset = offset + len(items)
    return annotate_tree(
        db,
        {
            "items": items,
            "total": total,
            "offset": offset,
            "next_offset": next_offset if next_offset < total else None,
            "counts": {
                "movies": counts["movie"],
                "shows": counts["tv"],
                "other": library_catalog.other_items(db, limit=0)[1],
            },
        },
    )


@app.get("/account/api/servers/library")
def account_servers_library(
    request: Request,
    type: str | None = Query(None, pattern="^(movie|tv|series|other)$"),
    sort: str = Query("added", pattern="^(added|title|year|popular)$"),
    filter: str | None = Query(None, pattern="^(unwatched|4k)$"),
    offset: int = Query(0, ge=0, le=100_000),
    limit: int = Query(60, ge=1, le=120),
    db: Session = Depends(get_db_dependency),
):
    """Owned titles for the web Library tab (sorted, filtered, paged)."""
    user = _get_active_profile(request, db)
    return _owned_library_page(
        db, user, kind=type, sort=sort, filter_=filter, offset=offset, limit=limit
    )


@app.get("/api/v2/servers/library")
def native_servers_library(
    user_key: BearerUserKey,
    type: str | None = Query(None, pattern="^(movie|tv|series|other)$"),
    sort: str = Query("added", pattern="^(added|title|year|popular)$"),
    filter: str | None = Query(None, pattern="^(unwatched|4k)$"),
    offset: int = Query(0, ge=0, le=100_000),
    limit: int = Query(60, ge=1, le=120),
    db: Session = Depends(get_db_dependency),
):
    """Owned titles for the native Library tab (sorted, filtered, paged)."""
    _, user = _account_for_user_key(user_key, db)
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Profile is deactivated")
    return _owned_library_page(
        db, user, kind=type, sort=sort, filter_=filter, offset=offset, limit=limit
    )


@app.get("/api/v2/search")
def native_api_search(
    user_key: BearerUserKey,
    q: str = Query(..., min_length=1, max_length=80),
    tags: str = Query("", max_length=400),
    db: Session = Depends(get_db_dependency),
):
    """Native, profile-key-authenticated title and tag-aware search."""
    _, user = _account_for_user_key(user_key, db)
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Profile is deactivated")
    return _scoped_search(user, annotate_tree(db, _discovery_search(db, q, tags, user)))


def _discovery_search(
    db: Session, q: str, tags: str = "", user: User | None = None
) -> dict:
    """Shared search implementation for browser sessions and native profiles."""

    tag_ids = [p.strip() for p in tags.split(",") if p.strip().isdigit()]
    if tag_ids:
        return _search_with_tags(db, q, [int(t) for t in tag_ids])

    from app.genre_search import parse_genre_query

    genre = parse_genre_query(db, q)
    if genre is not None and (genre.explicit or _reads_as_vibe(db, q)):
        genre_hit = _genre_search(db, genre, user, q)
        if genre_hit is not None:
            return genre_hit

    our_items = _search_media_multi(db, ["movie", "tv"], q, 80)
    safe_items = _drop_explicit_results(our_items)
    ranked = _rank_search_results(safe_items, q)[:40]
    logger.info(
        f"search assembled local={len(our_items)} "
        f"safe={len(safe_items)} returned={len(ranked)}"
    )
    return {"items": [_serialize_search_result(it) for it in ranked]}


def _tmdb_detail_item(detail: dict, media_type: str) -> dict:
    """Serialize a TMDB detail payload for the browser dashboard."""
    release_date = detail.get("release_date") or detail.get("first_air_date") or ""
    logo_path = _logo_path_from_detail(detail)
    # Digital home-release date (movies only); None when TMDB lists none. The web detail
    # page renders it as an upcoming-aware "Digital" chip.
    digital_release = None
    if media_type == "movie":
        from app.tmdb_client import tmdb_client

        digital_release = tmdb_client.digital_release_date(
            detail.get("release_dates") or {}
        )
    return {
        "tmdb_id": detail.get("id"),
        "media_type": media_type,
        "title": detail.get("title") or detail.get("name") or "Untitled",
        "year": release_date[:4] if release_date else "",
        "digitalReleaseInfo": digital_release,
        "overview": detail.get("overview") or "",
        "genres": [g["name"] for g in detail.get("genres", []) if g.get("name")],
        "poster_path": detail.get("poster_path"),
        "backdrop_path": detail.get("backdrop_path"),
        "poster": _image_url(detail.get("poster_path"), "w500"),
        "backdrop": _image_url(detail.get("backdrop_path"), "w780"),
        "logo_path": logo_path,
        "logo": _image_url(logo_path, "original"),
        "rating": detail.get("vote_average"),
        "runtime": detail.get("runtime")
        or (detail.get("episode_run_time") or [None])[0],
        "imdb_id": (detail.get("external_ids") or {}).get("imdb_id"),
        "video_id": (detail.get("external_ids") or {}).get("imdb_id")
        or f"tmdb:{detail.get('id')}",
        "content_type": _api_media_type(media_type),
    }


def _tmdb_result_item(result: dict, media_type: str) -> dict:
    """Serialize a TMDB list result for dashboard rails."""
    release_date = result.get("release_date") or result.get("first_air_date") or ""
    item = {
        "tmdb_id": result.get("id"),
        "media_type": media_type,
        "title": result.get("title") or result.get("name") or "Untitled",
        "year": release_date[:4] if release_date else "",
        "overview": result.get("overview") or "",
        "poster_path": result.get("poster_path"),
        "backdrop_path": result.get("backdrop_path"),
        "poster": _image_url(result.get("poster_path"), "w500"),
        "backdrop": _image_url(result.get("backdrop_path"), "w780"),
        "logo_path": result.get("logo_path"),
        "logo": _image_url(result.get("logo_path"), "original"),
        "rating": result.get("vote_average"),
    }
    item["video_id"] = _playback_id_for_item(item)
    item["content_type"] = _api_media_type(media_type)
    return item


def _title_tags(db: Session, tmdb_id: int, media_type: str, limit: int = 12) -> list:
    """The display tags for one title, heaviest first.

    Ordered by ``weight`` rather than ``confidence``: confidence is what the
    model asserted, weight is that scaled by the tag's rarity, so ordering by
    it puts *Neo-Noir* above *Drama* instead of whichever the model happened to
    feel strongest about. NULL weights sort last -- a title tagged before the
    relevance layer ran still shows its chips.
    """
    rows = (
        db.query(Tag.id, Tag.name, Tag.category, MovieTag.confidence)
        .join(MovieTag, MovieTag.tag_id == Tag.id)
        .filter(
            MovieTag.tmdb_id == tmdb_id,
            MovieTag.media_type == media_type,
            Tag.display.is_(True),
            Tag.is_active.is_(True),
        )
        .order_by(MovieTag.weight.desc().nullslast(), Tag.sort_hint, Tag.name)
        .limit(limit)
        .all()
    )
    return [
        {
            "id": int(r[0]),
            "name": r[1],
            "facet": r[2],
            "confidence": round(float(r[3]), 3) if r[3] is not None else None,
        }
        for r in rows
    ]


#: Display names for the rating sources MDBList aggregates. Anything not listed
#: is title-cased from its own key, so a new source appears rather than vanishes.
_RATING_SOURCE_LABELS = {
    "imdb": "IMDb",
    "tmdb": "TMDB",
    "trakt": "Trakt",
    "tomatoes": "Rotten Tomatoes",
    "tomatoesaudience": "RT Audience",
    "metacritic": "Metacritic",
    "metacriticuser": "Metacritic Users",
    "letterboxd": "Letterboxd",
    "rogerebert": "Roger Ebert",
    "myanimelist": "MyAnimeList",
    "popcorn": "Popcorn",
}

# Product order, not score order. Sorting by the score itself made the row jump
# around from title to title and whenever an upstream value changed. Keep closely
# related critics/audience pairs adjacent, then put any newly introduced source at
# the end in a deterministic alphabetical order.
_RATING_SOURCE_ORDER = {
    "tomatoes": 0,
    "rottentomatoes": 0,
    "tomatometer": 0,
    "tomatoesaudience": 1,
    "popcorn": 1,
    "popcornmeter": 1,
    "rtaudience": 1,
    "metacritic": 10,
    "metacriticuser": 11,
    "metacriticusers": 11,
    "rogerebert": 20,
    "imdb": 30,
    "tmdb": 40,
    "themoviedb": 40,
    "trakt": 50,
    "letterboxd": 60,
    "myanimelist": 70,
}


def _rating_source_key(source: str) -> tuple[int, str]:
    normalized = "".join(ch for ch in source.casefold() if ch.isalnum())
    return (_RATING_SOURCE_ORDER.get(normalized, 1_000), normalized)


def _title_ratings(db: Session, tmdb_id: int, media_type: str) -> list:
    """Multi-source ratings for one title in a fixed product order.

    ``normalized`` is the 0-10 comparable value and ``percentage`` is its 0-100
    presentation. ``value`` remains on the source's own scale for compatibility
    with older clients, but current clients can render every source consistently.
    Empty when MDBList never covered the title, and the caller hides the row
    entirely rather than showing a lone TMDB score twice.
    """
    rows = (
        db.query(TitleRating)
        .filter(
            TitleRating.tmdb_id == tmdb_id,
            TitleRating.media_type == media_type,
            TitleRating.normalized.isnot(None),
        )
        .all()
    )
    rows.sort(key=lambda row: _rating_source_key(row.source))
    return [
        {
            "source": r.source,
            "label": _RATING_SOURCE_LABELS.get(r.source, r.source.title()),
            "value": r.value,
            "normalized": round(float(r.normalized), 2),
            "percentage": int(round(float(r.normalized) * 10)),
            "votes": r.votes,
        }
        for r in rows
    ]


_DETAIL_REVIEW_LIMIT = 10
_DETAIL_REVIEW_CHARS = 2_000


def _clean_review_text(value: object) -> str:
    """Bound user-supplied review prose so one detail response stays compact."""
    return " ".join(str(value or "").split())[:_DETAIL_REVIEW_CHARS]


def _title_reviews(meta: MediaMetadata | None, detail: dict) -> tuple[list, dict]:
    """Return attributed TMDB reviews plus stored MDBList excerpts.

    MDBList enrichment deliberately stores only two short excerpts, without
    authorship metadata. They are labelled as excerpts rather than pretending
    MDBList authored them. TMDB reviews retain their attribution and canonical
    URL. Duplicate prose is removed across the two providers.
    """
    review_block = detail.get("reviews") or {}
    tmdb_rows = review_block.get("results") if isinstance(review_block, dict) else []
    reviews: list[dict] = []
    seen: set[str] = set()
    rated: list[float] = []

    for raw in tmdb_rows or []:
        if not isinstance(raw, dict):
            continue
        content = _clean_review_text(raw.get("content"))
        fingerprint = content.casefold()[:240]
        if not content or fingerprint in seen:
            continue
        seen.add(fingerprint)
        raw_author_details = raw.get("author_details") or {}
        author_details = (
            raw_author_details if isinstance(raw_author_details, dict) else {}
        )
        avatar_path = str(author_details.get("avatar_path") or "")
        avatar = (
            avatar_path[1:]
            if avatar_path.startswith("/http")
            else _image_url(avatar_path, "w185")
        )
        rating = author_details.get("rating")
        try:
            normalized_rating = float(rating) if rating is not None else None
        except (TypeError, ValueError):
            normalized_rating = None
        if normalized_rating is not None and 0 <= normalized_rating <= 10:
            rated.append(normalized_rating)
        else:
            normalized_rating = None
        reviews.append(
            {
                "id": str(raw.get("id") or f"tmdb-{len(reviews) + 1}"),
                "source": "tmdb",
                "source_name": "TMDB",
                "kind": "user_review",
                "author": raw.get("author")
                or author_details.get("username")
                or "TMDB user",
                "username": author_details.get("username"),
                "avatar": avatar or None,
                "rating": normalized_rating,
                "content": content,
                "created_at": raw.get("created_at"),
                "updated_at": raw.get("updated_at"),
                "url": raw.get("url"),
            }
        )
        if len(reviews) >= _DETAIL_REVIEW_LIMIT:
            break

    mdb_facts = meta.mdb_facts if meta and isinstance(meta.mdb_facts, dict) else {}
    for index, raw in enumerate(mdb_facts.get("reviews") or []):
        content = _clean_review_text(raw)
        fingerprint = content.casefold()[:240]
        if not content or fingerprint in seen or len(reviews) >= _DETAIL_REVIEW_LIMIT:
            continue
        seen.add(fingerprint)
        reviews.append(
            {
                "id": f"mdblist-excerpt-{index + 1}",
                "source": "mdblist",
                "source_name": "MDBList",
                "kind": "review_excerpt",
                "author": None,
                "username": None,
                "avatar": None,
                "rating": None,
                "content": content,
                "created_at": None,
                "updated_at": None,
                "url": None,
            }
        )

    tmdb_total = (
        int(review_block.get("total_results") or 0)
        if isinstance(review_block, dict)
        else 0
    )
    return reviews, {
        "returned": len(reviews),
        "total_available": max(
            tmdb_total, len([r for r in reviews if r["source"] == "tmdb"])
        )
        + len([r for r in reviews if r["source"] == "mdblist"]),
        "average_user_rating": round(sum(rated) / len(rated), 2) if rated else None,
        "rated_review_count": len(rated),
        "sources": list(dict.fromkeys(r["source"] for r in reviews)),
    }


def _tmdb_certification(detail: dict, media_type: str) -> str | None:
    """Prefer a US content rating, then fall back to the first available one."""
    block_name = "content_ratings" if media_type == "tv" else "release_dates"
    block = detail.get(block_name) or {}
    rows = block.get("results") if isinstance(block, dict) else []
    candidates: list[tuple[bool, str]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        country = str(row.get("iso_3166_1") or "").upper()
        if media_type == "tv":
            values = [row.get("rating")]
        else:
            values = [
                item.get("certification") for item in row.get("release_dates") or []
            ]
        for value in values:
            certification = str(value or "").strip()
            if certification:
                candidates.append((country == "US", certification))
    return next((value for preferred, value in candidates if preferred), None) or next(
        (value for _, value in candidates), None
    )


def _title_extras(meta: MediaMetadata | None, detail: dict, media_type: str) -> dict:
    """Stable native-facing facts beyond the core title/card payload."""
    from app.tmdb_client import tmdb_client

    mdb = meta.mdb_facts if meta and isinstance(meta.mdb_facts, dict) else {}
    stored_certification = _stored_content_rating(meta)
    live_certification = _tmdb_certification(detail, media_type)
    certification = stored_certification or live_certification
    keyword_block = detail.get("keywords") or {}
    keyword_rows = (
        keyword_block.get("results") or keyword_block.get("keywords") or []
        if isinstance(keyword_block, dict)
        else []
    )
    keywords = list(
        dict.fromkeys(
            str(item.get("name") if isinstance(item, dict) else item).strip()
            for item in [*(keyword_rows or []), *(mdb.get("keywords") or [])]
            if str(item.get("name") if isinstance(item, dict) else item).strip()
        )
    )[:30]

    videos: list[dict] = []
    for raw in (detail.get("videos") or {}).get("results") or []:
        if not isinstance(raw, dict) or raw.get("site") not in {"YouTube", "Vimeo"}:
            continue
        kind = str(raw.get("type") or "").lower()
        if kind not in {"trailer", "teaser", "clip", "featurette"}:
            continue
        key = str(raw.get("key") or "").strip()
        if not key:
            continue
        site = str(raw.get("site"))
        videos.append(
            {
                "id": str(raw.get("id") or key),
                "name": raw.get("name") or raw.get("type") or "Video",
                "type": kind,
                "site": site.lower(),
                "key": key,
                "url": (
                    f"https://www.youtube.com/watch?v={key}"
                    if site == "YouTube"
                    else f"https://vimeo.com/{key}"
                ),
                "official": bool(raw.get("official")),
                "published_at": raw.get("published_at"),
            }
        )
    videos.sort(key=lambda video: (not video["official"], video["type"] != "trailer"))

    extras = {
        "tagline": detail.get("tagline") or None,
        "status": detail.get("status") or None,
        "original_language": detail.get("original_language") or None,
        "homepage": detail.get("homepage") or None,
        "content_rating": certification,
        "content_advisory": {
            "certification": certification,
            "age_rating": mdb.get("age_rating"),
            "common_sense": mdb.get("commonsense"),
            "source": (
                "database"
                if stored_certification
                else "tmdb" if live_certification else None
            ),
        },
        "keywords": keywords,
        "trailers": videos[:10],
        "studios": [
            company.get("name")
            for company in detail.get("production_companies") or []
            if isinstance(company, dict) and company.get("name")
        ],
        "networks": [
            network.get("name")
            for network in detail.get("networks") or []
            if isinstance(network, dict) and network.get("name")
        ],
        "countries": [
            country.get("name") or country.get("iso_3166_1")
            for country in detail.get("production_countries") or []
            if isinstance(country, dict)
            and (country.get("name") or country.get("iso_3166_1"))
        ],
    }
    live_date = (
        tmdb_client.next_episode_air_date(detail)
        if media_type == "tv"
        else tmdb_client.digital_release_date(detail.get("release_dates") or {})
    )
    stored_date = (
        meta.next_episode_air_date
        if meta and media_type == "tv"
        else meta.digital_release_date if meta else None
    )
    # An appended movie release_dates block is authoritative even when the
    # corrected extractor returns None. Falling back in that case would revive a
    # stale storefront/rerelease date that the live payload just disproved.
    has_live_movie_release_dates = (
        media_type == "movie" and _dated_movie_release_block(detail) is not None
    )
    effective_date = (
        live_date if has_live_movie_release_dates else live_date or stored_date
    )
    if (
        media_type == "tv"
        and not effective_date
        and _provider_confirms_no_regular_episodes({"_provider_detail": detail})
    ):
        # Unaired series can have a known premiere before TMDB creates their
        # first episode. Keep this rule in extras so cached overlay refreshes
        # preserve the same reminder date as the initial hero response.
        effective_date = detail.get("first_air_date")
    coming_soon = _coming_soon_date(
        {
            "next_episode_air_date": effective_date,
            "digital_release_date": effective_date,
        },
        media_type,
    )
    if coming_soon:
        extras["comingSoonDate"] = coming_soon
    return extras


def _items_from_ids(db: Session, ids: list, media_type: str) -> list:
    """Serialize tmdb ids into dashboard items, preserving the given order.

    The caller's order is a ranking. A plain ``IN`` query returns rows in
    whatever order the database likes, so re-sorting here is what keeps the
    ranking the point of the query rather than a discarded intermediate.
    """
    if not ids:
        return []
    rows = (
        db.query(MediaMetadata)
        .options(load_only(*_MEDIA_CARD_COLUMNS))
        .filter(
            MediaMetadata.tmdb_id.in_(ids),
            MediaMetadata.media_type == media_type,
        )
        .all()
    )
    by_id = {r.tmdb_id: r for r in rows}
    items = []
    for tid in ids:
        meta = by_id.get(tid)
        if meta is None:
            continue
        items.append(
            _serialize_media_item(
                tmdb_id=tid,
                media_type=media_type,
                title=meta.title,
                meta=meta,
            )
        )
    return items


def _catalog_similar_fallback(
    db: Session,
    tmdb_id: int,
    media_type: str,
    *,
    target_genres: object = None,
    limit: int = 18,
) -> list:
    """Return a fast, deterministic local fallback when providers have no rail.

    Vector similarity is still preferred, followed by TMDB's `/similar`. This
    final tier prevents a complete detail snapshot from permanently carrying an
    empty row. It considers a bounded set of popular catalog titles, favors genre
    overlap, and uses stable tie-breakers so both content and order stay unchanged
    across requests.
    """
    if target_genres is None:
        target_genres = (
            db.query(MediaMetadata.genres)
            .filter(
                MediaMetadata.tmdb_id == tmdb_id,
                MediaMetadata.media_type == media_type,
            )
            .scalar()
        )

    def genre_keys(value: object) -> set[str]:
        if not isinstance(value, list):
            return set()
        return {str(genre).strip().casefold() for genre in value if str(genre).strip()}

    wanted = genre_keys(target_genres)
    candidates = (
        db.query(MediaMetadata)
        .options(
            # The card columns, plus the two this function sorts on.
            load_only(
                *_MEDIA_CARD_COLUMNS,
                MediaMetadata.vote_count,
                MediaMetadata.popularity,
            )
        )
        .filter(
            MediaMetadata.media_type == media_type,
            MediaMetadata.tmdb_id != tmdb_id,
            MediaMetadata.adult.isnot(True),
        )
        .order_by(
            func.coalesce(MediaMetadata.popularity, 0).desc(),
            func.coalesce(MediaMetadata.vote_count, 0).desc(),
            MediaMetadata.tmdb_id.asc(),
        )
        .limit(max(120, limit * 12))
        .all()
    )

    def rank(meta: MediaMetadata) -> tuple:
        genres = genre_keys(meta.genres)
        shared = len(wanted & genres)
        union = len(wanted | genres)
        overlap = shared / union if union else 0.0
        return (
            -int(shared > 0),
            -shared,
            -overlap,
            -float(meta.popularity or 0),
            -int(meta.vote_count or 0),
            int(meta.tmdb_id),
        )

    ranked = sorted(candidates, key=rank)[:limit]
    return [
        _serialize_media_item(
            tmdb_id=meta.tmdb_id,
            media_type=media_type,
            title=meta.title,
            meta=meta,
        )
        for meta in ranked
    ]


@app.get("/account/api/facets")
def account_api_facets(
    request: Request,
    response: Response,
    media_type: Literal["movie", "tv"] = Query("movie"),
    min_titles: int = Query(25, ge=1, le=10_000),
    db: Session = Depends(get_db_dependency),
):
    """The browsable vocabulary, grouped by facet.

    Read entirely from ``tag_stats``, which the relevance refresh precomputes,
    so this is a few hundred rows of already-aggregated counts rather than a
    ``GROUP BY`` over millions of ``movie_tags``.
    """
    user = _get_active_profile(request, db)
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is deactivated")

    response.headers["Cache-Control"] = _FACETS_CACHE_CONTROL
    return _discovery_facets(db, media_type, min_titles)


@app.get("/api/v2/facets")
def native_api_facets(
    user_key: BearerUserKey,
    response: Response,
    media_type: Literal["movie", "tv"] = Query("movie"),
    min_titles: int = Query(25, ge=1, le=10_000),
    db: Session = Depends(get_db_dependency),
):
    """Native, profile-key-authenticated browsable tag vocabulary."""
    _, user = _account_for_user_key(user_key, db)
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Profile is deactivated")
    response.headers["Cache-Control"] = _FACETS_CACHE_CONTROL
    return _discovery_facets(db, media_type, min_titles)


def _discovery_facets(
    db: Session, media_type: Literal["movie", "tv"], min_titles: int
) -> dict:
    """Shared pre-aggregated tag facets for browser and native clients."""

    rows = (
        db.query(Tag.id, Tag.name, Tag.category, TagStats.doc_count)
        .join(TagStats, TagStats.tag_id == Tag.id)
        .filter(
            TagStats.media_type == media_type,
            TagStats.doc_count >= min_titles,
            Tag.display.is_(True),
            Tag.is_active.is_(True),
        )
        .order_by(Tag.category, TagStats.doc_count.desc(), Tag.name)
        .all()
    )

    facets: dict = {}
    for tag_id, name, facet, doc_count in rows:
        facets.setdefault(facet, []).append(
            {"id": int(tag_id), "name": name, "count": int(doc_count or 0)}
        )
    return {
        "media_type": media_type,
        "facets": [
            {"facet": facet, "tags": tags} for facet, tags in sorted(facets.items())
        ],
        "total_tags": len(rows),
    }


@app.get("/account/api/browse")
def account_api_browse(
    request: Request,
    response: Response,
    tags: str = Query("", max_length=400),
    media_type: Literal["movie", "tv"] = Query("movie"),
    mode: Literal["all", "any"] = Query("all"),
    skip: int = Query(0, ge=0, le=10_000),
    limit: int = Query(60, ge=1, le=120),
    db: Session = Depends(get_db_dependency),
):
    """Rank the library against an ad-hoc tag set.

    ``tags`` is a comma-separated list of tag ids. ``mode=all`` intersects
    them, ``mode=any`` unions. Ranking is the same relevance the catalog rows
    use, so browsing a facet combination and seeing a curated row built from
    the same tags produce the same order.
    """
    user = _get_active_profile(request, db)
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is deactivated")

    response.headers["Cache-Control"] = _BROWSE_CACHE_CONTROL
    return annotate_tree(db, _discovery_browse(db, tags, media_type, mode, skip, limit))


@app.get("/api/v2/browse")
def native_api_browse(
    user_key: BearerUserKey,
    response: Response,
    tags: str = Query("", max_length=400),
    media_type: Literal["movie", "tv"] = Query("movie"),
    mode: Literal["all", "any"] = Query("all"),
    skip: int = Query(0, ge=0, le=10_000),
    limit: int = Query(60, ge=1, le=120),
    db: Session = Depends(get_db_dependency),
):
    """Native multi-tag browsing with the same ranking used by catalog rows."""
    _, user = _account_for_user_key(user_key, db)
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Profile is deactivated")
    response.headers["Cache-Control"] = _BROWSE_CACHE_CONTROL
    return annotate_tree(db, _discovery_browse(db, tags, media_type, mode, skip, limit))


def _discovery_browse(
    db: Session,
    tags: str,
    media_type: Literal["movie", "tv"],
    mode: Literal["all", "any"],
    skip: int,
    limit: int,
) -> dict:
    """Shared relevance-ranked tag browsing for browser and native clients."""

    tag_ids = []
    for part in tags.split(","):
        part = part.strip()
        if part.isdigit():
            tag_ids.append(int(part))
    if not tag_ids:
        return {"items": [], "total_tags": 0, "has_more": False}

    from app.relevance import browse_by_tags

    # One extra row, so "is there a next page" is answered by the query rather
    # than by guessing from a full page.
    ranked = browse_by_tags(
        db,
        tag_ids,
        media_type,
        mode=mode,
        limit=limit + 1,
        offset=skip,
    )
    has_more = len(ranked) > limit
    ranked = ranked[:limit]
    scores = dict(ranked)
    items = _items_from_ids(db, [tid for tid, _ in ranked], media_type)
    for item in items:
        item["relevance"] = round(scores.get(item["tmdb_id"], 0.0), 4)
    return {
        "items": items,
        "total_tags": len(tag_ids),
        "has_more": has_more,
    }


@app.get("/account/api/details")
async def account_api_details(
    request: Request,
    tmdb_id: int = Query(0, ge=0),
    media_type: Literal["movie", "tv"] | None = Query(None),
    imdb_id: str | None = Query(None),
    include: Literal["meta"] | None = Query(None),
    view: Literal["full", "hero"] = Query("full"),
    refresh: bool = Query(False),
    ids: list[str] | None = Query(None),
    db: Session = Depends(get_db_dependency),
):
    """Return full browser detail content: cast, episodes, and similar titles.

    Accepts either a ``tmdb_id`` + ``media_type`` (the normal path) or an
    ``imdb_id``, which is resolved to a tmdb id + type (404 when it cannot be).
    """
    await _request_db_call(db, _active_profile_or_403, request)

    if ids is not None:
        if refresh:
            raise HTTPException(
                status_code=400,
                detail="refresh cannot be combined with ids",
            )
        if tmdb_id > 0 or imdb_id is not None:
            raise HTTPException(
                status_code=400,
                detail="ids cannot be combined with tmdb_id or imdb_id",
            )
        return await _with_availability(
            await _discovery_details_batch(
                ids, media_type, include_meta=include == "meta", view=view
            )
        )
    return await _with_availability(
        await _discovery_details(
            tmdb_id,
            media_type,
            imdb_id,
            include_meta=include == "meta",
            force_refresh=refresh,
            view=view,
        )
    )


@app.get("/api/v2/details")
async def native_api_details(
    user_key: BearerUserKey,
    tmdb_id: int = Query(0, ge=0),
    media_type: Literal["movie", "tv"] | None = Query(None),
    imdb_id: str | None = Query(None),
    include: Literal["meta"] | None = Query(None),
    view: Literal["full", "hero"] = Query("full"),
    refresh: bool = Query(False),
    ids: list[str] | None = Query(None),
    db: Session = Depends(get_db_dependency),
):
    """Native enriched details authenticated by the active profile user_key."""
    _, user = await _request_db_call(
        db, lambda session: _account_for_user_key(user_key, session)
    )
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Profile is deactivated")
    if ids is not None:
        if refresh:
            raise HTTPException(
                status_code=400,
                detail="refresh cannot be combined with ids",
            )
        if tmdb_id > 0 or imdb_id is not None:
            raise HTTPException(
                status_code=400,
                detail="ids cannot be combined with tmdb_id or imdb_id",
            )
        return await _with_availability(
            await _discovery_details_batch(
                ids, media_type, include_meta=include == "meta", view=view
            )
        )
    return await _with_availability(
        await _discovery_details(
            tmdb_id,
            media_type,
            imdb_id,
            include_meta=include == "meta",
            force_refresh=refresh,
            view=view,
        )
    )


async def _with_availability(result):
    """Annotate a details payload (dict or JSONResponse) with availability.

    Runs after the details cache, so cached snapshots never hold a stale flag.
    """

    def annotate(payload):
        with get_db() as session:
            return annotate_tree(session, payload)

    if isinstance(result, dict):
        return await asyncio.to_thread(annotate, result)
    if isinstance(result, JSONResponse) and result.status_code == 200:
        try:
            payload = json.loads(result.body)
        except ValueError:
            return result
        payload = await asyncio.to_thread(annotate, payload)
        headers = {
            k: v
            for k, v in result.headers.items()
            if k.lower() not in ("content-length", "content-type")
        }
        return JSONResponse(content=payload, status_code=200, headers=headers)
    return result


_DETAIL_CACHE_CONTROL = "private, max-age=1800"
_DETAIL_PARTIAL_CACHE_CONTROL = "private, no-store"
_DETAIL_BATCH_MAX = 40
_DETAIL_BATCH_CONCURRENCY = 6
_DETAIL_TMDB_CONCURRENCY = 12
_DETAIL_BACKGROUND_TMDB_CONCURRENCY = 4
_DETAIL_DYNAMIC_REFRESH_CONCURRENCY = settings.detail_dynamic_refresh_concurrency
_DETAIL_DYNAMIC_OVERLAY_TTL_SECONDS = settings.detail_dynamic_overlay_ttl_seconds
_DETAIL_SNAPSHOT_SCHEMA_VERSION = 1
_DETAIL_FORCE_REFRESH_COOLDOWN_SECONDS = 15
_DETAIL_MOVIE_FRESH_SECONDS = 7 * 24 * 60 * 60
_DETAIL_TV_FRESH_SECONDS = 6 * 60 * 60
_DETAIL_SNAPSHOT_ACCESS_WRITE_SECONDS = 60 * 60
_DETAIL_SNAPSHOT_RETENTION_DAYS = 90

# Details fan-out runs on more than one event loop in tests and maintenance workers.
# Keep one shared limit per loop: a module-level asyncio.Semaphore would otherwise bind
# to the first contended loop and fail when a later loop tried to reuse it.
_detail_semaphore_lock = threading.Lock()
_detail_batch_semaphores: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_detail_tmdb_semaphores: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_detail_background_tmdb_semaphores: weakref.WeakKeyDictionary = (
    weakref.WeakKeyDictionary()
)
_detail_dynamic_refresh_semaphores: weakref.WeakKeyDictionary = (
    weakref.WeakKeyDictionary()
)
_detail_build_tasks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_detail_core_tasks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_detail_refresh_tasks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_detail_dynamic_refresh_tasks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _loop_detail_semaphore(
    cache: weakref.WeakKeyDictionary, limit: int
) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    with _detail_semaphore_lock:
        semaphore = cache.get(loop)
        if semaphore is None:
            semaphore = asyncio.Semaphore(limit)
            cache[loop] = semaphore
    return semaphore


def _detail_batch_semaphore() -> asyncio.Semaphore:
    """Globally cap concurrent detail builds on this server event loop."""
    return _loop_detail_semaphore(_detail_batch_semaphores, _DETAIL_BATCH_CONCURRENCY)


def _detail_tmdb_semaphore() -> asyncio.Semaphore:
    """Leave headroom in TMDB's 20-connection pool for non-details traffic."""
    return _loop_detail_semaphore(_detail_tmdb_semaphores, _DETAIL_TMDB_CONCURRENCY)


def _detail_background_tmdb_semaphore() -> asyncio.Semaphore:
    """Keep speculative batches from filling every interactive TMDB permit."""
    return _loop_detail_semaphore(
        _detail_background_tmdb_semaphores, _DETAIL_BACKGROUND_TMDB_CONCURRENCY
    )


def _detail_dynamic_refresh_semaphore() -> asyncio.Semaphore:
    """Bound database/vector work so background refresh cannot swamp requests."""
    return _loop_detail_semaphore(
        _detail_dynamic_refresh_semaphores, _DETAIL_DYNAMIC_REFRESH_CONCURRENCY
    )


@asynccontextmanager
async def _detail_tmdb_slot(
    *,
    background: bool,
    promotion_event: asyncio.Event | None = None,
):
    """Reserve TMDB capacity, promoting a tapped background flight in place.

    A visible batch may create the single-flight before the user taps the title.
    If it is still waiting for the stricter background permit, an interactive
    join signals ``promotion_event`` and the same build moves to the foreground
    limiter. No provider request is duplicated and foreground capacity remains
    available to the title the user is actually opening.
    """
    if not background or (promotion_event is not None and promotion_event.is_set()):
        async with _detail_tmdb_semaphore():
            yield
        return

    background_semaphore = _detail_background_tmdb_semaphore()
    if promotion_event is None:
        async with background_semaphore:
            async with _detail_tmdb_semaphore():
                yield
        return

    acquire_background = asyncio.create_task(background_semaphore.acquire())
    await_promotion = asyncio.create_task(promotion_event.wait())
    background_acquired = False
    try:
        done, _ = await asyncio.wait(
            {acquire_background, await_promotion},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if acquire_background not in done:
            acquire_background.cancel()
        await_promotion.cancel()
        await asyncio.gather(
            acquire_background, await_promotion, return_exceptions=True
        )
        background_acquired = (
            acquire_background.done()
            and not acquire_background.cancelled()
            and acquire_background.exception() is None
            and acquire_background.result()
        )
        async with _detail_tmdb_semaphore():
            yield
    finally:
        for waiter in (acquire_background, await_promotion):
            if not waiter.done():
                waiter.cancel()
        await asyncio.gather(
            acquire_background, await_promotion, return_exceptions=True
        )
        # The acquire can win the race immediately before cancellation even when
        # promotion was the completed waiter observed above. Never leak that permit.
        if (
            not background_acquired
            and acquire_background.done()
            and not acquire_background.cancelled()
            and acquire_background.exception() is None
            and acquire_background.result()
        ):
            background_acquired = True
        if background_acquired:
            background_semaphore.release()


def _loop_task_map(cache: weakref.WeakKeyDictionary) -> dict:
    loop = asyncio.get_running_loop()
    with _detail_semaphore_lock:
        tasks = cache.get(loop)
        if tasks is None:
            tasks = {}
            cache[loop] = tasks
    return tasks


def _detail_episode_stats(payload: dict) -> dict:
    """Summarize one **un-stripped** detail payload's episode completeness.

    Must be given the internal payload, not the wire content: `_details_content`
    removes every ``_``-prefixed key, including ``_provider_detail``, which
    `_detail_snapshot_is_complete` needs to tell "no regular seasons yet" apart
    from "the season fetches produced nothing". Deriving the verdict from the
    stripped body made the response's cache policy disagree with the decision
    the persistence layer had just made about the same payload.
    """
    media_type = str((payload.get("item") or {}).get("media_type") or "movie")
    complete = _detail_episode_index_is_complete(payload, media_type)
    hero_complete = _detail_snapshot_is_complete(payload, media_type)
    return {
        "media_type": media_type,
        "usable": _detail_usable_episode_count(payload),
        "expected": _detail_expected_regular_episode_count(payload),
        "complete": complete,
        "partial": payload.get("episodes_complete") is False or not hero_complete,
        "source": str(payload.get("_detail_source") or "unknown"),
    }


def _detail_response(content: dict, *, stats: list[dict] | None = None) -> JSONResponse:
    """Return a native detail response with one cache policy everywhere."""
    response = JSONResponse(content=content)
    if stats is None:
        details = [content]
        if isinstance(content.get("results"), list):
            details = [
                result.get("details") or {}
                for result in content["results"]
                if isinstance(result, dict)
            ]
        stats = [
            _detail_episode_stats(detail)
            for detail in details
            if isinstance(detail, dict)
        ]
    partial_episodes = any(entry["partial"] for entry in stats)
    transient_batch_error = any(
        isinstance(error, dict)
        and isinstance(error.get("status"), int)
        and error["status"] >= 500
        for error in content.get("errors") or []
    )
    response.headers["Cache-Control"] = (
        _DETAIL_PARTIAL_CACHE_CONTROL
        if partial_episodes or transient_batch_error
        else _DETAIL_CACHE_CONTROL
    )
    if stats:
        # Diagnostic only. Lets a client log correlate its own "episodes are
        # missing" verdict with the server's, without a second request.
        response.headers["X-Curatio-Detail-Episodes"] = ";".join(
            f"usable={entry['usable']}"
            f",expected={entry['expected']}"
            f",complete={str(entry['complete']).lower()}"
            f",type={entry['media_type']}"
            f",source={entry['source']}"
            for entry in stats[:10]
        )
    return response


def _details_item_meta(payload: dict, identity_id: str) -> dict:
    """Project an enriched detail payload into the compact ``meta`` shape.

    Served with ``include=meta`` on the details endpoints. This deliberately derives
    from the already-built response instead of a second request. Playback
    identifiers retain the identity the caller opened: an IMDb-keyed request keeps
    ``tt`` episode ids, while a TMDB-keyed item keeps ``tmdb`` episode ids.
    """
    item = payload.get("item") or {}
    content_type = item.get("content_type") or _api_media_type(
        item.get("media_type") or "movie"
    )
    meta: dict = {
        "id": identity_id,
        "type": content_type,
        "name": item.get("title") or "Untitled",
        "posterShape": "poster",
    }

    field_map = {
        "overview": "description",
        "year": "releaseInfo",
        "digitalReleaseInfo": "digitalReleaseInfo",
        "genres": "genres",
        "content_rating": "contentRating",
    }
    for source, target in field_map.items():
        value = item.get(source)
        if value:
            meta[target] = value

    # Match legacy /meta image quality rather than reusing card-sized detail URLs.
    for source, fallback, target in (
        ("poster_path", "poster", "poster"),
        ("backdrop_path", "backdrop", "background"),
        ("logo_path", "logo", "logo"),
    ):
        image = _image_url(item.get(source) or item.get(fallback), "original")
        if image:
            meta[target] = image

    rating = item.get("rating")
    if rating is not None:
        meta["imdbRating"] = str(round(float(rating), 1))
    runtime = item.get("runtime")
    if runtime:
        minutes = int(runtime)
        if content_type == "series":
            meta["runtime"] = f"{minutes}min"
        else:
            meta["runtime"] = (
                f"{minutes // 60}h {minutes % 60}min"
                if minutes >= 60
                else f"{minutes}min"
            )
    imdb_id = item.get("imdb_id")
    if imdb_id:
        meta["imdb_id"] = imdb_id

    cast_names = [
        row.get("name") for row in payload.get("cast") or [] if row.get("name")
    ]
    if cast_names:
        meta["cast"] = cast_names
    crew = payload.get("crew") or {}
    if crew.get("directors"):
        meta["director"] = crew["directors"]
    if crew.get("writers"):
        meta["writer"] = crew["writers"]

    links = []
    for member in payload.get("cast") or []:
        name = member.get("name")
        if not name:
            continue
        link = {"name": name, "category": "Cast"}
        if member.get("profile"):
            link["poster"] = member["profile"]
        links.append(link)
    for name in crew.get("directors") or []:
        links.append({"name": name, "category": "Directors"})
    if links:
        meta["links"] = links

    trailers = [
        {"source": trailer["key"], "type": "Trailer"}
        for trailer in payload.get("trailers") or []
        if trailer.get("site") == "youtube"
        and trailer.get("type") == "trailer"
        and trailer.get("key")
    ][:5]
    if trailers:
        meta["trailers"] = trailers

    videos = []
    if content_type == "series":
        for season in payload.get("seasons") or []:
            for episode in season.get("episodes") or []:
                season_number = episode.get("season")
                episode_number = episode.get("episode")
                if (
                    not isinstance(season_number, int)
                    or isinstance(season_number, bool)
                    or season_number <= 0
                    or not isinstance(episode_number, int)
                    or isinstance(episode_number, bool)
                    or episode_number <= 0
                ):
                    continue
                if identity_id.startswith("tt"):
                    video_id = f"{identity_id}:{season_number}:{episode_number}"
                else:
                    video_id = episode.get("video_id")
                if not video_id:
                    continue
                video = {
                    "id": video_id,
                    "title": episode.get("title") or f"Episode {episode_number}",
                    "season": season_number,
                    "episode": episode_number,
                    "overview": episode.get("overview") or "",
                    "released": episode.get("air_date") or "",
                }
                if episode.get("still"):
                    video["thumbnail"] = episode["still"]
                videos.append(video)
        # Series-level default_video_id historically resolves to the IMDb/show id in the
        # native client. Pointing it at episode 1 silently changes resume/cache identity.
        meta["episodesComplete"] = bool(payload.get("episodes_complete"))
    else:
        default_video_id = (
            identity_id
            if identity_id.startswith("tt")
            else (item.get("video_id") or identity_id)
        )
        # A single video entry is useful to native clients even for movies: it
        # makes the playback key explicit instead of requiring type-specific logic.
        videos.append({"id": default_video_id, "title": meta["name"]})
        meta["default_video_id"] = default_video_id

    meta["videos"] = videos
    return meta


def _details_content(payload: dict, identity_id: str, include_meta: bool) -> dict:
    """Copy a cached detail payload and add request-specific metadata when asked.

    ``addon_type`` (the content type field the Apple client decodes) is stamped
    here rather than in the serializers because a payload reaching this point can
    be a freshly built one, a persisted snapshot, or an overlay recomposition.
    Adding it on the way out gives every source the field identically, and it
    keeps the alias off the shared card serializers, which also feed the home
    screen, browse, search and library. ``seasons[].episodes[]`` deliberately keep
    ``content_type`` alone: clients decode that object but never read the type
    from it.

    Entries are rebuilt rather than mutated. Callers hand over a shallow copy whose
    nested objects are shared with cached payloads and with the ORM-attached dict
    `_load_detail_snapshot` returns (`_apply_detail_dynamic_overlay` no longer
    deep-copies), so nothing here may edit a nested object in place.
    """
    content = {key: value for key, value in payload.items() if not key.startswith("_")}
    media_type = str((payload.get("item") or {}).get("media_type") or "movie")
    hero_complete = _detail_snapshot_is_complete(payload, media_type)
    content["hero_complete"] = hero_complete
    content["details_complete"] = (
        hero_complete and payload.get("details_complete") is not False
    )
    item = content.get("item")
    if isinstance(item, dict):
        content["item"] = {**item, "addon_type": _entry_content_type(item)}
    similar = content.get("similar")
    if isinstance(similar, list):
        content["similar"] = [
            (
                {**entry, "addon_type": _entry_content_type(entry)}
                if isinstance(entry, dict)
                else entry
            )
            for entry in similar
        ]
    if include_meta:
        content["meta"] = _details_item_meta(payload, identity_id)
    return content


def _detail_batch_tokens(raw_ids: list[str]) -> list[str]:
    """Accept comma-separated and repeated ``ids`` query parameters."""
    tokens = [
        part.strip() for value in raw_ids for part in value.split(",") if part.strip()
    ]
    if not tokens:
        raise HTTPException(status_code=400, detail="ids must contain at least one id")
    if len(tokens) > _DETAIL_BATCH_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"ids accepts at most {_DETAIL_BATCH_MAX} items",
        )
    return tokens


def _parse_detail_batch_id(
    value: str, default_media_type: Literal["movie", "tv"] | None
) -> tuple[int, Literal["movie", "tv"] | None, str | None]:
    """Parse a compact batch id without making a provider request.

    Preferred form is ``movie:550`` or ``tv:1399``. Bare numeric and
    ``tmdb:`` ids inherit the request's ``media_type``; IMDb ids are resolved by
    the normal details path. ``series`` is accepted as an alias for TV.
    """
    token = value.strip()
    explicit_type: Literal["movie", "tv"] | None = None
    first, separator, remainder = token.partition(":")
    if separator and first.lower() in {"movie", "tv", "series"}:
        explicit_type = (
            "tv"
            if first.lower() == "series"
            else cast(Literal["movie", "tv"], first.lower())
        )
        token = remainder

    if token.startswith("tmdb:"):
        token = token.split(":", 1)[1]
    if token.isdigit() and int(token) > 0:
        media_type = explicit_type or default_media_type
        if media_type is None:
            raise ValueError("media_type is required for TMDB ids")
        return int(token), media_type, None

    imdb_id = _normalize_imdb_id(token)
    if imdb_id:
        return 0, explicit_type or default_media_type, imdb_id
    raise ValueError("expected movie:<tmdb_id>, tv:<tmdb_id>, tmdb:<id>, or tt<id>")


async def _discovery_details_batch(
    raw_ids: list[str],
    default_media_type: Literal["movie", "tv"] | None,
    *,
    include_meta: bool,
    view: Literal["full", "hero"] = "full",
) -> JSONResponse:
    """Return up to 40 independent details results; one failure never drops peers."""
    started_at = monotonic()
    tokens = _detail_batch_tokens(raw_ids)

    async def fetch_one(
        request_id: str,
    ) -> tuple[dict | None, dict | None, dict | None]:
        try:
            tmdb_id, media_type, imdb_id = _parse_detail_batch_id(
                request_id, default_media_type
            )
        except ValueError as exc:
            return None, {"id": request_id, "status": 400, "detail": str(exc)}, None

        try:
            async with _detail_batch_semaphore():
                payload, identity_id = await _discovery_details_payload(
                    tmdb_id,
                    media_type,
                    imdb_id,
                    background=True,
                    refresh_dynamic_overlay=False,
                    view=view,
                )
            return (
                {
                    "id": request_id,
                    "details": _details_content(payload, identity_id, include_meta),
                },
                None,
                # From the payload, before `_details_content` strips `_` keys.
                _detail_episode_stats(payload),
            )
        except HTTPException as exc:
            return (
                None,
                {
                    "id": request_id,
                    "status": exc.status_code,
                    "detail": str(exc.detail),
                },
                None,
            )
        except Exception as exc:
            logger.warning(f"Batch detail fetch failed for {request_id}: {exc}")
            return (
                None,
                {
                    "id": request_id,
                    "status": 502,
                    "detail": "Could not load details",
                },
                None,
            )

    # Preserve caller order/duplicates in the response while coalescing duplicate work.
    unique_tokens = list(dict.fromkeys(tokens))
    unique_fetched = await asyncio.gather(
        *[fetch_one(token) for token in unique_tokens]
    )
    fetched_by_token = dict(zip(unique_tokens, unique_fetched))
    fetched = [fetched_by_token[token] for token in tokens]
    results = [result for result, _, _ in fetched if result is not None]
    errors = [error for _, error, _ in fetched if error is not None]
    stats = [entry for _, _, entry in fetched if entry is not None]
    response = _detail_response(
        {
            "results": results,
            "errors": errors,
            "requested": len(tokens),
            "returned": len(results),
        },
        stats=stats,
    )
    response.headers["Server-Timing"] = (
        f"curatio-details;dur={(monotonic() - started_at) * 1000:.1f}"
    )
    return response


async def _discovery_details(
    tmdb_id: int,
    media_type: Literal["movie", "tv"] | None,
    imdb_id: str | None,
    *,
    include_meta: bool = False,
    force_refresh: bool = False,
    view: Literal["full", "hero"] = "full",
) -> JSONResponse:
    """Shared rich detail response for browser sessions and native profiles."""

    started_at = monotonic()
    payload, identity_id = await _discovery_details_payload(
        tmdb_id, media_type, imdb_id, force_refresh=force_refresh, view=view
    )
    response = _detail_response(
        _details_content(payload, identity_id, include_meta),
        stats=[_detail_episode_stats(payload)],
    )
    response.headers["Server-Timing"] = (
        f"curatio-details;dur={(monotonic() - started_at) * 1000:.1f}"
    )
    return response


def _detail_expected_regular_episode_count(payload: dict) -> int:
    """Return the strongest regular-season episode count available in a payload."""

    totals = []
    provider_detail = payload.get("_provider_detail")
    season_sources = [payload.get("seasons")]
    if isinstance(provider_detail, dict):
        season_sources.append(provider_detail.get("seasons"))

    for seasons in season_sources:
        if not isinstance(seasons, list):
            continue
        total = 0
        for season in seasons:
            if not isinstance(season, dict):
                continue
            season_number = season.get("season", season.get("season_number"))
            episode_count = season.get("episode_count")
            if (
                isinstance(season_number, int)
                and not isinstance(season_number, bool)
                and season_number > 0
                and isinstance(episode_count, int)
                and not isinstance(episode_count, bool)
                and episode_count > 0
            ):
                total += episode_count
        totals.append(total)
    return max(totals, default=0)


def _detail_usable_episode_count(payload: dict) -> int:
    count = 0
    for season in payload.get("seasons") or []:
        if not isinstance(season, dict):
            continue
        for episode in season.get("episodes") or []:
            if not isinstance(episode, dict):
                continue
            season_number = episode.get("season", episode.get("season_number"))
            episode_number = episode.get("episode", episode.get("episode_number"))
            if (
                isinstance(season_number, int)
                and not isinstance(season_number, bool)
                and season_number > 0
                and isinstance(episode_number, int)
                and not isinstance(episode_number, bool)
                and episode_number > 0
                and bool(episode.get("video_id"))
            ):
                count += 1
    return count


def _provider_confirms_no_regular_episodes(payload: dict) -> bool:
    """Whether the provider body positively states this show has no regular episodes.

    The distinction matters because an empty episode list has two very different
    causes: a series that genuinely has not aired a regular season yet, and a
    build whose season requests came back empty.  Only the first may be frozen
    as a complete snapshot.

    Presence of ``_provider_detail`` is NOT evidence of either -- every freshly
    built payload carries one -- so this asks what the provider actually said.
    Absent a provider body we cannot confirm anything, which is the safe answer.
    """
    provider = payload.get("_provider_detail")
    if not isinstance(provider, dict):
        return False
    for season in provider.get("seasons") or []:
        if not isinstance(season, dict):
            continue
        season_number = season.get("season_number")
        if (
            isinstance(season_number, int)
            and not isinstance(season_number, bool)
            and season_number > 0
        ):
            return False
    total_episodes = provider.get("number_of_episodes")
    # Absence is not a positive statement. A sparse/transient provider body
    # without either count or seasons used to be accepted as authoritative zero.
    return (
        isinstance(total_episodes, int)
        and not isinstance(total_episodes, bool)
        and total_episodes == 0
    )


def _detail_snapshot_is_complete(payload: dict, media_type: str) -> bool:
    provider = payload.get("_provider_detail")
    if isinstance(provider, dict):
        required_blocks = (
            "credits",
            "external_ids",
            "images",
            "videos",
            "content_ratings" if media_type == "tv" else "release_dates",
        )
        # Appended TMDB endpoints can fail independently inside an HTTP-200
        # base response. Explicit failure means unresolved hero data, whereas
        # a successful empty block legitimately means no cast/logo/rating/etc.
        if provider.get("success") is False or any(
            isinstance(provider.get(key), dict)
            and provider[key].get("success") is False
            for key in required_blocks
        ):
            return False
    return _detail_episode_index_is_complete(payload, media_type)


def _detail_episode_index_is_complete(payload: dict, media_type: str) -> bool:
    if media_type != "tv":
        return True
    if payload.get("episodes_complete") is not True:
        return False
    # Every expected regular season must be represented. One populated season
    # cannot turn another season's empty HTTP-200 response into a complete index.
    seasons = payload.get("seasons") or []
    actual: dict[int, int] = {}
    expected: dict[int, int] = {}
    for season in seasons:
        if season.get("episodes_complete") is False:
            return False
        number = season.get("season", season.get("season_number"))
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            continue
        actual[number] = len(
            {
                episode.get("episode", episode.get("episode_number"))
                for episode in season.get("episodes") or []
                if isinstance(
                    episode.get("episode", episode.get("episode_number")), int
                )
                and not isinstance(
                    episode.get("episode", episode.get("episode_number")), bool
                )
                and episode.get("episode", episode.get("episode_number")) > 0
                and episode.get("video_id")
                and episode.get("season", episode.get("season_number")) == number
            }
        )
        count = season.get("episode_count")
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            expected[number] = count
    provider = payload.get("_provider_detail") or {}
    for season in provider.get("seasons") or []:
        number = season.get("season_number")
        count = season.get("episode_count")
        if isinstance(number, int) and not isinstance(number, bool) and number > 0:
            if number not in actual:
                return False
            if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                expected[number] = max(expected.get(number, 0), count)
    if any(actual.get(number, 0) < count for number, count in expected.items()):
        return False
    if _detail_usable_episode_count(payload) > 0:
        # Legacy snapshots did not always persist episode_count, but a stored
        # playable episode is direct evidence that the snapshot is usable.
        return True
    expected_count = _detail_expected_regular_episode_count(payload)
    if expected_count > 0:
        return False

    # A provider-complete not-yet-premiered series can legitimately have no
    # regular episodes -- but only the provider can say so.  Treating the mere
    # presence of ``_provider_detail`` as that confirmation froze episode-less
    # payloads permanently: the build wrote them, and this same function then
    # accepted them on every read, so the "rebuild it once" path never ran.
    if _provider_confirms_no_regular_episodes(payload):
        return True
    # A future date says when a title airs, not whether this empty index is
    # authoritative. Sparse provider bodies and old snapshots must rebuild.
    return False


def _detail_hero_projection(payload: dict) -> dict:
    """A complete hero and lightweight episode index, never a full-page cache entry."""
    keys = (
        "item",
        "ratings",
        "content_rating",
        "content_advisory",
        "trailers",
        "episodes_complete",
        "_provider_detail",
        "_detail_source",
    )
    hero = {key: payload[key] for key in keys if key in payload}
    hero["cast"] = [
        {"name": member.get("name")} for member in (payload.get("cast") or [])[:3]
    ]
    crew = payload.get("crew") or {}
    hero["crew"] = {"directors": (crew.get("directors") or [])[:2]}
    hero["seasons"] = [
        {
            **{key: value for key, value in season.items() if key != "poster"},
            "episodes": [
                {key: value for key, value in episode.items() if key != "still"}
                for episode in season.get("episodes") or []
            ],
        }
        for season in payload.get("seasons") or []
    ]
    media_type = str((payload.get("item") or {}).get("media_type") or "movie")
    hero["hero_complete"] = _detail_snapshot_is_complete(payload, media_type)
    hero["details_complete"] = False
    return hero


def _load_detail_hero_snapshot(
    db: Session, tmdb_id: int, media_type: str
) -> dict | None:
    """Read a durable core without similarity queries, overlay composition or writes."""
    row = db.get(DetailSnapshot, (tmdb_id, media_type))
    if (
        row is None
        or row.schema_version != _DETAIL_SNAPSHOT_SCHEMA_VERSION
        or not row.episodes_complete
        or not isinstance(row.payload, dict)
        or not _detail_snapshot_is_complete(row.payload, media_type)
    ):
        return None
    freshness = (
        _DETAIL_TV_FRESH_SECONDS if media_type == "tv" else _DETAIL_MOVIE_FRESH_SECONDS
    )
    stale = (utc_now() - row.refreshed_at).total_seconds() >= freshness
    return {**row.payload, "_detail_source": "stale" if stale else "hit"}


async def _discovery_detail_core_payload(
    tmdb_id: int,
    media_type: Literal["movie", "tv"],
    *,
    background: bool,
    force_refresh: bool,
    hero: bool,
) -> dict:
    """Serve the fast view from complete snapshots or shared provider-only work."""
    hero_key = f"detail-hero:{media_type}:{tmdb_id}"
    if not force_refresh:
        keys = [f"web-detail:{media_type}:{tmdb_id}"]
        if hero:
            keys.append(hero_key)
        keys.append(f"detail-core:{media_type}:{tmdb_id}")
        for key in keys:
            cached = _meta_cache.get(key)
            if cached and time() - cached[0] < _META_CACHE_TTL:
                payload = cast(dict, cached[1])
                if _detail_snapshot_is_complete(payload, media_type):
                    return _detail_hero_projection(payload) if hero else payload
        snapshot = await _detail_db_call(
            _load_detail_hero_snapshot, tmdb_id, media_type
        )
        if snapshot is not None:
            if snapshot.get("_detail_source") == "stale":
                _schedule_detail_snapshot_refresh(tmdb_id, media_type)
            payload = _detail_hero_projection(snapshot) if hero else snapshot
            if hero:
                _meta_cache[hero_key] = (time(), payload)
            else:
                # The core view is reused by later requests; without it every
                # request for a title cold in memory re-decoded the snapshot.
                _meta_cache[f"detail-core:{media_type}:{tmdb_id}"] = (
                    time(),
                    {**snapshot, "_detail_source": "memory"},
                )
            _meta_cache_evict()
            return payload
    core = await _singleflight_detail_core(tmdb_id, media_type, background=background)
    if force_refresh and _detail_snapshot_is_complete(core, media_type):
        # A Retry can refresh the hero before the full page is requested. Keep
        # that fresh core authoritative over the old durable full snapshot until
        # the following full build has incorporated it.
        core = {**core, "_full_refresh_pending": True}
        _meta_cache[f"detail-core:{media_type}:{tmdb_id}"] = (time(), core)
    payload = _detail_hero_projection(core) if hero else core
    if hero and payload.get("hero_complete"):
        _meta_cache[hero_key] = (time(), payload)
        _meta_cache_evict()
    return payload


def _compute_detail_dynamic_overlay(
    db: Session, payload: dict, tmdb_id: int, media_type: str
) -> dict:
    """Calculate current Curatio fields without copying large episode bodies."""
    from app.relevance import similar_by_vector

    metadata = (
        db.query(MediaMetadata)
        .filter(
            MediaMetadata.tmdb_id == tmdb_id,
            MediaMetadata.media_type == media_type,
        )
        .first()
    )
    overlay = {
        "tags": _title_tags(db, tmdb_id, media_type),
        "ratings": _title_ratings(db, tmdb_id, media_type),
    }
    tag_similar = _items_from_ids(
        db,
        [tid for tid, _ in similar_by_vector(db, tmdb_id, media_type, limit=18)],
        media_type,
    )
    if tag_similar:
        overlay["similar"] = tag_similar
        overlay["similar_source"] = "tags"
    elif not payload.get("similar"):
        item = payload.get("item")
        catalog_similar = _catalog_similar_fallback(
            db,
            tmdb_id,
            media_type,
            target_genres=item.get("genres") if isinstance(item, dict) else None,
        )
        if catalog_similar:
            overlay["similar"] = catalog_similar
            overlay["similar_source"] = "catalog"

    provider_detail = payload.get("_provider_detail")
    if isinstance(provider_detail, dict):
        reviews, review_summary = _title_reviews(metadata, provider_detail)
        extras = _title_extras(metadata, provider_detail, media_type)
        overlay["reviews"] = reviews
        overlay["review_summary"] = review_summary
        overlay.update(extras)
        item = payload.get("item")
        if isinstance(item, dict):
            item_overlay = {
                "content_rating": extras.get("content_rating"),
                "comingSoonDate": extras.get("comingSoonDate"),
            }
            if media_type == "movie" and (
                release_block := _dated_movie_release_block(provider_detail)
            ):
                from app.tmdb_client import tmdb_client

                item_overlay["digitalReleaseInfo"] = tmdb_client.digital_release_date(
                    release_block
                )
            overlay["_item"] = item_overlay
    return overlay


def _refresh_persisted_detail_dynamic_overlay(
    db: Session, tmdb_id: int, media_type: Literal["movie", "tv"]
) -> bool:
    """Refresh a snapshot's dynamic overlay for future requests and restarts."""
    snapshot = db.get(DetailSnapshot, (tmdb_id, media_type))
    if (
        snapshot is None
        or not isinstance(snapshot.payload, dict)
        or not _detail_snapshot_is_complete(snapshot.payload, media_type)
    ):
        return False

    computed = _compute_detail_dynamic_overlay(
        db, snapshot.payload, tmdb_id, media_type
    )
    refreshed = _apply_detail_dynamic_overlay(snapshot.payload, computed)
    snapshot.payload = refreshed
    snapshot.overlay_refreshed_at = utc_now()
    db.commit()
    _detail_response_cache_store(
        tmdb_id, media_type, _detail_dynamic_overlay(refreshed)
    )
    return True


def _load_detail_snapshot(
    db: Session, tmdb_id: int, media_type: Literal["movie", "tv"]
) -> tuple[dict | None, bool]:
    """Read a usable snapshot as ``(payload, is_stale)``; ``(None, False)`` if unusable."""
    snapshot = db.get(DetailSnapshot, (tmdb_id, media_type))
    if (
        snapshot is None
        or snapshot.schema_version != _DETAIL_SNAPSHOT_SCHEMA_VERSION
        or not snapshot.episodes_complete
        or not isinstance(snapshot.payload, dict)
        or snapshot.payload.get("details_complete") is False
    ):
        return None, False

    # Quarantine rows written before the completeness rule was tightened. An
    # episode-less TV snapshot is only legitimate when the provider says the show
    # has no regular episodes yet; anything else is a failed build that was frozen
    # as authoritative. Clearing the flag here is the migration -- rows heal on
    # next access, and the read below falls through to a real rebuild.
    if (
        media_type == "tv"
        and _detail_usable_episode_count(snapshot.payload) == 0
        and not _provider_confirms_no_regular_episodes(snapshot.payload)
    ):
        logger.warning(
            f"detail_snapshot quarantined tmdb_id={tmdb_id} media_type=tv "
            f"reason=zero_usable_episodes "
            f"expected={_detail_expected_regular_episode_count(snapshot.payload)}"
        )
        snapshot.episodes_complete = False
        db.commit()
        return None, False

    if not _detail_snapshot_is_complete(snapshot.payload, media_type):
        return None, False

    payload = cast(dict, snapshot.payload)
    changed = False
    if not payload.get("similar"):
        item = payload.get("item")
        catalog_similar = _catalog_similar_fallback(
            db,
            tmdb_id,
            media_type,
            target_genres=item.get("genres") if isinstance(item, dict) else None,
        )
        if catalog_similar:
            payload = copy.deepcopy(payload)
            payload["similar"] = catalog_similar
            payload["similar_source"] = "catalog"
            snapshot.payload = payload
            changed = True

    now = utc_now()
    freshness = (
        _DETAIL_TV_FRESH_SECONDS if media_type == "tv" else _DETAIL_MOVIE_FRESH_SECONDS
    )
    stale = max(0.0, (now - snapshot.refreshed_at).total_seconds()) >= freshness
    overlay_refreshed_at = snapshot.overlay_refreshed_at
    overlay_is_fresh = (
        overlay_refreshed_at is not None
        and max(0.0, (now - overlay_refreshed_at).total_seconds())
        < _DETAIL_DYNAMIC_OVERLAY_TTL_SECONDS
    )
    if overlay_is_fresh:
        # Seed the compact process cache from durable freshness. This is the
        # restart path that avoids re-running vector similarity for every card.
        _detail_response_cache_store(
            tmdb_id, media_type, _detail_dynamic_overlay(payload)
        )
    if (
        now - snapshot.last_accessed_at
    ).total_seconds() >= _DETAIL_SNAPSHOT_ACCESS_WRITE_SECONDS:
        snapshot.last_accessed_at = now
        changed = True
    if changed:
        db.commit()
    return payload, stale


def _persist_detail_snapshot(
    db: Session, tmdb_id: int, media_type: str, payload: dict
) -> bool:
    """Persist only complete payloads; a partial TV refresh never wins."""
    # Canonical identity enrichment belongs to this short persistence
    # transaction. Do it even when an incomplete TV payload is rejected, which
    # preserves the old build-session behavior without retaining that session
    # during the provider request.
    provider_detail = payload.get("_provider_detail") or {}
    metadata = db.get(MediaMetadata, (tmdb_id, media_type))
    if metadata is not None and isinstance(provider_detail, dict):
        external_ids = provider_detail.get("external_ids") or {}
        provider_imdb_id = (
            external_ids.get("imdb_id") if isinstance(external_ids, dict) else None
        )
        if provider_imdb_id and not metadata.imdb_id:
            metadata.imdb_id = provider_imdb_id
        provider_logo_path = _logo_path_from_detail(provider_detail)
        if provider_logo_path and not metadata.logo_path:
            metadata.logo_path = provider_logo_path
    if payload.get("details_complete") is False:
        return False
    usable = _detail_usable_episode_count(payload) if media_type == "tv" else 0
    if not _detail_snapshot_is_complete(payload, media_type):
        logger.info(
            f"detail_snapshot_write tmdb_id={tmdb_id} media_type={media_type} "
            f"written=False reason=incomplete usable={usable} "
            f"expected={_detail_expected_regular_episode_count(payload)}"
        )
        return False
    # Independent floor. `_detail_snapshot_is_complete` is also the read-side
    # gate, so a future loosening there would silently re-open the door to
    # freezing an episode-less series. This check answers only one question and
    # never delegates it.
    if (
        media_type == "tv"
        and usable == 0
        and not _provider_confirms_no_regular_episodes(payload)
    ):
        logger.warning(
            f"detail_snapshot_write tmdb_id={tmdb_id} media_type=tv written=False "
            f"reason=zero_usable_episodes "
            f"expected={_detail_expected_regular_episode_count(payload)}"
        )
        return False
    now = utc_now()
    row = db.get(DetailSnapshot, (tmdb_id, media_type))
    if row is None:
        row = DetailSnapshot(tmdb_id=tmdb_id, media_type=media_type, payload=payload)
        db.add(row)
    else:
        row.payload = payload
    row.episodes_complete = True
    row.schema_version = _DETAIL_SNAPSHOT_SCHEMA_VERSION
    row.refreshed_at = now
    row.overlay_refreshed_at = now
    row.last_accessed_at = now
    db.commit()
    logger.info(
        f"detail_snapshot_write tmdb_id={tmdb_id} media_type={media_type} "
        f"written=True usable={usable}"
    )
    # The provider build already calculated current Curatio facts. Seed the
    # ready-to-serve overlay instead of discarding it and calculating it again on
    # the response path.
    _detail_response_cache_store(tmdb_id, media_type, _detail_dynamic_overlay(payload))
    return True


async def _detail_db_call(fn, *args):
    """Run one synchronous detail query off the loop, on a session the thread owns.

    A cancelled request closes its own session as it unwinds, but a thread that has
    already started cannot be cancelled with it — so a request-scoped session must
    never be handed to one.
    """

    def run():
        with get_db() as db:
            return fn(db, *args)

    return await asyncio.to_thread(run)


def _schedule_detail_dynamic_refresh(
    tmdb_id: int, media_type: Literal["movie", "tv"]
) -> None:
    """Refresh one cold dynamic overlay without delaying the current response."""
    key = (media_type, tmdb_id)
    tasks = _loop_task_map(_detail_dynamic_refresh_tasks)
    if (existing := tasks.get(key)) is not None and not existing.done():
        return

    async def refresh() -> None:
        started_at = monotonic()
        try:
            async with _detail_dynamic_refresh_semaphore():
                updated = await _detail_db_call(
                    _refresh_persisted_detail_dynamic_overlay,
                    tmdb_id,
                    media_type,
                )
            logger.info(
                "detail_overlay source=background "
                f"media_type={media_type} tmdb_id={tmdb_id} "
                f"updated={updated} duration_ms={(monotonic() - started_at) * 1000:.1f}"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                f"Detail dynamic overlay refresh failed for {media_type}/{tmdb_id}: {exc}"
            )

    task = asyncio.create_task(refresh())
    tasks[key] = task

    def remove(finished: asyncio.Task) -> None:
        if tasks.get(key) is finished:
            tasks.pop(key, None)

    task.add_done_callback(remove)


async def _singleflight_detail_build(
    tmdb_id: int, media_type: Literal["movie", "tv"], *, background: bool
) -> dict:
    """Collapse overlapping single and batch builds onto one provider request."""
    key = (media_type, tmdb_id)
    tasks = _loop_task_map(_detail_build_tasks)
    flight = tasks.get(key)
    if flight is None:
        promotion_event = asyncio.Event()
        if not background:
            promotion_event.set()

        async def build_and_persist() -> dict:
            # Provider requests may take seconds. Read facts and persist the
            # result in separate worker-owned sessions so the flight never pins
            # a pooled connection while awaiting TMDB.
            built = await _build_discovery_details_payload(
                tmdb_id,
                media_type,
                background=background,
                promotion_event=promotion_event,
            )
            built.pop("_full_refresh_pending", None)
            written = await _detail_db_call(
                _persist_detail_snapshot, tmdb_id, media_type, built
            )
            core_key = f"detail-core:{media_type}:{tmdb_id}"
            cached_core = _meta_cache.get(core_key)
            if written and cached_core:
                core = cast(dict, cached_core[1])
                if core.get("_provider_detail") is built.get("_provider_detail"):
                    _meta_cache[core_key] = (
                        cached_core[0],
                        {k: v for k, v in core.items() if k != "_full_refresh_pending"},
                    )
            return built

        task = asyncio.create_task(build_and_persist())
        flight = (task, promotion_event)
        tasks[key] = flight

        def remove(finished: asyncio.Task):
            if tasks.get(key) is flight:
                tasks.pop(key, None)

        task.add_done_callback(remove)
    else:
        task, promotion_event = flight
        if not background:
            promotion_event.set()
    return await asyncio.shield(task)


async def _singleflight_detail_core(
    tmdb_id: int,
    media_type: Literal["movie", "tv"],
    *,
    background: bool,
    promotion_event: asyncio.Event | None = None,
) -> dict:
    """Share base metadata and season work across hero, full and legacy requests."""
    cache_key = f"detail-core:{media_type}:{tmdb_id}"
    cached = _meta_cache.get(cache_key)
    if cached and time() - cached[0] < _META_CACHE_TTL:
        return cast(dict, cached[1])
    key = (media_type, tmdb_id)
    tasks = _loop_task_map(_detail_core_tasks)
    flight = tasks.get(key)
    if flight is None:
        promoted = asyncio.Event()

        async def build() -> dict:
            payload = await _build_detail_core_payload(
                tmdb_id, media_type, background=background, promotion_event=promoted
            )
            if _detail_snapshot_is_complete(payload, media_type):
                _meta_cache[cache_key] = (time(), payload)
                _meta_cache_evict()
            return payload

        task = asyncio.create_task(build())
        flight = (task, promoted)
        tasks[key] = flight

        def remove(finished: asyncio.Task) -> None:
            if tasks.get(key) is flight:
                tasks.pop(key, None)
            if not finished.cancelled():
                finished.exception()  # A disconnected shielded caller still warms the cache.

        task.add_done_callback(remove)
    task, promoted = flight
    if not background or (promotion_event is not None and promotion_event.is_set()):
        promoted.set()

    async def relay_promotion() -> None:
        await promotion_event.wait()
        promoted.set()

    relay = (
        asyncio.create_task(relay_promotion())
        if promotion_event is not None and not promoted.is_set()
        else None
    )
    try:
        return await asyncio.shield(task)
    finally:
        if relay is not None:
            relay.cancel()
            await asyncio.gather(relay, return_exceptions=True)


def _schedule_detail_snapshot_refresh(
    tmdb_id: int, media_type: Literal["movie", "tv"]
) -> None:
    """Refresh stale snapshots off the response path."""
    key = (media_type, tmdb_id)
    tasks = _loop_task_map(_detail_refresh_tasks)
    if (existing := tasks.get(key)) is not None and not existing.done():
        return

    async def refresh() -> None:
        try:
            payload = await _singleflight_detail_build(
                tmdb_id, media_type, background=True
            )
            if _detail_snapshot_is_complete(payload, media_type):
                cache_key = f"web-detail:{media_type}:{tmdb_id}"
                _meta_cache[cache_key] = (time(), payload)
                _meta_cache_evict()
        except Exception as exc:
            logger.warning(
                f"Detail snapshot refresh failed for {media_type}/{tmdb_id}: {exc}"
            )

    task = asyncio.create_task(refresh())
    tasks[key] = task

    def remove(finished: asyncio.Task):
        if tasks.get(key) is finished:
            tasks.pop(key, None)

    task.add_done_callback(remove)


def purge_expired_detail_snapshots(db: Session, *, now: datetime | None = None) -> int:
    cutoff = (now or utc_now()) - timedelta(days=_DETAIL_SNAPSHOT_RETENTION_DAYS)
    deleted = (
        db.query(DetailSnapshot)
        .filter(DetailSnapshot.last_accessed_at < cutoff)
        .delete(synchronize_session=False)
    )
    db.commit()
    return int(deleted)


async def _discovery_details_payload(
    tmdb_id: int,
    media_type: Literal["movie", "tv"] | None,
    imdb_id: str | None,
    *,
    background: bool = False,
    force_refresh: bool = False,
    refresh_dynamic_overlay: bool = True,
    view: Literal["full", "hero", "core"] = "full",
) -> tuple[dict, str]:
    """Load complete details from memory, persistent snapshot, or one shared build."""
    started_at = time()
    if tmdb_id <= 0:
        norm_imdb = _normalize_imdb_id(imdb_id)
        if not norm_imdb:
            raise HTTPException(
                status_code=400, detail="A tmdb_id or imdb_id is required"
            )
        async with _detail_tmdb_slot(background=background):
            resolved = await _resolve_imdb_to_tmdb(norm_imdb)
            if (
                media_type is not None
                and resolved
                and resolved.get("media_type") != media_type
            ):
                # One IMDb id, two TMDB records: the caller is showing the other
                # one. Without this a series card opened a movie stub.
                resolved = (
                    await _resolve_imdb_to_tmdb_as(norm_imdb, media_type)
                ) or resolved
        if not resolved or not resolved.get("tmdb_id"):
            raise HTTPException(status_code=404, detail="Title not found")
        tmdb_id = int(resolved["tmdb_id"])
        media_type = cast(Literal["movie", "tv"], resolved["media_type"])
        identity_id = norm_imdb
    elif media_type is None:
        raise HTTPException(status_code=400, detail="media_type is required")
    else:
        requested_type = media_type
        media_type, repair_reason = await _detail_media_type_repair(
            tmdb_id, requested_type
        )
        if media_type != requested_type:
            logger.warning(
                f"detail identity repaired tmdb_id={tmdb_id} "
                f"requested={requested_type} resolved={media_type} "
                f"reason={repair_reason}"
            )
        identity_id = f"tmdb:{tmdb_id}"

    resolved_type = cast(Literal["movie", "tv"], media_type)
    cache_key = f"web-detail:{resolved_type}:{tmdb_id}"
    now_seconds = time()
    force_refresh = force_refresh and _claim_detail_force_refresh(
        tmdb_id, resolved_type
    )
    if force_refresh:
        # Explicit Retry must cross every server-side read cache. The completed
        # provider build below atomically replaces the persistent snapshot only
        # when it passes the same completeness validation as an ordinary build.
        _meta_cache.pop(cache_key, None)
        _meta_cache.pop(f"detail-core:{resolved_type}:{tmdb_id}", None)
        _meta_cache.pop(f"detail-hero:{resolved_type}:{tmdb_id}", None)
        _meta_cache.pop(f"meta:{resolved_type}:{tmdb_id}", None)
        _detail_response_cache_remove(tmdb_id, resolved_type)
    if view != "full":
        payload = await _discovery_detail_core_payload(
            tmdb_id,
            resolved_type,
            background=background,
            force_refresh=force_refresh,
            hero=view == "hero",
        )
        return payload, identity_id

    core = _meta_cache.get(f"detail-core:{resolved_type}:{tmdb_id}")
    if core and now_seconds - core[0] < _META_CACHE_TTL:
        force_refresh = force_refresh or bool(core[1].get("_full_refresh_pending"))
    cached = _meta_cache.get(cache_key)
    if not force_refresh and cached and now_seconds - cached[0] < _META_CACHE_TTL:
        refreshed, overlay_was_cold = _detail_payload_from_cached_overlay(
            cast(dict, cached[1]), tmdb_id, resolved_type
        )
        if overlay_was_cold and refresh_dynamic_overlay:
            _schedule_detail_dynamic_refresh(tmdb_id, resolved_type)
        logger.info(
            f"detail_snapshot source=memory media_type={resolved_type} "
            f"tmdb_id={tmdb_id} duration_ms={(time() - started_at) * 1000:.1f}"
        )
        # `_` prefix: stripped from the wire by `_details_content`, read only by
        # `_detail_episode_stats` for the diagnostic response header.
        refreshed["_detail_source"] = "memory"
        return refreshed, identity_id

    payload, stale = (None, False)
    if not force_refresh:
        payload, stale = await _detail_db_call(
            _load_detail_snapshot, tmdb_id, resolved_type
        )
    if payload is not None:
        _meta_cache[cache_key] = (now_seconds, payload)
        _meta_cache_evict()
        if stale:
            _schedule_detail_snapshot_refresh(tmdb_id, resolved_type)
        refreshed, overlay_was_cold = _detail_payload_from_cached_overlay(
            payload, tmdb_id, resolved_type
        )
        if overlay_was_cold and refresh_dynamic_overlay:
            _schedule_detail_dynamic_refresh(tmdb_id, resolved_type)
        logger.info(
            f"detail_snapshot source={'stale' if stale else 'hit'} "
            f"media_type={resolved_type} tmdb_id={tmdb_id} "
            f"duration_ms={(time() - started_at) * 1000:.1f}"
        )
        refreshed["_detail_source"] = "stale" if stale else "hit"
        return refreshed, identity_id

    payload = await _singleflight_detail_build(
        tmdb_id, resolved_type, background=background
    )
    built_complete = _detail_snapshot_is_complete(payload, resolved_type)
    if built_complete:
        _meta_cache[cache_key] = (now_seconds, payload)
        _meta_cache_evict()
    elif force_refresh:
        # This request spent the cooldown and got nothing usable. Hand it back so
        # the user's next Retry is a real rebuild rather than a cached read.
        _release_detail_force_refresh(tmdb_id, resolved_type)
    refreshed, overlay_was_cold = _detail_payload_from_cached_overlay(
        payload, tmdb_id, resolved_type
    )
    if overlay_was_cold and built_complete and refresh_dynamic_overlay:
        _schedule_detail_dynamic_refresh(tmdb_id, resolved_type)
    logger.info(
        f"detail_snapshot source={'build' if built_complete else 'partial'} "
        f"media_type={resolved_type} tmdb_id={tmdb_id} "
        f"duration_ms={(time() - started_at) * 1000:.1f}"
    )
    refreshed["_detail_source"] = "build" if built_complete else "partial"
    return refreshed, identity_id


def _stored_detail_media_type_with_reason(
    db: Session,
    tmdb_id: int,
    requested_type: Literal["movie", "tv"],
) -> tuple[Literal["movie", "tv"], str]:
    """Repair a stale typed TMDB request from already-persisted identity facts.

    TMDB movie and TV ids are separate namespaces, but old cards can retain the
    wrong namespace. This performs no provider request.

    The repair is deliberately **one-directional**: it may upgrade a ``movie``
    request to ``tv``, never the reverse. Downgrading a series to a movie deletes
    the entire episode list -- ``_build_discovery_details_payload`` skips its whole
    seasons block, and ``_details_item_meta`` emits a single movie video -- and
    the result then validates as a complete movie snapshot and is cached for
    30 minutes. Nothing available here is strong enough to justify that, because
    a colliding movie id and a cold TV cache are both entirely ordinary.

    Returns ``(media_type, reason)``; the reason names the branch that decided.
    """
    rows = (
        db.query(MediaMetadata)
        .filter(
            MediaMetadata.tmdb_id == tmdb_id,
            MediaMetadata.media_type.in_(["movie", "tv"]),
        )
        .all()
    )
    by_type = {str(row.media_type): row for row in rows}
    requested_row = by_type.get(requested_type)
    if requested_row is not None:
        # A stored row of the requested type IS the positive evidence for that
        # namespace, so a `tv` request that has one is already answered.
        if requested_type == "tv":
            return "tv", "requested_row"
        resolved_type = None
        if requested_row.imdb_id:
            # Scoped to this tmdb_id: `imdb_id` is the table's primary key and
            # `tmdb_id` is nullable, so an unscoped lookup could return a
            # resolution for a different work — and `_search_row_media_type`
            # short-circuits on it, outranking every positive TV signal.
            resolved_type = (
                db.query(ImdbResolution.media_type)
                .filter(
                    ImdbResolution.imdb_id == requested_row.imdb_id,
                    ImdbResolution.tmdb_id == tmdb_id,
                    ImdbResolution.status == "found",
                    ImdbResolution.media_type.in_(["movie", "tv"]),
                )
                .limit(1)
                .scalar()
            )
        canonical = _search_row_media_type(requested_row, resolved_type)
        if canonical == "tv":
            return "tv", (
                "imdb_resolution" if resolved_type == "tv" else "row_evidence"
            )
        return requested_type, "requested_row"

    # Only a movie request may borrow the opposite namespace. For a `tv` request,
    # "no stored TV row" is the normal cold-cache state for a series whose details
    # have never been built, and TMDB's id spaces are disjoint — a movie row with
    # the same integer says nothing about it.
    if requested_type == "movie" and by_type.get("tv") is not None:
        return "tv", "opposite_namespace"
    return requested_type, "requested"


async def _detail_media_type_repair(
    tmdb_id: int, requested_type: Literal["movie", "tv"]
) -> tuple[Literal["movie", "tv"], str]:
    """Memoized :func:`_stored_detail_media_type_with_reason` for the request path.

    Every details/meta request runs this before any memory-cache lookup, so an
    unmemoized repair put a full-row MediaMetadata read (raw_data included) in
    front of every cache hit. Kept in ``_meta_cache`` so the same TTL and the
    same invalidation apply.
    """
    key = f"detail-type:{requested_type}:{tmdb_id}"
    cached = _meta_cache.get(key)
    if cached and time() - cached[0] < _META_CACHE_TTL:
        return cast(tuple[Literal["movie", "tv"], str], cached[1])
    result = await _detail_db_call(
        _stored_detail_media_type_with_reason, tmdb_id, requested_type
    )
    _meta_cache[key] = (time(), result)
    _meta_cache_evict()
    return result


def _stored_detail_media_type(
    db: Session,
    tmdb_id: int,
    requested_type: Literal["movie", "tv"],
) -> Literal["movie", "tv"]:
    return _stored_detail_media_type_with_reason(db, tmdb_id, requested_type)[0]


def _detail_build_facts(
    db: Session, tmdb_id: int, media_type: Literal["movie", "tv"]
) -> tuple[SimpleNamespace | None, list, list, list, list]:
    """Read every Curatio-side fact a build needs, in one synchronous pass."""
    from app.relevance import similar_by_vector

    metadata = (
        db.query(MediaMetadata)
        .filter(
            MediaMetadata.tmdb_id == tmdb_id,
            MediaMetadata.media_type == media_type,
        )
        .first()
    )
    # "More Like This" from our own tag vectors, with TMDB's /similar as the
    # fallback for titles we have not tagged. Computed before the TMDB call so
    # a tagged title costs one request instead of two -- /similar was awaited
    # serially, so this is a latency win as well as a quality one.
    tag_similar = _items_from_ids(
        db,
        [tid for tid, _ in similar_by_vector(db, tmdb_id, media_type, limit=18)],
        media_type,
    )
    catalog_similar = (
        []
        if tag_similar
        else _catalog_similar_fallback(
            db,
            tmdb_id,
            media_type,
            target_genres=metadata.genres if metadata is not None else None,
        )
    )
    # Never return an ORM instance from a worker-owned session. Tests commonly
    # commit on context exit (which expires it), and production callers must not
    # be able to lazy-load after the connection has been returned to the pool.
    metadata_snapshot = (
        SimpleNamespace(
            genres=metadata.genres,
            mdb_facts=metadata.mdb_facts,
            tag_facts=metadata.tag_facts,
            next_episode_air_date=metadata.next_episode_air_date,
            digital_release_date=metadata.digital_release_date,
        )
        if metadata is not None
        else None
    )
    return (
        metadata_snapshot,
        tag_similar,
        catalog_similar,
        _title_tags(db, tmdb_id, media_type),
        _title_ratings(db, tmdb_id, media_type),
    )


def _detail_hero_facts(
    db: Session, tmdb_id: int, media_type: str
) -> tuple[SimpleNamespace | None, list]:
    """Only certification, release dates and scores; never vector/rail work."""
    metadata = (
        db.query(MediaMetadata)
        .options(
            load_only(
                MediaMetadata.mdb_facts,
                MediaMetadata.tag_facts,
                MediaMetadata.next_episode_air_date,
                MediaMetadata.digital_release_date,
            )
        )
        .filter(
            MediaMetadata.tmdb_id == tmdb_id, MediaMetadata.media_type == media_type
        )
        .first()
    )
    snapshot = (
        SimpleNamespace(
            mdb_facts=metadata.mdb_facts,
            tag_facts=metadata.tag_facts,
            next_episode_air_date=metadata.next_episode_air_date,
            digital_release_date=metadata.digital_release_date,
        )
        if metadata is not None
        else None
    )
    return snapshot, _title_ratings(db, tmdb_id, media_type)


async def _build_detail_core_payload(
    tmdb_id: int,
    media_type: Literal["movie", "tv"],
    *,
    background: bool,
    promotion_event: asyncio.Event | None = None,
) -> dict:
    """Resolve hero facts and episodes without awaiting any lower-page enrichment."""
    from app.tmdb_client import TMDBServerError, tmdb_client

    metadata, ratings_payload = await _detail_db_call(
        _detail_hero_facts, tmdb_id, media_type
    )

    try:
        async with _detail_tmdb_slot(
            background=background, promotion_event=promotion_event
        ):
            detail = (
                await tmdb_client.get_movie(tmdb_id)
                if media_type == "movie"
                else await tmdb_client.get_tv_show(tmdb_id)
            )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Title not found") from exc
        logger.warning(f"Web detail fetch failed for {media_type}/{tmdb_id}: {exc}")
        raise HTTPException(status_code=502, detail="Could not load details") from exc
    except Exception as exc:
        logger.warning(f"Web detail fetch failed for {media_type}/{tmdb_id}: {exc}")
        raise HTTPException(status_code=502, detail="Could not load details") from exc

    cast_members = []
    for person in (detail.get("credits") or {}).get("cast", [])[:18]:
        name = person.get("name")
        if not name:
            continue
        cast_members.append(
            {
                "name": name,
                "character": person.get("character") or "",
                "profile": _image_url(person.get("profile_path"), "w185"),
            }
        )

    crew = (detail.get("credits") or {}).get("crew", [])
    creators = [p.get("name") for p in detail.get("created_by", []) if p.get("name")]
    directors = [
        p.get("name") for p in crew if p.get("job") == "Director" and p.get("name")
    ]
    writers = list(
        dict.fromkeys(
            p.get("name")
            for p in crew
            if p.get("department") == "Writing" and p.get("name")
        )
    )[:5]

    seasons_payload = []
    episodes_complete = True
    if media_type == "tv":
        regular_seasons = [
            s for s in detail.get("seasons", []) if s.get("season_number", 0) > 0
        ]
        season_sem = asyncio.Semaphore(4)

        async def fetch_season(season_number: int):
            async with season_sem:
                for attempt in range(2):
                    try:
                        async with _detail_tmdb_slot(
                            background=background, promotion_event=promotion_event
                        ):
                            return await tmdb_client.get_tv_season(
                                tmdb_id, season_number
                            )
                    except TMDBServerError as exc:
                        if attempt == 0:
                            logger.warning(
                                f"Retrying season detail after TMDB 5xx for "
                                f"tv/{tmdb_id}/s{season_number}: {exc}"
                            )
                            continue
                        logger.warning(
                            f"Season detail fetch failed for "
                            f"tv/{tmdb_id}/s{season_number}: {exc}"
                        )
                    except Exception as exc:
                        logger.warning(
                            f"Season detail fetch failed for "
                            f"tv/{tmdb_id}/s{season_number}: {exc}"
                        )
                    return None
                return None

        season_details = await asyncio.gather(
            *[fetch_season(int(s["season_number"])) for s in regular_seasons]
        )
        # Transport success is not the same as getting episodes. `all([])` is
        # True, so a show whose provider body carried no regular seasons used to
        # be declared complete with an empty list -- and TMDB also returns the
        # occasional 200 whose `episodes` array is empty. Require at least one
        # real episode whenever regular seasons were expected at all.
        episodes_complete = all(data is not None for data in season_details) and (
            not regular_seasons
            or any((data or {}).get("episodes") for data in season_details)
        )
        for season_summary, season_data in zip(regular_seasons, season_details):
            episodes = []
            if season_data:
                for ep in season_data.get("episodes", []):
                    season_number = ep.get("season_number") or season_summary.get(
                        "season_number"
                    )
                    episode_number = ep.get("episode_number")
                    if (
                        not isinstance(season_number, int)
                        or isinstance(season_number, bool)
                        or season_number <= 0
                        or season_number != season_summary["season_number"]
                        or not isinstance(episode_number, int)
                        or isinstance(episode_number, bool)
                        or episode_number <= 0
                    ):
                        logger.warning(
                            f"Skipping malformed episode identity for "
                            f"tv/{tmdb_id}: season={season_number!r}, "
                            f"episode={episode_number!r}"
                        )
                        continue
                    episodes.append(
                        {
                            "season": season_number,
                            "episode": episode_number,
                            "title": ep.get("name") or f"Episode {episode_number}",
                            "overview": ep.get("overview") or "",
                            "air_date": ep.get("air_date") or "",
                            "still": _image_url(ep.get("still_path"), "w300"),
                            "tmdb_id": tmdb_id,
                            "media_type": "tv",
                            "video_id": f"tmdb:{tmdb_id}:{season_number}:{episode_number}",
                            "content_type": "series",
                        }
                    )
            seasons_payload.append(
                {
                    "season": season_summary.get("season_number"),
                    "title": season_summary.get("name")
                    or f"Season {season_summary.get('season_number')}",
                    "overview": season_summary.get("overview") or "",
                    "poster": _image_url(season_summary.get("poster_path"), "w300"),
                    "episode_count": season_summary.get("episode_count")
                    or len(episodes),
                    "episodes_complete": season_data is not None
                    and (len(episodes) >= (season_summary.get("episode_count") or 0)),
                    "episodes": episodes,
                }
            )

    extras = _title_extras(metadata, detail, media_type)
    item_payload = _tmdb_detail_item(detail, media_type)
    item_payload["content_rating"] = extras["content_rating"]
    if extras.get("comingSoonDate"):
        item_payload["comingSoonDate"] = extras["comingSoonDate"]

    payload = {
        # Hidden from the wire by `_details_content`; retained so snapshot hits can
        # re-compose current MDBList reviews/certification and Curatio facts without
        # another provider call.
        "_provider_detail": detail,
        "item": item_payload,
        "cast": cast_members,
        "crew": {"creators": creators, "directors": directors, "writers": writers},
        "seasons": seasons_payload,
        **({"episodes_complete": episodes_complete} if media_type == "tv" else {}),
        "ratings": ratings_payload,
        "details_complete": False,
        **extras,
    }
    if media_type == "tv" and not _detail_episode_index_is_complete(
        payload, media_type
    ):
        # TMDB can occasionally return a successful season envelope whose episode
        # list is empty. Treat that exactly like a failed season request so neither
        # HTTP clients nor the persistent snapshot cache can freeze the empty body.
        payload["episodes_complete"] = False
        for season in payload["seasons"]:
            if season.get("episode_count", 0) > 0 and not season.get("episodes"):
                season["episodes_complete"] = False
    if media_type == "tv":
        # The single line that explains an empty episode list. `regular_seasons=0`
        # is the fingerprint of a provider body with no seasons array at all --
        # previously indistinguishable in the logs from a healthy build.
        logger.info(
            f"detail_episodes tmdb_id={tmdb_id} "
            f"provider_seasons={len(detail.get('seasons') or [])} "
            f"regular_seasons={len(regular_seasons)} "
            f"season_ok={sum(1 for d in season_details if d is not None)} "
            f"season_fail={sum(1 for d in season_details if d is None)} "
            f"expected={_detail_expected_regular_episode_count(payload)} "
            f"usable={_detail_usable_episode_count(payload)} "
            f"complete={payload.get('episodes_complete')}"
        )
    payload["hero_complete"] = _detail_snapshot_is_complete(payload, media_type)
    return payload


async def _build_discovery_details_payload(
    tmdb_id: int,
    media_type: Literal["movie", "tv"],
    *,
    background: bool,
    promotion_event: asyncio.Event | None = None,
) -> dict:
    """Enrich the shared core only when a caller requests the full page."""
    from app.tmdb_client import tmdb_client

    core = await _singleflight_detail_core(
        tmdb_id, media_type, background=background, promotion_event=promotion_event
    )
    (
        metadata,
        tag_similar,
        catalog_similar,
        tags_payload,
        ratings_payload,
    ) = await _detail_db_call(_detail_build_facts, tmdb_id, media_type)
    detail = core["_provider_detail"]
    # Similar titles are enrichment, not title existence. A failure here should not
    # turn an otherwise valid detail response into a 404/502.
    similar_data = {}
    if not tag_similar:
        try:
            async with _detail_tmdb_slot(
                background=background, promotion_event=promotion_event
            ):
                similar_data = (
                    await tmdb_client.get_similar_movies(tmdb_id)
                    if media_type == "movie"
                    else await tmdb_client.get_similar_tv_shows(tmdb_id)
                )
        except Exception as exc:
            logger.warning(
                f"Similar-title fetch failed for {media_type}/{tmdb_id}: {exc}"
            )

    tmdb_similar = [
        _tmdb_result_item(result, media_type)
        for result in similar_data.get("results", [])[:18]
        if result.get("id")
    ]
    similar = tag_similar or tmdb_similar or catalog_similar
    reviews, review_summary = _title_reviews(metadata, detail)
    return {
        **core,
        "cast": core.get("cast") or [],
        "similar": similar,
        "similar_source": (
            "tags" if tag_similar else "tmdb" if tmdb_similar else "catalog"
        ),
        "tags": tags_payload,
        "ratings": ratings_payload,
        "reviews": reviews,
        "review_summary": review_summary,
        "details_complete": bool(core.get("hero_complete")),
    }


@app.get("/account/api/segments/{video_id}")
async def account_api_segments(
    request: Request, video_id: str, db: Session = Depends(get_db_dependency)
):
    """Cookie-authenticated wrapper for existing intro/credits segment markers."""
    parts = video_id.split(":")

    def build(session: Session):
        _active_profile_or_403(session, request)
        if len(parts) < 3:
            raise HTTPException(status_code=400, detail="invalid video_id")
        try:
            imdb_id, season, episode = parts[0], int(parts[1]), int(parts[2])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid video_id") from exc
        return _build_segments_response(session, video_id, imdb_id, season, episode)

    return await _request_db_call(db, build)


def _get_playback_profile(
    request: Request, db: Session, profile_id: int | None
) -> User:
    """Bind playback to its initiating profile, even if another tab switches."""
    if profile_id is None:
        return _get_active_profile(request, db)
    account = _get_account_from_session(request, db)
    profile = (
        db.query(User)
        .filter(User.id == profile_id, User.account_id == account.id)
        .first()
    )
    if not profile or not profile.is_active:
        raise HTTPException(403, "Playback profile is unavailable")
    return profile


@app.post("/account/api/playback/presence")
def account_playback_presence(
    request: Request, payload: PresenceReport, db: Session = Depends(get_db_dependency)
):
    profile = _get_playback_profile(request, db, payload.profile_id)
    if not profile.is_active:
        raise HTTPException(403, "Account is deactivated")
    return playback_activity.record_presence(db, profile, payload)


@app.post("/api/v2/playback/presence")
def native_playback_presence(
    user_key: BearerUserKey,
    payload: PresenceReport,
    db: Session = Depends(get_db_dependency),
):
    profile = _require_active_user_by_key(user_key, db)
    if payload.profile_id is not None and payload.profile_id != profile.id:
        raise HTTPException(403, "Playback profile is unavailable")
    return playback_activity.record_presence(db, profile, payload)


class PlaybackSourcesRequest(BaseModel):
    """v2 playback lookup (see docs/playback-api.md)."""

    type: Literal["movie", "series"]
    imdb_id: Optional[str] = Field(default=None, max_length=20)
    tmdb_id: Optional[int] = None
    season: Optional[int] = Field(default=None, ge=0, le=10000)
    episode: Optional[int] = Field(default=None, ge=0, le=100000)
    quality: Optional[Literal["original", "optimized"]] = None
    refresh: bool = False
    viewing_session_id: UUID | None = None
    title: Optional[str] = Field(default=None, max_length=500)
    profile_id: int | None = None
    # An unmatched server video ("<provider>:<item_key>", from the Library
    # tab's Other Videos). Takes precedence over the IMDb/TMDB identity.
    library_item: Optional[str] = Field(
        default=None, max_length=200, pattern=r"^[a-z0-9_-]{1,32}:[^\s]{1,160}$"
    )
    # What the device can take, so Plex/Jellyfin size a transcode to it
    # (PlaybackHints). All optional; older clients send none.
    max_bitrate_kbps: Optional[int] = Field(default=None, ge=500, le=400_000)
    max_resolution: Optional[
        Literal["2160p", "1440p", "1080p", "720p", "576p", "480p"]
    ] = None
    audio_channels: Optional[int] = Field(default=None, ge=1, le=16)

    def hints(self) -> PlaybackHints:
        return PlaybackHints(
            max_bitrate_kbps=self.max_bitrate_kbps,
            max_resolution=self.max_resolution,
            audio_channels=self.audio_channels,
        )

    def media_ref(self) -> MediaRef:
        try:
            return MediaRef(
                type=self.type,
                imdb_id=self.imdb_id or None,
                tmdb_id=self.tmdb_id,
                season=self.season if self.type == "series" else None,
                episode=self.episode if self.type == "series" else None,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_media", "message": str(exc)},
            )


def _profile_quality(user: User) -> str:
    """The profile's saved playback quality (stored in ``bandwidth_tier``)."""
    return "optimized" if getattr(user, "bandwidth_tier", None) == "low" else "original"


def _media_title(db: Session, media: MediaRef) -> Optional[str]:
    query = db.query(MediaMetadata.title).filter(
        MediaMetadata.media_type == media.tmdb_media_type
    )
    if media.imdb_id:
        row = query.filter(MediaMetadata.imdb_id == media.imdb_id).first()
    else:
        row = query.filter(MediaMetadata.tmdb_id == media.tmdb_id).first()
    return row[0] if row else None


async def _tracked_sources(
    media: MediaRef,
    *,
    user_key: str,
    quality: str,
    title: Optional[str],
    session_id=None,
    refresh: bool = False,
    hints: Optional[PlaybackHints] = None,
) -> dict:
    """Resolve sources with activity telemetry; telemetry never blocks playback."""
    request_id = None
    ctx = PlaybackContext(
        user_key=user_key,
        quality=quality,  # type: ignore[arg-type]
        refresh=refresh,
        viewing_session_id=str(session_id) if session_id else None,
        title=title,
        hints=hints or PlaybackHints(),
    )
    try:
        request_id = await _db_call(
            playback_activity.begin_request,
            user_key=user_key,
            media_id=media.key(),
            media_type=media.type,
            tier=quality,
            title=title,
            session_id=session_id,
        )
    except Exception:
        logger.warning("Source request activity could not be recorded")
    sources: list = []
    failure = None
    try:
        sources = await source_resolver.resolve(media, ctx)
    except source_resolver.ProvidersUnavailable:
        failure = "providers_unavailable"
        raise HTTPException(
            status_code=502,
            detail={
                "code": "providers_unavailable",
                "message": "No playback source could be reached. Try again shortly.",
            },
        )
    except BaseException:
        failure = "request_interrupted"
        raise
    finally:
        if request_id:
            try:
                await _db_call(
                    playback_activity.finish_request,
                    request_id,
                    count=len(sources),
                    diagnostics=ctx.diagnostics,
                    error=failure,
                )
            except Exception:
                logger.warning("Source request activity could not be completed")

    def availability(session: Session) -> dict:
        index = AvailabilityIndex.build(session, [(media.imdb_id, media.tmdb_id)])
        return index.lookup(media.type, media.imdb_id, media.tmdb_id).model_dump()

    return {
        "quality": quality,
        "availability": await _db_call(availability),
        "sources": [source.model_dump() for source in sources],
    }


async def _library_item_sources(
    library_item: str,
    *,
    user_key: str,
    quality: str,
    hints: Optional[PlaybackHints] = None,
) -> dict:
    """Sources for an unmatched server video, played by its item key."""
    ctx = PlaybackContext(user_key=user_key, quality=quality, hints=hints or PlaybackHints())  # type: ignore[arg-type]
    sources = await source_resolver.resolve_library_item(library_item, ctx)
    provider_id = library_item.split(":", 1)[0]
    return {
        "quality": quality,
        "availability": {
            "playable": bool(sources),
            "in_library": bool(sources),
            "sources": (
                [
                    {
                        "provider_id": provider_id,
                        "label": sources[0].provider_label,
                        "kind": "library",
                    }
                ]
                if sources
                else []
            ),
        },
        "sources": [source.model_dump() for source in sources],
    }


@app.post("/account/api/playback/sources")
async def account_playback_sources(
    request: Request,
    payload: PlaybackSourcesRequest,
    db: Session = Depends(get_db_dependency),
):
    if payload.library_item:

        def authorize_item(session: Session) -> tuple[str, str]:
            profile = _get_playback_profile(request, session, payload.profile_id)
            return profile.user_key, _profile_quality(profile)

        profile_key, default_quality = await _request_db_call(db, authorize_item)
        result = await _library_item_sources(
            payload.library_item,
            user_key=profile_key,
            quality=payload.quality or default_quality,
            hints=payload.hints(),
        )
        return JSONResponse(content=result, headers={"Cache-Control": "no-store"})
    media = payload.media_ref()

    def authorize(session: Session) -> tuple[str, str, Optional[str]]:
        profile = _get_playback_profile(request, session, payload.profile_id)
        return profile.user_key, _profile_quality(profile), _media_title(session, media)

    profile_key, default_quality, title = await _request_db_call(db, authorize)
    result = await _tracked_sources(
        media,
        user_key=profile_key,
        quality=payload.quality or default_quality,
        title=payload.title or title,
        session_id=payload.viewing_session_id,
        refresh=payload.refresh,
        hints=payload.hints(),
    )
    return JSONResponse(content=result, headers={"Cache-Control": "no-store"})


@app.post("/account/api/playback/failure")
async def account_playback_failure(request: Request, payload: FailureReport):
    """Cookie-authenticated provider-health signal from the web player."""

    def check() -> None:
        with get_db() as session:
            profile = _get_active_profile(request, session)
            if not profile.is_active:
                raise HTTPException(status_code=403, detail="Account is deactivated")

    await asyncio.to_thread(check)
    await source_resolver.report_failure(payload)
    return {"status": "ok"}


class WebLibraryAddRequest(BaseModel):
    tmdb_id: Optional[int] = None
    imdb_id: Optional[str] = None  # 'tt1234567'
    media_type: Literal["movie", "tv"]
    title: Optional[str] = None
    poster_path: Optional[str] = None
    year: Optional[str] = None


# --- Same-origin media byte proxy -------------------------------------------
#
# Some stream hosts (debrid/stream proxies) return a malformed/duplicated CORS header
# (`Access-Control-Allow-Origin` with more than one origin), which browsers
# reject — so the in-browser remux/transcode `fetch()` of the media bytes is
# blocked outright. Safari additionally refuses a native <video> source unless
# byte-range requests are answered with 206. This thin same-origin passthrough
# streams the upstream bytes from Curatio's own origin with the Range
# request/response relayed verbatim. It is a byte pipe — NOT a transcoder.

_PROXY_CHUNK = settings.playback_proxy_chunk_bytes
_PROXY_PASSTHROUGH_HEADERS = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "content-encoding",
    "last-modified",
    "etag",
)

# One shared, pooled upstream client for ALL proxy traffic. Browsers open many
# parallel Range connections per stream, so a fresh AsyncClient per request (the
# old behaviour) meant no connection reuse and no global cap on upstream sockets.
# The finite read timeout means a stalled origin is torn down instead of hanging
# the task forever while holding sockets.
_proxy_client: httpx.AsyncClient | None = None


def _own_hosts() -> tuple[str, ...]:
    """Curatio's own host, exempt from the public-address rule (signed /media/)."""
    host = urlparse(settings.base_url).hostname
    return (host,) if host else ()


def _proxy_http_client() -> httpx.AsyncClient:
    """Return the pooled proxy client, recreating it after an app restart."""
    global _proxy_client
    if _proxy_client is None or _proxy_client.is_closed:
        limits = httpx.Limits(max_connections=128, max_keepalive_connections=32)
        _proxy_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0),
            follow_redirects=False,
            limits=limits,
            transport=public_only_transport(allowed_hosts=_own_hosts(), limits=limits),
        )
    return _proxy_client


# Admission control: cap concurrent proxy streams and shed excess with a fast 503
# rather than accumulating unbounded upstream connections/FDs. The event loop is
# single-threaded, so the read-modify-write of this counter is race-free.
_PROXY_MAX_CONCURRENT_STREAMS = settings.playback_proxy_max_streams
_active_proxy_streams = 0
# ffmpeg reconnects and ffprobe seeks open a few loopback reads per session;
# the fingerprint worker's background reads use the same pool (+4 for those).
_PROXY_MAX_INTERNAL_STREAMS = settings.playback_remux_max_streams * 4 + 12
_active_internal_proxy_streams = 0

# A browser playing through the proxy issues a Range request every few
# seconds, and a prepared HLS session fetches a segment every few seconds.
# Resolving the profile for each one held a DB session per request; a
# short-lived per-cookie verdict (the profile id) avoids that. Signing out or
# deactivating takes effect for playback within the TTL.
_PROXY_AUTH_TTL_SECONDS = 60.0
_PROXY_AUTH_CACHE_MAX = 1024
_proxy_auth_cache: dict[str, tuple[float, Optional[int]]] = {}


async def _cached_active_profile_id(request: Request) -> Optional[int]:
    """The active profile id for this cookie, validated at most once per TTL."""
    token = request.cookies.get("user_session")
    now = monotonic()
    if token:
        hit = _proxy_auth_cache.get(token)
        if hit is not None and hit[0] > now:
            return hit[1]

    def check() -> Optional[int]:
        # Short-lived session released BEFORE streaming: a dependency would
        # hold the DB connection for the full movie.
        with get_db() as db:
            user = _get_active_profile(request, db)
            if not user.is_active:
                raise HTTPException(status_code=403, detail="Account is deactivated")
            return getattr(user, "id", None)

    profile_id = await asyncio.to_thread(check)
    if not token:
        return profile_id
    if len(_proxy_auth_cache) >= _PROXY_AUTH_CACHE_MAX:
        for key, value in list(_proxy_auth_cache.items()):
            if value[0] <= now:
                _proxy_auth_cache.pop(key, None)
        if len(_proxy_auth_cache) >= _PROXY_AUTH_CACHE_MAX:
            _proxy_auth_cache.clear()
    _proxy_auth_cache[token] = (now + _PROXY_AUTH_TTL_SECONDS, profile_id)
    return profile_id


async def _authorize_proxy_request(request: Request) -> None:
    await _cached_active_profile_id(request)


def _is_own_media_url(url: str) -> bool:
    """A signed ``/media/`` URL this server minted for its own Plex/Jellyfin.

    These point at Curatio itself (often a LAN or loopback address on a
    self-hosted install), so the public-host SSRF rule would wrongly refuse them.
    Only the exact configured origin plus the ``/media/`` prefix qualifies.
    """
    return url.startswith(f"{settings.base_url}/media/")


async def _is_safe_source_url_async(url: str) -> bool:
    """SSRF check offloaded off the event loop (it does a blocking DNS lookup)."""
    if _is_own_media_url(url):
        return True
    return await asyncio.get_running_loop().run_in_executor(
        None, is_safe_source_url, url
    )


# Follow redirects manually so the SSRF guard runs on EVERY hop. httpx's built-in
# follow_redirects would fetch a redirect target without re-checking it, letting a
# public URL bounce the server into internal / cloud-metadata endpoints.
_MAX_SOURCE_REDIRECTS = 5


class _UnsafeRedirect(Exception):
    """A source URL, or one of its redirect hops, is not a safe public target."""


async def _send_with_validated_redirects(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: dict,
    *,
    max_redirects: int = _MAX_SOURCE_REDIRECTS,
) -> httpx.Response:
    """Send ``method url`` following redirects manually, re-validating every hop.

    The client MUST be built with ``follow_redirects=False``. Returns the final
    streamed response (the caller must close it); raises ``_UnsafeRedirect`` if any
    hop fails the SSRF check or the redirect chain exceeds ``max_redirects``.
    """
    current = url
    for _ in range(max_redirects + 1):
        if not await _is_safe_source_url_async(current):
            raise _UnsafeRedirect(current)
        request = client.build_request(method, current, headers=headers)
        resp = await client.send(request, stream=True)
        if resp.is_redirect and "location" in resp.headers:
            location = str(resp.url.join(resp.headers["location"]))
            await resp.aclose()
            current = location
            continue
        return resp
    raise _UnsafeRedirect("too many redirects")


# Remote source URLs often redirect through a resolver (often several
# hops, each SSRF-checked) before reaching the CDN. A browser issues a new Range
# request every few seconds and ffmpeg reconnects on every seek, so walking the
# chain each time costs latency and resolver rate limit. Remember where each
# source URL ended up for a few minutes, keyed by the source URL.
_RESOLVED_URL_TTL_SECONDS = 300.0
_RESOLVED_URL_CACHE_MAX = 512
_resolved_url_cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()


def _cached_resolved_url(url: str) -> Optional[str]:
    entry = _resolved_url_cache.get(url)
    if entry is None:
        return None
    if entry[0] <= monotonic():
        _resolved_url_cache.pop(url, None)
        return None
    _resolved_url_cache.move_to_end(url)
    return entry[1]


def _remember_resolved_url(url: str, final: str) -> None:
    _resolved_url_cache[url] = (monotonic() + _RESOLVED_URL_TTL_SECONDS, final)
    _resolved_url_cache.move_to_end(url)
    while len(_resolved_url_cache) > _RESOLVED_URL_CACHE_MAX:
        _resolved_url_cache.popitem(last=False)


def _forget_resolved_url(url: str) -> None:
    _resolved_url_cache.pop(url, None)


# An expired signed/resolver link typically answers 200 with an HTML error page
# or a JSON error object instead of media bytes.
_ERROR_BODY_TYPES = frozenset(
    {"text/html", "application/xhtml+xml", "application/json", "text/json"}
)


def _upstream_error_body_type(resp) -> Optional[str]:
    """The content type when a 2xx upstream answer is an error page, not media."""
    if not 200 <= int(getattr(resp, "status_code", 0) or 0) < 300:
        return None
    raw = str((getattr(resp, "headers", None) or {}).get("content-type") or "")
    ctype = raw.split(";", 1)[0].strip().lower()
    if ctype in _ERROR_BODY_TYPES or ctype.endswith("+json"):
        return ctype
    return None


#: Media types the byte proxy relays as-is. Anything else is served as an opaque
#: download so a hostile upstream can never get HTML/SVG/XML rendered (and its
#: script run) on Curatio's own origin.
_PROXY_MEDIA_TYPE_PREFIXES = ("video/", "audio/")
_PROXY_MEDIA_TYPES = frozenset(
    {
        "application/octet-stream",
        "application/mp4",
        "application/vnd.apple.mpegurl",
        "application/x-mpegurl",
        "application/x-matroska",
        "application/x-subrip",
        "text/vtt",
        "text/plain",
    }
)
#: HLS playlists name further URLs that FFmpeg would open itself, outside the
#: proxy's SSRF checks, so the internal remux path never relays them.
_PLAYLIST_TYPES = frozenset(
    {"application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl"}
)
#: Headers set on every proxied byte response, error or not.
_PROXY_SAFETY_HEADERS = {
    "x-content-type-options": "nosniff",
    "content-security-policy": "sandbox; default-src 'none'",
}


def _bare_content_type(raw: str | None) -> str:
    return str(raw or "").split(";", 1)[0].strip().lower()


def _safe_proxy_content_type(raw: str | None) -> str:
    """The upstream content type when it is media, else an opaque download type."""
    ctype = _bare_content_type(raw)
    if ctype in _PROXY_MEDIA_TYPES or ctype.startswith(_PROXY_MEDIA_TYPE_PREFIXES):
        return str(raw).strip()
    return "application/octet-stream"


def _source_expired(ctype: str) -> HTTPException:
    return HTTPException(
        status_code=410,
        detail=f"Source link expired: upstream returned {ctype} instead of media",
        headers={"X-Curatio-Error": "source-expired", "Cache-Control": "no-store"},
    )


async def _send_source_request(method: str, url: str, headers: dict) -> httpx.Response:
    """Fetch a source, going straight to its cached final URL when known.

    A cached hop that now fails (expired CDN link, error page) is dropped and
    the full validated redirect chain from the source URL is walked again.
    """
    client = _proxy_http_client()
    cached = _cached_resolved_url(url)
    if cached and cached != url:
        resp = None
        try:
            resp = await _send_with_validated_redirects(client, method, cached, headers)
        except (_UnsafeRedirect, httpx.HTTPError):
            resp = None
        if resp is not None:
            if (
                resp.status_code < 400 or resp.status_code == 416
            ) and not _upstream_error_body_type(resp):
                return resp
            await resp.aclose()
        _forget_resolved_url(url)
    resp = await _send_with_validated_redirects(client, method, url, headers)
    final = getattr(resp, "url", None)
    final_url = str(final) if final is not None else url
    if (
        final_url != url
        and resp.status_code < 400
        and not _upstream_error_body_type(resp)
    ):
        _remember_resolved_url(url, final_url)
    return resp


_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)
#: Fresh Range requests made to finish one proxied body after the upstream drops.
_PROXY_RESUME_ATTEMPTS = 3


def _body_byte_span(resp) -> Optional[tuple[int, Optional[int]]]:
    """``(first_byte, last_byte_or_None)`` of a 200/206 body, when resumable."""
    headers = getattr(resp, "headers", None) or {}
    if headers.get("content-encoding"):
        return None  # raw bytes are encoded; offsets don't map to the file
    if resp.status_code == 206:
        match = _CONTENT_RANGE_RE.fullmatch(
            str(headers.get("content-range") or "").strip()
        )
        if not match:
            return None
        return int(match.group(1)), int(match.group(2))
    if resp.status_code == 200:
        length = str(headers.get("content-length") or "")
        return 0, (int(length) - 1 if length.isdigit() else None)
    return None


class PlaybackRemuxStartRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=8192)
    title: Optional[str] = Field(None, max_length=512)
    start: float = Field(0.0, ge=0, le=604800)
    audio_language: str = Field("system", max_length=32)
    # The viewer picked a specific audio track (ffprobe stream index from the
    # inspect facts); wins over ``audio_language``.
    audio_index: Optional[int] = Field(None, ge=0, le=999)
    # What the browser told us it can decode (MediaCapabilities). HEVC is
    # copied only when the browser can play it; otherwise video is transcoded
    # to H.264 only when the player explicitly allows that last resort.
    allow_hevc: bool = False
    allow_transcode: bool = False
    surround: bool = False


class PlaybackInspectRequest(BaseModel):
    urls: list[str] = Field(default_factory=list, max_length=8)


class PlaybackRemuxStopRequest(BaseModel):
    session: str = Field(..., min_length=1, max_length=128)


def _internal_proxy_access(request: Request, source_url: str) -> tuple[str, str]:
    """Build the loopback URL and URL-bound token used by FFmpeg/ffprobe."""
    server = request.scope.get("server")
    try:
        port = int(server[1]) if server else 8000
    except (TypeError, ValueError, IndexError):
        port = 8000
    token = create_proxy_token(
        source_url,
        secret_key=settings.secret_key,
        ttl_seconds=remux_manager.max_lifetime_seconds + 300,
    )
    query = urlencode({"url": source_url})
    return f"http://127.0.0.1:{port}/account/api/playback/proxy?{query}", token


@app.post("/account/api/playback/remux/start")
async def account_playback_remux_start(
    request: Request,
    payload: PlaybackRemuxStartRequest,
    db: Session = Depends(get_db_dependency),
):
    """Start a bounded H.264/AAC EVENT-HLS browser compatibility session."""
    user = _get_active_profile(request, db)
    source_url = (payload.url or "").strip()
    if not await _is_safe_source_url_async(source_url):
        raise HTTPException(status_code=400, detail="unsafe or invalid source url")
    proxy_url, proxy_token = _internal_proxy_access(request, source_url)
    try:
        session = await remux_manager.start(
            source_url,
            proxy_url,
            proxy_token=proxy_token,
            profile_id=user.id,
            start_offset=max(0.0, float(payload.start or 0.0)),
            audio_language=(payload.audio_language or "system")[:32],
            audio_index=payload.audio_index,
            allow_hevc=bool(payload.allow_hevc),
            allow_transcode=bool(payload.allow_transcode),
            surround=bool(payload.surround),
        )
    except RemuxCapacityError as exc:
        raise HTTPException(
            status_code=429, detail=str(exc), headers={"Retry-After": "3"}
        ) from exc
    except UnsupportedRemuxVideoError as exc:
        hint = getattr(exc, "hint", None)
        # With a hint the detail is structured so the client can say where the
        # source will play ("desktop_app"); plain refusals keep a string.
        detail = {"message": str(exc), "hint": hint} if hint else str(exc)
        raise HTTPException(status_code=422, detail=detail) from exc
    except RemuxPrepareError as exc:
        status = 504 if "timed out" in str(exc).lower() else 502
        raise HTTPException(status_code=status, detail=str(exc)) from exc

    base = f"/account/api/playback/remux/{session.session_id}"
    media_start = (
        session.media_start if session.media_start is not None else session.start_offset
    )
    return {
        "playlist_url": f"{base}/index.m3u8",
        "session": session.session_id,
        "duration": session.duration,
        # Movie time of the session's first frame (the keyframe at or before
        # the requested start); local player time 0 maps to this.
        "start": media_start,
        "requested_start": session.start_offset,
        "mode": "transcode" if session.video_mode == "transcode" else "remux",
        "video_mode": session.video_mode,
        # Which of the file's audio tracks is in this session (for the Audio menu).
        "audio_index": session.audio_index,
        "keepalive_url": f"{base}/keepalive",
        "subtitles": [
            {
                "index": track.stream_index,
                "language": track.language,
                "title": track.title,
                "forced": track.forced,
                "default": track.default,
                "url": f"{base}/sub_{track.stream_index}.vtt",
            }
            for track in session.subtitles
        ],
    }


@app.post("/account/api/playback/remux/{session_id}/keepalive")
async def account_playback_remux_keepalive(
    session_id: str,
    request: Request,
    db: Session = Depends(get_db_dependency),
):
    """Renew a prepared session's lease while its player is open or paused."""
    profile_id = await _request_db_call(
        db, lambda session: _get_active_profile(request, session).id
    )
    if not remux_manager.keepalive(session_id, profile_id=profile_id):
        raise HTTPException(status_code=404, detail="Playback session ended")
    return {"ok": True}


@app.post("/account/api/playback/remux/stop")
async def account_playback_remux_stop(
    request: Request,
    payload: PlaybackRemuxStopRequest,
    db: Session = Depends(get_db_dependency),
):
    """Stop an owned compatibility session; missing sessions are idempotent."""
    user = _get_active_profile(request, db)
    await remux_manager.stop((payload.session or "").strip(), profile_id=user.id)
    return {"ok": True}


@app.get("/account/api/playback/remux/{session_id}/{filename}")
async def account_playback_remux_asset(
    session_id: str,
    filename: str,
    request: Request,
):
    """Serve one profile-owned HLS playlist or completed MPEG-TS segment."""
    # Fetched once per segment for the whole of a remuxed playback: the cookie
    # is validated once per TTL, not with a DB lookup per segment.
    profile_id = await _cached_active_profile_id(request)
    if profile_id is None:
        raise HTTPException(status_code=404, detail="Playback asset not found")
    try:
        path = remux_manager.file_path(session_id, filename, profile_id=profile_id)
    except RemuxSessionFailed as exc:
        # FFmpeg died: tell the player now instead of letting it poll a playlist
        # that will never grow.
        raise HTTPException(
            status_code=502,
            detail=str(exc),
            headers={"X-Curatio-Error": "session-failed", "Cache-Control": "no-store"},
        ) from exc
    if path is None:
        raise HTTPException(status_code=404, detail="Playback asset not found")
    if filename == "index.m3u8":
        media_type = "application/vnd.apple.mpegurl"
    elif filename.endswith(".vtt"):
        media_type = "text/vtt; charset=utf-8"
    else:
        media_type = "video/mp4"
    if filename == "index.m3u8" and request.query_params.get("hold") == "1":
        # hls.js: long-poll for the next segment (see RemuxManager.playlist_text).
        # Native HLS players (Safari) omit ?hold and get the playlist as written.
        text = await remux_manager.playlist_text(session_id, path)
        return Response(
            content=text,
            media_type=media_type,
            headers={"Cache-Control": "private, no-store"},
        )
    if filename.endswith(".m4s"):
        # Media segments: align audio/video for browsers that ignore edit lists.
        content = await asyncio.to_thread(
            remux_manager.aligned_segment, session_id, path
        )
        return Response(
            content=content,
            media_type=media_type,
            headers={"Cache-Control": "private, no-store"},
        )
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "private, no-store"},
    )


@app.api_route("/account/api/playback/proxy", methods=["GET", "HEAD"])
async def account_playback_proxy(
    request: Request,
    url: str = Query(..., min_length=1),
    remux_token: Optional[str] = Query(None),
):
    """Range-aware, same-origin byte proxy for browser playback.

    Lets the in-browser remux/transcode and native <video> read media bytes from
    Curatio's own origin, sidestepping broken upstream CORS and guaranteeing
    Safari-friendly 206 range responses. No transcoding — bytes pass through.
    """
    # Browser requests authenticate with their normal profile cookie. FFmpeg's
    # loopback requests use a short-lived token scoped to this exact source URL,
    # avoiding a user session secret in the subprocess command line.
    header_token = request.headers.get("x-curatio-remux-token")
    supplied_remux_token = remux_token or header_token
    if remux_token and header_token and remux_token != header_token:
        raise HTTPException(status_code=401, detail="conflicting playback proxy token")
    internal_remux_request = bool(supplied_remux_token) and verify_proxy_token(
        url,
        supplied_remux_token or "",
        secret_key=settings.secret_key,
    )
    if supplied_remux_token and not internal_remux_request:
        raise HTTPException(status_code=401, detail="invalid playback proxy token")
    if not internal_remux_request:
        await _authorize_proxy_request(request)
    if not await _is_safe_source_url_async(url):
        raise HTTPException(status_code=400, detail="unsafe or invalid source url")

    # Browser reads and Curatio's own ffmpeg/ffprobe reads draw from separate
    # pools, so a busy conversion can't starve a viewer's direct playback (or
    # the other way round).
    global _active_proxy_streams, _active_internal_proxy_streams
    if internal_remux_request:
        if _active_internal_proxy_streams >= _PROXY_MAX_INTERNAL_STREAMS:
            raise HTTPException(
                status_code=503, detail="proxy at capacity, retry shortly"
            )
        _active_internal_proxy_streams += 1
    else:
        if _active_proxy_streams >= _PROXY_MAX_CONCURRENT_STREAMS:
            raise HTTPException(
                status_code=503, detail="proxy at capacity, retry shortly"
            )
        _active_proxy_streams += 1
    slot_released = False

    def _release_slot() -> None:
        nonlocal slot_released
        global _active_proxy_streams, _active_internal_proxy_streams
        if not slot_released:
            slot_released = True
            if internal_remux_request:
                _active_internal_proxy_streams -= 1
            else:
                _active_proxy_streams -= 1

    try:
        upstream_headers: dict[str, str] = {"User-Agent": "Mozilla/5.0"}
        range_header = request.headers.get("range")
        if range_header:
            upstream_headers["Range"] = range_header

        try:
            upstream = await _send_source_request(request.method, url, upstream_headers)
        except _UnsafeRedirect as exc:
            _release_slot()
            raise HTTPException(
                status_code=400, detail="unsafe or invalid source url"
            ) from exc
        except httpx.HTTPError as exc:
            _release_slot()
            raise HTTPException(
                status_code=502, detail="upstream fetch failed"
            ) from exc

        error_body = _upstream_error_body_type(upstream)
        if error_body:
            await upstream.aclose()
            _forget_resolved_url(url)
            _release_slot()
            raise _source_expired(error_body)

        if internal_remux_request and (
            _bare_content_type(upstream.headers.get("content-type")) in _PLAYLIST_TYPES
        ):
            await upstream.aclose()
            _release_slot()
            raise HTTPException(
                status_code=415, detail="playlists cannot be remuxed via the proxy"
            )

        # Upstream error pages are never relayed: their body is attacker-chosen
        # content that would otherwise render on this origin.
        # (HEAD 405/501 is handled below by a ranged-GET fallback.)
        if upstream.status_code >= 400 and not (
            request.method == "HEAD" and upstream.status_code in (405, 501)
        ):
            status_code = upstream.status_code
            error_headers = {"cache-control": "no-store", **_PROXY_SAFETY_HEADERS}
            if "content-range" in upstream.headers:  # 416 tells the size
                error_headers["content-range"] = upstream.headers["content-range"]
            await upstream.aclose()
            _release_slot()
            return Response(status_code=status_code, headers=error_headers)

        resp_headers = {
            key: upstream.headers[key]
            for key in _PROXY_PASSTHROUGH_HEADERS
            if key in upstream.headers
        }
        resp_headers["content-type"] = _safe_proxy_content_type(
            upstream.headers.get("content-type")
        )
        resp_headers.update(_PROXY_SAFETY_HEADERS)
        resp_headers.setdefault("accept-ranges", "bytes")
        resp_headers["cache-control"] = "no-store"
        # Tell any nginx/ingress in front not to spool this response: media must
        # stream through, and buffering would both delay first byte and hold the
        # proxy slot while nginx slurps gigabytes it may never deliver.
        resp_headers["x-accel-buffering"] = "no"

        # HEAD: relay status + headers, no body. Some stream proxies
        # reject HEAD outright with 405/501 while serving range GETs fine, so
        # synthesize the HEAD answer from a 1-byte range GET in that case —
        # otherwise a size-probing client sees the 405 and gives up on a
        # perfectly streamable source.
        if request.method == "HEAD":
            status_code = upstream.status_code
            await upstream.aclose()
            if status_code in (405, 501):
                fallback_headers = dict(upstream_headers)
                fallback_headers["Range"] = "bytes=0-0"
                try:
                    probe = await _send_source_request("GET", url, fallback_headers)
                except (_UnsafeRedirect, httpx.HTTPError):
                    probe = None
                if probe is not None:
                    try:
                        if probe.status_code == 206:
                            total = (
                                probe.headers.get("content-range", "")
                                .rpartition("/")[2]
                                .strip()
                            )
                            resp_headers = {
                                key: probe.headers[key]
                                for key in _PROXY_PASSTHROUGH_HEADERS
                                if key in probe.headers
                            }
                            resp_headers.pop("content-range", None)
                            resp_headers["content-type"] = _safe_proxy_content_type(
                                probe.headers.get("content-type")
                            )
                            resp_headers.update(_PROXY_SAFETY_HEADERS)
                            if total.isdigit():
                                resp_headers["content-length"] = total
                            else:
                                resp_headers.pop("content-length", None)
                            resp_headers["accept-ranges"] = "bytes"
                            resp_headers["cache-control"] = "no-store"
                            resp_headers["x-accel-buffering"] = "no"
                            status_code = 200
                    finally:
                        await probe.aclose()
            _release_slot()
            return Response(status_code=status_code, headers=resp_headers)

        span = _body_byte_span(upstream)

        async def body_stream():
            # Remote CDNs drop long-lived connections mid-body. Instead of
            # ending the response early (a stall/error in the player, a
            # reconnect in ffmpeg), continue with a fresh Range request from
            # the next byte this response still owes.
            current = upstream
            sent = 0
            resumes = 0
            try:
                while True:
                    try:
                        async for chunk in current.aiter_raw(_PROXY_CHUNK):
                            sent += len(chunk)
                            yield chunk
                        return
                    except httpx.TransportError as exc:
                        if span is None or resumes >= _PROXY_RESUME_ATTEMPTS:
                            raise
                        first, last = span
                        next_byte = first + sent
                        if last is not None and next_byte > last:
                            return
                        resumes += 1
                        logger.info(
                            "playback_proxy_resume byte={} attempt={} error={}",
                            next_byte,
                            resumes,
                            type(exc).__name__,
                        )
                        try:
                            await current.aclose()
                        except Exception:  # noqa: BLE001 - already broken
                            pass
                        resume_headers = dict(upstream_headers)
                        resume_headers["Range"] = (
                            f"bytes={next_byte}-{last if last is not None else ''}"
                        )
                        await asyncio.sleep(0.25 * resumes)
                        try:
                            nxt = await _send_source_request("GET", url, resume_headers)
                        except (_UnsafeRedirect, httpx.HTTPError):
                            raise exc
                        resumed = _body_byte_span(nxt)
                        if (
                            nxt.status_code != 206
                            or not resumed
                            or resumed[0] != next_byte
                        ):
                            await nxt.aclose()
                            raise exc
                        current = nxt
            finally:
                # Close the response (returns the connection to the shared pool);
                # never close the shared client. Release the slot regardless.
                try:
                    await current.aclose()
                finally:
                    _release_slot()

        return StreamingResponse(
            body_stream(), status_code=upstream.status_code, headers=resp_headers
        )
    except BaseException:
        # Anything before we hand off to body_stream() must free the slot.
        _release_slot()
        raise


# --- Source preflight probe + verified-source fingerprinting ----------------
#
# The browser cannot reliably Range-probe remote hosts itself (they often omit
# CORS headers), so the player asks the server to do it. The same probe feeds
# the intro/credits fingerprint worker with a source confirmed *online* instead
# of a blind `streams[0]` that may be dead or never actually played.

PROBE_MAX_URLS = 4
PROBE_CONCURRENCY = 4
PROBE_TIMEOUT_SECONDS = 5.0


class ProbeRequest(BaseModel):
    urls: list[str]
    imdb_id: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None


class ConfirmSourceRequest(BaseModel):
    url: str
    imdb_id: str
    season: int
    episode: int


def _trigger_episode_fingerprint(
    db: Session,
    user_key: str,
    imdb_id: str,
    season: int,
    episode: int,
    url: str,
) -> None:
    """Queue an intro/credits fingerprint job for a verified, online source URL.

    Ensures a ``pending`` row exists so ``/segments`` returns immediately, then
    queues the current episode (highest priority) plus lookahead/prelearning off
    the event loop. Idempotent: the worker skips re-extraction when the
    ``source_url_hash`` already matches the current fingerprint, and re-runs
    (logging ``fingerprint_url_drift``) when the verified URL differs from a
    previously fingerprinted one. Dead sources never reach here.
    """
    from workers.fingerprint_worker import (
        PRIORITY_STREAM_OPEN,
        queue_fingerprint_job,
        queue_stream_open_fingerprints,
    )

    if not url or not settings.fingerprint_segments_enabled:
        return
    canonical_id = f"{imdb_id}:{season}:{episode}"
    try:
        existing_fp = (
            db.query(EpisodeFingerprint)
            .filter_by(imdb_id=imdb_id, season=season, episode=episode)
            .first()
        )
        if existing_fp is None:
            db.add(
                EpisodeFingerprint(
                    imdb_id=imdb_id,
                    season=season,
                    episode=episode,
                    fingerprint={},
                    duration_sec=0.0,
                    status="pending",
                )
            )
            db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning(
            "episode_fp_row_failed canonical_id={} reason={}", canonical_id, exc
        )
    queue_fingerprint_job(canonical_id, url, PRIORITY_STREAM_OPEN)
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(
            None,
            queue_stream_open_fingerprints,
            user_key,
            imdb_id,
            int(season),
            int(episode),
        )
    except Exception as exc:
        logger.warning(
            "episode_fp_trigger_failed canonical_id={} reason={}", canonical_id, exc
        )


_PROVIDER_UNAVAILABLE_MESSAGES = (
    "provider unavailable",
    "provider is unavailable",
    "provider_unavailable",
    "service unavailable",
    "service_unavailable",
)


async def _probe_text_prefix(resp, content_type: str, limit: int = 2048) -> str:
    """The first bytes of a text/JSON/HTML answer, '' for media or on any error."""
    if not (
        "text/" in content_type or "json" in content_type or "html" in content_type
    ):
        return ""
    reader = getattr(resp, "aiter_bytes", None)
    if reader is None:
        return ""
    data = b""
    try:
        async for chunk in reader():
            data += chunk
            if len(data) >= limit:
                break
    except Exception:  # a broken error page is just not evidence
        return ""
    return data[:limit].decode("utf-8", "ignore")


def _names_unavailable_provider(text: str) -> bool:
    lowered = text.strip().lower()
    return bool(lowered) and any(m in lowered for m in _PROVIDER_UNAVAILABLE_MESSAGES)


async def _probe_one(client: httpx.AsyncClient, url: str) -> dict:
    """Range-probe a single source. Returns ``{state, reason, status}``.

    ``passed`` for 200/206/416 (alive, range honored or knowingly refused).
    A 4xx/5xx is ``http_error`` — an HTTP response came back, so the origin is
    reachable. A timeout is indeterminate: an upstream resolver may still be
    recovering. Only connection failures feed ``originLooksUnreachable``.
    """
    if not await _is_safe_source_url_async(url):
        return {"state": "failed", "reason": "unsafe_url", "status": None}
    try:
        # Follow redirects manually so every hop is re-checked for SSRF safety (the
        # client has follow_redirects disabled). Stream so we only read the status
        # line/headers — a Range-ignoring origin would otherwise send the whole file.
        resp = await _send_with_validated_redirects(
            client, "GET", url, {"Range": "bytes=0-1023"}
        )
        try:
            status = resp.status_code
            content_type = (resp.headers.get("content-type") or "").lower()
            size = _content_size(resp.headers)
            prefix = await _probe_text_prefix(resp, content_type)
        finally:
            await resp.aclose()
    except _UnsafeRedirect:
        return {"state": "failed", "reason": "unsafe_url", "status": None}
    except httpx.TimeoutException:
        return {"state": "indeterminate", "reason": "timeout", "status": None}
    except (httpx.NetworkError, httpx.ConnectError, OSError):
        return {"state": "failed", "reason": "network", "status": None}
    except httpx.HTTPError:
        return {"state": "failed", "reason": "network", "status": None}
    if _names_unavailable_provider(prefix):
        # The provider itself is down, not just this link: clients skip every
        # source it serves (native SourceProbeContentPolicy.providerUnavailable).
        return {"state": "failed", "reason": "provider_unavailable", "status": status}
    if status in (200, 206) and content_type.startswith(
        ("text/html", "application/json")
    ):
        # Expired signed links often answer 200 with an HTML or JSON error
        # page. That is not media, whatever the status says.
        return {"state": "failed", "reason": "not_media", "status": status}
    if status in (200, 206, 416):
        return {
            "state": "passed",
            "reason": None,
            "status": status,
            "content_type": content_type or None,
            "size": size,
            "range": status in (206, 416),
        }
    return {"state": "failed", "reason": "http_error", "status": status}


def _content_size(headers) -> Optional[int]:
    """Total resource size from Content-Range, else Content-Length."""
    total = (headers.get("content-range") or "").rpartition("/")[2].strip()
    if total.isdigit():
        return int(total)
    length = (headers.get("content-length") or "").strip()
    return int(length) if length.isdigit() else None


@app.post("/account/api/playback/probe")
async def account_playback_probe(
    request: Request,
    payload: ProbeRequest,
    db: Session = Depends(get_db_dependency),
):
    """Server-side preflight for the web player (Phase A→B bridge).

    Range-probes the top candidate sources concurrently and returns a per-URL
    verdict plus ``originLooksUnreachable`` (>=2 network-level failures with no
    HTTP response at all), so the client can say "server unreachable, try again"
    rather than "no sources". When episode context is supplied, the first source
    that passes is fed to the fingerprint worker — guaranteeing it only ever
    fingerprints a source confirmed online.
    """
    user = await _request_db_call(db, _active_profile_or_403, request)

    urls: list[str] = []
    for raw in payload.urls or []:
        candidate = (raw or "").strip()
        if candidate and candidate not in urls:
            urls.append(candidate)
        if len(urls) >= PROBE_MAX_URLS:
            break

    results: dict[str, dict] = {}
    if urls:
        sem = asyncio.Semaphore(PROBE_CONCURRENCY)
        timeout = httpx.Timeout(PROBE_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            transport=public_only_transport(allowed_hosts=_own_hosts()),
        ) as client:

            async def _guarded(u: str) -> tuple[str, dict]:
                async with sem:
                    return u, await _probe_one(client, u)

            for u, verdict in await asyncio.gather(*(_guarded(u) for u in urls)):
                results[u] = verdict

    network_failures = sum(
        1 for v in results.values() if v["status"] is None and v["reason"] == "network"
    )
    any_http_response = any(v["status"] is not None for v in results.values())
    origin_unreachable = network_failures >= 2 and not any_http_response

    if payload.imdb_id and payload.season is not None and payload.episode is not None:
        passed_url = next(
            (u for u in urls if results.get(u, {}).get("state") == "passed"),
            None,
        )
        if passed_url:
            await _request_db_call(
                db,
                _trigger_episode_fingerprint,
                user.user_key,
                payload.imdb_id,
                int(payload.season),
                int(payload.episode),
                passed_url,
            )

    return {"results": results, "originLooksUnreachable": origin_unreachable}


INSPECT_MAX_URLS = 4


@app.post("/account/api/playback/inspect")
async def account_playback_inspect(
    request: Request,
    payload: PlaybackInspectRequest,
    db: Session = Depends(get_db_dependency),
):
    """ffprobe facts for the web player's path decision (direct vs prepared).

    Each URL is read through the byte proxy with a token bound to that URL, so
    the SSRF checks cover every redirect hop. Results are cached per URL, so
    inspecting on Details open makes the later Play instant.
    """
    await _request_db_call(db, _active_profile_or_403, request)
    urls: list[str] = []
    for raw in payload.urls or []:
        candidate = (raw or "").strip()
        if candidate and candidate not in urls:
            urls.append(candidate)
        if len(urls) >= INSPECT_MAX_URLS:
            break

    async def inspect_one(url: str) -> tuple[str, dict]:
        if not await _is_safe_source_url_async(url):
            return url, {"state": "failed", "reason": "unsafe_url"}
        proxy_url, token = _internal_proxy_access(request, url)
        try:
            facts = await media_inspector.inspect(url, proxy_url, proxy_token=token)
        except MediaProbeTimeout:
            return url, {"state": "indeterminate", "reason": "timeout"}
        except MediaProbeError as exc:
            return url, {"state": "failed", "reason": exc.reason}
        return url, {"state": "ok", "media": facts.to_json()}

    results = dict(await asyncio.gather(*(inspect_one(url) for url in urls)))
    return {"results": results}


@app.post("/account/api/playback/confirm-source")
async def account_playback_confirm_source(
    request: Request,
    payload: ConfirmSourceRequest,
    db: Session = Depends(get_db_dependency),
):
    """Lock in the source that actually rendered a frame as the fingerprint input.

    The strongest "verified" signal: a probe can pass while playback still fails,
    but a rendered frame cannot. After any failover this corrects the worker to
    the source the user truly watched (idempotent via ``source_url_hash``).
    """
    user = await _request_db_call(db, _active_profile_or_403, request)
    url = (payload.url or "").strip()
    if not await _is_safe_source_url_async(url):
        raise HTTPException(status_code=400, detail="Invalid source URL")
    await _request_db_call(
        db,
        _trigger_episode_fingerprint,
        user.user_key,
        payload.imdb_id,
        int(payload.season),
        int(payload.episode),
        url,
    )
    return {"ok": True}


@app.post("/account/api/progress")
async def account_report_progress(
    request: Request,
    payload: ProgressRequest,
    db: Session = Depends(get_db_dependency),
):
    """Cookie-authenticated wrapper for browser playback progress updates."""
    user = await _request_db_call(db, _active_profile_or_403, request)
    return await report_progress(user.user_key, payload)


@app.post("/account/api/library")
async def account_add_library_item(
    request: Request,
    payload: WebLibraryAddRequest,
    db: Session = Depends(get_db_dependency),
):
    """Cookie-authenticated wrapper for saving an item from the web dashboard."""
    user = await _request_db_call(db, _active_profile_or_403, request)
    return await add_library_item(user.user_key, payload)


# The web dashboard mirrors the native app's library/watched actions. These are
# thin cookie-authenticated wrappers over the user_key routes the native clients
# call, resolved to the session's active profile.


@app.delete("/account/api/library/{media_type}/{item_id}")
def account_remove_library_item(
    request: Request,
    media_type: Literal["movie", "tv"],
    item_id: str,
    db: Session = Depends(get_db_dependency),
):
    """Cookie-authenticated wrapper for removing a saved item."""
    user = _active_profile_or_403(db, request)
    return remove_library_item(user.user_key, media_type, item_id)


@app.delete("/account/api/progress/{media_type}/{item_id}")
def account_remove_progress(
    request: Request,
    media_type: Literal["movie", "tv"],
    item_id: str,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    db: Session = Depends(get_db_dependency),
):
    """Cookie-authenticated wrapper for un-marking watched / clearing progress."""
    user = _active_profile_or_403(db, request)
    return remove_progress(user.user_key, media_type, item_id, season, episode)


@app.post("/account/api/scrobble")
async def account_scrobble(
    request: Request,
    payload: ScrobbleRequest,
    db: Session = Depends(get_db_dependency),
):
    """Cookie-authenticated wrapper for marking an item watched."""
    user = await _request_db_call(db, _active_profile_or_403, request)
    return await scrobble(user.user_key, payload)


@app.get("/account/api/watched")
def account_watched_items(
    request: Request,
    limit: int = Query(1000, ge=1, le=5000),
    db: Session = Depends(get_db_dependency),
):
    """Cookie-authenticated wrapper for the watched (completed) set."""
    user = _active_profile_or_403(db, request)
    return watched_items(user.user_key, limit)


@app.post("/account/api/segments/{video_id}/feedback")
def account_segment_feedback(
    request: Request,
    video_id: str,
    payload: SegmentFeedbackRequest,
    db: Session = Depends(get_db_dependency),
):
    """Cookie-authenticated wrapper for intro/credits marker feedback."""
    user = _active_profile_or_403(db, request)
    return post_segment_feedback(user.user_key, video_id, payload)


def _find_progress_row(
    db: Session,
    user_id: int,
    tmdb_id: int | None,
    imdb_id: str | None,
    media_type: str,
    season: int | None,
    episode: int | None,
) -> WatchProgress | None:
    """Find the canonical WatchProgress row to update, merging any duplicate.

    Prefers the (tmdb_id, media_type, season, episode) identity. If a separate
    imdb-only row exists for the same imdb_id+season+episode, the two are merged
    (newer ``updated_at`` wins, ids are unioned, the loser is dropped) so a title
    reported under either id system collapses to one canonical row.
    """
    tmdb_row = None
    if tmdb_id is not None:
        tmdb_row = (
            db.query(WatchProgress)
            .filter(
                WatchProgress.user_id == user_id,
                WatchProgress.tmdb_id == tmdb_id,
                WatchProgress.media_type == media_type,
                func.coalesce(WatchProgress.season, 0) == (season or 0),
                func.coalesce(WatchProgress.episode, 0) == (episode or 0),
            )
            .first()
        )

    imdb_row = None
    if imdb_id:
        imdb_row = (
            db.query(WatchProgress)
            .filter(
                WatchProgress.user_id == user_id,
                WatchProgress.imdb_id == imdb_id,
                func.coalesce(WatchProgress.season, 0) == (season or 0),
                func.coalesce(WatchProgress.episode, 0) == (episode or 0),
            )
            .first()
        )

    if tmdb_row and imdb_row and tmdb_row.id != imdb_row.id:
        keep, drop = tmdb_row, imdb_row
        if (imdb_row.updated_at or datetime.min) > (
            tmdb_row.updated_at or datetime.min
        ):
            keep, drop = imdb_row, tmdb_row
        merged_tmdb = keep.tmdb_id if keep.tmdb_id is not None else drop.tmdb_id
        merged_imdb = keep.imdb_id or drop.imdb_id
        # Drop the loser BEFORE writing the unioned ids onto the survivor — the
        # unit-of-work orders UPDATEs ahead of DELETEs, so assigning the shared
        # tmdb_id first would momentarily violate the uniqueness constraint.
        db.delete(drop)
        db.flush()
        keep.tmdb_id = merged_tmdb
        keep.imdb_id = merged_imdb
        db.flush()
        return keep

    return tmdb_row or imdb_row


@app.post("/api/v2/progress")
async def report_progress(user_key: BearerUserKey, payload: ProgressRequest):
    """Record or update playback progress for Continue Watching.

    Upserts one row per item, keyed by (tmdb_id, media_type, season, episode)
    or, for imdb-only items, by (imdb_id, season, episode). An imdb-only write
    is canonicalized to its tmdb_id when TMDB knows it; otherwise it is stored
    imdb-only and upgraded on a later write. If position >= 90 % of duration the
    row is deleted (item is finished).
    """
    if payload.position < 0:
        raise HTTPException(status_code=400, detail="position must be non-negative")
    if payload.duration <= 0:
        raise HTTPException(status_code=400, detail="duration must be positive")
    if payload.position > payload.duration:
        raise HTTPException(status_code=400, detail="position cannot exceed duration")

    req_tmdb_id, req_imdb_id = _require_sync_ids(payload.tmdb_id, payload.imdb_id)
    tmdb_id, imdb_id, media_type, season, episode = await _canonicalize_write_ids(
        req_tmdb_id, req_imdb_id, payload.media_type, payload.season, payload.episode
    )

    if media_type == "tv":
        if season is None or episode is None:
            raise HTTPException(
                status_code=400, detail="season and episode are required for TV"
            )
    else:
        season = 0
        episode = 0

    return await _db_call(
        _write_progress_row,
        user_key,
        payload,
        tmdb_id,
        imdb_id,
        media_type,
        season,
        episode,
    )


def _write_progress_row(
    db: Session,
    user_key: str,
    payload: ProgressRequest,
    tmdb_id: int | None,
    imdb_id: str | None,
    media_type: str,
    season: int | None,
    episode: int | None,
) -> dict:
    """The DB half of ``report_progress``, run off the loop by ``_db_call``."""
    user = _require_active_user_by_key(user_key, db)
    from app.relevance import completion_weights
    from app.home_feed import invalidate_discovery

    previous_weight = (
        completion_weights(db, user.id, tmdb_id=tmdb_id) if tmdb_id is not None else {}
    )

    def publish_taste_change():
        if tmdb_id is None:
            return
        db.flush()
        if completion_weights(db, user.id, tmdb_id=tmdb_id) != previous_weight:
            invalidate_discovery(db, user.id)

    existing = _find_progress_row(
        db, user.id, tmdb_id, imdb_id, media_type, season, episode
    )

    # If >=90% watched, treat as finished — remove the progress row
    if payload.position / payload.duration >= 0.9:
        if existing:
            db.delete(existing)
        publish_taste_change()
        _invalidate_dashboard_cache(user.id)
        return {"status": "ok", "completed": True}

    if existing:
        # Upgrade/union ids in case this row was previously keyed by only one.
        # media_type/season/episode are intentionally left untouched: the row
        # already matched on the same (coalesced) identity, so rewriting them
        # would only clobber a legacy NULL season/episode with 0.
        if tmdb_id is not None:
            existing.tmdb_id = tmdb_id
        if imdb_id:
            existing.imdb_id = imdb_id
        existing.position = payload.position
        existing.duration = payload.duration
        existing.title = payload.title or existing.title
        existing.updated_at = utc_now()
    else:
        db.add(
            WatchProgress(
                user_id=user.id,
                tmdb_id=tmdb_id,
                imdb_id=imdb_id,
                media_type=media_type,
                season=season,
                episode=episode,
                position=payload.position,
                duration=payload.duration,
                title=payload.title,
            )
        )
    publish_taste_change()
    _invalidate_dashboard_cache(user.id)

    return {"status": "ok"}


@app.get("/api/v2/history")
def watch_history(user_key: BearerUserKey, limit: int = Query(50, ge=1, le=200)):
    """Return recent watch progress for Continue Watching.

    Items are sorted by most-recently-updated first.  Only items that
    are still in progress (< 90 % watched) are returned — finished items
    are automatically cleaned up by the progress endpoint.
    """
    with get_db() as db:
        user = _require_active_user_by_key(user_key, db)

        rows = (
            db.query(WatchProgress)
            .filter(WatchProgress.user_id == user.id)
            .order_by(WatchProgress.updated_at.desc())
            .limit(limit)
            .all()
        )

        imdb_fallback = _imdb_id_fallback_map(db, rows)

        items = []
        for r in rows:
            item = {
                "tmdb_id": r.tmdb_id,
                "imdb_id": r.imdb_id or imdb_fallback.get((r.tmdb_id, r.media_type)),
                "media_type": r.media_type,
                "title": r.title,
                "position": r.position,
                "duration": r.duration,
                "updated_at": r.updated_at.isoformat() + "Z" if r.updated_at else None,
            }
            if r.media_type == "tv":
                item["season"] = r.season
                item["episode"] = r.episode
            items.append(item)

    return {"items": items}


@app.delete("/api/v2/progress/{media_type}/{item_id}")
def remove_progress(
    user_key: BearerUserKey,
    media_type: Literal["movie", "tv"],
    item_id: str,
    season: Optional[int] = None,
    episode: Optional[int] = None,
):
    """Delete watch progress and completion records for a single item.

    Un-marks a movie, or a specific episode, as watched — syncing the removal
    across devices. ``item_id`` is a numeric TMDB id or an IMDb id (``tt...``),
    matched by shape like the library-delete endpoint. For TV, pass ``season``
    and ``episode`` to target one episode, or omit both to clear the whole show.

    Clears both the in-progress row (``watch_progress``) and the durable
    completion log (``watch_events``) so an un-mark actually sticks — a watched
    item lives only in ``watch_events``. Idempotent: always returns 200, with
    ``removed`` reporting whether anything was deleted.
    """
    norm_imdb = _normalize_imdb_id(item_id)
    tmdb_id: int | None = None
    if norm_imdb is None:
        try:
            tmdb_id = int(item_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid item id")

    # Movies are stored with (season, episode) == (0, 0); force that match. For
    # TV, target the given episode, or every episode of the show when omitted.
    if media_type == "movie":
        match_season: int | None = 0
        match_episode: int | None = 0
    else:
        match_season = season
        match_episode = episode

    with get_db() as db:
        user = _require_active_user_by_key(user_key, db)

        prog_query = db.query(WatchProgress).filter(WatchProgress.user_id == user.id)
        evt_query = db.query(WatchEvent).filter(
            WatchEvent.user_id == user.id, WatchEvent.action == "complete"
        )
        if norm_imdb:
            prog_query = prog_query.filter(WatchProgress.imdb_id == norm_imdb)
            evt_query = evt_query.filter(WatchEvent.imdb_id == norm_imdb)
        else:
            prog_query = prog_query.filter(
                WatchProgress.tmdb_id == tmdb_id,
                WatchProgress.media_type == media_type,
            )
            evt_query = evt_query.filter(
                WatchEvent.tmdb_id == tmdb_id,
                WatchEvent.media_type == media_type,
            )
        if match_season is not None and match_episode is not None:
            prog_query = prog_query.filter(
                func.coalesce(WatchProgress.season, 0) == match_season,
                func.coalesce(WatchProgress.episode, 0) == match_episode,
            )
            evt_query = evt_query.filter(
                func.coalesce(WatchEvent.season, 0) == match_season,
                func.coalesce(WatchEvent.episode, 0) == match_episode,
            )

        removed_progress = prog_query.delete(synchronize_session=False)
        removed_events = evt_query.delete(synchronize_session=False)

    return {"status": "ok", "removed": bool(removed_progress or removed_events)}


@app.get("/api/v2/watched")
def watched_items(user_key: BearerUserKey, limit: int = Query(1000, ge=1, le=5000)):
    """Return the set of watched (completed) items for cross-device sync.

    Reads the durable ``watch_events`` completion log — unlike ``/history``
    (Continue Watching), which drops an item once it is finished. A fresh device
    uses this to reconstruct the full watched set and decide which shows are
    "caught up". Repeat completions of the same item collapse to one row, keyed
    by ``(tmdb_id, imdb_id, media_type, season, episode)``; TV rows carry
    ``season`` / ``episode``. Sorted most-recently-watched first.
    """
    with get_db() as db:
        user = _require_active_user_by_key(user_key, db)

        rows = (
            db.query(
                WatchEvent.tmdb_id,
                WatchEvent.imdb_id,
                WatchEvent.media_type,
                WatchEvent.season,
                WatchEvent.episode,
                func.max(WatchEvent.title).label("title"),
                func.max(WatchEvent.created_at).label("watched_at"),
            )
            .filter(
                WatchEvent.user_id == user.id,
                WatchEvent.action == "complete",
            )
            .group_by(
                WatchEvent.tmdb_id,
                WatchEvent.imdb_id,
                WatchEvent.media_type,
                WatchEvent.season,
                WatchEvent.episode,
            )
            .order_by(func.max(WatchEvent.created_at).desc())
            .limit(limit)
            .all()
        )

        items = []
        for r in rows:
            item = {
                "tmdb_id": r.tmdb_id,
                "imdb_id": r.imdb_id,
                "media_type": r.media_type,
                "title": r.title,
                "watched_at": r.watched_at.isoformat() + "Z" if r.watched_at else None,
            }
            if r.media_type == "tv":
                item["season"] = r.season
                item["episode"] = r.episode
            items.append(item)

    return {"items": items}


# ---------------------------------------------------------------------------
# Library — saved items synced across all the user's devices
# ---------------------------------------------------------------------------
class LibraryAddRequest(BaseModel):
    """Payload to add a single item to the user's library.

    At least one of ``tmdb_id`` / ``imdb_id`` must be present (validated in the
    handler). ``imdb_id`` lets the client save items with no TMDB id.
    """

    tmdb_id: Optional[int] = None
    imdb_id: Optional[str] = None  # 'tt1234567'
    media_type: Literal["movie", "tv"]
    title: Optional[str] = None
    poster_path: Optional[str] = None
    year: Optional[str] = None


class LibrarySyncItem(BaseModel):
    """One item in a bulk sync request (tmdb_id and/or imdb_id)."""

    tmdb_id: Optional[int] = None
    imdb_id: Optional[str] = None  # 'tt1234567'
    media_type: Literal["movie", "tv"]
    title: Optional[str] = None
    poster_path: Optional[str] = None
    year: Optional[str] = None


class LibrarySyncRequest(BaseModel):
    """Bulk sync: client pushes adds + removes, server returns full state."""

    added: list[LibrarySyncItem] = []
    removed: list[LibrarySyncItem] = []


def _serialize_library_item(item: LibraryItem, imdb_id: str | None = None) -> dict:
    return {
        "tmdb_id": item.tmdb_id,
        "imdb_id": item.imdb_id or imdb_id,
        "media_type": item.media_type,
        "title": item.title,
        "poster_path": item.poster_path,
        "year": item.year,
        "added_at": item.added_at.isoformat() + "Z" if item.added_at else None,
    }


def _serialize_library_items(db: Session, rows: list[LibraryItem]) -> list[dict]:
    """Serialize library rows, filling missing imdb_ids from the metadata cache."""
    imdb_fallback = _imdb_id_fallback_map(db, rows)
    return [
        _serialize_library_item(r, imdb_fallback.get((r.tmdb_id, r.media_type)))
        for r in rows
    ]


def _find_library_row(
    db: Session,
    user_id: int,
    tmdb_id: int | None,
    imdb_id: str | None,
    media_type: str,
) -> LibraryItem | None:
    """Find the canonical LibraryItem to update, merging any duplicate.

    Mirrors :func:`_find_progress_row` for saved items: prefers the
    (tmdb_id, media_type) identity and merges a separate imdb-only row for the
    same imdb_id so a title saved under either id system collapses to one row.
    """
    tmdb_row = None
    if tmdb_id is not None:
        tmdb_row = (
            db.query(LibraryItem)
            .filter(
                LibraryItem.user_id == user_id,
                LibraryItem.tmdb_id == tmdb_id,
                LibraryItem.media_type == media_type,
            )
            .first()
        )

    imdb_row = None
    if imdb_id:
        imdb_row = (
            db.query(LibraryItem)
            .filter(
                LibraryItem.user_id == user_id,
                LibraryItem.imdb_id == imdb_id,
            )
            .first()
        )

    if tmdb_row and imdb_row and tmdb_row.id != imdb_row.id:
        # Prefer the canonical (tmdb) row as the survivor; keep the earliest
        # added_at and union the ids. Drop the loser before assigning the shared
        # imdb_id so the imdb-only partial unique index can't be violated mid-flush.
        keep, drop = tmdb_row, imdb_row
        merged_imdb = keep.imdb_id or drop.imdb_id
        merged_added = keep.added_at
        if drop.added_at and (not keep.added_at or drop.added_at < keep.added_at):
            merged_added = drop.added_at
        db.delete(drop)
        db.flush()
        keep.imdb_id = merged_imdb
        keep.added_at = merged_added
        db.flush()
        return keep

    return tmdb_row or imdb_row


@app.get("/api/v2/library")
def get_library(
    user_key: BearerUserKey,
    media_type: Optional[Literal["movie", "tv"]] = None,
    since: Optional[str] = None,
    limit: int = Query(500, ge=1, le=2000),
):
    """Return the user's saved library items.

    Sorted by most-recently-added first. Supports filtering by media_type
    and incremental sync via ``since`` (ISO 8601 timestamp — only items
    added after that time are returned).
    """
    with get_db() as db:
        user = _require_active_user_by_key(user_key, db)

        query = db.query(LibraryItem).filter(LibraryItem.user_id == user.id)
        if media_type:
            query = query.filter(LibraryItem.media_type == media_type)
        if since:
            try:
                since_dt = datetime.fromisoformat(since.rstrip("Z"))
                query = query.filter(LibraryItem.added_at > since_dt)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid 'since' timestamp")

        rows = query.order_by(LibraryItem.added_at.desc()).limit(limit).all()
        return annotate_tree(db, {"items": _serialize_library_items(db, rows)})


@app.post("/api/v2/library")
async def add_library_item(user_key: BearerUserKey, payload: LibraryAddRequest):
    """Add an item to the user's library. Idempotent — re-adding is a no-op.

    Items may be keyed by ``tmdb_id`` and/or ``imdb_id``; imdb-only items are
    canonicalized to a tmdb_id when TMDB knows them, otherwise stored imdb-only.
    """
    req_tmdb_id, req_imdb_id = _require_sync_ids(payload.tmdb_id, payload.imdb_id)
    tmdb_id, imdb_id, media_type, _season, _episode = await _canonicalize_write_ids(
        req_tmdb_id, req_imdb_id, payload.media_type, None, None
    )

    result = await _db_call(
        _write_library_item, user_key, payload, tmdb_id, imdb_id, media_type
    )
    if result.get("added") and tmdb_id is not None:
        # Saved titles are likely to be owned next, so they are tagged too.
        from app import library_catalog

        await library_catalog.queue_owned_tagging()
    return result


def _write_library_item(
    db: Session,
    user_key: str,
    payload: LibraryAddRequest,
    tmdb_id: int | None,
    imdb_id: str | None,
    media_type: str,
) -> dict:
    """The DB half of ``add_library_item``, run off the loop by ``_db_call``."""
    user = db.query(User).filter(User.user_key == user_key).first()
    if not user:
        raise invalid_profile_credential()
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User is inactive")

    existing = _find_library_row(db, user.id, tmdb_id, imdb_id, media_type)
    if existing:
        # Upgrade/union ids and refresh denormalized fields if newer.
        if tmdb_id is not None:
            existing.tmdb_id = tmdb_id
        if imdb_id:
            existing.imdb_id = imdb_id
        if payload.title:
            existing.title = payload.title
        if payload.poster_path:
            existing.poster_path = payload.poster_path
        if payload.year:
            existing.year = payload.year
        db.flush()
        _invalidate_dashboard_cache(user.id)
        return {
            "status": "ok",
            "added": False,
            "item": _serialize_library_item(existing),
        }

    item = LibraryItem(
        user_id=user.id,
        tmdb_id=tmdb_id,
        imdb_id=imdb_id,
        media_type=media_type,
        title=payload.title,
        poster_path=payload.poster_path,
        year=payload.year,
    )
    db.add(item)
    db.flush()
    _invalidate_dashboard_cache(user.id)
    return {
        "status": "ok",
        "added": True,
        "item": _serialize_library_item(item),
    }


@app.delete("/api/v2/library/{media_type}/{item_id}")
def remove_library_item(
    user_key: BearerUserKey, media_type: Literal["movie", "tv"], item_id: str
):
    """Remove an item from the user's library.

    ``item_id`` is either a numeric TMDB id or an IMDb id (``tt...``); the
    matching key is chosen by its shape, so tmdb-only clients are unaffected.
    """
    with get_db() as db:
        user = db.query(User).filter(User.user_key == user_key).first()
        if not user:
            raise invalid_profile_credential()
        if not user.is_active:
            raise HTTPException(status_code=403, detail="User is inactive")

        query = db.query(LibraryItem).filter(LibraryItem.user_id == user.id)
        norm_imdb = _normalize_imdb_id(item_id)
        if norm_imdb:
            query = query.filter(LibraryItem.imdb_id == norm_imdb)
        else:
            try:
                tmdb_id = int(item_id)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid item id")
            query = query.filter(
                LibraryItem.tmdb_id == tmdb_id,
                LibraryItem.media_type == media_type,
            )

        deleted = query.delete(synchronize_session=False)
        if deleted:
            _invalidate_dashboard_cache(user.id)
        return {"status": "ok", "removed": bool(deleted)}


# --- "Not for me" ---------------------------------------------------------------
class HiddenTitleRequest(BaseModel):
    tmdb_id: int = Field(gt=0)
    media_type: Literal["movie", "tv"]


def _hidden_titles_changed(db: Session, user_id: int) -> None:
    from app.home_feed import invalidate_discovery

    invalidate_discovery(db, user_id)
    db.commit()
    _taste_cache.pop(user_id, None)
    _invalidate_dashboard_cache(user_id)
    # Personal rows are rebuilt with the new taste (hidden tags count against).
    _schedule_local_sync(user_id)


def _hidden_profile(db: Session, user_key: str) -> User:
    user = db.query(User).filter(User.user_key == user_key).first()
    if not user:
        raise invalid_profile_credential()
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User is inactive")
    return user


def _list_hidden(db: Session, user: User) -> dict:
    rows = (
        db.query(HiddenTitle)
        .filter(HiddenTitle.user_id == user.id)
        .order_by(HiddenTitle.created_at.desc())
        .all()
    )
    keys = [(row.media_type, row.tmdb_id) for row in rows]
    metas = {
        (m.media_type, m.tmdb_id): m
        for m in (
            db.query(MediaMetadata)
            .options(load_only(MediaMetadata.title, MediaMetadata.poster_path))
            .filter(tuple_(MediaMetadata.media_type, MediaMetadata.tmdb_id).in_(keys))
            .all()
            if keys
            else []
        )
    }
    return {
        "items": [
            {
                "tmdb_id": row.tmdb_id,
                "media_type": row.media_type,
                "title": getattr(
                    metas.get((row.media_type, row.tmdb_id)), "title", None
                ),
                "poster_path": getattr(
                    metas.get((row.media_type, row.tmdb_id)), "poster_path", None
                ),
                "hidden_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    }


def _set_hidden(db: Session, user: User, tmdb_id: int, media_type: str, hide: bool):
    query = db.query(HiddenTitle).filter(
        HiddenTitle.user_id == user.id,
        HiddenTitle.tmdb_id == tmdb_id,
        HiddenTitle.media_type == media_type,
    )
    existing = query.first()
    changed = False
    if hide and existing is None:
        db.add(HiddenTitle(user_id=user.id, tmdb_id=tmdb_id, media_type=media_type))
        changed = True
    elif not hide and existing is not None:
        query.delete(synchronize_session=False)
        changed = True
    if changed:
        _hidden_titles_changed(db, user.id)
    else:
        db.commit()
    return {"status": "ok", "hidden": hide, "changed": changed}


@app.get("/api/v2/hidden")
def list_hidden_titles(user_key: BearerUserKey):
    """Titles this profile marked "Not for me"."""
    with get_db() as db:
        return _list_hidden(db, _hidden_profile(db, user_key))


@app.post("/api/v2/hidden")
def hide_title(user_key: BearerUserKey, payload: HiddenTitleRequest):
    """Mark a title "Not for me": it leaves every row and the hero."""
    with get_db() as db:
        user = _hidden_profile(db, user_key)
        return _set_hidden(db, user, payload.tmdb_id, payload.media_type, True)


@app.delete("/api/v2/hidden/{media_type}/{tmdb_id}")
def unhide_title(
    user_key: BearerUserKey, media_type: Literal["movie", "tv"], tmdb_id: int
):
    """Undo "Not for me"."""
    with get_db() as db:
        user = _hidden_profile(db, user_key)
        return _set_hidden(db, user, tmdb_id, media_type, False)


@app.get("/account/api/hidden")
def account_list_hidden_titles(
    request: Request, db: Session = Depends(get_db_dependency)
):
    """Cookie-authenticated wrapper for the web dashboard."""
    user = _active_profile_or_403(db, request)
    return list_hidden_titles(user.user_key)


@app.post("/account/api/hidden")
def account_hide_title(
    request: Request,
    payload: HiddenTitleRequest,
    db: Session = Depends(get_db_dependency),
):
    user = _active_profile_or_403(db, request)
    return hide_title(user.user_key, payload)


@app.delete("/account/api/hidden/{media_type}/{tmdb_id}")
def account_unhide_title(
    request: Request,
    media_type: Literal["movie", "tv"],
    tmdb_id: int,
    db: Session = Depends(get_db_dependency),
):
    user = _active_profile_or_403(db, request)
    return unhide_title(user.user_key, media_type, tmdb_id)


@app.post("/api/v2/library/sync")
async def sync_library(user_key: BearerUserKey, payload: LibrarySyncRequest):
    """Bulk sync: apply client's adds + removes, return full server state.

    Useful for first-launch reconciliation when the app has offline edits
    or for migrating from another service. Items may be tmdb- and/or imdb-keyed.
    """
    # Canonicalize adds up front (outside the DB session) so a slow TMDB /find
    # never holds a transaction open. Resolutions are cached, so this is cheap.
    #
    # Concurrently, not one at a time: a first-launch migration can carry a couple
    # hundred items, and each uncached one is its own TMDB /find. Serially awaited
    # that was a few hundred round trips end to end before a single row was written.
    # The semaphore keeps the burst off TMDB's rate limiter.
    _sync_find_sem = asyncio.Semaphore(8)

    async def _canonicalize(a):
        req_tmdb_id, req_imdb_id = _require_sync_ids(a.tmdb_id, a.imdb_id)
        async with _sync_find_sem:
            c_tmdb, c_imdb, c_media, _s, _e = await _canonicalize_write_ids(
                req_tmdb_id, req_imdb_id, a.media_type, None, None
            )
        return (a, c_tmdb, c_imdb, c_media)

    canonical_added = list(
        await asyncio.gather(*(_canonicalize(a) for a in payload.added))
    )

    return await _db_call(_apply_library_sync, user_key, payload, canonical_added)


def _apply_library_sync(
    db: Session,
    user_key: str,
    payload: LibrarySyncRequest,
    canonical_added: list,
) -> dict:
    """The DB half of ``sync_library``, run off the loop by ``_db_call``."""
    user = db.query(User).filter(User.user_key == user_key).first()
    if not user:
        raise invalid_profile_credential()
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User is inactive")

    # Apply removes first so re-adds in the same payload still work. Match by
    # whichever id(s) the client supplied (tmdb and/or imdb).
    for r in payload.removed:
        r_imdb = _normalize_imdb_id(r.imdb_id)
        del_query = db.query(LibraryItem).filter(LibraryItem.user_id == user.id)
        if r.tmdb_id is not None:
            del_query.filter(
                LibraryItem.tmdb_id == r.tmdb_id,
                LibraryItem.media_type == r.media_type,
            ).delete(synchronize_session=False)
        if r_imdb:
            db.query(LibraryItem).filter(
                LibraryItem.user_id == user.id,
                LibraryItem.imdb_id == r_imdb,
            ).delete(synchronize_session=False)

    for a, c_tmdb, c_imdb, c_media in canonical_added:
        existing = _find_library_row(db, user.id, c_tmdb, c_imdb, c_media)
        if existing:
            if c_tmdb is not None:
                existing.tmdb_id = c_tmdb
            if c_imdb:
                existing.imdb_id = c_imdb
            if a.title:
                existing.title = a.title
            if a.poster_path:
                existing.poster_path = a.poster_path
            if a.year:
                existing.year = a.year
        else:
            db.add(
                LibraryItem(
                    user_id=user.id,
                    tmdb_id=c_tmdb,
                    imdb_id=c_imdb,
                    media_type=c_media,
                    title=a.title,
                    poster_path=a.poster_path,
                    year=a.year,
                )
            )

    db.flush()
    _invalidate_dashboard_cache(user.id)
    rows = (
        db.query(LibraryItem)
        .filter(LibraryItem.user_id == user.id)
        .order_by(LibraryItem.added_at.desc())
        .all()
    )
    return {
        "status": "ok",
        "applied": {"added": len(payload.added), "removed": len(payload.removed)},
        "items": _serialize_library_items(db, rows),
    }


@app.get("/auth/verify-invite")
@limiter.limit("10/minute")
async def verify_invite(
    request: Request,
    invite: str = Query(..., description="Invite code to verify"),
):
    """Verify an invite code is valid without consuming it."""
    with get_db() as db_session:
        inv = (
            db_session.query(InviteCode)
            .filter(InviteCode.code == invite, InviteCode.is_used.is_(False))
            .first()
        )
        if inv:
            if inv.expires_at and utc_now() > inv.expires_at:
                raise HTTPException(status_code=403, detail="Invite code has expired")
            return {"status": "valid"}
        else:
            raise HTTPException(status_code=403, detail="Invalid invite code")


# ---------------------------------------------------------------------------
# Playback sources (native clients)
# ---------------------------------------------------------------------------


def _native_playback_context(
    db: Session, user_key: str, media: MediaRef
) -> tuple[str, Optional[str]]:
    """``(saved quality, title)`` as plain values for an active profile."""
    user = _get_cached_user(user_key, db)
    if not user:
        raise invalid_profile_credential()
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User is inactive")
    return _profile_quality(user), _media_title(db, media)


@app.post("/api/v2/playback/sources")
async def native_playback_sources(
    user_key: BearerUserKey,
    payload: PlaybackSourcesRequest,
    db: Session = Depends(get_db_dependency),
):
    if payload.library_item:

        def item_quality(session: Session) -> str:
            user = _get_cached_user(user_key, session)
            if not user:
                raise invalid_profile_credential()
            if not user.is_active:
                raise HTTPException(status_code=403, detail="User is inactive")
            return _profile_quality(user)

        default_quality = await _request_db_call(db, item_quality)
        result = await _library_item_sources(
            payload.library_item,
            user_key=user_key,
            quality=payload.quality or default_quality,
            hints=payload.hints(),
        )
        return JSONResponse(content=result, headers={"Cache-Control": "no-store"})
    media = payload.media_ref()
    default_quality, title = await _request_db_call(
        db, lambda session: _native_playback_context(session, user_key, media)
    )
    result = await _tracked_sources(
        media,
        user_key=user_key,
        quality=payload.quality or default_quality,
        title=payload.title or title,
        session_id=payload.viewing_session_id,
        refresh=payload.refresh,
        hints=payload.hints(),
    )

    sources = result["sources"]
    if media.is_episode and sources and media.imdb_id:

        def fingerprint(session: Session) -> None:
            _trigger_episode_fingerprint(
                session,
                user_key,
                media.imdb_id,
                int(media.season or 0),
                int(media.episode or 0),
                sources[0].get("url", ""),
            )

        try:
            await _request_db_call(db, fingerprint)
        except Exception:
            logger.warning("Episode fingerprint scheduling failed")

    return JSONResponse(
        content=result,
        # The first path segment is the profile credential: never shared-cacheable.
        headers={"Cache-Control": "no-store"},
    )


_SEGMENT_CONFIDENCE: dict[str, float] = {"intro": 0.65, "credits": 0.70}
_SEGMENT_NEGATIVE_FEEDBACK_SUPPRESS = 2
_SEGMENT_FEEDBACK_CORRECT_BOOST = 0.1


def _segment_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _aggregate_segment_feedback(db, imdb_id, season, episode, segment_type):
    """Aggregate all users' feedback for one (episode, segment_type).

    Returns (wrong_count, correct_count, adjusted_start, adjusted_end) where the adjusted
    boundaries are the median of any user-supplied corrections (None if none)."""
    rows = (
        db.query(SegmentFeedback)
        .filter_by(
            imdb_id=imdb_id, season=season, episode=episode, segment_type=segment_type
        )
        .all()
    )
    wrong = sum(1 for r in rows if r.feedback == "wrong")
    correct = sum(1 for r in rows if r.feedback == "correct")
    adjustments = [
        (r.adjusted_start, r.adjusted_end)
        for r in rows
        if r.adjusted_start is not None and r.adjusted_end is not None
    ]
    adjusted_start = adjusted_end = None
    if adjustments:
        starts = sorted(a for a, _ in adjustments)
        ends = sorted(b for _, b in adjustments)
        adjusted_start = starts[len(starts) // 2]
        adjusted_end = ends[len(ends) // 2]
    return wrong, correct, adjusted_start, adjusted_end


def _build_served_segments(db, imdb_id, season, episode, markers):
    """Apply aggregated user feedback to auto markers, then the plausibility and confidence
    gates, and return the serializable segment list. Feedback is authoritative at serve time
    (the async learner stays pure): net-negative feedback suppresses an auto marker, user
    adjustments override its boundaries, and confirmations nudge confidence above the gate.
    User-provided markers are never suppressed. Auto credits markers starting implausibly
    early — below CREDITS_MIN_START_RATIO of the episode, or more than
    CREDITS_MAX_START_DISTANCE_FROM_END_SEC before its end — are dropped. The detector
    guards only markers written after they were added, and the player treats a served
    credits marker as authoritative for the Next Episode prompt."""
    from app.segment_policy import season_segment_enabled
    from workers.fingerprint_worker import (
        CREDITS_TERMINAL_WINDOW_SEC,
        CREDITS_VISUAL_MAX_START_DISTANCE_FROM_END_SEC,
        CREDITS_VISUAL_MIN_SCORE,
        MARKER_FINGERPRINT_VERSION,
        CHROMAPRINT_SAMPLE_DURATION,
        VISUAL_FEATURE_VERSION,
    )

    duration_sec = None
    if any(m.segment_type == "credits" for m in markers):
        row = (
            db.query(EpisodeFingerprint.duration_sec)
            .filter_by(imdb_id=imdb_id, season=season, episode=episode)
            .first()
        )
        duration_sec = row[0] if row else None
    segments = []
    markers_by_type: dict[str, list] = {}
    for marker in markers:
        markers_by_type.setdefault(marker.segment_type, []).append(marker)
    for seg_type in ("intro", "credits"):
        if not season_segment_enabled(db, imdb_id, season, seg_type):
            continue
        typed_markers = markers_by_type.get(seg_type, [])
        if not typed_markers:
            continue
        user_markers = [
            marker
            for marker in typed_markers
            if bool(getattr(marker, "is_user_provided", False))
            or getattr(marker, "source", None) in ("manual", "client_feedback")
        ]
        candidates = sorted(
            user_markers or typed_markers,
            key=lambda marker: (
                int(
                    getattr(marker, "fingerprint_version", None)
                    == MARKER_FINGERPRINT_VERSION
                ),
                _segment_float(marker.confidence),
                int(getattr(marker, "id", 0) or 0),
            ),
            reverse=True,
        )
        marker = candidates[0]
        seg_type = marker.segment_type
        wrong, correct, adj_start, adj_end = _aggregate_segment_feedback(
            db, imdb_id, season, episode, seg_type
        )
        is_user = bool(getattr(marker, "is_user_provided", False)) or (
            getattr(marker, "source", None) in ("manual", "client_feedback")
        )
        if (
            not is_user
            and wrong >= _SEGMENT_NEGATIVE_FEEDBACK_SUPPRESS
            and correct < wrong
        ):
            continue
        start, end = marker.start_sec, marker.end_sec
        applied_adjustment = (
            adj_start is not None and adj_end is not None and adj_end > adj_start
        )
        if applied_adjustment:
            start, end = adj_start, adj_end
        # Detector-version and terminal-evidence gates are repeated here deliberately.
        # Old auto markers disappear immediately after rollout, and unknown duration,
        # non-terminal, or weak visual credits always fail closed.
        if (
            not is_user
            and not applied_adjustment
            and marker.fingerprint_version != MARKER_FINGERPRINT_VERSION
        ):
            continue
        if (
            seg_type == "credits"
            and not is_user
            and not applied_adjustment
            and (
                not duration_sec
                or getattr(marker, "source", None)
                not in {"visual-v3", "visual-v3+boundary"}
                or not isinstance(getattr(marker, "evidence", None), dict)
                or marker.evidence.get("version") != VISUAL_FEATURE_VERSION
                or _segment_float(marker.evidence.get("score"))
                < CREDITS_VISUAL_MIN_SCORE
                or _segment_float(marker.evidence.get("onset_sec"), -1.0) < 0
                or abs(_segment_float(marker.evidence.get("onset_sec")) - start) > 0.25
                or (duration_sec - start)
                > CREDITS_VISUAL_MAX_START_DISTANCE_FROM_END_SEC
                or abs(duration_sec - end)
                > CREDITS_TERMINAL_WINDOW_SEC + CHROMAPRINT_SAMPLE_DURATION
            )
        ):
            continue
        confidence = marker.confidence
        if correct > wrong:
            confidence = min(confidence + _SEGMENT_FEEDBACK_CORRECT_BOOST, 1.0)
        gate = _SEGMENT_CONFIDENCE.get(seg_type, 0.65)
        if is_user or confidence >= gate or applied_adjustment:
            segments.append(
                {
                    "type": seg_type,
                    "start": start,
                    "end": end,
                    "confidence": confidence,
                    "boundary_status": "confirmed",
                    "detector_version": getattr(marker, "detector_version", None),
                    "source": getattr(marker, "source", None)
                    or ("manual" if is_user else "unknown"),
                }
            )
    return segments


def _apply_community_segments(
    db, imdb_id: str, season: int, episode: int, segments: list[dict]
) -> tuple[list[dict], bool]:
    """Put the community databases' intro and credits ahead of fingerprint
    markers. A manual or client-feedback marker still wins, and the same
    feedback that suppresses or adjusts an auto marker applies here."""
    from app.intro_sources import best_by_type, lookup
    from app.segment_policy import season_segment_enabled

    community, cached = lookup(db, imdb_id, season, episode)
    if not community:
        return segments, cached
    served = {segment["type"]: segment for segment in segments}
    for seg_type, candidate in best_by_type(community).items():
        current = served.get(seg_type)
        if current is not None and current.get("source") in (
            "manual",
            "client_feedback",
        ):
            continue
        if not season_segment_enabled(db, imdb_id, season, seg_type):
            continue
        wrong, correct, adj_start, adj_end = _aggregate_segment_feedback(
            db, imdb_id, season, episode, seg_type
        )
        if wrong >= _SEGMENT_NEGATIVE_FEEDBACK_SUPPRESS and correct < wrong:
            served.pop(seg_type, None)
            continue
        start, end = candidate["start"], candidate["end"]
        if adj_start is not None and adj_end is not None and adj_end > adj_start:
            start, end = adj_start, adj_end
        served[seg_type] = {
            "type": seg_type,
            "start": start,
            "end": end,
            "confidence": candidate["confidence"],
            "boundary_status": "confirmed",
            "detector_version": None,
            "source": candidate["source"],
        }
    order = {"intro": 0, "credits": 1}
    return sorted(served.values(), key=lambda s: order.get(s["type"], 2)), cached


def _build_segments_response(
    db, video_id: str, imdb_id: str, season: int, episode: int
) -> dict:
    """Shared response builder for cookie- and key-authenticated segment routes."""
    from workers.fingerprint_worker import (
        DETECTOR_VERSION,
        _audio_phase_ready,
        _visual_phase_ready,
    )

    fp_row = (
        db.query(EpisodeFingerprint)
        .filter_by(imdb_id=imdb_id, season=season, episode=episode)
        .first()
    )
    markers = (
        db.query(SegmentMarker)
        .filter_by(imdb_id=imdb_id, season=season, episode=episode)
        .all()
    )
    segments = _build_served_segments(db, imdb_id, season, episode, markers)
    if not settings.fingerprint_segments_enabled:
        # The learner is switched off: serve only hand-set markers, and report
        # no fingerprint progress so the apps don't wait on it.
        segments = [
            s for s in segments if s.get("source") in ("manual", "client_feedback")
        ]
        fp_row = None
    segments, cached = _apply_community_segments(db, imdb_id, season, episode, segments)
    offered = {
        "intro": settings.skip_intro_enabled,
        "credits": settings.skip_credits_enabled,
    }
    segments = [s for s in segments if offered.get(s["type"], False)]
    served_sources = {s["type"]: s.get("source") for s in segments}
    logger.info(
        f"segments_source video={video_id} intro={served_sources.get('intro') or 'none'}"
        f" credits={served_sources.get('credits') or 'none'} cached={cached}"
    )
    if fp_row is None:
        fingerprint_status = "unavailable"
        status = "ready" if segments else "not_found"
        retry_after_sec = None
    else:
        payload = fp_row.fingerprint or {}
        head_ready = _audio_phase_ready(payload, "head")
        tail_ready = _audio_phase_ready(payload, "tail")
        visual_ready = _visual_phase_ready(payload)
        if (
            fp_row.status == "done"
            and head_ready
            and visual_ready
            and payload.get("detector_version") == DETECTOR_VERSION
        ):
            fingerprint_status = "complete"
            status = "ready" if segments else "learning"
            retry_after_sec = None
        elif head_ready or tail_ready or visual_ready:
            fingerprint_status = "partial"
            status = "ready" if segments else "learning"
            retry_after_sec = 15
        else:
            fingerprint_status = "pending"
            status = "ready" if segments else "pending"
            retry_after_sec = 5
    response = {
        "video_id": video_id,
        "status": status,
        "segments": segments,
        "fingerprint_status": fingerprint_status,
    }
    if retry_after_sec is not None:
        response["retry_after_sec"] = retry_after_sec
    return response


@app.get("/api/v2/segments/{video_id}")
def get_segments(user_key: BearerUserKey, video_id: str):
    parts = video_id.split(":")
    if len(parts) < 3:
        return JSONResponse(status_code=400, content={"error": "invalid video_id"})
    try:
        imdb_id, season, episode = parts[0], int(parts[1]), int(parts[2])
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "invalid video_id"})
    with get_db() as db:
        try:
            _require_active_user_by_key(user_key, db)
        except HTTPException:
            return JSONResponse(status_code=401, content={"error": "unauthorized"})

        return JSONResponse(
            content=_build_segments_response(db, video_id, imdb_id, season, episode)
        )


@app.post("/api/v2/segments/{video_id}/feedback")
def post_segment_feedback(
    user_key: BearerUserKey, video_id: str, payload: SegmentFeedbackRequest
):
    parts = video_id.split(":")
    if len(parts) < 3:
        return JSONResponse(status_code=400, content={"error": "invalid video_id"})
    try:
        imdb_id, season, episode = parts[0], int(parts[1]), int(parts[2])
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "invalid video_id"})

    with get_db() as db:
        try:
            user = _require_active_user_by_key(user_key, db)
        except HTTPException:
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
        existing = (
            db.query(SegmentFeedback)
            .filter_by(
                user_id=user.id,
                imdb_id=imdb_id,
                season=season,
                episode=episode,
                segment_type=payload.segment_type,
            )
            .first()
        )
        if existing:
            existing.feedback = payload.feedback
            existing.adjusted_start = payload.adjusted_start
            existing.adjusted_end = payload.adjusted_end
        else:
            db.add(
                SegmentFeedback(
                    user_id=user.id,
                    imdb_id=imdb_id,
                    season=season,
                    episode=episode,
                    segment_type=payload.segment_type,
                    feedback=payload.feedback,
                    adjusted_start=payload.adjusted_start,
                    adjusted_end=payload.adjusted_end,
                )
            )
    return JSONResponse(content={"status": "ok"})


@app.post("/api/v2/playback/failure")
async def native_playback_failure(user_key: BearerUserKey, payload: FailureReport):
    """Best-effort playback-failure signal that nudges future source ordering
    away from a provider having a real outage. Bookkeeping problems never turn
    into an error for the client: the failure it reports already happened.
    """

    def check() -> bool:
        with get_db() as db:
            try:
                _require_active_user_by_key(user_key, db)
                return True
            except HTTPException:
                return False

    if not await asyncio.to_thread(check):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    await source_resolver.report_failure(payload)
    return JSONResponse(content={"status": "ok"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
