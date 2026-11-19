"""Signed, short-lived playback URLs for media on the owner's Plex/Jellyfin servers.

Clients never see a media-server address or token. A source URL looks like
``{base_url}/media/{token}`` where ``token`` is an encrypted, expiring
reference ``(provider id, server-relative path)``. This route decrypts it, asks
the provider for the real upstream URL plus auth headers, and relays the bytes
with Range support. HLS playlists are rewritten so every child playlist,
segment and key is itself a signed ``/media/`` URL.

The target host comes from the admin's own integration settings (never from
the client), which is why LAN addresses are allowed here, unlike the general
proxy.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import time
from typing import Optional
from urllib.parse import urljoin, urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from loguru import logger

from app.config import settings

router = APIRouter(tags=["media"])

DEFAULT_TTL_SECONDS = 6 * 3600
_CHUNK = 256 * 1024
_HLS_TYPES = ("mpegurl", "m3u8")
_PASSTHROUGH = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "last-modified",
    "etag",
)
_URI_ATTR = re.compile(r'URI="([^"]+)"')

_client: Optional[httpx.AsyncClient] = None


def _fernet() -> Fernet:
    digest = hmac.new(
        settings.secret_key.encode(), b"curatio:media-url:v1", hashlib.sha256
    ).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def mint_path(
    provider_id: str, path: str, *, ttl: int = DEFAULT_TTL_SECONDS, ext: str = ""
) -> str:
    """Server-relative ``/media/...`` path for ``path`` on ``provider_id``."""
    payload = json.dumps(
        {"p": provider_id, "u": path, "e": int(time.time()) + ttl},
        separators=(",", ":"),
    )
    token = _fernet().encrypt(payload.encode()).decode()
    return f"/media/{token}{ext}"


def mint_url(
    provider_id: str, path: str, *, ttl: int = DEFAULT_TTL_SECONDS, ext: str = ""
) -> str:
    """Absolute signed URL. ``ext`` (e.g. ``.m3u8``) helps players sniff type."""
    return f"{settings.base_url}{mint_path(provider_id, path, ttl=ttl, ext=ext)}"


def read_token(token: str) -> tuple[str, str]:
    """Return ``(provider_id, path)`` or raise ``HTTPException(404)``."""
    token = token.split(".", 1)[0]
    try:
        data = json.loads(_fernet().decrypt(token.encode()))
    except (InvalidToken, ValueError):
        raise HTTPException(status_code=404, detail="Not found")
    if not isinstance(data, dict) or int(data.get("e", 0)) < time.time():
        raise HTTPException(status_code=404, detail="Not found")
    provider_id, path = data.get("p"), data.get("u")
    if not isinstance(provider_id, str) or not isinstance(path, str):
        raise HTTPException(status_code=404, detail="Not found")
    return provider_id, path


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15.0, read=60.0, write=30.0, pool=30.0),
            # Never follow redirects: httpx drops Authorization on a cross-origin
            # hop but would carry X-Plex-Token / X-Emby-Token to the new host.
            follow_redirects=False,
            limits=httpx.Limits(max_connections=96, max_keepalive_connections=24),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _relative_to(base: str, absolute: str) -> Optional[str]:
    """``absolute`` as a path relative to the provider's ``base`` URL, or None."""
    b, a = urlsplit(base), urlsplit(absolute)
    if (a.scheme, a.netloc) != (b.scheme, b.netloc):
        return None
    prefix = b.path.rstrip("/")
    if prefix and not a.path.startswith(prefix + "/"):
        return None
    rel = a.path[len(prefix) :] or "/"
    return rel + (f"?{a.query}" if a.query else "")


def rewrite_playlist(
    text: str, *, provider_id: str, upstream_url: str, server_base: str, strip_params=()
) -> str:
    """Re-sign every URI in an HLS playlist so the client stays on ``/media/``."""

    def sign(uri: str) -> str:
        absolute = urljoin(upstream_url, uri.strip())
        rel = _relative_to(server_base, absolute)
        if rel is None:
            # Off-server URIs are dropped rather than handed to the client.
            return ""
        if strip_params:
            parts = urlsplit(rel)
            kept = [
                pair
                for pair in parts.query.split("&")
                if pair and pair.split("=", 1)[0] not in strip_params
            ]
            rel = parts.path + (("?" + "&".join(kept)) if kept else "")
        ext = ".m3u8" if ".m3u8" in rel.split("?", 1)[0] else ""
        return mint_path(provider_id, rel, ext=ext)

    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            out.append(_URI_ATTR.sub(lambda m: f'URI="{sign(m.group(1))}"', line))
        else:
            out.append(sign(stripped))
    return "\n".join(out) + "\n"


async def _open(provider_id: str, path: str, request: Request):
    from app.sources import registry

    provider = registry.get(provider_id)
    if provider is None or not provider.enabled():
        raise HTTPException(status_code=404, detail="Not found")
    media_request = getattr(provider, "media_request", None)
    if media_request is None:
        raise HTTPException(status_code=404, detail="Not found")
    url, headers, server_base = await media_request(path)
    forward = dict(headers)
    for name in ("range", "if-range", "if-none-match", "if-modified-since"):
        if name in request.headers:
            forward[name] = request.headers[name]
    upstream = await _http().send(
        _http().build_request(request.method, url, headers=forward), stream=True
    )
    return provider, upstream, url, server_base


@router.api_route("/media/{token}", methods=["GET", "HEAD"])
async def media(token: str, request: Request):
    provider_id, path = read_token(token)
    try:
        provider, upstream, url, server_base = await _open(provider_id, path, request)
    except httpx.HTTPError as exc:
        logger.warning("media upstream unreachable for {}: {}", provider_id, exc)
        raise HTTPException(status_code=502, detail="Media server unreachable")

    content_type = upstream.headers.get("content-type", "").lower()
    is_playlist = any(t in content_type for t in _HLS_TYPES) or path.split("?", 1)[
        0
    ].endswith(".m3u8")
    if upstream.status_code >= 300:  # redirects are not followed (token safety)
        await upstream.aclose()
        status = 404 if upstream.status_code in (401, 403, 404) else 502
        raise HTTPException(status_code=status, detail="Media unavailable")

    if is_playlist and request.method == "GET":
        try:
            body = (await upstream.aread()).decode("utf-8", "replace")
        finally:
            await upstream.aclose()
        rewritten = rewrite_playlist(
            body,
            provider_id=provider_id,
            upstream_url=str(upstream.url),
            server_base=server_base,
            strip_params=getattr(provider, "media_secret_params", ()),
        )
        return Response(
            rewritten,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store"},
        )

    headers = {k: v for k, v in upstream.headers.items() if k.lower() in _PASSTHROUGH}
    headers.setdefault("accept-ranges", "bytes")
    headers["cache-control"] = "private, max-age=60"
    if request.method == "HEAD":
        await upstream.aclose()
        return Response(status_code=upstream.status_code, headers=headers)

    async def body():
        try:
            async for chunk in upstream.aiter_bytes(_CHUNK):
                yield chunk
        except (httpx.HTTPError, asyncio.CancelledError):
            pass
        finally:
            await upstream.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code, headers=headers)
