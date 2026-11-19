"""Server-prepared HLS sessions for browser playback.

The web player plays a source directly when the browser can decode it. For
everything else (MKV/AVI/TS containers, AC-3/E-AC-3/DTS/TrueHD audio, and as a
last resort video the browser cannot decode) this module prepares a bounded
fMP4/CMAF HLS session: video is copied unchanged when the browser can decode it
(H.264, or HEVC tagged ``hvc1``) and otherwise transcoded to H.264; audio is
normalized to AAC; text subtitles are written as WebVTT sidecars. Safari plays
the EVENT playlist natively and HLS.js plays it everywhere else.

FFmpeg never receives the remote source URL directly.  The caller supplies a
short-lived, signed URL for Curatio's existing byte proxy, which re-validates
DNS and every redirect hop before relaying bytes.  That preserves the proxy's
SSRF boundary even though FFmpeg performs its own range requests and reconnects.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from loguru import logger

from app.config import settings
from app.media_probe import (
    SAFE_INPUT_ARGS,
    MediaFacts,
    MediaProbeError,
    media_inspector,
    parse_ffprobe,
)


_SESSION_FILE_RE = re.compile(
    r"^(?:index\.m3u8|init\.mp4|seg_\d{5}\.m4s|sub_\d{1,3}\.vtt)$"
)
_SEGMENT_RE = re.compile(r"^seg_(\d{5})\.m4s$")
_SEGMENT_SECONDS = 4
_TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
_HEVC_COPY_PIXEL_FORMATS = {"yuv420p", "yuvj420p", "yuv420p10le"}
_HDR_RANGES = {"hdr10", "hlg", "dv"}
#: Hint for clients when only a native (desktop) player can play a source.
DESKTOP_APP_HINT = "desktop_app"
_ENDLIST = "#EXT-X-ENDLIST"
# Input pacing: read at 1.5x realtime after a 30 s burst, so ffmpeg doesn't pull
# the whole file to disk far ahead of the playhead.
_READRATE = "1.5"
_READRATE_INITIAL_BURST = "30"
_SAFE_H264_PROFILES = {
    "baseline",
    "constrained baseline",
    "main",
    "high",
}
_LANGUAGE_ALIASES: dict[str, set[str]] = {
    "en": {"en", "eng", "english"},
    "es": {"es", "spa", "spanish", "espanol", "español"},
    "fr": {"fr", "fre", "fra", "french", "francais", "français"},
    "de": {"de", "ger", "deu", "german", "deutsch"},
    "ja": {"ja", "jpn", "jp", "japanese"},
}


class RemuxCapacityError(RuntimeError):
    """The configured compatibility-session limit has been reached."""


class UnsupportedRemuxVideoError(RuntimeError):
    """The source video would require a full video transcode.

    ``hint`` is ``DESKTOP_APP_HINT`` when server transcoding is switched off, so
    the client can point the viewer at the desktop app instead of retrying.
    """

    def __init__(self, message: str = "", *, hint: Optional[str] = None) -> None:
        super().__init__(message)
        self.hint = hint


class RemuxSessionFailed(RuntimeError):
    """FFmpeg exited with an error; the session will produce nothing more."""


@dataclass(frozen=True)
class FfmpegCapabilities:
    """Optional ffmpeg features, detected once per process."""

    readrate: bool = False
    zscale: bool = False


_capabilities: Optional[FfmpegCapabilities] = None
_capabilities_lock: Optional[asyncio.Lock] = None


async def _ffmpeg_output(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        return ""
    return stdout.decode(errors="replace")


async def detect_ffmpeg_capabilities(*, refresh: bool = False) -> FfmpegCapabilities:
    """Detect ``-readrate_initial_burst`` (ffmpeg 6.1+) and the zscale filter.

    Debian bookworm ships ffmpeg 5.1, which has ``-readrate`` but not the burst
    option; pacing without a burst would slow every start, so both must exist.
    Never raises: a missing ffmpeg reports no optional features.
    """
    global _capabilities, _capabilities_lock
    if _capabilities is not None and not refresh:
        return _capabilities
    if _capabilities_lock is None:
        _capabilities_lock = asyncio.Lock()
    async with _capabilities_lock:
        if _capabilities is not None and not refresh:
            return _capabilities
        try:
            options = await _ffmpeg_output("-h", "full")
            filters = await _ffmpeg_output("-filters")
        except (OSError, Exception) as exc:  # noqa: BLE001 - detection is best effort
            logger.warning("ffmpeg_capability_detection_failed error={}", exc)
            options, filters = "", ""
        _capabilities = FfmpegCapabilities(
            readrate="-readrate_initial_burst" in options and "-readrate " in options,
            zscale=bool(re.search(r"^\s*\S*\s+zscale\s", filters, re.MULTILINE)),
        )
        logger.info(
            "ffmpeg_capabilities readrate={} zscale={}",
            _capabilities.readrate,
            _capabilities.zscale,
        )
        return _capabilities


class RemuxPrepareError(RuntimeError):
    """The source could not be probed or FFmpeg could not prepare HLS."""


@dataclass(frozen=True)
class SubtitleSidecar:
    stream_index: int
    language: str
    title: str
    forced: bool
    default: bool


@dataclass(frozen=True)
class MediaProbe:
    duration: Optional[float]
    video_codec: str
    video_profile: str
    pixel_format: str
    audio_stream_index: Optional[int]
    dv_profile: Optional[int] = None
    height: Optional[int] = None
    audio_channels: Optional[int] = None
    subtitles: tuple[SubtitleSidecar, ...] = ()
    dynamic_range: str = "sdr"


@dataclass
class RemuxSession:
    session_id: str
    profile_id: int
    source_url: str
    directory: Path
    start_offset: float = 0.0
    duration: Optional[float] = None
    process: Optional[asyncio.subprocess.Process] = None
    last_access: float = field(default_factory=time.monotonic)
    started_at: float = field(default_factory=time.monotonic)
    video_mode: str = "copy"
    media_start: Optional[float] = None
    subtitles: tuple[SubtitleSidecar, ...] = ()
    last_segment_requested: int = 0
    deleted_below: int = 0
    # Set by the exit watcher when FFmpeg dies with an error.
    failed: bool = False
    failure: Optional[str] = None
    exit_code: Optional[int] = None
    stderr_tail: deque = field(default_factory=lambda: deque(maxlen=12))
    # The source stream index of the audio track in this session (None: no audio).
    audio_index: Optional[int] = None
    # Per-track tfdt shifts from init.mp4 (see track_shifts); computed once.
    track_shifts: Optional[dict[int, int]] = None
    track_shifts_known: bool = False
    # Segments listed in the playlist last served with ?hold=1 (see playlist_text).
    playlist_served: int = -1

    def touch(self) -> None:
        self.last_access = time.monotonic()


def _read_box_headers(data: bytes, start: int = 0, end: Optional[int] = None):
    """Yield ``(type, payload_start, box_end)`` for ISO-BMFF boxes in a range."""
    end = len(data) if end is None else end
    offset = start
    while offset + 8 <= end:
        size = int.from_bytes(data[offset : offset + 4], "big")
        kind = data[offset + 4 : offset + 8].decode("latin-1")
        header = 8
        if size == 1 and offset + 16 <= end:
            size = int.from_bytes(data[offset + 8 : offset + 16], "big")
            header = 16
        elif size == 0:
            size = end - offset
        if size < header or offset + size > end:
            return
        yield kind, offset + header, offset + size
        offset += size


def _find_box(data: bytes, path: list[str], start: int = 0, end: Optional[int] = None):
    """Return ``(payload_start, box_end)`` of the first box along ``path``."""
    for kind, payload, box_end in _read_box_headers(data, start, end):
        if kind != path[0]:
            continue
        if len(path) == 1:
            return payload, box_end
        found = _find_box(data, path[1:], payload, box_end)
        if found:
            return found
    return None


def fmp4_timescale(init_segment: bytes) -> Optional[int]:
    """Timescale of the first track (the copied/transcoded video) in init.mp4."""
    box = _find_box(init_segment, ["moov", "trak", "mdia", "mdhd"])
    if not box:
        return None
    payload, _ = box
    version = init_segment[payload]
    offset = payload + (20 if version == 1 else 12)
    if offset + 4 > len(init_segment):
        return None
    timescale = int.from_bytes(init_segment[offset : offset + 4], "big")
    return timescale or None


def fmp4_initial_empty_edit(init_segment: bytes) -> Optional[float]:
    """Seconds of leading empty edit on the first track in init.mp4.

    FFmpeg's fragmented-MP4 muxer rebases every fragment's decode time to zero
    and records where the stream really starts as an empty edit (``media_time``
    -1) at the head of the track's edit list, in movie-timescale units.
    """
    mvhd = _find_box(init_segment, ["moov", "mvhd"])
    elst = _find_box(init_segment, ["moov", "trak", "edts", "elst"])
    if not mvhd or not elst:
        return None
    payload, _ = mvhd
    movie_scale_at = payload + (20 if init_segment[payload] == 1 else 12)
    if movie_scale_at + 4 > len(init_segment):
        return None
    movie_scale = int.from_bytes(
        init_segment[movie_scale_at : movie_scale_at + 4], "big"
    )
    payload, _ = elst
    version = init_segment[payload]
    width = 8 if version == 1 else 4
    entry = payload + 8
    if not movie_scale or entry + width * 2 > len(init_segment):
        return None
    duration = int.from_bytes(init_segment[entry : entry + width], "big")
    media_time = int.from_bytes(
        init_segment[entry + width : entry + width * 2], "big", signed=True
    )
    if media_time != -1 or duration <= 0:
        return None
    return duration / movie_scale


def fmp4_base_decode_time(segment: bytes) -> Optional[int]:
    """``baseMediaDecodeTime`` of the first track fragment in a media segment."""
    box = _find_box(segment, ["moof", "traf", "tfdt"])
    if not box:
        return None
    payload, _ = box
    version = segment[payload]
    width = 8 if version == 1 else 4
    start = payload + 4
    if start + width > len(segment):
        return None
    return int.from_bytes(segment[start : start + width], "big")


# ---- A/V sync: apply the edit lists browsers ignore ------------------------
#
# FFmpeg's fragmented MP4 aligns each track with an edit list: B-frame video
# presents a few frames after its first decode time, AAC skips its encoder
# priming, and after a seek each track may start with an empty edit. Browser
# MSE ignores edit lists, so audio and video played 40-150 ms apart depending
# on the source's B-frames (measured in Chromium). When serving a segment, each
# track's baseMediaDecodeTime is moved by its edit shift, relative to the
# most-shifted track so nothing goes negative.


def track_shifts(init_segment: bytes) -> Optional[dict[int, int]]:
    """Per track id, ticks (track timescale) to add to its tfdt; None if aligned."""
    mvhd = _find_box(init_segment, ["moov", "mvhd"])
    moov = _find_box(init_segment, ["moov"])
    if not mvhd or not moov:
        return None
    movie_scale_at = mvhd[0] + (20 if init_segment[mvhd[0]] == 1 else 12)
    movie_scale = int.from_bytes(
        init_segment[movie_scale_at : movie_scale_at + 4], "big"
    )
    if not movie_scale:
        return None
    tracks: list[tuple[int, int, float]] = []
    for kind, payload, end in _read_box_headers(init_segment, moov[0], moov[1]):
        if kind != "trak":
            continue
        tkhd = _find_box(init_segment, ["tkhd"], payload, end)
        mdhd = _find_box(init_segment, ["mdia", "mdhd"], payload, end)
        if not tkhd or not mdhd:
            continue
        id_at = tkhd[0] + (20 if init_segment[tkhd[0]] == 1 else 12)
        track_id = int.from_bytes(init_segment[id_at : id_at + 4], "big")
        scale_at = mdhd[0] + (20 if init_segment[mdhd[0]] == 1 else 12)
        timescale = int.from_bytes(init_segment[scale_at : scale_at + 4], "big")
        if not timescale:
            continue
        # Presentation offset = leading empty edits - media_time of the first real edit.
        offset = 0.0
        elst = _find_box(init_segment, ["edts", "elst"], payload, end)
        if elst:
            v1 = init_segment[elst[0]] == 1
            width = 8 if v1 else 4
            count = int.from_bytes(init_segment[elst[0] + 4 : elst[0] + 8], "big")
            at = elst[0] + 8
            for _ in range(count):
                if at + width * 2 + 4 > elst[1]:
                    break
                duration = int.from_bytes(init_segment[at : at + width], "big")
                media_time = int.from_bytes(
                    init_segment[at + width : at + width * 2], "big", signed=True
                )
                at += width * 2 + 4
                if media_time == -1:
                    offset += duration / movie_scale
                else:
                    offset -= media_time / timescale
                    break
        tracks.append((track_id, timescale, offset))
    if not tracks:
        return None
    base = min(offset for _, _, offset in tracks)
    shifts = {
        track_id: round((offset - base) * timescale)
        for track_id, timescale, offset in tracks
    }
    return shifts if any(shifts.values()) else None


def shift_segment(segment: bytes, shifts: Optional[dict[int, int]]) -> bytes:
    """A copy of a media segment with each track's tfdt moved by its shift."""
    if not shifts:
        return segment
    out = bytearray(segment)
    for kind, payload, end in _read_box_headers(out):
        if kind != "moof":
            continue
        for inner, traf_start, traf_end in _read_box_headers(out, payload, end):
            if inner != "traf":
                continue
            tfhd = _find_box(out, ["tfhd"], traf_start, traf_end)
            tfdt = _find_box(out, ["tfdt"], traf_start, traf_end)
            if not tfhd or not tfdt:
                continue
            shift = shifts.get(int.from_bytes(out[tfhd[0] + 4 : tfhd[0] + 8], "big"))
            if not shift:
                continue
            at = tfdt[0] + 4
            width = 8 if out[tfdt[0]] == 1 else 4
            value = int.from_bytes(out[at : at + width], "big") + shift
            out[at : at + width] = (value % (1 << (8 * width))).to_bytes(width, "big")
    return bytes(out)


_PLAYLIST_HOLD_SECONDS = 5.0
_TARGET_DURATION_RE = re.compile(r"^#EXT-X-TARGETDURATION:\d+$", re.MULTILINE)


def live_playlist(text: str) -> str:
    """The playlist as served to hls.js while it is still growing.

    hls.js re-reads a growing playlist once per TARGETDURATION, and a copied
    source's segments are as long as its keyframe spacing (often 10 s): it
    played the first segment, then sat stalled for 10-20 s before asking for
    the second. A 1 s target makes it ask again right away; ``playlist_text``
    holds that request until the next segment exists.
    """
    if "#EXT-X-ENDLIST" in text:
        return text
    return _TARGET_DURATION_RE.sub("#EXT-X-TARGETDURATION:1", text, count=1)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def create_proxy_token(
    source_url: str,
    *,
    secret_key: str,
    ttl_seconds: int,
    now: Optional[int] = None,
) -> str:
    """Create a short-lived token scoped to one exact remote source URL."""
    expires = int(now if now is not None else time.time()) + max(1, ttl_seconds)
    payload = f"remux-proxy\0{expires}\0{source_url}".encode("utf-8")
    signature = hmac.new(secret_key.encode("utf-8"), payload, hashlib.sha256).digest()
    return f"{expires}.{_b64url(signature)}"


#: How long a background (fingerprint) read of the loopback proxy stays valid.
LOOPBACK_INPUT_TTL_SECONDS = 2 * 60 * 60


def loopback_proxy_input(
    source_url: str, *, ttl_seconds: int = LOOPBACK_INPUT_TTL_SECONDS
) -> list[str]:
    """FFmpeg/ffprobe input args that read ``source_url`` via Curatio's own proxy.

    Background jobs have no request to borrow a port from, so this targets the
    port the app was started on (``PORT``, as in the Dockerfile). Reading
    through the proxy means every redirect hop is SSRF-checked, the connection
    is pinned to a vetted public address, playlists are refused, and FFmpeg
    itself can only speak HTTP to loopback (``SAFE_INPUT_ARGS``).
    """
    port = int(os.environ.get("PORT") or 8000)
    token = create_proxy_token(
        source_url, secret_key=settings.secret_key, ttl_seconds=ttl_seconds
    )
    query = urlencode({"url": source_url})
    return [
        "-headers",
        f"X-Curatio-Remux-Token: {token}\r\n",
        *SAFE_INPUT_ARGS,
        "-i",
        f"http://127.0.0.1:{port}/account/api/playback/proxy?{query}",
    ]


def verify_proxy_token(
    source_url: str,
    token: str,
    *,
    secret_key: str,
    now: Optional[int] = None,
) -> bool:
    """Validate expiry and the URL-bound HMAC without leaking comparison timing."""
    try:
        raw_expires, supplied = token.split(".", 1)
        expires = int(raw_expires)
    except (AttributeError, TypeError, ValueError):
        return False
    current = int(now if now is not None else time.time())
    if expires < current:
        return False
    payload = f"remux-proxy\0{expires}\0{source_url}".encode("utf-8")
    expected = _b64url(
        hmac.new(secret_key.encode("utf-8"), payload, hashlib.sha256).digest()
    )
    return secrets.compare_digest(supplied, expected)


def _language_matches(candidate: str, preferred: str) -> bool:
    wanted = preferred.strip().lower()
    if not wanted or wanted == "system":
        return False
    aliases = _LANGUAGE_ALIASES.get(wanted, {wanted})
    value = candidate.strip().lower()
    return value in aliases or any(
        alias in value for alias in aliases if len(alias) > 2
    )


class RemuxManager:
    """Own FFmpeg compatibility sessions under a global concurrency limit."""

    def __init__(
        self,
        *,
        max_concurrent: int = 3,
        idle_timeout_seconds: int = 90,
        max_lifetime_seconds: int = 21600,
        playlist_wait_seconds: float = 20.0,
        probe_timeout_seconds: float = 15.0,
        back_window_seconds: int = 600,
        max_session_bytes: int = 8 * 1024**3,
        max_transcodes: Optional[int] = None,
        allow_transcode: Optional[bool] = None,
        inspector=None,
    ) -> None:
        self.max_concurrent = max(1, max_concurrent)
        self.idle_timeout_seconds = max(30, idle_timeout_seconds)
        self.max_lifetime_seconds = max(600, max_lifetime_seconds)
        self.playlist_wait_seconds = max(1.0, playlist_wait_seconds)
        self.probe_timeout_seconds = max(1.0, probe_timeout_seconds)
        self.back_window_segments = max(15, back_window_seconds // _SEGMENT_SECONDS)
        self.max_session_bytes = max(256 * 1024**2, max_session_bytes)
        self._sessions: dict[str, RemuxSession] = {}
        self._lock = asyncio.Lock()
        self._reaper: Optional[asyncio.Task] = None
        # None = follow the live (admin-editable) settings.
        self._max_transcodes = max_transcodes
        self._allow_transcode = allow_transcode
        self.inspector = inspector if inspector is not None else media_inspector
        self.capabilities = FfmpegCapabilities()
        self._watchers: set[asyncio.Task] = set()

    @property
    def transcode_enabled(self) -> bool:
        if self._allow_transcode is not None:
            return self._allow_transcode
        return bool(getattr(settings, "allow_server_transcode", False))

    @property
    def max_transcodes(self) -> int:
        if self._max_transcodes is not None:
            return max(1, self._max_transcodes)
        return max(1, int(getattr(settings, "playback_transcode_max_streams", 1)))

    def _ensure_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            try:
                self._reaper = asyncio.get_running_loop().create_task(self._reap_loop())
            except RuntimeError:
                self._reaper = None

    async def start(
        self,
        source_url: str,
        internal_proxy_url: str,
        *,
        proxy_token: str,
        profile_id: int,
        start_offset: float = 0.0,
        audio_language: str = "system",
        audio_index: Optional[int] = None,
        allow_hevc: bool = False,
        allow_transcode: bool = False,
        surround: bool = False,
    ) -> RemuxSession:
        """Probe and start one prepared HLS session.

        A slot is reserved before probing, preventing concurrent starts from
        racing past the cap.  Every failure removes the reservation and its
        temporary directory before propagating a typed error.

        ``allow_hevc`` copies HEVC video when the browser reported it can
        decode it; ``allow_transcode`` permits a full H.264 transcode when the
        video cannot be copied. ``surround`` keeps up to 5.1 AAC channels.
        """
        self._ensure_reaper()
        session_id = secrets.token_urlsafe(18)
        directory = Path(tempfile.mkdtemp(prefix=f"curatio-remux-{session_id}-"))
        session = RemuxSession(
            session_id=session_id,
            profile_id=profile_id,
            source_url=source_url,
            directory=directory,
            start_offset=max(0.0, float(start_offset or 0.0)),
        )
        async with self._lock:
            if len(self._sessions) >= self.max_concurrent:
                shutil.rmtree(directory, ignore_errors=True)
                raise RemuxCapacityError(
                    f"Server conversion is busy ({self.max_concurrent} of "
                    f"{self.max_concurrent} sessions in use). Try again shortly "
                    "or choose a source this browser plays directly."
                )
            self._sessions[session_id] = session

        try:
            # The player usually inspected this source moments ago: reuse those
            # ffprobe facts instead of probing the same URL again.
            cached = self.inspector.cached(source_url) if self.inspector else None
            if cached is not None and cached.video is not None:
                probe = self._probe_from_facts(cached, audio_language, audio_index)
            else:
                probe = await self._probe_media(
                    internal_proxy_url,
                    audio_language,
                    proxy_token=proxy_token,
                    source_url=source_url,
                    audio_index=audio_index,
                )
            server_transcode = self.transcode_enabled
            try:
                video_mode = self._validate_video(
                    probe,
                    allow_hevc=allow_hevc,
                    allow_transcode=allow_transcode and server_transcode,
                )
            except UnsupportedRemuxVideoError as exc:
                if not server_transcode:
                    # Nothing on this server can make it playable in a browser.
                    raise UnsupportedRemuxVideoError(
                        f"{exc} — server transcoding is turned off. "
                        "Play it in the desktop app.",
                        hint=DESKTOP_APP_HINT,
                    ) from exc
                raise
            if video_mode == "transcode":
                async with self._lock:
                    transcodes = sum(
                        1
                        for other in self._sessions.values()
                        if other is not session and other.video_mode == "transcode"
                    )
                    if transcodes >= self.max_transcodes:
                        raise RemuxCapacityError(
                            "Server transcoding is busy. Try again shortly or "
                            "choose a source this browser plays directly."
                        )
                    session.video_mode = video_mode
            else:
                session.video_mode = video_mode
            self.capabilities = await detect_ffmpeg_capabilities()
            session.duration = probe.duration
            session.subtitles = probe.subtitles
            session.audio_index = probe.audio_stream_index
            await self._spawn_ffmpeg(
                session,
                internal_proxy_url,
                proxy_token=proxy_token,
                audio_stream_index=probe.audio_stream_index,
                video_mode=session.video_mode,
                audio_channels=(min(6, probe.audio_channels or 2) if surround else 2),
                subtitles=probe.subtitles,
                target_height=probe.height,
                video_codec=probe.video_codec,
                dynamic_range=probe.dynamic_range,
            )
            await self._wait_for_playlist(session)
            session.media_start = self.measure_media_start(session)
            session.touch()
            return session
        except (RemuxCapacityError, UnsupportedRemuxVideoError, RemuxPrepareError):
            await self.stop(session_id)
            raise
        except Exception as exc:
            await self.stop(session_id)
            raise RemuxPrepareError("Could not prepare compatible playback") from exc

    async def _probe_media(
        self,
        internal_proxy_url: str,
        audio_language: str,
        *,
        proxy_token: str,
        source_url: Optional[str] = None,
        audio_index: Optional[int] = None,
    ) -> MediaProbe:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            "-headers",
            f"X-Curatio-Remux-Token: {proxy_token}\r\n",
            *SAFE_INPUT_ARGS,
            internal_proxy_url,
        ]
        proc: Optional[asyncio.subprocess.Process] = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.probe_timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise RemuxPrepareError("Source probe timed out") from exc
        except OSError as exc:
            raise RemuxPrepareError("ffprobe is unavailable") from exc
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip()[-500:]
            logger.warning("remux_probe_failed detail={}", detail)
            raise RemuxPrepareError("Source probe failed")
        try:
            data = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemuxPrepareError("Source probe returned invalid metadata") from exc
        try:
            facts = parse_ffprobe(data)
        except MediaProbeError as exc:
            raise UnsupportedRemuxVideoError("Source has no playable streams") from exc
        if facts.video is None:
            raise UnsupportedRemuxVideoError("Source has no video track")
        if source_url and self.inspector is not None:
            # Share the facts with /playback/inspect (and later restarts).
            self.inspector.remember(source_url, facts)
        return self._probe_from_facts(facts, audio_language, audio_index)

    @staticmethod
    def _probe_from_facts(
        facts: MediaFacts, audio_language: str, audio_index: Optional[int] = None
    ) -> MediaProbe:
        """Pick the audio track and text subtitles from parsed ffprobe facts.

        ``audio_index`` (an ffprobe stream index, as listed in the facts) is the
        viewer's explicit choice in the player's Audio menu and wins over the
        language preference; an index the file does not have is ignored.
        """
        assert facts.video is not None
        selected_audio = None
        if audio_index is not None:
            selected_audio = next(
                (track for track in facts.audio if track.index == audio_index), None
            )
        if selected_audio is None and audio_language and audio_language != "system":
            selected_audio = next(
                (
                    track
                    for track in facts.audio
                    if _language_matches(track.language, audio_language)
                ),
                None,
            )
        if selected_audio is None and facts.audio:
            selected_audio = next(
                (track for track in facts.audio if track.default), facts.audio[0]
            )
        subtitles = tuple(
            SubtitleSidecar(
                stream_index=track.index,
                language=track.language,
                title=track.title,
                forced=track.forced,
                default=track.default,
            )
            for track in facts.subtitles
            if track.codec in _TEXT_SUBTITLE_CODECS
        )[:8]
        return MediaProbe(
            duration=facts.duration,
            video_codec=facts.video.codec,
            video_profile=facts.video.profile.lower(),
            pixel_format=facts.video.pix_fmt,
            audio_stream_index=selected_audio.index if selected_audio else None,
            dv_profile=facts.video.dv_profile,
            height=facts.video.height,
            audio_channels=selected_audio.channels if selected_audio else None,
            subtitles=subtitles,
            dynamic_range=facts.video.dynamic_range or "sdr",
        )

    @staticmethod
    def _validate_video(
        probe: MediaProbe,
        *,
        allow_hevc: bool = False,
        allow_transcode: bool = False,
    ) -> str:
        """Return ``copy`` or ``transcode``, or raise when neither is allowed."""
        reason = ""
        if probe.video_codec == "h264":
            if probe.video_profile and probe.video_profile not in _SAFE_H264_PROFILES:
                reason = f"Unsupported H.264 profile: {probe.video_profile}"
            # Browser-safe H.264 is 8-bit 4:2:0. Reject High 10/4:2:2/4:4:4
            # instead of copying a stream that will black-screen in HLS.
            elif probe.pixel_format and probe.pixel_format not in {
                "yuv420p",
                "yuvj420p",
            }:
                reason = f"Unsupported H.264 pixel format: {probe.pixel_format}"
            else:
                return "copy"
        elif probe.video_codec == "hevc" and allow_hevc:
            # Dolby Vision profile 5 has no HDR10/SDR base layer: copied as
            # plain HEVC it plays with purple/green colours.
            if probe.dv_profile == 5:
                reason = "Dolby Vision profile 5 needs full video transcoding"
            elif probe.pixel_format and probe.pixel_format not in (
                _HEVC_COPY_PIXEL_FORMATS
            ):
                reason = f"Unsupported HEVC pixel format: {probe.pixel_format}"
            else:
                return "copy"
        else:
            reason = "This source needs full video transcoding"
        if allow_transcode:
            return "transcode"
        raise UnsupportedRemuxVideoError(reason or "Unsupported video")

    async def _spawn_ffmpeg(
        self,
        session: RemuxSession,
        internal_proxy_url: str,
        *,
        proxy_token: str,
        audio_stream_index: Optional[int],
        video_mode: str = "copy",
        audio_channels: int = 2,
        subtitles: tuple[SubtitleSidecar, ...] = (),
        target_height: Optional[int] = None,
        video_codec: str = "h264",
        dynamic_range: str = "sdr",
    ) -> None:
        channels = max(1, min(6, int(audio_channels or 2)))
        caps = self.capabilities
        audio_args = (
            [
                "-map",
                f"0:{audio_stream_index}",
                # Stretch/squeeze audio to its timestamps so gaps or drift in
                # the source don't pull A/V out of sync over a long movie.
                "-af",
                "aresample=async=1",
                "-c:a",
                "aac",
                "-b:a",
                "384k" if channels > 2 else "160k",
                "-ac",
                str(channels),
            ]
            if audio_stream_index is not None
            else ["-an"]
        )
        if video_mode == "transcode":
            height = min(1080, int(target_height or 1080))
            filters = [f"scale=-2:'min({height},ih)'"]
            if dynamic_range in _HDR_RANGES:
                filters.append(self._tonemap_filter(caps.zscale, dynamic_range))
            filters.append("format=yuv420p")
            video_args = [
                "-c:v",
                "libx264",
                "-preset",
                "superfast",
                # Leave the rest of a small VPS to the web app and other viewers.
                "-threads",
                "2",
                "-crf",
                "21",
                "-maxrate",
                "12M",
                "-bufsize",
                "24M",
                "-pix_fmt",
                "yuv420p",
                "-vf",
                ",".join(filters),
                "-profile:v",
                "high",
                "-g",
                str(_SEGMENT_SECONDS * 24),
                "-sc_threshold",
                "0",
                "-force_key_frames",
                f"expr:gte(t,n_forced*{_SEGMENT_SECONDS})",
            ]
        else:
            video_args = ["-c:v", "copy"]
            # Safari refuses HEVC tagged hev1; H.264 keeps its own avc1 tag.
            if video_codec == "hevc":
                video_args += ["-tag:v", "hvc1"]
        subtitle_outputs: list[str] = []
        for track in subtitles:
            subtitle_outputs += [
                "-map",
                f"0:{track.stream_index}",
                "-c:s",
                "webvtt",
                "-f",
                "webvtt",
                # Cue by cue: a buffered sidecar stays empty for minutes.
                "-flush_packets",
                "1",
                str(session.directory / f"sub_{track.stream_index}.vtt"),
            ]
        seek = session.start_offset > 0
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_on_network_error",
            "1",
            "-reconnect_delay_max",
            "5",
            "-headers",
            f"X-Curatio-Remux-Token: {proxy_token}\r\n",
            # After an input seek, keep source timestamps so the first
            # segment's decode time says where playback really starts (the
            # keyframe before the requested time, for copied video).
            # -noaccurate_seek: audio starts at that same keyframe, not at the
            # exact requested time. Otherwise the tracks start seconds apart,
            # recorded only in edit lists that browsers' MSE ignores, and audio
            # ran ahead of the picture by up to a whole GOP on every resume.
            *(["-noaccurate_seek", "-copyts", "-start_at_zero"] if seek else []),
            *(["-ss", f"{session.start_offset:.3f}"] if seek else []),
            *(
                [
                    "-readrate",
                    _READRATE,
                    "-readrate_initial_burst",
                    _READRATE_INITIAL_BURST,
                ]
                if caps.readrate
                else []
            ),
            *SAFE_INPUT_ARGS,
            "-i",
            internal_proxy_url,
            "-map",
            "0:v:0",
            *video_args,
            *audio_args,
            # Shift negative start timestamps (B-frame DTS, audio priming) so
            # players don't drop the first frames or start A/V out of step.
            # After a seek, -copyts keeps the source time the player maps
            # through media_start; make_non_negative leaves positive starts
            # alone so that measurement survives.
            "-avoid_negative_ts",
            "make_non_negative" if seek else "make_zero",
            "-max_muxing_queue_size",
            "1024",
            "-f",
            "hls",
            "-hls_segment_type",
            "fmp4",
            "-hls_fmp4_init_filename",
            "init.mp4",
            "-hls_time",
            str(_SEGMENT_SECONDS),
            "-hls_list_size",
            "0",
            "-hls_flags",
            "independent_segments+temp_file",
            "-hls_playlist_type",
            "event",
            "-hls_segment_filename",
            str(session.directory / "seg_%05d.m4s"),
            str(session.directory / "index.m3u8"),
            *subtitle_outputs,
        ]
        try:
            session.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise RemuxPrepareError("ffmpeg is unavailable") from exc
        logger.info(
            "playback_remux_started session={} profile={} pid={} start={} video={}",
            session.session_id,
            session.profile_id,
            session.process.pid,
            session.start_offset,
            video_mode,
        )
        stderr_task = asyncio.create_task(self._log_ffmpeg_stderr(session))
        watcher = asyncio.create_task(self._watch_ffmpeg(session, stderr_task))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)

    @staticmethod
    def _tonemap_filter(zscale: bool, dynamic_range: str = "hdr10") -> str:
        """HDR (PQ/HLG, BT.2020) to SDR BT.709.

        zscale linearizes so ``tonemap`` works on real light levels. The input
        transfer/matrix/primaries are stated explicitly: MKVs often lose those
        tags, and zscale then fails with "no path between colorspaces". Without
        zscale (some static builds), fall back to a BT.2020→BT.709 matrix
        conversion: no real tone mapping, so a little flat, but correct hues
        instead of the washed-out green cast of untouched HDR.
        """
        if zscale:
            transfer = "arib-std-b67" if dynamic_range == "hlg" else "smpte2084"
            return (
                f"zscale=tin={transfer}:min=bt2020nc:pin=bt2020:rin=tv:"
                "t=linear:npl=100:p=bt2020:m=bt2020nc:r=tv,"
                "format=gbrpf32le,zscale=p=bt709,"
                "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv"
            )
        return "scale=in_color_matrix=bt2020:out_color_matrix=bt709:out_range=tv"

    async def _watch_ffmpeg(
        self, session: RemuxSession, stderr_task: Optional[asyncio.Task] = None
    ) -> None:
        """React to FFmpeg exiting on its own.

        Failure marks the session failed so its playlist and missing segments
        answer with an error at once (instead of the player stalling until a
        timeout). Success makes sure the playlist ends with ``#EXT-X-ENDLIST``
        so players treat the stream as complete.
        """
        proc = session.process
        if proc is None:
            return
        try:
            code = await proc.wait()
        except asyncio.CancelledError:
            return
        if code is None:
            return
        if stderr_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(stderr_task), timeout=1)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        self.note_ffmpeg_exit(session, code)

    def note_ffmpeg_exit(self, session: RemuxSession, code: int) -> None:
        """Record an FFmpeg exit for a live session (ignored once stopped)."""
        if self._sessions.get(session.session_id) is not session:
            return  # stopped deliberately; stop() owns the cleanup
        session.exit_code = code
        if code == 0:
            self._ensure_endlist(session)
            logger.info(
                "playback_remux_finished session={} profile={}",
                session.session_id,
                session.profile_id,
            )
            return
        detail = " | ".join(session.stderr_tail) if session.stderr_tail else ""
        session.failed = True
        session.failure = f"Playback preparation failed (ffmpeg exit {code})" + (
            f": {detail[-300:]}" if detail else ""
        )
        logger.warning(
            "playback_remux_failed session={} profile={} code={} detail={}",
            session.session_id,
            session.profile_id,
            code,
            detail[-500:],
        )

    @staticmethod
    def _ensure_endlist(session: RemuxSession) -> None:
        playlist = session.directory / "index.m3u8"
        try:
            text = playlist.read_text()
        except OSError:
            return
        if _ENDLIST in text:
            return
        try:
            with playlist.open("a") as handle:
                handle.write(("" if text.endswith("\n") else "\n") + _ENDLIST + "\n")
        except OSError as exc:
            logger.warning(
                "playback_remux_endlist_failed session={} error={}",
                session.session_id,
                exc,
            )

    async def _wait_for_playlist(self, session: RemuxSession) -> None:
        deadline = time.monotonic() + self.playlist_wait_seconds
        playlist = session.directory / "index.m3u8"
        while time.monotonic() < deadline:
            if playlist.exists() and playlist.stat().st_size > 0:
                return
            if session.process and session.process.returncode is not None:
                raise RemuxPrepareError("FFmpeg exited before producing playback")
            await asyncio.sleep(0.1)
        raise RemuxPrepareError("Compatible playback preparation timed out")

    @staticmethod
    def measure_media_start(session: RemuxSession) -> float:
        """Source time of the session's first frame.

        For copied video an input seek lands on the keyframe before the
        requested time. With ``-copyts -start_at_zero`` the first segment's
        decode time records that keyframe, so the player can map local time
        back to movie time exactly. Falls back to the requested start.
        """
        if session.start_offset <= 0:
            return 0.0
        try:
            init = (session.directory / "init.mp4").read_bytes()
            first = min(session.directory.glob("seg_*.m4s"), default=None)
            if first is None:
                return session.start_offset
            timescale = fmp4_timescale(init)
            decode_time = fmp4_base_decode_time(first.read_bytes()[: 1 << 20])
        except OSError:
            return session.start_offset
        if not timescale or decode_time is None:
            return session.start_offset
        measured = decode_time / timescale
        if measured == 0:
            # FFmpeg rebases fragments to zero; the real start is the track's
            # leading empty edit.
            measured = fmp4_initial_empty_edit(init)
            if measured is None:
                return session.start_offset
        # Trust it only when it is the keyframe at or shortly before the
        # requested time; anything else means the timestamps were rebased.
        if session.start_offset - 30 <= measured <= session.start_offset + 0.5:
            return round(measured, 3)
        return session.start_offset

    @staticmethod
    async def _log_ffmpeg_stderr(session: RemuxSession) -> None:
        stderr = session.process.stderr if session.process else None
        if stderr is None:
            return
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    return
                text = line.decode(errors="replace").rstrip()
                session.stderr_tail.append(text[:300])
                logger.warning(
                    "playback_remux_ffmpeg session={} {}",
                    session.session_id,
                    text,
                )
        except (asyncio.CancelledError, Exception):
            return

    async def playlist_text(self, session_id: str, path: Path) -> str:
        """A session's playlist for hls.js (requested with ``?hold=1``).

        The first request is answered at once; a repeat request is held (up to
        5 s) until a segment the player has not seen exists, so each segment
        is picked up the moment it is written instead of on the next poll.
        """
        session = self._sessions.get(session_id)
        deadline = time.monotonic() + _PLAYLIST_HOLD_SECONDS
        while True:
            try:
                text = path.read_text()
            except OSError:
                text = ""
            if session is None or session_id not in self._sessions:
                return live_playlist(text)
            count = text.count("#EXTINF")
            done = (
                "#EXT-X-ENDLIST" in text
                or session.failed
                or (
                    session.process is not None
                    and session.process.returncode is not None
                )
            )
            if count > session.playlist_served or done or time.monotonic() >= deadline:
                session.playlist_served = count
                session.touch()
                return live_playlist(text)
            await asyncio.sleep(0.1)

    def aligned_segment(self, session_id: str, path: Path) -> bytes:
        """A media segment's bytes with the tracks aligned for browser MSE."""
        data = path.read_bytes()
        session = self._sessions.get(session_id)
        if session is None:
            return data
        if not session.track_shifts_known:
            try:
                session.track_shifts = track_shifts(
                    (session.directory / "init.mp4").read_bytes()
                )
            except OSError:
                return data  # init not readable yet: serve as-is, retry next segment
            session.track_shifts_known = True
        return shift_segment(data, session.track_shifts)

    def file_path(
        self, session_id: str, filename: str, *, profile_id: int
    ) -> Optional[Path]:
        if not _SESSION_FILE_RE.fullmatch(filename):
            return None
        session = self._sessions.get(session_id)
        if not session or session.profile_id != profile_id:
            return None
        if session.failed:
            # Already-written segments still play; the playlist and anything
            # not yet produced fail fast so the player recovers right away.
            path = session.directory / filename
            if filename == "index.m3u8" or not path.is_file():
                raise RemuxSessionFailed(
                    session.failure or "Playback preparation failed"
                )
        segment = _SEGMENT_RE.fullmatch(filename)
        if segment:
            number = int(segment.group(1))
            if number < session.deleted_below:
                return None
            session.last_segment_requested = max(session.last_segment_requested, number)
        path = session.directory / filename
        if not path.is_file():
            return None
        session.touch()
        return path

    def keepalive(self, session_id: str, *, profile_id: int) -> bool:
        """Renew a session's lease while its player is open (even paused)."""
        session = self.session_for_profile(session_id, profile_id=profile_id)
        if session is None:
            return False
        session.touch()
        return True

    def prune_segments(self, session: RemuxSession) -> int:
        """Delete segments far behind the playhead to bound temp disk use.

        Segments more than the back window behind the newest requested segment
        are removed; if the directory is still over the per-session byte cap,
        older ones behind the playhead go too. A seek back into a deleted range
        gets a 404 and the player starts a new session at that time.
        """
        segments: list[tuple[int, Path, int]] = []
        total = 0
        for path in session.directory.glob("seg_*.m4s"):
            match = _SEGMENT_RE.fullmatch(path.name)
            if not match:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            segments.append((int(match.group(1)), path, size))
            total += size
        segments.sort()
        keep_from = session.last_segment_requested - self.back_window_segments
        removed = 0
        for number, path, size in segments:
            behind_window = number < keep_from
            over_cap = (
                total > self.max_session_bytes
                and number < session.last_segment_requested - 2
            )
            if not (behind_window or over_cap):
                continue
            try:
                path.unlink()
            except OSError:
                continue
            total -= size
            removed += 1
            session.deleted_below = max(session.deleted_below, number + 1)
        return removed

    def session_for_profile(
        self, session_id: str, *, profile_id: int
    ) -> Optional[RemuxSession]:
        session = self._sessions.get(session_id)
        if not session or session.profile_id != profile_id:
            return None
        return session

    async def stop(self, session_id: str, *, profile_id: Optional[int] = None) -> bool:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session or (
                profile_id is not None and session.profile_id != profile_id
            ):
                return False
            self._sessions.pop(session_id, None)
        proc = session.process
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
        shutil.rmtree(session.directory, ignore_errors=True)
        logger.info(
            "playback_remux_stopped session={} profile={}",
            session.session_id,
            session.profile_id,
        )
        return True

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            await self.reap_stale()

    async def reap_stale(self, *, now: Optional[float] = None) -> int:
        """Stop idle/expired sessions once; exposed for deterministic tests."""
        current = time.monotonic() if now is None else now
        stale = [
            session_id
            for session_id, session in list(self._sessions.items())
            if current - session.last_access > self.idle_timeout_seconds
            or current - session.started_at > self.max_lifetime_seconds
        ]
        for session_id in stale:
            await self.stop(session_id)
        for session in list(self._sessions.values()):
            self.prune_segments(session)
        return len(stale)

    async def shutdown(self) -> None:
        if self._reaper and not self._reaper.done():
            self._reaper.cancel()
            try:
                await self._reaper
            except asyncio.CancelledError:
                pass
        for session_id in list(self._sessions):
            await self.stop(session_id)


remux_manager = RemuxManager(
    max_concurrent=settings.playback_remux_max_streams,
    idle_timeout_seconds=settings.playback_remux_idle_timeout,
)
