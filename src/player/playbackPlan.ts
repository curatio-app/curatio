import { localEngineRequest } from '../api/accountClient';
import { hasLocalEngine, prewarmLocalSession } from '../api/desktopBridge';
import { inspectSources, peekMediaFacts } from '../api/mediaFacts';
import { loadPlaybackSources, type SourceTier } from '../api/playbackSources';
import { streamUrl } from '../api/mediaUtils';
import { defaultSourceTier, trackMatchesLanguage } from '../api/preferences';
import { classifyStream, getBrowserCaps, type BrowserCaps, type StreamCompat } from '../api/streamCompat';
import { isDolbyVision, isOpaqueRoute } from '../api/streamMeta';
import type { MediaAudioFacts, MediaFacts, PlaybackSource, WebMediaItem } from '../api/types';

// ---------------------------------------------------------------------------
// Playback plan: how THIS browser should play one source.
//
//   direct            MP4/WebM container + decodable video + decodable audio → <video src>
//   hls               the source itself is HLS → hls.js / native HLS
//   server-hls        the server copies the video into fMP4 HLS (MKV/AVI/TS, or
//                     audio the browser can't decode: AC-3/E-AC-3/DTS/TrueHD, or
//                     HEVC the browser decodes in a container it can't open)
//   server-transcode  video the browser cannot decode: the server re-encodes to
//                     H.264 ≤1080p. Last resort — ranked behind everything else.
//
// The decision is made from the server's ffprobe facts when available, using a
// cached MediaCapabilities answer per codec string. Filename classification
// (streamCompat) is only the fallback when inspection failed.
// ---------------------------------------------------------------------------

export type PlanKind = 'direct' | 'hls' | 'server-hls' | 'server-transcode' | 'unplayable';

export interface DecodeQuery {
  kind: 'video' | 'audio';
  /** 'file' for <video src> / native HLS, 'media-source' for hls.js. */
  type: 'file' | 'media-source';
  contentType: string;
  width?: number;
  height?: number;
  bitrate?: number;
  framerate?: number;
  channels?: number;
}

/** Synchronous decode answer: true/false, or null when unknown. */
export type DecodeLookup = (query: DecodeQuery) => boolean | null;

export interface PlanOptions {
  decode: DecodeLookup;
  /** Prepared HLS plays through native HLS (Safari) rather than MSE/hls.js. */
  nativeHls: boolean;
  /** Preferred audio language ('system' or a code), from preferences. */
  audioLanguage?: string;
  /** Whether the element exposes `audioTracks` (only then can direct play switch audio). */
  audioTrackSwitching?: boolean;
  /** Codec+profile scopes disabled by a verified decode error this session. */
  disabledScopes?: ReadonlySet<string>;
}

export interface PlaybackPlan {
  kind: PlanKind;
  reason: string;
  /** Where the decision came from. */
  basis: 'facts' | 'fields';
  allowHevc: boolean;
  allowTranscode: boolean;
  surround: boolean;
  /** Codec+profile scope of the video stream, e.g. "hevc:main 10" ("hevc:*" when unknown). */
  videoScope: string | null;
  /** Higher plays more reliably here; used to order sources. */
  score: number;
}

const KIND_SCORE: Record<PlanKind, number> = {
  direct: 400,
  hls: 380,
  'server-hls': 200,
  'server-transcode': 40,
  unplayable: 0,
};

const DIRECT_CONTAINERS = new Set(['mp4', 'mov', 'webm', 'm4v']);

function plan(kind: PlanKind, reason: string, basis: PlaybackPlan['basis'], extra: Partial<PlaybackPlan> = {}): PlaybackPlan {
  const base: PlaybackPlan = {
    kind,
    reason,
    basis,
    allowHevc: false,
    allowTranscode: kind === 'server-transcode',
    surround: false,
    videoScope: null,
    score: KIND_SCORE[kind],
    ...extra,
  };
  return base;
}

/** Codec string for a video stream, deriving a conservative one when ffprobe gave none. */
export function videoCodecString(video: NonNullable<MediaFacts['video']>): string | null {
  if (video.dynamic_range === 'dv' && video.dv_profile === 5) {
    // Profile 5 has no HDR10/SDR base layer: it needs a real Dolby Vision decoder.
    return 'dvh1.05.06';
  }
  if (video.codec_string) return video.codec_string;
  const depth = Number(video.bit_depth) || 8;
  switch ((video.codec || '').toLowerCase()) {
    case 'h264':
      return 'avc1.640028';
    case 'hevc':
      return depth >= 10 ? 'hvc1.2.4.L153.B0' : 'hvc1.1.6.L120.B0';
    case 'av1':
      return `av01.0.08M.${depth >= 10 ? '10' : '08'}`;
    case 'vp9':
      return `vp09.0${depth >= 10 ? '2' : '0'}.40.${depth >= 10 ? '10' : '08'}`;
    case 'vp8':
      return 'vp8';
    default:
      return null; // mpeg4/xvid/mpeg2/vc1…: no browser decodes these.
  }
}

/** "codec:profile" scope used to disable a codec after a verified decode error. */
export function videoScopeOf(video: MediaFacts['video']): string | null {
  if (!video?.codec) return null;
  return `${video.codec.toLowerCase()}:${String(video.profile ?? '').toLowerCase().trim() || '*'}`;
}

function scopeDisabled(scope: string | null, disabled?: ReadonlySet<string>): boolean {
  if (!scope || !disabled?.size) return false;
  if (disabled.has(scope)) return true;
  // An unknown-profile scope ("hevc:*") can't be told apart from a disabled one.
  const [codec, profile] = scope.split(':');
  if (profile === '*') return [...disabled].some((entry) => entry.startsWith(`${codec}:`));
  return disabled.has(`${codec}:*`);
}

function videoQuery(facts: MediaFacts, type: DecodeQuery['type'], container: 'mp4' | 'webm'): DecodeQuery | null {
  const video = facts.video;
  if (!video) return null;
  const codec = videoCodecString(video);
  if (!codec) return null;
  return {
    kind: 'video',
    type,
    contentType: `video/${container}; codecs="${codec}"`,
    width: Number(video.width) || 1920,
    height: Number(video.height) || 1080,
    bitrate: Number(facts.bitrate) || 8_000_000,
    framerate: Number(video.frame_rate) || 24,
  };
}

function audioQuery(track: MediaAudioFacts, type: DecodeQuery['type'], container: 'mp4' | 'webm'): DecodeQuery | null {
  const codec = track.codec_string || null;
  if (!codec) return null;
  return {
    kind: 'audio',
    type,
    contentType: `audio/${container}; codecs="${codec}"`,
    channels: Number(track.channels) || 2,
  };
}

/** The audio track the browser will play by itself (default, else first). */
export function browserDefaultAudio(facts: MediaFacts): MediaAudioFacts | null {
  return facts.audio.find((track) => track.default) ?? facts.audio[0] ?? null;
}

/** The audio track the viewer wants (preferred language), else the default one. */
export function preferredAudio(facts: MediaFacts, language?: string): MediaAudioFacts | null {
  if (language && language !== 'system') {
    const match = facts.audio.find((track) => trackMatchesLanguage(`${track.language ?? ''} ${track.title ?? ''}`, language));
    if (match) return match;
  }
  return browserDefaultAudio(facts);
}

/** Decide the path from server facts. Pure: capability answers come from `decode`. */
export function planFromFacts(facts: MediaFacts, options: PlanOptions): PlaybackPlan {
  const container = String(facts.container || '').toLowerCase();
  const video = facts.video;
  const scope = videoScopeOf(video);
  if (container === 'hls') return plan('hls', 'HLS source', 'facts', { videoScope: scope });

  const preparedType: DecodeQuery['type'] = options.nativeHls ? 'file' : 'media-source';
  const directContainer = DIRECT_CONTAINERS.has(container);
  const fileContainer = container === 'webm' ? 'webm' : 'mp4';
  const disabled = scopeDisabled(scope, options.disabledScopes);

  const decodes = (query: DecodeQuery | null, missingMeans: boolean) => {
    if (!query) return missingMeans;
    return options.decode(query) === true;
  };
  // No video stream (audio only) decodes trivially; an unknown codec does not.
  const videoDirect = !video || (!disabled && decodes(videoQuery(facts, 'file', fileContainer), false));
  // Prepared HLS is always fMP4.
  const videoPrepared = !video || (!disabled && decodes(videoQuery(facts, preparedType, 'mp4'), false));

  const wanted = preferredAudio(facts, options.audioLanguage);
  const playsByDefault = browserDefaultAudio(facts);
  const audioDirect = !playsByDefault || decodes(audioQuery(playsByDefault, 'file', fileContainer), false);
  const channels = Number(wanted?.channels) || 0;
  const surround = channels > 2;
  const isHevc = (video?.codec ?? '').toLowerCase() === 'hevc';
  const shared = { basis: 'facts' as const, videoScope: scope, surround };

  if (!videoPrepared && !videoDirect) {
    return plan('server-transcode', `${(video?.codec || 'This').toUpperCase()} video can't be decoded by this browser`, 'facts', {
      ...shared,
      score: KIND_SCORE['server-transcode'],
    });
  }
  const languageMismatch = Boolean(
    wanted && playsByDefault && wanted !== playsByDefault && !options.audioTrackSwitching,
  );
  if (directContainer && videoDirect && audioDirect && !languageMismatch) {
    const dvPenalty = video?.dynamic_range === 'dv' ? 10 : 0;
    return plan('direct', 'Direct play', 'facts', { ...shared, score: KIND_SCORE.direct - dvPenalty });
  }
  const why = !directContainer
    ? `${container.toUpperCase() || 'Unknown'} container prepared by the server`
    : !audioDirect
      ? `${(playsByDefault?.codec || 'audio').toUpperCase()} audio prepared by the server`
      : languageMismatch
        ? 'Preferred audio language prepared by the server'
        : 'Prepared by the server';
  return plan('server-hls', why, 'facts', {
    ...shared,
    allowHevc: isHevc && videoPrepared,
    // HEVC the browser decodes directly but not through MSE: let the server
    // fall back to H.264 rather than fail.
    allowTranscode: !videoPrepared,
    score: KIND_SCORE['server-hls'] - (videoPrepared ? 0 : 60),
  });
}

/** Fallback when inspection failed: classify from the source's container/codec fields. */
export function planFromClassification(
  stream: PlaybackSource,
  compat: StreamCompat,
  caps: BrowserCaps,
  disabledScopes?: ReadonlySet<string>,
): PlaybackPlan {
  const { parsed } = compat;
  const scope = parsed.videoCodec === 'unknown' ? null : `${parsed.videoCodec}:*`;
  const disabled = scopeDisabled(scope, disabledScopes);
  const shared = { basis: 'fields' as const, videoScope: scope };
  const dvPenalty = isDolbyVision(stream) ? 10 : 0;
  if (!parsed.url) return plan('unplayable', 'No playable URL', 'fields', shared);
  if (compat.status === 'hls') return plan('hls', 'HLS source', 'fields', shared);
  if (disabled || compat.status === 'incompatible') {
    return plan('server-transcode', compat.reason ?? 'Video needs transcoding', 'fields', shared);
  }
  if (hasLocalEngine() && (compat.status === 'native' || compat.status === 'unknown')) {
    // Desktop app: never play directly on a codec-field guess. Chromium shows the
    // picture of a file whose audio it can't decode and stays silent; the local
    // engine copies what plays and converts what doesn't, for little cost.
    return plan('server-hls', 'Prepared on this computer (format not verified)', 'fields', { ...shared, allowHevc: true, surround: true, score: KIND_SCORE['server-hls'] });
  }
  if (compat.status === 'native') return plan('direct', 'Direct play', 'fields', { ...shared, score: KIND_SCORE.direct - dvPenalty });
  if (compat.status === 'unknown') {
    // No hints at all: attempt directly, ranked below known-good direct sources.
    return plan('direct', 'Direct play (format unknown)', 'fields', { ...shared, score: KIND_SCORE.direct - 60 });
  }
  return plan('server-hls', compat.reason ?? 'Prepared by the server', 'fields', {
    ...shared,
    allowHevc: parsed.videoCodec === 'hevc' && caps.hevc,
    surround: Number(stream.audio_channels ?? 0) > 2,
    score: KIND_SCORE['server-hls'] - dvPenalty,
  });
}

// ---------------------------------------------------------------------------
// Capability cache: MediaCapabilities.decodingInfo answers per codec string,
// kept in memory + localStorage, with canPlayType / isTypeSupported fallback.
// ---------------------------------------------------------------------------

const DECODE_STORAGE_KEY = 'curatio-decode-caps-v1';
const DECODE_TTL_MS = 7 * 24 * 60 * 60 * 1000;
const decodeMemo = new Map<string, boolean>();
let decodeHydrated = false;
const disabledScopes = new Set<string>();

export function decodeKey(query: DecodeQuery): string {
  const size = query.kind === 'video' ? `${query.width ?? 0}x${query.height ?? 0}@${Math.round(query.framerate ?? 0)}` : `${query.channels ?? 2}ch`;
  return `${query.type}|${query.contentType}|${size}`;
}

function hydrateDecode() {
  if (decodeHydrated) return;
  decodeHydrated = true;
  try {
    const raw = localStorage.getItem(DECODE_STORAGE_KEY);
    const parsed = raw ? (JSON.parse(raw) as Record<string, [boolean, number]>) : {};
    for (const [key, [value, at]] of Object.entries(parsed)) {
      if (Date.now() - at < DECODE_TTL_MS) decodeMemo.set(key, value);
    }
  } catch {
    // Convenience only.
  }
}

function persistDecode(key: string, value: boolean) {
  try {
    const raw = localStorage.getItem(DECODE_STORAGE_KEY);
    const parsed = raw ? (JSON.parse(raw) as Record<string, [boolean, number]>) : {};
    parsed[key] = [value, Date.now()];
    localStorage.setItem(DECODE_STORAGE_KEY, JSON.stringify(parsed));
  } catch {
    // Quota or privacy mode.
  }
}

function fallbackDecode(query: DecodeQuery): boolean | null {
  const codecs = /codecs="([^"]+)"/.exec(query.contentType)?.[1] ?? '';
  // canPlayType is optimistic for HEVC on machines without a decoder; trust the
  // primed browser caps (MediaCapabilities, 1080p 8-bit) and refuse 10-bit/4K
  // HEVC without a real MediaCapabilities answer.
  if (/^(hvc1|hev1)/.test(codecs)) {
    const caps = getBrowserCaps();
    if (!caps.hevc) return false;
    const tenBit = /^(hvc1|hev1)\.2\./.test(codecs);
    const uhd = (query.height ?? 0) > 1088;
    return tenBit || uhd ? Boolean(caps.hevc10) : true;
  }
  if (/^dvh|^dvhe/.test(codecs)) return false;
  try {
    if (query.type === 'media-source') {
      if (typeof MediaSource === 'undefined' || typeof MediaSource.isTypeSupported !== 'function') return false;
      const mp4Type = query.contentType.replace(/^audio\//, 'video/');
      return MediaSource.isTypeSupported(query.contentType) || MediaSource.isTypeSupported(mp4Type);
    }
    const probe = document.createElement('video');
    return probe.canPlayType(query.contentType) !== '';
  } catch {
    return null;
  }
}

/** Cached answer, else the synchronous canPlayType/isTypeSupported fallback. */
export const lookupDecode: DecodeLookup = (query) => {
  hydrateDecode();
  const key = decodeKey(query);
  if (decodeMemo.has(key)) return decodeMemo.get(key)!;
  return fallbackDecode(query);
};

type MediaCapabilitiesLike = {
  decodingInfo: (config: unknown) => Promise<{ supported: boolean; smooth?: boolean; powerEfficient?: boolean }>;
};

/** Ask MediaCapabilities for any uncached query and remember the answers. */
export async function primeDecodeCapabilities(queries: DecodeQuery[]): Promise<void> {
  hydrateDecode();
  const mc = typeof navigator !== 'undefined'
    ? (navigator as Navigator & { mediaCapabilities?: MediaCapabilitiesLike }).mediaCapabilities
    : undefined;
  if (!mc?.decodingInfo) return;
  const pending = queries.filter((query) => !decodeMemo.has(decodeKey(query)));
  await Promise.all(pending.map(async (query) => {
    const key = decodeKey(query);
    try {
      const config = query.kind === 'video'
        ? {
          type: query.type,
          video: {
            contentType: query.contentType,
            width: query.width ?? 1920,
            height: query.height ?? 1080,
            bitrate: query.bitrate ?? 8_000_000,
            framerate: query.framerate ?? 24,
          },
        }
        : {
          type: query.type,
          audio: { contentType: query.contentType, channels: String(query.channels ?? 2) },
        };
      const info = await mc.decodingInfo(config);
      const codecs = /codecs="([^"]+)"/.exec(query.contentType)?.[1] ?? '';
      // HEVC has no usable software decoder in Chromium/Firefox.
      const value = /^(hvc1|hev1|dvh)/.test(codecs) ? Boolean(info.supported && info.powerEfficient) : Boolean(info.supported);
      decodeMemo.set(key, value);
      persistDecode(key, value);
    } catch {
      // Leave it to the fallback.
    }
  }));
}

/** Every capability question a plan for these facts may ask. */
export function decodeQueriesFor(facts: MediaFacts, nativeHls: boolean): DecodeQuery[] {
  const container = String(facts.container || '').toLowerCase() === 'webm' ? 'webm' : 'mp4';
  const prepared: DecodeQuery['type'] = nativeHls ? 'file' : 'media-source';
  const queries: Array<DecodeQuery | null> = [
    videoQuery(facts, 'file', container),
    videoQuery(facts, prepared, 'mp4'),
    ...facts.audio.map((track) => audioQuery(track, 'file', container)),
  ];
  return queries.filter((query): query is DecodeQuery => Boolean(query));
}

/** Session-disable one codec+profile after a verified decode error. */
export function disableDecodeScope(scope: string | null) {
  if (scope) disabledScopes.add(scope);
}

export function disabledDecodeScopes(): ReadonlySet<string> {
  return disabledScopes;
}

/** Test-only: reset capability memo and disabled scopes. */
export function resetPlaybackPlanState() {
  decodeMemo.clear();
  decodeHydrated = false;
  disabledScopes.clear();
  try {
    localStorage.removeItem(DECODE_STORAGE_KEY);
  } catch {
    // ignore
  }
}

function prefersNativeHls(): boolean {
  try {
    const caps = getBrowserCaps();
    return caps.nativeHls;
  } catch {
    return false;
  }
}

function audioTrackSwitching(): boolean {
  try {
    return 'audioTracks' in HTMLMediaElement.prototype;
  } catch {
    return false;
  }
}

/** The live plan for a source: facts when known, codec-field classification otherwise. */
export function planForStream(stream: PlaybackSource, audioLanguage?: string): PlaybackPlan {
  const url = streamUrl(stream);
  const facts = url ? peekMediaFacts(url) : null;
  const options: PlanOptions = {
    decode: lookupDecode,
    nativeHls: prefersNativeHls(),
    audioLanguage,
    audioTrackSwitching: audioTrackSwitching(),
    disabledScopes,
  };
  if (facts) return planFromFacts(facts, options);
  return planFromClassification(stream, classifyStream(stream), getBrowserCaps(), disabledScopes);
}

/**
 * Inspect the top candidates (opaque resolver routes only when `includeOpaque`
 * lists them, since a speculative inspect would exercise the resolver) and
 * prime the capability cache for their facts. Never rejects.
 */
export async function inspectCandidates(
  streams: PlaybackSource[],
  options: { includeOpaque?: PlaybackSource[]; limit?: number; signal?: AbortSignal } = {},
): Promise<void> {
  const limit = options.limit ?? 4;
  const chosen: PlaybackSource[] = [];
  for (const stream of options.includeOpaque ?? []) if (!chosen.includes(stream)) chosen.push(stream);
  for (const stream of streams) {
    if (chosen.length >= limit) break;
    if (!isOpaqueRoute(stream) && !chosen.includes(stream)) chosen.push(stream);
  }
  const urls = chosen.map(streamUrl).filter(Boolean).slice(0, limit);
  if (!urls.length) return;
  try {
    const results = await inspectSources(urls, options.signal);
    const nativeHls = prefersNativeHls();
    const queries = [...results.values()].flatMap((result) => (result.state === 'ok' ? decodeQueriesFor(result.media, nativeHls) : []));
    if (queries.length) await primeDecodeCapabilities(queries);
  } catch {
    // Planning falls back to codec-field classification.
  }
}

/**
 * Stable sort by playability. In a browser: browser-decodable first, transcode
 * last, server order within a bucket. In the desktop app, like iOS/iPadOS:
 * server order, since preparing a source there is a cheap local remux; only
 * sources needing a full video re-encode (or unplayable) move back.
 */
export function orderByPlayability(streams: PlaybackSource[], planOf: (stream: PlaybackSource) => PlaybackPlan): PlaybackSource[] {
  // Server order wins (native parity); only what can't play here moves back:
  // unplayable last, transcode-only before it, everything else as served.
  const scoreOf = (plan: PlaybackPlan) => (plan.kind === 'unplayable' ? 0 : plan.kind === 'server-transcode' ? 1 : 2);
  return streams
    .map((stream, index) => ({ stream, index, score: scoreOf(planOf(stream)) }))
    .sort((a, b) => (b.score - a.score) || (a.index - b.index))
    .map((entry) => entry.stream);
}

/**
 * Warm everything Play needs: the shared source cache and the server facts of
 * the top candidate(s), plus the capability answers for them. Called when
 * Details opens and ~2 minutes before an episode ends. Only non-opaque routes
 * are inspected here (speculative resolver hits are not allowed); the player
 * inspects an opaque start candidate itself. Returns a cancel function.
 */
export function prefetchPlayback(
  item: WebMediaItem,
  options: { tier?: SourceTier; profileId?: number; limit?: number; start?: number; audioLanguage?: string } = {},
): () => void {
  const controller = new AbortController();
  void loadPlaybackSources(item, { tier: options.tier ?? defaultSourceTier(), profileId: options.profileId, signal: controller.signal })
    .then(async ({ streams }) => {
      if (controller.signal.aborted || !streams.length) return;
      await inspectCandidates(streams.slice(0, 6), { limit: options.limit ?? 1, signal: controller.signal });
      if (controller.signal.aborted) return;
      prewarmStartCandidate(streams, options);
    })
    .catch(() => undefined);
  return () => controller.abort();
}

/**
 * Desktop app: have the local engine start preparing the source Play will open
 * (the first in order, as the player picks it), so pressing Play finds it ready.
 */
function prewarmStartCandidate(streams: PlaybackSource[], options: { start?: number; audioLanguage?: string }) {
  if (!hasLocalEngine()) return;
  const stream = orderByPlayability(streams, (candidate) => planForStream(candidate, options.audioLanguage))[0];
  const url = stream ? streamUrl(stream) : '';
  // An opaque route is resolved by its provider when opened: not speculatively.
  if (!url || isOpaqueRoute(stream)) return;
  const planned = planForStream(stream, options.audioLanguage);
  if (planned.kind !== 'server-hls') return;
  prewarmLocalSession(localEngineRequest(url, {
    start: options.start,
    audioLanguage: options.audioLanguage,
    allowHevc: planned.allowHevc,
    allowTranscode: planned.allowTranscode,
    headers: sourceRequestHeaders(stream),
    facts: peekMediaFacts(url),
  }));
}

/** Request headers a source needs (the server's `headers`), or undefined when none. */
export function sourceRequestHeaders(stream: PlaybackSource): Record<string, string> | undefined {
  const request = stream.headers;
  if (!request || typeof request !== 'object') return undefined;
  const headers = Object.fromEntries(Object.entries(request).filter(([, value]) => typeof value === 'string')) as Record<string, string>;
  return Object.keys(headers).length ? headers : undefined;
}
