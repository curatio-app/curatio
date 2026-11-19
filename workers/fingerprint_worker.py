import hashlib
import json
import math
import queue
import re
import subprocess
import threading
import time
from collections import OrderedDict
from datetime import timedelta
from urllib.parse import urlsplit
from functools import lru_cache

import numpy as np
from loguru import logger
from sqlalchemy.exc import IntegrityError

from app.time_utils import utc_now  # noqa: E402
from app.segment_policy import season_segment_enabled
from app.config import settings
from app.remux import loopback_proxy_input
from app.database import get_db
from app.models import (
    AdminSetting,
    EpisodeFingerprint,
    SegmentMarker,
    SeasonLearningState,
    SeasonSegmentReference,
)

HEAD_WINDOW = 300
EXTENDED_HEAD_WINDOW = 600
TAIL_WINDOW = 600
MIN_EPISODES_TO_LEARN = 2
MAX_LEARNING_EPISODES = 6
DETECTOR_VERSION = "hybrid-v8"
FINGERPRINT_VERSION = "chromaprint-v2"
VISUAL_FEATURE_VERSION = "credits-visual-v3"
MARKER_FINGERPRINT_VERSION = f"{FINGERPRINT_VERSION}:{DETECTOR_VERSION}"
CHROMAPRINT_SAMPLE_DURATION = 4096.0 / 11025.0 / 3.0
CHROMAPRINT_HASH_WINDOW_DURATION = 2.6

MAX_POINT_BIT_DIFF = 6
# Credits are matched exactly as tightly as intros. Chromaprint points are 32-bit, so the
# previous value of 10 called two points equal at a 31% bit-error rate — loose enough that
# shared outro music playing *under* unique per-episode dialogue still matched. The run
# then extended backwards out of the credits and into the closing scene, and that start is
# written verbatim (no boundary refinement is active), so the Next Episode prompt fired
# minutes early. An intro never hit this: a theme song is pure shared audio with a hard cut.
MAX_CREDITS_POINT_BIT_DIFF = 6
MAX_TIME_SKIP_SEC = 3.5
MIN_MATCH_DENSITY = 0.60
SINGLE_INTRO_MIN_MATCH_DENSITY = 0.85
CANDIDATE_BOUNDARY_TOLERANCE_SEC = 5.0
INVERTED_INDEX_SHIFT = 2

# Robust shift discovery (Track 1a). The legacy ±INVERTED_INDEX_SHIFT integer probe
# misses alignments where points differ in high bits (large integer delta but small
# Hamming distance) and a coarse brute-force scan steps over the single-shift correlation
# spike that a repeated intro/credits produces. Instead we anchor on exact point-value
# matches and let each anchor vote for its alignment offset (b_index - a_index); the true
# shift wins the histogram, and the top bins are verified Hamming-tolerantly. O(N) and
# bounded by SHIFT_SEARCH_WINDOW_SEC (intros sit near offset 0, credits near the tail end).
ENABLE_ROBUST_SHIFTS = True
SHIFT_SEARCH_WINDOW_SEC = 300.0
SHIFT_MIN_ANCHOR_MATCHES = 3
SHIFT_TOP_K = 12
SHIFT_MAX_ANCHOR_FREQ = 16  # ignore non-discriminative values repeated this often

# Quality-aware confidence (Track 1b).
CONFIDENCE_HAMMING_FULL = 2.0
CONFIDENCE_DURATION_TOLERANCE_SEC = 3.0
# Number of confirming pairs that counts as "fully supported". Capping the support
# denominator here (instead of dividing by the episode's total possible pairs) means
# fingerprinting MORE episodes in a season can never lower a previously-served episode's
# confidence: two clean confirming pairs is full support regardless of season size.
SUPPORT_SATURATION_PAIRS = 2
# Strong-evidence floor: a single near-pristine, in-spec pair is real evidence even
# without a second confirming pair. It can lift confidence to just above the serve gate
# (so the marker is served) while staying below the 0.85 reference-building bar (so a
# lone pair never seeds a cross-season reference).
SINGLE_PAIR_FLOOR_BASE = 0.45
SINGLE_PAIR_FLOOR_QUALITY = 0.30

# Cross-season reuse (Track 2).
REFERENCE_MIN_CONFIDENCE = 0.85
REFERENCE_MIN_EPISODES = 2
REFERENCE_SLICE_PAD_SEC = 1.0
CROSS_SEASON_CONFIDENCE_CAP = 0.80

# Visual/silence credits detection (Track 4). Default-off; vision-only confidence is
# deliberately below the credits serve gate (0.70) so it never serves un-fused.
ENABLE_BLACKFRAME_CREDITS = False
BLACKDETECT_MIN_DURATION = 0.10
BLACKDETECT_PIX_THRESHOLD = 0.10
SILENCEDETECT_NOISE_DB = -35
SILENCEDETECT_MIN_DURATION = 0.5
CREDITS_SNAP_MAX_DELTA_SEC = 8.0
VISION_ONLY_CONFIDENCE_CAP = 0.55

MIN_INTRO_DURATION = 20
SHORT_INTRO_FALLBACK_MIN_DURATION = 10
SHORT_INTRO_FALLBACK_MAX_AVG_HAMMING = 1.0
SHORT_INTRO_FALLBACK_MIN_CONFIDENCE = 0.5
SHORT_INTRO_FALLBACK_MAX_START_SEC = 30
MAX_INTRO_DURATION = 180
MIN_CREDITS_DURATION = 15
CREDITS_DURATION_TOLERANCE_SEC = 0.25
SHORT_CREDITS_FALLBACK_MIN_DURATION = 10
SHORT_CREDITS_FALLBACK_MAX_AVG_HAMMING = 3.0
SHORT_CREDITS_FALLBACK_MIN_CONFIDENCE = 0.5
SHORT_CREDITS_EARLY_GUARD_MAX_DURATION = 20
SHORT_CREDITS_EARLY_GUARD_MIN_START_RATIO = 0.92
MAX_CREDITS_DURATION = 180
CREDITS_TERMINAL_WINDOW_SEC = 30
CREDITS_VISUAL_MAX_START_DISTANCE_FROM_END_SEC = 600
# End-credit analysis intentionally stays small enough for the same low-powered
# server that runs playback. One 256x144 RGB frame per second preserves enough
# colour and text layout information to reject subtitles and scene changes. No
# ML model, OCR engine, GPU, or high-resolution frame buffer is used.
CREDITS_VISUAL_SAMPLE_FPS = 1
CREDITS_VISUAL_WIDTH = 256
CREDITS_VISUAL_HEIGHT = 144
CREDITS_VISUAL_MIN_RUN_SEC = 10.0
CREDITS_VISUAL_MIN_QUALIFYING_RATIO = 0.75
CREDITS_VISUAL_MIN_SCORE = 0.75
CREDITS_V2_MIN_CONFIDENCE = 0.80
CREDITS_V2_VISUAL_ONLY_MIN_CONFIDENCE = 0.86
CREDITS_V2_AUDIO_CONFIRM_WINDOW_SEC = 12.0
# Signed URLs rotate often.  This compact head sample distinguishes actual
# content from a different cut with a coincidentally similar runtime without
# restarting the old 300-second head + 600-second tail pipeline.
MEDIA_IDENTITY_SENTINEL_WINDOW_SEC = 20.0
# Absolute floor on credits start position. Even a long, high-confidence fingerprint
# match is rejected below this ratio — it is a recurring mid-episode block (outro song,
# recap, sponsor bumper), not the credits. Bounds the long/confident escape hatches so
# a mid-episode match can never seed a credits marker no matter how long or clean it is.
CREDITS_MIN_START_RATIO = 0.75
# Absolute backstop, in seconds before the episode end, on how early credits may start.
# The ratio floor above is inert on long episodes: the tail window is TAIL_WINDOW seconds,
# so the earliest start a 60-min episode can even produce is ratio 0.833 — a marker nine
# minutes early passes every ratio gate here. Expressed in absolute seconds it bites
# regardless of runtime, and bounds anything the match tolerance still lets through.
CREDITS_MAX_START_DISTANCE_FROM_END_SEC = 120
ENABLE_AUDIO_FEATURE_EXTRACTION = False
ENABLE_WAVEFORM_FALLBACK = False

SEASON_PRELEARNING_ENABLED = True
SEASON_PRELEARNING_MODE = "conservative"
SEASON_PRELEARNING_MAX_ADDITIONAL_EPISODES = 1
SEASON_PRELEARNING_MAX_QUEUED_PER_SEASON = 1
SEASON_PRELEARNING_STOP_WHEN_INTRO_READY = True
SEASON_PRELEARNING_STOP_WHEN_BOTH_READY = False
SEASON_PRELEARNING_RESOLVE_STREAMS = True
SEASON_PRELEARNING_COOLDOWN_MINUTES = 30
SEASON_PRELEARNING_REFINGERPRINT_STALE = False
SEASON_PRELEARNING_MAX_FALLBACK_EPISODE_GUESS = 8

# Intro learning only needs one nearby episode.  Credits are now per-episode
# terminal analysis, so the old two-episode lookahead/prelearning fan-out was
# all cost and no longer adds confidence.
FINGERPRINT_LOOKAHEAD_EPISODES = 1

PRIORITY_STREAM_OPEN = 1
PRIORITY_LOOKAHEAD = 2
PRIORITY_ADJACENT = 3
PRIORITY_BACKGROUND = 4
PRIORITY_CURRENT_TAIL = 3
PRIORITY_COMPANION_TAIL = 4
PRIORITY_EXTENDED_HEAD = 5

FINGERPRINT_PRESETS = {
    "CONSERVATIVE": {
        "prelearning_enabled": False,
        "prelearning_mode": "off",
        "max_additional_episodes": 0,
        "max_queued_per_season": 1,
        "cooldown_minutes": 60,
        "description": "Only fingerprint when explicitly requested",
    },
    "BALANCED": {
        "prelearning_enabled": True,
        "prelearning_mode": "conservative",
        "max_additional_episodes": 1,
        "max_queued_per_season": 1,
        "cooldown_minutes": 30,
        "description": "Fingerprint one nearby episode after playback",
    },
    "AGGRESSIVE": {
        "prelearning_enabled": True,
        "prelearning_mode": "aggressive",
        "max_additional_episodes": 8,
        "max_queued_per_season": 8,
        "cooldown_minutes": 10,
        "description": "Fingerprint broadly, maximize coverage",
    },
}


def get_fingerprint_preset_name() -> str:
    try:
        with get_db() as db:
            row = (
                db.query(AdminSetting)
                .filter(AdminSetting.key == "FINGERPRINT_PRESET")
                .first()
            )
            preset = (row.value if row else "BALANCED").upper()
    except Exception as exc:
        logger.warning("fingerprint_preset_load_failed error={}", exc)
        preset = "BALANCED"
    if preset not in FINGERPRINT_PRESETS:
        preset = "BALANCED"
    return preset


def get_fingerprint_runtime_config() -> dict:
    preset_name = get_fingerprint_preset_name()
    preset = dict(FINGERPRINT_PRESETS[preset_name])
    preset.update(
        {
            "preset": preset_name,
            "max_point_bit_diff": MAX_POINT_BIT_DIFF,
            "intro_ber_threshold": INTRO_BER_THRESHOLD,
            "credits_ber_threshold": CREDITS_BER_THRESHOLD,
            "min_intro_duration": MIN_INTRO_DURATION,
            "short_intro_fallback_min_duration": SHORT_INTRO_FALLBACK_MIN_DURATION,
            "min_credits_duration": MIN_CREDITS_DURATION,
            "short_credits_fallback_min_duration": SHORT_CREDITS_FALLBACK_MIN_DURATION,
            "max_intro_duration": MAX_INTRO_DURATION,
            "max_credits_duration": MAX_CREDITS_DURATION,
            "credits_min_start_ratio": CREDITS_MIN_START_RATIO,
            "fingerprint_version": FINGERPRINT_VERSION,
            "detector_version": DETECTOR_VERSION,
        }
    )
    return preset


WAVEFORM_FALLBACK_CONFIDENCE_CAP = 0.65
WAVEFORM_FALLBACK_SCORE_THRESHOLD = 80.0
FEATURE_SAMPLE_STEP = 1.0
SILENCE_RMS_THRESHOLD = 0.01

# Legacy constants kept only for import/backward compatibility; Chromaprint v5
# matching never uses this as a point-to-time conversion rate.
FRAME_RATE = 86.1
INTRO_BER_THRESHOLD = 0.22
CREDITS_BER_THRESHOLD = 0.25
MIN_CREDITS_SECONDS = MIN_CREDITS_DURATION
COARSE_STEP = 16
WIN_SECONDS = 30
MAX_OFFSET_SECONDS = 120
MAX_BUCKET_CANDIDATES = 2
MAX_GLOBAL_CANDIDATES = 20


def safe_float(value: object, default: float = 0.0) -> float:
    if value is None:
        return default
    s = str(value).strip().lower()
    if s in {"", "-", "nan", "inf", "-inf"}:
        return default
    try:
        parsed = float(s)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return parsed


def point_to_seconds(index: int) -> float:
    return index * CHROMAPRINT_SAMPLE_DURATION


def seconds_to_point(seconds: float) -> int:
    return int(round(seconds / CHROMAPRINT_SAMPLE_DURATION))


def _ffmpeg_input_thread_args() -> list[str]:
    """Bound decoder parallelism for background work in the web container."""
    return ["-threads", str(settings.fingerprint_ffmpeg_threads)]


# Legacy alias kept so nothing outside breaks
BER_THRESHOLD = INTRO_BER_THRESHOLD

_job_queue: queue.PriorityQueue = queue.PriorityQueue()
_job_seq = 0
_job_seq_lock = threading.Lock()
_inflight: set[tuple[str, str]] = set()
_inflight_lock = threading.Lock()
# A probe URL and the URL that actually rendered a frame can differ.  Keep only
# the newest URL while a phase is running, then replay that phase once.  The
# old implementation dropped this update because dedupe keyed only on episode
# and phase, which made a stale source look authoritative.
_superseding_sources: dict[tuple[str, str], str] = {}
_needs_companion: dict[str, set[int]] = {}
_needs_companion_lock = threading.Lock()

# Pair-match memoization (efficiency). _try_learn_season runs on every episode
# completion and recomputes EVERY episode pair from scratch, so an N-episode season
# filled in over time costs ~O(N^2) redundant matching. Cache each pair's match result
# keyed by everything that changes it (episode pair + each side's fingerprint version and
# source-url hash); a re-fingerprint changes the version/hash and therefore the key, so a
# stale entry is simply never hit again. Bounded LRU. The compute runs outside the lock.
_PAIR_MATCH_CACHE_MAXSIZE = 4096
_pair_match_cache: "OrderedDict[tuple, dict]" = OrderedDict()
_pair_match_cache_lock = threading.Lock()


def _pair_match_cache_get(key: tuple) -> dict | None:
    with _pair_match_cache_lock:
        value = _pair_match_cache.get(key)
        if value is not None:
            _pair_match_cache.move_to_end(key)
        return value


def _pair_match_cache_put(key: tuple, value: dict) -> None:
    with _pair_match_cache_lock:
        _pair_match_cache[key] = value
        _pair_match_cache.move_to_end(key)
        while len(_pair_match_cache) > _PAIR_MATCH_CACHE_MAXSIZE:
            _pair_match_cache.popitem(last=False)


def _pair_match_cache_clear_season(imdb_id: str, season: int) -> None:
    """Drop cached pair matches for a season (e.g. on reset-learning)."""
    with _pair_match_cache_lock:
        stale = [
            key for key in _pair_match_cache if key[0] == imdb_id and key[1] == season
        ]
        for key in stale:
            _pair_match_cache.pop(key, None)


def _drain_job_queue() -> None:
    while True:
        _priority, _seq, canonical_id, url, phase = _job_queue.get()
        try:
            _process_job(canonical_id, url, phase=phase, priority=_priority)
        except Exception:
            logger.exception(
                "fingerprint job failed canonical_id={} phase={} url={}",
                canonical_id,
                phase,
                _sanitize_media_url_for_log(url),
            )
        finally:
            _job_queue.task_done()


_drain_thread = threading.Thread(
    target=_drain_job_queue, daemon=True, name="fingerprint-worker"
)
_drain_thread.start()
_POPCOUNT_TABLE = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def _mean_popcount(arr: np.ndarray) -> float:
    return float(_POPCOUNT_TABLE[arr.view(np.uint8)].sum()) / len(arr)


def _decode_fpcalc_fingerprint(raw: object) -> list[int]:
    # Already a list of ints (some fpcalc builds return this directly)
    if isinstance(raw, list):
        return [int(x) & 0xFFFFFFFF for x in raw]
    raw_str = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    # Comma-separated integer list
    if "," in raw_str:
        return [int(x) & 0xFFFFFFFF for x in raw_str.split(",") if x.strip()]
    # Compressed Chromaprint base64 string — decode_fingerprint expects bytes
    decode_input = raw_str.encode("utf-8") if isinstance(raw_str, str) else raw_str
    logger.debug(
        "fpcalc fingerprint raw type={} decode_input type={} prefix={}",
        type(raw).__name__,
        type(decode_input).__name__,
        raw_str[:80],
    )
    try:
        from acoustid import chromaprint

        decoded = chromaprint.decode_fingerprint(decode_input)
        fp_values = decoded[0] if isinstance(decoded, tuple) else decoded
        return [int(x) & 0xFFFFFFFF for x in fp_values]
    except Exception as exc:
        raise RuntimeError(
            f"decode_fingerprint failed: raw type={type(raw).__name__!r} "
            f"decode_input type={type(decode_input).__name__!r} "
            f"prefix={raw_str[:80]!r}: {exc}"
        ) from exc


def _terminate_process(proc: "subprocess.Popen | None") -> None:
    """Kill and reap a subprocess if still running, then close its pipes.

    Safe to call on an already-exited process (no-op) and never raises. Used to
    guarantee ffmpeg/fpcalc children are cleaned up even when communicate() raises
    TimeoutExpired — which, on a bare Popen, does NOT kill the children.
    """
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
    except Exception:
        pass
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass


def _extract_fpcalc(url: str, start_sec: float, duration_sec: float) -> list[int]:
    logger.info("Using WAV pipe for fpcalc input extraction_mode=fpcalc_json_fallback")
    ffmpeg_cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
        "-user_agent",
        "Mozilla/5.0",
        "-ss",
        str(start_sec),
        *_ffmpeg_input_thread_args(),
        *loopback_proxy_input(url),
        "-t",
        str(duration_sec),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "11025",
        "-f",
        "wav",
        "pipe:1",
    ]
    fpcalc_cmd = ["fpcalc", "-json", "-length", str(int(duration_sec)), "-"]
    p1 = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        p2 = subprocess.Popen(
            fpcalc_cmd,
            stdin=p1.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except BaseException:
        # fpcalc failed to spawn — don't leak the ffmpeg child.
        _terminate_process(p1)
        raise

    try:
        # Release parent's copy so p2 gets EOF when p1 exits
        if p1.stdout:
            p1.stdout.close()
        # communicate(timeout=) raises TimeoutExpired WITHOUT killing the children;
        # the finally below kills+reaps both so a stalled/flaky source can't leave
        # orphaned ffmpeg/fpcalc processes (and their sockets/pipe FDs) running.
        out, err = p2.communicate(timeout=120)
        # Drain ffmpeg stderr to avoid buffer deadlock, then reap the process
        ffmpeg_err = p1.stderr.read() if p1.stderr else b""
        p1.wait()

        # Validate fpcalc first: if it succeeded and produced a fingerprint, that is
        # authoritative. ffmpeg rc=224 (EPIPE/broken pipe) is expected when fpcalc
        # exits after processing its -length seconds, so never raise on it here.
        if p2.returncode == 0:
            fingerprint: list[int] | None = None
            stdout = out.strip()
            if stdout.startswith("{"):
                payload = json.loads(stdout)
                raw = str(payload.get("fingerprint", ""))
                if raw:
                    fingerprint = _decode_fpcalc_fingerprint(raw)
            else:
                fp_line = next(
                    (
                        line
                        for line in stdout.splitlines()
                        if line.startswith("FINGERPRINT=")
                    ),
                    "",
                )
                if fp_line:
                    fingerprint = _decode_fpcalc_fingerprint(
                        fp_line.split("=", 1)[1].strip()
                    )

            if fingerprint:
                if p1.returncode != 0:
                    logger.debug(
                        f"ffmpeg exited rc={p1.returncode} (broken pipe — fpcalc finished early, fingerprint OK)"
                    )
                return fingerprint

        # fpcalc failed or produced no usable fingerprint — surface error details.
        # Check ffmpeg first: its failure is often the root cause of bad input.
        if p1.returncode != 0:
            raise RuntimeError(
                f"ffmpeg failed (rc={p1.returncode}): {ffmpeg_err.decode(errors='replace').strip()}"
            )
        if p2.returncode != 0:
            raise RuntimeError(f"fpcalc failed: {err.strip()}")
        raise RuntimeError("FINGERPRINT payload missing")
    finally:
        _terminate_process(p2)
        _terminate_process(p1)


@lru_cache(maxsize=1)
def _ffmpeg_supports_chromaprint_muxer() -> bool:
    try:
        out = subprocess.check_output(
            ["ffmpeg", "-hide_banner", "-muxers"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        )
    except Exception as exc:
        logger.warning("chromaprint_muxer_probe_failed reason={}", exc)
        return False
    supported = "chromaprint" in out
    logger.info("chromaprint_muxer_supported={}", supported)
    return supported


def _extract_ffmpeg_chromaprint_raw(
    url: str, start_sec: float, duration_sec: float
) -> list[int]:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
        "-user_agent",
        "Mozilla/5.0",
        "-ss",
        str(start_sec),
        *_ffmpeg_input_thread_args(),
        *loopback_proxy_input(url),
        "-t",
        str(duration_sec),
        "-vn",
        "-ac",
        "2",
        "-f",
        "chromaprint",
        "-fp_format",
        "raw",
        "pipe:1",
    ]
    out = subprocess.check_output(cmd, stderr=subprocess.PIPE, timeout=120)
    usable = len(out) - (len(out) % 4)
    if usable <= 0:
        return []
    if usable != len(out):
        logger.warning(
            "ffmpeg_chromaprint_raw_truncated_bytes total_bytes={} usable_bytes={}",
            len(out),
            usable,
        )
    return [
        int.from_bytes(out[i : i + 4], "little", signed=False) & 0xFFFFFFFF
        for i in range(0, usable, 4)
    ]


def _log_extraction_timing(
    label: str, points: list[int], requested_duration: float, extraction_mode: str
) -> None:
    inferred_duration = (
        point_to_seconds(len(points)) + CHROMAPRINT_HASH_WINDOW_DURATION
        if points
        else 0.0
    )
    log_fn = (
        logger.warning
        if abs(inferred_duration - requested_duration) > 5
        else logger.info
    )
    log_fn(
        "fingerprint_extracted label={} extraction_mode={} point_count={} inferred_duration={:.2f} requested_duration={:.2f} chromaprint_sample_duration={:.6f} detector_version={} fingerprint_version={}",
        label,
        extraction_mode,
        len(points),
        inferred_duration,
        requested_duration,
        CHROMAPRINT_SAMPLE_DURATION,
        DETECTOR_VERSION,
        FINGERPRINT_VERSION,
    )


def _extract_chromaprint(
    url: str, start_sec: float, duration_sec: float, label: str
) -> tuple[list[int], str]:
    if _ffmpeg_supports_chromaprint_muxer():
        try:
            points = _extract_ffmpeg_chromaprint_raw(url, start_sec, duration_sec)
            if points:
                mode = "ffmpeg_chromaprint_raw"
                _log_extraction_timing(label, points, duration_sec, mode)
                return points, mode
            logger.warning("ffmpeg_chromaprint_raw_empty label={}", label)
        except Exception as exc:
            logger.warning(
                "ffmpeg_chromaprint_raw_failed label={} reason={}", label, exc
            )
    points = _extract_fpcalc(url, start_sec, duration_sec)
    mode = "fpcalc_json_fallback"
    _log_extraction_timing(label, points, duration_sec, mode)
    return [p & 0xFFFFFFFF for p in points], mode


def _visual_frame_score(
    frame: np.ndarray, previous_mask: np.ndarray | None
) -> tuple[float, np.ndarray]:
    """Return a conservative credits-text likelihood for one small video frame.

    The classifier intentionally favors precision.  It looks for a comparatively uniform
    background, a bounded amount of high-contrast detail, text-like edge density, and
    temporal persistence (including small vertical shifts for scrolling titles).  It is
    used across a sustained terminal run, followed by a bounded high-rate boundary pass.
    """
    if frame.ndim == 3:
        rgb = frame.astype(np.int16)
        pixels = (
            0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]
        ).astype(np.int16)
        saturation = float(np.mean(np.max(rgb, axis=2) - np.min(rgb, axis=2)))
    else:
        pixels = frame.astype(np.int16)
        saturation = 0.0
    median = int(np.median(pixels))
    p10, p90 = np.percentile(pixels, (10, 90))
    spread = float(p90 - p10)
    contrast_mask = np.abs(pixels - median) >= 58
    contrast_ratio = float(np.mean(contrast_mask))
    dx = np.abs(np.diff(pixels, axis=1)) >= 45
    dy = np.abs(np.diff(pixels, axis=0)) >= 45
    edge_ratio = (float(np.mean(dx)) + float(np.mean(dy))) / 2.0

    uniform_score = max(0.0, min(1.0, (170.0 - spread) / 110.0))
    low_saturation_score = max(0.0, min(1.0, (75.0 - saturation) / 55.0))
    contrast_score = (
        1.0
        if 0.008 <= contrast_ratio <= 0.22
        else max(
            0.0,
            1.0 - min(abs(contrast_ratio - 0.008), abs(contrast_ratio - 0.22)) / 0.15,
        )
    )
    edge_score = (
        1.0
        if 0.01 <= edge_ratio <= 0.28
        else max(0.0, 1.0 - min(abs(edge_ratio - 0.01), abs(edge_ratio - 0.28)) / 0.18)
    )
    row_density = np.mean(contrast_mask, axis=1)
    text_rows = np.logical_and(row_density >= 0.015, row_density <= 0.45)
    row_score = min(1.0, float(np.mean(text_rows)) / 0.32)
    thirds = np.array_split(np.arange(text_rows.size), 3)
    third_scores = [float(np.mean(text_rows[part])) for part in thirds if len(part)]
    third_contrast = [
        float(np.mean(contrast_mask[part, :])) for part in thirds if len(part)
    ]
    spatial_score = min(1.0, sum(value >= 0.04 for value in third_scores) / 2.0)
    coherence_score = 0.0
    if previous_mask is not None and previous_mask.shape == contrast_mask.shape:
        union_floor = max(1, int(contrast_mask.sum()))
        for shift in range(-4, 5):
            shifted = np.roll(previous_mask, shift, axis=0)
            intersection = int(np.logical_and(contrast_mask, shifted).sum())
            union = int(np.logical_or(contrast_mask, shifted).sum())
            if union:
                coherence_score = max(
                    coherence_score, intersection / max(union, union_floor)
                )
        coherence_score = min(1.0, coherence_score / 0.30)
    card_score = (
        0.24 * uniform_score
        + 0.16 * low_saturation_score
        + 0.20 * contrast_score
        + 0.16 * edge_score
        + 0.14 * row_score
        + 0.10 * coherence_score
    )
    overlay_score = (
        0.26 * row_score
        + 0.22 * spatial_score
        + 0.20 * edge_score
        + 0.18 * coherence_score
        + 0.14 * contrast_score
    )
    score = max(card_score, overlay_score)
    # A small lower-third text band is usually dialogue subtitles, not credits.
    # Full-screen and center-weighted credit layouts remain unaffected.
    lower_third_only = len(third_scores) == 3 and (
        (third_scores[2] >= 0.04 and max(third_scores[:2]) < 0.02)
        or (third_contrast[2] >= 0.005 and max(third_contrast[:2]) < 0.002)
    )
    if lower_third_only:
        score *= 0.65
    return round(score, 3), contrast_mask


def _visual_credit_runs(
    scores: list[dict], sample_fps: float = CREDITS_VISUAL_SAMPLE_FPS
) -> list[dict]:
    """Collapse frame scores into persistent, high-precision credit-like runs."""
    if not scores:
        return []
    frame_step = 1.0 / sample_fps
    runs: list[list[dict]] = []
    current: list[dict] = []
    last_qualifying_t: float | None = None
    for sample in scores:
        qualifies = sample["score"] >= CREDITS_VISUAL_MIN_SCORE
        if qualifies and (
            not current
            or last_qualifying_t is None
            or sample["t"] - last_qualifying_t <= frame_step * 3
        ):
            current.append(sample)
            last_qualifying_t = sample["t"]
        elif qualifies:
            if current:
                runs.append(current)
            current = [sample]
            last_qualifying_t = sample["t"]
        elif (
            current
            and last_qualifying_t is not None
            and sample["t"] - last_qualifying_t <= frame_step * 2
        ):
            current.append(sample)
        elif current:
            runs.append(current)
            current = []
            last_qualifying_t = None
    if current:
        runs.append(current)

    accepted: list[dict] = []
    for run in runs:
        duration = run[-1]["t"] - run[0]["t"] + frame_step
        qualifying = [s for s in run if s["score"] >= CREDITS_VISUAL_MIN_SCORE]
        ratio = len(qualifying) / len(run)
        if (
            duration < CREDITS_VISUAL_MIN_RUN_SEC
            or ratio < CREDITS_VISUAL_MIN_QUALIFYING_RATIO
        ):
            continue
        accepted.append(
            {
                "start_sec": round(run[0]["t"], 2),
                "end_sec": round(run[-1]["t"] + frame_step, 2),
                "score": round(
                    sum(s["score"] for s in qualifying) / len(qualifying), 3
                ),
                "qualifying_ratio": round(ratio, 3),
            }
        )
    return accepted


def _extract_tail_visual_runs(
    url: str, start_sec: float, duration_sec: float
) -> list[dict]:
    frame_size = CREDITS_VISUAL_WIDTH * CREDITS_VISUAL_HEIGHT * 3
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
        "-user_agent",
        "Mozilla/5.0",
        "-ss",
        str(start_sec),
        *_ffmpeg_input_thread_args(),
        *loopback_proxy_input(url),
        "-t",
        str(duration_sec),
        "-an",
        "-vf",
        f"fps={CREDITS_VISUAL_SAMPLE_FPS},scale={CREDITS_VISUAL_WIDTH}:{CREDITS_VISUAL_HEIGHT},format=rgb24",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    try:
        raw = subprocess.check_output(cmd, stderr=subprocess.PIPE, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("tail visual extraction timed out") from exc
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "tail_visual_extraction_failed url={} returncode={}",
            _sanitize_media_url_for_log(url),
            exc.returncode,
        )
        raise RuntimeError("tail visual extraction failed") from exc
    usable = len(raw) - (len(raw) % frame_size)
    previous_mask: np.ndarray | None = None
    scores: list[dict] = []
    for index, offset in enumerate(range(0, usable, frame_size)):
        frame = np.frombuffer(
            raw[offset : offset + frame_size], dtype=np.uint8
        ).reshape(CREDITS_VISUAL_HEIGHT, CREDITS_VISUAL_WIDTH, 3)
        score, previous_mask = _visual_frame_score(frame, previous_mask)
        scores.append({"t": index / CREDITS_VISUAL_SAMPLE_FPS, "score": score})
    runs = _visual_credit_runs(scores)
    logger.info(
        "tail_visual_extracted start_sec={:.2f} duration_sec={:.2f} frames={} qualifying_frames={} runs={} visual_version={}",
        start_sec,
        duration_sec,
        usable // frame_size,
        sum(1 for sample in scores if sample["score"] >= CREDITS_VISUAL_MIN_SCORE),
        len(runs),
        VISUAL_FEATURE_VERSION,
    )
    return runs


def _fuse_credits_with_visual(
    audio_segment: dict | None,
    visual_runs: list[dict],
    episode_duration_sec: float,
    tail_duration_sec: float,
) -> tuple[dict | None, str | None]:
    if not audio_segment or episode_duration_sec <= 0 or not visual_runs:
        return None, "missing_audio_or_visual_evidence"
    tail_base = max(0.0, episode_duration_sec - tail_duration_sec)
    audio_start = safe_float(audio_segment.get("start_sec"))
    audio_end = safe_float(audio_segment.get("end_sec"))
    compatible: list[tuple[float, float, dict]] = []
    for run in visual_runs:
        start = tail_base + safe_float(run.get("start_sec"))
        end = tail_base + safe_float(run.get("end_sec"))
        if start < audio_start - 2.0 or start > audio_end + 2.0:
            continue
        if (
            episode_duration_sec - start
            > CREDITS_VISUAL_MAX_START_DISTANCE_FROM_END_SEC
        ):
            continue
        compatible.append((start, end, run))
    if not compatible:
        return None, "no_visual_onset_inside_audio_range"
    visual_start, visual_end, chosen = sorted(compatible, key=lambda item: item[0])[0]
    fused_end = min(episode_duration_sec, max(audio_end, visual_end))
    if fused_end <= visual_start:
        return None, "invalid_fused_credits_range"
    confidence = min(
        safe_float(audio_segment.get("confidence")), safe_float(chosen.get("score"))
    )
    return (
        {
            **audio_segment,
            "audio_start_sec": round(audio_start, 2),
            "start_sec": round(visual_start, 2),
            "end_sec": round(fused_end, 2),
            "duration_sec": round(fused_end - visual_start, 2),
            "confidence": round(confidence, 3),
            "source": "audio+visual",
            "visual_evidence": {
                "version": VISUAL_FEATURE_VERSION,
                "score": safe_float(chosen.get("score")),
                "qualifying_ratio": safe_float(chosen.get("qualifying_ratio")),
                "onset_sec": round(visual_start, 2),
            },
        },
        None,
    )


def _visual_first_credits_candidate(
    visual_runs: list[dict], episode_duration_sec: float, tail_duration_sec: float
) -> tuple[dict | None, str | None]:
    """Pick a terminal credits candidate without comparing episode outro audio.

    End credits are frequently episode-specific (names, music, previews, and
    post-credit tags), so a repeated-tail-audio requirement both burns CPU and
    produces early false positives.  This stage only considers persistent
    text-like visual runs in the final allowed window.  It deliberately emits a
    *candidate*, not a marker; `_confirm_visual_credits_candidate` adds a short
    boundary check before the learner can save it.
    """
    if episode_duration_sec <= 0 or not visual_runs:
        return None, "no_terminal_visual_credits_run"
    tail_base = max(0.0, episode_duration_sec - tail_duration_sec)
    candidates: list[dict] = []
    for run in visual_runs:
        local_start = safe_float(run.get("start_sec"))
        local_end = safe_float(run.get("end_sec"))
        visual_score = safe_float(run.get("score"))
        qualifying_ratio = safe_float(run.get("qualifying_ratio"))
        start = tail_base + local_start
        end = tail_base + local_end
        terminal_continuation = any(
            safe_float(later.get("start_sec")) >= local_end
            and safe_float(later.get("start_sec")) - local_end <= 120.0
            and episode_duration_sec - (tail_base + safe_float(later.get("end_sec")))
            <= 90.0
            for later in visual_runs
        )
        if (
            end <= start
            or end - start < CREDITS_VISUAL_MIN_RUN_SEC
            or visual_score < CREDITS_VISUAL_MIN_SCORE
            or qualifying_ratio < CREDITS_VISUAL_MIN_QUALIFYING_RATIO
            or (episode_duration_sec - end > 90.0 and not terminal_continuation)
        ):
            continue
        start_distance = episode_duration_sec - start
        if (
            start_distance < 0
            or start_distance > CREDITS_VISUAL_MAX_START_DISTANCE_FROM_END_SEC
        ):
            continue
        # Strongly prefer a run nearest the end, but preserve the earliest
        # qualifying terminal run so a real scrolling block is not delayed to
        # a later end-card.
        terminal_score = max(
            0.0,
            min(
                1.0,
                1.0 - start_distance / CREDITS_VISUAL_MAX_START_DISTANCE_FROM_END_SEC,
            ),
        )
        candidates.append(
            {
                "start_sec": round(start, 2),
                "end_sec": round(episode_duration_sec, 2),
                "duration_sec": round(episode_duration_sec - start, 2),
                "visual_score": round(visual_score, 3),
                "qualifying_ratio": round(qualifying_ratio, 3),
                "terminal_score": round(terminal_score, 3),
            }
        )
    if not candidates:
        return None, "no_terminal_visual_credits_run"
    candidates.sort(key=lambda item: (item["start_sec"], -item["visual_score"]))
    return candidates[0], None


def _refine_visual_credits_start(url: str, coarse_start_sec: float) -> float | None:
    """Refine a coarse credits onset at 4 FPS in a bounded neighborhood."""
    sample_fps = 4.0
    width, height = 192, 108
    probe_start = max(0.0, coarse_start_sec - 4.0)
    probe_duration = 16.0
    frame_size = width * height * 3
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
        "-user_agent",
        "Mozilla/5.0",
        "-ss",
        str(probe_start),
        *_ffmpeg_input_thread_args(),
        *loopback_proxy_input(url),
        "-t",
        str(probe_duration),
        "-an",
        "-vf",
        f"fps={sample_fps},scale={width}:{height},format=rgb24",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    try:
        raw = subprocess.check_output(cmd, stderr=subprocess.PIPE, timeout=30)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return None
    usable = len(raw) - (len(raw) % frame_size)
    previous_mask: np.ndarray | None = None
    scores: list[dict] = []
    for index, offset in enumerate(range(0, usable, frame_size)):
        frame = np.frombuffer(
            raw[offset : offset + frame_size], dtype=np.uint8
        ).reshape(height, width, 3)
        score, previous_mask = _visual_frame_score(frame, previous_mask)
        scores.append({"t": index / sample_fps, "score": score})
    compatible = [
        run
        for run in _visual_credit_runs(scores, sample_fps=sample_fps)
        if abs(probe_start + safe_float(run.get("start_sec")) - coarse_start_sec) <= 4.0
    ]
    if not compatible:
        return None
    first = min(compatible, key=lambda run: safe_float(run.get("start_sec")))
    return round(probe_start + safe_float(first.get("start_sec")), 2)


def _confirm_visual_credits_candidate(url: str, candidate: dict) -> dict | None:
    """Add a bounded black/silence transition check to a visual candidate.

    The confirmation never creates a candidate and only examines twelve seconds
    around the proposed boundary.  This is the low-power replacement for the
    former 240-second audio Chromaprint tail pass.
    """
    start_sec = safe_float(candidate.get("start_sec"))
    if start_sec < 0:
        return None
    probe_start = max(0.0, start_sec - CREDITS_V2_AUDIO_CONFIRM_WINDOW_SEC / 2.0)
    evidence = _detect_black_and_silence(
        url,
        probe_start,
        CREDITS_V2_AUDIO_CONFIRM_WINDOW_SEC,
        force=True,
    )
    anchors = _credits_vision_anchors(
        evidence.get("black", []), evidence.get("silence", [])
    )
    has_boundary_anchor = any(
        abs(anchor - start_sec) <= CREDITS_SNAP_MAX_DELTA_SEC for anchor in anchors
    )
    visual_score = safe_float(candidate.get("visual_score"))
    terminal_score = safe_float(candidate.get("terminal_score"))
    confidence = min(
        1.0,
        0.70 * visual_score
        + 0.20 * terminal_score
        + (0.15 if has_boundary_anchor else 0.0),
    )
    if confidence < CREDITS_V2_MIN_CONFIDENCE:
        return None
    # A particularly strong visual run in the final minute is sufficient even
    # when credits music begins without a silence/black cut.  Anything weaker
    # needs the inexpensive local transition confirmation.
    if not has_boundary_anchor and confidence < CREDITS_V2_VISUAL_ONLY_MIN_CONFIDENCE:
        return None
    refined_start = _refine_visual_credits_start(url, start_sec)
    if refined_start is not None:
        start_sec = refined_start
    source = "visual-v3+boundary" if has_boundary_anchor else "visual-v3"
    return {
        "start_sec": round(start_sec, 2),
        "end_sec": candidate["end_sec"],
        "duration_sec": round(candidate["end_sec"] - start_sec, 2),
        "confidence": round(confidence, 3),
        "support_count": 1,
        "source": source,
        "visual_evidence": {
            "version": VISUAL_FEATURE_VERSION,
            "score": visual_score,
            "qualifying_ratio": safe_float(candidate.get("qualifying_ratio")),
            "onset_sec": round(start_sec, 2),
            "terminal_score": terminal_score,
            "boundary_confirmed": has_boundary_anchor,
        },
    }


def _stored_visual_credits_candidate(
    fp: dict, episode_duration_sec: float
) -> tuple[dict | None, str | None, dict]:
    stored = fp.get("credits_v2") if isinstance(fp, dict) else None
    if not isinstance(stored, dict) or stored.get("version") != VISUAL_FEATURE_VERSION:
        return None, "credits_v2_not_analyzed", {"version": VISUAL_FEATURE_VERSION}
    candidate = stored.get("candidate")
    if not isinstance(candidate, dict):
        return (
            None,
            str(stored.get("reason") or "no_terminal_visual_credits_run"),
            {
                "version": VISUAL_FEATURE_VERSION,
                "reason": stored.get("reason"),
            },
        )
    start = safe_float(candidate.get("start_sec"))
    end = safe_float(candidate.get("end_sec"))
    confidence = safe_float(candidate.get("confidence"))
    if (
        episode_duration_sec <= 0
        or start < 0
        or end <= start
        or abs(episode_duration_sec - end) > CREDITS_TERMINAL_WINDOW_SEC
        or episode_duration_sec - start > CREDITS_VISUAL_MAX_START_DISTANCE_FROM_END_SEC
        or confidence < CREDITS_V2_MIN_CONFIDENCE
    ):
        return (
            None,
            "invalid_terminal_visual_credits_candidate",
            {"version": VISUAL_FEATURE_VERSION},
        )
    diag = {
        "version": VISUAL_FEATURE_VERSION,
        "visual_score": (candidate.get("visual_evidence") or {}).get("score"),
        "visual_onset_sec": start,
        "boundary_confirmed": (candidate.get("visual_evidence") or {}).get(
            "boundary_confirmed"
        ),
        "confidence": confidence,
    }
    return candidate, None, diag


@lru_cache(maxsize=1)
def detect_fpcalc_format() -> str:
    return "unknown"


# ---------------------------------------------------------------------------
# Matcher helpers
# ---------------------------------------------------------------------------


def _build_inverted_index(points: list[int]) -> dict[int, int]:
    """Map normalized Chromaprint point value to the last index where it appears."""
    return {int(point) & 0xFFFFFFFF: index for index, point in enumerate(points)}


def _count_bits(value: int) -> int:
    return int((value & 0xFFFFFFFF).bit_count())


def _discover_candidate_shifts(
    fp_a: list[int], fp_b: list[int], inverted_index_shift: int = INVERTED_INDEX_SHIFT
) -> set[int]:
    index_a = _build_inverted_index(fp_a)
    index_b = _build_inverted_index(fp_b)
    shifts: set[int] = set()
    for point_value, a_index in index_a.items():
        for delta in range(-inverted_index_shift, inverted_index_shift + 1):
            # delta may be negative; masking keeps the value in uint32 space.
            # Do not remove this mask.
            modified_value = (point_value + delta) & 0xFFFFFFFF
            b_index = index_b.get(modified_value)
            if b_index is not None:
                shifts.add(b_index - a_index)
    return shifts


def _count_shift_matches(
    arr_a: np.ndarray, arr_b: np.ndarray, shift: int, max_point_bit_diff: int
) -> int:
    """Count aligned points within max_point_bit_diff Hamming for a single shift."""
    na, nb = len(arr_a), len(arr_b)
    a_start = max(0, -shift)
    a_end = min(na, nb - shift)
    if a_end - a_start <= 0:
        return 0
    sa = arr_a[a_start:a_end]
    sb = arr_b[a_start + shift : a_end + shift]
    xor = (sa ^ sb).astype(np.uint32)
    # Per-element popcount: view uint32 as 4 bytes, table-lookup, sum across the 4 bytes.
    pop = _POPCOUNT_TABLE[xor.view(np.uint8)].reshape(-1, 4).sum(axis=1)
    return int((pop <= max_point_bit_diff).sum())


def _build_inverted_multi_index(
    points: list[int], max_freq: int = SHIFT_MAX_ANCHOR_FREQ
) -> dict[int, list[int]]:
    """Map each point value to ALL indices where it appears (anchors for shift voting).

    Values that repeat more than ``max_freq`` times are dropped — they are not
    discriminative and would explode the vote loop to O(N^2)."""
    index: dict[int, list[int]] = {}
    for idx, point in enumerate(points):
        index.setdefault(int(point) & 0xFFFFFFFF, []).append(idx)
    return {value: idxs for value, idxs in index.items() if len(idxs) <= max_freq}


def _discover_candidate_shifts_robust(
    fp_a: list[int],
    fp_b: list[int],
    max_point_bit_diff: int,
    window_points: int,
    min_anchor_matches: int = SHIFT_MIN_ANCHOR_MATCHES,
    top_k: int = SHIFT_TOP_K,
) -> set[int]:
    """Anchor on exact point-value matches, vote for alignment offsets, verify the top bins.

    Repeated intro/credits audio produces many bit-identical (or near-identical) Chromaprint
    points that all vote for the same shift, so the true offset wins the histogram even when
    only a fraction of points match exactly. The top bins (±1) are then verified with the
    Hamming-tolerant point count, and the survivors feed the existing per-shift run loop.
    """
    if not fp_a or not fp_b:
        return set()
    index_a = _build_inverted_multi_index(fp_a)
    votes: dict[int, int] = {}
    for j, point in enumerate(fp_b):
        anchors = index_a.get(int(point) & 0xFFFFFFFF)
        if not anchors:
            continue
        for i in anchors:
            shift = j - i
            if -window_points <= shift <= window_points:
                votes[shift] = votes.get(shift, 0) + 1
    candidate_shifts: set[int] = set()
    if votes:
        for shift, _count in sorted(votes.items(), key=lambda kv: kv[1], reverse=True)[
            :top_k
        ]:
            candidate_shifts.update((shift - 1, shift, shift + 1))
    # Always include the legacy integer-delta seeds as a cheap safety net.
    candidate_shifts.update(_discover_candidate_shifts(fp_a, fp_b))
    arr_a = np.array(fp_a, dtype=np.uint32)
    arr_b = np.array(fp_b, dtype=np.uint32)
    survivors: set[int] = set()
    for shift in candidate_shifts:
        if abs(shift) > window_points:
            continue
        if (
            _count_shift_matches(arr_a, arr_b, shift, max_point_bit_diff)
            >= min_anchor_matches
        ):
            survivors.add(shift)
    return survivors


def _find_chromaprint_ranges(
    fp_a: list[int],
    fp_b: list[int],
    label: str,
    min_duration_sec: float,
    max_duration_sec: float,
    max_point_bit_diff: int = MAX_POINT_BIT_DIFF,
    max_time_skip_sec: float = MAX_TIME_SKIP_SEC,
    inverted_index_shift: int = INVERTED_INDEX_SHIFT,
    max_ranges: int = 10,
    segment_type: str = "intro",
    pair_key: str = "",
    full_shift_search: bool = False,
) -> list[dict]:
    if not fp_a or not fp_b:
        logger.info("{} empty fingerprint, skipping pair", label)
        return []
    min_points = seconds_to_point(min_duration_sec)
    if len(fp_a) < min_points or len(fp_b) < min_points:
        logger.info("{} fingerprint too short for min duration, skipping pair", label)
        return []

    a = [int(point) & 0xFFFFFFFF for point in fp_a]
    b = [int(point) & 0xFFFFFFFF for point in fp_b]
    arr_a = np.array(a, dtype=np.uint32)
    arr_b = np.array(b, dtype=np.uint32)
    if ENABLE_ROBUST_SHIFTS:
        # Episode pairs align at small offsets (intros near 0, credits near the tail end),
        # so the 300s cap keeps the scan cheap. Reference matching (a short slice vs a full
        # 600s window) can align anywhere, so the full window is searched.
        if full_shift_search:
            window_points = max(len(a), len(b))
        else:
            window_points = min(
                len(a), len(b), seconds_to_point(SHIFT_SEARCH_WINDOW_SEC)
            )
        shifts = _discover_candidate_shifts_robust(
            a, b, max_point_bit_diff, window_points
        )
    else:
        shifts = _discover_candidate_shifts(a, b, inverted_index_shift)
    logger.info("{} shift_count={}", label, len(shifts))

    candidates: list[dict] = []
    rejected_short_candidates: list[dict] = []
    shift_match_counts: list[tuple[int, int]] = []
    for shift in sorted(shifts):
        a_start = max(0, -shift)
        a_end = min(len(a), len(b) - shift)
        if a_end - a_start < min_points:
            continue
        # Vectorized per-shift Hamming match (mirrors _count_shift_matches): slice both
        # fingerprints at the shift, XOR, popcount via the byte-lookup table, then keep
        # only points within tolerance. Produces the same (a_sec, b_sec, hamming, idx_a,
        # idx_b) 5-tuples the Python loop did, but without per-point interpreter overhead.
        sa = arr_a[a_start:a_end]
        sb = arr_b[a_start + shift : a_end + shift]
        pop = _POPCOUNT_TABLE[(sa ^ sb).view(np.uint8)].reshape(-1, 4).sum(axis=1)
        keep = np.nonzero(pop <= max_point_bit_diff)[0]
        matched: list[tuple[float, float, int, int, int]] = [
            (
                point_to_seconds(a_start + int(k)),
                point_to_seconds(a_start + int(k) + shift),
                int(pop[k]),
                a_start + int(k),
                a_start + int(k) + shift,
            )
            for k in keep
        ]
        shift_match_counts.append((shift, len(matched)))
        if not matched:
            continue

        run: list[tuple[float, float, int, int, int]] = []
        for item in matched:
            if not run or item[0] - run[-1][0] <= max_time_skip_sec:
                run.append(item)
            else:
                _append_chromaprint_run(
                    candidates,
                    run,
                    shift,
                    label,
                    min_duration_sec,
                    max_duration_sec,
                    rejected_short_candidates,
                )
                run = [item]
        _append_chromaprint_run(
            candidates,
            run,
            shift,
            label,
            min_duration_sec,
            max_duration_sec,
            rejected_short_candidates,
        )

    top_shifts = sorted(shift_match_counts, key=lambda x: x[1], reverse=True)[:5]
    logger.info("{} top_shifts_by_matched_points={}", label, top_shifts)
    if segment_type == "credits":
        tail_end_a = point_to_seconds(len(a))
        tail_end_b = point_to_seconds(len(b))
        terminal_candidates: list[dict] = []
        non_terminal_candidates: list[dict] = []
        for candidate in candidates:
            distance_from_tail_end = max(
                tail_end_a - candidate["end_sec_a"],
                tail_end_b - candidate["end_sec_b"],
            )
            candidate["credits_tail_end_distance_sec"] = round(
                distance_from_tail_end, 2
            )
            if distance_from_tail_end <= (
                CREDITS_TERMINAL_WINDOW_SEC + CHROMAPRINT_SAMPLE_DURATION
            ):
                terminal_candidates.append(candidate)
            else:
                non_terminal_candidates.append(candidate)
        for rejected in non_terminal_candidates:
            logger.info(
                "chromaprint_candidate_rejected label={} pair={} segment_type=credits shift={} reason=not_terminal end_distance={:.2f} max_allowed={:.2f} duration={:.2f} matched_points={} avg_hamming={:.3f}",
                label,
                pair_key,
                rejected["shift"],
                rejected["credits_tail_end_distance_sec"],
                CREDITS_TERMINAL_WINDOW_SEC,
                rejected["duration_sec_a"],
                rejected["matched_point_count"],
                rejected["avg_hamming_distance"],
            )
        candidates = terminal_candidates
        candidates.sort(
            key=lambda r: (
                r["duration_sec_a"]
                < (MIN_CREDITS_DURATION - CREDITS_DURATION_TOLERANCE_SEC),
                r["credits_tail_end_distance_sec"],
                r["avg_hamming_distance"],
                -r["matched_point_count"],
                -r["duration_sec_a"],
            )
        )
    else:
        candidates.sort(
            key=lambda r: (
                -r["duration_sec_a"],
                r["avg_hamming_distance"],
                -r["matched_point_count"],
            )
        )
    if candidates:
        longest = max(candidates, key=lambda r: r["duration_sec_a"])
        logger.info(
            "{} longest_range a={:.2f}-{:.2f} b={:.2f}-{:.2f} duration={:.2f} shift={} matched_points={}",
            label,
            longest["start_sec_a"],
            longest["end_sec_a"],
            longest["start_sec_b"],
            longest["end_sec_b"],
            longest["duration_sec_a"],
            longest["shift"],
            longest["matched_point_count"],
        )
    if not candidates and rejected_short_candidates:
        rejected = sorted(
            rejected_short_candidates,
            key=lambda r: (
                -r["contiguous_duration"],
                -r["matched_point_count"],
                r["avg_hamming_distance"],
            ),
        )[0]
        logger.info(
            "chromaprint_candidate_rejected label={} pair={} segment_type={} shift={} reason={} range_a={:.2f}-{:.2f} range_b={:.2f}-{:.2f} contiguous_duration={:.2f} min_required={:.2f} max_allowed={:.2f} matched_points={} avg_hamming={:.3f} max_gap={:.2f}",
            label,
            pair_key,
            segment_type,
            rejected["shift"],
            rejected.get("reason", "duration_rejected"),
            rejected.get("start_sec_a", 0.0),
            rejected.get("end_sec_a", 0.0),
            rejected.get("start_sec_b", 0.0),
            rejected.get("end_sec_b", 0.0),
            rejected["contiguous_duration"],
            min_duration_sec,
            max_duration_sec,
            rejected["matched_point_count"],
            rejected["avg_hamming_distance"],
            rejected.get("max_gap_sec", 0.0),
        )
    for result in candidates[:max_ranges]:
        logger.info(
            "{} returned_range a={:.2f}-{:.2f} dur_a={:.2f} b={:.2f}-{:.2f} dur_b={:.2f} shift={} matched_points={} avg_hamming={:.3f} max_gap={:.2f}",
            label,
            result["start_sec_a"],
            result["end_sec_a"],
            result["duration_sec_a"],
            result["start_sec_b"],
            result["end_sec_b"],
            result["duration_sec_b"],
            result["shift"],
            result["matched_point_count"],
            result["avg_hamming_distance"],
            result["max_gap_sec"],
        )
    return candidates[:max_ranges]


def _append_chromaprint_run(
    candidates: list[dict],
    run: list[tuple[float, float, int, int, int]],
    shift: int,
    label: str,
    min_duration_sec: float,
    max_duration_sec: float,
    rejected_short_candidates: list[dict] | None = None,
) -> None:
    if not run:
        return
    start_a, start_b = run[0][0], run[0][1]
    end_a = run[-1][0] + CHROMAPRINT_SAMPLE_DURATION
    end_b = run[-1][1] + CHROMAPRINT_SAMPLE_DURATION
    duration_a = end_a - start_a
    duration_b = end_b - start_b
    gaps = [run[i][0] - run[i - 1][0] for i in range(1, len(run))]
    hamming_values = [item[2] for item in run]
    avg_hamming = sum(hamming_values) / len(hamming_values)
    expected_points = max(1, seconds_to_point(duration_a))
    match_density = min(len(run) / expected_points, 1.0)
    effective_min_duration_sec = min_duration_sec
    if min_duration_sec == MIN_CREDITS_DURATION:
        # Keep the normal 15s credits requirement in candidate selection, but
        # retain 10-15s terminal runs so the chooser can accept only well-supported
        # short-credit edge cases instead of lowering the global threshold.
        effective_min_duration_sec = SHORT_CREDITS_FALLBACK_MIN_DURATION
    if duration_a < effective_min_duration_sec:
        if rejected_short_candidates is not None:
            rejected_short_candidates.append(
                {
                    "shift": shift,
                    "reason": "duration_too_short",
                    "contiguous_duration": duration_a,
                    "matched_point_count": len(run),
                    "avg_hamming_distance": avg_hamming,
                    "max_gap_sec": max(gaps) if gaps else 0.0,
                    "start_sec_a": start_a,
                    "end_sec_a": end_a,
                    "start_sec_b": start_b,
                    "end_sec_b": end_b,
                }
            )
        return
    if duration_a > max_duration_sec:
        if rejected_short_candidates is not None:
            rejected_short_candidates.append(
                {
                    "shift": shift,
                    "reason": "duration_too_long",
                    "contiguous_duration": duration_a,
                    "matched_point_count": len(run),
                    "avg_hamming_distance": avg_hamming,
                    "max_gap_sec": max(gaps) if gaps else 0.0,
                    "start_sec_a": start_a,
                    "end_sec_a": end_a,
                    "start_sec_b": start_b,
                    "end_sec_b": end_b,
                }
            )
        return
    if match_density < MIN_MATCH_DENSITY:
        if rejected_short_candidates is not None:
            rejected_short_candidates.append(
                {
                    "shift": shift,
                    "reason": "match_density_too_low",
                    "contiguous_duration": duration_a,
                    "matched_point_count": len(run),
                    "match_density": round(match_density, 3),
                    "avg_hamming_distance": avg_hamming,
                    "max_gap_sec": max(gaps) if gaps else 0.0,
                    "start_sec_a": start_a,
                    "end_sec_a": end_a,
                    "start_sec_b": start_b,
                    "end_sec_b": end_b,
                }
            )
        return
    candidates.append(
        {
            "start_sec_a": round(start_a, 2),
            "end_sec_a": round(end_a, 2),
            "duration_sec_a": round(duration_a, 2),
            "start_sec_b": round(start_b, 2),
            "end_sec_b": round(end_b, 2),
            "duration_sec_b": round(duration_b, 2),
            "shift": shift,
            "matched_point_count": len(run),
            "match_density": round(match_density, 3),
            "avg_hamming_distance": round(avg_hamming, 3),
            "max_gap_sec": round(max(gaps) if gaps else 0.0, 2),
            "label": label,
        }
    )


def _ber_window(a: np.ndarray, b: np.ndarray) -> float:
    """Average BER across two equal-length uint32 arrays (Chromaprint int32 XOR)."""
    return _mean_popcount((a ^ b).view(np.uint8)) / 32.0


def _find_repeated_candidates(
    fp_a: list[int],
    fp_b: list[int],
    label: str,
    min_duration_sec: float,
    max_duration_sec: float,
    ber_threshold: float,
    pair_key: str,
    episode_a: int | None = None,
    episode_b: int | None = None,
    max_candidates: int = MAX_GLOBAL_CANDIDATES,
) -> list[dict]:
    if not fp_a or not fp_b:
        return []

    a = np.array(fp_a, dtype=np.int32).view(np.uint32).copy()
    b = np.array(fp_b, dtype=np.int32).view(np.uint32).copy()
    na, nb = len(a), len(b)
    min_frames = int(min_duration_sec * FRAME_RATE)
    max_frames = int(max_duration_sec * FRAME_RATE)
    win_frames = int(WIN_SECONDS * FRAME_RATE)
    max_off = min(int(MAX_OFFSET_SECONDS * FRAME_RATE), max(na, nb) - min_frames)
    if max_off <= 0 or min(na, nb) < min_frames:
        return []

    raw_hits: list[dict] = []
    for d in range(-max_off, max_off + 1, COARSE_STEP):
        if d >= 0:
            overlap = min(na - d, nb)
            a_slice = a[d : d + overlap]
            b_slice = b[:overlap]
            a_abs_off = d
        else:
            abs_d = -d
            overlap = min(na, nb - abs_d)
            a_slice = a[:overlap]
            b_slice = b[abs_d : abs_d + overlap]
            a_abs_off = 0

        if overlap < min_frames:
            continue

        merged_runs: list[dict] = []
        for i in range(0, overlap - min_frames + 1, COARSE_STEP):
            end = min(i + win_frames, overlap)
            if end - i < min_frames:
                break
            ber = _ber_window(a_slice[i:end], b_slice[i:end])
            if ber > ber_threshold:
                continue
            s = a_abs_off + i
            e = a_abs_off + end
            if merged_runs and s <= merged_runs[-1]["a_end"] + COARSE_STEP:
                prev = merged_runs[-1]
                prev["a_end"] = max(prev["a_end"], e)
                prev["ber_weighted_sum"] += ber * (end - i)
                prev["frames"] += end - i
            else:
                merged_runs.append(
                    {
                        "a_start": s,
                        "a_end": e,
                        "offset_frames": d,
                        "ber_weighted_sum": ber * (end - i),
                        "frames": end - i,
                    }
                )
        for run in merged_runs:
            dur = run["a_end"] - run["a_start"]
            if dur < min_frames or dur > max_frames:
                continue
            raw_hits.append(
                {
                    "start_sec": round(run["a_start"] / FRAME_RATE, 2),
                    "end_sec": round(run["a_end"] / FRAME_RATE, 2),
                    "duration_sec": round(dur / FRAME_RATE, 2),
                    "ber": round(run["ber_weighted_sum"] / run["frames"], 4),
                    "offset_frames": run["offset_frames"],
                }
            )

    ranked = sorted(raw_hits, key=lambda h: (h["ber"], -h["duration_sec"]))
    buckets: dict[int, list[dict]] = {}
    for cand in ranked:
        b = int(cand["start_sec"] // 30)
        bucket_list = buckets.setdefault(b, [])
        if len(bucket_list) < MAX_BUCKET_CANDIDATES:
            bucket_list.append(cand)
    kept = sorted(
        [c for v in buckets.values() for c in v],
        key=lambda h: (h["ber"], -h["duration_sec"]),
    )[:max_candidates]

    for cand in kept:
        cand["pair_key"] = pair_key
        cand["episode_a"] = episode_a
        cand["episode_b"] = episode_b
        logger.info(
            "{} candidate pair={} start={:.1f} end={:.1f} dur={:.1f}s ber={:.4f} offset_frames={}",
            label,
            pair_key,
            cand["start_sec"],
            cand["end_sec"],
            cand["duration_sec"],
            cand["ber"],
            cand["offset_frames"],
        )
    return kept


def _cluster_hits(hits: list[dict]) -> dict | None:
    """Return median start/end across a list of match dicts."""
    if not hits:
        return None
    n = len(hits)
    starts = sorted(h["start_sec"] for h in hits)
    ends = sorted(h["end_sec"] for h in hits)
    start = starts[n // 2]
    end = ends[n // 2]
    return {
        "start_sec": start,
        "end_sec": end,
        "duration_sec": round(end - start, 2),
    }


def _support_count(cluster: list[dict]) -> int:
    return len({h["pair_key"] for h in cluster if h.get("pair_key")})


def _feature_slice(
    features: list[dict], start_sec: float, end_sec: float
) -> list[dict]:
    return [f for f in features if start_sec <= f["t"] <= end_sec]


def _normalized_rms(feature: dict) -> float:
    raw = feature.get("normalized_rms", feature.get("rms"))
    value = safe_float(raw, default=0.0)
    if isinstance(raw, str) and raw.strip().lower().endswith("db"):
        value = safe_float(raw.strip()[:-2], default=-90.0)
        return max(0.0, min(1.0, 10 ** (value / 20.0)))
    if value < 0.0:
        return max(0.0, min(1.0, 10 ** (value / 20.0)))
    return max(0.0, min(1.0, value))


def _sample_silent(feature: dict) -> bool:
    return bool(
        feature.get("silent", _normalized_rms(feature) <= SILENCE_RMS_THRESHOLD)
    )


def _state_persists(
    features: list[dict], transition_t: float, want_silent: bool, before: bool
) -> bool:
    if before:
        samples = [f for f in features if transition_t - 1.0 <= f["t"] < transition_t]
    else:
        samples = [f for f in features if transition_t <= f["t"] <= transition_t + 1.0]
    if not samples:
        return False
    return all(_sample_silent(f) is want_silent for f in samples)


def _snap_audio_boundary(
    ts: float,
    features: list[dict],
    direction: str,
    window_sec: float = 2.0,
) -> tuple[float, bool, str]:
    nearby = sorted(
        (f for f in features if abs(f["t"] - ts) <= window_sec),
        key=lambda f: f["t"],
    )
    if len(nearby) < 2:
        return ts, False, "no_nearby_audio_transition"

    transitions: list[float] = []
    for a, b in zip(nearby, nearby[1:]):
        a_silent = _sample_silent(a)
        b_silent = _sample_silent(b)
        if a_silent == b_silent:
            continue
        if direction == "start" and not (a_silent and not b_silent):
            continue
        if direction == "end" and not ((not a_silent) and b_silent):
            continue
        transition_t = b["t"]
        if _state_persists(
            features, transition_t, a_silent, before=True
        ) and _state_persists(features, transition_t, b_silent, before=False):
            transitions.append(transition_t)
    if not transitions:
        return ts, False, "no_persistent_audio_transition"
    snapped = min(transitions, key=lambda t: abs(t - ts))
    if abs(snapped - ts) > window_sec:
        return ts, False, "snap_outside_window"
    return round(snapped, 2), True, f"{direction}_audio_transition"


def _refine_audio_boundaries(
    start_sec: float,
    end_sec: float,
    features: list[dict],
    label: str,
    feature_base_sec: float = 0.0,
) -> tuple[float, float]:
    original_start, original_end = start_sec, end_sec
    if not features:
        logger.info(
            "{} boundary_snap_applied=false original_start={:.2f} original_end={:.2f} snapped_start={:.2f} snapped_end={:.2f} snap_reason=no_features",
            label,
            original_start,
            original_end,
            start_sec,
            end_sec,
        )
        return round(start_sec, 2), round(end_sec, 2)

    local_start = start_sec - feature_base_sec
    local_end = end_sec - feature_base_sec
    snapped_local_start, start_applied, start_reason = _snap_audio_boundary(
        local_start, features, "start"
    )
    snapped_local_end, end_applied, end_reason = _snap_audio_boundary(
        local_end, features, "end"
    )
    snapped_start = round(feature_base_sec + snapped_local_start, 2)
    snapped_end = round(feature_base_sec + snapped_local_end, 2)
    if snapped_end <= snapped_start:
        snapped_start, snapped_end = round(start_sec, 2), round(end_sec, 2)
        start_applied = end_applied = False
        snap_reason = "snap_invalid_duration"
    else:
        snap_reason = (
            ",".join(
                reason
                for applied, reason in (
                    (start_applied, start_reason),
                    (end_applied, end_reason),
                )
                if applied
            )
            or f"{start_reason},{end_reason}"
        )
    logger.info(
        "{} boundary_snap_applied={} original_start={:.2f} original_end={:.2f} snapped_start={:.2f} snapped_end={:.2f} snap_reason={}",
        label,
        str(start_applied or end_applied).lower(),
        original_start,
        original_end,
        snapped_start,
        snapped_end,
        snap_reason,
    )
    return snapped_start, snapped_end


def _score_intro_clusters(
    hits: list[dict], n_pairs: int, label: str = "", features: list[dict] | None = None
) -> dict | None:
    """Score Chromaprint intro clusters using unique episode-pair support only."""
    if not hits or n_pairs <= 0:
        return None

    sorted_hits = sorted(hits, key=lambda h: h["start_sec"])
    clusters: list[list[dict]] = []
    for hit in sorted_hits:
        for cluster in clusters:
            c_start = min(h["start_sec"] for h in cluster)
            c_end = max(h["end_sec"] for h in cluster)
            if hit["start_sec"] < c_end and hit["end_sec"] > c_start:
                cluster.append(hit)
                break
        else:
            clusters.append([hit])

    scored: list[tuple[float, dict]] = []
    for cluster in clusters:
        support = _support_count(cluster)
        confidence = min(support / n_pairs, 1.0)
        candidate_bers = [safe_float(h.get("ber")) for h in cluster]
        avg_ber = sum(candidate_bers) / len(candidate_bers)
        max_ber = max(candidate_bers)
        if avg_ber > max_ber + 0.05:
            logger.warning(
                "{} cluster abnormal_avg_ber candidate_bers={} calculated_avg_ber={:.4f} max_candidate_ber={:.4f}",
                label,
                candidate_bers,
                avg_ber,
                max_ber,
            )
        starts = sorted(h["start_sec"] for h in cluster)
        ends = sorted(h["end_sec"] for h in cluster)
        durations = sorted(h["duration_sec"] for h in cluster)
        mid = len(cluster) // 2
        start = starts[mid]
        end = ends[mid]
        duration = durations[mid]
        penalties: list[str] = []
        bonuses: list[str] = []
        score = (
            (support * 4) + (duration * 2) - (avg_ber * 100) + (math.log(1 + start) * 3)
        )
        if duration < 20:
            score -= 50
            penalties.append("duration<20:-50")
        if start < 30 and duration < 20:
            score -= 30
            penalties.append("start<30&duration<20:-30")
        if duration >= 30:
            score += 20
            bonuses.append("duration>=30:+20")
        if duration >= 45:
            score += 30
            bonuses.append("duration>=45:+30")
        wf = _feature_slice(features or [], start, end)
        if wf:
            rms_values = [_normalized_rms(x) for x in wf]
            mean_rms = float(np.mean(rms_values))
            std_rms = float(np.std(rms_values))
            if mean_rms < 0.03:
                score -= 15
                penalties.append("low_normalized_rms:-15")
            if std_rms < 0.02:
                score += 10
                bonuses.append("consistent_normalized_rms:+10")
        reason = "candidate"
        if not (MIN_INTRO_DURATION <= duration <= MAX_INTRO_DURATION):
            reason = "rejected_duration"
        elif avg_ber >= INTRO_BER_THRESHOLD:
            reason = "rejected_ber"
        elif n_pairs >= 3 and support < 2:
            reason = "rejected_support"
        logger.info(
            "{} cluster start={:.1f} end={:.1f} dur={:.1f}s unique_pair_support={}/{} confidence={:.3f} candidate_bers={} avg_ber={:.4f} max_candidate_ber={:.4f} score={:.2f} penalties={} bonuses={} reason={}",
            label,
            start,
            end,
            duration,
            support,
            n_pairs,
            confidence,
            candidate_bers,
            avg_ber,
            max_ber,
            score,
            penalties,
            bonuses,
            reason,
        )
        if reason == "candidate":
            scored.append(
                (
                    score,
                    {
                        "start_sec": start,
                        "end_sec": end,
                        "duration_sec": duration,
                        "ber": avg_ber,
                        "support_count": support,
                        "confidence": round(confidence, 3),
                        "source": "chromaprint",
                    },
                )
            )

    if not scored:
        return None
    best_score, best = sorted(scored, key=lambda x: x[0], reverse=True)[0]
    logger.info(
        "{} chosen start={:.1f} end={:.1f} dur={:.1f}s unique_pair_support={}/{} avg_ber={:.4f} score={:.2f}",
        label,
        best["start_sec"],
        best["end_sec"],
        best["duration_sec"],
        best["support_count"],
        n_pairs,
        best["ber"],
        best_score,
    )
    start, end = _refine_audio_boundaries(
        best["start_sec"], best["end_sec"], features or [], label
    )
    best["start_sec"], best["end_sec"] = start, end
    best["duration_sec"] = round(end - start, 2)
    return best


def _score_credits_clusters(
    hits: list[dict],
    n_pairs: int,
    episode_duration_sec: float,
    label: str = "",
    features: list[dict] | None = None,
) -> dict | None:
    # TODO: Consider optional visual credits detection in a future separate worker, not in this audio fingerprint worker.
    if not hits or n_pairs <= 0:
        return None
    tail_base = max(0.0, episode_duration_sec - TAIL_WINDOW)
    adjusted = []
    for h in hits:
        abs_start = tail_base + h["start_sec"]
        abs_end = tail_base + h["end_sec"]
        adjusted.append(
            {
                **h,
                "absolute_start_sec": abs_start,
                "absolute_end_sec": abs_end,
                "absolute_start_ratio": (
                    abs_start / episode_duration_sec
                    if episode_duration_sec > 0
                    else 0.0
                ),
            }
        )
    sorted_hits = sorted(adjusted, key=lambda h: h["absolute_start_sec"])
    clusters: list[list[dict]] = []
    for hit in sorted_hits:
        for cluster in clusters:
            c_start = min(h["absolute_start_sec"] for h in cluster)
            c_end = max(h["absolute_end_sec"] for h in cluster)
            if hit["absolute_start_sec"] < c_end and hit["absolute_end_sec"] > c_start:
                cluster.append(hit)
                break
        else:
            clusters.append([hit])

    scored: list[tuple[float, dict]] = []
    for cluster in clusters:
        support = _support_count(cluster)
        confidence = min(support / n_pairs, 1.0)
        candidate_bers = [safe_float(h.get("ber")) for h in cluster]
        avg_ber = sum(candidate_bers) / len(candidate_bers)
        max_ber = max(candidate_bers)
        if avg_ber > max_ber + 0.05:
            logger.warning(
                "{} cluster abnormal_avg_ber candidate_bers={} calculated_avg_ber={:.4f} max_candidate_ber={:.4f}",
                label,
                candidate_bers,
                avg_ber,
                max_ber,
            )
        mid = len(cluster) // 2
        duration = sorted(h["duration_sec"] for h in cluster)[mid]
        local_start = sorted(h["start_sec"] for h in cluster)[mid]
        local_end = sorted(h["end_sec"] for h in cluster)[mid]
        abs_start = sorted(h["absolute_start_sec"] for h in cluster)[mid]
        abs_end = sorted(h["absolute_end_sec"] for h in cluster)[mid]
        start_ratio = sorted(h["absolute_start_ratio"] for h in cluster)[mid]
        penalties: list[str] = []
        bonuses: list[str] = []
        score = (support * 4) + (duration * 1.5) - (avg_ber * 100) + (start_ratio * 40)
        if start_ratio < 0.75:
            score -= 40
            penalties.append("start_ratio<0.75:-40")
        if start_ratio < 0.85:
            score -= 15
            penalties.append("start_ratio<0.85:-15")
        if duration < 25:
            score -= 10
            penalties.append("duration<25:-10")
        if start_ratio >= 0.90:
            score += 25
            bonuses.append("start_ratio>=0.90:+25")
        if duration >= 45:
            score += 15
            bonuses.append("duration>=45:+15")
        if duration >= 60:
            score += 25
            bonuses.append("duration>=60:+25")
        wf = _feature_slice(features or [], local_start, local_end)
        if wf and float(np.mean([_normalized_rms(x) for x in wf])) > 0.05:
            score += 10
            bonuses.append("high_normalized_rms:+10")
        reason = "candidate"
        if not (
            (MIN_CREDITS_SECONDS - CREDITS_DURATION_TOLERANCE_SEC) <= duration <= 180
        ):
            reason = "rejected_duration"
        elif avg_ber > CREDITS_BER_THRESHOLD:
            reason = "rejected_ber"
        elif start_ratio < 0.85 and not (duration >= 60 and confidence >= 0.90):
            reason = "no_reliable_credits_candidate"
        elif n_pairs >= 3 and support < 2:
            reason = "rejected_support"
        logger.info(
            "{} cluster local_start={:.1f} local_end={:.1f} absolute_start={:.1f} absolute_end={:.1f} start_ratio={:.3f} dur={:.1f}s unique_pair_support={}/{} confidence={:.3f} candidate_bers={} avg_ber={:.4f} max_candidate_ber={:.4f} score={:.2f} penalties={} bonuses={} reason={}",
            label,
            local_start,
            local_end,
            abs_start,
            abs_end,
            start_ratio,
            duration,
            support,
            n_pairs,
            confidence,
            candidate_bers,
            avg_ber,
            max_ber,
            score,
            penalties,
            bonuses,
            reason,
        )
        if reason == "candidate":
            scored.append(
                (
                    score,
                    {
                        "start_sec": abs_start,
                        "end_sec": abs_end,
                        "duration_sec": duration,
                        "ber": avg_ber,
                        "support_count": support,
                        "confidence": round(confidence, 3),
                        "absolute_start_ratio": start_ratio,
                        "source": "chromaprint",
                    },
                )
            )
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    if len(scored) >= 2 and (scored[0][0] - scored[1][0]) <= 10:
        if scored[1][1]["start_sec"] > scored[0][1]["start_sec"]:
            scored[0], scored[1] = scored[1], scored[0]
    best_score, best = scored[0]
    logger.info(
        "{} chosen absolute_start={:.1f} absolute_end={:.1f} dur={:.1f}s unique_pair_support={}/{} avg_ber={:.4f} score={:.2f}",
        label,
        best["start_sec"],
        best["end_sec"],
        best["duration_sec"],
        best["support_count"],
        n_pairs,
        best["ber"],
        best_score,
    )
    start, end = _refine_audio_boundaries(
        best["start_sec"], best["end_sec"], features or [], label, tail_base
    )
    best["start_sec"], best["end_sec"] = start, end
    best["duration_sec"] = round(end - start, 2)
    best["absolute_start_ratio"] = (
        start / episode_duration_sec if episode_duration_sec > 0 else 0.0
    )
    return best


def _extract_audio_features(
    url: str, start_sec: float, duration_sec: float
) -> list[dict]:
    if not ENABLE_AUDIO_FEATURE_EXTRACTION:
        return []
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-ss",
        str(start_sec),
        *_ffmpeg_input_thread_args(),
        *loopback_proxy_input(url),
        "-t",
        str(duration_sec),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "11025",
        "-af",
        "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
        "-f",
        "null",
        "-",
    ]
    try:
        out = subprocess.check_output(
            cmd, stderr=subprocess.STDOUT, text=True, timeout=120
        )
    except Exception as exc:
        if ENABLE_AUDIO_FEATURE_EXTRACTION:
            logger.warning("feature_extraction_failed reason={}", exc)
        return []
    try:
        vals = []
        sample_index = 0
        for line in out.splitlines():
            m = re.search(r"RMS_level=([^\s]+)", line)
            if not m:
                continue
            raw_value = m.group(1)
            db = safe_float(raw_value, default=-90.0)
            normalized_rms = 0.0 if db <= -90 else float(10 ** (db / 20.0))
            normalized_rms = max(0.0, min(1.0, normalized_rms))
            # astats emits one RMS value per reset interval; keep timestamps in
            # seconds relative to this extracted window rather than sample/frame
            # counters from ffmpeg metadata.
            t = round(sample_index * FEATURE_SAMPLE_STEP, 2)
            sample_index += 1
            vals.append(
                {
                    "t": t,
                    "rms": round(normalized_rms, 5),
                    "normalized_rms": round(normalized_rms, 5),
                    "silent": normalized_rms <= SILENCE_RMS_THRESHOLD,
                }
            )
        max_t = max([safe_float(f.get("t")) for f in vals] or [0.0])
        if any(f["t"] < 0 or f["t"] > duration_sec + 1.0 for f in vals):
            if ENABLE_AUDIO_FEATURE_EXTRACTION:
                logger.warning(
                    "feature_extraction_failed reason=invalid_time_units duration_sec={} max_t={}",
                    duration_sec,
                    max_t,
                )
            return []
        return vals
    except Exception as exc:
        if ENABLE_AUDIO_FEATURE_EXTRACTION:
            logger.warning("feature_extraction_failed reason={}", exc)
        return []


def _find_waveform_intro_fallback(features: list[dict], label: str = "") -> dict | None:
    if not features:
        logger.info(
            "{} fallback_intro_candidates=0 fallback_reason=no_head_features", label
        )
        return None
    samples = sorted(features, key=lambda f: f["t"])
    candidates: list[dict] = []
    i = 0
    while i < len(samples):
        while i < len(samples) and _sample_silent(samples[i]):
            i += 1
        start_i = i
        while i < len(samples) and not _sample_silent(samples[i]):
            i += 1
        end_i = i - 1
        if start_i >= len(samples) or end_i < start_i:
            continue
        region = samples[start_i : end_i + 1]
        start = float(region[0]["t"])
        end = float(region[-1]["t"] + FEATURE_SAMPLE_STEP)
        duration = end - start
        if duration < 20 or duration > 120:
            continue
        rms_values = [_normalized_rms(f) for f in region]
        avg_rms = float(np.mean(rms_values)) if rms_values else 0.0
        silence_ratio = sum(1 for f in region if _sample_silent(f)) / len(region)
        score = (duration * 2) + (avg_rms * 50) - (silence_ratio * 100) + (start * 0.03)
        candidate = {
            "start_sec": round(start, 2),
            "end_sec": round(end, 2),
            "duration_sec": round(duration, 2),
            "waveform_score": round(score, 2),
            "avg_normalized_rms": round(avg_rms, 5),
            "silence_ratio": round(silence_ratio, 3),
        }
        candidates.append(candidate)
        logger.info(
            "{} fallback_intro_candidate waveform_score={:.2f} fallback_start={:.1f} fallback_end={:.1f} fallback_duration={:.1f} avg_normalized_rms={:.5f} silence_ratio={:.3f}",
            label,
            score,
            start,
            end,
            duration,
            avg_rms,
            silence_ratio,
        )
    logger.info("{} fallback_intro_candidates={}", label, len(candidates))
    if not candidates:
        logger.info("{} fallback_reason=no_music_like_region", label)
        return None
    best = sorted(candidates, key=lambda c: c["waveform_score"], reverse=True)[0]
    if (
        best["waveform_score"] < WAVEFORM_FALLBACK_SCORE_THRESHOLD
        or not (20 <= best["duration_sec"] <= 120)
        or best["silence_ratio"] > 0.25
    ):
        logger.info(
            "{} fallback_reason=below_threshold waveform_score={:.2f} threshold={:.2f} silence_ratio={:.3f}",
            label,
            best["waveform_score"],
            WAVEFORM_FALLBACK_SCORE_THRESHOLD,
            best["silence_ratio"],
        )
        return None
    start, end = _refine_audio_boundaries(
        best["start_sec"], best["end_sec"], features, f"{label} waveform_fallback"
    )
    best.update(
        {
            "start_sec": start,
            "end_sec": end,
            "duration_sec": round(end - start, 2),
            "confidence": WAVEFORM_FALLBACK_CONFIDENCE_CAP,
            "support_count": 0,
            "source": "waveform_fallback",
        }
    )
    logger.info(
        "{} fallback_reason=accepted waveform_score={:.2f} fallback_start={:.1f} fallback_end={:.1f} fallback_duration={:.1f} avg_normalized_rms={:.5f} silence_ratio={:.3f} source=waveform_fallback",
        label,
        best["waveform_score"],
        best["start_sec"],
        best["end_sec"],
        best["duration_sec"],
        best["avg_normalized_rms"],
        best["silence_ratio"],
    )
    return best


def _delete_auto_markers(
    db, imdb_id: str, season: int, episode: int, segment_type: str
) -> int:
    query = db.query(SegmentMarker).filter_by(
        imdb_id=imdb_id,
        season=season,
        episode=episode,
        segment_type=segment_type,
    )
    if hasattr(SegmentMarker, "source"):
        query = query.filter(
            (SegmentMarker.source.is_(None))
            | (~SegmentMarker.source.in_(["manual", "client_feedback"]))
        )
    if hasattr(SegmentMarker, "is_user_provided"):
        query = query.filter(
            (SegmentMarker.is_user_provided.is_(False))
            | (SegmentMarker.is_user_provided.is_(None))
        )
    return int(query.delete(synchronize_session=False) or 0)


def _marker_matches(existing: SegmentMarker, start_sec: float, end_sec: float) -> bool:
    return (
        abs(safe_float(existing.start_sec) - start_sec) <= 0.1
        and abs(safe_float(existing.end_sec) - end_sec) <= 0.1
        and existing.fingerprint_version == MARKER_FINGERPRINT_VERSION
    )


def _save_auto_marker(
    db,
    imdb_id: str,
    season: int,
    episode: int,
    segment_type: str,
    segment: dict,
    confidence: float,
) -> int:
    if segment.get("source") == "waveform_fallback" and not ENABLE_WAVEFORM_FALLBACK:
        logger.warning(
            "waveform_fallback_save_blocked segment_type={} episode={}",
            segment_type,
            episode,
        )
        return 0
    if (
        segment.get("source") in ("blackframe", "audio+blackframe")
        and not ENABLE_BLACKFRAME_CREDITS
    ):
        logger.warning(
            "blackframe_credits_save_blocked segment_type={} episode={}",
            segment_type,
            episode,
        )
        return 0
    if segment.get("source") == "blackframe":
        confidence = min(confidence, VISION_ONLY_CONFIDENCE_CAP)
    start_sec = round(segment["start_sec"], 2)
    end_sec = round(segment["end_sec"], 2)
    existing_query = db.query(SegmentMarker).filter_by(
        imdb_id=imdb_id,
        season=season,
        episode=episode,
        segment_type=segment_type,
    )
    if hasattr(SegmentMarker, "source"):
        existing_query = existing_query.filter(
            (SegmentMarker.source.is_(None))
            | (~SegmentMarker.source.in_(["manual", "client_feedback"]))
        )
    if hasattr(SegmentMarker, "is_user_provided"):
        existing_query = existing_query.filter(
            (SegmentMarker.is_user_provided.is_(False))
            | (SegmentMarker.is_user_provided.is_(None))
        )
    existing = existing_query.first()
    if existing and _marker_matches(existing, start_sec, end_sec):
        logger.info(
            "marker_unchanged segment_type={} episode={} start_sec={} end_sec={} fingerprint_version={}",
            segment_type,
            episode,
            start_sec,
            end_sec,
            MARKER_FINGERPRINT_VERSION,
        )
        return 0

    deleted = _delete_auto_markers(db, imdb_id, season, episode, segment_type)
    kwargs = {
        "imdb_id": imdb_id,
        "season": season,
        "episode": episode,
        "segment_type": segment_type,
        "start_sec": start_sec,
        "end_sec": end_sec,
        "confidence": min(round(confidence, 3), 1.0),
    }
    if hasattr(SegmentMarker, "source"):
        kwargs["source"] = segment.get("source", "chromaprint")
    if hasattr(SegmentMarker, "fingerprint_version"):
        kwargs["fingerprint_version"] = MARKER_FINGERPRINT_VERSION
    if hasattr(SegmentMarker, "detector_version"):
        kwargs["detector_version"] = DETECTOR_VERSION
    if hasattr(SegmentMarker, "evidence"):
        kwargs["evidence"] = segment.get("visual_evidence")
    if hasattr(SegmentMarker, "is_user_provided"):
        kwargs["is_user_provided"] = False
    db.add(SegmentMarker(**kwargs))
    logger.info(
        "marker_upsert segment_type={} episode={} source={} fingerprint_version={} stale_deleted={}",
        segment_type,
        episode,
        segment.get("source", "chromaprint"),
        MARKER_FINGERPRINT_VERSION,
        deleted,
    )
    return deleted


def _upsert_learning_state(
    db,
    imdb_id: str,
    season: int,
    done_episodes: list[int],
    selected_episodes: list[int],
    intro_hits: int,
    credits_hits: int,
    reason: str | None,
    diagnostics: dict | None = None,
) -> None:

    state = (
        db.query(SeasonLearningState).filter_by(imdb_id=imdb_id, season=season).first()
    )
    if state is None:
        state = SeasonLearningState(imdb_id=imdb_id, season=season)
        db.add(state)
    state.done_episodes = done_episodes
    state.selected_episodes = selected_episodes
    state.intro_pair_hits = intro_hits
    state.credits_pair_hits = credits_hits
    state.last_reason = reason
    state.diagnostics = diagnostics or {}
    state.attempted_at = utc_now()
    db.commit()


def build_full_season_pair_plan(
    episodes: list[int], unresolved_episodes: set[int] | None = None
) -> list[tuple[int, int]]:
    """Build an ordered full-season comparison plan that keeps late episodes covered."""
    ordered = sorted({int(ep) for ep in episodes})
    unresolved = set(unresolved_episodes or [])
    pairs: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()

    def add_pair(a: int, b: int) -> None:
        if a == b:
            return
        pair = (min(a, b), max(a, b))
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)

    for distance in (1, 2):
        for index, episode in enumerate(ordered):
            peer_index = index + distance
            if peer_index < len(ordered):
                add_pair(episode, ordered[peer_index])

    # If a prior pass left episodes unresolved, make sure they are compared with
    # nearby companions even when gaps in episode numbering exist.
    if unresolved:
        for episode in ordered:
            if episode not in unresolved:
                continue
            companions = sorted(
                (other for other in ordered if other != episode),
                key=lambda other: (abs(other - episode), other),
            )[:3]
            for companion in companions:
                add_pair(episode, companion)

    return pairs


def aggregate_episode_candidates(
    episodes: list[int], candidates_by_episode: dict[int, list[dict]]
) -> dict[str, int]:
    return {
        str(int(ep)): len(candidates_by_episode.get(int(ep), [])) for ep in episodes
    }


def classify_episode_marker_state(
    segment: dict | None,
    reason: str | None,
    valid_comparisons: int,
    min_valid_comparisons: int = 2,
) -> tuple[str, str | None]:
    if segment and safe_float(segment.get("confidence")) > 0:
        return "confirmed", None
    if valid_comparisons < min_valid_comparisons:
        return "pending_companion", reason or "insufficient_valid_comparisons"
    return "rejected", reason or "no_reliable_marker_candidate"


def should_cleanup_stale_marker(
    marker_state: str, source: str | None = None, is_user_provided: bool = False
) -> bool:
    if is_user_provided or source in {"manual", "client_feedback"}:
        return False
    return marker_state == "rejected"


def _possible_pairs_for_episode(
    selected_pairs: list[tuple[int, int]], episode: int
) -> int:
    return len([pair for pair in selected_pairs if episode in pair])


def _candidate_quality(
    candidates: list[dict], segment_type: str
) -> tuple[float, float]:
    """Return (hamming_quality, duration_agreement) in [0, 1] for a candidate set.

    hamming_quality: 1.0 when the average per-pair match is at/below CONFIDENCE_HAMMING_FULL
      bits, falling linearly to 0 at the segment's per-point bit-diff ceiling.
    duration_agreement: 1.0 when per-pair durations agree, decreasing as their spread
      approaches CONFIDENCE_DURATION_TOLERANCE_SEC. A noisy or inconsistent match can no
      longer earn the same confidence as a clean, consistent one purely on pair support.
    """
    if not candidates:
        return 0.0, 0.0
    max_bit_diff = (
        MAX_CREDITS_POINT_BIT_DIFF if segment_type == "credits" else MAX_POINT_BIT_DIFF
    )
    # Use the cleanest candidate per pair so a single noisy duplicate can't dominate.
    best_per_pair: dict[object, dict] = {}
    for candidate in candidates:
        pair_key = candidate.get("pair_key")
        hamming = safe_float(candidate.get("avg_hamming_distance"), max_bit_diff)
        current = best_per_pair.get(pair_key)
        if current is None or hamming < safe_float(
            current.get("avg_hamming_distance"), max_bit_diff
        ):
            best_per_pair[pair_key] = candidate
    reps = list(best_per_pair.values())
    hammings = [safe_float(c.get("avg_hamming_distance"), max_bit_diff) for c in reps]
    avg_hamming = sum(hammings) / len(hammings)
    span = max(1e-6, max_bit_diff - CONFIDENCE_HAMMING_FULL)
    hamming_quality = max(
        0.0, min(1.0, 1.0 - (avg_hamming - CONFIDENCE_HAMMING_FULL) / span)
    )
    durations = [safe_float(c.get("duration_sec")) for c in reps]
    spread = float(np.std(durations)) if len(durations) >= 2 else 0.0
    duration_agreement = max(0.0, 1.0 - spread / CONFIDENCE_DURATION_TOLERANCE_SEC)
    return hamming_quality, duration_agreement


def _candidate_confidence(
    candidates: list[dict],
    selected_pairs: list[tuple[int, int]],
    episode: int,
    segment_type: str = "intro",
) -> tuple[int, int, float]:
    possible_pairs = _possible_pairs_for_episode(selected_pairs, episode)
    support = len({candidate["pair_key"] for candidate in candidates})
    # Saturate support at SUPPORT_SATURATION_PAIRS confirming pairs rather than dividing by
    # the episode's total possible pairs: adding more episodes to a season no longer makes
    # full support harder to reach, so one anomalous neighbour can't drag a clean match
    # below the serve gate (the "inconsistent across episodes" failure).
    support_frac = min(support / SUPPORT_SATURATION_PAIRS, 1.0) if support > 0 else 0.0
    hamming_quality, duration_agreement = _candidate_quality(candidates, segment_type)
    # Support stays dominant (so existing thresholds keep their meaning) but match quality
    # and cross-pair agreement now modulate it: a pristine, consistent match keeps ~full
    # confidence while a noisy or inconsistent one is discounted.
    confidence = (
        support_frac * (0.5 + 0.5 * hamming_quality) * (0.7 + 0.3 * duration_agreement)
    )
    # Strong-evidence floor for a single confirming pair: a near-pristine, consistent match
    # is real signal even before a second pair confirms it. Let quality lift confidence to
    # just over the serve gate while staying below the 0.85 reference bar (a lone pair must
    # never seed a cross-season reference).
    if support >= 1:
        quality_floor = (
            SINGLE_PAIR_FLOOR_BASE + SINGLE_PAIR_FLOOR_QUALITY * hamming_quality
        ) * (0.7 + 0.3 * duration_agreement)
        confidence = max(confidence, quality_floor)
    return support, possible_pairs, min(confidence, 1.0)


def _candidate_match_density(candidate: dict) -> float:
    stored = candidate.get("match_density")
    if stored is not None:
        return max(0.0, min(1.0, safe_float(stored)))
    duration = max(0.0, safe_float(candidate.get("duration_sec")))
    matched = int(candidate.get("matched_point_count") or 0)
    if duration <= 0 or matched <= 0:
        return 0.0
    return min(1.0, matched / max(1, seconds_to_point(duration)))


def _cluster_episode_candidates(candidates: list[dict]) -> list[list[dict]]:
    """Group only candidates that agree on both boundaries within five seconds.

    Each episode pair gets at most one vote in a cluster. A pair may still contribute to
    two genuinely different clusters (for example a recap and a title sequence), but its
    duplicate matcher paths can never inflate either cluster's confidence.
    """
    clusters: list[dict[str, dict]] = []
    ordered = sorted(
        candidates,
        key=lambda candidate: (
            safe_float(candidate.get("start_sec")),
            safe_float(candidate.get("end_sec")),
            safe_float(candidate.get("avg_hamming_distance"), 99.0),
        ),
    )
    for candidate in ordered:
        if _candidate_match_density(candidate) < MIN_MATCH_DENSITY:
            continue
        pair_key = str(candidate.get("pair_key") or "unknown")
        start = safe_float(candidate.get("start_sec"))
        end = safe_float(candidate.get("end_sec"))
        compatible: list[tuple[float, dict[str, dict]]] = []
        for votes in clusters:
            reps = list(votes.values())
            center_start = float(
                np.median([safe_float(c.get("start_sec")) for c in reps])
            )
            center_end = float(np.median([safe_float(c.get("end_sec")) for c in reps]))
            if (
                abs(start - center_start) <= CANDIDATE_BOUNDARY_TOLERANCE_SEC
                and abs(end - center_end) <= CANDIDATE_BOUNDARY_TOLERANCE_SEC
            ):
                compatible.append(
                    (abs(start - center_start) + abs(end - center_end), votes)
                )
        if compatible:
            votes = min(compatible, key=lambda item: item[0])[1]
            previous = votes.get(pair_key)
            candidate_rank = (
                safe_float(candidate.get("avg_hamming_distance"), 99.0),
                -_candidate_match_density(candidate),
                -safe_float(candidate.get("duration_sec")),
            )
            if previous is None or candidate_rank < (
                safe_float(previous.get("avg_hamming_distance"), 99.0),
                -_candidate_match_density(previous),
                -safe_float(previous.get("duration_sec")),
            ):
                votes[pair_key] = candidate
        else:
            clusters.append({pair_key: candidate})
    return [list(votes.values()) for votes in clusters]


def _cluster_summary(
    cluster: list[dict],
    selected_pairs: list[tuple[int, int]],
    episode: int,
    segment_type: str,
) -> dict:
    support, possible_pairs, confidence = _candidate_confidence(
        cluster, selected_pairs, episode, segment_type
    )
    starts = [safe_float(candidate.get("start_sec")) for candidate in cluster]
    ends = [safe_float(candidate.get("end_sec")) for candidate in cluster]
    durations = [safe_float(candidate.get("duration_sec")) for candidate in cluster]
    hammings = [
        safe_float(candidate.get("avg_hamming_distance"), 99.0) for candidate in cluster
    ]
    densities = [_candidate_match_density(candidate) for candidate in cluster]
    hamming_quality, duration_agreement = _candidate_quality(cluster, segment_type)
    coverage = sum(densities) / len(densities)
    quality = (hamming_quality + duration_agreement + coverage) / 3.0
    confidence = min(1.0, confidence * (0.75 + 0.25 * coverage))
    return {
        "candidates": cluster,
        "support": support,
        "possible_pairs": possible_pairs,
        "start_sec": float(np.median(starts)),
        "end_sec": float(np.median(ends)),
        "duration_sec": float(np.median(durations)),
        "avg_hamming_distance": sum(hammings) / len(hammings),
        "point_coverage": coverage,
        "boundary_spread_sec": max(max(starts) - min(starts), max(ends) - min(ends)),
        "quality": quality,
        "confidence": confidence,
        "matched_point_count": int(
            np.median(
                [
                    int(candidate.get("matched_point_count") or 0)
                    for candidate in cluster
                ]
            )
        ),
    }


def _choose_intro_candidate(
    candidates: list[dict], selected_pairs: list[tuple[int, int]], episode: int
) -> tuple[dict | None, str | None, dict]:
    max_candidate_start = max([c["start_sec"] for c in candidates] or [0.0])
    candidates_after_120s = len([c for c in candidates if c["start_sec"] > 120])
    clusters = [
        _cluster_summary(cluster, selected_pairs, episode, "intro")
        for cluster in _cluster_episode_candidates(candidates)
    ]
    diag: dict[str, object] = {
        "candidate_cluster_count": len(clusters),
        "max_candidate_start": max_candidate_start,
        "candidates_after_120s": candidates_after_120s,
        "clusters": [
            {
                "support": cluster["support"],
                "start_sec": round(cluster["start_sec"], 2),
                "end_sec": round(cluster["end_sec"], 2),
                "duration_sec": round(cluster["duration_sec"], 2),
                "point_coverage": round(cluster["point_coverage"], 3),
                "avg_hamming_distance": round(cluster["avg_hamming_distance"], 3),
                "confidence": round(cluster["confidence"], 3),
            }
            for cluster in clusters
        ],
    }
    valid: list[dict] = []
    for cluster in clusters:
        duration = cluster["duration_sec"]
        normal_duration = MIN_INTRO_DURATION <= duration <= MAX_INTRO_DURATION
        short_duration = (
            SHORT_INTRO_FALLBACK_MIN_DURATION <= duration < MIN_INTRO_DURATION
        )
        multi_pair = cluster["support"] >= 2
        pristine_single = (
            cluster["support"] == 1
            and duration >= 20.0
            and cluster["avg_hamming_distance"] <= 2.0
            and cluster["point_coverage"] >= SINGLE_INTRO_MIN_MATCH_DENSITY
        )
        pristine_short = (
            multi_pair
            and short_duration
            and cluster["avg_hamming_distance"] <= SHORT_INTRO_FALLBACK_MAX_AVG_HAMMING
            and cluster["point_coverage"] >= SINGLE_INTRO_MIN_MATCH_DENSITY
            and cluster["start_sec"] <= SHORT_INTRO_FALLBACK_MAX_START_SEC
        )
        if (normal_duration and (multi_pair or pristine_single)) or pristine_short:
            valid.append(cluster)
    if not valid:
        diag["rejection_reason"] = "no_boundary_cluster_with_required_support"
        return None, "no_reliable_intro_candidate", diag
    selected = max(
        valid,
        key=lambda cluster: (
            cluster["support"],
            cluster["quality"],
            cluster["duration_sec"],
            -cluster["start_sec"],
        ),
    )
    diag.update(
        {
            "support": selected["support"],
            "possible_pairs": selected["possible_pairs"],
            "confidence": round(selected["confidence"], 3),
            "point_coverage": round(selected["point_coverage"], 3),
            "boundary_spread_sec": round(selected["boundary_spread_sec"], 2),
        }
    )
    return (
        {
            "start_sec": round(selected["start_sec"], 2),
            "end_sec": round(selected["end_sec"], 2),
            "duration_sec": round(selected["duration_sec"], 2),
            "avg_hamming_distance": round(selected["avg_hamming_distance"], 3),
            "matched_point_count": selected["matched_point_count"],
            "match_density": round(selected["point_coverage"], 3),
            "support_count": selected["support"],
            "confidence": round(selected["confidence"], 3),
            "source": "chromaprint",
            "short_intro_fallback": selected["duration_sec"] < MIN_INTRO_DURATION,
        },
        None,
        diag,
    )


def _choose_credits_candidate(
    candidates: list[dict],
    selected_pairs: list[tuple[int, int]],
    episode: int,
    episode_duration_sec: float,
) -> tuple[dict | None, str | None, dict]:
    clusters = [
        _cluster_summary(cluster, selected_pairs, episode, "credits")
        for cluster in _cluster_episode_candidates(candidates)
    ]
    diag: dict[str, object] = {
        "candidate_cluster_count": len(clusters),
        "clusters": [
            {
                "support": cluster["support"],
                "start_sec": round(cluster["start_sec"], 2),
                "end_sec": round(cluster["end_sec"], 2),
                "duration_sec": round(cluster["duration_sec"], 2),
                "point_coverage": round(cluster["point_coverage"], 3),
                "avg_hamming_distance": round(cluster["avg_hamming_distance"], 3),
                "confidence": round(cluster["confidence"], 3),
            }
            for cluster in clusters
        ],
    }
    valid = [
        cluster
        for cluster in clusters
        if cluster["support"] >= 2
        and (MIN_CREDITS_DURATION - CREDITS_DURATION_TOLERANCE_SEC)
        <= cluster["duration_sec"]
        <= MAX_CREDITS_DURATION
        and episode_duration_sec > 0
        and abs(episode_duration_sec - cluster["end_sec"])
        <= CREDITS_TERMINAL_WINDOW_SEC + CHROMAPRINT_SAMPLE_DURATION
    ]
    if not valid:
        diag["rejection_reason"] = "credits_require_two_pair_terminal_audio_cluster"
        return None, "no_reliable_credits_candidate", diag
    selected = max(
        valid,
        key=lambda cluster: (
            cluster["support"],
            cluster["quality"],
            cluster["duration_sec"],
            cluster["start_sec"],
        ),
    )
    start_ratio = selected["start_sec"] / episode_duration_sec
    start_distance_from_end = episode_duration_sec - selected["start_sec"]
    diag.update(
        {
            "support": selected["support"],
            "possible_pairs": selected["possible_pairs"],
            "confidence": round(selected["confidence"], 3),
            "point_coverage": round(selected["point_coverage"], 3),
            "boundary_spread_sec": round(selected["boundary_spread_sec"], 2),
            "credits_start_ratio": round(start_ratio, 3),
            "credits_start_distance_from_end_sec": round(start_distance_from_end, 2),
            "credits_end_distance_from_end_sec": round(
                abs(episode_duration_sec - selected["end_sec"]), 2
            ),
        }
    )
    return (
        {
            "start_sec": round(selected["start_sec"], 2),
            "end_sec": round(selected["end_sec"], 2),
            "duration_sec": round(selected["duration_sec"], 2),
            "absolute_start_ratio": start_ratio,
            "avg_hamming_distance": round(selected["avg_hamming_distance"], 3),
            "matched_point_count": selected["matched_point_count"],
            "match_density": round(selected["point_coverage"], 3),
            "support_count": selected["support"],
            "confidence": round(selected["confidence"], 3),
            "source": "chromaprint",
        },
        None,
        diag,
    )


_BLACK_INTERVAL_RE = re.compile(r"black_start:([0-9.]+)\s+black_end:([0-9.]+)")
_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[0-9.]+)")


def _parse_black_silence(stderr: str, offset_sec: float) -> dict:
    """Parse ffmpeg blackdetect/silencedetect stderr into absolute-time intervals."""
    black: list[tuple[float, float]] = []
    for match in _BLACK_INTERVAL_RE.finditer(stderr):
        black.append(
            (float(match.group(1)) + offset_sec, float(match.group(2)) + offset_sec)
        )
    silence: list[tuple[float, float]] = []
    pending: float | None = None
    for line in stderr.splitlines():
        start_match = _SILENCE_START_RE.search(line)
        if start_match:
            pending = float(start_match.group(1))
            continue
        end_match = _SILENCE_END_RE.search(line)
        if end_match and pending is not None:
            silence.append(
                (pending + offset_sec, float(end_match.group(1)) + offset_sec)
            )
            pending = None
    return {"black": black, "silence": silence}


def _detect_black_and_silence(
    url: str, start_sec: float, duration_sec: float, *, force: bool = False
) -> dict:
    """Run a single ffmpeg pass over a window detecting black frames and silence.

    Used to anchor credits boundaries (the cut into end credits is a consistent black +
    silence transition even when the credits music varies per episode). Default-off."""
    if not force and not ENABLE_BLACKFRAME_CREDITS:
        return {"black": [], "silence": []}
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-ss",
        str(start_sec),
        *_ffmpeg_input_thread_args(),
        *loopback_proxy_input(url),
        "-t",
        str(duration_sec),
        "-vf",
        f"blackdetect=d={BLACKDETECT_MIN_DURATION}:pic_th={BLACKDETECT_PIX_THRESHOLD}",
        "-af",
        f"silencedetect=n={SILENCEDETECT_NOISE_DB}dB:d={SILENCEDETECT_MIN_DURATION}",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(45, int(duration_sec * 3)),
        )
        return _parse_black_silence(proc.stderr or "", start_sec)
    except Exception as exc:
        logger.warning("black_silence_detect_failed reason={}", exc)
        return {"black": [], "silence": []}


def _credits_vision_anchors(
    black: list, silence: list, pad: float = 1.0
) -> list[float]:
    """Credits-start anchors: black-frame starts that co-occur with silence (a hard cut
    into credits), requiring both to avoid mid-episode act-break false positives."""
    anchors: list[float] = []
    for black_start, black_end in black:
        if any(
            not (sil_end < black_start - pad or sil_start > black_end + pad)
            for sil_start, sil_end in silence
        ):
            anchors.append(float(black_start))
    return sorted(anchors)


def _fuse_credits_with_vision(
    credits_segment: dict | None,
    black: list,
    silence: list,
    episode_duration_sec: float,
) -> tuple[dict | None, str]:
    """Fuse the audio credits candidate with black/silence anchors.

    - audio + nearby anchor -> snap the start to the visual cut (source 'audio+blackframe')
    - audio + no nearby anchor -> unchanged
    - no audio + a strong terminal anchor -> vision-only credits (source 'blackframe',
      confidence capped below the serve gate so it never serves un-fused)."""
    anchors = _credits_vision_anchors(black, silence)
    if credits_segment:
        start = safe_float(credits_segment.get("start_sec"))
        near = [a for a in anchors if abs(a - start) <= CREDITS_SNAP_MAX_DELTA_SEC]
        if near:
            snapped = min(near, key=lambda a: abs(a - start))
            fused = dict(credits_segment)
            fused["start_sec"] = round(snapped, 2)
            fused["duration_sec"] = round(safe_float(fused.get("end_sec")) - snapped, 2)
            fused["source"] = "audio+blackframe"
            return fused, "audio+blackframe"
        return credits_segment, str(credits_segment.get("source") or "chromaprint")
    if episode_duration_sec > 0 and anchors:
        terminal = [a for a in anchors if a / episode_duration_sec >= 0.90]
        if terminal:
            start = min(terminal)
            duration = episode_duration_sec - start
            if duration >= MIN_CREDITS_DURATION:
                return (
                    {
                        "start_sec": round(start, 2),
                        "end_sec": round(episode_duration_sec, 2),
                        "duration_sec": round(duration, 2),
                        "confidence": VISION_ONLY_CONFIDENCE_CAP,
                        "support_count": 0,
                        "source": "blackframe",
                    },
                    "blackframe",
                )
    return None, "no_reliable_credits_candidate"


def _extract_reference_slice(
    fp: dict, segment_type: str, segment: dict, duration_sec: float
) -> list[int]:
    """Slice the confirmed segment's Chromaprint points out of an episode fingerprint."""
    pad = REFERENCE_SLICE_PAD_SEC
    if segment_type == "intro":
        points = fp.get("head", []) or []
        local_start = safe_float(segment.get("start_sec"))
        local_end = safe_float(segment.get("end_sec"))
    else:
        points = fp.get("tail", []) or []
        tail_dur = safe_float(
            fp.get("tail_duration_sec"), min(duration_sec, TAIL_WINDOW)
        )
        tail_base = max(0.0, duration_sec - tail_dur)
        local_start = safe_float(segment.get("start_sec")) - tail_base
        local_end = safe_float(segment.get("end_sec")) - tail_base
    i0 = max(0, seconds_to_point(local_start - pad))
    i1 = min(len(points), seconds_to_point(local_end + pad))
    if i1 <= i0:
        return []
    return [int(p) & 0xFFFFFFFF for p in points[i0:i1]]


def _write_season_reference(
    db,
    imdb_id: str,
    season: int,
    segment_type: str,
    slice_points: list[int],
    duration_sec: float,
    confidence: float,
    derived_from_episodes: int,
) -> None:
    """Upsert a canonical intro/credits reference for a confidently-learned season."""
    if not slice_points:
        return
    payload = {
        "points": slice_points,
        "fingerprint_version": FINGERPRINT_VERSION,
        "detector_version": DETECTOR_VERSION,
    }
    existing = (
        db.query(SeasonSegmentReference)
        .filter_by(imdb_id=imdb_id, season=season, segment_type=segment_type)
        .first()
    )
    if existing:
        existing.fingerprint = payload
        existing.duration_sec = duration_sec
        existing.confidence = confidence
        existing.derived_from_episodes = derived_from_episodes
    else:
        db.add(
            SeasonSegmentReference(
                imdb_id=imdb_id,
                season=season,
                segment_type=segment_type,
                fingerprint=payload,
                duration_sec=duration_sec,
                confidence=confidence,
                derived_from_episodes=derived_from_episodes,
            )
        )
    logger.info(
        "season_reference_written imdb_id={} season={} segment_type={} points={} confidence={:.3f} derived_from={}",
        imdb_id,
        season,
        segment_type,
        len(slice_points),
        confidence,
        derived_from_episodes,
    )


def _load_season_references(
    db, imdb_id: str, segment_type: str, exclude_season: int | None = None
) -> list[SeasonSegmentReference]:
    """Load version-matched references for a show, strongest confidence first."""
    rows = (
        db.query(SeasonSegmentReference)
        .filter_by(imdb_id=imdb_id, segment_type=segment_type)
        .all()
    )
    refs: list[SeasonSegmentReference] = []
    for row in rows:
        if exclude_season is not None and row.season == exclude_season:
            continue
        fp = row.fingerprint or {}
        if fp.get("fingerprint_version") != FINGERPRINT_VERSION:
            continue
        if fp.get("detector_version") != DETECTOR_VERSION:
            continue
        if not fp.get("points"):
            continue
        refs.append(row)
    refs.sort(key=lambda r: safe_float(r.confidence), reverse=True)
    return refs


def _reference_segment_for_episode(
    db, imdb_id: str, season: int, row: EpisodeFingerprint, segment_type: str
) -> dict | None:
    """Match one episode's head/tail against cross-season references; return a capped-
    confidence segment dict (source="cross_season") or None. Reuses _find_chromaprint_ranges
    with the reference slice as fp_a so all run/quality logic is identical."""
    refs = _load_season_references(db, imdb_id, segment_type, exclude_season=season)
    if not refs:
        return None
    fp: dict = row.fingerprint or {}
    duration_sec = safe_float(row.duration_sec)
    if segment_type == "intro":
        window = fp.get("head", []) or []
        min_dur, max_dur, max_bit = (
            SHORT_INTRO_FALLBACK_MIN_DURATION,
            MAX_INTRO_DURATION,
            MAX_POINT_BIT_DIFF,
        )
    else:
        window = fp.get("tail", []) or []
        min_dur, max_dur, max_bit = (
            SHORT_CREDITS_FALLBACK_MIN_DURATION,
            MAX_CREDITS_DURATION,
            MAX_CREDITS_POINT_BIT_DIFF,
        )
    if not window:
        return None
    best: dict | None = None
    chosen_ref: SeasonSegmentReference | None = None
    for ref in refs:
        ref_points = (ref.fingerprint or {}).get("points", [])
        ranges = _find_chromaprint_ranges(
            ref_points,
            window,
            label=f"{imdb_id}:S{season:02d} {segment_type} ref:S{ref.season:02d}",
            min_duration_sec=min_dur,
            max_duration_sec=max_dur,
            max_point_bit_diff=max_bit,
            segment_type=segment_type,
            pair_key=f"ref:{ref.season}",
            full_shift_search=True,
        )
        if ranges:
            best = (
                ranges[0]
                if segment_type == "credits"
                else max(ranges, key=lambda r: r["duration_sec_b"])
            )
            chosen_ref = ref
            break
    if not best or chosen_ref is None:
        return None

    if segment_type == "intro":
        start = best["start_sec_b"]
        end = best["end_sec_b"]
    else:
        tail_dur = safe_float(
            fp.get("tail_duration_sec"), min(duration_sec, TAIL_WINDOW)
        )
        tail_base = max(0.0, duration_sec - tail_dur)
        start = tail_base + best["start_sec_b"]
        end = tail_base + best["end_sec_b"]
    duration = end - start
    if duration <= 0:
        return None
    if segment_type == "credits":
        if (
            duration_sec <= 0
            or abs(duration_sec - end)
            > CREDITS_TERMINAL_WINDOW_SEC + CHROMAPRINT_SAMPLE_DURATION
            or int(chosen_ref.derived_from_episodes or 0) < 2
        ):
            return None
    elif not (MIN_INTRO_DURATION <= duration <= MAX_INTRO_DURATION):
        # short intros only when the cross-season match is very clean
        if not (
            duration >= SHORT_INTRO_FALLBACK_MIN_DURATION
            and safe_float(best.get("avg_hamming_distance"), 99.0)
            <= SHORT_INTRO_FALLBACK_MAX_AVG_HAMMING
        ):
            return None

    span = max(1e-6, max_bit - CONFIDENCE_HAMMING_FULL)
    hamming_quality = max(
        0.0,
        min(
            1.0,
            1.0
            - (safe_float(best.get("avg_hamming_distance")) - CONFIDENCE_HAMMING_FULL)
            / span,
        ),
    )
    confidence = min(
        CROSS_SEASON_CONFIDENCE_CAP,
        safe_float(chosen_ref.confidence) * (0.6 + 0.4 * hamming_quality),
    )
    segment = {
        "start_sec": round(start, 2),
        "end_sec": round(end, 2),
        "duration_sec": round(duration, 2),
        "confidence": round(confidence, 3),
        "support_count": max(1, int(chosen_ref.derived_from_episodes or 0)),
        "source": "cross_season",
        "avg_hamming_distance": best.get("avg_hamming_distance"),
        "reference_season": chosen_ref.season,
    }
    if segment_type == "credits":
        fused, _reason = _fuse_credits_with_visual(
            segment,
            fp.get("tail_visual_runs", []) if _visual_phase_ready(fp) else [],
            duration_sec,
            safe_float(fp.get("tail_duration_sec"), min(duration_sec, TAIL_WINDOW)),
        )
        if not fused:
            return None
        fused["reference_season"] = chosen_ref.season
        return fused
    return segment


def _apply_cross_season_references(
    db,
    imdb_id: str,
    season: int,
    rows: list[EpisodeFingerprint],
    confirmed: set[tuple[int, str]],
) -> dict[str, int]:
    """Fill unconfirmed (episode, segment_type) slots from cross-season references."""
    saved = {"intro": 0, "credits": 0}
    for row in rows:
        episode = int(row.episode)
        for segment_type in ("intro", "credits"):
            if (episode, segment_type) in confirmed:
                continue
            segment = _reference_segment_for_episode(
                db, imdb_id, season, row, segment_type
            )
            if not segment:
                continue
            _save_auto_marker(
                db,
                imdb_id,
                season,
                episode,
                segment_type,
                segment,
                safe_float(segment.get("confidence")),
            )
            saved[segment_type] += 1
            logger.info(
                "cross_season_marker_saved imdb_id={} season={} episode={} segment_type={} reference_season={} confidence={:.3f}",
                imdb_id,
                season,
                episode,
                segment_type,
                segment.get("reference_season"),
                safe_float(segment.get("confidence")),
            )
    return saved


def _try_learn_season(imdb_id: str, season: int) -> None:
    """
    Run pairwise Chromaprint point-index matching for a season and write per-episode markers.
    """
    season_key = f"{imdb_id}:S{season:02d}"

    with get_db() as db:
        _nc_key = f"{imdb_id}:{season}"
        with _needs_companion_lock:
            _nc_episodes = set(_needs_companion.get(_nc_key, set()))

        # Lightweight no-op pre-check. _try_learn_season runs on every episode completion,
        # so an already-learned, unchanged season would otherwise reload and deserialize
        # every episode's fingerprint JSON (~tens of KB each) just to decide there is nothing
        # to do. Decide that from a cheap (episode, source_url_hash) projection and bail
        # before the heavy load. Skip only when the prior run reached a clean terminal state
        # AND nothing affecting the result changed: same done-set, no pending companion, same
        # detector version, and an identical per-episode source-hash signature (a silent
        # re-fingerprint of an existing episode changes its hash and so still forces a re-learn).
        light_rows = (
            db.query(EpisodeFingerprint.episode, EpisodeFingerprint.source_url_hash)
            .filter_by(imdb_id=imdb_id, season=season, status="done")
            .order_by(EpisodeFingerprint.episode)
            .all()
        )
        active_partial_count = (
            db.query(EpisodeFingerprint)
            .filter(
                EpisodeFingerprint.imdb_id == imdb_id,
                EpisodeFingerprint.season == season,
                EpisodeFingerprint.status.in_(("partial", "processing")),
            )
            .count()
        )
        light_numbers = [r.episode for r in light_rows]
        if (
            len(light_rows) >= MIN_EPISODES_TO_LEARN
            and not _nc_episodes
            and active_partial_count == 0
        ):
            prior_state = (
                db.query(SeasonLearningState)
                .filter_by(imdb_id=imdb_id, season=season)
                .first()
            )
            if prior_state is not None and prior_state.last_reason is None:
                prior_diag = prior_state.diagnostics or {}
                current_signature = {
                    str(r.episode): r.source_url_hash for r in light_rows
                }
                prior_signature = {
                    ep: (meta or {}).get("source_url_hash")
                    for ep, meta in (
                        prior_diag.get("episode_fingerprints") or {}
                    ).items()
                }
                if (
                    list(prior_state.done_episodes or []) == list(light_numbers)
                    and prior_diag.get("fingerprint_version")
                    == MARKER_FINGERPRINT_VERSION
                    and prior_signature == current_signature
                ):
                    logger.info(
                        "Season learning {} — skip_noop done_episodes={} reason=already_complete",
                        season_key,
                        light_numbers,
                    )
                    return

        candidate_rows = (
            db.query(EpisodeFingerprint)
            .filter(
                EpisodeFingerprint.imdb_id == imdb_id,
                EpisodeFingerprint.season == season,
                EpisodeFingerprint.status.in_(("done", "partial", "processing")),
            )
            .order_by(EpisodeFingerprint.episode)
            .all()
        )
        done_rows = [
            row
            for row in candidate_rows
            if _audio_phase_ready(row.fingerprint or {}, "head")
            or _audio_phase_ready(row.fingerprint or {}, "tail")
        ]
        done_numbers = [r.episode for r in done_rows]
        logger.info(
            "Season learning start {} — done_episodes={}", season_key, done_numbers
        )

        if len(done_rows) < MIN_EPISODES_TO_LEARN:
            reason = "insufficient_episodes"
            desired_companions = [ep for ep in (2, 3, 4) if ep not in done_numbers]
            if done_numbers == [1]:
                logger.info(
                    "season_learning_waiting imdb_id={} season={} episode=1 reason=needs_companion done_episodes={} desired_companions={}",
                    imdb_id,
                    season,
                    done_numbers,
                    desired_companions,
                )
            else:
                logger.info(
                    "season_learning_waiting imdb_id={} season={} reason=insufficient_episodes done_episodes={} min_required={}",
                    imdb_id,
                    season,
                    done_numbers,
                    MIN_EPISODES_TO_LEARN,
                )
            # Episodes that cannot yet form an in-season pair (e.g. a new season's
            # episode 1) can still get markers from a prior season's reference.
            cross_season_saved = _apply_cross_season_references(
                db, imdb_id, season, done_rows, confirmed=set()
            )
            _upsert_learning_state(
                db,
                imdb_id,
                season,
                done_numbers,
                [],
                0,
                0,
                reason,
                {
                    "desired_companions": desired_companions,
                    "cross_season_markers": cross_season_saved,
                },
            )
            db.commit()
            return

        selected = list(done_rows)
        selected_numbers = [int(r.episode) for r in selected]
        selected_pairs = build_full_season_pair_plan(selected_numbers, _nc_episodes)
        row_by_episode = {int(row.episode): row for row in selected}
        pairs = [
            (row_by_episode[episode_a], row_by_episode[episode_b])
            for episode_a, episode_b in selected_pairs
            if episode_a in row_by_episode and episode_b in row_by_episode
        ]
        total_pairs = len(pairs)
        logger.info(
            "Season learning {} — full_season_episode_count={} generated_pair_plan={}",
            season_key,
            len(selected_numbers),
            [f"{a}x{b}" for a, b in selected_pairs],
        )
        if _nc_episodes:
            logger.info(
                "needs_companion_included imdb_id={} season={} episodes={}",
                imdb_id,
                season,
                sorted(_nc_episodes & set(selected_numbers)),
            )
        intro_candidates_by_episode: dict[int, list[dict]] = {
            int(row.episode): [] for row in selected
        }
        credits_candidates_by_episode: dict[int, list[dict]] = {
            int(row.episode): [] for row in selected
        }
        intro_hit_pairs: set[str] = set()
        credits_hit_pairs: set[str] = set()
        intro_valid_comparisons_by_episode: dict[int, int] = {
            int(row.episode): 0 for row in selected
        }
        credits_valid_comparisons_by_episode: dict[int, int] = {
            int(row.episode): 0 for row in selected
        }
        extraction_modes: set[str] = set()

        for row in selected:
            fp: dict = row.fingerprint or {}
            if fp.get("extraction_mode"):
                extraction_modes.add(str(fp.get("extraction_mode")))

        for ep_a, ep_b in pairs:
            ep_a_number = int(ep_a.episode)
            ep_b_number = int(ep_b.episode)
            pair_key = f"{ep_a_number}x{ep_b_number}"
            pair_label = f"ep{ep_a_number}×ep{ep_b_number}"
            fp_a: dict = ep_a.fingerprint or {}
            fp_b: dict = ep_b.fingerprint or {}
            hash_a = ep_a.source_url_hash
            hash_b = ep_b.source_url_hash
            head_a = fp_a.get("head", [])
            head_b = fp_b.get("head", [])
            tail_a = fp_a.get("tail", [])
            tail_b = fp_b.get("tail", [])
            logger.debug(
                "Pair {} {} — head_a={} head_b={} tail_a={} tail_b={} points",
                season_key,
                pair_label,
                len(head_a),
                len(head_b),
                len(tail_a),
                len(tail_b),
            )
            if head_a and head_b:
                intro_valid_comparisons_by_episode[ep_a_number] += 1
                intro_valid_comparisons_by_episode[ep_b_number] += 1
            if tail_a and tail_b:
                credits_valid_comparisons_by_episode[ep_a_number] += 1
                credits_valid_comparisons_by_episode[ep_b_number] += 1

            cache_key = (
                imdb_id,
                season,
                ep_a_number,
                ep_b_number,
                fp_a.get("fingerprint_version"),
                fp_b.get("fingerprint_version"),
                hash_a,
                hash_b,
            )
            cached_pair = _pair_match_cache_get(cache_key)
            if cached_pair is not None:
                intro_ranges = cached_pair["intro"]
                credits_ranges = cached_pair.get("credits", [])
            else:
                intro_ranges = _find_chromaprint_ranges(
                    head_a,
                    head_b,
                    label=f"{season_key} intro {pair_label}",
                    min_duration_sec=SHORT_INTRO_FALLBACK_MIN_DURATION,
                    max_duration_sec=MAX_INTRO_DURATION,
                    max_point_bit_diff=MAX_POINT_BIT_DIFF,
                )
                # Credits v2 is terminal visual analysis per episode.  Comparing
                # every tail against every other episode was both expensive and
                # semantically wrong for credits that vary by episode.
                credits_ranges = []
                if head_a and head_b:
                    _pair_match_cache_put(
                        cache_key,
                        {"intro": intro_ranges, "credits": credits_ranges},
                    )
            if intro_ranges:
                intro_hit_pairs.add(pair_key)
                for best in intro_ranges:
                    intro_candidates_by_episode[ep_a_number].append(
                        {
                            "episode": ep_a_number,
                            "segment_type": "intro",
                            "start_sec": best["start_sec_a"],
                            "end_sec": best["end_sec_a"],
                            "duration_sec": best["duration_sec_a"],
                            "pair_key": pair_key,
                            "avg_hamming_distance": best["avg_hamming_distance"],
                            "matched_point_count": best["matched_point_count"],
                            "match_density": best.get("match_density", 0.0),
                        }
                    )
                    intro_candidates_by_episode[ep_b_number].append(
                        {
                            "episode": ep_b_number,
                            "segment_type": "intro",
                            "start_sec": best["start_sec_b"],
                            "end_sec": best["end_sec_b"],
                            "duration_sec": best["duration_sec_b"],
                            "pair_key": pair_key,
                            "avg_hamming_distance": best["avg_hamming_distance"],
                            "matched_point_count": best["matched_point_count"],
                            "match_density": best.get("match_density", 0.0),
                        }
                    )
            else:
                logger.debug("Intro pair {} {} — no_hit", season_key, pair_label)

            if credits_ranges:
                credits_hit_pairs.add(pair_key)
                tail_duration_a = safe_float(
                    fp_a.get("tail_duration_sec"),
                    min(safe_float(ep_a.duration_sec), TAIL_WINDOW),
                )
                tail_duration_b = safe_float(
                    fp_b.get("tail_duration_sec"),
                    min(safe_float(ep_b.duration_sec), TAIL_WINDOW),
                )
                tail_base_a = max(0.0, safe_float(ep_a.duration_sec) - tail_duration_a)
                tail_base_b = max(0.0, safe_float(ep_b.duration_sec) - tail_duration_b)
                for best in credits_ranges:
                    credits_candidates_by_episode[ep_a_number].append(
                        {
                            "episode": ep_a_number,
                            "segment_type": "credits",
                            "start_sec": round(tail_base_a + best["start_sec_a"], 2),
                            "end_sec": round(tail_base_a + best["end_sec_a"], 2),
                            "duration_sec": best["duration_sec_a"],
                            "pair_key": pair_key,
                            "avg_hamming_distance": best["avg_hamming_distance"],
                            "matched_point_count": best["matched_point_count"],
                            "match_density": best.get("match_density", 0.0),
                        }
                    )
                    credits_candidates_by_episode[ep_b_number].append(
                        {
                            "episode": ep_b_number,
                            "segment_type": "credits",
                            "start_sec": round(tail_base_b + best["start_sec_b"], 2),
                            "end_sec": round(tail_base_b + best["end_sec_b"], 2),
                            "duration_sec": best["duration_sec_b"],
                            "pair_key": pair_key,
                            "avg_hamming_distance": best["avg_hamming_distance"],
                            "matched_point_count": best["matched_point_count"],
                            "match_density": best.get("match_density", 0.0),
                        }
                    )
            else:
                logger.info("Credits pair {} {} — no_hit", season_key, pair_label)

        intro_pair_hits = min(len(intro_hit_pairs), total_pairs)
        credits_pair_hits = min(len(credits_hit_pairs), total_pairs)
        saved_marker_types: set[str] = set()
        stale_cleanup = {"intro": 0, "credits": 0}
        per_episode_intro: dict[str, dict] = {}
        per_episode_credits: dict[str, dict] = {}
        intro_reasons: dict[str, str | None] = {}
        credits_reasons: dict[str, str | None] = {}
        max_intro_candidate_start = 0.0
        intro_candidates_after_120s = 0
        credits_start_ratios: dict[str, float | None] = {}
        per_episode_marker_states: dict[str, dict[str, dict[str, str | None | int]]] = (
            {}
        )

        confirmed: set[tuple[int, str]] = set()
        confirmed_segments: dict[str, list[tuple[int, dict, EpisodeFingerprint]]] = {
            "intro": [],
            "credits": [],
        }
        for row in done_rows:
            episode_number = int(row.episode)
            if episode_number in intro_candidates_by_episode:
                intro_segment, intro_reason, intro_diag = _choose_intro_candidate(
                    intro_candidates_by_episode[episode_number],
                    selected_pairs,
                    episode_number,
                )
                per_episode_intro[str(episode_number)] = intro_diag
                max_intro_candidate_start = max(
                    max_intro_candidate_start, intro_diag["max_candidate_start"]
                )
                intro_candidates_after_120s += intro_diag["candidates_after_120s"]
            else:
                intro_segment, intro_reason = None, "no_reliable_intro_candidate"
                per_episode_intro[str(episode_number)] = {
                    "support": 0,
                    "possible_pairs": _possible_pairs_for_episode(
                        selected_pairs, episode_number
                    ),
                    "valid_comparisons": intro_valid_comparisons_by_episode.get(
                        episode_number, 0
                    ),
                    "confidence": 0.0,
                }
            intro_reasons[str(episode_number)] = intro_reason

            fp_row: dict = row.fingerprint or {}
            credits_segment, credits_reason, credits_diag = (
                _stored_visual_credits_candidate(fp_row, safe_float(row.duration_sec))
            )
            credits_diag["visual_version"] = fp_row.get("tail_visual_version")
            credits_diag["visual_run_count"] = len(
                fp_row.get("tail_visual_runs", []) or []
            )
            credits_diag["support"] = 1 if credits_segment else 0
            credits_diag["possible_pairs"] = 0
            credits_diag["valid_comparisons"] = 0
            if credits_segment:
                credits_diag["credits_start_ratio"] = round(
                    safe_float(credits_segment.get("start_sec"))
                    / max(1.0, safe_float(row.duration_sec)),
                    3,
                )
            else:
                credits_diag["rejection_reason"] = credits_reason
            per_episode_credits[str(episode_number)] = credits_diag
            credits_start_ratios[str(episode_number)] = credits_diag.get(
                "credits_start_ratio"
            )
            credits_reasons[str(episode_number)] = credits_reason

            intro_state, intro_state_reason = classify_episode_marker_state(
                intro_segment,
                intro_reason,
                intro_valid_comparisons_by_episode.get(episode_number, 0),
            )
            credits_state, credits_state_reason = classify_episode_marker_state(
                credits_segment,
                credits_reason,
                0,
                min_valid_comparisons=0,
            )
            per_episode_marker_states[str(episode_number)] = {
                "intro": {
                    "state": intro_state,
                    "reason": intro_state_reason,
                    "valid_comparisons": intro_valid_comparisons_by_episode.get(
                        episode_number, 0
                    ),
                },
                "credits": {
                    "state": credits_state,
                    "reason": credits_state_reason,
                    "valid_comparisons": credits_valid_comparisons_by_episode.get(
                        episode_number, 0
                    ),
                },
            }

            if intro_state == "confirmed" and intro_segment:
                if not season_segment_enabled(db, imdb_id, season, "intro"):
                    deleted = _delete_auto_markers(
                        db, imdb_id, season, row.episode, "intro"
                    )
                    stale_cleanup["intro"] += deleted
                else:
                    stale_cleanup["intro"] += _save_auto_marker(
                        db,
                        imdb_id,
                        season,
                        row.episode,
                        "intro",
                        intro_segment,
                        safe_float(intro_segment.get("confidence")),
                    )
                    saved_marker_types.add("intro")
                    confirmed.add((episode_number, "intro"))
                    confirmed_segments["intro"].append(
                        (episode_number, intro_segment, row)
                    )
            elif should_cleanup_stale_marker(intro_state):
                deleted = _delete_auto_markers(
                    db, imdb_id, season, row.episode, "intro"
                )
                stale_cleanup["intro"] += deleted
                logger.debug(
                    "stale_marker_cleanup_executed segment_type=intro episode={} deleted={} preserved_sources=[manual,client_feedback] state={} reason={}",
                    row.episode,
                    deleted,
                    intro_state,
                    intro_state_reason,
                )
            else:
                logger.debug(
                    "stale_marker_cleanup_skipped segment_type=intro episode={} preserved_sources=[manual,client_feedback] state={} reason={}",
                    row.episode,
                    intro_state,
                    intro_state_reason,
                )

            if credits_state == "confirmed" and credits_segment:
                if not season_segment_enabled(db, imdb_id, season, "credits"):
                    deleted = _delete_auto_markers(
                        db, imdb_id, season, row.episode, "credits"
                    )
                    stale_cleanup["credits"] += deleted
                else:
                    stale_cleanup["credits"] += _save_auto_marker(
                        db,
                        imdb_id,
                        season,
                        row.episode,
                        "credits",
                        credits_segment,
                        safe_float(credits_segment.get("confidence")),
                    )
                    saved_marker_types.add("credits")
                    confirmed.add((episode_number, "credits"))
                    confirmed_segments["credits"].append(
                        (episode_number, credits_segment, row)
                    )
            elif should_cleanup_stale_marker(credits_state):
                deleted = _delete_auto_markers(
                    db, imdb_id, season, row.episode, "credits"
                )
                stale_cleanup["credits"] += deleted
                logger.debug(
                    "stale_marker_cleanup_executed segment_type=credits episode={} deleted={} preserved_sources=[manual,client_feedback] state={} reason={}",
                    row.episode,
                    deleted,
                    credits_state,
                    credits_state_reason,
                )
            else:
                logger.debug(
                    "stale_marker_cleanup_skipped segment_type=credits episode={} preserved_sources=[manual,client_feedback] state={} reason={}",
                    row.episode,
                    credits_state,
                    credits_state_reason,
                )

        # Persist canonical references from confidently-confirmed segments so future
        # seasons (and pair-less episodes) can reuse them. Only real in-season detections
        # seed references — never cross-season-derived markers — to avoid reference chains.
        for segment_type, items in confirmed_segments.items():
            strong = [
                (ep, seg, r)
                for (ep, seg, r) in items
                if safe_float(seg.get("confidence")) >= REFERENCE_MIN_CONFIDENCE
                and seg.get("source")
                == ("audio+visual" if segment_type == "credits" else "chromaprint")
            ]
            if len(strong) >= REFERENCE_MIN_EPISODES:
                best_ep, best_seg, best_row = max(
                    strong, key=lambda x: safe_float(x[1].get("confidence"))
                )
                slice_points = _extract_reference_slice(
                    best_row.fingerprint or {},
                    segment_type,
                    best_seg,
                    safe_float(best_row.duration_sec),
                )
                _write_season_reference(
                    db,
                    imdb_id,
                    season,
                    segment_type,
                    slice_points,
                    safe_float(best_seg.get("duration_sec")),
                    safe_float(best_seg.get("confidence")),
                    len(strong),
                )

        # Fill any still-unconfirmed (episode, segment_type) slots from prior seasons.
        cross_season_saved = _apply_cross_season_references(
            db, imdb_id, season, done_rows, confirmed
        )
        if cross_season_saved.get("intro"):
            saved_marker_types.add("intro")
        if cross_season_saved.get("credits"):
            saved_marker_types.add("credits")

        # Vision-only terminal credits where audio found nothing (capped below the serve
        # gate, so stored for diagnostics but never served un-fused). Default-off.
        if ENABLE_BLACKFRAME_CREDITS:
            for row in done_rows:
                episode_number = int(row.episode)
                if (episode_number, "credits") in confirmed:
                    continue
                fp_row = row.fingerprint or {}
                vision_seg, _vision_src = _fuse_credits_with_vision(
                    None,
                    fp_row.get("tail_black", []),
                    fp_row.get("tail_silence", []),
                    safe_float(row.duration_sec),
                )
                if vision_seg:
                    _save_auto_marker(
                        db,
                        imdb_id,
                        season,
                        episode_number,
                        "credits",
                        vision_seg,
                        safe_float(vision_seg.get("confidence")),
                    )

        unresolved_episodes = {
            ep: states
            for ep, states in per_episode_marker_states.items()
            if any(
                segment_state.get("state") == "pending_companion"
                for segment_state in states.values()
            )
        }
        rejected_episodes = {
            ep: states
            for ep, states in per_episode_marker_states.items()
            if any(
                segment_state.get("state") == "rejected"
                for segment_state in states.values()
            )
        }
        logger.debug(
            "Season learning {} — per_episode_candidate_counts intro={} credits={}",
            season_key,
            aggregate_episode_candidates(selected_numbers, intro_candidates_by_episode),
            aggregate_episode_candidates(
                selected_numbers, credits_candidates_by_episode
            ),
        )
        logger.debug(
            "Season learning {} — unresolved_episodes={} rejected_episodes={}",
            season_key,
            unresolved_episodes,
            rejected_episodes,
        )

        reason: str | None = None
        if unresolved_episodes:
            reason = "pending_companion"
        # A well-evidenced rejection is a terminal detector result, not unfinished
        # learning. Keep the explicit per-marker rejection reasons in diagnostics
        # while allowing completed, ambiguous episodes to stop polling/relearning.

        diagnostics = {
            "detector_version": DETECTOR_VERSION,
            "fingerprint_version": MARKER_FINGERPRINT_VERSION,
            "selected_episodes": selected_numbers,
            "full_season_episode_count": len(selected_numbers),
            "generated_pair_plan": [f"{a}x{b}" for a, b in selected_pairs],
            "total_pairs": total_pairs,
            "intro_pair_hits": intro_pair_hits,
            "credits_pair_hits": credits_pair_hits,
            "per_episode_intro": per_episode_intro,
            "per_episode_credits": per_episode_credits,
            "per_episode_candidate_counts": {
                "intro": aggregate_episode_candidates(
                    selected_numbers, intro_candidates_by_episode
                ),
                "credits": aggregate_episode_candidates(
                    selected_numbers, credits_candidates_by_episode
                ),
            },
            "per_episode_marker_states": per_episode_marker_states,
            "unresolved_episodes": unresolved_episodes,
            "max_intro_candidate_start": max_intro_candidate_start,
            "intro_candidates_after_120s": intro_candidates_after_120s,
            "intro_reliability_reason": intro_reasons,
            "credits_start_ratio": credits_start_ratios,
            "credits_reliability_reason": credits_reasons,
            "saved_marker_types": sorted(saved_marker_types),
            "stale_marker_cleanup": stale_cleanup,
            "cross_season_markers": cross_season_saved,
            "episode_fingerprints": {
                str(row.episode): {
                    "status": row.status,
                    "source_url_hash": row.source_url_hash,
                    "fingerprint_version": (row.fingerprint or {}).get(
                        "fingerprint_version"
                    ),
                    "detector_version": (row.fingerprint or {}).get("detector_version"),
                    "phases": (row.fingerprint or {}).get("phases") or {},
                    "head_ready": _audio_phase_ready(row.fingerprint or {}, "head"),
                    "tail_ready": _audio_phase_ready(row.fingerprint or {}, "tail"),
                    "visual_ready": _visual_phase_ready(row.fingerprint or {}),
                    "visual_run_count": len(
                        (row.fingerprint or {}).get("tail_visual_runs") or []
                    ),
                }
                for row in done_rows
            },
            "extraction_mode": sorted(extraction_modes) or ["unknown"],
            "waveform_fallback_enabled": ENABLE_WAVEFORM_FALLBACK,
        }

        _upsert_learning_state(
            db,
            imdb_id,
            season,
            done_numbers,
            selected_numbers,
            intro_pair_hits,
            credits_pair_hits,
            reason,
            diagnostics,
        )
        db.commit()
        logger.info(
            "Season learning {} — complete total_pairs={} intro_pair_hits={} credits_pair_hits={} saved_marker_types={} stale_marker_cleanup={}",
            season_key,
            total_pairs,
            intro_pair_hits,
            credits_pair_hits,
            sorted(saved_marker_types),
            stale_cleanup,
        )

        pending_episode_numbers = {
            int(ep)
            for ep, states in per_episode_marker_states.items()
            if any(
                segment_state.get("state") == "pending_companion"
                for segment_state in states.values()
            )
        }
        with _needs_companion_lock:
            _existing_nc = _needs_companion.get(_nc_key, set())
            _new_entries = pending_episode_numbers - _existing_nc
            for _ep_num in sorted(_new_entries):
                _states = per_episode_marker_states.get(str(_ep_num), {})
                _valid_comparisons = (
                    max(
                        int(_state.get("valid_comparisons") or 0)
                        for _state in _states.values()
                    )
                    if _states
                    else 0
                )
                _next_companions = [
                    ep
                    for ep in sorted(
                        selected_numbers, key=lambda ep: (abs(ep - _ep_num), ep)
                    )
                    if ep != _ep_num
                ][:3]
                logger.info(
                    "episode_needs_companion imdb_id={} season={} episode={} reason=pending_companion valid_comparisons={} min_required=2 next_companions={}",
                    imdb_id,
                    season,
                    _ep_num,
                    _valid_comparisons,
                    _next_companions,
                )
            if pending_episode_numbers:
                _needs_companion[_nc_key] = pending_episode_numbers
            elif _nc_key in _needs_companion:
                del _needs_companion[_nc_key]


def _sanitize_media_url_for_log(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    path_parts = [part for part in parsed.path.split("/") if part]
    suffix = path_parts[-1] if path_parts else ""
    if len(suffix) > 160:
        suffix = f"…{suffix[-159:]}"
    if not suffix:
        return f"{parsed.scheme}://{parsed.netloc}"
    return f"{parsed.scheme}://{parsed.netloc}/…/{suffix}"


def _probe_duration(url: str) -> float:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        *loopback_proxy_input(url),
    ]
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        safe_url = _sanitize_media_url_for_log(url)
        logger.error(
            "ffprobe_duration_failed url={} returncode=timeout stderr={} stdout={}",
            safe_url,
            (exc.stderr or "")[:500] if isinstance(exc.stderr, str) else "",
            (exc.stdout or "")[:500] if isinstance(exc.stdout, str) else "",
        )
        raise RuntimeError(f"ffprobe duration timeout url={safe_url}") from exc

    if completed.returncode != 0:
        safe_url = _sanitize_media_url_for_log(url)
        stderr = (completed.stderr or "").strip()[:1000]
        stdout = (completed.stdout or "").strip()[:1000]
        logger.error(
            "ffprobe_duration_failed url={} returncode={} stderr={} stdout={}",
            safe_url,
            completed.returncode,
            stderr,
            stdout,
        )
        raise RuntimeError(
            f"ffprobe duration failed returncode={completed.returncode} url={safe_url} stderr={stderr}"
        )

    try:
        return float(json.loads(completed.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        safe_url = _sanitize_media_url_for_log(url)
        stdout = (completed.stdout or "").strip()[:1000]
        logger.error(
            "ffprobe_duration_parse_failed url={} stdout={}",
            safe_url,
            stdout,
        )
        raise RuntimeError(f"ffprobe duration parse failed url={safe_url}") from exc


def _canonical_episode_id(imdb_id: str, season: int, episode: int) -> str:
    return f"{imdb_id}:{season}:{episode}"


def _parse_canonical_episode_id(canonical_id: str) -> tuple[str, int, int] | None:
    parts = canonical_id.split(":")
    if len(parts) < 3 or not parts[0].startswith("tt"):
        return None
    try:
        return parts[0], int(parts[1]), int(parts[2])
    except (TypeError, ValueError):
        return None


def _source_url_hash(url: str | None) -> str | None:
    if not url:
        return None
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _duration_media_identity(
    duration_sec: float, head_points: list[int] | None = None
) -> dict | None:
    """Return a compact, privacy-safe identity for URL-rotation reuse.

    Signed playback URLs are transport credentials, not media identities.  A
    duration mismatch is a hard incompatibility.  When a URL changes, a short
    head Chromaprint sample makes matching runtimes safe to reuse as well.  The
    duration-only form is retained for lazy upgrades of old rows, but never
    permits reuse by itself.
    """
    duration = safe_float(duration_sec)
    if duration <= 0:
        return None
    points = list(head_points or [])[
        : seconds_to_point(MEDIA_IDENTITY_SENTINEL_WINDOW_SEC)
    ]
    if not points:
        return {"version": "duration-v1", "duration_sec": round(duration, 2)}
    encoded = b"".join(
        (int(point) & 0xFFFFFFFF).to_bytes(4, "big", signed=False) for point in points
    )
    return {
        "version": "duration+head-sentinel-v2",
        "duration_sec": round(duration, 2),
        "head_sentinel_hash": hashlib.sha256(encoded).hexdigest()[:16],
        "head_sentinel_point_count": len(points),
    }


def _media_identities_are_compatible(previous: object, incoming: object) -> bool:
    if not isinstance(previous, dict) or not isinstance(incoming, dict):
        return False
    if (
        previous.get("version") != "duration+head-sentinel-v2"
        or incoming.get("version") != "duration+head-sentinel-v2"
    ):
        return False
    old_duration = safe_float(previous.get("duration_sec"))
    new_duration = safe_float(incoming.get("duration_sec"))
    if old_duration <= 0 or new_duration <= 0:
        return False
    # Preserve a known marker across provider/CDN URL rotation, but fail closed
    # for alternate cuts.  A sub-percent allowance absorbs mux/container timing
    # drift without treating a materially different episode as equivalent.
    tolerance = max(2.0, old_duration * 0.005)
    return (
        abs(old_duration - new_duration) <= tolerance
        and bool(previous.get("head_sentinel_hash"))
        and previous.get("head_sentinel_hash") == incoming.get("head_sentinel_hash")
    )


def _is_current_fingerprint(row: EpisodeFingerprint | None) -> bool:
    if row is None or row.status != "done":
        return False
    fp: dict = row.fingerprint or {}
    return (
        _audio_phase_ready(fp, "head")
        and _visual_phase_ready(fp)
        and fp.get("detector_version") == DETECTOR_VERSION
    )


def _audio_phase_ready(fp: dict, phase: str) -> bool:
    return (
        fp.get("fingerprint_version") == FINGERPRINT_VERSION
        and bool(fp.get(phase))
        and int(fp.get(f"{phase}_point_count") or len(fp.get(phase) or [])) > 0
    )


def _visual_phase_ready(fp: dict) -> bool:
    return (
        fp.get("tail_visual_version") == VISUAL_FEATURE_VERSION
        and "tail_visual_runs" in fp
    )


def _get_same_season_episode_ids(
    imdb_id: str, season: int, current_episode: int | None = None
) -> list[str]:
    try:
        from app.models import MediaMetadata

        with get_db() as db:
            meta = (
                db.query(MediaMetadata)
                .filter(
                    MediaMetadata.imdb_id == imdb_id,
                    MediaMetadata.media_type == "tv",
                )
                .first()
            )
            raw: dict = (
                meta.raw_data if meta and isinstance(meta.raw_data, dict) else {}
            )
            count = 0
            for season_meta in raw.get("seasons") or []:
                if int(season_meta.get("season_number") or -1) == season:
                    count = int(season_meta.get("episode_count") or 0)
                    break
            if count > 0:
                return [
                    _canonical_episode_id(imdb_id, season, ep)
                    for ep in range(1, count + 1)
                ]
    except Exception as exc:
        logger.warning(
            "season_episode_discovery_failed imdb_id={} season={} reason={}",
            imdb_id,
            season,
            exc,
        )
        return []

    if current_episode is not None:
        max_guess = max(4, current_episode + 2)
        max_guess = min(max_guess, SEASON_PRELEARNING_MAX_FALLBACK_EPISODE_GUESS)
        return [
            _canonical_episode_id(imdb_id, season, ep) for ep in range(1, max_guess + 1)
        ]

    logger.warning(
        "season_episode_discovery_failed imdb_id={} season={} reason=no_metadata",
        imdb_id,
        season,
    )
    return []


def _pick_best_stream_url(sources: list[dict]) -> str | None:
    """Prefer the owner's own library file (stable, full quality) for analysis."""
    if not sources:
        return None

    def score(source: dict) -> tuple[int, int, int]:
        return (
            int(source.get("kind") == "library"),
            int(source.get("variant") == "original"),
            int(source.get("delivery") == "file"),
        )

    best = sorted(sources, key=score, reverse=True)[0]
    return best.get("url") or None


def _resolve_prefetch_stream_url(user_key: str, canonical_video_id: str) -> str | None:
    if not SEASON_PRELEARNING_RESOLVE_STREAMS:
        return None
    try:
        import asyncio
        from app.sources import resolver
        from app.sources.models import MediaRef, PlaybackContext

        parts = canonical_video_id.split(":")
        media = MediaRef(
            type="series", imdb_id=parts[0], season=int(parts[1]), episode=int(parts[2])
        )
        # Source resolution performs network I/O and owns its own short DB stages.
        sources = asyncio.run(
            resolver.resolve(media, PlaybackContext(user_key=user_key))
        )
        return _pick_best_stream_url([s.model_dump() for s in sources])
    except Exception as exc:
        logger.warning(
            "season_prelearning_candidate episode={} action=skipped reason=stream_resolution_failed error={}",
            canonical_video_id,
            exc,
        )
        return None


def _season_marker_readiness(db, imdb_id: str, season: int) -> dict:
    intro_ready = (
        db.query(SegmentMarker.episode)
        .filter_by(imdb_id=imdb_id, season=season, segment_type="intro")
        .filter(SegmentMarker.confidence >= 0.90)
        .distinct()
        .count()
        >= 2
    )
    credits_ready = (
        db.query(SegmentMarker.episode)
        .filter_by(imdb_id=imdb_id, season=season, segment_type="credits")
        .filter(SegmentMarker.confidence >= 0.90)
        .distinct()
        .count()
        >= 2
    )
    return {"intro_ready": intro_ready, "credits_ready": credits_ready}


def _prelearning_skipped_counts(result: dict) -> dict:
    return {k: v for k, v in result.items() if k.startswith("skipped_")}


def _selected_prelearning_candidates(
    episode_ids: list[str], current_episode: int, mode: str
) -> list[str]:
    seen: set[str] = set()
    selected: list[str] = []

    def add(ep_num: int):
        for eid in episode_ids:
            parsed = _parse_canonical_episode_id(eid)
            if parsed and parsed[2] == ep_num and eid not in seen:
                seen.add(eid)
                selected.append(eid)
                return

    # A nearby episode is the cheapest useful companion for intro matching.
    # Credits are now confirmed per episode, so starting with season anchors
    # (E2/E3/E4) only consumes streams and decoder time without improving them.
    for delta in (1, -1, 2, -2):
        ep_num = current_episode + delta
        if ep_num > 0 and ep_num != current_episode:
            add(ep_num)
    if mode == "aggressive":
        for eid in sorted(
            episode_ids, key=lambda x: (_parse_canonical_episode_id(x) or ("", 0, 0))[2]
        ):
            parsed = _parse_canonical_episode_id(eid)
            if parsed and parsed[2] != current_episode and eid not in seen:
                seen.add(eid)
                selected.append(eid)
    return selected


def _claim_prelearning_candidate(
    imdb_id: str, season: int, episode: int, source_hash: str
) -> str:
    """Atomically recheck and mark one resolved candidate pending."""
    with get_db() as db:
        row = (
            db.query(EpisodeFingerprint)
            .filter_by(imdb_id=imdb_id, season=season, episode=episode)
            .first()
        )
        if row and row.status in {"pending", "processing"}:
            return row.status
        if row and row.source_url_hash == source_hash and _is_current_fingerprint(row):
            return "source_current"
        if not row:
            row = EpisodeFingerprint(
                imdb_id=imdb_id,
                season=season,
                episode=episode,
                fingerprint={},
                duration_sec=0.0,
                source_url_hash=source_hash,
                status="pending",
            )
            db.add(row)
        else:
            row.status = "pending"
            row.source_url_hash = source_hash
        db.commit()
    return "claimed"


def queue_season_prelearning(
    user_key: str,
    imdb_id: str,
    season: int,
    current_episode: int,
    reason: str = "after_playback",
    mode: str | None = None,
    limit: int | None = None,
) -> dict:
    runtime_config = get_fingerprint_runtime_config()
    mode = mode or str(
        runtime_config.get("prelearning_mode") or SEASON_PRELEARNING_MODE
    )
    if mode not in {"off", "conservative", "aggressive"}:
        mode = "conservative"
    result = {
        "candidates_considered": 0,
        "queued": 0,
        "skipped_done": 0,
        "skipped_pending": 0,
        "skipped_no_stream": 0,
        "skipped_limit": 0,
        "skipped_ready": 0,
    }
    if (
        not bool(runtime_config.get("prelearning_enabled", SEASON_PRELEARNING_ENABLED))
        or mode == "off"
        or not imdb_id.startswith("tt")
    ):
        result["skipped_ready"] += 1
        return result
    logger.info(
        "season_prelearning_start imdb_id={} season={} current_episode={} mode={} reason={}",
        imdb_id,
        season,
        current_episode,
        mode,
        reason,
    )
    now = utc_now()
    with get_db() as db:
        state = (
            db.query(SeasonLearningState)
            .filter_by(imdb_id=imdb_id, season=season)
            .first()
        )
        if (
            state
            and state.last_prelearning_at
            and reason not in ("admin", "stream_open")
        ):
            if now - state.last_prelearning_at < timedelta(
                minutes=int(
                    runtime_config.get(
                        "cooldown_minutes", SEASON_PRELEARNING_COOLDOWN_MINUTES
                    )
                )
            ):
                result["skipped_ready"] += 1
                logger.info(
                    "season_prelearning_complete queued={} skipped={} reason=cooldown",
                    result["queued"],
                    _prelearning_skipped_counts(result),
                )
                return result
        readiness = _season_marker_readiness(db, imdb_id, season)
        if (
            mode == "conservative"
            and reason not in ("admin", "stream_open")
            and (
                (
                    SEASON_PRELEARNING_STOP_WHEN_BOTH_READY
                    and readiness["intro_ready"]
                    and readiness["credits_ready"]
                )
                or (
                    SEASON_PRELEARNING_STOP_WHEN_INTRO_READY
                    and readiness["intro_ready"]
                )
            )
        ):
            result["skipped_ready"] += 1
            logger.info(
                "season_prelearning_complete queued={} skipped={} reason=markers_ready",
                result["queued"],
                _prelearning_skipped_counts(result),
            )
            return result
        queued_active = (
            db.query(EpisodeFingerprint)
            .filter_by(imdb_id=imdb_id, season=season)
            .filter(EpisodeFingerprint.status.in_(["pending", "processing"]))
            .count()
        )
        max_queued_per_season = int(
            runtime_config.get(
                "max_queued_per_season", SEASON_PRELEARNING_MAX_QUEUED_PER_SEASON
            )
        )
        remaining_queue_slots = max(0, max_queued_per_season - int(queued_active or 0))
        if remaining_queue_slots <= 0:
            result["skipped_limit"] += 1
            return result

    episode_ids = _get_same_season_episode_ids(imdb_id, season, current_episode)
    max_to_queue = (
        limit
        if limit is not None
        else int(
            runtime_config.get(
                "max_additional_episodes", SEASON_PRELEARNING_MAX_ADDITIONAL_EPISODES
            )
        )
    )
    if mode == "aggressive" and limit is None:
        max_to_queue = len(episode_ids)
    max_to_queue = max(0, min(max_to_queue, remaining_queue_slots))
    _raw_selected = _selected_prelearning_candidates(episode_ids, current_episode, mode)
    _fwd_candidates: list[str] = []
    _bwd_candidates: list[str] = []
    for _cid in _raw_selected:
        _p = _parse_canonical_episode_id(_cid)
        if _p is None:
            continue
        if _p[2] > current_episode:
            _fwd_candidates.append(_cid)
        elif _p[2] < current_episode:
            _bwd_candidates.append(_cid)
    selected = _fwd_candidates + _bwd_candidates
    for candidate_id in selected:
        if result["queued"] >= max_to_queue:
            result["skipped_limit"] += 1
            continue
        parsed = _parse_canonical_episode_id(candidate_id)
        if not parsed:
            continue
        c_imdb, c_season, c_episode = parsed
        _job_priority = (
            PRIORITY_LOOKAHEAD if c_episode > current_episode else PRIORITY_ADJACENT
        )
        result["candidates_considered"] += 1
        with get_db() as db:
            row = (
                db.query(EpisodeFingerprint)
                .filter_by(imdb_id=c_imdb, season=c_season, episode=c_episode)
                .first()
            )
            if row and _is_current_fingerprint(row):
                result["skipped_done"] += 1
                logger.info(
                    "season_prelearning_candidate episode={} action=skipped reason=done",
                    candidate_id,
                )
                continue
            if row and row.status in {"pending", "processing"}:
                result["skipped_pending"] += 1
                logger.info(
                    "season_prelearning_candidate episode={} action=skipped reason={}",
                    candidate_id,
                    row.status,
                )
                continue
            if (
                row
                and row.status == "done"
                and not _is_current_fingerprint(row)
                and not SEASON_PRELEARNING_REFINGERPRINT_STALE
            ):
                result["skipped_done"] += 1
                logger.info(
                    "season_prelearning_candidate episode={} action=skipped reason=stale_current_disabled",
                    candidate_id,
                )
                continue

        # Upstream resolution can take many seconds. No candidate/session state
        # is retained while it runs.
        stream_url = _resolve_prefetch_stream_url(user_key, candidate_id)
        if not stream_url:
            result["skipped_no_stream"] += 1
            logger.info(
                "season_prelearning_candidate episode={} action=skipped reason=no_stream",
                candidate_id,
            )
            continue
        source_hash = _source_url_hash(stream_url)

        # Re-read immediately before claiming the row: another stream-open task
        # may have queued the same episode while this task was awaiting upstream.
        try:
            claim = _claim_prelearning_candidate(
                c_imdb, c_season, c_episode, source_hash
            )
        except IntegrityError:
            # A concurrent stream-open inserted the unique episode row after our
            # recheck. Treat that winner exactly like an observed pending row.
            claim = "pending"
        if claim in {"pending", "processing"}:
            result["skipped_pending"] += 1
            logger.info(
                "season_prelearning_candidate episode={} action=skipped reason={}",
                candidate_id,
                claim,
            )
            continue
        if claim == "source_current":
            result["skipped_done"] += 1
            logger.info(
                "season_prelearning_candidate episode={} action=skipped reason=source_current",
                candidate_id,
            )
            continue
        queue_fingerprint_job(candidate_id, stream_url, _job_priority)
        result["queued"] += 1
        logger.info(
            "season_prelearning_candidate episode={} action=queued reason={}",
            candidate_id,
            reason,
        )

    with get_db() as db:
        state = (
            db.query(SeasonLearningState)
            .filter_by(imdb_id=imdb_id, season=season)
            .first()
        )
        if state is None:
            state = SeasonLearningState(imdb_id=imdb_id, season=season)
            db.add(state)
        state.last_prelearning_reason = reason
        state.last_prelearning_at = now
        db.commit()
    logger.info(
        "season_prelearning_complete queued={} skipped={}",
        result["queued"],
        _prelearning_skipped_counts(result),
    )
    return result


def queue_fingerprint_job(
    canonical_id: str,
    url: str,
    priority: int = PRIORITY_BACKGROUND,
    phase: str = "head",
) -> bool:
    """Enqueue a fingerprint job. Returns True if queued, False if already inflight."""
    global _job_seq
    inflight_key = (canonical_id, phase)
    with _inflight_lock:
        if inflight_key in _inflight:
            _superseding_sources[inflight_key] = url
            logger.info(
                "fingerprint_job_source_coalesced episode={} phase={}",
                canonical_id,
                phase,
            )
            return False
        _inflight.add(inflight_key)
    with _job_seq_lock:
        _job_seq += 1
        seq = _job_seq
    _job_queue.put((priority, seq, canonical_id, url, phase))
    return True


def queue_stream_open_fingerprints(
    user_key: str, imdb_id: str, season: int, current_episode: int
) -> None:
    """Fire-and-forget: queue current ep + next N lookahead eps, then broader prelearning."""
    episode_key = f"{imdb_id}:{season}:{current_episode}"
    with get_db() as db:
        _has_markers = (
            db.query(SegmentMarker)
            .filter_by(imdb_id=imdb_id, season=season, episode=current_episode)
            .first()
        ) is not None
        _cur_fp = (
            (
                db.query(EpisodeFingerprint)
                .filter_by(imdb_id=imdb_id, season=season, episode=current_episode)
                .first()
            )
            if not _has_markers
            else None
        )

    if _has_markers:
        logger.info("stream_open_markers_ready episode={}", episode_key)
        current_queued = False
    else:
        current_queued = _cur_fp is None or not _is_current_fingerprint(_cur_fp)

    lookahead_queued: list[int] = []
    for delta in range(1, FINGERPRINT_LOOKAHEAD_EPISODES + 1):
        ep_num = current_episode + delta
        candidate_id = f"{imdb_id}:{season}:{ep_num}"
        with get_db() as db:
            fp = (
                db.query(EpisodeFingerprint)
                .filter_by(imdb_id=imdb_id, season=season, episode=ep_num)
                .first()
            )
            if fp and _is_current_fingerprint(fp):
                continue
            if fp and fp.status in ("pending", "processing"):
                continue
        stream_url = _resolve_prefetch_stream_url(user_key, candidate_id)
        if not stream_url:
            continue
        source_hash = _source_url_hash(stream_url)
        with get_db() as db:
            fp = (
                db.query(EpisodeFingerprint)
                .filter_by(imdb_id=imdb_id, season=season, episode=ep_num)
                .first()
            )
            if not fp:
                fp = EpisodeFingerprint(
                    imdb_id=imdb_id,
                    season=season,
                    episode=ep_num,
                    fingerprint={},
                    duration_sec=0.0,
                    source_url_hash=source_hash,
                    status="pending",
                )
                db.add(fp)
            else:
                fp.status = "pending"
                fp.source_url_hash = source_hash
            db.commit()
        if queue_fingerprint_job(candidate_id, stream_url, PRIORITY_LOOKAHEAD):
            lookahead_queued.append(ep_num)

    logger.info(
        "stream_open_fingerprint_triggered episode={} current_queued={} lookahead_queued={}",
        episode_key,
        current_queued,
        lookahead_queued,
    )
    if SEASON_PRELEARNING_ENABLED:
        queue_season_prelearning(
            user_key, imdb_id, season, current_episode, reason="stream_open"
        )


def get_fingerprint_worker_stats() -> dict:
    with _inflight_lock:
        return {
            "inflight": len(_inflight),
            "queued": _job_queue.qsize(),
            "superseded_sources": len(_superseding_sources),
        }


def _process_job(
    canonical_id: str,
    url: str,
    phase: str = "head",
    priority: int = PRIORITY_BACKGROUND,
):
    imdb_id = ""
    season = 0
    episode = 0
    phase_error: Exception | None = None
    source_reused = False
    phase_started_at = time.monotonic()
    with _inflight_lock:
        was_queued = (canonical_id, phase) in _inflight
    try:
        imdb_id, season, episode = canonical_id.split(":")
        season, episode = int(season), int(episode)
        source_hash = _source_url_hash(url)

        # Snapshot the persisted state, then return the connection before any
        # ffprobe/Chromaprint work. Only plain values cross this boundary.
        with get_db() as db:
            row = (
                db.query(EpisodeFingerprint)
                .filter_by(imdb_id=imdb_id, season=season, episode=episode)
                .first()
            )
            if row and row.source_url_hash != source_hash:
                logger.warning(
                    "fingerprint_url_drift episode={} old_hash={} new_hash={}",
                    canonical_id,
                    row.source_url_hash,
                    source_hash,
                )
            old_source_hash = row.source_url_hash if row else None
            source_changed = bool(old_source_hash and old_source_hash != source_hash)
            existing_payload = dict(row.fingerprint or {}) if row else {}
            previous_identity = existing_payload.get("media_identity")
            duration = safe_float(row.duration_sec) if row else 0.0

        # A signed source URL is expected to rotate. Re-probe only when a URL
        # changed (or no duration is known), entirely outside a DB session.
        if source_changed or duration <= 0:
            duration = _probe_duration(url)
        incoming_sentinel: list[int] | None = None
        if source_changed and previous_identity:
            try:
                incoming_sentinel, _identity_mode = _extract_chromaprint(
                    url,
                    0,
                    min(duration, MEDIA_IDENTITY_SENTINEL_WINDOW_SEC),
                    f"{canonical_id}:identity",
                )
            except Exception as exc:
                # Treat an unreadable sentinel as incompatible, then let the
                # requested phase do the normal full extraction/retry path.
                logger.warning(
                    "fingerprint_source_identity_probe_failed episode={} phase={} reason={}",
                    canonical_id,
                    phase,
                    exc,
                )

        # Mark the phase processing in a fresh short transaction. The commit
        # returns its connection before the long phase extraction below.
        with get_db() as db:
            row = (
                db.query(EpisodeFingerprint)
                .filter_by(imdb_id=imdb_id, season=season, episode=episode)
                .first()
            )
            if not row:
                row = EpisodeFingerprint(
                    imdb_id=imdb_id,
                    season=season,
                    episode=episode,
                    status="processing",
                    fingerprint={},
                    duration_sec=0.0,
                    source_url_hash=source_hash,
                )
                db.add(row)
            incoming_identity = _duration_media_identity(duration, incoming_sentinel)
            source_reused = source_changed and _media_identities_are_compatible(
                previous_identity, incoming_identity
            )
            payload: dict = (
                existing_payload if (not source_changed or source_reused) else {}
            )
            if incoming_identity:
                payload["media_identity"] = incoming_identity
            if source_reused:
                logger.info(
                    "fingerprint_source_reused episode={} phase={} identity={}",
                    canonical_id,
                    phase,
                    incoming_identity,
                )
            elif source_changed:
                logger.info(
                    "fingerprint_source_invalidated episode={} phase={} old_identity={} new_identity={}",
                    canonical_id,
                    phase,
                    previous_identity,
                    incoming_identity,
                )
            phases = dict(payload.get("phases") or {})
            phase_meta = dict(phases.get(phase) or {})
            phase_meta.update(
                {
                    "status": "processing",
                    "version": (
                        VISUAL_FEATURE_VERSION
                        if phase in {"tail", "tail_visual"}
                        else FINGERPRINT_VERSION
                    ),
                    "audio_version": FINGERPRINT_VERSION,
                    "visual_version": (
                        VISUAL_FEATURE_VERSION
                        if phase in {"tail", "tail_visual"}
                        else None
                    ),
                    "error": None,
                }
            )
            phases[phase] = phase_meta
            payload["phases"] = phases
            row.fingerprint = payload
            row.status = "processing"
            row.error_message = None
            row.source_url_hash = source_hash
            db.commit()

            row.duration_sec = duration

            if phase in {"head", "extended_head"}:
                requested_window = (
                    EXTENDED_HEAD_WINDOW if phase == "extended_head" else HEAD_WINDOW
                )
                head_window = min(duration, requested_window)
                already_long_enough = (
                    _audio_phase_ready(payload, "head")
                    and safe_float(payload.get("head_duration_sec")) >= head_window
                )
                if not already_long_enough:
                    logger.info(
                        "fingerprint_phase_start episode={} phase={} duration_sec={:.1f} window_sec={:.1f}",
                        canonical_id,
                        phase,
                        duration,
                        head_window,
                    )
                    head, head_mode = _extract_chromaprint(
                        url, 0, head_window, f"{canonical_id}:{phase}"
                    )
                    payload.update(
                        {
                            "head": head,
                            "head_duration_sec": head_window,
                            "head_point_count": len(head),
                            "chromaprint_sample_duration": CHROMAPRINT_SAMPLE_DURATION,
                            "fingerprint_version": FINGERPRINT_VERSION,
                            "head_extraction_mode": head_mode,
                            "head_features": (
                                _extract_audio_features(url, 0, head_window)
                                if ENABLE_AUDIO_FEATURE_EXTRACTION
                                else []
                            ),
                        }
                    )
                    payload["media_identity"] = _duration_media_identity(duration, head)
                phase_meta.update(
                    {
                        "status": "done",
                        "duration_sec": head_window,
                        "point_count": len(payload.get("head") or []),
                    }
                )
            elif phase in {"tail", "tail_visual"}:
                tail_window = min(duration, TAIL_WINDOW)
                tail_start = max(0.0, duration - TAIL_WINDOW)
                # Credits v2 is intentionally per-episode and visual-first.  Do
                # not decode a second 240-second audio tail or compare it against
                # every other episode: that was the main CPU/network cost and the
                # source of shared-outro false positives.
                payload["tail_duration_sec"] = tail_window
                if not _visual_phase_ready(payload):
                    payload["tail_visual_runs"] = _extract_tail_visual_runs(
                        url, tail_start, tail_window
                    )
                    payload["tail_visual_version"] = VISUAL_FEATURE_VERSION
                    payload["tail_visual_sample_fps"] = CREDITS_VISUAL_SAMPLE_FPS
                    raw_candidate, candidate_reason = _visual_first_credits_candidate(
                        payload["tail_visual_runs"], duration, tail_window
                    )
                    confirmed_candidate = (
                        _confirm_visual_credits_candidate(url, raw_candidate)
                        if raw_candidate
                        else None
                    )
                    payload["credits_v2"] = {
                        "version": VISUAL_FEATURE_VERSION,
                        "candidate": confirmed_candidate,
                        "reason": (
                            candidate_reason
                            if raw_candidate is None
                            else (
                                None
                                if confirmed_candidate
                                else "visual_credits_boundary_not_confirmed"
                            )
                        ),
                    }
                phase_meta.update(
                    {
                        "status": "done",
                        "duration_sec": tail_window,
                        "point_count": 0,
                        "visual_run_count": len(payload.get("tail_visual_runs") or []),
                        "credits_v2_candidate": bool(
                            (payload.get("credits_v2") or {}).get("candidate")
                        ),
                    }
                )
            else:
                raise ValueError(f"unknown fingerprint phase: {phase}")

            phases[phase] = phase_meta
            payload["phases"] = phases
            payload["detector_version"] = DETECTOR_VERSION
            modes = [
                str(payload.get("head_extraction_mode") or ""),
                str(payload.get("tail_extraction_mode") or ""),
            ]
            payload["extraction_mode"] = ",".join(mode for mode in modes if mode)
            payload.setdefault("analysis_metrics", {})[phase] = {
                "elapsed_ms": round((time.monotonic() - phase_started_at) * 1000),
                "decode_window_sec": round(
                    head_window if phase in {"head", "extended_head"} else tail_window,
                    2,
                ),
                "source_reused": source_reused,
            }
            row.fingerprint = payload
            row.source_url_hash = source_hash
            complete = _audio_phase_ready(payload, "head") and _visual_phase_ready(
                payload
            )
            # Extended 600-second head scans are now admin/manual work only.
            # A single failed playback must not fan out into another expensive
            # background decode of every nearby episode.
            row.status = "done" if complete else "partial"
            db.commit()
            logger.info(
                "fingerprint_phase_complete episode={} phase={} aggregate_status={} head_ready={} tail_ready={} visual_ready={} source_reused={}",
                canonical_id,
                phase,
                row.status,
                _audio_phase_ready(payload, "head"),
                _audio_phase_ready(payload, "tail"),
                _visual_phase_ready(payload),
                source_reused,
            )
        _try_learn_season(imdb_id, season)
        # Do not launch a downstream tail phase from an obsolete probe URL.
        # The coalesced source is replayed after this phase releases its in-flight
        # key, so the rendered source becomes the only one that can fan out.
        with _inflight_lock:
            superseding_url = _superseding_sources.pop((canonical_id, phase), None)
            if superseding_url and superseding_url != url:
                _inflight.discard((canonical_id, phase))
        if superseding_url and superseding_url != url:
            queue_fingerprint_job(canonical_id, superseding_url, priority, phase)
            return
        if was_queued and phase == "head":
            tail_priority = (
                PRIORITY_CURRENT_TAIL
                if priority == PRIORITY_STREAM_OPEN
                else PRIORITY_COMPANION_TAIL
            )
            queue_fingerprint_job(canonical_id, url, tail_priority, phase="tail")
    except Exception as e:
        phase_error = e
        logger.error(
            "Fingerprint phase failed for {} phase={}: {}", canonical_id, phase, e
        )
        if imdb_id:
            with get_db() as db:
                row = (
                    db.query(EpisodeFingerprint)
                    .filter_by(imdb_id=imdb_id, season=season, episode=episode)
                    .first()
                )
                if row:
                    payload = dict(row.fingerprint or {})
                    phases = dict(payload.get("phases") or {})
                    phase_meta = dict(phases.get(phase) or {})
                    phase_meta.update(
                        {
                            "status": "failed",
                            "error": str(e)[:500],
                            "version": (
                                VISUAL_FEATURE_VERSION
                                if phase in {"tail", "tail_visual"}
                                else FINGERPRINT_VERSION
                            ),
                        }
                    )
                    phases[phase] = phase_meta
                    payload["phases"] = phases
                    row.fingerprint = payload
                    row.error_message = str(e)[:1000]
                    core_complete = _audio_phase_ready(
                        payload, "head"
                    ) and _visual_phase_ready(payload)
                    row.status = (
                        "done"
                        if phase == "extended_head" and core_complete
                        else (
                            "partial"
                            if _audio_phase_ready(payload, "head")
                            or _audio_phase_ready(payload, "tail")
                            else "failed"
                        )
                    )
                    db.commit()
    finally:
        replay_url = None
        with _inflight_lock:
            replay_url = _superseding_sources.pop((canonical_id, phase), None)
            _inflight.discard((canonical_id, phase))
        # Covers a newer source arriving after the pre-fan-out check above.
        if replay_url and replay_url != url:
            queue_fingerprint_job(canonical_id, replay_url, priority, phase)
    if phase_error is not None:
        return
