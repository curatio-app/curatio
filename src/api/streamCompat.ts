import type { PlaybackSource } from './types';
import { isDolbyVision } from './streamMeta';

// ---------------------------------------------------------------------------
// Browser playback capability detection + per-source compatibility.
//
// Library and addon sources are arbitrary files (MKV/MP4, H.264/HEVC,
// AAC/AC3/DTS). Browsers can only play a narrow set of those natively, so
// before we ever touch a `<video>` element we classify each source, from the
// server's structured codec fields, against what THIS browser can decode. The pure helpers below take an injectable `caps` object
// so they can be unit-tested for simulated Chrome/Firefox/Safari/Edge.
// ---------------------------------------------------------------------------

// How THIS browser can play a source, best → worst. This classification from
// the source's container/codec fields is the FALLBACK used when the server's
// ffprobe facts (see src/player/playbackPlan.ts) are unavailable.
// - `hls` / `native`      → play directly; the browser HW-decodes everything.
// - `server-remux`        → the server copies the video into fMP4 HLS and
//                           converts the audio (MKV/AVI/TS containers, AC-3/
//                           E-AC-3/DTS/TrueHD audio). No client-side converters.
// - `incompatible`        → video this browser can't decode: only a server
//                           transcode can play it, so it ranks last.
// - `unknown`             → no codec hints; attempt directly and let fallback sort it.
export type StreamStatus =
  | 'hls'
  | 'native'
  | 'server-remux'
  | 'incompatible'
  | 'unknown';

/** `legacy`: VC-1 / MPEG-2 / MPEG-4 ASP (Xvid, DivX): no browser decodes them. */
export type VideoCodec = 'h264' | 'hevc' | 'vp9' | 'av1' | 'legacy' | 'unknown';
export type AudioCodec = 'aac' | 'ac3' | 'eac3' | 'dts' | 'truehd' | 'opus' | 'flac' | 'unknown';
export type Container = 'hls' | 'mp4' | 'webm' | 'mov' | 'mkv' | 'avi' | 'ts' | 'unknown';

export interface BrowserCaps {
  h264: boolean;
  hevc: boolean;
  /** HEVC Main10 / 4K decode (HDR releases); absent ⇒ unknown, treated as false. */
  hevc10?: boolean;
  vp9: boolean;
  av1: boolean;
  aac: boolean;
  ac3: boolean;
  eac3: boolean;
  opus: boolean;
  nativeHls: boolean;
  mseAvailable: boolean;
}

export interface ParsedMedia {
  container: Container;
  videoCodec: VideoCodec;
  audioCodec: AudioCodec;
  isHls: boolean;
  /** Needs HEVC Main10-class decode (2160p or HDR). */
  highDynamicOrUhd: boolean;
  url: string;
}

export interface StreamCompat {
  status: StreamStatus;
  reason?: string;
  parsed: ParsedMedia;
}

let cachedCaps: BrowserCaps | null = null;
// Bumped whenever the memoized caps change, so per-stream classifications made
// against the old caps are recomputed instead of served stale.
let capsVersion = 0;

/**
 * Probe the current browser's real codec support. Memoized for the session
 * because the probe spins up a throwaway `<video>` element and queries
 * `MediaSource.isTypeSupported`, which is wasteful to repeat per stream.
 */
export function getBrowserCaps(): BrowserCaps {
  if (cachedCaps) return cachedCaps;
  const mse = typeof MediaSource !== 'undefined' && typeof MediaSource.isTypeSupported === 'function';
  const mseOk = (type: string) => mse && MediaSource.isTypeSupported(type);
  let canPlay: (type: string) => boolean = () => false;
  try {
    const probe = document.createElement('video');
    canPlay = (type: string) => probe.canPlayType(type) !== '';
  } catch {
    canPlay = () => false;
  }
  cachedCaps = {
    h264: mseOk('video/mp4; codecs="avc1.42E01E"') || canPlay('video/mp4; codecs="avc1.42E01E"'),
    hevc:
      mseOk('video/mp4; codecs="hvc1.1.6.L93.B0"') ||
      canPlay('video/mp4; codecs="hvc1.1.6.L93.B0"') ||
      canPlay('video/mp4; codecs="hev1.1.6.L93.B0"'),
    hevc10: mseOk('video/mp4; codecs="hvc1.2.4.L153.B0"') || canPlay('video/mp4; codecs="hvc1.2.4.L153.B0"'),
    vp9: mseOk('video/webm; codecs="vp9"') || canPlay('video/webm; codecs="vp9"') || mseOk('video/mp4; codecs="vp09.00.10.08"'),
    av1: mseOk('video/mp4; codecs="av01.0.05M.08"') || canPlay('video/mp4; codecs="av01.0.05M.08"'),
    aac:
      mseOk('video/mp4; codecs="mp4a.40.2"') ||
      canPlay('audio/mp4; codecs="mp4a.40.2"') ||
      canPlay('video/mp4; codecs="mp4a.40.2"'),
    ac3: canPlay('audio/mp4; codecs="ac-3"') || mseOk('video/mp4; codecs="ac-3"'),
    eac3: canPlay('audio/mp4; codecs="ec-3"') || mseOk('video/mp4; codecs="ec-3"'),
    opus: mseOk('video/webm; codecs="opus"') || canPlay('audio/ogg; codecs="opus"'),
    nativeHls: canPlay('application/vnd.apple.mpegurl') || canPlay('application/x-mpegURL'),
    mseAvailable: mse,
  };
  return cachedCaps;
}

/** Test-only: reset the memoized capability probe. */
export function resetBrowserCaps() {
  cachedCaps = null;
  capsVersion += 1;
}

/**
 * Session-disable a video codec after a real decode failure (e.g. the browser
 * claimed HEVC support but black-screened). Mutates the memoized caps so sibling
 * sources sharing that codec reclassify as `incompatible` and are skipped by
 * {@link classifyStream}/failover instead of being retried one-by-one. We never
 * disable H.264 (universally decodable — a failure there is a bad source, not a
 * missing decoder), only the GPU-gated codecs.
 */
export function disableVideoCodec(codec: VideoCodec): void {
  const caps = getBrowserCaps();
  if (codec === 'hevc') {
    caps.hevc = false;
    caps.hevc10 = false;
  }
  else if (codec === 'av1') caps.av1 = false;
  else if (codec === 'vp9') caps.vp9 = false;
  capsVersion += 1;
}

// Session flag: Dolby-Vision sources proved unplayable here (MSE rejected a DV
// codec string). DV support varies by browser/OS in ways no upfront probe
// captures, so this is evidence-driven — set only after a real failure, and it
// hides sibling DV releases from selection/failover instead of letting the
// player re-probe each one just to hit the same rejection.
let dolbyVisionDisabled = false;

/** Session-disable DV sources after a real DV playback failure. */
export function disableDolbyVisionPlayback(): void {
  dolbyVisionDisabled = true;
}

export function isDolbyVisionDisabled(): boolean {
  return dolbyVisionDisabled;
}

/** Test-only: reset the session DV-disable flag. */
export function resetDolbyVisionDisabled(): void {
  dolbyVisionDisabled = false;
}

interface MediaCapabilitiesLike {
  decodingInfo: (config: unknown) => Promise<{ supported: boolean; smooth: boolean; powerEfficient: boolean }>;
}

/**
 * Tighten the memoized caps using `navigator.mediaCapabilities`, which — unlike
 * `canPlayType` — reflects whether the platform actually has a *hardware* HEVC
 * decoder. Chrome/Firefox answer `canPlayType('hvc1…')` with "probably" even on
 * machines with no HEVC decoder at all, which would black-screen the moment we
 * attach the source. We require `supported && powerEfficient` (HEVC has no usable
 * software fallback in those browsers) and downgrade `caps.hevc` to false when the
 * GPU can't decode it. Safari keeps HEVC. Call once at player mount, before
 * classifying sources; it only ever tightens caps, never loosens them.
 */
export async function primeBrowserCaps(): Promise<BrowserCaps> {
  const caps = getBrowserCaps();
  if (!caps.hevc) return caps; // already false — nothing to tighten
  const mc = typeof navigator !== 'undefined'
    ? (navigator as Navigator & { mediaCapabilities?: MediaCapabilitiesLike }).mediaCapabilities
    : undefined;
  if (!mc?.decodingInfo) return caps;
  try {
    // 1080p 8-bit Main and 4K 10-bit Main10 are separate questions: plenty of
    // GPUs decode the first and not the second, and most HEVC releases are
    // 2160p HDR10 Main10.
    const [main, main10] = await Promise.all([
      mc.decodingInfo({
        type: 'media-source',
        video: { contentType: 'video/mp4; codecs="hvc1.1.6.L93.B0"', width: 1920, height: 1080, bitrate: 5_000_000, framerate: 24 },
      }),
      mc.decodingInfo({
        type: 'media-source',
        video: { contentType: 'video/mp4; codecs="hvc1.2.4.L153.B0"', width: 3840, height: 2160, bitrate: 20_000_000, framerate: 24 },
      }).catch(() => ({ supported: false, smooth: false, powerEfficient: false })),
    ]);
    const before = `${caps.hevc}:${caps.hevc10}`;
    caps.hevc = Boolean(main.supported && main.powerEfficient);
    caps.hevc10 = caps.hevc && Boolean(main10.supported && main10.powerEfficient);
    if (`${caps.hevc}:${caps.hevc10}` !== before) capsVersion += 1;
  } catch {
    // Leave the canPlayType-derived value in place if the probe throws.
  }
  return caps;
}

function pathExtension(url: string): string {
  const lower = url.toLowerCase();
  const withoutQuery = lower.split(/[?#]/)[0];
  const match = withoutQuery.match(/\.([a-z0-9]{2,4})$/);
  return match ? match[1] : '';
}

const CONTAINERS: Record<string, Container> = {
  hls: 'hls', m3u8: 'hls',
  mp4: 'mp4', m4v: 'mp4', mov: 'mov',
  webm: 'webm',
  mkv: 'mkv', matroska: 'mkv',
  avi: 'avi',
  ts: 'ts', mpegts: 'ts', m2ts: 'ts',
};

function detectContainer(source: PlaybackSource, url: string): Container {
  if (source.delivery === 'hls') return 'hls';
  const declared = String(source.container ?? '').toLowerCase().trim();
  if (declared) return CONTAINERS[declared] ?? 'unknown';
  // A file whose container the server didn't know: its URL extension, if any.
  return CONTAINERS[pathExtension(url)] ?? 'unknown';
}

function detectVideoCodec(value: unknown): VideoCodec {
  const codec = String(value ?? '').toLowerCase();
  if (!codec) return 'unknown';
  if (codec === 'h264' || codec === 'avc') return 'h264';
  if (codec === 'hevc' || codec === 'h265') return 'hevc';
  if (codec === 'av1') return 'av1';
  if (codec === 'vp9') return 'vp9';
  if (['mpeg2', 'mpeg2video', 'mpeg4', 'vc1', 'wmv3', 'xvid', 'divx', 'msmpeg4v3'].includes(codec)) return 'legacy';
  return 'unknown';
}

function detectAudioCodec(value: unknown): AudioCodec {
  const codec = String(value ?? '').toLowerCase();
  if (codec === 'aac') return 'aac';
  if (codec === 'ac3') return 'ac3';
  if (codec === 'eac3') return 'eac3';
  if (codec === 'truehd') return 'truehd';
  if (codec === 'dts' || codec === 'dtshd' || codec.startsWith('dts')) return 'dts';
  if (codec === 'opus') return 'opus';
  if (codec === 'flac') return 'flac';
  return 'unknown';
}

/** Read a source's container and codecs from its structured fields. */
export function parseStreamMedia(source: PlaybackSource): ParsedMedia {
  const url = typeof source.url === 'string' ? source.url : '';
  const container = detectContainer(source, url);
  return {
    container,
    videoCodec: detectVideoCodec(source.video_codec),
    audioCodec: detectAudioCodec(source.audio_codec),
    isHls: container === 'hls',
    highDynamicOrUhd: resolutionScore(source) >= 4 || Boolean(source.hdr),
    url,
  };
}

// `true` = browser can decode, `false` = cannot, `null` = unknown (don't block).
// HEVC files that are 4K or HDR need Main10 decode, not just Main.
function videoSupported(codec: VideoCodec, caps: BrowserCaps, needsMain10 = false): boolean | null {
  switch (codec) {
    case 'h264':
      return caps.h264;
    case 'hevc':
      if (!caps.hevc) return false;
      // `hevc10` undefined (caps from an older probe/tests) ⇒ trust `hevc`.
      return needsMain10 && caps.hevc10 === false ? false : true;
    case 'vp9':
      return caps.vp9;
    case 'av1':
      return caps.av1;
    case 'legacy':
      return false;
    default:
      return null;
  }
}

function audioSupported(codec: AudioCodec, caps: BrowserCaps): boolean | null {
  switch (codec) {
    case 'aac':
      return caps.aac;
    case 'ac3':
      return caps.ac3;
    case 'eac3':
      return caps.eac3;
    case 'opus':
      return caps.opus;
    case 'dts':
    case 'truehd':
      return false;
    default:
      return null; // flac / unknown — let the element try.
  }
}

// Container families the browser opens through <video src> directly.
const NATIVE_CONTAINERS: Container[] = ['mp4', 'webm', 'mov'];
// Containers the browser won't open; the server re-wraps them into fMP4 HLS
// without touching the video bitstream.
const REMUXABLE_CONTAINERS: Container[] = ['mkv', 'avi', 'ts'];

/**
 * Classify a source for the current browser from its container/codec fields.
 * See {@link StreamStatus}. Only the fallback for when server inspection failed;
 * every server path keeps the video bitstream untouched unless transcoding is
 * the last resort.
 */
// The source list, failover and the Sources panel all ask for this repeatedly —
// memoize per source.
const classifyCache = new WeakMap<PlaybackSource, { caps: BrowserCaps; version: number; result: StreamCompat }>();

export function classifyStream(stream: PlaybackSource, caps: BrowserCaps = getBrowserCaps()): StreamCompat {
  const hit = classifyCache.get(stream);
  if (hit && hit.caps === caps && hit.version === capsVersion) return hit.result;
  const result = classifyUncached(stream, caps);
  classifyCache.set(stream, { caps, version: capsVersion, result });
  return result;
}

function classifyUncached(stream: PlaybackSource, caps: BrowserCaps): StreamCompat {
  const parsed = parseStreamMedia(stream);
  if (parsed.isHls) {
    return { status: 'hls', parsed };
  }

  const videoOk = videoSupported(parsed.videoCodec, caps, parsed.highDynamicOrUhd);
  const audioOk = audioSupported(parsed.audioCodec, caps);
  const nativeContainer = NATIVE_CONTAINERS.includes(parsed.container);
  const remuxContainer = REMUXABLE_CONTAINERS.includes(parsed.container);

  // Video this browser can't decode can only play through a server transcode.
  if (videoOk === false) {
    return { status: 'incompatible', reason: `${parsed.videoCodec.toUpperCase()} video not supported in this browser`, parsed };
  }

  if (nativeContainer) {
    if (audioOk !== false) return { status: 'native', parsed };
    return { status: 'server-remux', reason: `${parsed.audioCodec.toUpperCase()} audio converted on server`, parsed };
  }

  if (remuxContainer) {
    if (audioOk === false) {
      return { status: 'server-remux', reason: `${parsed.container.toUpperCase()} + ${parsed.audioCodec.toUpperCase()} audio prepared on server`, parsed };
    }
    return { status: 'server-remux', reason: `${parsed.container.toUpperCase()} prepared on server`, parsed };
  }

  // Unknown container with a URL — attempt directly; auto-fallback covers misses.
  return { status: parsed.url ? 'unknown' : 'incompatible', reason: parsed.url ? undefined : 'No playable URL', parsed };
}

const STATUS_RANK: Record<StreamStatus, number> = {
  hls: 100,
  native: 90,
  'server-remux': 30,
  unknown: 20,
  incompatible: 5,
};

/**
 * Tiebreak score among same-status sources: prefer formats that direct-play most
 * reliably across browsers — MP4/MOV containers, H.264 video (universal hardware
 * decode), and AAC/Opus audio — so a clean MP4/H.264/AAC sibling auto-wins over an
 * equally-ranked source that would lean on the in-browser remux/transcode path.
 */
export function webFriendlyScore(stream: PlaybackSource): number {
  const parsed = parseStreamMedia(stream);
  let score = 0;
  if (parsed.container === 'mp4' || parsed.container === 'mov') score += 2;
  if (parsed.videoCodec === 'h264') score += 2;
  else if (parsed.videoCodec === 'av1' || parsed.videoCodec === 'vp9') score += 1;
  if (parsed.audioCodec === 'aac' || parsed.audioCodec === 'opus') score += 1;
  return score;
}

/** 4 for 2160p, 3 for 1080p, 2 for 720p, 1 for SD, 0 when unknown. */
export function resolutionScore(source: PlaybackSource): number {
  const resolution = String(source.resolution ?? '').toLowerCase();
  const height = Number(source.height ?? 0);
  if (resolution === '2160p' || height >= 1600) return 4;
  if (resolution === '1080p' || height >= 900) return 3;
  if (resolution === '720p' || height >= 600) return 2;
  if (resolution === '480p' || resolution === '576p' || resolution === '360p' || (height > 0 && height < 600)) return 1;
  return 0;
}

/** File size in MB from `size_bytes`; 0 when unknown. */
export function sizeScore(source: PlaybackSource): number {
  const bytes = Number(source.size_bytes ?? 0);
  return Number.isFinite(bytes) && bytes > 0 ? bytes / (1024 * 1024) : 0;
}

/** Capability-aware rank; higher = try first. Replaces the old extension rank. */
export function browserStreamRank(stream: PlaybackSource, caps: BrowserCaps = getBrowserCaps()): number {
  return STATUS_RANK[classifyStream(stream, caps).status];
}

export function sortBrowserStreams(streams: PlaybackSource[], caps: BrowserCaps = getBrowserCaps()): PlaybackSource[] {
  // The server order owns quality and size; the browser only moves sources
  // between capability buckets (stable within one), so a direct-playable source
  // is never stuck behind one this browser can't open. It reorders by what it can actually do:
  // direct play before in-browser rescue before server conversion, and Dolby
  // Vision (no browser ladder) last. Within a bucket, server order is kept.
  return streams
    .map((stream, index) => ({
      stream,
      index,
      rank: browserStreamRank(stream, caps),
      dv: isDolbyVision(stream) ? 1 : 0,
    }))
    .sort((a, b) => {
      if (b.rank !== a.rank) return b.rank - a.rank;
      if (a.dv !== b.dv) return a.dv - b.dv;
      return a.index - b.index; // stable: server order
    })
    .map((entry) => entry.stream);
}

/** Sources the current browser can actually play (hides incompatible ones,
 * and — once a DV source has actually failed here — Dolby Vision ones). */
export function playableStreams(streams: PlaybackSource[], caps: BrowserCaps = getBrowserCaps()): PlaybackSource[] {
  return streams.filter(
    (stream) =>
      classifyStream(stream, caps).status !== 'incompatible' &&
      !(dolbyVisionDisabled && isDolbyVision(stream)),
  );
}
