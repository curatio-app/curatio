"""Jellyfin integration.

The admin connects with a server URL and either a username/password (exchanged
once for an access token) or an API key. Curatio mirrors the server's movies
and series into the availability index and plays the original files or
Jellyfin's HLS transcode through signed ``/media/`` URLs.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Iterable, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import media_proxy
from app.models import MediaAvailability
from app.sources.library_base import (
    LibraryError,
    LibraryProvider,
    ServerTitle,
    describe,
    int_or_none,
    iso_to_datetime,
    resolution_from_height,
    transcode_ladder,
)
from app.sources.models import MediaRef, PlaybackHints, PlaybackSource, SubtitleTrack
from app.sources.sdk import http_client

CLIENT = "Curatio"
PAGE_SIZE = 500
RECENT_PAGE_SIZE = 100
_LISTING_PARAMS = {
    "Recursive": "true",
    "IncludeItemTypes": "Movie,Series",
    "Fields": "ProviderIds,ProductionYear,DateCreated",
    "EnableImageTypes": "Primary",
    "ImageTypeLimit": 1,
    "EnableUserData": "false",
}


def _watch_state(entry: dict) -> tuple[Optional[int], Optional[datetime]]:
    """(play_count, watched_at) from Jellyfin ``UserData``.

    Movies report plays. Series report no episode count, so a fully played
    series is 3 (counts as watched) and a started one is 1.
    """
    data = entry.get("UserData")
    if not isinstance(data, dict):
        return None, None
    watched_at = iso_to_datetime(data.get("LastPlayedDate"))
    if entry.get("Type") == "Movie":
        plays = int_or_none(data.get("PlayCount")) or 0
        return (max(plays, 1) if data.get("Played") else plays), watched_at
    if data.get("Played"):
        return 3, watched_at
    if (data.get("PlayedPercentage") or 0) > 0:
        return 1, watched_at
    return 0, watched_at


def _server_title(entry: dict) -> ServerTitle:
    ids = {k.lower(): v for k, v in (entry.get("ProviderIds") or {}).items()}
    imdb = ids.get("imdb")
    if imdb and not str(imdb).startswith("tt"):
        imdb = None
    is_movie = entry.get("Type") == "Movie"
    play_count, watched_at = _watch_state(entry)
    return ServerTitle(
        item_key=str(entry.get("Id")),
        media_type="movie" if is_movie else "series",
        imdb_id=imdb,
        tmdb_id=int_or_none(ids.get("tmdb")),
        tvdb_id=int_or_none(ids.get("tvdb")),
        title=entry.get("Name"),
        year=int_or_none(entry.get("ProductionYear")),
        added_at=iso_to_datetime(entry.get("DateCreated")),
        resolution=(
            resolution_from_height(
                int_or_none(entry.get("Height")), int_or_none(entry.get("Width"))
            )
            if is_movie
            else None
        ),
        thumb=(
            f"/Items/{entry.get('Id')}/Images/Primary"
            if (entry.get("ImageTags") or {}).get("Primary")
            else None
        ),
        play_count=play_count,
        watched_at=watched_at,
    )


_HDR_MAP = {
    "DOVI": "dv",
    "DOVIWithHDR10": "dv",
    "DOVIWithHLG": "dv",
    "DOVIWithSDR": "dv",
    "HDR10": "hdr10",
    "HDR10Plus": "hdr10plus",
    "HLG": "hlg",
}


def _codec(value: Any) -> Optional[str]:
    text = str(value or "").lower() or None
    return {"h265": "hevc", "avc": "h264", "dca": "dts"}.get(text or "", text)


class JellyfinConnectBody(BaseModel):
    server_url: str
    username: Optional[str] = None
    password: Optional[str] = None
    api_key: Optional[str] = None


class JellyfinProvider(LibraryProvider):
    id = "jellyfin"
    label = "Jellyfin"
    media_secret_params = ("api_key", "ApiKey", "X-Emby-Token")

    def _device_id(self) -> str:
        cfg = self.config() or {}
        return cfg.get("device_id") or "curatio-" + uuid.uuid4().hex

    def _auth_value(self, token: Optional[str], device_id: str) -> str:
        value = f'MediaBrowser Client="{CLIENT}", Device="Curatio Server", DeviceId="{device_id}", Version="2"'
        if token:
            value += f', Token="{token}"'
        return value

    def auth_headers(self, cfg: dict) -> dict[str, str]:
        device = cfg.get("device_id") or "curatio"
        return {
            "Authorization": self._auth_value(cfg.get("token"), device),
            "X-Emby-Token": cfg.get("token") or "",
            "Accept": "application/json",
        }

    async def _get(self, cfg: dict, path: str, params: Optional[dict] = None) -> Any:
        url = cfg["server_url"].rstrip("/") + path
        try:
            resp = await http_client().get(
                url, params=params, headers=self.auth_headers(cfg), timeout=30
            )
        except Exception as exc:
            raise LibraryError(
                f"Can't reach the Jellyfin server ({type(exc).__name__})", 502
            )
        if resp.status_code in (401, 403):
            raise LibraryError(
                "Jellyfin rejected the sign-in. Reconnect Jellyfin.", 401
            )
        if resp.status_code != 200:
            raise LibraryError(f"Jellyfin returned HTTP {resp.status_code}", 502)
        try:
            return resp.json()
        except ValueError:
            raise LibraryError("Jellyfin sent an unexpected response", 502)

    # --- connect ----------------------------------------------------------------------
    async def connect(
        self,
        server_url: str,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> dict:
        server_url = server_url.strip().rstrip("/")
        if not server_url.startswith(("http://", "https://")):
            raise LibraryError("Server URL must start with http:// or https://")
        device_id = self._device_id()
        user_id = None
        if api_key:
            token = api_key.strip()
        elif username:
            try:
                resp = await http_client().post(
                    f"{server_url}/Users/AuthenticateByName",
                    json={"Username": username, "Pw": password or ""},
                    headers={"Authorization": self._auth_value(None, device_id)},
                    timeout=20,
                )
            except Exception as exc:
                raise LibraryError(
                    f"Can't reach the Jellyfin server ({type(exc).__name__})", 502
                )
            if resp.status_code in (401, 403):
                raise LibraryError("Wrong Jellyfin username or password", 401)
            if resp.status_code != 200:
                raise LibraryError(f"Jellyfin returned HTTP {resp.status_code}", 502)
            data = resp.json()
            token = data.get("AccessToken")
            user_id = (data.get("User") or {}).get("Id")
            if not token:
                raise LibraryError("Jellyfin did not return an access token", 502)
        else:
            raise LibraryError("Enter a username and password, or an API key")
        trial = {"server_url": server_url, "token": token, "device_id": device_id}
        info = await self._get(trial, "/System/Info")
        self.update_config(
            enabled=True,
            server_url=server_url,
            token=token,
            user_id=user_id,
            device_id=device_id,
            server_id=info.get("Id") or server_url,
            server_name=info.get("ServerName") or "Jellyfin",
        )
        return self.public_config()

    def _items_path(self, cfg: dict) -> str:
        return f"/Users/{cfg['user_id']}/Items" if cfg.get("user_id") else "/Items"

    def _listing_params(self, cfg: dict) -> dict:
        # Watch state belongs to a user, so it is only asked for on the
        # user-scoped listing.
        if cfg.get("user_id"):
            return {**_LISTING_PARAMS, "EnableUserData": "true"}
        return dict(_LISTING_PARAMS)

    # --- sync -----------------------------------------------------------------------------
    async def list_recent(self, cfg: dict, since: datetime) -> list[ServerTitle]:
        """Newest additions first, paging only until older than ``since``."""
        items: list[ServerTitle] = []
        start = 0
        while True:
            page = await self._get(
                cfg,
                self._items_path(cfg),
                params={
                    **self._listing_params(cfg),
                    "SortBy": "DateCreated",
                    "SortOrder": "Descending",
                    "StartIndex": start,
                    "Limit": RECENT_PAGE_SIZE,
                },
            )
            entries = page.get("Items") or []
            fresh = [
                e
                for e in entries
                if (iso_to_datetime(e.get("DateCreated")) or datetime.min) >= since
            ]
            items.extend(_server_title(entry) for entry in fresh)
            start += len(entries)
            total = int_or_none(page.get("TotalRecordCount")) or 0
            if len(fresh) < len(entries) or not entries or start >= total:
                break
        return items

    async def list_items(self, cfg: dict) -> Iterable[ServerTitle]:
        items: list[ServerTitle] = []
        start = 0
        while True:
            page = await self._get(
                cfg,
                self._items_path(cfg),
                params={
                    **self._listing_params(cfg),
                    "StartIndex": start,
                    "Limit": PAGE_SIZE,
                },
            )
            entries = page.get("Items") or []
            items.extend(_server_title(entry) for entry in entries)
            start += len(entries)
            total = int_or_none(page.get("TotalRecordCount")) or 0
            if not entries or start >= total:
                break
        return items

    async def list_recently_watched(
        self, cfg: dict, since: datetime
    ) -> Optional[list[ServerTitle]]:
        """Titles the signed-in user played since ``since``, most recent first."""
        if not cfg.get("user_id"):
            return None
        page = await self._get(
            cfg,
            self._items_path(cfg),
            params={
                **self._listing_params(cfg),
                "SortBy": "DatePlayed",
                "SortOrder": "Descending",
                "Limit": RECENT_PAGE_SIZE,
            },
        )
        items: list[ServerTitle] = []
        for entry in page.get("Items") or []:
            title = _server_title(entry)
            if not title.watched_at or title.watched_at < since:
                break
            items.append(title)
        return items

    # --- playback -------------------------------------------------------------------------
    async def _playable_item(
        self, cfg: dict, row: MediaAvailability, media: MediaRef
    ) -> Optional[dict]:
        if media.is_episode:
            params = {"Season": media.season, "Fields": "MediaSources,MediaStreams"}
            if cfg.get("user_id"):
                params["UserId"] = cfg["user_id"]
            page = await self._get(
                cfg, f"/Shows/{row.item_key}/Episodes", params=params
            )
            for entry in page.get("Items") or []:
                if int_or_none(entry.get("IndexNumber")) == media.episode:
                    return entry
            return None
        page = await self._get(
            cfg,
            self._items_path(cfg),
            params={"Ids": row.item_key, "Fields": "MediaSources,MediaStreams"},
        )
        items = page.get("Items") or []
        return items[0] if items else None

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
        item_id = str(item.get("Id"))
        sources: list[PlaybackSource] = []
        for ms in item.get("MediaSources") or []:
            msid = str(ms.get("Id") or item_id)
            streams = ms.get("MediaStreams") or []
            video: dict = next((s for s in streams if s.get("Type") == "Video"), {})
            audio_index = ms.get("DefaultAudioStreamIndex")
            audio = next(
                (
                    s
                    for s in streams
                    if s.get("Type") == "Audio" and s.get("Index") == audio_index
                ),
                None,
            ) or next((s for s in streams if s.get("Type") == "Audio"), {})
            width, height = int_or_none(video.get("Width")), int_or_none(
                video.get("Height")
            )
            resolution = resolution_from_height(height, width)
            video_codec = _codec(video.get("Codec"))
            audio_codec = _codec(audio.get("Codec"))
            channels = int_or_none(audio.get("Channels"))
            hdr = _HDR_MAP.get(str(video.get("VideoRangeType") or ""))
            languages = sorted(
                {
                    str(s.get("Language"))[:2]
                    for s in streams
                    if s.get("Type") == "Audio" and s.get("Language")
                }
            )
            subtitles = [
                SubtitleTrack(
                    id=str(s.get("Index")),
                    url=media_proxy.mint_url(
                        self.id,
                        f"/Videos/{item_id}/{msid}/Subtitles/{s.get('Index')}/Stream.vtt",
                    ),
                    lang=str(s.get("Language") or "")[:2] or None,
                    label=s.get("DisplayTitle") or s.get("Language"),
                    format="vtt",
                    forced=bool(s.get("IsForced")),
                )
                for s in streams
                if s.get("Type") == "Subtitle"
                and (s.get("IsExternal") or s.get("IsTextSubtitleStream"))
            ]
            ticks = int_or_none(ms.get("RunTimeTicks") or item.get("RunTimeTicks"))
            duration = ticks / 10_000_000 if ticks else None
            file_name = (
                str(ms.get("Path") or "").replace("\\", "/").rsplit("/", 1)[-1] or None
            )
            file_id = self.file_id(item_id, msid)
            sources.append(
                PlaybackSource(
                    id=self.source_id(item_id, msid, "original"),
                    provider_id=self.id,
                    provider_label=self.label,
                    kind="library",
                    variant="original",
                    delivery="file",
                    url=media_proxy.mint_url(
                        self.id,
                        f"/Videos/{item_id}/stream?"
                        + urlencode({"static": "true", "MediaSourceId": msid}),
                    ),
                    label=describe(
                        resolution=resolution,
                        video_codec=video_codec,
                        hdr=hdr,
                        audio_channels=channels,
                    ),
                    detail=file_name,
                    badges=["Original"],
                    container=(str(ms.get("Container") or "").split(",")[0] or None),
                    video_codec=video_codec,
                    audio_codec=audio_codec,
                    hdr=hdr,
                    resolution=resolution,
                    width=width,
                    height=height,
                    audio_channels=channels,
                    languages=languages,
                    size_bytes=int_or_none(ms.get("Size")),
                    bitrate=int_or_none(ms.get("Bitrate")),
                    duration_seconds=duration,
                    subtitles=subtitles,
                    route_id=self.route_id(item_id, msid, "original"),
                    file_id=file_id,
                )
            )
            for rung_index, rung in enumerate(transcode_ladder(hints, height)):
                variant_key = (
                    "optimized" if rung_index == 0 else f"optimized-{rung.bitrate_kbps}"
                )
                params = {
                    "MediaSourceId": msid,
                    "VideoCodec": "h264",
                    "AudioCodec": "aac",
                    # Jellyfin copies the video when it already fits these limits.
                    "MaxStreamingBitrate": rung.bitrate_kbps * 1000,
                    "MaxWidth": rung.width,
                    "MaxHeight": rung.height,
                    "TranscodingMaxAudioChannels": rung.audio_channels,
                    "SegmentContainer": "ts",
                    "PlaySessionId": uuid.uuid4().hex,
                    "DeviceId": cfg.get("device_id") or "curatio",
                }
                sources.append(
                    PlaybackSource(
                        id=self.source_id(item_id, msid, variant_key),
                        provider_id=self.id,
                        provider_label=self.label,
                        kind="library",
                        variant="optimized",
                        delivery="hls",
                        url=media_proxy.mint_url(
                            self.id,
                            f"/Videos/{item_id}/master.m3u8?" + urlencode(params),
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
                        route_id=self.route_id(item_id, msid, variant_key),
                        file_id=file_id,
                        startup_timeout_seconds=30,
                    )
                )
        return sources

    # --- admin ------------------------------------------------------------------------------
    def admin_router(self) -> APIRouter:
        router = self.base_admin_router()

        @router.post("/connect")
        async def connect(body: JellyfinConnectBody):
            try:
                await self.connect(
                    body.server_url,
                    username=body.username,
                    password=body.password,
                    api_key=body.api_key,
                )
            except LibraryError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc))
            await self.sync()
            return self.public_config()

        return router


provider = JellyfinProvider()
