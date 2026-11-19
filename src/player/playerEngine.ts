import { isMarkedStartFromBeginning } from './startFromBeginning';
import type Hls from 'hls.js';
import { getDetails, getSegments, reportPlaybackProgress } from '../api/accountClient';
import { startupTimeoutMs } from '../api/streamMeta';
import type {
  MediaAudioFacts,
  EpisodeItem,
  SeasonItem,
  SegmentMarker,
  SegmentsResponse,
  PlaybackSource,
  WebMediaItem,
} from '../api/types';
import {
  clamp,
  displayTitle,
  episodeLabel,
  episodeHasAired,
  episodePlayItem,
  findNextEpisode,
  flattenEpisodes,
  itemImage,
  itemKind,
  playbackProgressKey,
  playbackProgressPrefix,
  safeUrl,
  streamUrl,
} from '../api/mediaUtils';
import type { FailureSignal } from './recovery';
import { retiredTextTracks } from './serverSession';

// ---------------------------------------------------------------------------
// Playback engines. Exactly three ways to put a source on screen:
//   - native <video src>       (direct play: MP4/WebM the browser decodes)
//   - hls.js over MSE          (HLS sources and server-prepared fMP4 HLS)
//   - native HLS (Safari)      (same playlists, played by the element itself)
// There are no client-side converters: containers or codecs the browser can't
// open are prepared by the server (see playbackPlan.ts / remux sessions).
// ---------------------------------------------------------------------------

export type PlayerMarkers = {
  introStart?: number;
  introEnd?: number;
  /** Only filled while ENABLE_RECAP_SEGMENTS is on (Apple ships it off too). */
  recapStart?: number;
  recapEnd?: number;
  creditsStart?: number;
  creditsEnd?: number;
  /** A server "next episode" marker, when one is served (none today: see nextEpisodePromptStart). */
  nextEpisodeStart?: number;
};

/** Recap segments are parsed but not offered, as in the Apple apps (SkipSegments.swift). */
export const ENABLE_RECAP_SEGMENTS = false;

/** Up Next countdown, as PlaybackPromptRules.autoAdvanceDuration. */
export const UP_NEXT_COUNTDOWN_SECONDS = 30;
/** Without any marker, Up Next shows this long before the end of a measured duration. */
export const UP_NEXT_FALLBACK_LEAD_SECONDS = 30;

/**
 * When the Up Next card appears, in priority order: the server's next-episode
 * marker, then its credits marker, then 30 s before the end. The duration
 * fallback only applies to a duration measured from the media itself — a
 * runtime estimate can put the card in the middle of a scene.
 */
export function nextEpisodePromptStart(params: {
  nextEpisodeStart?: number;
  creditsStart?: number;
  duration?: number;
  durationReliable?: boolean;
}): number | null {
  const { nextEpisodeStart, creditsStart, duration, durationReliable } = params;
  if (nextEpisodeStart != null && Number.isFinite(nextEpisodeStart)) return nextEpisodeStart;
  if (creditsStart != null && Number.isFinite(creditsStart)) return creditsStart;
  if (durationReliable && duration != null && Number.isFinite(duration) && duration > UP_NEXT_FALLBACK_LEAD_SECONDS) {
    return duration - UP_NEXT_FALLBACK_LEAD_SECONDS;
  }
  return null;
}

export function shouldShowNextEpisode(params: {
  hasNextEpisode: boolean;
  current: number;
  nextEpisodeStart?: number;
  creditsStart?: number;
  duration?: number;
  durationReliable?: boolean;
  mediaEnded: boolean;
}): boolean {
  const { hasNextEpisode, current, mediaEnded } = params;
  if (!hasNextEpisode) return false;
  if (mediaEnded) return true;
  const start = nextEpisodePromptStart(params);
  return start != null && Number.isFinite(current) && current >= start;
}

/**
 * Feedback to send when the viewer accepts Up Next. Only a credits marker the
 * viewer is inside is confirmed (Apple: onServerNextEpisodeAccepted); the server
 * takes no feedback for other types, and a duration fallback confirms nothing.
 */
export function nextEpisodeAcceptFeedback(markers: PlayerMarkers, current: number): 'credits' | null {
  if (markers.nextEpisodeStart != null) return null;
  if (markers.creditsStart == null || !(current >= markers.creditsStart)) return null;
  return 'credits';
}

/** Never offer an unaired episode as next (PlaybackPromptRules.nextEpisode). */
export function airedNextEpisode<T extends { air_date?: unknown }>(next: T | null | undefined, now: Date = new Date()): T | null {
  return next && episodeHasAired(next, now) ? next : null;
}

export type TrackOption = {
  id: string;
  label: string;
  detail?: string;
  selected: boolean;
  disabled?: boolean;
};

export type SettingsTab = 'display' | 'sources' | 'audio' | 'subtitles' | 'speed' | 'episodes';

type NativeAudioTrack = {
  label?: string;
  language?: string;
  enabled?: boolean;
};

type NativeAudioTrackList = ArrayLike<NativeAudioTrack> & {
  addEventListener?: (type: string, listener: EventListener) => void;
  removeEventListener?: (type: string, listener: EventListener) => void;
};

type NormalizedSubtitleEntry = {
  id: string;
  url: string;
  label: string;
  lang: string;
  /** SRT needs converting to WebVTT before a <track> can show it. */
  srt: boolean;
};

// How often (ms) to persist playback progress to the server while watching.
export const PROGRESS_REPORT_THROTTLE_MS = 10000;
// Fraction of a title that counts as "watched" (clears resume / marks complete).
export const WATCHED_THRESHOLD = 0.9;
// Seconds skipped by the rewind/forward controls and keyboard shortcuts.
export const SEEK_STEP_SECONDS = 10;

/** Presses closer together than this keep accelerating the arrow-key seek (tvOS remote). */
export const SEEK_REPEAT_WINDOW_MS = 400;

/**
 * tvOS `directionalScrubStep`: one press moves 10 s (or 0.6% of a long film);
 * quick repeats accelerate, about 20 s by the 3rd and 60 s by the 8th, capped
 * at 45 s or 2.5% of the film.
 */
export function directionalSeekStep(repeatCount: number, duration: number): number {
  const length = Number.isFinite(duration) && duration > 0 ? duration : 0;
  const repeats = Math.min(Math.max(repeatCount, 0), 8);
  const base = Math.max(SEEK_STEP_SECONDS, length * 0.006);
  const accelerated = base * (1 + Math.pow(repeats * 0.45, 1.35));
  return Math.min(accelerated, Math.max(45, length * 0.025));
}
/** Lock-screen / headset skip, as the Apple apps' remote command center (keyboard stays 10 s). */
export const MEDIA_SESSION_SEEK_SECONDS = 15;

// Same-origin byte proxy (server: GET|HEAD /account/api/playback/proxy). Wraps a
// remote stream URL so the browser reads its bytes from Curatio's own origin,
// sidestepping hosts with broken/duplicated CORS headers and giving Safari proper
// 206 byte-range responses. Pure passthrough — no server transcode.
export function proxiedUrl(rawUrl: string): string {
  return `${window.location.origin}/account/api/playback/proxy?url=${encodeURIComponent(rawUrl)}`;
}

/** A signed `/media/` URL is same-origin when the server's base_url matches this page: no proxy needed. */
export function isSameOriginUrl(rawUrl: string): boolean {
  try {
    return new URL(rawUrl, window.location.origin).origin === window.location.origin;
  } catch {
    return false;
  }
}

/**
 * A prepared session's playlist as hls.js should ask for it: `?hold=1` makes the
 * server (and the desktop engine) long-poll, answering the moment the next
 * segment exists. Without it hls.js polls once per segment duration and, with
 * 10 s segments, stalls after the first one. Other HLS sources are untouched.
 */
export function heldPlaylistUrl(url: string): string {
  if (!/\/account\/api\/playback\/remux\/[^/]+\/index\.m3u8$/.test(url.split('?')[0]) || /[?&]hold=/.test(url)) return url;
  return `${url}${url.includes('?') ? '&' : '?'}hold=1`;
}

export const HLS_NETWORK_RECOVERY_LIMIT = 2;
export const HLS_MEDIA_RECOVERY_LIMIT = 3;

/** Fatal HLS errors only get a bounded number of in-place recovery attempts. */
export function canRecoverFatalHls(kind: 'network' | 'media', attempts: number): boolean {
  return attempts < (kind === 'network' ? HLS_NETWORK_RECOVERY_LIMIT : HLS_MEDIA_RECOVERY_LIMIT);
}

/** Which engine family to use for a load. */
export type EngineMode = 'direct' | 'hls';

/** Engine failure callback: a message ('' clears) plus the classified signal. */
export type EngineFailure = (message: string, signal?: FailureSignal) => void;

// One AbortController per element load: superseding a load removes every
// listener the previous one registered (no leftover ready/seek listeners).
const loadControllers = new WeakMap<HTMLVideoElement, AbortController>();

function beginLoad(video: HTMLVideoElement): AbortSignal {
  loadControllers.get(video)?.abort();
  const controller = new AbortController();
  loadControllers.set(video, controller);
  return controller.signal;
}

/**
 * Attach a source to the element. Argument order is load-bearing for tests:
 * index 6 is the start position (local element time), 7 the failure callback
 * and 8 the ready callback.
 */
export async function loadStreamIntoVideo(
  stream: PlaybackSource,
  video: HTMLVideoElement | null,
  hlsRef: React.MutableRefObject<Hls | null>,
  autoplay: boolean,
  mode: EngineMode,
  preferProxy: boolean,
  startSeconds: number,
  onFailure: EngineFailure,
  onReady: () => void,
  onTracksChanged: () => void,
) {
  if (!video) return;
  cleanupPlayback(video, hlsRef);
  const loadToken = `${Date.now()}:${Math.random().toString(36).slice(2)}`;
  video.dataset.curatioLoadToken = loadToken;
  const signal = beginLoad(video);
  const url = streamUrl(stream);
  try {
    // Never awaited: subtitle sidecars load in parallel with the media itself,
    // so a slow/hung subtitle host can't delay (or block) playback start.
    void attachExternalSubtitles(stream, video, loadToken).catch(() => undefined);
    if (!url) {
      onFailure('This source does not include a browser-playable URL.', { kind: 'no-url' });
      return;
    }
    if (mode === 'hls' && canPlayNativeHls(video)) {
      loadNativeVideoSource(video, url, autoplay, onReady, loadToken, Math.max(0, startSeconds), signal);
      return;
    }
    if (mode === 'hls' || isHlsUrl(url)) {
      // Load hls.js on demand so the ~200KB library stays out of the initial
      // bundle and is only fetched the first time an HLS source is played.
      const { default: Hls } = await import('hls.js');
      if (video.dataset.curatioLoadToken !== loadToken) return;
      if (!Hls.isSupported()) {
        if (canPlayNativeHls(video)) {
          loadNativeVideoSource(video, url, autoplay, onReady, loadToken, Math.max(0, startSeconds), signal);
        } else {
          onFailure('This browser cannot play HLS streams.', { kind: 'hls', fatalType: 'other' });
        }
        return;
      }
      attachHlsJs(Hls, stream, heldPlaylistUrl(url), video, hlsRef, autoplay, startSeconds, loadToken, onFailure, onReady, onTracksChanged);
      return;
    }
    loadNativeVideoSource(video, preferProxy && !isSameOriginUrl(url) ? proxiedUrl(url) : url, autoplay, onReady, loadToken, Math.max(0, startSeconds), signal);
  } catch (err) {
    onFailure(err instanceof Error ? err.message : 'Unable to load this source.', {
      kind: 'transport',
      message: err instanceof Error ? err.message : 'Unable to load this source.',
    });
  }
}

function attachHlsJs(
  Hls: typeof import('hls.js').default,
  stream: PlaybackSource,
  url: string,
  video: HTMLVideoElement,
  hlsRef: React.MutableRefObject<Hls | null>,
  autoplay: boolean,
  startSeconds: number,
  loadToken: string,
  onFailure: EngineFailure,
  onReady: () => void,
  onTracksChanged: () => void,
) {
  const firstByteMs = Math.max(10000, startupTimeoutMs(stream));
  const hlsConfig: Partial<import('hls.js').HlsConfig> = {
    enableWorker: true,
    // Resume to the saved offset (0 ⇒ beginning). Pinning the start also keeps
    // hls.js off the galloping live edge of EVENT playlists (no #EXT-X-ENDLIST),
    // which would otherwise leave blank video while audio drains.
    startPosition: Math.max(0, startSeconds),
    renderTextTracksNatively: true,
    lowLatencyMode: false,
    maxBufferLength: 60,
    maxMaxBufferLength: 240,
    maxBufferSize: 120 * 1000 * 1000,
    backBufferLength: 90,
    maxBufferHole: 0.5,
    highBufferWatchdogPeriod: 2,
    nudgeOffset: 0.2,
    nudgeMaxRetry: 6,
    startFragPrefetch: true,
    abrBandWidthFactor: 0.95,
    abrBandWidthUpFactor: 0.7,
    fragLoadPolicy: {
      default: {
        maxTimeToFirstByteMs: firstByteMs,
        maxLoadTimeMs: 120000,
        timeoutRetry: { maxNumRetry: 4, retryDelayMs: 0, maxRetryDelayMs: 0 },
        // A 404 fragment on a prepared stream means it was pruned; retrying
        // can't bring it back, so let it surface quickly.
        errorRetry: {
          maxNumRetry: 6,
          retryDelayMs: 1000,
          maxRetryDelayMs: 8000,
          backoff: 'exponential',
          shouldRetry: (retryConfig, retryCount, _isTimeout, loaderResponse) => {
            if (loaderResponse?.code === 404 || loaderResponse?.code === 401 || loaderResponse?.code === 403) return false;
            return Boolean(retryConfig) && retryCount < (retryConfig?.maxNumRetry ?? 0);
          },
        },
      },
    },
    playlistLoadPolicy: {
      default: {
        maxTimeToFirstByteMs: firstByteMs,
        maxLoadTimeMs: Math.max(20000, startupTimeoutMs(stream)),
        timeoutRetry: { maxNumRetry: 2, retryDelayMs: 0, maxRetryDelayMs: 0 },
        errorRetry: { maxNumRetry: 4, retryDelayMs: 1000, maxRetryDelayMs: 8000, backoff: 'exponential' },
      },
    },
    manifestLoadPolicy: {
      default: {
        maxTimeToFirstByteMs: firstByteMs,
        maxLoadTimeMs: Math.max(20000, startupTimeoutMs(stream)),
        timeoutRetry: { maxNumRetry: 2, retryDelayMs: 0, maxRetryDelayMs: 0 },
        errorRetry: { maxNumRetry: 4, retryDelayMs: 1000, maxRetryDelayMs: 8000, backoff: 'exponential' },
      },
    },
  };
  const hls = new Hls(hlsConfig);
  hlsRef.current = hls;
  const syncEvents = [
    Hls.Events.MANIFEST_PARSED,
    Hls.Events.AUDIO_TRACKS_UPDATED,
    Hls.Events.AUDIO_TRACK_SWITCHED,
    Hls.Events.SUBTITLE_TRACKS_UPDATED,
    Hls.Events.SUBTITLE_TRACKS_CLEARED,
    Hls.Events.SUBTITLE_TRACK_SWITCH,
    Hls.Events.SUBTITLE_TRACK_LOADED,
  ];
  syncEvents.forEach((eventName) => hls.on(eventName, onTracksChanged));
  hls.loadSource(url);
  hls.attachMedia(video);
  hls.on(Hls.Events.MANIFEST_PARSED, () => {
    if (video.dataset.curatioLoadToken !== loadToken) return;
    onReady();
    if (autoplay) void attemptPlay(video);
  });
  let mediaRecoveryAttempts = 0;
  let networkRecoveryAttempts = 0;
  hls.on(Hls.Events.ERROR, (_event, data) => {
    if (!data.fatal || video.dataset.curatioLoadToken !== loadToken) return;
    const httpStatus = (data as { response?: { code?: number } }).response?.code ?? null;
    const terminalHttp = httpStatus === 401 || httpStatus === 403 || httpStatus === 404 || httpStatus === 410;
    // hls.js's own bounded recovery first, unless the server said the bytes are gone.
    if (!terminalHttp && data.type === Hls.ErrorTypes.NETWORK_ERROR && canRecoverFatalHls('network', networkRecoveryAttempts)) {
      networkRecoveryAttempts += 1;
      // An explicit position: with none, hls.js restarts a growing playlist at its live edge.
      hls.startLoad(video.currentTime > 0 ? video.currentTime : Math.max(0, startSeconds));
      return;
    }
    if (data.type === Hls.ErrorTypes.MEDIA_ERROR && canRecoverFatalHls('media', mediaRecoveryAttempts)) {
      mediaRecoveryAttempts += 1;
      hls.recoverMediaError();
      return;
    }
    const fatalType = data.type === Hls.ErrorTypes.NETWORK_ERROR
      ? 'network'
      : data.type === Hls.ErrorTypes.MEDIA_ERROR ? 'media' : 'other';
    onFailure('This HLS source reported a fatal playback error.', {
      kind: 'hls', fatalType, httpStatus, details: String(data.details ?? ''), message: data.error?.message,
    });
  });
}

export function isHlsUrl(url: string) {
  const lower = url.toLowerCase();
  if (lower.includes('.m3u8')) return true;
  try {
    return new URL(url, window.location.href).pathname.toLowerCase().endsWith('.m3u8');
  } catch {
    return false;
  }
}

export function canPlayNativeHls(video: HTMLVideoElement) {
  return Boolean(video.canPlayType('application/vnd.apple.mpegurl') || video.canPlayType('application/x-mpegURL'));
}

// Whether the element's seekable range actually covers `time` yet.
function seekableCovers(video: HTMLVideoElement, time: number): boolean {
  try {
    const ranges = video.seekable;
    for (let i = 0; i < ranges.length; i += 1) {
      if (time >= ranges.start(i) - 0.5 && time <= ranges.end(i) + 0.5) return true;
    }
  } catch {
    // `seekable` can throw before metadata is known.
  }
  return false;
}

export function loadNativeVideoSource(
  video: HTMLVideoElement,
  url: string,
  autoplay: boolean,
  onReady: () => void,
  loadToken: string,
  startSeconds = 0,
  signal?: AbortSignal,
) {
  let resolved = false;
  const listen = { signal } as AddEventListenerOptions;
  // Apply the resume seek. Idempotent: a no-op if the playhead is already there.
  const applyStart = () => {
    if (startSeconds <= 0 || Math.abs(video.currentTime - startSeconds) <= 1) return;
    try {
      video.currentTime = startSeconds;
    } catch {
      // currentTime can throw before the media is seekable; `reassert` covers it.
    }
  };
  const ready = () => {
    if (resolved || video.dataset.curatioLoadToken !== loadToken) return;
    resolved = true;
    onReady();
    applyStart();
    if (autoplay) void attemptPlay(video);
  };
  // Re-assert the resume seek once the seekable range covers the target — some
  // browsers clamp a currentTime set in loadedmetadata before any range exists.
  const reassert = () => {
    if (video.dataset.curatioLoadToken !== loadToken) return;
    if (startSeconds > 0 && Math.abs(video.currentTime - startSeconds) > 1.5 && seekableCovers(video, startSeconds)) {
      applyStart();
      video.removeEventListener('durationchange', reassert);
    }
  };
  video.addEventListener('loadedmetadata', ready, { ...listen, once: true });
  video.addEventListener('canplay', ready, { ...listen, once: true });
  if (startSeconds > 0) video.addEventListener('durationchange', reassert, listen);
  video.src = url;
  video.load();
}

export function cleanupPlayback(
  video: HTMLVideoElement | null,
  hlsRef: React.MutableRefObject<Hls | null>,
) {
  hlsRef.current?.destroy();
  hlsRef.current = null;
  if (video) {
    loadControllers.get(video)?.abort();
    loadControllers.delete(video);
    video.pause();
    delete video.dataset.curatioLoadToken;
    Array.from(video.querySelectorAll<HTMLTrackElement>('track[data-curatio-subtitle]')).forEach((track) => {
      if (track.dataset.objectUrl) URL.revokeObjectURL(track.dataset.objectUrl);
      track.remove();
    });
    video.removeAttribute('src');
    video.load();
  }
}

export function attachNativeTrackListeners(video: HTMLVideoElement | null, onTracksChanged: () => void) {
  if (!video) return undefined;
  const audioTracks = nativeAudioTracks(video);
  const textTracks = video.textTracks;
  const events = ['addtrack', 'removetrack', 'change'];
  events.forEach((eventName) => {
    audioTracks?.addEventListener?.(eventName, onTracksChanged);
    textTracks?.addEventListener(eventName, onTracksChanged);
  });
  const mediaEvents = ['loadedmetadata', 'canplay', 'durationchange'];
  mediaEvents.forEach((eventName) => video.addEventListener(eventName, onTracksChanged));
  onTracksChanged();
  return () => {
    events.forEach((eventName) => {
      audioTracks?.removeEventListener?.(eventName, onTracksChanged);
      textTracks?.removeEventListener(eventName, onTracksChanged);
    });
    mediaEvents.forEach((eventName) => video.removeEventListener(eventName, onTracksChanged));
  };
}

function nativeAudioTracks(video: HTMLVideoElement | null): NativeAudioTrackList | null {
  return video ? ((video as unknown as { audioTracks?: NativeAudioTrackList }).audioTracks ?? null) : null;
}

function subtitleEntriesFromStream(stream: PlaybackSource): NormalizedSubtitleEntry[] {
  const subtitles = Array.isArray(stream.subtitles) ? stream.subtitles : [];
  const seen = new Set<string>();
  return subtitles
    .map((entry, index) => {
      const url = safeUrl(entry.url);
      const lang = String(entry.lang ?? 'und').trim() || 'und';
      const label = String(entry.label ?? lang.toUpperCase()).trim();
      return {
        id: String(entry.id ?? `${lang}:${url || index}`),
        url,
        label: label || `Subtitle ${index + 1}`,
        lang,
        srt: String(entry.format ?? '').toLowerCase() === 'srt' || url.toLowerCase().split('?')[0].endsWith('.srt'),
      };
    })
    .filter((entry) => {
      if (!entry.url || seen.has(entry.url)) return false;
      seen.add(entry.url);
      return true;
    });
}

// Bound each SRT sidecar fetch so one hung subtitle host can't hold tracks
// (or, historically, playback start) hostage.
const SUBTITLE_FETCH_TIMEOUT_MS = 5000;

async function attachExternalSubtitles(stream: PlaybackSource, video: HTMLVideoElement, loadToken: string) {
  const entries = subtitleEntriesFromStream(stream);
  await Promise.all(
    entries.map(async (entry) => {
      const track = document.createElement('track');
      track.kind = 'subtitles';
      track.label = entry.label;
      track.srclang = entry.lang;
      track.dataset.curatioSubtitle = entry.id;
      let src = entry.url;
      if (entry.srt) {
        try {
          const response = await fetch(entry.url, { signal: AbortSignal.timeout(SUBTITLE_FETCH_TIMEOUT_MS) });
          if (response.ok) {
            const blob = new Blob([srtToVtt(await response.text())], { type: 'text/vtt' });
            src = URL.createObjectURL(blob);
            track.dataset.objectUrl = src;
          }
        } catch {
          // The browser may block cross-origin subtitle conversion; keep the source URL.
        }
      }
      // This runs concurrently with (not before) the media load — a failover or
      // source swap may have superseded us while the fetch was in flight, and a
      // stale track would attach to the wrong source.
      if (video.dataset.curatioLoadToken !== loadToken) {
        if (track.dataset.objectUrl) URL.revokeObjectURL(track.dataset.objectUrl);
        return;
      }
      track.src = src;
      track.default = false;
      video.append(track);
    }),
  );
}

function srtToVtt(source: string) {
  return `WEBVTT\n\n${source.replace(/\r+/g, '').replace(/(\d\d:\d\d:\d\d),(\d{3})/g, '$1.$2')}`;
}

/**
 * The title's total length in movie time.
 *
 * A directly-played file reports it itself (`video.duration`). A prepared
 * session (server remux or the desktop engine) does not: hls.js sets the
 * element's duration to the end of what has been prepared SO FAR, a number that
 * keeps growing while the session runs. Using it as the total made the timeline
 * wrong in every way (a duration that crept upward, 50% meaning "half of what
 * is ready", the next-episode prompt and "finished" firing near the prepared
 * edge). For a prepared session the total is `fallbackDuration`: the probed
 * length of the source. Only without one is the prepared edge shown.
 */
export function effectiveDuration(video: HTMLVideoElement | null, fallbackDuration = 0, offset = 0, prepared = false): number {
  const fallback = Number.isFinite(fallbackDuration) && fallbackDuration > 0 ? fallbackDuration : 0;
  if (!video) return fallback;
  const elementDuration = Number.isFinite(video.duration) && video.duration > 0 ? video.duration : 0;
  if (prepared) {
    if (fallback > 0) return fallback;
    return elementDuration > 0 ? Math.max(0, offset) + elementDuration : 0;
  }
  if (elementDuration > 0) return Math.max(0, offset) + elementDuration;
  if (fallback > 0) return fallback;
  try {
    if (video.seekable.length) {
      const end = video.seekable.end(video.seekable.length - 1);
      if (Number.isFinite(end) && end > 0) return end;
    }
  } catch {
    // `seekable` can throw before metadata is known.
  }
  return 0;
}

// Low-level local-time seek. Prefers `fastSeek` (keyframe-accurate) — far
// snappier for HLS than an exact byte-precise seek — and falls back to setting
// `currentTime`. Callers clamp; `offset` (movie-time base of the current remux)
// is unrelated here since this operates in the element's own timeline.
export function applySeek(video: HTMLVideoElement, seconds: number) {
  const target = Math.max(0, seconds);
  if (typeof video.fastSeek === 'function') {
    try {
      video.fastSeek(target);
      return;
    } catch {
      // Some browsers throw before metadata; fall back to a precise seek.
    }
  }
  video.currentTime = target;
}

// All timeline math is in *movie time*: `offset` is the movie-second that the
// element's `currentTime === 0` maps to (non-zero for a seek-restarted remux).
export function updatePlaybackTimeline(
  video: HTMLVideoElement | null,
  setProgress: (value: number) => void,
  setDuration: (value: number) => void,
  setBuffered: (value: number) => void,
  fallbackDuration = 0,
  offset = 0,
  setDurationIsMediaDerived?: (value: boolean) => void,
  /** A prepared session: see `effectiveDuration`. `exact` = its total came from a probe of the file. */
  prepared: { exact: boolean } | null = null,
) {
  if (!video) return;
  const duration = effectiveDuration(video, fallbackDuration, offset, Boolean(prepared));
  // "Media-derived" = a total that can be trusted to end the title on: the
  // element's own duration for a direct file, the probed length for a prepared
  // session. A metadata runtime guess is neither.
  setDurationIsMediaDerived?.(prepared ? prepared.exact && duration > 0 : Number.isFinite(video.duration) && video.duration > 0);
  const current = offset + (Number.isFinite(video.currentTime) ? video.currentTime : 0);
  setDuration(duration);
  setProgress(duration > 0 ? clamp(current / duration, 0, 1) : 0);
  try {
    const localEnd = video.buffered.length ? video.buffered.end(video.buffered.length - 1) : 0;
    const end = offset + localEnd;
    setBuffered(duration > 0 ? clamp(end / duration, 0, 1) : 0);
  } catch {
    setBuffered(0);
  }
}

export async function togglePlay(video: HTMLVideoElement | null) {
  if (!video) return;
  if (video.paused) await attemptPlay(video);
  else video.pause();
}

/** Attempt playback without confusing an autoplay-policy rejection for a bad source. */
export async function attemptPlay(video: HTMLVideoElement): Promise<'playing' | 'blocked' | 'failed'> {
  try {
    await video.play();
    delete video.dataset.curatioAutoplayBlocked;
    return 'playing';
  } catch (err) {
    if ((err as { name?: string } | null)?.name === 'NotAllowedError') {
      video.dataset.curatioAutoplayBlocked = 'true';
      video.dispatchEvent(new Event('curatioautoplayblocked'));
      return 'blocked';
    }
    return 'failed';
  }
}

export async function toggleFullscreen(player: HTMLElement | null) {
  try {
    if (!document.fullscreenElement) await (player ?? document.documentElement).requestFullscreen();
    else await document.exitFullscreen();
  } catch {
    // Fullscreen can fail when the browser disallows it.
  }
}

export function updateMediaSession(
  item: WebMediaItem,
  video: HTMLVideoElement | null,
  isPlaying: boolean,
  offset = 0,
  seekMovieTime?: (seconds: number) => void,
  onNextTrack?: (() => void) | null,
) {
  if (!('mediaSession' in navigator)) return;
  try {
    navigator.mediaSession.setActionHandler('nexttrack', onNextTrack ?? null);
  } catch {
    // Not every browser knows 'nexttrack'.
  }
  try {
    const artwork = itemImage(item, true, 'w500');
    navigator.mediaSession.metadata = new MediaMetadata({
      title: displayTitle(item),
      artist: episodeLabel(item) || (itemKind(item) === 'series' ? 'Series' : 'Movie'),
      artwork: artwork ? [{ src: artwork, sizes: '500x750', type: 'image/jpeg' }] : undefined,
    });
    navigator.mediaSession.playbackState = isPlaying ? 'playing' : 'paused';
    navigator.mediaSession.setActionHandler('play', () => {
      if (video) void attemptPlay(video);
    });
    navigator.mediaSession.setActionHandler('pause', () => video?.pause());
    const seekRelative = (seconds: number) => {
      if (!video) return;
      if (seekMovieTime) seekMovieTime(Math.max(0, offset + video.currentTime + seconds));
      else applySeek(video, Math.max(0, video.currentTime + seconds));
    };
    // Always 15 s, as the Apple apps: some platforms send their own seekOffset (often 10 s).
    navigator.mediaSession.setActionHandler('seekbackward', () => seekRelative(-MEDIA_SESSION_SEEK_SECONDS));
    navigator.mediaSession.setActionHandler('seekforward', () => seekRelative(MEDIA_SESSION_SEEK_SECONDS));
    navigator.mediaSession.setActionHandler('seekto', (details) => {
      if (details.seekTime == null || !video) return;
      if (seekMovieTime) seekMovieTime(details.seekTime);
      else if (details.fastSeek && 'fastSeek' in video) video.fastSeek(details.seekTime);
      else video.currentTime = details.seekTime;
    });
  } catch {
    // Media Session support varies across browsers.
  }
}

// Kept in mediaUtils so non-player code (mark watched, Continue Watching
// removal) can clear saved positions without loading the player engine.
export { playbackProgressKey };

export function savedPlaybackState(item: WebMediaItem) {
  try {
    const raw = localStorage.getItem(playbackProgressKey(item));
    if (!raw) return { position: 0, duration: 0 };
    const data = JSON.parse(raw) as { position?: number; duration?: number };
    return {
      position: Number(data.position ?? 0),
      duration: Number(data.duration ?? 0),
    };
  } catch {
    return { position: 0, duration: 0 };
  }
}

export function savedProgressIsResumable(item: WebMediaItem, saved: { position: number; duration: number }) {
  const position = Number(saved.position || 0);
  if (!Number.isFinite(position) || position <= 30) return false;
  if (progressPercentOf(item) >= 90) return false;
  const duration = Number(saved.duration || item.duration || item.runtime_seconds || item.runtimeSeconds || 0);
  if (Number.isFinite(duration) && duration > 0 && position / duration >= WATCHED_THRESHOLD) return false;
  return true;
}

function progressPercentOf(item: WebMediaItem) {
  const explicit = Number(item?.progress_percent ?? item?.progressPercent);
  if (Number.isFinite(explicit) && explicit > 0) return clamp(explicit, 0, 100);
  const position = Number(item?.position ?? 0);
  const duration = Number(item?.duration ?? 0);
  if (duration > 0) return clamp(Math.round((position / duration) * 100), 0, 100);
  return 0;
}

export function clearSavedPlaybackState(item: WebMediaItem) {
  try {
    localStorage.removeItem(playbackProgressKey(item));
  } catch {
    // Ignore storage failures.
  }
}

function normalizeMarkerSeconds(value: unknown) {
  const n = Number(value);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

const NEXT_EPISODE_SEGMENT_TYPES = new Set(['next_episode', 'next-episode', 'nextEpisode', 'up_next']);

export function markersFromSegments(segments?: SegmentMarker[], options: { recap?: boolean } = {}) {
  const recapEnabled = options.recap ?? ENABLE_RECAP_SEGMENTS;
  const markers: PlayerMarkers = {};
  (segments ?? []).forEach((segment) => {
    const start = normalizeMarkerSeconds(segment.start ?? segment.start_sec ?? segment.startSeconds);
    const type = String(segment.type ?? '');
    // A next-episode marker is a point; its end is optional.
    if (NEXT_EPISODE_SEGMENT_TYPES.has(type)) {
      if (start != null) markers.nextEpisodeStart = start;
      return;
    }
    const end = normalizeMarkerSeconds(segment.end ?? segment.end_sec ?? segment.endSeconds);
    if (start == null || end == null || end <= start) return;
    if (type === 'intro') {
      markers.introStart = start;
      markers.introEnd = end;
    } else if (type === 'recap') {
      if (!recapEnabled) return;
      markers.recapStart = start;
      markers.recapEnd = end;
    } else if (type === 'credits') {
      markers.creditsStart = start;
      markers.creditsEnd = end;
    }
  });
  return markers;
}

function markersFromItem(item: WebMediaItem) {
  const introStart = normalizeMarkerSeconds(item.intro_start ?? item.skip_intro_start);
  const introEnd = normalizeMarkerSeconds(item.intro_end ?? item.skip_intro_end);
  const creditsStart = normalizeMarkerSeconds(item.credits_start ?? item.outro_start);
  const creditsEnd = normalizeMarkerSeconds(item.credits_end);
  return {
    ...(introStart != null && introEnd != null ? { introStart, introEnd } : {}),
    ...(creditsStart != null ? { creditsStart } : {}),
    ...(creditsEnd != null ? { creditsEnd } : {}),
  };
}

export async function preparePlaybackMetadata(
  item: WebMediaItem,
  signal?: AbortSignal,
): Promise<{
  markers: PlayerMarkers;
  nextEpisode: WebMediaItem | null;
  segmentResponse?: SegmentsResponse;
  /** Series only: seasons with episodes, for the player's Episodes sheet. */
  seasons?: SeasonItem[];
}> {
  let markers = markersFromItem(item);
  let seasons: SeasonItem[] | undefined;
  let next = airedNextEpisode((item.next_episode ?? item.nextEpisode ?? null) as WebMediaItem | null);
  let segmentResponse: SegmentsResponse | undefined;
  if (itemKind(item) === 'series') {
    // Item-level series markers may belong to a previous metadata snapshot. The
    // server response is the single source of truth, including an empty list.
    markers = {};
    const [detailsResult, segmentsResult] = await Promise.allSettled([
      getDetails(item, signal),
      refreshPlaybackSegments(item, signal),
    ]);
    if (detailsResult.status === 'fulfilled') {
      next = next ?? findNextEpisode(item, detailsResult.value.seasons);
      seasons = detailsResult.value.seasons;
    }
    if (segmentsResult.status === 'fulfilled') {
      segmentResponse = segmentsResult.value;
      markers = markersFromSegments(segmentResponse.segments);
    }
  }
  return { markers, nextEpisode: next, segmentResponse, seasons };
}

export async function refreshPlaybackSegments(
  item: WebMediaItem,
  signal?: AbortSignal,
): Promise<SegmentsResponse> {
  if (
    itemKind(item) !== 'series'
    || !(item.imdb_id || item.video_id)
    || item.season == null
    || item.episode == null
  ) {
    return { segments: [], fingerprint_status: 'unavailable' };
  }
  const segmentVideoId = [item.imdb_id || item.video_id, item.season, item.episode].join(':');
  return getSegments(segmentVideoId, signal);
}

// Whether the video has at least `minAhead` seconds buffered at/around `time`.
export function isPositionBuffered(video: HTMLVideoElement | null, time: number, minAhead = 0.5): boolean {
  if (!video || !Number.isFinite(time)) return false;
  try {
    const ranges = video.buffered;
    for (let i = 0; i < ranges.length; i += 1) {
      const start = ranges.start(i);
      const end = ranges.end(i);
      if (time >= start - 0.1 && end - time >= minAhead) return true;
    }
  } catch {
    return false;
  }
  return false;
}

const ISO_LANGUAGE_LABELS: Record<string, string> = {
  en: 'English', eng: 'English',
  es: 'Spanish', spa: 'Spanish',
  fr: 'French', fre: 'French', fra: 'French',
  de: 'German', ger: 'German', deu: 'German',
  it: 'Italian', ita: 'Italian',
  pt: 'Portuguese', por: 'Portuguese',
  ja: 'Japanese', jpn: 'Japanese', jp: 'Japanese',
  ko: 'Korean', kor: 'Korean',
  zh: 'Chinese', zho: 'Chinese', chi: 'Chinese',
  ru: 'Russian', rus: 'Russian',
  hi: 'Hindi', hin: 'Hindi',
  ar: 'Arabic', ara: 'Arabic',
  nl: 'Dutch', dut: 'Dutch', nld: 'Dutch',
  sv: 'Swedish', swe: 'Swedish',
  no: 'Norwegian', nor: 'Norwegian',
  da: 'Danish', dan: 'Danish',
  fi: 'Finnish', fin: 'Finnish',
  pl: 'Polish', pol: 'Polish',
  tr: 'Turkish', tur: 'Turkish',
  cs: 'Czech', ces: 'Czech', cze: 'Czech',
  el: 'Greek', ell: 'Greek', gre: 'Greek',
  he: 'Hebrew', heb: 'Hebrew',
  th: 'Thai', tha: 'Thai',
  uk: 'Ukrainian', ukr: 'Ukrainian',
  hu: 'Hungarian', hun: 'Hungarian',
  ro: 'Romanian', ron: 'Romanian', rum: 'Romanian',
};

// Turn an ISO language code (en, eng, en-US, …) into a clean human label.
export function languageLabel(code?: string | null): string {
  const normalized = String(code ?? '').toLowerCase().trim().replace(/_/g, '-');
  if (!normalized || normalized === 'und' || normalized === 'unknown') return '';
  const base = normalized.split('-')[0];
  if (ISO_LANGUAGE_LABELS[normalized]) return ISO_LANGUAGE_LABELS[normalized];
  if (ISO_LANGUAGE_LABELS[base]) return ISO_LANGUAGE_LABELS[base];
  if (typeof Intl !== 'undefined' && 'DisplayNames' in Intl) {
    try {
      const display = new Intl.DisplayNames(['en'], { type: 'language' }).of(base);
      if (display && display.toLowerCase() !== base) return display;
    } catch {
      // Intl.DisplayNames may reject unusual codes; fall through.
    }
  }
  return base.toUpperCase();
}

// Pull channel layout / codec / Atmos hints out of an audio track's free text.
function audioSecondary(track: { name?: string | null; lang?: string | null; language?: string | null; label?: string | null }): string {
  const text = [track.name, track.label, track.lang, track.language].filter(Boolean).join(' ');
  const parts: string[] = [];
  if (/\b7\.1\b/.test(text)) parts.push('7.1');
  else if (/\b5\.1\b/.test(text)) parts.push('5.1');
  else if (/\bstereo\b|\b2\.0\b/i.test(text)) parts.push('Stereo');
  if (/\be-?ac-?3\b|\beac3\b|\bddp\b|\bdolby digital plus\b/i.test(text)) parts.push('E-AC-3');
  else if (/\bac-?3\b|\bdolby digital\b/i.test(text)) parts.push('AC-3');
  else if (/\btruehd\b/i.test(text)) parts.push('TrueHD');
  else if (/\bdts\b/i.test(text)) parts.push('DTS');
  else if (/\baac\b/i.test(text)) parts.push('AAC');
  if (/\batmos\b/i.test(text)) parts.push('Atmos');
  return parts.join(' ');
}

// Pull SDH / Forced hints out of a subtitle track's free text.
function subtitleSecondary(track: { name?: string | null; lang?: string | null; language?: string | null; kind?: string | null; label?: string | null }): string {
  const text = [track.name, track.label, track.lang, track.language, track.kind].filter(Boolean).join(' ');
  const parts: string[] = [];
  if (/\bsdh\b|\bhearing impaired\b/i.test(text)) parts.push('SDH');
  if (/\bforced\b/i.test(text)) parts.push('Forced');
  return parts.join(' ');
}

// English-first, then alphabetical by visible label. Preserves the objects.
function sortTracksEnglishFirst(tracks: TrackOption[]): TrackOption[] {
  return [...tracks].sort((a, b) => {
    const aEng = /^english\b/i.test(a.label) ? 0 : 1;
    const bEng = /^english\b/i.test(b.label) ? 0 : 1;
    if (aEng !== bEng) return aEng - bEng;
    return a.label.localeCompare(b.label);
  });
}

function channelLabel(channels?: number | null): string {
  if (!channels) return '';
  if (channels === 1) return 'Mono';
  if (channels === 2) return 'Stereo';
  if (channels === 6) return '5.1';
  if (channels === 8) return '7.1';
  return `${channels} ch`;
}

/**
 * Audio menu for a prepared session. The stream the player receives carries ONE
 * audio track (the server or the desktop engine picked it), so neither hls.js
 * nor the browser can list the others; the file's probe facts can. Ids are
 * `file:<stream index>`; choosing one re-prepares the session with that track.
 */
export function fileAudioOptions(tracks: MediaAudioFacts[], selectedIndex: number | null): TrackOption[] {
  return tracks.map((track, position) => {
    const language = languageLabel(track.language);
    const title = String(track.title ?? '').trim();
    const format = [String(track.codec ?? '').toUpperCase().replace('EAC3', 'E-AC-3').replace(/^AC3$/, 'AC-3'), channelLabel(track.channels)].filter(Boolean).join(' ');
    // A title usually already says what the track is ("TrueHD 5.1", "Commentary").
    const what = title || format;
    return {
      id: `file:${track.index}`,
      label: [language || (title ? '' : `Audio ${position + 1}`), what].filter(Boolean).join(' · '),
      detail: title && format && !title.toLowerCase().includes(format.toLowerCase()) ? format : '',
      selected: selectedIndex != null ? track.index === selectedIndex : false,
    };
  });
}

export function refreshTrackState(
  video: HTMLVideoElement | null,
  hls: Hls | null,
  setAudioTracks: (tracks: TrackOption[]) => void,
  setSubtitleTracks: (tracks: TrackOption[]) => void,
) {
  let audio: TrackOption[] = [];
  const subtitlesOff: TrackOption = { id: 'off', label: 'Off', detail: '', selected: true };
  let subtitles: TrackOption[] = [];
  if (hls?.audioTracks?.length) {
    const selectedAudio = hls.audioTrack >= 0 ? hls.audioTrack : hls.audioTracks.length === 1 ? 0 : -1;
    hls.audioTracks.forEach((track, index) => {
      audio.push({
        id: `hls:${index}`,
        label: languageLabel(track.lang) || track.name || track.lang || `Audio ${index + 1}`,
        detail: audioSecondary(track),
        selected: selectedAudio === index,
      });
    });
  } else if (video && nativeAudioTracks(video)) {
    const nativeTracks = Array.from(nativeAudioTracks(video) ?? []);
    nativeTracks.forEach((track, index) => {
      audio.push({
        id: `native:${index}`,
        label: languageLabel(track.language) || track.label || track.language || `Audio ${index + 1}`,
        detail: audioSecondary(track),
        selected: Boolean(track.enabled) || (nativeTracks.length === 1 && !nativeTracks.some((entry) => entry.enabled)),
      });
    });
  }
  if (hls?.subtitleTracks?.length) {
    subtitlesOff.selected = hls.subtitleTrack < 0;
    hls.subtitleTracks.forEach((track, index) => {
      subtitles.push({
        id: `hls:${index}`,
        label: languageLabel(track.lang) || track.name || track.lang || `Subtitle ${index + 1}`,
        detail: subtitleSecondary(track),
        selected: hls.subtitleTrack === index,
      });
    });
  }
  if (video?.textTracks?.length) {
    // Tracks a previous prepared session created stay on the element (they
    // can't be removed); keep their indices but hide them from the menu.
    const nativeTextTracks = Array.from(video.textTracks);
    const includeNativeTracks = !hls?.subtitleTracks?.length || hasExternalSubtitleElements(video);
    if (includeNativeTracks) subtitlesOff.selected = nativeTextTracks.every((track) => track.mode !== 'showing') && subtitlesOff.selected;
    nativeTextTracks.forEach((track, index) => {
      if (!includeNativeTracks || retiredTextTracks.has(track)) return;
      subtitles.push({
        id: `native:${index}`,
        label: languageLabel(track.language) || track.label || track.language || `Subtitle ${index + 1}`,
        detail: subtitleSecondary(track),
        selected: track.mode === 'showing',
      });
    });
  }
  audio = sortTracksEnglishFirst(audio);
  subtitles = [subtitlesOff, ...sortTracksEnglishFirst(subtitles)];
  setAudioTracks(audio);
  setSubtitleTracks(subtitles);
}

function hasExternalSubtitleElements(video: HTMLVideoElement) {
  return Boolean(video.querySelector('track[data-curatio-subtitle]'));
}

export function selectAudioTrack(
  id: string,
  video: HTMLVideoElement | null,
  hls: Hls | null,
  setAudioTracks: (tracks: TrackOption[]) => void,
  setSubtitleTracks: (tracks: TrackOption[]) => void,
) {
  if (id.startsWith('hls:') && hls) {
    hls.audioTrack = Number(id.split(':')[1]);
  }
  const nativeTracks = nativeAudioTracks(video);
  if (id.startsWith('native:') && nativeTracks) {
    const selected = Number(id.split(':')[1]);
    Array.from(nativeTracks).forEach((track, index) => {
      track.enabled = index === selected;
    });
  }
  refreshTrackState(video, hls, setAudioTracks, setSubtitleTracks);
}

export function selectSubtitleTrack(
  id: string,
  video: HTMLVideoElement | null,
  hls: Hls | null,
  setAudioTracks: (tracks: TrackOption[]) => void,
  setSubtitleTracks: (tracks: TrackOption[]) => void,
) {
  if (hls) hls.subtitleTrack = id === 'off' ? -1 : id.startsWith('hls:') ? Number(id.split(':')[1]) : hls.subtitleTrack;
  if (id.startsWith('hls:') && video) {
    Array.from(video.querySelectorAll<HTMLTrackElement>('track[data-curatio-subtitle]')).forEach((track) => {
      track.track.mode = 'disabled';
    });
  } else if (video?.textTracks) {
    Array.from(video.textTracks).forEach((track, index) => {
      track.mode = id === `native:${index}` ? 'showing' : 'disabled';
    });
  }
  refreshTrackState(video, hls, setAudioTracks, setSubtitleTracks);
}

export type ResumeInfo = { item: WebMediaItem; position: number; duration: number; updatedAt: number };

function savedUpdatedAt(item: WebMediaItem): number {
  try {
    const raw = localStorage.getItem(playbackProgressKey(item));
    if (!raw) return 0;
    const data = JSON.parse(raw) as { updatedAt?: number };
    return Number(data.updatedAt ?? 0) || 0;
  } catch {
    return 0;
  }
}

// Resume entry for a single playable item (movie or specific episode), or null.
export function resumeInfoFor(item: WebMediaItem): ResumeInfo | null {
  const saved = savedPlaybackState(item);
  if (!savedProgressIsResumable(item, saved)) return null;
  return { item, position: saved.position, duration: saved.duration, updatedAt: savedUpdatedAt(item) };
}

// Most recently watched, still-resumable episode of a series, or null.
export function latestSeriesResume(series: WebMediaItem, seasons?: SeasonItem[]): ResumeInfo | null {
  const prefix = `${playbackProgressPrefix()}${series.media_type}:${series.tmdb_id}:`;
  const episodes = flattenEpisodes(seasons);
  let best: ResumeInfo | null = null;
  for (let i = 0; i < localStorage.length; i += 1) {
    const key = localStorage.key(i);
    if (!key || !key.startsWith(prefix)) continue;
    try {
      const raw = localStorage.getItem(key);
      if (!raw) continue;
      const data = JSON.parse(raw) as { position?: number; duration?: number; updatedAt?: number };
      const tail = key.slice(prefix.length);
      const [season, episode] = tail.split(':');
      const ep = episodes.find(
        (candidate: EpisodeItem) => String(candidate.season) === season && String(candidate.episode) === episode,
      );
      if (!ep) continue;
      const position = Number(data.position ?? 0);
      const duration = Number(data.duration ?? 0);
      const play = episodePlayItem(series, ep);
      if (!savedProgressIsResumable(play, { position, duration })) continue;
      const updatedAt = Number(data.updatedAt ?? 0) || 0;
      if (!best || updatedAt > best.updatedAt) {
        best = { item: play, position, duration, updatedAt };
      }
    } catch {
      // Ignore malformed entries.
    }
  }
  return best;
}

/**
 * Resume entry from the server's Continue Watching list: what another device
 * (or this one, before its saved positions were cleared) was last playing.
 */
export function serverResumeFor(item: WebMediaItem, entries: WebMediaItem[], seasons?: SeasonItem[]): ResumeInfo | null {
  const sameTitle = (entry: WebMediaItem) =>
    entry.tmdb_id != null && String(entry.tmdb_id) === String(item.tmdb_id) && itemKind(entry) === itemKind(item);
  for (const entry of entries) {
    if (!sameTitle(entry)) continue;
    const state = { position: Number(entry.position ?? 0), duration: Number(entry.duration ?? 0) };
    let play = item;
    if (itemKind(item) === 'series') {
      const episode = flattenEpisodes(seasons).find(
        (candidate: EpisodeItem) => String(candidate.season) === String(entry.season) && String(candidate.episode) === String(entry.episode),
      );
      if (!episode) continue;
      play = episodePlayItem(item, episode);
    }
    if (savedProgressIsResumable(play, state)) return { item: play, ...state, updatedAt: 0 };
  }
  return null;
}

// ---- Auto-resume (Netflix/Prime/YouTube style: no blocking prompt) ----

export { markStartFromBeginning } from './startFromBeginning';

/** Local saved progress newer than this beats server progress (this device just watched). */
const LOCAL_PROGRESS_FRESH_MS = 6 * 60 * 60 * 1000;

/**
 * Where to start an item: 0 when marked "from beginning"; otherwise the freshest
 * resumable position — local progress from the last few hours, else server
 * progress (Continue Watching `position`/`duration` on the item or the matching
 * entry), else older local progress.
 */
export function resumeStartFor(item: WebMediaItem, serverEntry?: WebMediaItem | null, now = Date.now()): number {
  if (isMarkedStartFromBeginning(item)) return 0;
  const local = savedPlaybackState(item);
  const localAt = savedUpdatedAt(item);
  const localOk = savedProgressIsResumable(item, local);
  if (localOk && localAt && now - localAt < LOCAL_PROGRESS_FRESH_MS) return local.position;
  const server = serverEntry ?? item;
  const serverState = { position: Number(server.position ?? 0), duration: Number(server.duration ?? 0) };
  if (savedProgressIsResumable(server, serverState)) return serverState.position;
  return localOk ? local.position : 0;
}
