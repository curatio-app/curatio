"""Stable surface for playback source providers (built-in and installed addons).

Addons import from this module only. Anything not re-exported here is internal
and may change between Curatio releases. Bump ``SDK_VERSION`` when a change here
would break an existing addon; the registry refuses addons built for another
version.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import weakref
from typing import Any, Callable, Optional, TypeVar

import httpx
from fastapi import Request

from app.config import settings
from app.crypto import decrypt_token, encrypt_token
from app.database import get_db
from app import playback_activity, tvmaze_client
from app.database import init_database
from app.models import (
    AdminSetting,
    Base,
    DetailSnapshot,
    MediaMetadata,
    ProviderFailureEvent,
)
from app.time_utils import utc_now
from app.net_safety import is_browser_fetchable_url, is_safe_source_url
from app.sources.base import SourceProvider
from app.sources.models import (
    Availability,
    AvailabilitySource,
    FailureReport,
    MediaRef,
    PlaybackContext,
    PlaybackSource,
    SubtitleTrack,
    TitleRef,
)

SDK_VERSION = 1

T = TypeVar("T")

__all__ = [
    "SDK_VERSION",
    "AdminSetting",
    "Base",
    "DetailSnapshot",
    "MediaMetadata",
    "ProviderFailureEvent",
    "init_database",
    "playback_activity",
    "tvmaze_client",
    "decrypt_token",
    "encrypt_token",
    "utc_now",
    "Availability",
    "AvailabilitySource",
    "FailureReport",
    "MediaRef",
    "PlaybackContext",
    "PlaybackSource",
    "SourceProvider",
    "SubtitleTrack",
    "TitleRef",
    "db_call",
    "get_db",
    "http_client",
    "identity",
    "is_browser_fetchable_url",
    "is_safe_source_url",
    "read_secret_json",
    "read_setting",
    "require_admin",
    "server_secret",
    "settings",
    "write_secret_json",
    "write_setting",
]


# --- database --------------------------------------------------------------
async def db_call(fn: Callable[..., T], *args: Any) -> T:
    """Run ``fn(db, *args)`` on a short worker-thread session."""

    def run() -> T:
        with get_db() as db:
            return fn(db, *args)

    return await asyncio.to_thread(run)


def read_setting(db, key: str) -> Optional[str]:
    row = db.query(AdminSetting).filter(AdminSetting.key == key).first()
    return row.value if row and row.value else None


def write_setting(db, key: str, value: Optional[str]) -> None:
    row = db.query(AdminSetting).filter(AdminSetting.key == key).first()
    if value is None:
        if row is not None:
            db.delete(row)
    elif row is None:
        db.add(AdminSetting(key=key, value=value))
    else:
        row.value = value
    db.commit()


def read_secret_json(db, key: str) -> Optional[dict]:
    """Read an encrypted JSON object stored with ``write_secret_json``."""
    raw = read_setting(db, key)
    if not raw:
        return None
    try:
        value = json.loads(decrypt_token(raw))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def write_secret_json(db, key: str, value: Optional[dict]) -> None:
    write_setting(
        db,
        key,
        (
            None
            if value is None
            else encrypt_token(json.dumps(value, separators=(",", ":")))
        ),
    )


# --- identities --------------------------------------------------------------
def server_secret() -> str:
    return settings.secret_key


def identity(*parts: object) -> str:
    """Deterministic opaque id (HMAC of ``parts``) safe to hand to clients."""
    message = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hmac.new(
        server_secret().encode(), message.encode(), hashlib.sha256
    ).hexdigest()[:40]


# --- HTTP ------------------------------------------------------------------
_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = (
    weakref.WeakKeyDictionary()
)


def http_client() -> httpx.AsyncClient:
    """Pooled client for the running event loop (one per loop, never shared)."""
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=10.0),
            headers={"User-Agent": "Curatio/2"},
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=12),
        )
        _clients[loop] = client
    return client


async def close_http_clients() -> None:
    clients = list(_clients.values())
    _clients.clear()
    if clients:
        await asyncio.gather(*(c.aclose() for c in clients), return_exceptions=True)


# --- admin auth --------------------------------------------------------------
def require_admin(request: Request) -> bool:
    """FastAPI dependency: the caller is a signed-in Curatio admin."""
    from app.admin import verify_admin

    return verify_admin(request)
