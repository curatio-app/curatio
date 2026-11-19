"""Plex Media Server integration.

The admin signs in with Plex's PIN flow (plex.tv), picks one of their servers,
and Curatio mirrors that server's movie and show sections into the
availability index. Playback uses the server's original files or its
transcoder (HLS) through signed ``/media/`` URLs; the Plex token never leaves
Curatio.
"""

from __future__ import annotations

import ipaddress
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional
from urllib.parse import urlencode, urlparse

from loguru import logger
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import media_proxy
from app.models import MediaAvailability
from app.sources.library_base import (
    LibraryError,
    LibraryProvider,
    ServerTitle,
    describe,
    epoch_to_datetime,
    int_or_none,
    resolution_from_height,
    transcode_ladder,
)
from app.sources.models import MediaRef, PlaybackHints, PlaybackSource, SubtitleTrack
from app.sources.sdk import http_client

PLEX_TV = "https://plex.tv"
#: How long a transient token is reused before asking Plex for a new one.
#: Plex keeps them valid ~48 h; an hour bounds how long a leaked one is fresh.
TRANSIENT_TOKEN_TTL = 3600
RECENT_PAGE_SIZE = 100
PRODUCT = "Curatio"
PAGE_SIZE = 500

_HDR_MAP = {
    "dovi": "dv",
    "dolby vision": "dv",
    "hdr10+": "hdr10plus",
    "hdr10": "hdr10",
    "hlg": "hlg",
    "smpte2084": "hdr10",
}


def _first(value: Any) -> dict:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return value if isinstance(value, dict) else {}


_PLEX_LISTING_RES = {"4k": "2160p", "1080": "1080p", "720": "720p", "576": "576p"}


def _listing_resolution(entry: dict) -> Optional[str]:
    """Best copy's resolution from a section listing (movies only)."""
    best = None
    rank = {"2160p": 4, "1080p": 3, "720p": 2, "576p": 1, "480p": 0}
    for media in entry.get("Media") or []:
        raw = str(media.get("videoResolution") or "").lower()
        label = _PLEX_LISTING_RES.get(raw, "480p" if raw in ("sd", "480") else None)
        if label and (best is None or rank[label] > rank[best]):
            best = label
    return best


#: Home-network ranges. Explicit, because ``ip_address.is_private`` also
#: covers documentation and other reserved ranges that are not a LAN.
_HOME_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "127.0.0.0/8",
        "100.64.0.0/10",  # carrier-grade NAT / Tailscale
        "fc00::/7",
        "fe80::/10",
        "::1/128",
    )
)


def lan_base_url(server_url: Optional[str]) -> Optional[str]:
    """The configured Plex address when it is on a home network, else None.

    Accepts private / link-local / loopback IPs, ``.local`` names, and Plex's
    ``a-b-c-d.<hash>.plex.direct`` names that encode a private IP. A public
    address never gets a direct link: there the signed proxy stays the route.
    """
    if not server_url:
        return None
    parsed = urlparse(server_url)
    host = (parsed.hostname or "").lower()
    if not host or parsed.scheme not in ("http", "https"):
        return None
    candidate = host
    if host.endswith(".plex.direct"):
        candidate = host.split(".", 1)[0].replace("-", ".")
    elif host.endswith(".local"):
        return server_url.rstrip("/")
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if any(ip in network for network in _HOME_NETWORKS):
        return server_url.rstrip("/")
    return None


def _server_title(entry: dict, media_type: str) -> ServerTitle:
    imdb, tmdb, tvdb = parse_guids(entry)
    return ServerTitle(
        item_key=str(entry.get("ratingKey")),
        media_type=media_type,
        imdb_id=imdb,
        tmdb_id=tmdb,
        tvdb_id=tvdb,
        title=entry.get("title"),
        year=int_or_none(entry.get("year")),
        added_at=epoch_to_datetime(entry.get("addedAt")),
        resolution=_listing_resolution(entry),
        thumb=entry.get("thumb") or None,
        # Watch state of the signed-in account: plays for a movie, watched
        # episodes for a show.
        play_count=int_or_none(
            entry.get("viewCount" if media_type == "movie" else "viewedLeafCount")
        )
        or 0,
        watched_at=epoch_to_datetime(entry.get("lastViewedAt")),
    )


def parse_guids(entry: dict) -> tuple[Optional[str], Optional[int], Optional[int]]:
    imdb = tmdb = tvdb = None
    for guid in entry.get("Guid") or []:
        gid = str(guid.get("id") or "")
        if gid.startswith("imdb://"):
            imdb = gid[7:] or None
        elif gid.startswith("tmdb://"):
            tmdb = int_or_none(gid[7:])
        elif gid.startswith("tvdb://"):
            tvdb = int_or_none(gid[7:])
    # Older agents expose a single legacy guid string.
    legacy = str(entry.get("guid") or "")
    if not imdb and "imdb://" in legacy:
        imdb = legacy.split("imdb://", 1)[1].split("?", 1)[0] or None
    if imdb and not imdb.startswith("tt"):
        imdb = None
    return imdb, tmdb, tvdb


def _hdr(video_stream: dict) -> Optional[str]:
    if video_stream.get("DOVIPresent"):
        return "dv"
    for key in ("colorTrc", "displayTitle", "extendedDisplayTitle"):
        text = str(video_stream.get(key) or "").lower()
        for needle, value in _HDR_MAP.items():
            if needle in text:
                return value
    return None


def _codec(value: Any) -> Optional[str]:
    text = str(value or "").lower() or None
    return {"h265": "hevc", "avc": "h264", "dca": "dts", "dca-ma": "dtshd"}.get(
        text or "", text
    )


class PlexConnectBody(BaseModel):
    server_id: str
    uri: str


class PlexManualBody(BaseModel):
    server_url: str
    token: str


class PlexProvider(LibraryProvider):
    id = "plex"
    label = "Plex"
    media_secret_params = ("X-Plex-Token",)

    # --- HTTP helpers ------------------------------------------------------------
    def client_id(self) -> str:
        cfg = self.config() or {}
        return cfg.get("client_id") or "curatio-" + uuid.uuid4().hex

    def _headers(
        self, token: Optional[str] = None, client_id: Optional[str] = None
    ) -> dict:
        headers = {
            "Accept": "application/json",
            "X-Plex-Product": PRODUCT,
            "X-Plex-Version": "2",
            "X-Plex-Client-Identifier": client_id or self.client_id(),
            "X-Plex-Platform": "Chrome",
            "X-Plex-Device-Name": "Curatio Server",
        }
        if token:
            headers["X-Plex-Token"] = token
        return headers

    def auth_headers(self, cfg: dict) -> dict[str, str]:
        return self._headers(cfg.get("token"), cfg.get("client_id"))

    # --- home-network direct links ------------------------------------------------
    _transient: tuple[float, Optional[str]] = (0.0, None)

    async def _transient_token(self, cfg: dict) -> Optional[str]:
        """A short-lived Plex token for handing to clients, never the stored one."""
        stamp, token = self._transient
        if token and time.monotonic() - stamp < TRANSIENT_TOKEN_TTL:
            return token
        try:
            data = await self._get(
                cfg, "/security/token", params={"type": "delegation", "scope": "all"}
            )
        except LibraryError as exc:
            logger.debug("Plex transient token unavailable: {}", exc)
            return None
        token = data.get("token") if isinstance(data, dict) else None
        if not token or token == cfg.get("token"):
            return None
        self._transient = (time.monotonic(), token)
        return token

    async def _lan_link(self, cfg: dict, part_key: str) -> Optional[str]:
        """Direct file URL on the home network, or None (then use the proxy)."""
        if cfg.get("lan_direct_play") is False:  # admin switched it off
            return None
        base = lan_base_url(cfg.get("server_url"))
        if not base or not part_key.startswith("/"):
            return None
        token = await self._transient_token(cfg)
        if not token:
            return None
        return f"{base}{part_key}?{urlencode({'X-Plex-Token': token})}"

    async def _get(
        self,
        cfg: dict,
        path: str,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> dict:
        url = cfg["server_url"].rstrip("/") + path
        try:
            resp = await http_client().get(
                url,
                params=params,
                headers={**self.auth_headers(cfg), **(headers or {})},
                timeout=20,
            )
        except Exception as exc:
            raise LibraryError(
                f"Can't reach the Plex server ({type(exc).__name__})", 502
            )
        if resp.status_code in (401, 403):
            raise LibraryError("Plex rejected the sign-in. Reconnect Plex.", 401)
        if resp.status_code != 200:
            raise LibraryError(f"Plex returned HTTP {resp.status_code}", 502)
        try:
            return resp.json().get("MediaContainer") or {}
        except ValueError:
            raise LibraryError("Plex sent an unexpected response", 502)

    # --- sign-in (plex.tv PIN flow) ---------------------------------------------
    async def start_pin(self) -> dict:
        client_id = self.client_id()
        resp = await http_client().post(
            f"{PLEX_TV}/api/v2/pins",
            params={"strong": "true"},
            headers=self._headers(client_id=client_id),
        )
        if resp.status_code not in (200, 201):
            raise LibraryError("plex.tv did not start a sign-in", 502)
        pin = resp.json()
        self.update_config(client_id=client_id, pending_pin_id=pin["id"])
        auth_url = "https://app.plex.tv/auth#?" + urlencode(
            {
                "clientID": client_id,
                "code": pin["code"],
                "context[device][product]": PRODUCT,
            }
        )
        return {"pin_id": pin["id"], "auth_url": auth_url}

    async def poll_pin(self) -> dict:
        cfg = self.config() or {}
        pin_id = cfg.get("pending_pin_id")
        if not pin_id:
            raise LibraryError("No Plex sign-in in progress")
        resp = await http_client().get(
            f"{PLEX_TV}/api/v2/pins/{int(pin_id)}",
            headers=self._headers(client_id=cfg.get("client_id")),
        )
        if resp.status_code != 200:
            raise LibraryError("plex.tv sign-in expired. Start again.", 410)
        token = resp.json().get("authToken")
        if not token:
            return {"status": "pending"}
        self.update_config(account_token=token, pending_pin_id=None)
        return {"status": "signed_in", "servers": await self.list_servers()}

    async def list_servers(self) -> list[dict]:
        cfg = self.config() or {}
        token = cfg.get("account_token")
        if not token:
            raise LibraryError("Sign in to Plex first")
        resp = await http_client().get(
            f"{PLEX_TV}/api/v2/resources",
            params={"includeHttps": 1, "includeRelay": 0},
            headers=self._headers(token, cfg.get("client_id")),
        )
        if resp.status_code != 200:
            raise LibraryError("Couldn't list your Plex servers", 502)
        servers = []
        for res in resp.json():
            if "server" not in str(res.get("provides") or ""):
                continue
            connections = sorted(
                res.get("connections") or [],
                key=lambda c: (not c.get("local"), bool(c.get("relay"))),
            )
            servers.append(
                {
                    "id": res.get("clientIdentifier"),
                    "name": res.get("name"),
                    "owned": bool(res.get("owned")),
                    "connections": [
                        {"uri": c.get("uri"), "local": bool(c.get("local"))}
                        for c in connections
                        if c.get("uri") and not c.get("relay")
                    ],
                    "_token": res.get("accessToken") or token,
                }
            )
        self.update_config(_servers={s["id"]: s["_token"] for s in servers})
        return [{k: v for k, v in s.items() if k != "_token"} for s in servers]

    async def connect(self, server_id: str, uri: str) -> dict:
        cfg = self.config() or {}
        token = (cfg.get("_servers") or {}).get(server_id)
        if not token:
            raise LibraryError("Pick a server from the list")
        trial = {**cfg, "server_url": uri.rstrip("/"), "token": token}
        identity = await self._get(trial, "/identity")
        if identity.get("machineIdentifier") not in (None, server_id):
            raise LibraryError("That address belongs to a different Plex server")
        servers = await self.list_servers()
        name = next((s["name"] for s in servers if s["id"] == server_id), "Plex")
        self.update_config(
            enabled=True,
            server_url=uri.rstrip("/"),
            token=token,
            server_id=server_id,
            server_name=name,
        )
        return self.public_config()

    async def connect_manual(self, server_url: str, token: str) -> dict:
        trial = {
            "server_url": server_url.rstrip("/"),
            "token": token,
            "client_id": self.client_id(),
        }
        identity = await self._get(trial, "/identity")
        server_id = identity.get("machineIdentifier") or server_url
        self.update_config(
            enabled=True,
            client_id=trial["client_id"],
            server_url=trial["server_url"],
            token=token,
            server_id=server_id,
            server_name=identity.get("friendlyName") or "Plex",
        )
        return self.public_config()

    # --- sync -----------------------------------------------------------------------
    async def list_items(self, cfg: dict) -> Iterable[ServerTitle]:
        sections = await self._get(cfg, "/library/sections")
        items: list[ServerTitle] = []
        for section in sections.get("Directory") or []:
            kind = section.get("type")
            if kind not in ("movie", "show"):
                continue
            media_type = "movie" if kind == "movie" else "series"
            start = 0
            while True:
                page = await self._get(
                    cfg,
                    f"/library/sections/{section['key']}/all",
                    params={"includeGuids": 1},
                    headers={
                        "X-Plex-Container-Start": str(start),
                        "X-Plex-Container-Size": str(PAGE_SIZE),
                    },
                )
                entries = page.get("Metadata") or []
                items.extend(_server_title(entry, media_type) for entry in entries)
                total = int_or_none(page.get("totalSize")) or 0
                start += len(entries)
                if not entries or start >= total:
                    break
        return items

    async def list_recent(self, cfg: dict, since: datetime) -> list[ServerTitle]:
        """Newest additions per section, paging only until older than ``since``."""
        sections = await self._get(cfg, "/library/sections")
        items: list[ServerTitle] = []
        cutoff = since.replace(tzinfo=timezone.utc).timestamp()
        for section in sections.get("Directory") or []:
            kind = section.get("type")
            if kind not in ("movie", "show"):
                continue
            media_type = "movie" if kind == "movie" else "series"
            start = 0
            while True:
                page = await self._get(
                    cfg,
                    f"/library/sections/{section['key']}/all",
                    params={"includeGuids": 1, "sort": "addedAt:desc"},
                    headers={
                        "X-Plex-Container-Start": str(start),
                        "X-Plex-Container-Size": str(RECENT_PAGE_SIZE),
                    },
                )
                entries = page.get("Metadata") or []
                fresh = [
                    e for e in entries if (int_or_none(e.get("addedAt")) or 0) >= cutoff
                ]
                items.extend(_server_title(entry, media_type) for entry in fresh)
                start += len(entries)
                total = int_or_none(page.get("totalSize")) or 0
                if len(fresh) < len(entries) or not entries or start >= total:
                    break
        return items

    async def list_recently_watched(
        self, cfg: dict, since: datetime
    ) -> list[ServerTitle]:
        """Titles played since ``since``, most recent first, per section."""
        sections = await self._get(cfg, "/library/sections")
        items: list[ServerTitle] = []
        cutoff = since.replace(tzinfo=timezone.utc).timestamp()
        for section in sections.get("Directory") or []:
            kind = section.get("type")
            if kind not in ("movie", "show"):
                continue
            media_type = "movie" if kind == "movie" else "series"
            page = await self._get(
                cfg,
                f"/library/sections/{section['key']}/all",
                params={"includeGuids": 1, "sort": "lastViewedAt:desc"},
                headers={
                    "X-Plex-Container-Start": "0",
                    "X-Plex-Container-Size": str(RECENT_PAGE_SIZE),
                },
            )
            for entry in page.get("Metadata") or []:
                if (int_or_none(entry.get("lastViewedAt")) or 0) < cutoff:
                    break
                items.append(_server_title(entry, media_type))
        return items

    # --- playback -------------------------------------------------------------------
    async def _playable_item(
        self, cfg: dict, row: MediaAvailability, media: MediaRef
    ) -> Optional[dict]:
        if not media.is_episode:
            container = await self._get(cfg, f"/library/metadata/{row.item_key}")
            return _first(container.get("Metadata"))
        leaves = await self._get(cfg, f"/library/metadata/{row.item_key}/allLeaves")
        for entry in leaves.get("Metadata") or []:
            if (
                int_or_none(entry.get("parentIndex")) == media.season
                and int_or_none(entry.get("index")) == media.episode
            ):
                detail = await self._get(cfg, f"/library/metadata/{entry['ratingKey']}")
                return _first(detail.get("Metadata"))
        return None

    async def sources_for(
        self,
        cfg: dict,
        row: MediaAvailability,
        media: MediaRef,
        quality: str,
        hints: Optional[PlaybackHints] = None,
    ) -> list[PlaybackSource]:
        item = await self._playable_item(cfg, row, media)
        if not item:
            return []
        rating_key = str(item.get("ratingKey"))
        sources: list[PlaybackSource] = []
        for media_index, media_entry in enumerate(item.get("Media") or []):
            part = _first(media_entry.get("Part"))
            if not part.get("key"):
                continue
            streams = part.get("Stream") or []
            video: dict = next((s for s in streams if s.get("streamType") == 1), {})
            audio = next(
                (s for s in streams if s.get("streamType") == 2 and s.get("selected")),
                None,
            ) or next((s for s in streams if s.get("streamType") == 2), {})
            width, height = int_or_none(media_entry.get("width")), int_or_none(
                media_entry.get("height")
            )
            resolution = resolution_from_height(height, width)
            video_codec = _codec(media_entry.get("videoCodec") or video.get("codec"))
            audio_codec = _codec(media_entry.get("audioCodec") or audio.get("codec"))
            channels = int_or_none(
                media_entry.get("audioChannels") or audio.get("channels")
            )
            hdr = _hdr(video)
            languages = sorted(
                {
                    s.get("languageCode")[:2]
                    for s in streams
                    if s.get("streamType") == 2 and s.get("languageCode")
                }
            )
            subtitles = [
                SubtitleTrack(
                    id=str(s.get("id")),
                    url=media_proxy.mint_url(self.id, str(s["key"])),
                    lang=(s.get("languageCode") or "")[:2] or None,
                    label=s.get("displayTitle") or s.get("language"),
                    format=s.get("codec"),
                    forced=bool(s.get("forced")),
                )
                for s in streams
                if s.get("streamType") == 3 and s.get("key")
            ]
            file_name = (
                str(part.get("file") or "").replace("\\", "/").rsplit("/", 1)[-1]
                or None
            )
            file_id = self.file_id(rating_key, part.get("id"))
            duration = (
                int_or_none(part.get("duration") or media_entry.get("duration")) or 0
            ) / 1000 or None
            sources.append(
                PlaybackSource(
                    id=self.source_id(rating_key, part.get("id"), "original"),
                    provider_id=self.id,
                    provider_label=self.label,
                    kind="library",
                    variant="original",
                    delivery="file",
                    url=media_proxy.mint_url(self.id, str(part["key"])),
                    label=describe(
                        resolution=resolution,
                        video_codec=video_codec,
                        hdr=hdr,
                        audio_channels=channels,
                    ),
                    detail=file_name,
                    badges=["Original"],
                    container=(
                        part.get("container") or media_entry.get("container") or None
                    ),
                    video_codec=video_codec,
                    audio_codec=audio_codec,
                    hdr=hdr,
                    resolution=resolution,
                    width=width,
                    height=height,
                    audio_channels=channels,
                    languages=languages,
                    size_bytes=int_or_none(part.get("size")),
                    bitrate=(int_or_none(media_entry.get("bitrate")) or 0) * 1000
                    or None,
                    duration_seconds=duration,
                    subtitles=subtitles,
                    route_id=self.route_id(rating_key, part.get("id"), "original"),
                    file_id=file_id,
                    lan_url=await self._lan_link(cfg, str(part["key"])),
                )
            )
            for rung_index, rung in enumerate(transcode_ladder(hints, height)):
                # The first rung keeps the historic id so client-side caches
                # and failure reports stay comparable across releases.
                variant_key = (
                    "optimized" if rung_index == 0 else f"optimized-{rung.bitrate_kbps}"
                )
                params = {
                    "path": f"/library/metadata/{rating_key}",
                    "mediaIndex": media_index,
                    "partIndex": 0,
                    "protocol": "hls",
                    "fastSeek": 1,
                    "directPlay": 0,
                    # Plex copies the video when it already fits the budget
                    # and converts only what doesn't (audio, container).
                    "directStream": 1,
                    "directStreamAudio": 0,
                    "videoResolution": f"{rung.width}x{rung.height}",
                    "maxVideoBitrate": rung.bitrate_kbps,
                    "audioChannelCount": rung.audio_channels,
                    "subtitles": "none",
                    "session": uuid.uuid4().hex,
                    "X-Plex-Session-Identifier": uuid.uuid4().hex,
                    "X-Plex-Client-Identifier": cfg.get("client_id"),
                    "X-Plex-Product": PRODUCT,
                    "X-Plex-Platform": "Chrome",
                }
                sources.append(
                    PlaybackSource(
                        id=self.source_id(rating_key, part.get("id"), variant_key),
                        provider_id=self.id,
                        provider_label=self.label,
                        kind="library",
                        variant="optimized",
                        delivery="hls",
                        url=media_proxy.mint_url(
                            self.id,
                            "/video/:/transcode/universal/start.m3u8?"
                            + urlencode(params),
                            ext=".m3u8",
                        ),
                        label=describe(
                            resolution=rung.resolution,
                            video_codec="h264",
                            hdr=None,
                            audio_channels=rung.audio_channels,
                            optimized=True,
                        ),
                        detail=file_name,
                        badges=["Optimized"],
                        container="mpegts",
                        video_codec="h264",
                        audio_codec="aac",
                        resolution=rung.resolution,
                        width=rung.width,
                        height=rung.height,
                        audio_channels=rung.audio_channels,
                        bitrate=rung.bitrate_kbps * 1000,
                        languages=languages,
                        duration_seconds=duration,
                        subtitles=subtitles,
                        route_id=self.route_id(rating_key, part.get("id"), variant_key),
                        file_id=file_id,
                        startup_timeout_seconds=30,
                    )
                )
        return sources

    def public_config(self) -> dict:
        cfg = self.config() or {}
        # Lets the admin page skip straight to choosing a server after a sign-in.
        return {**super().public_config(), "signed_in": bool(cfg.get("account_token"))}

    # --- admin ----------------------------------------------------------------------
    def admin_router(self) -> APIRouter:
        router = self.base_admin_router()

        def wrap(exc: LibraryError):
            return HTTPException(status_code=exc.status_code, detail=str(exc))

        @router.post("/pin")
        async def pin():
            try:
                return await self.start_pin()
            except LibraryError as exc:
                raise wrap(exc)

        @router.get("/pin")
        async def pin_status():
            try:
                return await self.poll_pin()
            except LibraryError as exc:
                raise wrap(exc)

        @router.get("/servers")
        async def servers():
            try:
                return {"servers": await self.list_servers()}
            except LibraryError as exc:
                raise wrap(exc)

        @router.post("/connect")
        async def connect(body: PlexConnectBody):
            try:
                result = await self.connect(body.server_id, body.uri)
            except LibraryError as exc:
                raise wrap(exc)
            await self.sync()
            return self.public_config() | {"connected": result["connected"]}

        @router.post("/connect-manual")
        async def connect_manual(body: PlexManualBody):
            try:
                await self.connect_manual(body.server_url, body.token)
            except LibraryError as exc:
                raise wrap(exc)
            await self.sync()
            return self.public_config()

        return router


provider = PlexProvider()
