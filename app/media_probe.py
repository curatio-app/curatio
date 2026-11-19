"""What a playback source really contains, measured with ffprobe.

The web player used to guess codecs from file names ("Atmos" read as TrueHD,
unknown codecs tried as native and failed over later). This module asks
ffprobe instead and returns the facts the browser needs to pick a path:
container, exact RFC 6381 codec strings for MediaCapabilities, HDR and Dolby
Vision signalling, and every audio and subtitle track.

Like the compatibility remux, ffprobe never sees the remote URL. It reads
through Curatio's byte proxy with a short-lived token bound to that one URL, so
the proxy's SSRF checks still apply to every redirect hop. Reads are bounded by
``probesize``/``analyzeduration`` and a wall-clock timeout, and results are
cached so opening Details and then pressing Play only probes once.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from loguru import logger

#: Input restrictions for every ffmpeg/ffprobe read of the loopback proxy. FFmpeg
#: may only speak HTTP to that proxy, and only open single-file containers: a
#: playlist demuxer (HLS, DASH, concat) would fetch further URLs on its own,
#: outside the proxy's SSRF checks.
SAFE_INPUT_ARGS = [
    "-protocol_whitelist",
    "http,tcp",
    "-format_whitelist",
    "matroska,webm,mov,mp4,m4a,mpegts,avi,flv,ogg,mpeg,asf,wav,flac,mp3,aac,"
    "ac3,eac3,dts,truehd,h264,hevc,srt,webvtt,ass",
]


class MediaProbeError(RuntimeError):
    """ffprobe could not read the source. ``reason`` is a stable category."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


class MediaProbeTimeout(MediaProbeError):
    def __init__(self) -> None:
        super().__init__("timeout", "Source inspection timed out")


_TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}

_H264_PROFILES: dict[str, tuple[int, int]] = {
    "constrained baseline": (0x42, 0x40),
    "baseline": (0x42, 0x00),
    "main": (0x4D, 0x40),
    "extended": (0x58, 0x00),
    "high": (0x64, 0x00),
    "high 10": (0x6E, 0x00),
    "high 10 intra": (0x6E, 0x10),
    "high 4:2:2": (0x7A, 0x00),
    "high 4:2:2 intra": (0x7A, 0x10),
    "high 4:4:4 predictive": (0xF4, 0x00),
}

_HEVC_PROFILES: dict[str, tuple[int, str]] = {
    "main": (1, "6"),
    "main 10": (2, "4"),
    "main still picture": (3, "8"),
    "rext": (4, "10"),
}


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # drop NaN


def bit_depth(stream: dict) -> int:
    raw = _int(stream.get("bits_per_raw_sample"))
    if raw and raw >= 8:
        return raw
    pix_fmt = str(stream.get("pix_fmt") or "")
    match = re.search(r"p(\d{2})(?:le|be)?$", pix_fmt)
    if match:
        return int(match.group(1))
    return 8


def h264_codec_string(profile: str, level: Optional[int]) -> Optional[str]:
    entry = _H264_PROFILES.get(profile.strip().lower())
    if entry is None or not level or level <= 0:
        return None
    profile_idc, constraints = entry
    return f"avc1.{profile_idc:02X}{constraints:02X}{level:02X}"


def hevc_codec_string(profile: str, level: Optional[int]) -> Optional[str]:
    entry = _HEVC_PROFILES.get(profile.strip().lower())
    if entry is None or not level or level <= 0:
        return None
    profile_idc, compatibility = entry
    # ffprobe does not report the tier; main tier ("L") covers practically all
    # consumer releases. Browsers treat the constraint byte loosely.
    return f"hvc1.{profile_idc}.{compatibility}.L{level}.B0"


def av1_codec_string(profile: str, level: Optional[int], depth: int) -> str:
    profile_number = {"main": 0, "high": 1, "professional": 2}.get(
        profile.strip().lower(), 0
    )
    seq_level = level if level is not None and 0 <= level <= 31 else 8
    return f"av01.{profile_number}.{seq_level:02d}M.{depth:02d}"


def vp9_codec_string(profile: str, depth: int) -> str:
    match = re.search(r"(\d)", profile or "")
    profile_number = int(match.group(1)) if match else 0
    return f"vp09.{profile_number:02d}.40.{depth:02d}"


def audio_codec_string(codec: str, profile: str = "") -> Optional[str]:
    codec = codec.lower()
    profile = profile.lower()
    if codec == "aac":
        if "he-aacv2" in profile:
            return "mp4a.40.29"
        if "he-aac" in profile:
            return "mp4a.40.5"
        return "mp4a.40.2"
    return {
        "mp3": "mp3",
        "opus": "opus",
        "flac": "flac",
        "vorbis": "vorbis",
        "ac3": "ac-3",
        "eac3": "ec-3",
        "dts": "dtsc",
        "truehd": "mlpa",
        "alac": "alac",
    }.get(codec)


@dataclass(frozen=True)
class VideoFacts:
    index: int
    codec: str
    profile: str
    level: Optional[int]
    pix_fmt: str
    bit_depth: int
    width: Optional[int]
    height: Optional[int]
    frame_rate: Optional[float]
    dynamic_range: str  # sdr | hdr10 | hlg | dv
    dv_profile: Optional[int]
    codec_string: Optional[str]


@dataclass(frozen=True)
class AudioFacts:
    index: int
    codec: str
    profile: str
    channels: Optional[int]
    language: str
    title: str
    default: bool
    codec_string: Optional[str]


@dataclass(frozen=True)
class SubtitleFacts:
    index: int
    codec: str
    language: str
    title: str
    forced: bool
    default: bool
    text: bool


@dataclass(frozen=True)
class MediaFacts:
    container: str
    duration: Optional[float]
    bitrate: Optional[int]
    size: Optional[int]
    start_time: float
    video: Optional[VideoFacts]
    audio: list[AudioFacts] = field(default_factory=list)
    subtitles: list[SubtitleFacts] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)


def _container(format_name: str) -> str:
    names = {part.strip() for part in format_name.lower().split(",") if part.strip()}
    if "matroska" in names or "webm" in names:
        return "matroska"
    if names & {"mov", "mp4", "m4a", "3gp"}:
        return "mp4"
    if "mpegts" in names:
        return "mpegts"
    if "avi" in names:
        return "avi"
    if "hls" in names or "applehttp" in names:
        return "hls"
    return next(iter(sorted(names)), "unknown") if names else "unknown"


def _tags(stream: dict) -> dict:
    tags = stream.get("tags")
    return tags if isinstance(tags, dict) else {}


def _disposition(stream: dict, key: str) -> bool:
    disposition = stream.get("disposition")
    return bool(isinstance(disposition, dict) and disposition.get(key))


def _dynamic_range(stream: dict) -> tuple[str, Optional[int]]:
    dv_profile = None
    for side in stream.get("side_data_list") or []:
        if not isinstance(side, dict):
            continue
        if "dovi" in str(side.get("side_data_type") or "").lower():
            dv_profile = _int(side.get("dv_profile"))
            return "dv", dv_profile
    tag = str(stream.get("codec_tag_string") or "").lower()
    if tag in {"dvh1", "dvhe", "dav1"}:
        return "dv", None
    transfer = str(stream.get("color_transfer") or "").lower()
    if transfer == "smpte2084":
        return "hdr10", None
    if transfer == "arib-std-b67":
        return "hlg", None
    return "sdr", None


def _frame_rate(stream: dict) -> Optional[float]:
    raw = str(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "")
    if "/" in raw:
        numerator, _, denominator = raw.partition("/")
        num, den = _float(numerator), _float(denominator)
        if num and den:
            return round(num / den, 3)
        return None
    return _float(raw)


def _video_facts(stream: dict) -> VideoFacts:
    codec = str(stream.get("codec_name") or "").lower()
    profile = str(stream.get("profile") or "")
    level = _int(stream.get("level"))
    depth = bit_depth(stream)
    dynamic_range, dv_profile = _dynamic_range(stream)
    if codec == "h264":
        codec_string = h264_codec_string(profile, level)
    elif codec == "hevc":
        codec_string = hevc_codec_string(profile, level)
    elif codec == "av1":
        codec_string = av1_codec_string(profile, level, depth)
    elif codec == "vp9":
        codec_string = vp9_codec_string(profile, depth)
    elif codec == "vp8":
        codec_string = "vp8"
    else:
        codec_string = None
    return VideoFacts(
        index=_int(stream.get("index")) or 0,
        codec=codec,
        profile=profile,
        level=level,
        pix_fmt=str(stream.get("pix_fmt") or "").lower(),
        bit_depth=depth,
        width=_int(stream.get("width")),
        height=_int(stream.get("height")),
        frame_rate=_frame_rate(stream),
        dynamic_range=dynamic_range,
        dv_profile=dv_profile,
        codec_string=codec_string,
    )


def parse_ffprobe(data: dict) -> MediaFacts:
    """Turn ffprobe ``-show_format -show_streams`` JSON into MediaFacts."""
    if not isinstance(data, dict):
        raise MediaProbeError("not_media", "ffprobe returned no metadata")
    streams = [s for s in data.get("streams") or [] if isinstance(s, dict)]
    fmt = data.get("format") if isinstance(data.get("format"), dict) else {}
    if not streams:
        raise MediaProbeError("not_media", "Source has no media streams")

    video_stream = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video" and not _disposition(s, "attached_pic")
        ),
        None,
    )
    audio: list[AudioFacts] = []
    subtitles: list[SubtitleFacts] = []
    for stream in streams:
        kind = stream.get("codec_type")
        tags = _tags(stream)
        language = str(tags.get("language") or "").lower()
        title = str(tags.get("title") or "")
        if kind == "audio":
            codec = str(stream.get("codec_name") or "").lower()
            profile = str(stream.get("profile") or "")
            audio.append(
                AudioFacts(
                    index=_int(stream.get("index")) or 0,
                    codec=codec,
                    profile=profile,
                    channels=_int(stream.get("channels")),
                    language=language,
                    title=title,
                    default=_disposition(stream, "default"),
                    codec_string=audio_codec_string(codec, profile),
                )
            )
        elif kind == "subtitle":
            codec = str(stream.get("codec_name") or "").lower()
            subtitles.append(
                SubtitleFacts(
                    index=_int(stream.get("index")) or 0,
                    codec=codec,
                    language=language,
                    title=title,
                    forced=_disposition(stream, "forced") or "forced" in title.lower(),
                    default=_disposition(stream, "default"),
                    text=codec in _TEXT_SUBTITLE_CODECS,
                )
            )

    duration = _float(fmt.get("duration"))
    size = _int(fmt.get("size"))
    return MediaFacts(
        container=_container(str(fmt.get("format_name") or "")),
        duration=duration if duration and duration > 0 else None,
        bitrate=_int(fmt.get("bit_rate")),
        size=size if size and size > 0 else None,
        start_time=max(0.0, _float(fmt.get("start_time")) or 0.0),
        video=_video_facts(video_stream) if video_stream else None,
        audio=audio,
        subtitles=subtitles,
    )


def classify_ffprobe_error(stderr: str) -> str:
    text = stderr.lower()
    if re.search(r"server returned (401|403)", text) or "4xx client error" in text:
        return "expired"
    if "server returned 404" in text:
        return "gone"
    if "5xx server error" in text or re.search(r"server returned 5\d\d", text):
        return "network"
    if (
        "connection refused" in text
        or "connection timed out" in text
        or "end of file" in text
        or "i/o error" in text
    ):
        return "network"
    if "invalid data found" in text or "could not find codec" in text:
        return "not_media"
    return "probe_failed"


class MediaInspector:
    """Run bounded ffprobe inspections with a small TTL cache."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 8.0,
        max_concurrent: int = 4,
        cache_size: int = 512,
        ttl_seconds: float = 6 * 3600,
        failure_ttl_seconds: float = 60.0,
        probesize: int = 5_000_000,
    ) -> None:
        self.timeout_seconds = max(1.0, timeout_seconds)
        self.cache_size = max(16, cache_size)
        self.ttl_seconds = ttl_seconds
        self.failure_ttl_seconds = failure_ttl_seconds
        self.probesize = probesize
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))
        self._cache: "OrderedDict[str, tuple[float, MediaFacts | MediaProbeError]]" = (
            OrderedDict()
        )
        self._inflight: dict[str, asyncio.Future] = {}

    def cached(self, source_url: str) -> Optional[MediaFacts]:
        entry = self._cache.get(source_url)
        if entry and entry[0] > time.monotonic() and isinstance(entry[1], MediaFacts):
            return entry[1]
        return None

    def remember(self, source_url: str, facts: MediaFacts) -> None:
        """Store facts learned elsewhere (e.g. a remux session's own probe)."""
        self._remember(source_url, facts)

    def _remember(self, source_url: str, value: "MediaFacts | MediaProbeError") -> None:
        ttl = (
            self.ttl_seconds
            if isinstance(value, MediaFacts)
            else self.failure_ttl_seconds
        )
        self._cache[source_url] = (time.monotonic() + ttl, value)
        self._cache.move_to_end(source_url)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)

    async def inspect(
        self, source_url: str, internal_proxy_url: str, *, proxy_token: str
    ) -> MediaFacts:
        entry = self._cache.get(source_url)
        if entry and entry[0] > time.monotonic():
            self._cache.move_to_end(source_url)
            if isinstance(entry[1], MediaProbeError):
                raise entry[1]
            return entry[1]
        pending = self._inflight.get(source_url)
        if pending is not None:
            return await asyncio.shield(pending)

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[source_url] = future
        try:
            facts = await self._probe(internal_proxy_url, proxy_token)
        except MediaProbeError as exc:
            if not isinstance(exc, MediaProbeTimeout):
                self._remember(source_url, exc)
            future.set_exception(exc)
            future.exception()  # mark retrieved for waiters that never come
            raise
        except BaseException as exc:
            future.cancel()
            raise exc
        else:
            self._remember(source_url, facts)
            future.set_result(facts)
            return facts
        finally:
            self._inflight.pop(source_url, None)

    def command(self, internal_proxy_url: str, proxy_token: str) -> list[str]:
        return [
            "ffprobe",
            "-v",
            "error",
            "-probesize",
            str(self.probesize),
            "-analyzeduration",
            str(self.probesize),
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            "-headers",
            f"X-Curatio-Remux-Token: {proxy_token}\r\n",
            *SAFE_INPUT_ARGS,
            internal_proxy_url,
        ]

    async def _probe(self, internal_proxy_url: str, proxy_token: str) -> MediaFacts:
        async with self._semaphore:
            proc: Optional[asyncio.subprocess.Process] = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self.command(internal_proxy_url, proxy_token),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout_seconds
                )
            except asyncio.TimeoutError as exc:
                if proc and proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                raise MediaProbeTimeout() from exc
            except OSError as exc:
                raise MediaProbeError("unavailable", "ffprobe is unavailable") from exc
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            reason = classify_ffprobe_error(detail)
            logger.info("media_probe_failed reason={} detail={}", reason, detail[-300:])
            raise MediaProbeError(reason, "Source inspection failed")
        try:
            data = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MediaProbeError("not_media", "ffprobe returned invalid JSON") from exc
        return parse_ffprobe(data)


media_inspector = MediaInspector()
