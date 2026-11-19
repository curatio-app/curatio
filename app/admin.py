"""
Admin portal for Curatio.

Provides a web dashboard to manage settings, trigger builds, and monitor status.
Protected by master password authentication.
"""

import asyncio
import collections
import hashlib
from urllib.parse import urlparse
import secrets
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from loguru import logger
from sqlalchemy import and_, func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import defer

from app.time_utils import utc_now
from app.auth import (
    auth_requirement,
    verify_password,
    dummy_verify_password,
    verify_account_totp,
    verify_user_session,
    get_client_ip,
)
from app.config import settings
from app.crypto import decrypt_token, encrypt_token
from app.passkeys import (
    b64url_decode,
    b64url_encode,
    cookie_secure as _cookie_secure,
    credential_from_dict,
    get_webauthn_config,
    load_webauthn,
    options_to_dict,
)
from app.database import get_db
from app.taxonomy import TAXONOMY_VERSION
from workers.build_control import pause_event as build_pause_event
from app.models import (
    Account,
    AdminSetting,
    AdminSession,
    AppPairingSession,
    DetailSnapshot,
    DevicePairingSession,
    ImdbResolution,
    InviteCode,
    Tag,
    TagObservation,
    MovieTag,
    MediaMetadata,
    UniversalCategory,
    UniversalCatalogContent,
    User,
    UserCatalog,
    UserCatalogContent,
    UserSession,
    TaggingJob,
    EpisodeFingerprint,
    SegmentMarker,
    ExternalSegmentCache,
    PasskeyCredential,
    PasskeyChallenge,
    SeasonLearningState,
    SeasonSegmentReference,
)

from workers.fingerprint_worker import (
    SEASON_PRELEARNING_ENABLED,
    SEASON_PRELEARNING_MODE,
    get_fingerprint_runtime_config,
    get_fingerprint_worker_stats,
)

router = APIRouter(prefix="/admin", tags=["admin"])

_OVERVIEW_TTL_SECONDS = 10.0
_overview_cache: tuple[float, dict] | None = None


# Ground-truth liveness of the build worker thread. Set/cleared from INSIDE the
# thread (via _run_build_sync), so start_build can refuse a concurrent build even
# in the window after a stop was requested but before the thread has finished
# unwinding — the case that previously allowed two builds to race the same tables.
_build_thread_alive = False


def _run_build_sync(build_main, movies: int, shows: int, fetch_only: bool = False):
    """Run the async build worker in a dedicated event loop inside a thread.

    This keeps synchronous DB calls off the main event loop so the web UI
    stays responsive during long builds.
    """
    global _build_thread_alive
    _build_thread_alive = True
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(build_main(movies, shows, fetch_only=fetch_only))
    finally:
        loop.close()
        _build_thread_alive = False


def _run_repair_sync(on_step):
    """Re-tag stale-fact titles on a worker thread, like every other long job."""
    from workers.retag import run_retag

    loop = asyncio.new_event_loop()
    try:
        entry = {
            "key": "retag",
            "title": "Re-tag titles missing the review signal",
            "status": "running",
            "detail": "",
        }
        steps = [entry]
        on_step({"current": entry, "steps": steps})
        try:
            stats = loop.run_until_complete(run_retag(stale_facts=True))
            entry["status"] = "completed"
            tilde = "~" if stats.get("estimated_usage") else ""
            entry["detail"] = (
                f"{stats.get('processed', 0):,} titles, "
                f"{stats.get('tags_written', 0):,} tags, "
                f"{tilde}${stats.get('cost_usd', 0.0):.2f}"
            )
        except Exception as exc:  # noqa: BLE001
            entry["status"] = "failed"
            entry["detail"] = f"{type(exc).__name__}: {exc}"[:400]
            raise
        finally:
            on_step({"current": entry, "steps": steps})
        return {"steps": steps, "cost_usd": stats.get("cost_usd", 0.0)}
    finally:
        loop.close()


def _run_retag_sync(media_type, limit, min_votes, force, tmdb_ids):
    """Run a re-tag in a dedicated event loop inside a thread.

    Same reasoning as :func:`_run_upgrade_sync` -- see its docstring for the
    two failure modes an ``asyncio.create_task`` onto the web loop produces.
    """
    from workers.retag import run_retag

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            run_retag(media_type, limit, min_votes, force, tmdb_ids=tmdb_ids)
        )
    finally:
        loop.close()


def _run_upgrade_sync(skip, limit, min_votes, on_step):
    """Run the upgrade pipeline in a dedicated event loop inside a thread.

    Same reasoning as :func:`_run_build_sync`, and for the same reasons -- this
    was originally scheduled with ``asyncio.create_task`` onto the web server's
    own loop, which produced two distinct failures:

    **The pause gate deadlocked the server.** ``build_control.check_pause`` is
    ``async def`` but calls ``threading.Event.wait()`` with no timeout, which is
    correct only on a worker thread. On the main loop it parked the loop itself,
    so the Resume request could never be read off the socket and no amount of
    waiting could recover it -- a container restart was the only way out.

    **Synchronous DB work stalled every request.** ``enrich`` and ``mdb_sync``
    do their SQLAlchemy work inline; each chunk blocked the loop for 1-3
    seconds, hundreds of times across a multi-hour run, which reads to a
    client as a timeout.

    Running the whole pipeline here fixes both at once and leaves the workers
    untouched -- ``check_pause``'s blocking wait is now genuinely on a worker
    thread, exactly as its own comment claims.
    """
    from workers.upgrade import run_upgrade

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            run_upgrade(skip=skip, limit=limit, min_votes=min_votes, on_step=on_step)
        )
    finally:
        loop.close()


# Active build task (in-memory, intentionally not persisted)
_active_build_task: Optional[asyncio.Task] = None
SESSION_DURATION = timedelta(hours=24)
_ADMIN_LOGIN_ATTEMPTS: dict[str, collections.deque[float]] = {}
_ADMIN_LOGIN_LIMIT = 10
_ADMIN_LOGIN_WINDOW = 60

# ---- Default initial-build targets (used when a request omits them) ----
DEFAULT_BUILD_MOVIES = 100000
DEFAULT_BUILD_SHOWS = 50000


def _clear_catalog_cache(reason: str) -> None:
    """Drop the serving cache so a rebuild becomes visible immediately.

    Catalog rows are served from an in-process LRU with a ``cache_ttl``
    (default 1 h) lifetime, so anything that rewrites
    ``universal_catalog_content`` must clear it or the work stays invisible for
    up to an hour -- indistinguishable, from the operator's side, from the job
    having done nothing.

    Never raises: failing to clear a cache must not fail the operation that
    just succeeded.
    """
    try:
        from app.main import _catalog_cache, _invalidate_dashboard_cache

        _catalog_cache.clear()
        _invalidate_dashboard_cache()
        from app.home_feed import invalidate_published_discovery

        invalidate_published_discovery()
        logger.info(f"Catalog and dashboard caches cleared after {reason}")
    except Exception as exc:  # noqa: BLE001 - a cache miss is not a failure
        logger.warning(f"Could not clear catalog cache after {reason}: {exc}")


#: Matches ``TagAlias.normalized``'s String(160). An alias key longer than the
#: column silently truncates, and ``resolve_tag`` looks up the untruncated form,
#: so a truncated alias can never match.
_ALIAS_KEY_MAX = 160

#: Upper bound on a scoped re-tag's id list. Large enough for every sample id
#: the observation table can hold (500 observations x 50 samples), small enough
#: that the resulting ``IN`` clause stays sane.
_MAX_SCOPED_IDS = 20_000


def _parse_id_list(value) -> list[int] | None:
    """Coerce a request body's id list to ints, or None if absent.

    Returns None for a missing key and ``[]`` for an explicitly empty list --
    the distinction matters downstream, where None means "no scope" and an
    empty list means "nothing to do".
    """
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise HTTPException(status_code=400, detail="tmdb_ids must be a list")
    ids: list[int] = []
    for entry in value:
        try:
            ids.append(int(entry))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400, detail=f"tmdb_ids contains a non-integer: {entry!r}"
            ) from None
    if len(ids) > _MAX_SCOPED_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"tmdb_ids is limited to {_MAX_SCOPED_IDS:,} entries",
        )
    return ids


# ---- Fingerprint-series pagination (bounds the diagnostics payload/memory) ----
DEFAULT_FINGERPRINT_SERIES_PAGE = 100
MAX_FINGERPRINT_SERIES_PAGE = 500

# ---- Ring buffer sizes ----
_BUILD_LOG_BUFFER_SIZE = 2000
_FINGERPRINT_LOG_BUFFER_SIZE = 500

# ---- Build log ring buffer ----
_build_logs: collections.deque[str] = collections.deque(maxlen=_BUILD_LOG_BUFFER_SIZE)
_build_log_sink_id: Optional[int] = None

# ---- WebSocket clients for live build logs ----
_ws_clients: set = set()

# ---- Fingerprint log ring buffer ----
_fingerprint_logs: collections.deque[dict[str, Any]] = collections.deque(
    maxlen=_FINGERPRINT_LOG_BUFFER_SIZE
)
_fingerprint_log_seq = 0
_fingerprint_log_sink_id: Optional[int] = None
_fingerprint_ws_clients: set = set()
_fingerprint_log_loop: Optional[asyncio.AbstractEventLoop] = None


def _start_log_capture():
    """Add a loguru sink that captures build-related log lines."""
    global _build_log_sink_id
    if _build_log_sink_id is not None:
        return  # already capturing

    _build_logs.clear()

    # Capture the main event loop so we can schedule WS broadcasts
    # from the build thread (which has its own loop).
    try:
        _main_loop = asyncio.get_running_loop()
    except RuntimeError:
        _main_loop = None

    def _sink(message):
        line = str(message).rstrip()
        _build_logs.append(line)
        # Broadcast to WebSocket clients (fire-and-forget)
        if _ws_clients and _main_loop and _main_loop.is_running():
            _main_loop.call_soon_threadsafe(
                _main_loop.create_task, _broadcast_log(line)
            )

    _build_log_sink_id = logger.add(
        _sink,
        format="{time:HH:mm:ss} | {level:<7} | {message}",
        level="DEBUG",
        filter=lambda record: any(
            kw in record["message"]
            for kw in (
                "Fetch",
                "fetch",
                "Tag",
                "tag",
                "Progress",
                "Stor",
                "stor",
                "Generat",
                "generat",
                "Regenerat",
                "regenerat",
                "Dedup",
                "dedup",
                "Skip",
                "skip",
                "BUILD",
                "build",
                "DAILY",
                "Daily",
                "daily",
                "catalog",
                "Catalog",
                "COMPLETE",
                "complete",
                "Failed",
                "failed",
                "Error",
                "error",
                "items",
                "batch",
                "Batch",
                "TMDB",
                "Gemini",
                "gemini",
                "Step",
                "====",
                "Target",
                "Estimated",
                "Duration",
                "remaining",
                "processed",
                "succeeded",
                "paused",
                "Paused",
                "resumed",
                "Resumed",
                "Auto-resum",
            )
        ),
    )


def _stop_log_capture():
    """Remove the loguru sink."""
    global _build_log_sink_id
    if _build_log_sink_id is not None:
        logger.remove(_build_log_sink_id)
        _build_log_sink_id = None


def _record_fingerprint_log(message: Any):
    """Capture fingerprint worker loguru records for admin UI viewing."""
    global _fingerprint_log_seq
    record = getattr(message, "record", None) or {}
    ts = record.get("time")
    entry = {
        "seq": _fingerprint_log_seq,
        "ts": ts.isoformat() if ts else utc_now().isoformat(),
        "level": getattr(record.get("level"), "name", str(record.get("level", "INFO"))),
        "msg": record.get("message", str(message).rstrip()),
    }
    _fingerprint_log_seq += 1
    _fingerprint_logs.append(entry)
    if (
        _fingerprint_ws_clients
        and _fingerprint_log_loop
        and _fingerprint_log_loop.is_running()
    ):
        _fingerprint_log_loop.call_soon_threadsafe(
            _fingerprint_log_loop.create_task, _broadcast_fingerprint_log(entry)
        )


def _ensure_fingerprint_log_capture():
    """Install a bounded loguru sink for fingerprint worker diagnostics."""
    global _fingerprint_log_sink_id
    if _fingerprint_log_sink_id is not None:
        return
    _fingerprint_log_sink_id = logger.add(
        _record_fingerprint_log,
        format="{message}",
        # Honor the same threshold as stderr. At the normal INFO level this
        # keeps the useful phase/season summaries without formatting and
        # retaining hundreds of per-pair diagnostics. DEBUG remains available
        # explicitly when investigating detector behavior.
        level=settings.log_level,
        filter=lambda record: record.get("name") == "workers.fingerprint_worker",
    )


_ensure_fingerprint_log_capture()


# ---- Authentication ----


def _admin_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _check_admin_login_rate_limit(request: Request) -> None:
    # Key on the real client IP. The previous left-most X-Forwarded-For value is
    # client-controlled, so an attacker could bypass this limiter (and brute-force
    # the master password) by rotating a spoofed header per request.
    client = get_client_ip(request)
    now = time.time()
    if len(_ADMIN_LOGIN_ATTEMPTS) > 4096:
        # Drop idle clients so rotating source addresses can't grow this forever.
        for key, window in list(_ADMIN_LOGIN_ATTEMPTS.items()):
            if not window or now - window[-1] > _ADMIN_LOGIN_WINDOW:
                del _ADMIN_LOGIN_ATTEMPTS[key]
    attempts = _ADMIN_LOGIN_ATTEMPTS.setdefault(client, collections.deque())
    while attempts and now - attempts[0] > _ADMIN_LOGIN_WINDOW:
        attempts.popleft()
    if len(attempts) >= _ADMIN_LOGIN_LIMIT:
        raise HTTPException(status_code=429, detail="Too many login attempts")
    attempts.append(now)


def _validate_admin_session_token(token: Optional[str]) -> bool:
    """Validate an admin session token for REST and WebSocket callers."""
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    with get_db() as db:
        token_digest = _admin_token_digest(token)
        session = (
            db.query(AdminSession).filter(AdminSession.token == token_digest).first()
        )
        if not session:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if utc_now() > session.expires_at:
            db.query(AdminSession).filter(AdminSession.token == token_digest).delete()
            raise HTTPException(status_code=401, detail="Session expired")
        if session.user_id is not None:
            # An account-backed admin session ends as soon as that account is
            # demoted or deactivated, not when its 24 hours run out.
            account = db.query(Account).filter(Account.id == session.user_id).first()
            if not account or not account.is_active or not account.is_admin:
                db.query(AdminSession).filter(
                    AdminSession.token == token_digest
                ).delete()
                raise HTTPException(status_code=401, detail="Not authenticated")
    return True


def _check_ws_origin(ws) -> None:
    """Refuse cross-origin WebSocket upgrades that ride the admin cookie.

    The HTTP CSRF guard does not see WebSocket handshakes, and SameSite=Lax
    still sends the cookie from a sibling subdomain.
    """
    origin = ws.headers.get("origin")
    if not origin:
        return
    base = urlparse(settings.base_url)
    if origin.rstrip("/") != f"{base.scheme}://{base.netloc}":
        raise HTTPException(status_code=403, detail="Cross-origin WebSocket refused")


def verify_admin(request: Request):
    """Verify admin authentication via admin or signed-in admin user cookie.

    The ``admin_token`` path (master password) is deliberately left ungated by
    the passkey requirement — it is the break-glass route that keeps the
    exemption controls reachable if an admin cannot enroll. The ``user_session``
    path is an ordinary account sign-in and is gated like every other one.
    """
    admin_token = request.cookies.get("admin_token")
    if admin_token:
        return _validate_admin_session_token(admin_token)

    user_token = request.cookies.get("user_session")
    if user_token:
        with get_db() as db:
            user = verify_user_session(user_token, db)
            if user and user.is_active and user.is_admin:
                requirement = auth_requirement(db, user)
                if requirement != "none":
                    raise HTTPException(
                        status_code=403,
                        detail="Finish account security setup to use the admin area",
                        headers={"X-Auth-Requirement": requirement},
                    )
                return True

    raise HTTPException(status_code=401, detail="Not authenticated")


# ---- Settings helpers ----


def _bounded(attr: str, value):
    """Clamp a runtime override to the range its field validator declares.

    Overrides are applied with ``object.__setattr__`` -- the only way to mutate
    a frozen Pydantic model -- which skips every validator on it. For the
    quality floor that matters: a value out of range empties every row in the
    product and nothing raises, because the query just matches nothing.
    Clamping rather than rejecting so a bad stored value cannot brick startup,
    where this same function runs from ``load_settings_from_db``.
    """
    from app.config import NUMERIC_BOUNDS

    if attr not in NUMERIC_BOUNDS:
        return value
    low, high = NUMERIC_BOUNDS[attr]
    clamped = type(value)(min(max(value, low), high))
    if clamped != value:
        logger.warning(
            f"setting {attr}={value} is outside [{low}, {high}]; using {clamped}"
        )
    return clamped


#: Keys that live in ``admin_settings`` by design and are read straight out of
#: it, never through the config object:
#:
#:   BUILD_PAUSED            app/admin.py -- run state, not configuration
#:   FINGERPRINT_PRESET      workers/fingerprint_worker.py
#:   mdblist_api_key         app/mdblist_client.py
#:   integration_*_v1        app/sources/ -- encrypted media-server settings
#:
#: Installed playback addons keep their own lowercase keys; any key with a
#: lowercase first letter is storage, never configuration, and is skipped too.
#:
#: Listed so the "not a setting" warning below means what it says. That warning
#: exists to catch a control wired to nothing, which is a real bug that shipped;
#: firing it on every boot for six correctly-designed keys would teach the
#: reader to ignore the one case it is for.
_DB_ONLY_SETTINGS = frozenset(
    {
        "BUILD_PAUSED",
        "FINGERPRINT_PRESET",
        "CATALOG_SOURCE_FLOOR_V2",
        "CATALOG_SOURCE_FLOOR_V3",
        "mdblist_api_key",
    }
)


def _apply_setting(key: str, value: str):
    """Apply a setting to the in-memory config object after basic validation."""
    try:
        if key in {"MASTER_PASSWORD", "SECRET_KEY"}:
            raise ValueError(f"{key} cannot be hot-reloaded from the admin UI")
        if (
            key in _DB_ONLY_SETTINGS
            or key.startswith("CATALOG_TOGGLE_RESET_")
            or key[:1].islower()
        ):
            # Nothing to apply: their readers query admin_settings directly.
            return
        attr = key.lower()
        if not hasattr(settings, attr):
            # Loudly, because this silently no-op'ing is exactly how two
            # Feature toggles -- ENABLE_UNIVERSAL_CATALOGS and
            # ENABLE_PERSONALIZED_CATALOGS -- shipped in the dashboard, the
            # README and .env.example while being fields on nothing. They wrote
            # admin_settings rows no code read, and rendered OFF because the
            # read path fell back to "" for a missing attribute.
            raise ValueError(f"{key} is not a setting; nothing will read it")
        if hasattr(settings, attr):
            current = getattr(settings, attr)
            if isinstance(current, bool):
                object.__setattr__(
                    settings, attr, value.lower() in ("true", "1", "yes")
                )
            elif isinstance(current, int):
                object.__setattr__(settings, attr, _bounded(attr, int(value)))
            elif isinstance(current, float):
                object.__setattr__(settings, attr, _bounded(attr, float(value)))
            else:
                object.__setattr__(settings, attr, value)

        # Provider clients carry their configured model/key. Keep the selected
        # singleton synchronized with hot-reloaded Admin Center settings.
        if key in {
            "TAGGING_PROVIDER",
            "GEMINI_MODEL",
            "GEMINI_API_KEY",
            "OPENAI_MODEL",
            "OPENAI_API_KEY",
            "ANTHROPIC_MODEL",
            "ANTHROPIC_API_KEY",
        }:
            from app.tagging_provider import reconfigure_tagging_engines

            reconfigure_tagging_engines()
    except Exception as e:
        logger.warning(f"Failed to hot-reload setting {key}: {e}")


#: Third-party provider API keys. Stored Fernet-encrypted in admin_settings
#: (``v2:`` prefix from ``app.crypto``); older plaintext rows are re-encrypted
#: the next time settings load.
_SECRET_SETTING_KEYS = frozenset(
    {
        "TMDB_API_KEY",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "LOCAL_API_KEY",
    }
)
_ENCRYPTED_PREFIX = "v2:"


def _stored_setting_value(key: str, value: str) -> str:
    """Return the value to persist in admin_settings for ``key``."""
    if key in _SECRET_SETTING_KEYS and value:
        return encrypt_token(value)
    return value


def _setting_plaintext(key: str, stored: str | None) -> str:
    """Return the usable value of an admin_settings row."""
    if stored is None:
        return ""
    if key in _SECRET_SETTING_KEYS and stored.startswith(_ENCRYPTED_PREFIX):
        try:
            return decrypt_token(stored)
        except Exception:
            # Usually SECRET_KEY / SECURITY_ENCRYPTION_KEYS changed. Fall back to
            # the environment value rather than applying ciphertext as a key.
            logger.warning(f"Could not decrypt stored {key}; ignoring it")
            return ""
    return stored


def load_settings_from_db():
    """Load admin settings from database and apply to in-memory config."""
    try:
        with get_db() as db:
            db.query(AdminSetting).filter(
                AdminSetting.key.in_(("MASTER_PASSWORD", "INSTALL_TOKEN"))
            ).delete(synchronize_session=False)
            # ADDON_NAME was renamed APP_NAME; carry a saved value across once.
            legacy_name = (
                db.query(AdminSetting).filter(AdminSetting.key == "ADDON_NAME").first()
            )
            if legacy_name is not None:
                if db.get(AdminSetting, "APP_NAME") is None:
                    db.add(AdminSetting(key="APP_NAME", value=legacy_name.value))
                db.delete(legacy_name)
                db.flush()
            for setting in db.query(AdminSetting).all():
                value = _setting_plaintext(setting.key, setting.value)
                if (
                    setting.key in _SECRET_SETTING_KEYS
                    and value
                    and not setting.value.startswith(_ENCRYPTED_PREFIX)
                ):
                    setting.value = _stored_setting_value(setting.key, value)
                if value:
                    _apply_setting(setting.key, value)
                elif setting.key not in _SECRET_SETTING_KEYS:
                    _apply_setting(setting.key, setting.value)
            count = db.query(func.count(AdminSetting.key)).scalar() or 0
            if count:
                logger.info(f"Loaded {count} admin settings from database")
    except Exception as e:
        logger.warning(f"Could not load admin settings from DB: {e}")


def _mask(value: str) -> str:
    """Mask a sensitive value, showing only its last 4 chars."""
    if not value:
        return ""
    if len(value) <= 12:
        return "********"
    return "********" + value[-4:]


# ---- API Routes ----


def _store_admin_passkey_challenge(
    db, *, challenge: bytes, account: "Account | None"
) -> str:
    """Persist a short-lived passkey challenge for an admin login attempt."""
    token = secrets.token_urlsafe(32)
    db.add(
        PasskeyChallenge(
            token=token,
            challenge=b64url_encode(challenge),
            intent="admin_login",
            account_id=account.id if account else None,
            email=account.email if account else None,
            expires_at=utc_now() + timedelta(minutes=5),
        )
    )
    db.commit()
    return token


def _get_admin_passkey_challenge(db, token: str) -> PasskeyChallenge:
    """Load and validate a pending admin passkey challenge."""
    challenge = (
        db.query(PasskeyChallenge)
        .filter(
            PasskeyChallenge.token == token,
            PasskeyChallenge.intent == "admin_login",
        )
        .first()
    )
    if not challenge or challenge.expires_at < utc_now():
        raise HTTPException(status_code=400, detail="Passkey challenge expired")
    return challenge


def _issue_admin_session_response(
    request: Request, user_id: int | None = None
) -> JSONResponse:
    """Create an admin session cookie for a verified admin principal."""
    token = secrets.token_hex(32)
    expires_at = utc_now() + SESSION_DURATION
    with get_db() as db:
        db.query(AdminSession).filter(AdminSession.expires_at < utc_now()).delete()
        db.add(
            AdminSession(
                token=_admin_token_digest(token),
                user_id=user_id,
                expires_at=expires_at,
            )
        )
        db.commit()
    response = JSONResponse({"status": "ok"})
    response.set_cookie(
        key="admin_token",
        value=token,
        httponly=True,
        secure=_cookie_secure(request),
        max_age=int(SESSION_DURATION.total_seconds()),
        samesite="lax",
    )
    return response


@router.post("/api/passkeys/login/begin")
async def admin_passkey_login_begin(request: Request):
    """Start passkey-first admin authentication for an active admin user."""
    _check_admin_login_rate_limit(request)
    body = await request.json()
    email = body.get("email", "").strip().lower()

    webauthn = load_webauthn()
    cfg = get_webauthn_config(request)
    with get_db() as db:
        account = (
            db.query(Account).filter(Account.email == email).first() if email else None
        )
        # Never reveal whether an email belongs to an admin: an unknown email,
        # a non-admin, or an admin without passkeys all fall back to the
        # discoverable-credential flow, and finish still requires an admin.
        if account and (not account.is_active or not account.is_admin):
            account = None
        credentials = (
            db.query(PasskeyCredential)
            .filter(PasskeyCredential.account_id == account.id)
            .all()
            if account
            else []
        )
        if not credentials:
            account = None

        allow_credentials = None
        if account:
            allow_credentials = [
                webauthn["PublicKeyCredentialDescriptor"](
                    id=b64url_decode(cred.credential_id)
                )
                for cred in credentials
            ]

        options_kwargs = {
            "rp_id": cfg.rp_id,
            "user_verification": webauthn["UserVerificationRequirement"].REQUIRED,
        }
        if allow_credentials is not None:
            options_kwargs["allow_credentials"] = allow_credentials
        options = webauthn["generate_authentication_options"](**options_kwargs)
        token = _store_admin_passkey_challenge(
            db, challenge=options.challenge, account=account
        )
    return {
        "token": token,
        "options": options_to_dict(options, webauthn["options_to_json"]),
    }


@router.post("/api/passkeys/login/finish")
async def admin_passkey_login_finish(request: Request):
    """Verify an admin passkey assertion and issue an admin session cookie."""
    _check_admin_login_rate_limit(request)
    body = await request.json()
    webauthn = load_webauthn()
    cfg = get_webauthn_config(request)
    token = body.get("token", "")
    credential = body.get("credential") or {}
    credential_id = credential.get("id") or credential.get("rawId")
    if not token or not credential_id:
        raise HTTPException(status_code=400, detail="Missing passkey response")

    try:
        normalized_credential_id = b64url_encode(b64url_decode(credential_id))
    except Exception:
        normalized_credential_id = credential_id

    with get_db() as db:
        challenge = _get_admin_passkey_challenge(db, token)
        stored = (
            db.query(PasskeyCredential)
            .filter(PasskeyCredential.credential_id == normalized_credential_id)
            .first()
        )
        if not stored or (
            challenge.account_id and stored.account_id != challenge.account_id
        ):
            raise HTTPException(status_code=401, detail="Unknown passkey")
        account = db.query(Account).filter(Account.id == stored.account_id).first()
        if not account or not account.is_active:
            raise HTTPException(status_code=403, detail="Account is deactivated")
        if not account.is_admin:
            raise HTTPException(status_code=403, detail="Not an admin")
        try:
            verification = webauthn["verify_authentication_response"](
                credential=credential_from_dict(
                    webauthn["AuthenticationCredential"], credential
                ),
                expected_challenge=b64url_decode(challenge.challenge),
                expected_origin=cfg.origin,
                expected_rp_id=cfg.rp_id,
                credential_public_key=b64url_decode(stored.public_key),
                credential_current_sign_count=stored.sign_count,
                require_user_verification=True,
            )
        except Exception as exc:
            logger.warning(
                "Admin passkey verification failed for account_id={} origin={} rp_id={}: {}",
                account.id,
                cfg.origin,
                cfg.rp_id,
                exc,
            )
            raise HTTPException(status_code=401, detail="Passkey login failed") from exc
        stored.sign_count = verification.new_sign_count or stored.sign_count
        stored.last_used_at = utc_now()
        account.last_login = utc_now()
        db.delete(challenge)
        db.commit()
        account_id = account.id
    return _issue_admin_session_response(request, user_id=account_id)


@router.post("/api/login")
async def admin_login(request: Request):
    """Authenticate via user credentials (email + password + 2FA) or master password.

    User-based login: send {"email", "password", "totp_code"}.
    Master password fallback: send {"password"} only (no email).
    """
    _check_admin_login_rate_limit(request)
    body = await request.json()
    email = body.get("email", "").strip().lower()
    password = body.get("password", "")
    totp_code = body.get("totp_code", "")

    user_id = None

    if email:
        # --- Account-based admin login ---
        with get_db() as db:
            account = db.query(Account).filter(Account.email == email).first()
            if not account or not account.password_hash:
                await asyncio.to_thread(dummy_verify_password, password)
                raise HTTPException(status_code=401, detail="Invalid credentials")
            if not await asyncio.to_thread(
                verify_password, password, account.password_hash
            ):
                raise HTTPException(status_code=401, detail="Invalid credentials")
            if not account.is_admin:
                raise HTTPException(status_code=403, detail="Not an admin")
            if not account.is_active:
                raise HTTPException(status_code=403, detail="Account is deactivated")
            # Only the master password is break-glass; an account sign-in must
            # have finished its passkey/2FA setup like any other sign-in.
            requirement = auth_requirement(db, account)
            if requirement != "none":
                raise HTTPException(
                    status_code=403,
                    detail="Finish account security setup to use the admin area",
                    headers={"X-Auth-Requirement": requirement},
                )
            if account.totp_enabled:
                if not totp_code:
                    raise HTTPException(status_code=401, detail="2FA code required")
                if not verify_account_totp(account, totp_code):
                    raise HTTPException(status_code=401, detail="Invalid 2FA code")
            user_id = account.id
    else:
        # --- Master password fallback ---
        if not secrets.compare_digest(
            password.encode("utf-8"), settings.master_password.encode("utf-8")
        ):
            raise HTTPException(status_code=401, detail="Invalid password")

    return _issue_admin_session_response(request, user_id=user_id)


@router.post("/api/logout")
async def admin_logout(request: Request):
    """Clear admin session."""
    token = request.cookies.get("admin_token")
    if token:
        with get_db() as db:
            db.query(AdminSession).filter(
                AdminSession.token == _admin_token_digest(token)
            ).delete()
    response = JSONResponse({"status": "ok"})
    response.delete_cookie("admin_token")
    return response


@router.get("/api/stats")
def get_stats(request: Request, _=Depends(verify_admin)):
    """Get database statistics and recent job history."""
    with get_db() as db:
        stats = {
            "movies_tagged": db.query(func.count(func.distinct(MovieTag.tmdb_id)))
            .filter(MovieTag.media_type == "movie")
            .scalar()
            or 0,
            "shows_tagged": db.query(func.count(func.distinct(MovieTag.tmdb_id)))
            .filter(MovieTag.media_type == "tv")
            .scalar()
            or 0,
            "total_tags": db.query(func.count(Tag.id)).scalar() or 0,
            "total_metadata": db.query(func.count(MediaMetadata.tmdb_id)).scalar() or 0,
            "active_categories": db.query(func.count(UniversalCategory.id))
            .filter(UniversalCategory.is_active.is_(True))
            .scalar()
            or 0,
            "catalog_items": db.query(
                func.count(UniversalCatalogContent.tmdb_id)
            ).scalar()
            or 0,
            "total_users": db.query(func.count(User.id)).scalar() or 0,
        }

        jobs = (
            db.query(TaggingJob).order_by(TaggingJob.started_at.desc()).limit(20).all()
        )
        stats["recent_jobs"] = [
            {
                "id": j.id,
                "job_type": j.job_type,
                "status": j.status,
                "started_at": j.started_at.isoformat() if j.started_at else None,
                "completed_at": j.completed_at.isoformat() if j.completed_at else None,
                "items_processed": j.items_processed,
                "items_failed": j.items_failed,
                "error_message": (j.error_message or "")[:200],
            }
            for j in jobs
        ]

        running_job = (
            db.query(TaggingJob).filter(TaggingJob.status == "running").first()
        )
        stats["build_running"] = running_job is not None

    return stats


@router.get("/api/details/diagnose")
def diagnose_detail(
    tmdb_id: int,
    media_type: str = "tv",
    _=Depends(verify_admin),
):
    """Explain, without any provider call, why one title's episodes are missing.

    Reports every stored fact the details path consults, so "which of the several
    producers is firing for THIS show" is one request instead of a log hunt.
    """
    from app import main

    if media_type not in ("movie", "tv"):
        raise HTTPException(status_code=400, detail="media_type must be movie or tv")

    with get_db() as db:
        resolved_type, reason = main._stored_detail_media_type_with_reason(
            db, tmdb_id, media_type  # type: ignore[arg-type]
        )
        metadata_rows = [
            {
                "media_type": str(row.media_type),
                "title": row.title,
                "imdb_id": row.imdb_id,
                "number_of_seasons": row.number_of_seasons,
                "number_of_episodes": row.number_of_episodes,
                "next_episode_air_date": (
                    row.next_episode_air_date.isoformat()
                    if row.next_episode_air_date
                    else None
                ),
            }
            for row in db.query(MediaMetadata)
            .filter(
                MediaMetadata.tmdb_id == tmdb_id,
                MediaMetadata.media_type.in_(["movie", "tv"]),
            )
            .all()
        ]
        imdb_ids = [row["imdb_id"] for row in metadata_rows if row["imdb_id"]]
        resolutions = [
            {
                "imdb_id": row.imdb_id,
                "tmdb_id": row.tmdb_id,
                "media_type": row.media_type,
                "status": row.status,
            }
            for row in (
                db.query(ImdbResolution)
                .filter(ImdbResolution.imdb_id.in_(imdb_ids))
                .all()
                if imdb_ids
                else []
            )
        ]

        snapshots = {}
        for candidate in ("movie", "tv"):
            row = db.get(DetailSnapshot, (tmdb_id, candidate))
            if row is None:
                continue
            payload = row.payload if isinstance(row.payload, dict) else {}
            snapshots[candidate] = {
                "episodes_complete_column": bool(row.episodes_complete),
                "schema_version": row.schema_version,
                "refreshed_at": (
                    row.refreshed_at.isoformat() if row.refreshed_at else None
                ),
                "last_accessed_at": (
                    row.last_accessed_at.isoformat() if row.last_accessed_at else None
                ),
                "payload_episodes_complete": payload.get("episodes_complete"),
                "seasons": len(payload.get("seasons") or []),
                "usable_episodes": main._detail_usable_episode_count(payload),
                "expected_episodes": main._detail_expected_regular_episode_count(
                    payload
                ),
                "has_provider_detail": isinstance(
                    payload.get("_provider_detail"), dict
                ),
                "provider_confirms_no_regular_episodes": (
                    main._provider_confirms_no_regular_episodes(payload)
                ),
                "is_complete": main._detail_snapshot_is_complete(payload, candidate),
            }

    return {
        "tmdb_id": tmdb_id,
        "requested_media_type": media_type,
        "resolved_media_type": resolved_type,
        "resolution_reason": reason,
        "media_metadata": metadata_rows,
        "imdb_resolutions": resolutions,
        "detail_snapshots": snapshots,
        "memory_cached": {
            candidate: f"web-detail:{candidate}:{tmdb_id}" in main._meta_cache
            for candidate in ("movie", "tv")
        },
        "overlay_cached": {
            candidate: main._detail_response_cache_get(tmdb_id, candidate) is not None
            for candidate in ("movie", "tv")
        },
    }


@router.get("/api/details/episodeless")
def list_episodeless_details(limit: int = 50, _=Depends(verify_admin)):
    """Every stored TV snapshot that claims completeness but has no episodes.

    Run before and after a fix: the count is the blast radius, and it is also the
    number of titles that will rebuild against TMDB as users open them.
    """
    from app import main

    limit = max(1, min(limit, 500))
    sample: list[dict] = []
    scanned = 0
    affected = 0
    with get_db() as db:
        rows = (
            db.query(DetailSnapshot)
            .filter(
                DetailSnapshot.media_type == "tv",
                DetailSnapshot.episodes_complete.is_(True),
            )
            .yield_per(200)
        )
        for row in rows:
            scanned += 1
            payload = row.payload if isinstance(row.payload, dict) else {}
            if main._detail_usable_episode_count(payload) > 0:
                continue
            if main._provider_confirms_no_regular_episodes(payload):
                continue
            affected += 1
            if len(sample) < limit:
                item = payload.get("item") or {}
                sample.append(
                    {
                        "tmdb_id": row.tmdb_id,
                        "title": item.get("title"),
                        "seasons": len(payload.get("seasons") or []),
                        "expected_episodes": (
                            main._detail_expected_regular_episode_count(payload)
                        ),
                        "refreshed_at": (
                            row.refreshed_at.isoformat() if row.refreshed_at else None
                        ),
                    }
                )

    return {"scanned": scanned, "affected": affected, "sample": sample}


@router.get("/api/overview")
def admin_overview(request: Request, _=Depends(verify_admin)):
    """One short-lived operational snapshot for the admin home screen."""
    global _overview_cache
    now = time.monotonic()
    if _overview_cache and now - _overview_cache[0] < _OVERVIEW_TTL_SECONDS:
        return _overview_cache[1]

    stats = get_stats(request)
    metrics = system_metrics(request)
    streams = active_streams(request)
    from app.sources import registry as source_registry

    media_server_connected = any(
        p.kind == "library" for p in source_registry.enabled_providers()
    )
    setup = {
        "credentials": bool(
            settings.tmdb_api_key
            and settings.gemini_api_key
            and settings.tmdb_api_key != "your_tmdb_api_key_here"
            and settings.gemini_api_key != "your_gemini_api_key_here"
        ),
        "media_server": media_server_connected,
        "initial_build": int(stats["total_metadata"]) > 0,
        "first_user": int(stats["total_users"]) > 0,
    }
    warnings = []
    if not setup["credentials"]:
        warnings.append("API credentials need attention")
    if not setup["media_server"]:
        warnings.append("Connect Plex or Jellyfin to play titles you own")
    if not setup["initial_build"]:
        warnings.append("The initial catalog build has not completed")

    payload = {
        "generated_at": utc_now().isoformat(),
        "setup": setup,
        "ready": all(setup.values()),
        "warnings": warnings,
        "counts": {
            key: value
            for key, value in stats.items()
            if key not in {"recent_jobs", "build_running"}
        },
        "jobs": {
            "running": stats["build_running"],
            "recent": stats["recent_jobs"],
        },
        "streams": streams["streams"],
        "health": {
            "caches": metrics["caches"],
            "tmdb": metrics["tmdb"],
            "sync": metrics["sync"],
            "user_sync": metrics["user_sync"],
        },
    }
    _overview_cache = (now, payload)
    return payload


@router.post("/api/relevance/refresh")
def refresh_relevance(request: Request, _=Depends(verify_admin)):
    """Recompute IDF weights across the library.

    Ranking reads ``movie_tags.weight_norm``, which is written by
    ``refresh_tag_stats`` rather than at tag-insert time. Until this endpoint
    existed the only ways to recompute it were a full Upgrade run or a shell
    into the container for ``workers/refresh_relevance.py`` -- so a library that
    had drifted (new titles, promoted aliases, a changed taxonomy) could not be
    re-weighted without also re-running enrichment and tagging.

    Cheap: aggregates over ``movie_tags`` and chunked UPDATEs, no API calls.
    """
    from workers.daily_update import refresh_relevance_threaded

    stats = refresh_relevance_threaded("admin_relevance")
    logger.info(f"relevance refresh: {stats}")
    return {"status": "ok", "stats": stats}


# ---------------------------------------------------------------------------
# Catalog console — what the rows actually contain, and what the floor keeps
#
# There was no surface anywhere for rows. Synthesis could be previewed, and the
# quality floor could be typed into a settings box, but nothing could answer
# the two questions an operator actually has: "what is IN this row?" and "what
# does that number cost me?" Both are cheap to answer and neither was.
# ---------------------------------------------------------------------------
@router.get("/api/catalog/rows")
def catalog_rows(request: Request, _=Depends(verify_admin)):
    """Every active row with its stored size and its formula.

    Sizes come from the stored ``universal_catalog_content`` snapshot rather
    than from re-running the generator: that is what is being *served*, which
    is the thing worth showing. A row sitting at 0 has been generated and came
    up empty -- almost always a formula whose tags no longer exist, or a floor
    that excluded everything.
    """
    from workers.personalized_sync import VALID_SLOTS

    with get_db() as db:
        counts = dict(
            db.query(
                UniversalCatalogContent.category_id,
                func.count(UniversalCatalogContent.tmdb_id),
            )
            .group_by(UniversalCatalogContent.category_id)
            .all()
        )
        rows = (
            db.query(UniversalCategory)
            .filter(
                UniversalCategory.is_active.is_(True),
                UniversalCategory.source == "generated",
            )
            .order_by(UniversalCategory.sort_order)
            .all()
        )
        payload = []
        for row in rows:
            formula = row.tag_formula or {}
            item_count = int(counts.get(row.id, 0))
            payload.append(
                {
                    "id": row.id,
                    "name": row.name,
                    "media_type": row.media_type,
                    "source": row.source or "curated",
                    "items": item_count,
                    "servable": item_count >= 24,
                    "mandatory": formula.get("mandatory", []),
                    "required": formula.get("required", []),
                    "optional": formula.get("optional", []),
                    "min_required": formula.get(
                        "min_required", len(formula.get("required", []))
                    ),
                }
            )
        personal_rows = (
            db.query(func.count(UserCatalog.id))
            .filter(
                UserCatalog.is_active.is_(True),
                UserCatalog.slot_id.in_(VALID_SLOTS),
            )
            .scalar()
            or 0
        )
        personal_profiles = (
            db.query(func.count(func.distinct(UserCatalog.user_id)))
            .filter(
                UserCatalog.is_active.is_(True),
                UserCatalog.slot_id.in_(VALID_SLOTS),
            )
            .scalar()
            or 0
        )
        personal = []
        for catalog, count, profile_name in (
            db.query(
                UserCatalog,
                func.count(UserCatalogContent.tmdb_id),
                User.display_name,
            )
            .join(User, User.id == UserCatalog.user_id)
            .outerjoin(
                UserCatalogContent, UserCatalogContent.catalog_id == UserCatalog.id
            )
            .filter(
                UserCatalog.is_active.is_(True),
                UserCatalog.slot_id.in_(VALID_SLOTS),
            )
            .group_by(UserCatalog.id, User.display_name)
            .order_by(UserCatalog.last_generated.desc())
            .limit(200)
            .all()
        ):
            params = catalog.generation_params or {}
            personal.append(
                {
                    "id": catalog.id,
                    "user_id": catalog.user_id,
                    "profile_name": profile_name or f"Profile {catalog.user_id}",
                    "slot_id": catalog.slot_id,
                    "name": catalog.name,
                    "media_type": catalog.media_type,
                    "generation_method": catalog.generation_method,
                    "items": int(count or 0),
                    "servable": int(count or 0) >= 24,
                    "revision": params.get("generation_revision")
                    or catalog.last_generated.isoformat(),
                    "seed": params.get("seed"),
                    "basis_tags": params.get("basis_tags") or [],
                }
            )
    return {
        "count": len(payload),
        "rows": payload,
        "personal_rows": int(personal_rows),
        "personal_profiles": int(personal_profiles),
        "personal": personal,
        "suppressed_rows": sum(1 for row in payload if not row["servable"])
        + sum(1 for row in personal if not row["servable"]),
    }


@router.get("/api/catalog/personal/{catalog_id}/preview")
def personal_catalog_preview(
    catalog_id: int,
    request: Request,
    limit: int = 100,
    _=Depends(verify_admin),
):
    """Inspect one personal row's seed, basis and persisted per-title scores."""
    limit = max(1, min(limit, 200))
    with get_db() as db:
        catalog = db.get(UserCatalog, catalog_id)
        if catalog is None:
            raise HTTPException(status_code=404, detail="Unknown personal row")
        rows = (
            db.query(UserCatalogContent, MediaMetadata)
            .outerjoin(
                MediaMetadata,
                and_(
                    MediaMetadata.tmdb_id == UserCatalogContent.tmdb_id,
                    MediaMetadata.media_type == UserCatalogContent.media_type,
                ),
            )
            .filter(UserCatalogContent.catalog_id == catalog.id)
            .order_by(UserCatalogContent.rank)
            .limit(limit)
            .all()
        )
        params = catalog.generation_params or {}
        return {
            "id": catalog.id,
            "user_id": catalog.user_id,
            "slot_id": catalog.slot_id,
            "name": catalog.name,
            "media_type": catalog.media_type,
            "generation_method": catalog.generation_method,
            "seed": params.get("seed"),
            "basis_tags": params.get("basis_tags") or [],
            "algorithm_version": params.get("algorithm_version"),
            "revision": params.get("generation_revision")
            or catalog.last_generated.isoformat(),
            "items": [
                {
                    "tmdb_id": int(content.tmdb_id),
                    "title": metadata.title if metadata else f"TMDB {content.tmdb_id}",
                    "year": (metadata.release_date or "")[:4] if metadata else "",
                    "rank": int(content.rank),
                    "similarity": round(float(content.match_score or 0.0), 4),
                }
                for content, metadata in rows
            ],
        }


@router.get("/api/catalog/rows/{category_id}/preview")
def catalog_row_preview(
    category_id: str,
    request: Request,
    limit: int = 30,
    _=Depends(verify_admin),
):
    """What is in a row, with the score it was ranked by and the tags it matched.

    The "why is this here?" view. A row whose header disagrees with its contents
    is the hardest thing to diagnose from the outside, because every layer looks
    right in isolation -- the formula is sane, the tags are sane, the ordering is
    sane. Seeing the matched tags next to each title makes the mismatch obvious
    in one glance.
    """
    from app.catalog_generator import CatalogGenerator, _quality_value

    limit = max(1, min(limit, 100))
    with get_db() as db:
        category = (
            db.query(UniversalCategory)
            .filter(UniversalCategory.id == category_id)
            .first()
        )
        if not category:
            raise HTTPException(status_code=404, detail="Unknown row")

        ranked = CatalogGenerator(db).generate_universal_catalog(category, limit=limit)
        ids = [tmdb_id for tmdb_id, _ in ranked]
        if not ids:
            return {
                "id": category.id,
                "name": category.name,
                "media_type": category.media_type,
                "formula": category.tag_formula or {},
                "items": [],
            }

        meta = {
            row.tmdb_id: row
            for row in db.query(MediaMetadata)
            .options(defer(MediaMetadata.raw_data))
            .filter(
                MediaMetadata.media_type == category.media_type,
                MediaMetadata.tmdb_id.in_(ids),
            )
            .all()
        }
        formula = category.tag_formula or {}
        formula_names = set(
            formula.get("mandatory", [])
            + formula.get("required", [])
            + formula.get("optional", [])
        )
        hits: dict[int, list[str]] = {}
        for tmdb_id, name in (
            db.query(MovieTag.tmdb_id, Tag.name)
            .join(Tag, Tag.id == MovieTag.tag_id)
            .filter(
                MovieTag.media_type == category.media_type,
                MovieTag.tmdb_id.in_(ids),
                Tag.name.in_(formula_names) if formula_names else Tag.id.is_(None),
            )
            .all()
        ):
            hits.setdefault(int(tmdb_id), []).append(name)

        items = []
        for tmdb_id, score in ranked:
            row = meta.get(tmdb_id)
            items.append(
                {
                    "tmdb_id": tmdb_id,
                    "title": row.title if row else f"TMDB {tmdb_id}",
                    "year": (row.release_date or "")[:4] if row else "",
                    "quality": round(_quality_value(row), 2) if row else None,
                    "rating_source": (
                        "consensus"
                        if row is not None and row.rating_consensus is not None
                        else "tmdb"
                    ),
                    "votes": int(row.vote_count or 0) if row else 0,
                    "score": round(float(score), 4),
                    # Sorted so the row's own subject reads first and a title
                    # that matched only a modifier is visible at a glance.
                    "matched": sorted(hits.get(tmdb_id, [])),
                }
            )
    return {
        "id": category.id,
        "name": category.name,
        "media_type": category.media_type,
        "formula": formula,
        "items": items,
    }


@router.get("/api/catalog/quality/preview")
def catalog_quality_preview(
    request: Request,
    min_quality: float | None = None,
    min_vote_count: int | None = None,
    min_rating_sources: int | None = None,
    niche_min_quality: float | None = None,
    niche_min_vote_count: int | None = None,
    niche_min_age_days: int | None = None,
    _=Depends(verify_admin),
):
    """How many titles a proposed floor would keep, per media type.

    The floor is the single most consequential setting in the product and it
    was three numbers in a text box with no way to know what they did short of
    saving them, regenerating every catalog, and looking at the result. This
    evaluates the candidate predicate directly against ``media_metadata``.

    Reads the *proposed* values without persisting them. Candidate values are
    passed into the predicate builder directly, so a preview can never leak a
    temporary floor into a concurrent catalog request.
    """
    from app.catalog_generator import quality_floor_clauses

    applied = {
        "catalog_min_quality": (
            settings.catalog_min_quality if min_quality is None else min_quality
        ),
        "catalog_min_vote_count": (
            settings.catalog_min_vote_count
            if min_vote_count is None
            else min_vote_count
        ),
        "catalog_min_rating_sources": (
            settings.catalog_min_rating_sources
            if min_rating_sources is None
            else min_rating_sources
        ),
        "catalog_niche_min_quality": (
            settings.catalog_niche_min_quality
            if niche_min_quality is None
            else niche_min_quality
        ),
        "catalog_niche_min_vote_count": (
            settings.catalog_niche_min_vote_count
            if niche_min_vote_count is None
            else niche_min_vote_count
        ),
        "catalog_niche_min_age_days": (
            settings.catalog_niche_min_age_days
            if niche_min_age_days is None
            else niche_min_age_days
        ),
    }
    clauses = quality_floor_clauses(
        min_vote_count=applied["catalog_min_vote_count"],
        min_rating_sources=applied["catalog_min_rating_sources"],
        min_quality=applied["catalog_min_quality"],
        niche_min_quality=applied["catalog_niche_min_quality"],
        niche_min_vote_count=applied["catalog_niche_min_vote_count"],
        niche_min_age_days=applied["catalog_niche_min_age_days"],
    )
    with get_db() as db:
        out = {}
        for media_type in ("movie", "tv"):
            total = (
                db.query(func.count(MediaMetadata.tmdb_id))
                .filter(MediaMetadata.media_type == media_type)
                .scalar()
                or 0
            )
            query = db.query(func.count(MediaMetadata.tmdb_id)).filter(
                MediaMetadata.media_type == media_type
            )
            if clauses:
                query = query.filter(*clauses)
            kept = query.scalar() or 0
            out[media_type] = {
                "total": int(total),
                "kept": int(kept),
                "pct": round(100.0 * kept / total, 1) if total else 0.0,
            }
    return {
        "applied": applied,
        "counts": out,
    }


@router.get("/api/categories/synth/preview")
def preview_synth_categories(
    request: Request,
    limit: int = 70,
    _=Depends(verify_admin),
):
    """Compile the next snapshot without publishing it.

    The co-occurrence self-join is minutes on a multi-million-row
    ``movie_tags``, so this is deliberately a button rather than something the
    dashboard polls. When the editor is enabled, changed summaries may make a
    bounded paid call and their reusable decisions are cached; serving rows are
    never mutated by preview.
    """
    from app.category_synth import MIN_LIFT, compile_shelves
    from app.shelf_editor import ShelfEditorialError

    limit = max(1, min(limit, 200))
    with get_db() as db:
        try:
            compilation = compile_shelves(db, limit)
        except ShelfEditorialError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    rows = [
        {
            "id": shelf.spec.slug,
            "deterministic_name": shelf.spec.name,
            "name": shelf.display_name,
            "media_type": shelf.spec.media_type,
            "tags": [shelf.spec.tag_a, shelf.spec.tag_b],
            "facets": [shelf.spec.facet_a, shelf.spec.facet_b],
            "support": shelf.spec.support,
            "lift": shelf.spec.lift,
            "actual_item_count": len(shelf.ranked),
            "decision": "keep",
            "editorial_score": shelf.editorial_score,
            "reason_codes": list(shelf.reason_codes),
            "sample_titles": list(shelf.sample_titles),
            "cache_status": "cached" if shelf.decision_cached else "new",
        }
        for shelf in compilation.shelves
    ]
    return {
        "min_lift": MIN_LIFT,
        "count": len(rows),
        "rows": rows,
        "rejected": list(compilation.rejected),
        "cost_usd": (compilation.editorial.cost_usd if compilation.editorial else 0.0),
        "estimated_cost_usd": (
            compilation.editorial.reserved_cost_usd if compilation.editorial else 0.0
        ),
        "cached": compilation.editorial.cached if compilation.editorial else 0,
        "reviewed": compilation.editorial.reviewed if compilation.editorial else 0,
    }


@router.post("/api/categories/synth/apply")
def apply_synth_categories(
    request: Request,
    limit: int = 70,
    _=Depends(verify_admin),
):
    """Compile, validate, and atomically publish synthesized rows and content.

    Only touches ``source='generated'`` rows. A thin/duplicate/invalid concept,
    editor failure, or budget failure leaves the currently published snapshot
    untouched.
    """
    from app.category_synth import rebuild_synthesized_shelves
    from workers.upgrade import require_catalog_publication_ready

    limit = max(1, min(limit, 200))
    with get_db() as db:
        try:
            require_catalog_publication_ready(db)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            stats = rebuild_synthesized_shelves(db, limit)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    # Deactivating a row has to reach the serving layer immediately: its
    # content rows survive by design, so a cached catalog would keep serving a
    # row the operator just retired.
    _clear_catalog_cache("category synthesis")
    logger.info(f"category synthesis: {stats}")
    return {"status": "ok", "stats": stats}


# Which shelves an install wants is an editorial decision, so these live in
# admin_settings rather than the environment: changing them must not need a
# redeploy, and the default must be "no exclusions".
_SYNTH_CONFIG_FIELDS: tuple[tuple[str, str], ...] = (
    ("excluded_tags", "SYNTH_EXCLUDED_TAGS"),
    ("excluded_words", "SYNTH_EXCLUDED_WORDS"),
    ("rotation_days", "SYNTH_ROTATION_DAYS"),
    ("editor_enabled", "SYNTH_EDITOR_ENABLED"),
    ("editor_provider", "SYNTH_EDITOR_PROVIDER"),
    ("editor_model", "SYNTH_EDITOR_MODEL"),
    ("editor_cost_ceiling_usd", "SYNTH_EDITOR_COST_CEILING_USD"),
)


def _csv_items(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


@router.get("/api/categories/synth/config")
def get_synth_config(request: Request, _=Depends(verify_admin)):
    """The editorial controls over generated rows.

    ``known_tags`` is the taxonomy's own name list, so the console can offer a
    picker instead of asking the operator to spell hundreds of tag names correctly.
    """
    from app.shelf_editor import editor_model, editor_provider, editor_provider_options
    from app.taxonomy import TAXONOMY

    provider = editor_provider()

    return {
        "excluded_tags": _csv_items(settings.synth_excluded_tags),
        "excluded_words": _csv_items(settings.synth_excluded_words),
        "rotation_days": settings.synth_rotation_days,
        "editor_enabled": settings.synth_editor_enabled,
        "editor_provider": provider,
        "editor_model": editor_model(provider),
        "editor_cost_ceiling_usd": settings.synth_editor_cost_ceiling_usd,
        "providers": editor_provider_options(),
        "known_tags": sorted({t.name for t in TAXONOMY}),
    }


@router.post("/api/categories/synth/config")
async def set_synth_config(request: Request, _=Depends(verify_admin)):
    """Store the exclusions and the rotation period, and apply them live.

    Deliberately not routed through ``POST /api/settings``: that endpoint skips
    empty values, so it can set a list but never clear one. Clearing back to no
    exclusions is the default state and has to be reachable.

    Unrecognised tag names are stored anyway and reported back as ``unknown``.
    A name that matches nothing simply excludes nothing, and refusing it would
    break the operator who is excluding a tag from a taxonomy version they have
    not deployed yet.
    """
    from app.taxonomy import BY_NAME

    body = await request.json()
    saved: list[str] = []
    with get_db() as db:
        for field, setting_key in _SYNTH_CONFIG_FIELDS:
            if field not in body:
                continue
            raw = body.get(field)
            if field == "rotation_days":
                try:
                    value = str(max(0, int(raw)))
                except (TypeError, ValueError):
                    raise HTTPException(
                        status_code=400, detail="rotation_days must be a whole number"
                    )
            elif field == "editor_enabled":
                value = "true" if bool(raw) else "false"
            elif field == "editor_cost_ceiling_usd":
                try:
                    value = str(max(0.01, min(5.0, float(raw))))
                except (TypeError, ValueError):
                    raise HTTPException(
                        status_code=400,
                        detail="editor_cost_ceiling_usd must be a number",
                    )
            elif field == "editor_provider":
                from app.tagging_provider import normalize_provider

                value = normalize_provider(str(raw or ""))
            elif field == "editor_model":
                value = str(raw or "").strip()
                provider = str(
                    body.get("editor_provider")
                    or settings.synth_editor_provider
                    or settings.tagging_provider
                )
                if value:
                    from app.shelf_editor import validate_editor_model

                    value = validate_editor_model(provider, value)
            else:
                items = raw if isinstance(raw, list) else _csv_items(str(raw or ""))
                value = ",".join(
                    dict.fromkeys(
                        str(item).strip() for item in items if str(item).strip()
                    )
                )
            row = db.query(AdminSetting).filter(AdminSetting.key == setting_key).first()
            if row is None:
                row = AdminSetting(key=setting_key)
                db.add(row)
            row.value = _stored_setting_value(setting_key, value)
            row.updated_at = utc_now()
            _apply_setting(setting_key, value)
            saved.append(setting_key)
        db.commit()

    known = {name.lower() for name in BY_NAME}
    unknown = [
        name
        for name in _csv_items(settings.synth_excluded_tags)
        if name.lower() not in known
    ]
    logger.info(
        "synth config: %d excluded tags, %d excluded words, rotation %dd",
        len(_csv_items(settings.synth_excluded_tags)),
        len(_csv_items(settings.synth_excluded_words)),
        settings.synth_rotation_days,
    )
    return {
        "status": "ok",
        "saved": saved,
        "excluded_tags": _csv_items(settings.synth_excluded_tags),
        "excluded_words": _csv_items(settings.synth_excluded_words),
        "rotation_days": settings.synth_rotation_days,
        "editor_enabled": settings.synth_editor_enabled,
        "editor_provider": settings.synth_editor_provider,
        "editor_model": settings.synth_editor_model,
        "editor_cost_ceiling_usd": settings.synth_editor_cost_ceiling_usd,
        "unknown_tags": unknown,
    }


@router.get("/api/tags/offvocab")
def get_tag_offvocab(
    request: Request,
    limit: int = 100,
    offset: int = 0,
    resolution: str = "unmapped",
    media_type: str = "",
    normalized: str = "",
    _=Depends(verify_admin),
):
    """Strings the model produced that resolved to no tag, by frequency.

    The single largest quality signal in the system and, until this endpoint,
    entirely write-only: the last re-tag recorded **263,512** unresolved
    suggestions against 112,418 titles (2.3 per title), each with an occurrence
    count and up to 10 sample tmdb_ids, and there was no way to read any of it.

    The top entries are either aliases worth promoting -- free, no re-tag needed
    for future runs -- or genuine taxonomy gaps for the next version. This is the
    instrument for deciding whether another re-tag is worth buying, and the plan
    told the operator to consult it before spending, then shipped without it.

    Paged, and the totals describe the **filtered** set. Both were wrong on the
    first cut: without an offset the tail beyond the top 500 was unreachable, so
    a real triage pass had to go to the database directly; and the totals
    counted the whole table regardless of ``resolution``, so a filtered list of
    40 rows sat under a header claiming 263,512 -- two different quantities
    presented as one.
    """
    with get_db() as db:
        query = db.query(TagObservation)
        if resolution and resolution != "all":
            query = query.filter(TagObservation.resolution == resolution)
        if media_type:
            query = query.filter(TagObservation.media_type == media_type)
        if normalized:
            query = query.filter(TagObservation.normalized == normalized)

        counted = query.with_entities(
            func.count(TagObservation.normalized),
            func.coalesce(func.sum(TagObservation.occurrences), 0),
        ).one()
        total = int(counted[0] or 0)
        total_occurrences = int(counted[1] or 0)

        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        rows = (
            query.order_by(
                TagObservation.occurrences.desc(),
                # Ties are common in the tail (thousands of once-seen strings),
                # and without a deterministic second key paging would skip and
                # repeat rows between requests.
                TagObservation.normalized,
            )
            .offset(offset)
            .limit(limit)
            .all()
        )

        # Resolve sample ids to titles so an operator can judge whether a
        # suggestion is a real concept or a hallucination without leaving the
        # page. Bounded to what is on screen.
        sample_ids: set[int] = set()
        for row in rows:
            for tmdb_id in (row.sample_tmdb_ids or [])[:3]:
                sample_ids.add(int(tmdb_id))
        titles: dict[int, str] = {}
        if sample_ids:
            titles = {
                int(r[0]): r[1]
                for r in db.query(MediaMetadata.tmdb_id, MediaMetadata.title)
                .filter(MediaMetadata.tmdb_id.in_(list(sample_ids)))
                .all()
            }

        return {
            "total_observations": total,
            "total_occurrences": total_occurrences,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total,
            "items": [
                {
                    "normalized": r.normalized,
                    "raw_name": r.raw_name,
                    "media_type": r.media_type,
                    "occurrences": r.occurrences,
                    "resolution": r.resolution,
                    "mapped_tag_id": r.mapped_tag_id,
                    "first_seen": r.first_seen.isoformat() if r.first_seen else None,
                    "last_seen": r.last_seen.isoformat() if r.last_seen else None,
                    "samples": [
                        {"tmdb_id": int(t), "title": titles.get(int(t))}
                        for t in (r.sample_tmdb_ids or [])[:3]
                    ],
                }
                for r in rows
            ],
        }


@router.post("/api/tags/alias")
def promote_tag_alias(request: Request, body: dict, _=Depends(verify_admin)):
    """Map an off-vocabulary string onto a canonical tag, without a redeploy.

    Operator-managed aliases (``source='admin'``) sit alongside the ones seeded
    from ``TagDef.aliases`` and are consulted by ``app/tag_validation.py`` at
    step 2, so the next tagging run resolves the string instead of discarding it.

    **Not retroactive**, and the UI must say so. Nothing stores per-title raw
    model output -- only the aggregated observation row with at most 50 sample
    ids -- so titles already tagged do not gain the tag. Acting on a promotion
    means a force re-tag of the affected titles, which is why this returns the
    occurrence count: it is the size of what a re-tag would recover.
    """
    from app.models import TagAlias
    from app.taxonomy import normalize

    raw = str(body.get("raw") or "").strip()
    tag_id = body.get("tag_id")
    if not raw or tag_id is None:
        raise HTTPException(status_code=400, detail="raw and tag_id are required")

    key = normalize(raw)
    if not key:
        raise HTTPException(status_code=400, detail="raw normalises to nothing")
    # Three places used to disagree about this key's length: observations are
    # recorded at [:200] (TagObservation.normalized is String(200)), the alias
    # column is String(160), and resolve_tag looks the key up UNTRUNCATED. So
    # promoting an observation longer than 160 chars silently wrote an alias
    # that could never match anything -- the operator saw a success response
    # and got no behaviour. Reject it instead of pretending.
    if len(key) > _ALIAS_KEY_MAX:
        raise HTTPException(
            status_code=400,
            detail=(
                f"normalised name is {len(key)} characters; aliases are limited "
                f"to {_ALIAS_KEY_MAX}. A string this long is a malformed model "
                "response rather than a tag worth promoting."
            ),
        )

    with get_db() as db:
        tag = db.query(Tag).filter(Tag.id == int(tag_id)).first()
        if tag is None:
            raise HTTPException(status_code=404, detail="tag not found")

        observed = int(
            db.query(func.coalesce(func.sum(TagObservation.occurrences), 0))
            .filter(TagObservation.normalized == key)
            .scalar()
            or 0
        )
        if observed == 1:
            raise HTTPException(
                status_code=400,
                detail=(
                    "This suggestion has been observed only once. Leave it "
                    "unmapped until another title independently proposes it."
                ),
            )

        existing = db.query(TagAlias).filter(TagAlias.normalized == key).first()
        if existing is not None:
            existing.tag_id = tag.id
            existing.raw = raw[:200]
            existing.source = "admin"
        else:
            db.add(
                TagAlias(
                    normalized=key,
                    tag_id=tag.id,
                    raw=raw[:200],
                    source="admin",
                )
            )

        recovered = 0
        for obs in db.query(TagObservation).filter(TagObservation.normalized == key):
            obs.resolution = "aliased"
            obs.mapped_tag_id = tag.id
            recovered += obs.occurrences or 0
        db.commit()

    logger.info(f"tag alias: '{raw}' -> {tag.name} ({recovered:,} occurrences)")
    return {
        "status": "ok",
        "normalized": key,
        "tag": tag.name,
        "occurrences": recovered,
        "retroactive": False,
        "note": (
            "Future tagging resolves this string. Titles already tagged are "
            "unchanged — only a re-tag would apply it retroactively."
        ),
    }


@router.get("/api/tags/alias/sample-ids")
def get_alias_sample_ids(
    request: Request,
    resolution: str = "aliased",
    limit: int = 500,
    _=Depends(verify_admin),
):
    """The tmdb ids recorded against observations, for a validation re-tag.

    Promoting an alias is not retroactive, and it cannot be scoped precisely:
    ``TagObservation`` keeps at most ten sample ids per string, so for a string
    seen 5,000 times we hold 0.2% of the affected titles. That makes a *fully*
    scoped re-tag impossible.

    What it does make possible is a **validation pass**. Re-tag just these ids
    (a few thousand, cents rather than ~$25) and check whether the promoted
    strings drop out of ``tag_observations``. If they do, the promotion works
    and a full pass is worth buying. If they do not, something else is wrong
    and the full pass would have been wasted.

    Defaults to ``resolution=aliased`` — the observations you have just acted
    on. Pass ``resolution=all`` for every recorded sample.
    """
    limit = max(1, min(limit, 2000))
    with get_db() as db:
        query = db.query(TagObservation.sample_tmdb_ids, TagObservation.media_type)
        if resolution != "all":
            query = query.filter(TagObservation.resolution == resolution)
        rows = query.order_by(TagObservation.occurrences.desc()).limit(limit).all()

    by_type: dict[str, list[int]] = {"movie": [], "tv": []}
    seen: set[tuple[str, int]] = set()
    for samples, media_type in rows:
        bucket = by_type.get(media_type)
        if bucket is None:
            continue
        for tmdb_id in samples or []:
            try:
                tid = int(tmdb_id)
            except (TypeError, ValueError):
                continue
            if (media_type, tid) in seen:
                continue
            seen.add((media_type, tid))
            bucket.append(tid)

    total = len(seen)
    from workers.retag import (
        EST_INPUT_TOKENS,
        EST_OUTPUT_TOKENS,
        estimated_ai_titles,
    )
    from workers.tagging_common import estimate_cost

    return {
        "observations": len(rows),
        "tmdb_ids": total,
        "by_media_type": {k: len(v) for k, v in by_type.items()},
        "movie": by_type["movie"],
        "tv": by_type["tv"],
        "est_cost_usd": round(
            estimate_cost(
                estimated_ai_titles(total) * EST_INPUT_TOKENS,
                estimated_ai_titles(total) * EST_OUTPUT_TOKENS,
            ),
            4,
        ),
        "note": (
            "A validation sample, not the full affected set — at most 50 ids "
            "are recorded per observation. Re-tag these with force=true, then "
            "re-read /api/tags/offvocab to see whether the strings dropped."
        ),
    }


@router.get("/api/tags/list")
def get_tag_list(
    request: Request,
    facet: str = "",
    q: str = "",
    sort: str = "doc_count",
    offset: int = 0,
    limit: int = 200,
    _=Depends(verify_admin),
):
    """Every tag with its corpus statistics and catalog references."""
    from app.models import TagStats

    with get_db() as db:
        query = db.query(Tag)
        if facet:
            query = query.filter(Tag.category == facet)
        if q:
            query = query.filter(Tag.name.ilike(f"%{q}%"))
        page_limit = max(1, min(limit, 1000))
        page_offset = max(0, offset)
        total = query.count()
        if sort == "doc_count":
            totals = (
                db.query(
                    TagStats.tag_id.label("tag_id"),
                    func.sum(TagStats.doc_count).label("total_docs"),
                )
                .group_by(TagStats.tag_id)
                .subquery()
            )
            tags = (
                query.outerjoin(totals, totals.c.tag_id == Tag.id)
                .order_by(func.coalesce(totals.c.total_docs, 0).desc(), Tag.name)
                .offset(page_offset)
                .limit(page_limit)
                .all()
            )
        else:
            tags = query.order_by(Tag.name).offset(page_offset).limit(page_limit).all()

        stats: dict[int, dict] = {}
        for row in db.query(TagStats).filter(
            TagStats.tag_id.in_([t.id for t in tags] or [0])
        ):
            entry = stats.setdefault(row.tag_id, {})
            entry[row.media_type] = {
                "doc_count": row.doc_count,
                "idf": round(row.idf, 3) if row.idf is not None else None,
                "avg_confidence": (
                    round(row.avg_confidence, 3)
                    if row.avg_confidence is not None
                    else None
                ),
            }

        # Which curated rows reference each tag. 27 of the original 75 tags were
        # referenced by no formula at all, so a tag can be perfectly healthy and
        # still drive no discovery -- worth seeing next to the doc counts.
        refs: dict[str, list[str]] = {}
        for category in db.query(UniversalCategory).all():
            formula = category.tag_formula or {}
            for key in ("mandatory", "required", "optional"):
                for name in formula.get(key, []):
                    refs.setdefault(name, []).append(category.id)

        items = [
            {
                "id": t.id,
                "name": t.name,
                "facet": t.category,
                "description": t.description,
                "display": bool(t.display),
                "is_active": bool(t.is_active),
                "is_derived": bool(t.is_derived),
                "stats": stats.get(t.id, {}),
                "total_docs": sum(
                    v.get("doc_count", 0) for v in stats.get(t.id, {}).values()
                ),
                "category_refs": refs.get(t.name, []),
            }
            for t in tags
        ]
        return {
            "total": total,
            "offset": page_offset,
            "limit": page_limit,
            "has_more": page_offset + len(items) < total,
            "items": items,
        }


@router.get("/api/tags/dead")
def get_dead_tags(request: Request, _=Depends(verify_admin)):
    """Tags that drive no discovery, split by which kind of dead they are.

    ``zero_assignments`` is a tag no title carries -- the model never picks it,
    or it is unreachable from the prompt. ``zero_category_refs`` is a tag titles
    DO carry but no curated row asks for, which is the failure mode that left 27
    of the original 75 tags driving nothing. They need opposite responses, so
    they are reported separately rather than as one "unused" number.
    """
    from app.models import TagStats

    with get_db() as db:
        assigned = {
            row[0]
            for row in db.query(TagStats.tag_id).filter(TagStats.doc_count > 0).all()
        }
        referenced: set[str] = set()
        for category in db.query(UniversalCategory).all():
            formula = category.tag_formula or {}
            for key in ("mandatory", "required", "optional"):
                referenced.update(formula.get(key, []))

        tags = db.query(Tag).filter(Tag.is_active.is_(True)).all()
        zero_assignments = [
            {"id": t.id, "name": t.name, "facet": t.category}
            for t in tags
            if t.id not in assigned and not t.is_derived
        ]
        zero_refs = [
            {"id": t.id, "name": t.name, "facet": t.category}
            for t in tags
            if t.name not in referenced and t.id in assigned
        ]

        # Rows pointing at a tag id that no longer exists.
        #
        # Should always be 0: movie_tags.tag_id carries a real
        # ForeignKey("tags.id", ondelete="CASCADE"), so the database will not
        # allow one. It is reported anyway as a cheap integrity check -- a
        # non-zero value means the constraint is missing (an older schema, or
        # SQLite with foreign_keys pragma off), which would silently break the
        # dead-tag arithmetic above.
        tag_ids = {t.id for t in db.query(Tag.id).all()}
        orphans = (
            db.query(func.count(MovieTag.tag_id))
            .filter(MovieTag.tag_id.notin_(list(tag_ids) or [0]))
            .scalar()
            or 0
        )

        return {
            "zero_assignments": zero_assignments,
            "zero_category_refs": zero_refs,
            "orphan_rows": int(orphans),
        }


@router.post("/api/tags/{tag_id}/toggle")
def toggle_tag_display(request: Request, tag_id: int, _=Depends(verify_admin)):
    """Hide a noisy tag from user-facing chips without deleting anything.

    Flips ``display`` only. The tag keeps its assignments and keeps working in
    catalog formulas and similarity -- this governs what users *see*, not what
    the engine uses, which is why it is reversible and cheap.
    """
    with get_db() as db:
        tag = db.query(Tag).filter(Tag.id == tag_id).first()
        if tag is None:
            raise HTTPException(status_code=404, detail="tag not found")
        tag.display = not bool(tag.display)
        db.commit()
        return {"status": "ok", "id": tag.id, "name": tag.name, "display": tag.display}


@router.get("/api/tags/overview")
def get_tags_overview(request: Request, _=Depends(verify_admin)):
    """Tag coverage across the library.

    Pulled forward from the tagging overhaul's later phases because it needs
    nothing but count queries against tables that already exist, and it is the
    diagnostic that would have surfaced the untagged-library gap without anyone
    reading a dashboard screenshot: production carried ~150k metadata rows
    (42% of the library) with no movie_tags at all, invisible to every existing
    stat.

    ``zero_tag_items`` is the number that matters.
    """
    with get_db() as db:
        tagged_pairs = (
            db.query(MovieTag.tmdb_id, MovieTag.media_type).distinct().subquery()
        )

        per_type: list[dict] = []
        total_metadata = 0
        total_tagged = 0
        for media_type in ("movie", "tv"):
            metadata_rows = (
                db.query(func.count(MediaMetadata.tmdb_id))
                .filter(MediaMetadata.media_type == media_type)
                .scalar()
                or 0
            )
            tagged = (
                db.query(func.count(func.distinct(MovieTag.tmdb_id)))
                .filter(MovieTag.media_type == media_type)
                .scalar()
                or 0
            )
            total_metadata += metadata_rows
            total_tagged += tagged
            per_type.append(
                {
                    "media_type": media_type,
                    "metadata_rows": metadata_rows,
                    "tagged": tagged,
                    "zero_tag_items": max(0, metadata_rows - tagged),
                    "coverage_pct": (
                        round(100.0 * tagged / metadata_rows, 1)
                        if metadata_rows
                        else 0.0
                    ),
                }
            )

        # Assignments per facet, so a facet the tagger never uses is visible.
        facet_rows = (
            db.query(Tag.category, func.count(MovieTag.tag_id))
            .join(MovieTag, MovieTag.tag_id == Tag.id)
            .group_by(Tag.category)
            .all()
        )

        # Tags that exist but carry no assignments -- the phantom-tag class.
        assigned_tag_ids = db.query(MovieTag.tag_id).distinct().subquery()
        zero_assignment_tags = (
            db.query(func.count(Tag.id))
            .filter(~Tag.id.in_(db.query(assigned_tag_ids.c.tag_id)))
            .scalar()
            or 0
        )

        total_assignments = (
            db.query(func.count()).select_from(tagged_pairs).scalar() or 0
        )

        overview = {
            "taxonomy_version": TAXONOMY_VERSION,
            "total_metadata": total_metadata,
            "total_tagged": total_tagged,
            "zero_tag_items": max(0, total_metadata - total_tagged),
            "coverage_pct": (
                round(100.0 * total_tagged / total_metadata, 1)
                if total_metadata
                else 0.0
            ),
            "by_media_type": per_type,
            "tags_total": db.query(func.count(Tag.id)).scalar() or 0,
            "tags_active": (
                db.query(func.count(Tag.id)).filter(Tag.is_active.is_(True)).scalar()
                or 0
            ),
            "tags_zero_assignments": zero_assignment_tags,
            "assignments_total": (db.query(func.count(MovieTag.tag_id)).scalar() or 0),
            "distinct_tagged_titles": total_assignments,
            "by_facet": [
                {"facet": facet, "assignments": count} for facet, count in facet_rows
            ],
            "observations_unmapped": (
                db.query(func.count(TagObservation.normalized))
                .filter(TagObservation.resolution == "unmapped")
                .scalar()
                or 0
            ),
        }

        # tagger_version breakdown. The column arrives with the re-tag phase; on
        # a database that predates it, report the rows as unversioned rather
        # than failing the whole endpoint.
        try:
            version_rows = (
                db.query(
                    MovieTag.tagger_version,
                    func.count(func.distinct(MovieTag.tmdb_id)),
                )
                .group_by(MovieTag.tagger_version)
                .all()
            )
            overview["version_breakdown"] = [
                {"tagger_version": version or "unversioned", "titles": count}
                for version, count in version_rows
            ]
        except (AttributeError, SQLAlchemyError):
            db.rollback()
            overview["version_breakdown"] = [
                {"tagger_version": "unversioned", "titles": total_tagged}
            ]

    return overview


#: In-process handle on a running rebuild, so status/pause/stop can reach it.
_rebuild_task: dict = {"task": None, "job_id": None}


# ---- Library tagging and the tag pack ----

#: Largest tag pack accepted by upload (compressed).
_TAG_PACK_MAX_UPLOAD = 200 * 1024 * 1024


@router.get("/api/library-tagging/status")
def library_tagging_status(_=Depends(verify_admin)):
    """Owned/Watchlist tagging progress plus the installed tag pack."""
    from app import tag_pack
    from workers import library_tagging

    with get_db() as db:
        data = library_tagging.summary(db)
        data["pack"] = {
            "installed_version": tag_pack.installed_version(db),
            "titles": tag_pack.pack_title_count(db),
            "url": settings.tag_pack_url or "",
        }
    return data


@router.post("/api/library-tagging/run")
async def library_tagging_run(_=Depends(verify_admin)):
    """Tag the next batch of owned/Watchlist titles in the background."""
    from app import library_catalog

    await library_catalog.queue_owned_tagging()
    return {"status": "started"}


@router.get("/api/tagging/repair/rule-only")
def rule_only_status(min_votes: int = 5, _=Depends(verify_admin)):
    """Titles whose AI tagging failed and that only have rule-based tags."""
    from workers import rule_only_retry

    min_votes = max(0, min(int(min_votes), 1_000_000))
    with get_db() as db:
        waiting = rule_only_retry.count_rule_only(db, min_votes)
    return {
        "waiting": waiting,
        "min_votes": min_votes,
        "cost_ceiling_usd": settings.retag_cost_ceiling_usd,
        **rule_only_retry.status,
    }


@router.post("/api/tagging/repair/rule-only")
async def rule_only_start(request: Request, _=Depends(verify_admin)):
    """Retry AI tagging for rule-only titles in the background."""
    from app.tagging_provider import provider_ready
    from workers import rule_only_retry

    body = await request.json() if await request.body() else {}
    min_votes = max(0, min(int(body.get("min_votes") or 5), 1_000_000))
    if not provider_ready():
        raise HTTPException(status_code=400, detail="Set up an AI provider first.")
    if rule_only_retry.status["running"] or _upgrade_state["running"]:
        raise HTTPException(status_code=409, detail="A tagging run is already going.")
    with get_db() as db:
        busy = db.query(TaggingJob).filter(TaggingJob.status == "running").first()
    if busy:
        raise HTTPException(
            status_code=409,
            detail=f"A {busy.job_type} job is already running (id {busy.id}).",
        )

    async def run() -> None:
        try:
            await rule_only_retry.retry_rule_only(min_votes=min_votes)
        except Exception as exc:  # noqa: BLE001 - surfaced through status
            rule_only_retry.status["first_error"] = f"{type(exc).__name__}: {exc}"
            logger.error(f"rule-only retry failed: {exc}")
        finally:
            rule_only_retry.status["running"] = False

    # Shown as running at once; the run itself resets it when it ends.
    rule_only_retry.status["running"] = True
    asyncio.create_task(run())
    return {"status": "started"}


@router.get("/api/tag-pack/export")
async def tag_pack_export(min_votes: int = 50, _=Depends(verify_admin)):
    """Download this install's AI tags as a shareable tag pack.

    Only Curatio tags keyed by TMDB/IMDb id; no TMDB text, artwork or ratings.
    """
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask

    from app import tag_pack

    min_votes = max(0, min(int(min_votes), 1_000_000))

    def build():
        with get_db() as db:
            return tag_pack.export_pack(db, min_votes=min_votes)

    path, manifest = await asyncio.to_thread(build)
    filename = f"curatio-tag-pack-{manifest['version']}.jsonl.gz"
    return FileResponse(
        path,
        media_type="application/gzip",
        filename=filename,
        headers={
            "X-Curatio-Pack-Sha256": manifest["sha256"],
            "X-Curatio-Pack-Titles": str(manifest["titles"]),
            "X-Curatio-Pack-Version": manifest["version"],
        },
        background=BackgroundTask(lambda: Path(path).unlink(missing_ok=True)),
    )


@router.post("/api/tag-pack/import")
async def tag_pack_import(request: Request, _=Depends(verify_admin)):
    """Import a tag pack file (the raw .jsonl.gz as the request body)."""
    import io

    from app import tag_pack

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _TAG_PACK_MAX_UPLOAD:
            raise HTTPException(status_code=413, detail="Tag pack is too large")
    if not body:
        raise HTTPException(status_code=400, detail="No file was sent")

    def run():
        with get_db() as db:
            return tag_pack.import_pack(db, io.BytesIO(bytes(body)))

    try:
        result = await asyncio.to_thread(run)
    except tag_pack.TagPackError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await asyncio.to_thread(tag_pack._after_import)
    return {"status": "imported", **result}


@router.post("/api/tag-pack/update")
async def tag_pack_update(_=Depends(verify_admin)):
    """Download the pack from TAG_PACK_URL now (even if it is current)."""
    from app import tag_pack

    try:
        return await tag_pack.update_from_url(force=True)
    except tag_pack.TagPackError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Could not download the tag pack: {exc}"
        ) from exc


@router.get("/api/tagging/config")
def get_tagging_config(request: Request, _=Depends(verify_admin)):
    """Everything the Tagging tab needs to render."""
    from app.mdblist_client import get_api_key
    from app.tagging_provider import (
        PROVIDER_SPECS,
        active_model,
        normalize_provider,
        provider_label,
        provider_options,
    )
    from app.taxonomy import TAXONOMY_VERSION, facet_counts
    from workers.tagging_common import effective_batch_size

    with get_db() as db:
        key = get_api_key(db)
        running = (
            db.query(TaggingJob)
            .filter(
                TaggingJob.status == "running",
                TaggingJob.job_type == "manual_retag",
            )
            .first()
        )
        enriched = (
            db.query(func.count(MediaMetadata.tmdb_id))
            .filter(MediaMetadata.tag_facts.isnot(None))
            .scalar()
            or 0
        )
        mdb_enriched = (
            db.query(func.count(MediaMetadata.tmdb_id))
            .filter(MediaMetadata.mdb_facts.isnot(None))
            .scalar()
            or 0
        )
        rated = (
            db.query(func.count(MediaMetadata.tmdb_id))
            .filter(MediaMetadata.rating_consensus.isnot(None))
            .scalar()
            or 0
        )
        total = db.query(func.count(MediaMetadata.tmdb_id)).scalar() or 0

        # load_settings_from_db() replays every admin_settings row over the
        # environment on each boot, so a GEMINI_MODEL stored here outranks both
        # .env and the code default -- permanently, and invisibly. That is how
        # an install keeps calling a retired model after an upgrade that was
        # supposed to fix exactly that. Report it rather than silently winning.
        provider = normalize_provider()
        spec = PROVIDER_SPECS[provider]
        model_field = str(spec["model_field"])
        model_setting_key = str(spec["model_setting_key"])
        pinned = (
            db.query(AdminSetting).filter(AdminSetting.key == model_setting_key).first()
        )
        code_default = type(settings).model_fields[model_field].default
        selected_model = active_model(provider)
        selected_model_spec: dict[str, Any] = next(
            (item for item in spec["models"] if str(item["id"]) == selected_model),
            {},
        )

        return {
            "taxonomy_version": TAXONOMY_VERSION,
            "taxonomy_size": sum(facet_counts().values()),
            "provider": provider,
            "provider_label": provider_label(provider),
            "providers": provider_options(),
            "model": selected_model,
            "model_default": code_default,
            "model_pinned_in_db": pinned is not None,
            "model_overrides_default": selected_model != code_default,
            "model_production_ready": selected_model_spec.get("production_ready", True),
            "model_warning": selected_model_spec.get("warning", ""),
            "batch_size": effective_batch_size(),
            "cost_ceiling_usd": settings.retag_cost_ceiling_usd,
            "tag_max_per_item": settings.tag_max_per_item,
            "catalog_min_quality": settings.catalog_min_quality,
            "catalog_min_vote_count": settings.catalog_min_vote_count,
            "catalog_min_rating_sources": settings.catalog_min_rating_sources,
            "library_total": total,
            "tmdb_enriched": enriched,
            "mdb_enriched": mdb_enriched,
            "rating_consensus": rated,
            "mdblist_key_set": bool(key),
            "mdblist_key_hint": (key[:4] + "…" + key[-3:]) if key else "",
            "rebuild_running": running is not None,
            "rebuild_job_id": running.id if running else None,
            "paused": not build_pause_event.is_set(),
        }


@router.post("/api/tagging/config")
async def set_tagging_config(request: Request, _=Depends(verify_admin)):
    """Store/clear MDBList configuration and reset a pinned AI model."""
    from app.mdblist_client import set_api_key
    from app.tagging_provider import PROVIDER_SPECS, normalize_provider

    body = await request.json()
    with get_db() as db:
        if "mdblist_api_key" in body:
            set_api_key(db, body["mdblist_api_key"] or None)
        if body.get("clear_pinned_model"):
            # Deleting the row is the only way back to the code default: the
            # in-memory value was overwritten at boot and there is nothing to
            # restore it from until the next restart, so set it here too.
            provider = normalize_provider()
            spec = PROVIDER_SPECS[provider]
            model_field = str(spec["model_field"])
            model_key = str(spec["model_setting_key"])
            db.query(AdminSetting).filter(AdminSetting.key == model_key).delete(
                synchronize_session=False
            )
            db.commit()
            object.__setattr__(
                settings,
                model_field,
                type(settings).model_fields[model_field].default,
            )
            from app.tagging_provider import reconfigure_tagging_engines

            reconfigure_tagging_engines()
        return {"ok": True}


@router.get("/api/tagging/repair/status")
def repair_status(request: Request, _=Depends(verify_admin)):
    """How much work the two targeted repairs would do.

    Both exist because an enrichment source can be fixed *after* titles have
    already been processed with it. Neither is found by a version bump: the
    taxonomy did not change, only the inputs did.
    """
    from workers.mdb_sync import MDB_VERSION
    from workers.retag import _pending_query

    with get_db() as db:
        stale_mdb = (
            db.query(func.count(MediaMetadata.tmdb_id))
            .filter(
                MediaMetadata.mdb_enriched_at.isnot(None),
                (MediaMetadata.mdb_version.is_(None))
                | (MediaMetadata.mdb_version != MDB_VERSION),
            )
            .scalar()
            or 0
        )
        stale_tags = _pending_query(db, stale_facts=True).count()
        return {
            "stale_mdb_facts": stale_mdb,
            "stale_tags": stale_tags,
            "mdb_version": MDB_VERSION,
        }


@router.post("/api/tagging/repair/retag-stale")
async def repair_retag_stale(request: Request, _=Depends(verify_admin)):
    """Re-tag only titles whose tags predate their MDBList facts.

    The cheap, surgical alternative to a forced full re-tag: it spends Gemini
    calls on exactly the titles the missing data affected.
    """
    if _upgrade_state["running"]:
        raise HTTPException(status_code=409, detail="An upgrade is already running.")
    with get_db() as db:
        busy = db.query(TaggingJob).filter(TaggingJob.status == "running").first()
        if busy:
            raise HTTPException(
                status_code=409,
                detail=f"A {busy.job_type} job is already running (id {busy.id}).",
            )

    from workers.build_control import reset_build_stop

    reset_build_stop()
    build_pause_event.set()
    _upgrade_state.update(
        running=True,
        steps=[],
        started_at=utc_now().isoformat(),
        finished_at=None,
        report=None,
        error=None,
    )

    def on_step(status: dict) -> None:
        _upgrade_state["steps"] = status.get("steps") or []

    async def _run():
        try:
            report = await asyncio.to_thread(_run_repair_sync, on_step)
            _upgrade_state["report"] = report
            _upgrade_state["steps"] = report["steps"]
        except Exception as exc:  # noqa: BLE001 - surfaced through /status
            _upgrade_state["error"] = f"{type(exc).__name__}: {exc}"
            logger.error(f"stale re-tag failed: {exc}")
        finally:
            _upgrade_state["running"] = False
            _upgrade_state["finished_at"] = utc_now().isoformat()

    _upgrade_state["task"] = asyncio.create_task(_run())
    return {"ok": True, "started": True}


@router.post("/api/tagging/rebuild/estimate")
async def rebuild_estimate(request: Request, _=Depends(verify_admin)):
    """Count and projected cost. Makes no API calls and writes nothing."""
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - an empty body is fine
        pass
    from workers.retag import estimate

    # `force` was hardcoded False here, so the one operation whose cost an
    # operator most needs to see before running -- a whole-library re-tag --
    # was the one the estimator could not be asked about.
    return await asyncio.to_thread(
        estimate,
        body.get("media_type"),
        int(body.get("min_votes") or 0),
        bool(body.get("force")),
        limit=(int(body["limit"]) if body.get("limit") else None),
    )


@router.post("/api/tagging/rebuild/start")
async def rebuild_start(request: Request, _=Depends(verify_admin)):
    """Start a rebuild in the background.

    Runs against the live database: catalogs are served from pre-computed
    snapshots, and each title's tags swap inside one transaction, so browsing is
    unaffected for the whole run.
    """
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass

    limit = body.get("limit")
    from app.tagging_provider import active_model, production_model_ready

    if not production_model_ready() and (not limit or int(limit) > 1000):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{active_model()} is restricted to pilots of at most 1,000 titles "
                "after failing semantic QA. Choose a production-ready model in "
                "Tagging System before a full rebuild."
            ),
        )

    with get_db() as db:
        already = db.query(TaggingJob).filter(TaggingJob.status == "running").first()
        if already:
            raise HTTPException(
                status_code=409,
                detail=f"A {already.job_type} job is already running (id {already.id}).",
            )

    from workers.build_control import reset_build_stop

    reset_build_stop()
    build_pause_event.set()

    tmdb_ids = _parse_id_list(body.get("tmdb_ids"))
    # On a thread, not asyncio.create_task.
    #
    # build_control's own comment says "if you add a third caller, put it on a
    # thread" -- this was the third caller and it was not. run_retag threads
    # check_pause into tag_and_store, and check_pause is `async def` but calls
    # threading.Event.wait() with no timeout: correct on a worker thread,
    # fatal on the web server's loop. Pressing Pause in the Rebuild card parked
    # the loop, so the Resume request could never be read off the socket and
    # only a container restart recovered it.
    #
    # Second failure, same cause: _load runs query.all() plus build_features
    # over the whole pending set synchronously, stalling every client request
    # for the duration of each pass.
    task = asyncio.create_task(
        asyncio.to_thread(
            _run_retag_sync,
            body.get("media_type"),
            int(limit) if limit else None,
            int(body.get("min_votes") or 0),
            bool(body.get("force")),
            tmdb_ids,
        )
    )
    _rebuild_task["task"] = task
    return {"ok": True, "started": True, "scoped_ids": len(tmdb_ids or [])}


@router.get("/api/tagging/rebuild/status")
def rebuild_status(request: Request, _=Depends(verify_admin)):
    """Live progress, with the tagger-version breakdown."""
    with get_db() as db:
        job = (
            db.query(TaggingJob)
            .filter(TaggingJob.job_type == "manual_retag")
            .order_by(TaggingJob.started_at.desc())
            .first()
        )
        try:
            versions = [
                {"tagger_version": v or "legacy", "titles": n}
                for v, n in db.query(
                    MovieTag.tagger_version,
                    func.count(func.distinct(MovieTag.tmdb_id)),
                )
                .group_by(MovieTag.tagger_version)
                .all()
            ]
        except SQLAlchemyError:
            db.rollback()
            versions = []

        if job is None:
            return {"job": None, "version_breakdown": versions}
        return {
            "job": {
                "id": job.id,
                "status": job.status,
                "started_at": job.started_at.isoformat() if job.started_at else None,
                "completed_at": (
                    job.completed_at.isoformat() if job.completed_at else None
                ),
                "items_processed": job.items_processed,
                "items_failed": job.items_failed,
                "api_calls": job.total_api_calls,
                "api_attempts": job.total_api_attempts,
                "tokens": job.total_tokens_used,
                "input_tokens": job.input_tokens,
                "output_tokens": job.output_tokens,
                "thinking_tokens": job.thinking_tokens,
                "usage_estimated": job.usage_estimated,
                "pricing_basis": job.pricing_basis,
                "cost_usd": job.estimated_cost,
                "error_message": (job.error_message or "")[:400],
                "metadata": job.job_metadata,
            },
            "paused": not build_pause_event.is_set(),
            "version_breakdown": versions,
        }


@router.post("/api/tagging/rebuild/{action}")
def rebuild_control(action: str, request: Request, _=Depends(verify_admin)):
    """Pause, resume or stop a running rebuild.

    Stop unwinds at the next batch boundary rather than killing mid-write, so
    the library is never left with a half-swapped title.
    """
    from workers.build_control import request_build_stop

    if action == "pause":
        build_pause_event.clear()
    elif action == "resume":
        build_pause_event.set()
    elif action == "stop":
        request_build_stop()
    else:
        raise HTTPException(status_code=404, detail="Unknown action")
    return {"ok": True, "action": action, "paused": not build_pause_event.is_set()}


# ---- One-button upgrade ----
#
# The whole pipeline behind a single control. State lives in this dict rather
# than the database because it is per-process progress, not a record: the run
# dies with the container, and each step's own resumption query is what makes
# restarting safe. Persisting it would imply a resume guarantee this does not
# make and does not need.
_upgrade_state: dict = {
    "task": None,
    "running": False,
    "steps": [],
    "started_at": None,
    "finished_at": None,
    "report": None,
    "error": None,
}


@router.get("/api/tagging/upgrade/preflight")
async def upgrade_preflight(request: Request, _=Depends(verify_admin)):
    """What the upgrade needs and what it would do. Runs nothing, costs nothing."""
    from workers.upgrade import preflight

    data = await asyncio.to_thread(preflight)
    data["running"] = _upgrade_state["running"]
    return data


@router.post("/api/tagging/upgrade/keys")
async def upgrade_set_keys(request: Request, _=Depends(verify_admin)):
    """Store the keys the upgrade asked for.

    Written to ``admin_settings`` and applied to the live config immediately,
    so the operator never has to edit ``.env`` or restart to start a run.
    """
    from app.mdblist_client import set_api_key
    from app.tagging_provider import PROVIDER_SPECS, normalize_provider, validate_model

    body = await request.json()
    provider_fields = {
        "tagging_provider",
        "model",
        "gemini_api_key",
        "openai_api_key",
        "anthropic_api_key",
        "local_api_key",
        "local_base_url",
    }
    if provider_fields.intersection(body):
        if _upgrade_state["running"]:
            raise HTTPException(
                status_code=409,
                detail="Stop the running upgrade before changing its AI provider.",
            )
        with get_db() as check_db:
            busy = (
                check_db.query(TaggingJob)
                .filter(TaggingJob.status == "running")
                .first()
            )
            if busy:
                raise HTTPException(
                    status_code=409,
                    detail="Stop the running tagging job before changing its AI provider.",
                )

    try:
        provider = normalize_provider(body.get("tagging_provider"))
        model = validate_model(provider, body["model"]) if body.get("model") else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    saved = []
    with get_db() as db:
        for field, setting_key in (
            ("tmdb_api_key", "TMDB_API_KEY"),
            ("gemini_api_key", "GEMINI_API_KEY"),
            ("openai_api_key", "OPENAI_API_KEY"),
            ("anthropic_api_key", "ANTHROPIC_API_KEY"),
            ("local_api_key", "LOCAL_API_KEY"),
            ("local_base_url", "LOCAL_BASE_URL"),
        ):
            value = (body.get(field) or "").strip()
            if field == "local_base_url" and value:
                parsed = urlparse(value)
                if parsed.scheme not in ("http", "https") or not parsed.netloc:
                    raise HTTPException(
                        status_code=400,
                        detail="The local model address must be an http(s) URL.",
                    )
                value = value.rstrip("/")
            if not value:
                continue
            row = db.query(AdminSetting).filter(AdminSetting.key == setting_key).first()
            if row is None:
                row = AdminSetting(key=setting_key)
                db.add(row)
            row.value = value
            row.updated_at = utc_now()
            _apply_setting(setting_key, value)
            saved.append(setting_key)
        if body.get("tagging_provider"):
            row = (
                db.query(AdminSetting)
                .filter(AdminSetting.key == "TAGGING_PROVIDER")
                .first()
            )
            if row is None:
                row = AdminSetting(key="TAGGING_PROVIDER")
                db.add(row)
            row.value = provider
            row.updated_at = utc_now()
            _apply_setting("TAGGING_PROVIDER", provider)
            saved.append("TAGGING_PROVIDER")
        if model:
            model_key = str(PROVIDER_SPECS[provider]["model_setting_key"])
            row = db.query(AdminSetting).filter(AdminSetting.key == model_key).first()
            if row is None:
                row = AdminSetting(key=model_key)
                db.add(row)
            row.value = model
            row.updated_at = utc_now()
            _apply_setting(model_key, model)
            saved.append(model_key)
        if "mdblist_api_key" in body:
            set_api_key(db, (body.get("mdblist_api_key") or "").strip() or None)
            saved.append("MDBLIST_API_KEY")
        db.commit()
    return {"ok": True, "saved": saved}


@router.post("/api/tagging/upgrade/start")
async def upgrade_start(request: Request, _=Depends(verify_admin)):
    """Run the whole pipeline in the background."""
    from workers.upgrade import STEP_KEYS, preflight

    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - an empty body means "all defaults"
        pass

    if _upgrade_state["running"]:
        raise HTTPException(status_code=409, detail="An upgrade is already running.")

    with get_db() as db:
        busy = db.query(TaggingJob).filter(TaggingJob.status == "running").first()
        if busy:
            raise HTTPException(
                status_code=409,
                detail=f"A {busy.job_type} job is already running (id {busy.id}).",
            )

    checks = await asyncio.to_thread(preflight)
    skip = tuple(k for k in body.get("skip") or [] if k in STEP_KEYS)
    limit = body.get("limit")
    from app.tagging_provider import active_model, production_model_ready

    if (
        "retag" not in skip
        and not production_model_ready()
        and (not limit or int(limit) > 1000)
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{active_model()} is restricted to pilots of at most 1,000 titles "
                "after failing semantic QA. Choose a production-ready model first."
            ),
        )
    # Missing keys only block the steps that actually need them.
    if not checks["keys"]["tagging"] and "retag" not in skip:
        raise HTTPException(
            status_code=400,
            detail=(
                f"A {checks['provider_label']} key is required to re-tag. "
                "Add one, or skip that step."
            ),
        )
    if not checks["keys"]["tmdb"] and "enrich" not in skip:
        raise HTTPException(
            status_code=400,
            detail="A TMDB API key is required to fetch details. Add one, or skip "
            "that step.",
        )

    from workers.build_control import reset_build_stop

    reset_build_stop()
    build_pause_event.set()

    _upgrade_state.update(
        running=True,
        steps=[],
        started_at=utc_now().isoformat(),
        finished_at=None,
        report=None,
        error=None,
    )

    def on_step(status: dict) -> None:
        # Called from the upgrade thread; a dict assignment is atomic enough
        # under the GIL and /status only ever reads it.
        _upgrade_state["steps"] = status.get("steps") or []

    async def _run():
        try:
            report = await asyncio.to_thread(
                _run_upgrade_sync,
                skip,
                int(limit) if limit else None,
                int(body.get("min_votes") or 0),
                on_step,
            )
            _upgrade_state["report"] = report
            _upgrade_state["steps"] = report["steps"]
        except Exception as exc:  # noqa: BLE001 - surfaced through /status
            _upgrade_state["error"] = f"{type(exc).__name__}: {exc}"
            logger.error(f"upgrade failed: {exc}")
        finally:
            _upgrade_state["running"] = False
            _upgrade_state["finished_at"] = utc_now().isoformat()

    _upgrade_state["task"] = asyncio.create_task(_run())
    return {"ok": True, "started": True, "skipped": list(skip)}


@router.get("/api/tagging/upgrade/status")
def upgrade_status(request: Request, _=Depends(verify_admin)):
    """Live per-step progress."""
    steps = _upgrade_state["steps"] or []
    return {
        "running": _upgrade_state["running"],
        "started_at": _upgrade_state["started_at"],
        "finished_at": _upgrade_state["finished_at"],
        "error": _upgrade_state["error"],
        "steps": steps,
        "done": sum(1 for s in steps if s.get("status") == "completed"),
        "cost_usd": (_upgrade_state["report"] or {}).get("cost_usd", 0.0),
    }


@router.post("/api/tagging/upgrade/stop")
def upgrade_stop(request: Request, _=Depends(verify_admin)):
    """Stop after the current batch. Everything already written is kept."""
    from workers.build_control import request_build_stop

    request_build_stop()
    return {"ok": True}


@router.get("/api/fingerprint-status")
def get_fingerprint_status(request: Request, _=Depends(verify_admin)):
    now = utc_now()
    stale_cutoff = now - timedelta(minutes=10)
    recent_cutoff = now - timedelta(hours=24)
    with get_db() as db:
        # Aggregate status counts in SQL instead of loading every fingerprint
        # row into memory and counting in Python.
        status_counts: dict[str, int] = dict(
            db.query(EpisodeFingerprint.status, func.count(EpisodeFingerprint.id))
            .group_by(EpisodeFingerprint.status)
            .all()
        )
        pending = status_counts.get("pending", 0)
        processing = status_counts.get("processing", 0)
        done = status_counts.get("done", 0)
        failed = status_counts.get("failed", 0)

        markers = db.query(func.count(SegmentMarker.id)).scalar() or 0
        recent = (
            db.query(func.count(EpisodeFingerprint.id))
            .filter(
                EpisodeFingerprint.status == "done",
                EpisodeFingerprint.updated_at.isnot(None),
                EpisodeFingerprint.updated_at >= recent_cutoff,
            )
            .scalar()
            or 0
        )
        stale_inflight_count = (
            db.query(func.count(EpisodeFingerprint.id))
            .filter(
                EpisodeFingerprint.status == "processing",
                EpisodeFingerprint.updated_at.isnot(None),
                EpisodeFingerprint.updated_at < stale_cutoff,
            )
            .scalar()
            or 0
        )
        recent_attempted_counts: dict[str, int] = dict(
            db.query(EpisodeFingerprint.status, func.count(EpisodeFingerprint.id))
            .filter(
                EpisodeFingerprint.status.in_(["done", "failed"]),
                EpisodeFingerprint.updated_at.isnot(None),
                EpisodeFingerprint.updated_at >= recent_cutoff,
            )
            .group_by(EpisodeFingerprint.status)
            .all()
        )
        recent_failed = recent_attempted_counts.get("failed", 0)
        recent_attempted_total = recent_failed + recent_attempted_counts.get("done", 0)
        failure_rate_pct = (
            round((recent_failed / recent_attempted_total) * 100, 1)
            if recent_attempted_total
            else 0.0
        )

        marker_keys = set(
            db.query(SegmentMarker.imdb_id, SegmentMarker.season).distinct().all()
        )
        community_counts: dict[str, dict[str, int]] = {}
        for source, status, count in (
            db.query(
                ExternalSegmentCache.source,
                ExternalSegmentCache.status,
                func.count(ExternalSegmentCache.id),
            )
            .group_by(ExternalSegmentCache.source, ExternalSegmentCache.status)
            .all()
        ):
            community_counts.setdefault(source, {})[status] = count

        # Season-level rollups only need a few columns; avoid materialising full
        # ORM rows (which carry large fingerprint payloads) for the whole table.
        season_rows_all = db.query(
            EpisodeFingerprint.imdb_id,
            EpisodeFingerprint.season,
            EpisodeFingerprint.status,
            EpisodeFingerprint.error_message,
        ).all()

    season_groups: dict[tuple[str, int], list] = {}
    for row in season_rows_all:
        season_groups.setdefault((row.imdb_id, row.season), []).append(row)

    seasons_no_stream = 0
    seasons_fingerprinted_no_markers = 0
    for key, season_rows in season_groups.items():
        if any(
            row.status == "failed"
            and "no_stream" in (row.error_message or "").lower().replace(" ", "_")
            for row in season_rows
        ):
            seasons_no_stream += 1
        if (
            season_rows
            and all(row.status == "done" for row in season_rows)
            and key not in marker_keys
        ):
            seasons_fingerprinted_no_markers += 1

    return {
        "queue": {
            "pending": pending,
            "processing": processing,
            "done": done,
            "failed": failed,
        },
        "inflight": get_fingerprint_worker_stats().get("inflight", 0),
        "total_markers": markers,
        "recent_completions_24h": recent,
        "stale_inflight_count": stale_inflight_count,
        "failure_rate_pct": failure_rate_pct,
        "seasons_no_stream": seasons_no_stream,
        "seasons_fingerprinted_no_markers": seasons_fingerprinted_no_markers,
        # Episodes looked up in each community database, by hit / none / error.
        "community_lookups": community_counts,
    }


def _relearn_season(imdb_id: str, season: int) -> int:
    """Delete a season's auto markers and re-derive them from its existing fingerprints.

    Markers-only: no audio is re-extracted, since the stored fingerprints stay valid — it
    is their interpretation (match tolerance, plausibility gates) that changes. Returns the
    number of auto markers deleted. Synchronous and CPU-heavy; call it off the event loop.
    """
    with get_db() as db:
        marker_query = db.query(SegmentMarker).filter_by(imdb_id=imdb_id, season=season)
        if hasattr(SegmentMarker, "source"):
            marker_query = marker_query.filter(
                (SegmentMarker.source.is_(None))
                | (~SegmentMarker.source.in_(["manual", "client_feedback"]))
            )
        if hasattr(SegmentMarker, "is_user_provided"):
            marker_query = marker_query.filter(
                (SegmentMarker.is_user_provided.is_(False))
                | (SegmentMarker.is_user_provided.is_(None))
            )
        deleted_markers = marker_query.delete(synchronize_session=False)
        # Mandatory: _try_learn_season short-circuits for a season whose stored signature
        # still matches, so without this the relearn is a silent no-op.
        db.query(SeasonLearningState).filter_by(imdb_id=imdb_id, season=season).delete()
        # Stale cross-season reference slices would otherwise re-seed the very markers
        # being cleared, via _apply_cross_season_references.
        db.query(SeasonSegmentReference).filter_by(
            imdb_id=imdb_id, season=season
        ).delete()
        db.commit()
    logger.info(
        "reset_marker_only_relearn_started imdb_id={} season={} deleted_auto_markers={} preserved_sources=[manual,client_feedback]",
        imdb_id,
        season,
        deleted_markers,
    )
    from workers.fingerprint_worker import (
        _try_learn_season,
        _pair_match_cache_clear_season,
    )

    # Force a clean recompute: drop any memoized pair matches for this season. The cache
    # keys on fingerprint hashes only, so it can outlive a matcher-parameter change.
    _pair_match_cache_clear_season(imdb_id, season)
    _try_learn_season(imdb_id, season)
    logger.info(
        "reset_marker_only_relearn_complete imdb_id={} season={} deleted_auto_markers={}",
        imdb_id,
        season,
        deleted_markers,
    )
    return deleted_markers


@router.post("/api/fingerprint-series/{imdb_id}/{season}/reset-learning")
async def reset_season_learning(
    request: Request, imdb_id: str, season: int, _=Depends(verify_admin)
):
    """Clear auto SegmentMarker rows and re-run matching from existing fingerprints."""
    logger.info("reset_started imdb_id={} season={}", imdb_id, season)
    deleted_markers = await asyncio.to_thread(_relearn_season, imdb_id, season)
    return {
        "status": "complete",
        "state": "complete",
        "deleted_markers": deleted_markers,
    }


_relearn_all_task: Optional[asyncio.Task] = None
_relearn_all_state: dict[str, Any] = {"state": "idle"}


def _run_relearn_all() -> None:
    """Re-derive markers for every fingerprinted season. Runs in a worker thread."""
    with get_db() as db:
        seasons = [
            (imdb_id, season)
            for imdb_id, season in db.query(
                EpisodeFingerprint.imdb_id, EpisodeFingerprint.season
            )
            .distinct()
            .all()
        ]
    seasons.sort()
    _relearn_all_state.update(
        {"state": "running", "seasons_total": len(seasons), "seasons_done": 0}
    )
    logger.info("relearn_all_started seasons={}", len(seasons))
    for imdb_id, season in seasons:
        try:
            _relearn_all_state["markers_deleted"] += _relearn_season(imdb_id, season)
        except Exception as exc:
            # One bad season must not abort the sweep.
            _relearn_all_state["failures"] += 1
            logger.warning(
                "relearn_all_season_failed imdb_id={} season={} error={}",
                imdb_id,
                season,
                exc,
            )
        _relearn_all_state["seasons_done"] += 1
    _relearn_all_state["state"] = "complete"
    logger.info(
        "relearn_all_complete seasons={} deleted_auto_markers={} failures={}",
        len(seasons),
        _relearn_all_state["markers_deleted"],
        _relearn_all_state["failures"],
    )


@router.post("/api/fingerprint/relearn-all")
async def relearn_all_seasons(request: Request, _=Depends(verify_admin)):
    """Re-derive every season's markers from the fingerprints already stored.

    Markers-only: nothing is re-downloaded and no audio is re-extracted. User-provided
    markers and all user feedback are preserved."""
    global _relearn_all_task
    if _relearn_all_task and not _relearn_all_task.done():
        raise HTTPException(409, "A global relearn is already running")
    _relearn_all_state.clear()
    _relearn_all_state.update(
        {
            "state": "starting",
            "seasons_total": 0,
            "seasons_done": 0,
            "markers_deleted": 0,
            "failures": 0,
            "started_at": utc_now().isoformat(),
        }
    )

    async def _run():
        try:
            await asyncio.to_thread(_run_relearn_all)
        except Exception as exc:
            _relearn_all_state.update({"state": "failed", "error": str(exc)})
            logger.error("relearn_all_failed error={}", exc)

    _relearn_all_task = asyncio.create_task(_run())
    return {"status": "started", **_relearn_all_state}


@router.get("/api/fingerprint/relearn-all/status")
async def relearn_all_status(request: Request, _=Depends(verify_admin)):
    """Progress for the global relearn started by POST /api/fingerprint/relearn-all."""
    return dict(_relearn_all_state)


@router.get("/api/fingerprint-series")
def get_fingerprint_series(
    request: Request,
    skip: int = 0,
    limit: int = DEFAULT_FINGERPRINT_SERIES_PAGE,
    imdb_id: str | None = None,
    _=Depends(verify_admin),
):
    skip = max(0, skip)
    limit = max(1, min(limit, MAX_FINGERPRINT_SERIES_PAGE))
    with get_db() as db:
        # Page over distinct series (imdb_id) so the payload and memory stay
        # bounded no matter how many fingerprint rows exist.
        series_query = db.query(EpisodeFingerprint.imdb_id).distinct()
        if imdb_id:
            series_query = series_query.filter(EpisodeFingerprint.imdb_id == imdb_id)
        total_series = series_query.count()
        page_imdb_ids = [
            r[0]
            for r in series_query.order_by(EpisodeFingerprint.imdb_id)
            .offset(skip)
            .limit(limit)
            .all()
        ]
        if not page_imdb_ids:
            return {
                "series": [],
                "total": total_series,
                "skip": skip,
                "limit": limit,
                "has_more": False,
            }

        rows = (
            db.query(EpisodeFingerprint)
            .filter(EpisodeFingerprint.imdb_id.in_(page_imdb_ids))
            .order_by(EpisodeFingerprint.imdb_id, EpisodeFingerprint.season)
            .all()
        )
        # Aggregate fingerprint counts keyed by (imdb_id, season)
        agg: dict[tuple, dict] = {}
        for row in rows:
            key = (row.imdb_id, row.season)
            if key not in agg:
                agg[key] = {
                    "total": 0,
                    "done": 0,
                    "failed": 0,
                    "pending": 0,
                    "processing": 0,
                    "done_episode_numbers": [],
                    "fingerprint_records": [],
                    "last_updated": None,
                }
            d = agg[key]
            d["total"] += 1
            fingerprint_payload = row.fingerprint or {}
            visual_runs = fingerprint_payload.get("tail_visual_runs") or []
            d["fingerprint_records"].append(
                {
                    "episode": row.episode,
                    "status": row.status,
                    "source_url_hash": row.source_url_hash,
                    "duration_sec": row.duration_sec,
                    "error_message": row.error_message,
                    "phases": fingerprint_payload.get("phases") or {},
                    "head_ready": bool(fingerprint_payload.get("head")),
                    "tail_ready": bool(fingerprint_payload.get("tail")),
                    "visual_ready": (
                        fingerprint_payload.get("tail_visual_version")
                        == "credits-visual-v3"
                    ),
                    "visual_run_count": len(visual_runs),
                    "best_visual_score": max(
                        [float(run.get("score") or 0) for run in visual_runs] or [0]
                    ),
                    "created_at": (
                        row.created_at.isoformat() if row.created_at else None
                    ),
                    "updated_at": (
                        row.updated_at.isoformat() if row.updated_at else None
                    ),
                }
            )
            if row.status == "done":
                d["done"] += 1
                d["done_episode_numbers"].append(row.episode)
            elif row.status == "failed":
                d["failed"] += 1
            elif row.status == "processing":
                d["processing"] += 1
            else:
                d["pending"] += 1
            if row.updated_at and (
                d["last_updated"] is None or row.updated_at > d["last_updated"]
            ):
                d["last_updated"] = row.updated_at

        # Which (imdb_id, season) pairs have any segment markers? (page-scoped)
        marker_keys = set(
            db.query(SegmentMarker.imdb_id, SegmentMarker.season)
            .filter(SegmentMarker.imdb_id.in_(page_imdb_ids))
            .distinct()
            .all()
        )
        marker_types: dict[tuple[str, int], set[str]] = {}
        marker_records: dict[tuple[str, int], list[dict[str, Any]]] = {}
        detection_states: dict[tuple[str, int], dict[str, bool]] = {}
        from app.segment_policy import season_segment_enabled

        for marker_imdb, marker_season in agg:
            detection_states[(marker_imdb, marker_season)] = {
                "intro": season_segment_enabled(
                    db, marker_imdb, marker_season, "intro"
                ),
                "credits": season_segment_enabled(
                    db, marker_imdb, marker_season, "credits"
                ),
            }

        for marker in (
            db.query(SegmentMarker)
            .filter(SegmentMarker.imdb_id.in_(page_imdb_ids))
            .order_by(
                SegmentMarker.imdb_id,
                SegmentMarker.season,
                SegmentMarker.episode,
                SegmentMarker.segment_type,
            )
        ):
            key = (marker.imdb_id, marker.season)
            marker_records.setdefault(key, []).append(
                {
                    "episode": marker.episode,
                    "type": marker.segment_type,
                    "start": marker.start_sec,
                    "end": marker.end_sec,
                    "confidence": marker.confidence,
                    "source": marker.source,
                    "detector_version": marker.detector_version,
                    "boundary_status": "confirmed",
                    "is_user_provided": bool(marker.is_user_provided),
                    "evidence": marker.evidence or {},
                }
            )
        for marker_imdb, marker_season, segment_type in (
            db.query(
                SegmentMarker.imdb_id,
                SegmentMarker.season,
                SegmentMarker.segment_type,
            )
            .filter(SegmentMarker.imdb_id.in_(page_imdb_ids))
            .distinct()
        ):
            marker_types.setdefault((marker_imdb, marker_season), set()).add(
                segment_type
            )

        title_map = {
            m.imdb_id: m.title
            for m in db.query(MediaMetadata)
            .filter(MediaMetadata.media_type == "tv")
            .filter(MediaMetadata.imdb_id.in_(page_imdb_ids))
            .all()
            if m.imdb_id
        }

        # Learning state keyed by (imdb_id, season) (page-scoped)
        learning_states: dict[tuple, SeasonLearningState] = {
            (s.imdb_id, s.season): s
            for s in db.query(SeasonLearningState)
            .filter(SeasonLearningState.imdb_id.in_(page_imdb_ids))
            .all()
        }

    # Group seasons under each imdb_id
    series_map: dict[str, list] = {}
    for (series_imdb_id, season), d in agg.items():
        ls = learning_states.get((series_imdb_id, season))
        series_map.setdefault(series_imdb_id, []).append(
            {
                "season": season,
                "episodes_total": d["total"],
                "episodes_fingerprinted": d["done"],
                "episodes_failed": d["failed"],
                "episodes_pending": d["pending"],
                "episodes_processing": d["processing"],
                "done_episode_numbers": sorted(d["done_episode_numbers"]),
                "fingerprint_records": sorted(
                    d["fingerprint_records"], key=lambda record: record["episode"]
                ),
                "marker_records": marker_records.get((series_imdb_id, season), []),
                "intro_detection_enabled": detection_states[(series_imdb_id, season)][
                    "intro"
                ],
                "credits_detection_enabled": detection_states[(series_imdb_id, season)][
                    "credits"
                ],
                "prelearning_enabled": SEASON_PRELEARNING_ENABLED,
                "prelearning_mode": SEASON_PRELEARNING_MODE,
                "prelearning_pending_count": d["pending"],
                "prelearning_processing_count": d["processing"],
                "prelearning_done_count": d["done"],
                "prelearning_failed_count": d["failed"],
                "last_prelearning_reason": ls.last_prelearning_reason if ls else None,
                "last_prelearning_at": (
                    ls.last_prelearning_at.isoformat()
                    if ls and ls.last_prelearning_at
                    else None
                ),
                "marker_types": sorted(
                    marker_types.get((series_imdb_id, season), set())
                ),
                "intro_marker": "intro"
                in marker_types.get((series_imdb_id, season), set()),
                "credits_marker": "credits"
                in marker_types.get((series_imdb_id, season), set()),
                "markers_generated": (series_imdb_id, season) in marker_keys,
                "last_updated": (
                    d["last_updated"].isoformat() if d["last_updated"] else None
                ),
                # Matcher diagnostics from SeasonLearningState
                "selected_learning_episodes": ls.selected_episodes if ls else None,
                "intro_pair_hits": ls.intro_pair_hits if ls else None,
                "credits_pair_hits": ls.credits_pair_hits if ls else None,
                "last_learning_reason": ls.last_reason if ls else None,
                "max_intro_candidate_start": (
                    (ls.diagnostics or {}).get("max_intro_candidate_start")
                    if ls
                    else None
                ),
                "intro_candidates_after_120s": (
                    (ls.diagnostics or {}).get("intro_candidates_after_120s")
                    if ls
                    else None
                ),
                "intro_reliability_reason": (
                    (ls.diagnostics or {}).get("intro_reliability_reason")
                    if ls
                    else None
                ),
                "credits_start_ratio": (
                    (ls.diagnostics or {}).get("credits_start_ratio") if ls else None
                ),
                "credits_reliability_reason": (
                    (ls.diagnostics or {}).get("credits_reliability_reason")
                    if ls
                    else None
                ),
                "intro_raw_candidate_count": (
                    (ls.diagnostics or {}).get("intro_raw_candidate_count")
                    if ls
                    else None
                ),
                "credits_raw_candidate_count": (
                    (ls.diagnostics or {}).get("credits_raw_candidate_count")
                    if ls
                    else None
                ),
                "intro_unique_pair_support": (
                    (ls.diagnostics or {}).get("intro_unique_pair_support")
                    if ls
                    else None
                ),
                "credits_unique_pair_support": (
                    (ls.diagnostics or {}).get("credits_unique_pair_support")
                    if ls
                    else None
                ),
                "intro_confidence": (
                    (ls.diagnostics or {}).get("intro_confidence") if ls else None
                ),
                "credits_confidence": (
                    (ls.diagnostics or {}).get("credits_confidence") if ls else None
                ),
                "detector_version": (
                    (ls.diagnostics or {}).get("detector_version") if ls else None
                ),
                "fingerprint_version": (
                    (ls.diagnostics or {}).get("fingerprint_version") if ls else None
                ),
                "selected_episodes": (
                    (ls.diagnostics or {}).get("selected_episodes") if ls else None
                ),
                "total_pairs": (
                    (ls.diagnostics or {}).get("total_pairs") if ls else None
                ),
                "per_episode_intro": (
                    (ls.diagnostics or {}).get("per_episode_intro") if ls else None
                ),
                "per_episode_credits": (
                    (ls.diagnostics or {}).get("per_episode_credits") if ls else None
                ),
                "saved_marker_types": (
                    (ls.diagnostics or {}).get("saved_marker_types") if ls else None
                ),
                "stale_marker_cleanup": (
                    (ls.diagnostics or {}).get("stale_marker_cleanup") if ls else None
                ),
                "episode_fingerprints": (
                    (ls.diagnostics or {}).get("episode_fingerprints") if ls else None
                ),
                "extraction_mode": (
                    (ls.diagnostics or {}).get("extraction_mode") if ls else None
                ),
                "last_marker_attempt_at": (
                    ls.attempted_at.isoformat() if ls and ls.attempted_at else None
                ),
            }
        )

    return {
        "series": [
            {
                "imdb_id": imdb_id,
                "title": title_map.get(imdb_id) or imdb_id,
                "seasons": sorted(seasons, key=lambda s: s["season"]),
            }
            for imdb_id, seasons in sorted(series_map.items())
        ],
        "total": total_series,
        "skip": skip,
        "limit": limit,
        "has_more": skip + len(page_imdb_ids) < total_series,
    }


def _admin_prelearn_user_key() -> str:
    """Choose the highest-bandwidth active user for admin stream resolution."""
    with get_db() as db:
        user = (
            db.query(User)
            .filter(User.is_active.is_(True), User.bandwidth_tier == "high")
            .order_by(User.last_login.desc(), User.created_at.desc())
            .first()
        )
        if user is None:
            user = (
                db.query(User)
                .filter(User.is_active.is_(True))
                .order_by(User.last_login.desc(), User.created_at.desc())
                .first()
            )
        if user:
            return user.user_key
    logger.warning("admin_prelearn_no_user_context")
    return ""


@router.post("/api/fingerprint-series/{imdb_id}/{season}/{episode}/retry")
async def retry_fingerprint_episode(
    imdb_id: str, season: int, episode: int, request: Request, _=Depends(verify_admin)
):
    canonical_id = f"{imdb_id}:{season}:{episode}"
    from workers.fingerprint_worker import (
        _resolve_prefetch_stream_url,
        queue_fingerprint_job,
    )

    user_key = _admin_prelearn_user_key()
    stream_url = _resolve_prefetch_stream_url(user_key, canonical_id)
    if not stream_url:
        raise HTTPException(400, "No stream available for retry")

    with get_db() as db:
        row = (
            db.query(EpisodeFingerprint)
            .filter_by(imdb_id=imdb_id, season=season, episode=episode)
            .first()
        )
        if row:
            row.status = "pending"
            row.error_message = None
            row.updated_at = utc_now()
            db.commit()

    queue_fingerprint_job(canonical_id, stream_url)
    logger.info("admin_retry_fingerprint_queued canonical_id={}", canonical_id)
    return {"status": "queued", "canonical_id": canonical_id}


@router.get("/api/fingerprint-logs")
async def get_fingerprint_logs(
    request: Request, _=Depends(verify_admin), after: int = 0, limit: int = 100
):
    limit = max(1, min(limit, 500))
    logs = [entry for entry in list(_fingerprint_logs) if entry.get("seq", 0) >= after]
    logs = logs[-limit:]
    return {
        "lines": logs,
        "total": _fingerprint_log_seq,
        "next": _fingerprint_log_seq,
    }


@router.post("/api/fingerprint-series/{imdb_id}/{season}/detection")
async def update_season_segment_detection(
    imdb_id: str, season: int, request: Request, _=Depends(verify_admin)
):
    """Enable or suppress automatic segment types for one season."""
    from app.segment_policy import (
        season_segment_enabled,
        set_season_segment_enabled,
    )

    body = await request.json()
    allowed = {"intro_enabled": "intro", "credits_enabled": "credits"}
    supplied = [key for key in allowed if key in body]
    if not supplied or any(not isinstance(body[key], bool) for key in supplied):
        raise HTTPException(
            400, "intro_enabled or credits_enabled must be supplied as booleans"
        )
    with get_db() as db:
        for key in supplied:
            set_season_segment_enabled(db, imdb_id, season, allowed[key], body[key])
        db.commit()
        state = {
            "intro_enabled": season_segment_enabled(db, imdb_id, season, "intro"),
            "credits_enabled": season_segment_enabled(db, imdb_id, season, "credits"),
        }
    logger.info(
        "season_segment_detection_updated imdb_id={} season={} state={}",
        imdb_id,
        season,
        state,
    )
    return {"status": "complete", **state}


@router.websocket("/ws/fingerprint-logs")
async def ws_fingerprint_logs(ws):
    """WebSocket endpoint for real-time fingerprint log streaming."""
    from starlette.websockets import WebSocketDisconnect

    global _fingerprint_log_loop
    try:
        _check_ws_origin(ws)
        _validate_admin_session_token(ws.cookies.get("admin_token"))
    except HTTPException:
        await ws.close(code=1008)
        return

    await ws.accept()
    try:
        _fingerprint_log_loop = asyncio.get_running_loop()
    except RuntimeError:
        _fingerprint_log_loop = None
    _fingerprint_ws_clients.add(ws)
    for entry in list(_fingerprint_logs):
        await ws.send_json(entry)
    try:
        while True:
            await ws.receive_text()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _fingerprint_ws_clients.discard(ws)


async def _broadcast_fingerprint_log(entry: dict[str, Any]):
    """Send a fingerprint log entry to all connected WebSocket clients."""
    global _fingerprint_ws_clients
    dead = set()
    for ws in _fingerprint_ws_clients:
        try:
            await ws.send_json(entry)
        except Exception:
            dead.add(ws)
    _fingerprint_ws_clients -= dead


@router.post("/api/fingerprint-series/{imdb_id}/{season}/prelearn")
async def prelearn_fingerprint_series(
    imdb_id: str, season: int, request: Request, _=Depends(verify_admin)
):
    body = (
        await request.json()
        if request.headers.get("content-type", "").startswith("application/json")
        else {}
    )
    mode = body.get("mode") or "conservative"
    limit = body.get("limit")
    try:
        limit = int(limit) if limit is not None else None
    except (TypeError, ValueError):
        limit = None
    if mode not in {"conservative", "aggressive"}:
        raise HTTPException(
            status_code=400, detail="mode must be conservative or aggressive"
        )
    from workers.fingerprint_worker import queue_season_prelearning

    user_key = _admin_prelearn_user_key()
    result = queue_season_prelearning(
        user_key, imdb_id, season, 1, reason="admin", mode=mode, limit=limit
    )
    return {
        "status": "queued",
        "queued": result.get("queued", 0),
        "skipped": {k: v for k, v in result.items() if k.startswith("skipped_")},
        "result": result,
        "user_context": "selected" if user_key else "none",
    }


#: Skip Intro / end-credits switches shown in their own Settings panel.
_SEGMENT_SETTING_KEYS = (
    "SKIP_INTRO_ENABLED",
    "SKIP_CREDITS_ENABLED",
    "INTRODB_ENABLED",
    "THEINTRODB_ENABLED",
    "FINGERPRINT_SEGMENTS_ENABLED",
)


@router.get("/api/settings")
def get_settings(request: Request, _=Depends(verify_admin)):
    """Get current settings with sensitive values masked."""
    with get_db() as db:
        overrides = {
            s.key: _setting_plaintext(s.key, s.value)
            for s in db.query(AdminSetting).all()
        }

    def val(key: str):
        return overrides.get(key, str(getattr(settings, key.lower(), "")))

    return {
        "api_keys": {
            "TMDB_API_KEY": _mask(val("TMDB_API_KEY")),
            "GEMINI_API_KEY": _mask(val("GEMINI_API_KEY")),
        },
        "app": {
            "APP_NAME": val("APP_NAME"),
            "BASE_URL": val("BASE_URL"),
            "CATALOG_SIZE": int(val("CATALOG_SIZE")),
            "CATALOG_PAGE_SIZE": int(val("CATALOG_PAGE_SIZE")),
            "CATALOG_SHUFFLE_HOURS": int(val("CATALOG_SHUFFLE_HOURS")),
            "CATALOG_MIN_RATING_SOURCES": int(val("CATALOG_MIN_RATING_SOURCES")),
            "CATALOG_MIN_VOTE_COUNT": int(val("CATALOG_MIN_VOTE_COUNT")),
            "CATALOG_MIN_QUALITY": float(val("CATALOG_MIN_QUALITY")),
            "CATALOG_NICHE_MIN_QUALITY": float(val("CATALOG_NICHE_MIN_QUALITY")),
            "CATALOG_NICHE_MIN_VOTE_COUNT": int(val("CATALOG_NICHE_MIN_VOTE_COUNT")),
            "CATALOG_NICHE_MIN_AGE_DAYS": int(val("CATALOG_NICHE_MIN_AGE_DAYS")),
            "CATALOG_QUALITY_WEIGHT": float(val("CATALOG_QUALITY_WEIGHT")),
            "RETAG_COST_CEILING_USD": float(val("RETAG_COST_CEILING_USD")),
            "LIBRARY_TAGGING_MONTHLY_CAP_USD": float(
                val("LIBRARY_TAGGING_MONTHLY_CAP_USD")
            ),
            "GEMINI_MODEL": val("GEMINI_MODEL"),
        },
        "features": {
            "ENABLE_UNIVERSAL_CATALOGS": val("ENABLE_UNIVERSAL_CATALOGS").lower()
            in ("true", "1"),
            "ENABLE_PERSONALIZED_CATALOGS": val("ENABLE_PERSONALIZED_CATALOGS").lower()
            in ("true", "1"),
            "ENABLE_PERSONALIZED_SYNC": val("ENABLE_PERSONALIZED_SYNC").lower()
            in ("true", "1"),
            "HIDE_FOREIGN": val("HIDE_FOREIGN").lower() in ("true", "1"),
            "HIDE_ADULT": val("HIDE_ADULT").lower() in ("true", "1"),
            "HIDE_UNRELEASED": val("HIDE_UNRELEASED").lower() in ("true", "1"),
            # Full video transcoding for browsers; off keeps the VPS to cheap
            # remux (copy) sessions only.
            "ALLOW_SERVER_TRANSCODE": val("ALLOW_SERVER_TRANSCODE").lower()
            in ("true", "1"),
            # Off: only owned and Watchlist titles are AI-tagged.
            "TAG_CATALOG_BEYOND_LIBRARY": val("TAG_CATALOG_BEYOND_LIBRARY").lower()
            in ("true", "1"),
        },
        "schedule": {
            "DAILY_UPDATE_ENABLED": val("DAILY_UPDATE_ENABLED").lower()
            in ("true", "1"),
            "DAILY_UPDATE_TIME": val("DAILY_UPDATE_TIME"),
        },
        "fingerprint": get_fingerprint_runtime_config(),
        "segments": {
            key: val(key).lower() in ("true", "1") for key in _SEGMENT_SETTING_KEYS
        },
    }


@router.post("/api/settings")
async def update_settings(request: Request, _=Depends(verify_admin)):
    """Update settings. Only non-empty values are saved."""
    body = await request.json()

    ALLOWED = {
        "TMDB_API_KEY",
        "GEMINI_API_KEY",
        "APP_NAME",
        "BASE_URL",
        "CATALOG_SIZE",
        "CATALOG_PAGE_SIZE",
        "CATALOG_SHUFFLE_HOURS",
        "CATALOG_MIN_RATING_SOURCES",
        "CATALOG_MIN_VOTE_COUNT",
        "CATALOG_MIN_QUALITY",
        "CATALOG_NICHE_MIN_QUALITY",
        "CATALOG_NICHE_MIN_VOTE_COUNT",
        "CATALOG_NICHE_MIN_AGE_DAYS",
        "CATALOG_QUALITY_WEIGHT",
        "RETAG_COST_CEILING_USD",
        "LIBRARY_TAGGING_MONTHLY_CAP_USD",
        "TAG_CATALOG_BEYOND_LIBRARY",
        "GEMINI_MODEL",
        "ENABLE_UNIVERSAL_CATALOGS",
        "ENABLE_PERSONALIZED_CATALOGS",
        "ENABLE_PERSONALIZED_SYNC",
        "HIDE_FOREIGN",
        "HIDE_ADULT",
        "HIDE_UNRELEASED",
        "ALLOW_SERVER_TRANSCODE",
        "DAILY_UPDATE_ENABLED",
        "DAILY_UPDATE_TIME",
        "FINGERPRINT_PRESET",
        *_SEGMENT_SETTING_KEYS,
    }

    updated = []
    with get_db() as db:
        for key, value in body.items():
            if key not in ALLOWED:
                continue
            str_val = str(value)
            if not str_val:
                continue

            stored = _stored_setting_value(key, str_val)
            existing = db.query(AdminSetting).filter(AdminSetting.key == key).first()
            if existing:
                existing.value = stored
                existing.updated_at = utc_now()
            else:
                db.add(AdminSetting(key=key, value=stored, updated_at=utc_now()))

            _apply_setting(key, str_val)
            updated.append(key)

        if any(
            key.startswith(
                ("CATALOG_", "HIDE_", "ENABLE_PERSONALIZED", "ENABLE_UNIVERSAL")
            )
            for key in updated
        ):
            from app.home_feed import invalidate_discovery

            invalidate_discovery(db)
        db.commit()

    # Restart scheduler if schedule settings changed
    schedule_keys = {"DAILY_UPDATE_ENABLED", "DAILY_UPDATE_TIME"}
    if schedule_keys & set(updated):
        await _restart_scheduler()

    return {"status": "ok", "updated": updated}


async def _restart_scheduler():
    """Restart the daily update scheduler with current settings."""
    from app.main import app

    # Cancel existing scheduler
    if getattr(app.state, "scheduler_task", None) is not None:
        app.state.scheduler_task.cancel()
        try:
            await app.state.scheduler_task
        except asyncio.CancelledError:
            pass
        app.state.scheduler_task = None
        logger.info("Scheduler stopped for reconfiguration")

    # Start new scheduler if enabled
    if settings.daily_update_enabled:
        from app.scheduler import run_scheduler

        app.state.scheduler_task = asyncio.create_task(run_scheduler())
        logger.info(f"Scheduler restarted: daily at {settings.daily_update_time} UTC")


@router.post("/api/build/start")
async def start_build(request: Request, _=Depends(verify_admin)):
    """Start the initial database build in the background."""
    global _active_build_task

    body = await request.json()
    movies = body.get("movies", DEFAULT_BUILD_MOVIES)
    shows = body.get("shows", DEFAULT_BUILD_SHOWS)
    # Acquisition without tagging. Lets the library's coverage be widened or
    # measured on its own, rather than only as a side effect of a run that also
    # pays Gemini for every new title and rebuilds every catalog row.
    fetch_only = bool(body.get("fetch_only"))

    # Check for running builds
    with get_db() as db:
        running = db.query(TaggingJob).filter(TaggingJob.status == "running").first()
        if running:
            raise HTTPException(409, "A build is already running")

    if (_active_build_task and not _active_build_task.done()) or _build_thread_alive:
        raise HTTPException(409, "A build task is already active")

    _start_log_capture()

    async def _run():
        from workers.initial_build import main as build_main, reset_build_stop

        # Clear any stale stop flag from a previous run before the thread starts.
        reset_build_stop()
        try:
            # Run in a thread so synchronous DB calls don't block the event loop
            await asyncio.to_thread(
                _run_build_sync, build_main, movies, shows, fetch_only
            )
        except asyncio.CancelledError:
            logger.warning("Build task was cancelled by user")
            _mark_running_jobs_cancelled()
        except Exception as e:
            logger.error(f"Build task failed: {e}")
        finally:
            _stop_log_capture()
            reset_build_stop()

    _active_build_task = asyncio.create_task(_run())
    logger.info(
        f"Database build started: {movies} movies, {shows} shows"
        + (" (fetch only)" if fetch_only else "")
    )
    return {
        "status": "started",
        "movies": movies,
        "shows": shows,
        "fetch_only": fetch_only,
    }


@router.post("/api/build/daily")
async def trigger_daily_update(request: Request, _=Depends(verify_admin)):
    """Manually trigger a daily update."""
    global _active_build_task

    if (_active_build_task and not _active_build_task.done()) or _build_thread_alive:
        raise HTTPException(409, "A build task is already active")

    with get_db() as db:
        running = db.query(TaggingJob).filter(TaggingJob.status == "running").first()
        if running:
            raise HTTPException(409, "A build is already running")

    _start_log_capture()

    async def _run():
        try:
            from workers.isolated_jobs import run_daily_update_off_loop

            # The facade runs the job on a private worker loop and forwards UI
            # cancellation into that loop. No SQLAlchemy session or ORM object
            # crosses the thread boundary.
            await run_daily_update_off_loop()
        except asyncio.CancelledError:
            logger.warning("Daily update was cancelled by user")
            _mark_running_jobs_cancelled()
        except asyncio.TimeoutError:
            # run_daily_update already marked the job 'timed_out'; don't overwrite
            # that with 'cancelled' here.
            logger.error("Manual daily update hit its deadline and was killed")
        except Exception as e:
            logger.error(f"Daily update task failed: {e}")
        finally:
            _stop_log_capture()

    _active_build_task = asyncio.create_task(_run())
    logger.info("Manual daily update triggered")
    return {"status": "started"}


@router.post("/api/sync-users")
async def sync_all_users_endpoint(request: Request, _=Depends(verify_admin)):
    """Launch catalog sync for all users in the background.

    Returns immediately so the server stays responsive for meta/catalog
    requests while the sync runs.
    """
    from workers.isolated_jobs import sync_all_users_off_loop

    async def _run_sync():
        try:
            stats = await sync_all_users_off_loop()
            logger.info(f"Background sync complete: {stats}")
            _clear_catalog_cache("user sync")
        except Exception as e:
            logger.error(f"Background sync failed: {e}")

    asyncio.create_task(_run_sync())
    return {"status": "started", "message": "Sync running in background"}


@router.get("/api/build/status")
def get_build_status(request: Request, _=Depends(verify_admin)):
    """Get current build progress."""
    from workers.initial_build import pause_event

    with get_db() as db:
        running_job = (
            db.query(TaggingJob)
            .filter(TaggingJob.status == "running")
            .order_by(TaggingJob.started_at.desc())
            .first()
        )

        if running_job:
            movies_done = (
                db.query(func.count(func.distinct(MovieTag.tmdb_id)))
                .filter(MovieTag.media_type == "movie")
                .scalar()
                or 0
            )
            shows_done = (
                db.query(func.count(func.distinct(MovieTag.tmdb_id)))
                .filter(MovieTag.media_type == "tv")
                .scalar()
                or 0
            )
            elapsed = (utc_now() - running_job.started_at).total_seconds()

            return {
                "running": True,
                "paused": not pause_event.is_set(),
                "job_type": running_job.job_type,
                "started_at": running_job.started_at.isoformat(),
                "elapsed_seconds": int(elapsed),
                "movies_tagged": movies_done,
                "shows_tagged": shows_done,
            }

    task_running = _active_build_task is not None and not _active_build_task.done()
    return {"running": task_running, "paused": False}


def _mark_running_jobs_cancelled():
    """Mark any running TaggingJob rows as cancelled."""
    try:
        with get_db() as db:
            for job in db.query(TaggingJob).filter(TaggingJob.status == "running"):
                job.status = "cancelled"
                job.error_message = "Cancelled by user"
                job.completed_at = utc_now()
            db.commit()
    except Exception as exc:
        logger.error(f"Failed to mark jobs cancelled: {exc}")


@router.post("/api/build/pause")
async def pause_build(request: Request, _=Depends(verify_admin)):
    """Pause the currently running build between batches."""
    from workers.initial_build import pause_event

    if _active_build_task is None or _active_build_task.done():
        # Check if there's a DB-level running job (orphaned after restart)
        with get_db() as db:
            running = (
                db.query(TaggingJob).filter(TaggingJob.status == "running").first()
            )
            if not running:
                raise HTTPException(404, "No build is currently running")

    pause_event.clear()

    # Persist to DB so pause survives container restart
    with get_db() as db:
        existing = (
            db.query(AdminSetting).filter(AdminSetting.key == "BUILD_PAUSED").first()
        )
        if existing:
            existing.value = "true"
            existing.updated_at = utc_now()
        else:
            db.add(AdminSetting(key="BUILD_PAUSED", value="true", updated_at=utc_now()))
        db.commit()

    logger.info("Build paused by user")
    return {"status": "paused"}


@router.post("/api/build/resume")
async def resume_build(request: Request, _=Depends(verify_admin)):
    """Resume a paused build. If the container restarted while paused, re-launches it."""
    global _active_build_task
    from workers.initial_build import pause_event

    # Clear the pause flag in DB
    with get_db() as db:
        existing = (
            db.query(AdminSetting).filter(AdminSetting.key == "BUILD_PAUSED").first()
        )
        if existing:
            existing.value = "false"
            existing.updated_at = utc_now()
            db.commit()

    # If the build task is alive, just unpause it
    if _active_build_task is not None and not _active_build_task.done():
        pause_event.set()
        logger.info("Build resumed by user")
        return {"status": "resumed"}

    # No live task — check for an orphaned running job (container restarted while paused)
    with get_db() as db:
        orphaned = (
            db.query(TaggingJob)
            .filter(TaggingJob.status == "running")
            .order_by(TaggingJob.started_at.desc())
            .first()
        )
        if orphaned and orphaned.job_metadata:
            movies = orphaned.job_metadata.get("movies_target", DEFAULT_BUILD_MOVIES)
            shows = orphaned.job_metadata.get("shows_target", DEFAULT_BUILD_SHOWS)
        else:
            raise HTTPException(404, "No paused build found to resume")

    # Mark orphaned job and re-launch
    pause_event.set()
    _relaunch_build(movies, shows, reason="Resumed after container restart")
    logger.info("Build re-launched after resume (container had restarted)")
    return {"status": "resumed", "relaunched": True}


def _relaunch_build(
    movies: int, shows: int, reason: str = "Auto-resumed", fetch_only: bool = False
):
    """Re-launch the build, marking any orphaned jobs.

    ``fetch_only`` defaults to False: a resumed build is a full build unless
    told otherwise. The job row does not record the flag, so an interrupted
    fetch-only run resumes as a normal one -- deliberate, since a resume is
    reconstructing intent from a crash and the safe reconstruction is the
    complete pipeline.
    """
    global _active_build_task

    with get_db() as db:
        for job in db.query(TaggingJob).filter(TaggingJob.status == "running"):
            job.status = "cancelled"
            job.error_message = reason
            job.completed_at = utc_now()
        db.commit()

    _start_log_capture()

    async def _run():
        from workers.initial_build import main as build_main, reset_build_stop

        reset_build_stop()
        try:
            await asyncio.to_thread(
                _run_build_sync, build_main, movies, shows, fetch_only
            )
        except asyncio.CancelledError:
            logger.warning("Build task was cancelled by user")
            _mark_running_jobs_cancelled()
        except Exception as e:
            logger.error(f"Build task failed: {e}")
        finally:
            _stop_log_capture()
            reset_build_stop()

    _active_build_task = asyncio.create_task(_run())


#: Job types ``auto_resume_build`` knows how to relaunch. Everything else is
#: marked interrupted and left alone.
#:
#: This list is the whole point of the function's ``job_type`` filter. Without
#: it, an orphaned row of ANY type reached ``job_metadata.get("movies_target",
#: DEFAULT_BUILD_MOVIES)`` and fell through to a legacy 100k-movie /50k-show
#: ``initial_build`` — so a container restart during a re-tag would silently
#: cancel it and start a multi-hour TMDB crawl plus a full tagging pass at the
#: old vocabulary, with no vote floor and no cost ceiling. A restart must never
#: start work the operator did not ask for.
RESUMABLE_JOB_TYPES = ("database_build",)


async def auto_resume_build():
    """Called on app startup. Re-launches orphaned builds if not paused.

    Only ``database_build`` is relaunched. A ``manual_retag`` needs no relaunch
    to be recoverable: its selection is "titles with no ``movie_tags`` row at
    the current ``tagger_version``", so pressing Start again resumes exactly
    where it stopped. Restarting it automatically would spend money the
    operator did not authorize on this boot.
    """
    from workers.initial_build import pause_event

    with get_db() as db:
        # Any orphaned job is dead the moment the container went down -- the
        # asyncio task died with it. Close them all out so the row does not
        # sit "running" forever and block the next start with a 409.
        interrupted = (
            db.query(TaggingJob)
            .filter(
                TaggingJob.status == "running",
                TaggingJob.job_type.notin_(RESUMABLE_JOB_TYPES),
            )
            .all()
        )
        for job in interrupted:
            job.status = "cancelled"
            job.completed_at = utc_now()
            job.error_message = (
                f"Interrupted by container restart. A {job.job_type} resumes "
                "from where it stopped -- start it again when ready."
            )
        if interrupted:
            db.commit()
            logger.warning(
                f"Marked {len(interrupted)} orphaned job(s) cancelled: "
                + ", ".join(f"{j.job_type}#{j.id}" for j in interrupted)
            )

        orphaned = (
            db.query(TaggingJob)
            .filter(
                TaggingJob.status == "running",
                TaggingJob.job_type.in_(RESUMABLE_JOB_TYPES),
            )
            .order_by(TaggingJob.started_at.desc())
            .first()
        )
        if not orphaned:
            return

        # Check if build was paused when container went down
        paused_setting = (
            db.query(AdminSetting).filter(AdminSetting.key == "BUILD_PAUSED").first()
        )
        is_paused = paused_setting and paused_setting.value == "true"

        if not orphaned.job_metadata:
            # Can't resume without params — mark as interrupted
            orphaned.status = "cancelled"
            orphaned.error_message = (
                "Interrupted by container restart (no params to resume)"
            )
            orphaned.completed_at = utc_now()
            db.commit()
            logger.warning(
                "Found orphaned build job but no metadata — marked cancelled"
            )
            return

        movies = orphaned.job_metadata.get("movies_target", DEFAULT_BUILD_MOVIES)
        shows = orphaned.job_metadata.get("shows_target", DEFAULT_BUILD_SHOWS)

    if is_paused:
        pause_event.clear()
        logger.info(
            f"Found paused build (movies={movies}, shows={shows}). "
            "Waiting for manual resume via admin panel."
        )
        # Re-launch the build but it will immediately block on pause_event
        _relaunch_build(movies, shows, reason="Container restarted while paused")
    else:
        logger.info(
            f"Auto-resuming interrupted build (movies={movies}, shows={shows})..."
        )
        _relaunch_build(movies, shows, reason="Container restarted — auto-resuming")


@router.post("/api/build/stop")
async def stop_build(request: Request, _=Depends(verify_admin)):
    """Cancel the currently running build task.

    Handles two scenarios:
    1. Normal: asyncio task is alive -> cancel it
    2. Orphaned: DB says running but task ref is gone (e.g. after server restart
       or code redeploy) -> just mark DB jobs as cancelled
    """
    global _active_build_task

    has_task = _active_build_task is not None and not _active_build_task.done()

    # Check DB for running jobs even if task ref is gone
    with get_db() as db:
        has_db_job = (
            db.query(TaggingJob).filter(TaggingJob.status == "running").first()
            is not None
        )

    if not has_task and not has_db_job:
        raise HTTPException(404, "No build is currently running")

    # Cooperative stop: signal the build thread to unwind at its next batch
    # boundary. We must NOT cancel the asyncio task — cancelling only unwinds the
    # awaiting coroutine while the build THREAD keeps running its own event loop,
    # which orphaned the thread and let a second build start against the same
    # tables (and later overwrote the 'cancelled' row back to 'completed').
    from workers.initial_build import request_build_stop

    request_build_stop()

    if has_task and _active_build_task is not None:
        try:
            # Shield so the wait_for timeout doesn't cancel the run — we only wait.
            await asyncio.wait_for(asyncio.shield(_active_build_task), timeout=8.0)
            _active_build_task = None  # thread exited cleanly within the window
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # Still finishing its current batch; it will exit at the next
            # checkpoint and _run()'s finally cleans up. Keep the task ref so
            # start_build stays blocked (via _build_thread_alive) until it does.
            logger.info(
                "Build stop requested; thread will exit at its next batch boundary"
            )
    else:
        _active_build_task = None

    # Mark DB jobs as cancelled (safety net; the build also self-marks on stop).
    _mark_running_jobs_cancelled()
    _stop_log_capture()

    # Clear pause flag so a future build starts clean
    with get_db() as db:
        paused = (
            db.query(AdminSetting).filter(AdminSetting.key == "BUILD_PAUSED").first()
        )
        if paused:
            paused.value = "false"
            db.commit()

    # Ensure pause_event is reset for next build
    from workers.initial_build import pause_event

    pause_event.set()

    logger.info("Build stopped by user")
    return {"status": "stopped"}


@router.get("/api/build/logs")
async def get_build_logs(
    request: Request,
    _=Depends(verify_admin),
    after: int = 0,
):
    """
    Return captured build log lines.

    Query param 'after' returns only lines after that index,
    so the UI can poll incrementally without re-fetching everything.
    Auto-starts log capture if a build is running but capture isn't active
    (e.g. build started before code was deployed).
    """
    # Auto-start capture for builds that started before this code was deployed
    if _build_log_sink_id is None:
        with get_db() as db:
            running = (
                db.query(TaggingJob).filter(TaggingJob.status == "running").first()
            )
        if running:
            _start_log_capture()

    logs = list(_build_logs)
    total = len(logs)

    if after > 0:
        logs = logs[after:]

    return {"lines": logs, "total": total}


@router.websocket("/ws/build-logs")
async def ws_build_logs(ws):
    """WebSocket endpoint for real-time build log streaming."""
    from starlette.websockets import WebSocketDisconnect

    try:
        _check_ws_origin(ws)
        _validate_admin_session_token(ws.cookies.get("admin_token"))
    except HTTPException:
        await ws.close(code=1008)
        return

    await ws.accept()
    _ws_clients.add(ws)
    # Send existing logs as initial payload
    for line in list(_build_logs):
        await ws.send_text(line)
    try:
        while True:
            # Keep connection alive; client doesn't send data
            await ws.receive_text()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _ws_clients.discard(ws)


async def _broadcast_log(line: str):
    """Send a log line to all connected WebSocket clients."""
    global _ws_clients
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(line)
        except Exception:
            dead.add(ws)
    _ws_clients -= dead


@router.get("/api/debug")
def debug_catalogs(request: Request, _=Depends(verify_admin)):
    """Full diagnostic of the catalog pipeline to identify issues."""
    from app.catalog_generator import CatalogGenerator

    try:
        with get_db() as db:
            # Layer 1: Tags
            tag_count = db.query(func.count(Tag.id)).scalar() or 0

            # Layer 2: MovieTags (tagged items)
            movies_tagged = (
                db.query(func.count(func.distinct(MovieTag.tmdb_id)))
                .filter(MovieTag.media_type == "movie")
                .scalar()
                or 0
            )
            shows_tagged = (
                db.query(func.count(func.distinct(MovieTag.tmdb_id)))
                .filter(MovieTag.media_type == "tv")
                .scalar()
                or 0
            )

            # Layer 3: Metadata
            metadata_count = db.query(func.count(MediaMetadata.tmdb_id)).scalar() or 0

            # Layer 4: Universal categories
            categories = (
                db.query(UniversalCategory)
                .filter(
                    UniversalCategory.is_active.is_(True),
                    UniversalCategory.source == "generated",
                )
                .order_by(UniversalCategory.sort_order)
                .all()
            )

            # Layer 5: Pre-computed catalog content
            total_catalog_items = (
                db.query(func.count(UniversalCatalogContent.tmdb_id))
                .join(
                    UniversalCategory,
                    UniversalCategory.id == UniversalCatalogContent.category_id,
                )
                .filter(UniversalCategory.source == "generated")
                .scalar()
                or 0
            )

            # Per-category breakdown
            # Snapshot category attributes up-front so a later rollback
            # can't trigger DetachedInstanceError on lazy-refresh.
            cat_snapshots = [
                {
                    "id": cat.id,
                    "name": cat.name,
                    "media_type": cat.media_type,
                    "tag_formula": cat.tag_formula,
                }
                for cat in categories
            ]

            category_details = []
            generator = CatalogGenerator(db)
            for cat_info in cat_snapshots:
                try:
                    content_count = (
                        db.query(func.count(UniversalCatalogContent.tmdb_id))
                        .filter(UniversalCatalogContent.category_id == cat_info["id"])
                        .scalar()
                        or 0
                    )
                except Exception:
                    db.rollback()
                    content_count = -1
                # Check how many items would match the formula (live query)
                # Wrapped in try/except because DB contention during active
                # builds can cause this to fail
                try:
                    cat_obj = (
                        db.query(UniversalCategory)
                        .filter(UniversalCategory.id == cat_info["id"])
                        .first()
                    )
                    if cat_obj:
                        potential_matches = len(
                            generator.generate_universal_catalog_ids(cat_obj, limit=5)
                        )
                    else:
                        potential_matches = -1
                except Exception:
                    db.rollback()
                    potential_matches = -1  # indicates query failed
                category_details.append(
                    {
                        "id": cat_info["id"],
                        "name": cat_info["name"],
                        "media_type": cat_info["media_type"],
                        "tag_formula": cat_info["tag_formula"],
                        "pre_computed_items": content_count,
                        "potential_matches_sample": potential_matches,
                    }
                )

            # Layer 6: Last tagging job status
            last_job = (
                db.query(TaggingJob).order_by(TaggingJob.started_at.desc()).first()
            )

            # Build diagnosis
            issues = []
            if tag_count == 0:
                issues.append("No tags exist. Run a database build to create tags.")
            if movies_tagged == 0 and shows_tagged == 0:
                issues.append("No items have been tagged yet.")
            if metadata_count == 0:
                issues.append(
                    "No metadata cached. Items won't show posters/titles in clients."
                )
            if len(categories) == 0:
                issues.append("No active universal categories found.")
            if total_catalog_items == 0 and (movies_tagged > 0 or shows_tagged > 0):
                issues.append(
                    "CRITICAL: Items are tagged but catalog content table is empty. "
                    "The catalog generation step likely didn't run. "
                    "Use 'Regenerate Catalogs' to fix this."
                )

        return {
            "pipeline": {
                "tags": tag_count,
                "movies_tagged": movies_tagged,
                "shows_tagged": shows_tagged,
                "metadata_cached": metadata_count,
                "active_categories": len(categories),
                "pre_computed_catalog_items": total_catalog_items,
            },
            "categories": category_details,
            "last_job": {
                "type": last_job.job_type if last_job else None,
                "status": last_job.status if last_job else None,
                "error": (last_job.error_message or "")[:500] if last_job else None,
            },
            "issues": issues,
            "healthy": len(issues) == 0,
        }
    except Exception as e:
        logger.error(f"Diagnostic endpoint failed: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Diagnostic failed: {type(e).__name__}: {e}",
        )


@router.post("/api/catalogs/regenerate")
def regenerate_catalogs(request: Request, _=Depends(verify_admin)):
    """Compile and atomically publish synthesized universal catalogs.

    Clears the serving cache afterwards. Without that the rows are rebuilt in
    the database and *nothing changes for a user* until ``cache_ttl`` (1 h)
    expires -- which reads as "the button did nothing" and invites a second,
    equally invisible press. The upgrade pipeline has always cleared it
    (``workers/upgrade.py``); this endpoint is the same operation reached by a
    different button and was missed.
    """
    from app.category_synth import rebuild_synthesized_shelves
    from workers.upgrade import require_catalog_publication_ready

    with get_db() as db:
        # Sanity check: are there tagged items?
        tagged_count = (
            db.query(func.count(func.distinct(MovieTag.tmdb_id))).scalar() or 0
        )
        if tagged_count == 0:
            raise HTTPException(
                400,
                "No tagged items found. Run a build first to tag movies/shows.",
            )
        try:
            require_catalog_publication_ready(db)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        try:
            synth_stats = rebuild_synthesized_shelves(db)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        new_total = (
            db.query(func.count(UniversalCatalogContent.tmdb_id))
            .join(
                UniversalCategory,
                UniversalCategory.id == UniversalCatalogContent.category_id,
            )
            .filter(UniversalCategory.source == "generated")
            .scalar()
            or 0
        )

    _clear_catalog_cache("catalog regeneration")
    logger.info(f"Catalog regeneration complete: {new_total} total items")
    return {
        "status": "ok",
        "total_catalog_items": new_total,
        "synthesis": synth_stats,
    }


# ---- Invite Codes ----


@router.get("/api/invites")
def list_invites(request: Request, _=Depends(verify_admin)):
    """List all invite codes."""
    with get_db() as db:
        codes = db.query(InviteCode).order_by(InviteCode.created_at.desc()).all()
        return [
            {
                "code": c.code,
                "invite_url": f"{settings.base_url}/account/register?invite={c.code}",
                "label": c.label,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "expires_at": c.expires_at.isoformat() if c.expires_at else None,
                "is_used": c.is_used,
                "used_at": c.used_at.isoformat() if c.used_at else None,
                "used_by": c.used_by,
            }
            for c in codes
        ]


@router.post("/api/invites")
async def create_invite(request: Request, _=Depends(verify_admin)):
    """Generate a new one-time invite code."""
    body = await request.json()
    label = body.get("label", "")
    expires_hours = body.get("expires_hours")  # None = never expires

    code = secrets.token_urlsafe(24)

    expires_at = None
    if expires_hours:
        expires_at = utc_now() + timedelta(hours=int(expires_hours))

    with get_db() as db:
        db.add(
            InviteCode(
                code=code,
                label=label,
                expires_at=expires_at,
            )
        )

    return {
        "code": code,
        "invite_url": f"{settings.base_url}/account/register?invite={code}",
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


@router.delete("/api/invites/{code}")
async def delete_invite(code: str, request: Request, _=Depends(verify_admin)):
    """Delete an invite code."""
    with get_db() as db:
        deleted = db.query(InviteCode).filter(InviteCode.code == code).delete()
        if not deleted:
            raise HTTPException(404, "Invite code not found")
    return {"status": "ok"}


# ---- Users ----


@router.get("/api/users")
def list_users(request: Request, _=Depends(verify_admin)):
    """List all viewing profiles, enriched with their account's identity info.

    Each row is a profile (one native API user_key). Identity fields
    (email, 2FA, admin, passkeys) reflect the profile's owning Account, so
    multiple profiles of the same account share those values.
    """
    with get_db() as db:
        users = db.query(User).order_by(User.created_at.desc()).all()

        # Batch the per-user counts into grouped queries instead of running
        # scalar COUNT queries per user (an N+1 that scales with user count).
        catalog_counts: dict[int, int] = dict(
            db.query(UserCatalog.user_id, func.count(UserCatalog.id))
            .filter(UserCatalog.is_active.is_(True))
            .group_by(UserCatalog.user_id)
            .all()
        )
        # Passkeys are account-scoped now.
        passkey_counts: dict[int, int] = dict(
            db.query(PasskeyCredential.account_id, func.count(PasskeyCredential.id))
            .group_by(PasskeyCredential.account_id)
            .all()
        )
        accounts_by_id = {a.id: a for a in db.query(Account).all()}

        user_list = []
        for u in users:
            account = accounts_by_id.get(u.account_id)
            catalog_count = catalog_counts.get(u.id, 0)
            passkey_count = passkey_counts.get(u.account_id, 0)
            exempt_until = getattr(account, "passkey_exempt_until", None)
            user_list.append(
                {
                    "id": u.id,
                    "account_id": u.account_id,
                    "user_key": u.user_key[:8] + "...",
                    "is_active": u.is_active,
                    "auth_source": getattr(u, "auth_source", "local"),
                    "email": getattr(account, "email", None),
                    "display_name": getattr(u, "display_name", None),
                    "totp_enabled": getattr(account, "totp_enabled", False),
                    "passkey_count": passkey_count,
                    "passkey_exempt_until": (
                        exempt_until.isoformat() if exempt_until else None
                    ),
                    "bandwidth_tier": getattr(u, "bandwidth_tier", "high"),
                    "is_admin": getattr(account, "is_admin", False),
                    "created_at": u.created_at.isoformat() if u.created_at else None,
                    "last_login": u.last_login.isoformat() if u.last_login else None,
                    "last_sync": u.last_sync.isoformat() if u.last_sync else None,
                    "catalog_count": catalog_count,
                }
            )

        return user_list


@router.post("/api/users/{user_id}/toggle")
async def toggle_user(user_id: int, request: Request, _=Depends(verify_admin)):
    """Enable or disable a user."""
    with get_db() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(404, "User not found")
        user.is_active = not user.is_active
        new_status = user.is_active
        user_key = user.user_key
        db.commit()
    try:
        from app.main import _user_cache

        _user_cache.pop(user_key, None)
    except Exception:
        pass
    return {"status": "ok", "is_active": new_status}


@router.delete("/api/users/{user_id}")
async def delete_user(user_id: int, request: Request, _=Depends(verify_admin)):
    """Delete a user and their catalogs."""
    with get_db() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(404, "User not found")
        user_key = user.user_key
        db.delete(user)
        db.commit()
    try:
        from app.main import _user_cache

        _user_cache.pop(user_key, None)
    except Exception:
        pass
    return {"status": "ok"}


@router.post("/api/users/{user_id}/bandwidth")
async def set_user_bandwidth(user_id: int, request: Request, _=Depends(verify_admin)):
    """Set a user's bandwidth tier (low or high)."""
    body = await request.json()
    tier = body.get("tier", "high")
    if tier not in ("low", "high"):
        raise HTTPException(400, "tier must be 'low' or 'high'")

    with get_db() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(404, "User not found")
        user.bandwidth_tier = tier
        db.commit()
        user_key = user.user_key

    # Evict from user cache so the change takes effect immediately
    from app.main import _user_cache

    _user_cache.pop(user_key, None)

    return {"status": "ok", "bandwidth_tier": tier}


# ---- 2FA Reset ----


@router.get("/api/live-activity")
async def live_activity(request: Request, _=Depends(verify_admin)):
    from app.playback_activity import activity_snapshot
    from app.sources import registry as source_registry

    def snapshot():
        with get_db() as db:
            return activity_snapshot(db)

    data = await asyncio.to_thread(snapshot)
    # Optional per-provider transfer views (e.g. an addon's upstream proxy).
    upstream = []
    for provider in source_registry.enabled_providers():
        try:
            view = provider.live_activity()
            if asyncio.iscoroutine(view):
                view = await view
        except Exception:
            view = {
                "state": "unavailable",
                "streams": [],
                "summary": {},
                "updated_at": None,
                "error": f"{provider.label} statistics are unavailable",
            }
        if view:
            upstream.append(
                {"provider_id": provider.id, "label": provider.label, **view}
            )
    data["upstream"] = upstream
    return data


def _account_for_profile(db, user_id: int) -> Account:
    """Resolve the owning account for a profile id, for identity-level ops."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    account = (
        db.query(Account).filter(Account.id == user.account_id).first()
        if user.account_id
        else None
    )
    if not account:
        raise HTTPException(404, "Account not found")
    return account


@router.post("/api/users/{user_id}/reset-2fa")
async def reset_user_2fa(user_id: int, request: Request, _=Depends(verify_admin)):
    """Disable 2FA for a profile's account (admin override)."""
    with get_db() as db:
        account = _account_for_profile(db, user_id)
        if not account.totp_enabled:
            raise HTTPException(400, "User does not have 2FA enabled")
        account.totp_enabled = False
        account.totp_secret = None
        db.commit()
        logger.info(f"Admin reset 2FA for account {account.id}")
    return {"status": "ok"}


@router.post("/api/users/{user_id}/reset-passkeys")
async def reset_user_passkeys(user_id: int, request: Request, _=Depends(verify_admin)):
    """Remove all passkeys for a profile's account so they can recover with password."""
    with get_db() as db:
        account = _account_for_profile(db, user_id)
        deleted = (
            db.query(PasskeyCredential)
            .filter(PasskeyCredential.account_id == account.id)
            .delete()
        )
        db.commit()
        logger.info(f"Admin reset {deleted} passkeys for account {account.id}")
    return {"status": "ok", "deleted": deleted}


@router.post("/api/users/{user_id}/passkey-exemption")
async def set_user_passkey_exemption(
    user_id: int, request: Request, _=Depends(verify_admin)
):
    """Grant or clear a temporary exemption from the passkey requirement.

    The escape hatch for someone who cannot enroll — typically a browser without
    WebAuthn — so that a hard requirement never locks an account out permanently.
    """
    body = await request.json()
    with get_db() as db:
        account = _account_for_profile(db, user_id)
        if body.get("clear"):
            account.passkey_exempt_until = None
            db.commit()
            logger.info(f"Admin cleared passkey exemption for account {account.id}")
            return {"status": "ok", "passkey_exempt_until": None}

        try:
            days = int(body.get("days", 7))
        except (TypeError, ValueError):
            raise HTTPException(400, "days must be a whole number")
        if not 1 <= days <= 90:
            raise HTTPException(400, "days must be between 1 and 90")

        until = utc_now() + timedelta(days=days)
        account.passkey_exempt_until = until
        db.commit()
        logger.info(
            f"Admin granted account {account.id} a passkey exemption for {days} day(s)"
        )
        return {"status": "ok", "passkey_exempt_until": until.isoformat()}


@router.post("/api/users/{user_id}/admin")
async def set_user_admin(user_id: int, request: Request, _=Depends(verify_admin)):
    """Promote or demote a profile's account as admin."""
    body = await request.json()
    is_admin = body.get("is_admin", False)
    with get_db() as db:
        account = _account_for_profile(db, user_id)
        account.is_admin = bool(is_admin)
        db.commit()
        logger.info(f"Account {account.id} admin status set to {account.is_admin}")
    return {"status": "ok", "is_admin": is_admin}


# ---- Sessions Management ----


@router.get("/api/sessions")
def list_sessions(request: Request, _=Depends(verify_admin)):
    """List all active user sessions and pairing sessions."""
    now = utc_now()
    with get_db() as db:
        # Account web sessions
        user_sessions = (
            db.query(UserSession, Account)
            .join(Account, UserSession.account_id == Account.id)
            .filter(UserSession.expires_at > now)
            .order_by(UserSession.created_at.desc())
            .all()
        )
        web_sessions = [
            {
                "token": s.token[:8] + "...",
                "token_full": s.token,
                "user_id": s.active_profile_id,
                "username": a.email or f"Account #{a.id}",
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "expires_at": s.expires_at.isoformat() if s.expires_at else None,
                "type": "web",
            }
            for s, a in user_sessions
        ]

        # Pairing sessions (only unclaimed and not expired)
        pairing_sessions = (
            db.query(AppPairingSession, User)
            .join(User, AppPairingSession.user_id == User.id)
            .filter(
                AppPairingSession.expires_at > now,
                AppPairingSession.claimed == False,  # noqa: E712
            )
            .order_by(AppPairingSession.created_at.desc())
            .all()
        )
        pairing = [
            {
                "token": s.token[:8] + "...",
                "token_full": s.token,
                "user_id": s.user_id,
                "username": u.display_name or u.email or f"User #{u.id}",
                "short_code": s.short_code,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "expires_at": s.expires_at.isoformat() if s.expires_at else None,
                "type": "pairing",
            }
            for s, u in pairing_sessions
        ]

        # Device pairing sessions (unclaimed and not expired)
        device_sessions = (
            db.query(DevicePairingSession)
            .filter(
                DevicePairingSession.expires_at > now,
                DevicePairingSession.claimed == False,  # noqa: E712
            )
            .order_by(DevicePairingSession.created_at.desc())
            .all()
        )
        device = [
            {
                "token": s.device_token[:8] + "...",
                "token_full": s.device_token,
                "short_code": s.short_code,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "expires_at": s.expires_at.isoformat() if s.expires_at else None,
                "type": "device",
            }
            for s in device_sessions
        ]

    return {
        "web_sessions": web_sessions,
        "pairing_sessions": pairing,
        "device_sessions": device,
    }


@router.delete("/api/sessions/web/{token}")
async def revoke_web_session(token: str, request: Request, _=Depends(verify_admin)):
    """Revoke a user's web login session."""
    with get_db() as db:
        deleted = db.query(UserSession).filter(UserSession.token == token).delete()
        if not deleted:
            raise HTTPException(404, "Session not found")
    logger.info(f"Admin revoked web session {token[:8]}...")
    return {"status": "ok"}


@router.delete("/api/sessions/pairing/{token}")
async def revoke_pairing_session(token: str, request: Request, _=Depends(verify_admin)):
    """Revoke an active pairing session."""
    with get_db() as db:
        deleted = (
            db.query(AppPairingSession)
            .filter(AppPairingSession.token == token)
            .delete()
        )
        if not deleted:
            raise HTTPException(404, "Pairing session not found")
    logger.info(f"Admin revoked pairing session {token[:8]}...")
    return {"status": "ok"}


@router.get("/api/active-streams")
def active_streams(request: Request, _=Depends(verify_admin)):
    """Compatibility projection of fresh, client-reported viewing sessions."""
    from app.playback_activity import activity_snapshot, ACTIVE

    with get_db() as db:
        data = activity_snapshot(db)
    return {
        "streams": [
            {
                "user": row.get("viewer"),
                "video_id": row["media_id"],
                "type": row.get("media_type"),
                "tier": row.get("tier"),
                "title": row.get("title"),
                "state": row["state"],
                "started_at": row["last_seen"],
                "ago_seconds": row["ago_seconds"],
            }
            for row in data["playback"]
            if row["state"] in ACTIVE
        ]
    }


@router.get("/api/system-metrics")
def system_metrics(request: Request, _=Depends(verify_admin)):
    """Real-time system metrics: caches, TMDB API stats, sync status."""
    from app.main import (
        _catalog_cache,
        _user_cache,
        _CACHE_MAX_ENTRIES,
        _USER_CACHE_MAX,
    )
    from app.tmdb_client import tmdb_client
    from workers.personalized_sync import sync_status

    meta_cache_size = 0
    meta_cache_max = 512
    stream_cache_size = 0
    stream_cache_max = 500
    try:
        from app.main import _meta_cache, _META_CACHE_MAX

        meta_cache_size = len(_meta_cache)
        meta_cache_max = _META_CACHE_MAX
    except ImportError:
        pass
    from app.sources import resolver as source_resolver

    source_cache = source_resolver.cache_stats()
    stream_cache_size = source_cache["entries"]
    stream_cache_max = source_cache["max"]

    with get_db() as db:
        user_sync_rows = (
            db.query(
                User.id,
                User.display_name,
                User.email,
                User.user_key,
                User.last_sync,
                User.is_active,
            )
            .filter(User.is_active.is_(True))
            .all()
        )
        # One grouped COUNT instead of a per-user query (N+1) — this endpoint is
        # polled ~every 15s by the admin dashboard.
        catalog_counts: dict[int, int] = dict(
            db.query(UserCatalog.user_id, func.count(UserCatalog.id))
            .filter(UserCatalog.is_active.is_(True))
            .group_by(UserCatalog.user_id)
            .all()
        )
        user_sync = []
        now = utc_now()
        for u in user_sync_rows:
            name = u.display_name or u.email or u.user_key[:8]
            ago = int((now - u.last_sync).total_seconds()) if u.last_sync else None
            catalog_count = catalog_counts.get(u.id, 0)
            user_sync.append(
                {
                    "name": name,
                    "last_sync": u.last_sync.isoformat() if u.last_sync else None,
                    "ago_seconds": ago,
                    "catalogs": catalog_count,
                }
            )

    return {
        "caches": {
            "catalog": {"size": len(_catalog_cache), "max": _CACHE_MAX_ENTRIES},
            "user": {"size": len(_user_cache), "max": _USER_CACHE_MAX},
            "meta": {"size": meta_cache_size, "max": meta_cache_max},
            "stream": {"size": stream_cache_size, "max": stream_cache_max},
        },
        "tmdb": tmdb_client._stats,
        "sync": sync_status,
        "user_sync": user_sync,
    }


# ---- HTML Page ----


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
@router.get("/{admin_path:path}", response_class=HTMLResponse)
async def admin_page(request: Request, admin_path: str = ""):
    """Serve the login shell or the authenticated routed React application."""
    # The SPA fallback owns user-facing routes only. Unknown API/WebSocket
    # paths must stay honest 404s rather than returning HTML with a 200 status,
    # which otherwise hides endpoint typos from clients and monitoring.
    if admin_path == "api" or admin_path.startswith(("api/", "ws/")):
        raise HTTPException(status_code=404, detail="Not found")
    try:
        verify_admin(request)
    except HTTPException as exc:
        if exc.status_code not in (401, 403):
            raise
        return HTMLResponse(
            content=_admin_login_html(),
            status_code=exc.status_code if exc.status_code == 403 else 200,
            headers={"Cache-Control": "private, no-store"},
        )

    # Preserve old bookmarks without retaining or serving the retired inline
    # dashboard. The React center owns every operational surface now.
    if admin_path == "legacy":
        return RedirectResponse("/admin/overview", status_code=308)

    index_path = Path(__file__).resolve().parent.parent / "static/dashboard/admin.html"
    if index_path.exists():
        content = index_path.read_text(encoding="utf-8")
    else:
        content = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark">
<title>Curatio Admin</title><link rel="stylesheet" href="/static/workspace-tokens.css"><link rel="stylesheet" href="/static/auth.css"></head>
<body><main class="container build-missing" role="main"><h1>Admin build missing</h1><p class="subtitle">Run <code>npm run build</code> to build the admin center.</p></main>
</body></html>"""
    return HTMLResponse(
        content=content,
        headers={"Cache-Control": "private, no-store"},
    )


def _admin_login_html() -> str:
    """Small server-rendered passkey-first login shell for the React center.

    Behaviour lives in /static/js/admin-login.js so the page carries no inline
    script; the look comes from the shared ui_theme stylesheets.
    """
    from app.ui_theme import (
        curatio_auth_header,
        curatio_auth_style_tag,
        curatio_script_tags,
    )

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Curatio Admin</title>{curatio_auth_style_tag()}{curatio_script_tags("admin-login")}</head>
<body>{curatio_auth_header(section="Admin")}
<main class="admin-login"><img src="/static/logo.png" alt=""><h1>Curatio Admin</h1><p class="subtitle">Sign in securely to manage this installation.</p>
<form id="login-form" method="post" action="/admin/api/login"><label for="login-email">Admin email <span class="subtitle">(optional for passkey)</span></label><input type="email" id="login-email" autocomplete="email webauthn" autofocus placeholder="admin@example.com"><button type="button" id="passkey-login-btn">Sign in with passkey</button>
<details id="password-toggle"><summary>Use password / 2FA backup instead</summary><div class="fallback"><label for="login-password">Password</label><input type="password" id="login-password" autocomplete="current-password" placeholder="Password"><label for="login-totp" id="totp-label" class="hidden">2FA code</label><input type="text" id="login-totp" class="hidden" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="2FA Code"><button type="submit" id="password-login-btn" class="secondary">Sign in with password</button><a href="#" id="toggle-master-pw" class="mode-link">Use master password instead</a></div></details><p class="login-error" id="login-error" role="alert"></p></form></main>
</body></html>"""
