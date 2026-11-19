"""Data shapes shared by Curatio and every playback source provider.

These are the v2 client contract (see ``docs/playback-api.md``). Providers build
``PlaybackSource`` objects; the resolver orders them and returns them unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Optional

from pydantic import BaseModel, Field

SourceKind = Literal["library", "addon"]
Variant = Literal["original", "optimized"]
Delivery = Literal["file", "hls"]
Quality = Literal["original", "optimized"]
FailureCategory = Literal["network", "decode", "timeout", "auth", "not_found", "other"]

_IMDB_RE = re.compile(r"^tt\d{5,12}$")


@dataclass(frozen=True)
class MediaRef:
    """One playable thing: a movie, or one episode of a series."""

    type: Literal["movie", "series"]
    imdb_id: Optional[str] = None
    tmdb_id: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None

    def __post_init__(self) -> None:
        if self.type not in ("movie", "series"):
            raise ValueError("type must be movie or series")
        if self.imdb_id is not None and not _IMDB_RE.match(self.imdb_id):
            raise ValueError("imdb_id must look like tt1234567")
        if self.imdb_id is None and self.tmdb_id is None:
            raise ValueError("imdb_id or tmdb_id is required")
        if self.type == "series" and (self.season is None or self.episode is None):
            raise ValueError("season and episode are required for series")
        if self.type == "movie" and (
            self.season is not None or self.episode is not None
        ):
            raise ValueError("season and episode only apply to series")

    @property
    def is_episode(self) -> bool:
        return self.type == "series"

    @property
    def tmdb_media_type(self) -> str:
        return "tv" if self.type == "series" else "movie"

    def key(self) -> str:
        """Stable cache/telemetry key, preferring the IMDb identity."""
        base = self.imdb_id or f"tmdb-{self.tmdb_id}"
        if self.is_episode:
            return f"{self.type}:{base}:{self.season}:{self.episode}"
        return f"{self.type}:{base}"

    def title_ref(self) -> "MediaRef":
        """The title this ref belongs to (the show for an episode)."""
        if not self.is_episode:
            return self
        return TitleRef(type="series", imdb_id=self.imdb_id, tmdb_id=self.tmdb_id)

    def with_episode(self, season: int, episode: int) -> "MediaRef":
        return MediaRef(
            type="series",
            imdb_id=self.imdb_id,
            tmdb_id=self.tmdb_id,
            season=season,
            episode=episode,
        )


@dataclass(frozen=True)
class TitleRef(MediaRef):
    """A whole title (movie or show) for availability checks; no episode."""

    def __post_init__(self) -> None:
        if self.type not in ("movie", "series"):
            raise ValueError("type must be movie or series")
        if self.imdb_id is None and self.tmdb_id is None:
            raise ValueError("imdb_id or tmdb_id is required")

    def key(self) -> str:
        return f"{self.type}:{self.imdb_id or f'tmdb-{self.tmdb_id}'}"


@dataclass(frozen=True)
class PlaybackHints:
    """What the asking device can take, so the media server can size a
    transcode to it instead of one fixed 1080p / 8 Mbps / stereo rendition.

    All optional: a missing hint keeps the old default for that dimension.
    """

    #: Sustainable bitrate for this device right now (measured, with headroom).
    max_bitrate_kbps: Optional[int] = None
    #: Largest frame the device shows ("2160p", "1080p", "720p").
    max_resolution: Optional[str] = None
    #: Speaker channels the device can output (2, 6, 8).
    audio_channels: Optional[int] = None


@dataclass
class PlaybackContext:
    """Who is asking and how, for one resolve call."""

    user_key: str
    quality: Quality = "original"
    refresh: bool = False
    viewing_session_id: Optional[str] = None
    title: Optional[str] = None
    diagnostics: dict = field(default_factory=dict)
    hints: PlaybackHints = field(default_factory=PlaybackHints)


class SubtitleTrack(BaseModel):
    id: str
    url: str
    lang: Optional[str] = None
    label: Optional[str] = None
    format: Optional[str] = None
    forced: bool = False


class PlaybackSource(BaseModel):
    """One way to play a ``MediaRef``. Display strings are never parsed by clients."""

    id: str
    provider_id: str
    provider_label: str
    kind: SourceKind
    variant: Variant = "original"
    delivery: Delivery = "file"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    label: str = ""
    detail: Optional[str] = None
    badges: list[str] = Field(default_factory=list)
    container: Optional[str] = None
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None
    hdr: Optional[str] = None
    resolution: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    audio_channels: Optional[int] = None
    languages: list[str] = Field(default_factory=list)
    size_bytes: Optional[int] = None
    bitrate: Optional[int] = None
    duration_seconds: Optional[float] = None
    subtitles: list[SubtitleTrack] = Field(default_factory=list)
    route_id: str
    file_id: Optional[str] = None
    startup_timeout_seconds: Optional[float] = None
    #: Same file straight from the media server on the home network (Plex
    #: transient token, short-lived). Clients try it first and fall back to
    #: ``url`` (the signed proxy) when it doesn't answer quickly.
    lan_url: Optional[str] = None


class AvailabilitySource(BaseModel):
    provider_id: str
    label: str
    kind: SourceKind


class Availability(BaseModel):
    playable: bool = False
    in_library: bool = False
    sources: list[AvailabilitySource] = Field(default_factory=list)


class FailureReport(BaseModel):
    source_id: Optional[str] = Field(default=None, max_length=128)
    provider_id: Optional[str] = Field(default=None, max_length=64)
    route_id: Optional[str] = Field(default=None, max_length=128)
    file_id: Optional[str] = Field(default=None, max_length=128)
    failure_category: Optional[FailureCategory] = None
    reason: Optional[str] = Field(default=None, max_length=500)


class ProviderStatus(BaseModel):
    id: str
    label: str
    kind: SourceKind
    status: Literal["ok", "degraded", "offline"] = "ok"
