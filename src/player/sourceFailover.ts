import { hasLocalEngine } from '../api/desktopBridge';
import type { PlaybackSource, ProbeVerdict } from '../api/types';
import {
  type BrowserCaps,
  classifyStream,
  getBrowserCaps,
  isDolbyVisionDisabled,
  resolutionScore,
  sizeScore,
} from '../api/streamCompat';
import { streamUrl } from '../api/mediaUtils';
import {
  isDolbyVision,
  isOpaqueRoute,
  providerIdentity,
  routeIdentity,
} from '../api/streamMeta';
import { releasePriority } from './sourceReadiness';

export { isDolbyVision };

// ---------------------------------------------------------------------------
// Failover selection: a port of the native `SourceFailoverSelector` so every
// client picks the same next source.
//
// Ordinary failures keep the SERVER's order (library first, then addons, then
// quality) and only prefer, in turn: the same file on
// another provider (network/file failures), then any other provider.
//
// Only an explicit device failure (`preferLowerTier`) or repeated bandwidth
// failure (`preferLowerBitrate`) switches to the local recovery ranking.
//
// Web-only, ahead of everything: a source this browser can't decode, or can
// only play by transcoding, is never preferred over one it can play.
// ---------------------------------------------------------------------------

export type PreflightState = 'passed' | 'failed' | 'pending';

export interface FailoverContext {
  /** The browser-playable, server-ordered source list. */
  streams: PlaybackSource[];
  /** Indices already attempted and failed (never re-selected). */
  tried: ReadonlySet<number>;
  /** The index that just failed; drives provider rotation. */
  currentIndex: number | null;
  /** Backend preflight verdicts, keyed by source URL. */
  verdicts: ReadonlyMap<string, ProbeVerdict>;
  /** Injectable for tests; defaults to the live browser probe. */
  caps?: BrowserCaps;
  triedRoutes?: ReadonlySet<string>;
  /** Providers that reported themselves unavailable this session (native excludedProviders). */
  excludedProviders?: ReadonlySet<string>;
  /** A network/server/file failure: the same file on another provider is the best bet. */
  preferSameFile?: boolean;
  /** A device/decode failure: drop a resolution tier (native preferLowerTier). */
  preferLowerTier?: boolean;
  /** A second consecutive rebuffering failure: pick a lighter bitrate (native preferLowerBitrate). */
  preferLowerBitrate?: boolean;
  /** A decode failure on HDR: put Dolby-Vision-only sources last. */
  preferHdrFallback?: boolean;
  /** Title runtime in seconds, to estimate bitrates from file sizes. */
  runtimeSeconds?: number | null;
  /** Measured throughput (see bandwidthSampler.ts); the budget is 75% of it. */
  measuredMbps?: number | null;
  /** Resolution cap as a tier (0 SD, 1 720p, 2 1080p, 3 4K). Default 4K. */
  maximumTier?: number;
  /**
   * The playback plan per candidate (see playbackPlan.ts). When given it
   * replaces codec-field classification: unplayable sources are skipped and
   * transcode-only ones stay eligible but rank behind everything decodable.
   */
  planOf?: (stream: PlaybackSource, index: number) => { kind: string; score: number };
}

/** Backend preflight outcome for a source (no verdict yet ⇒ pending). */
export function preflightState(
  stream: PlaybackSource,
  verdicts: ReadonlyMap<string, ProbeVerdict>,
): PreflightState {
  const verdict = verdicts.get(streamUrl(stream));
  if (isOpaqueRoute(stream) || !verdict || verdict.state === 'indeterminate' || verdict.reason === 'timeout') return 'pending';
  return verdict.state === 'passed' ? 'passed' : 'failed';
}

/** Native `resolutionTier`: 0 SD, 1 720p, 2 1080p, 3 4K. */
export function resolutionTier(stream: PlaybackSource): number {
  return Math.max(0, resolutionScore(stream) - 1);
}

function fileSizeBytes(stream: PlaybackSource): number | null {
  const mb = sizeScore(stream);
  return mb > 0 ? mb * 1024 * 1024 : null;
}

/** Average bitrate in Mbps: the server's `bitrate`, else size over runtime; null when unknown. */
export function estimatedMbps(stream: PlaybackSource, runtimeSeconds?: number | null): number | null {
  const bitrate = Number(stream.bitrate ?? 0);
  if (Number.isFinite(bitrate) && bitrate > 0) return bitrate / 1_000_000;
  const size = fileSizeBytes(stream);
  const runtime = runtimeSeconds && runtimeSeconds > 0 ? runtimeSeconds : Number(stream.duration_seconds ?? 0);
  if (!size || !runtime || runtime <= 0) return null;
  return (size * 8) / runtime / 1_000_000;
}

/** Resolution-dominant quality score (size as a within-tier tiebreak). */
function qualityScore(stream: PlaybackSource): number {
  return resolutionScore(stream) * 100000 + Math.min(sizeScore(stream), 99999);
}

/** The server reports `hdr: "dv"` for Dolby Vision; it has no HDR10 fallback to rely on here. */
function isDolbyVisionOnly(stream: PlaybackSource): boolean {
  return isDolbyVision(stream);
}

function otherProvider(stream: PlaybackSource, current: PlaybackSource | null): boolean {
  if (!current) return false;
  const provider = providerIdentity(current);
  const alternate = providerIdentity(stream);
  // Unknown identities cannot establish an independent provider.
  return Boolean(provider && alternate && provider !== alternate);
}

/** Web-only lead: can't-decode-well sources rank behind ones this client plays as-is. */
function playabilityKey(stream: PlaybackSource, index: number, ctx: FailoverContext): number[] {
  const transcodeLast = ctx.planOf?.(stream, index).kind === 'server-transcode' ? 1 : 0;
  // Browsers often can't show Dolby Vision; the desktop engine plays its HDR10 base.
  const dvHere = isDolbyVision(stream) && !hasLocalEngine() ? 1 : 0;
  return [transcodeLast, dvHere];
}

/** The lexicographic sort key for one candidate (lower wins). */
export function failoverSortKey(stream: PlaybackSource, index: number, ctx: FailoverContext): number[] {
  const current = ctx.currentIndex != null ? ctx.streams[ctx.currentIndex] ?? null : null;
  const lead = playabilityKey(stream, index, ctx);
  if (!ctx.preferLowerTier && !ctx.preferLowerBitrate) {
    const sameFile = Boolean(
      ctx.preferSameFile && current?.file_id
      && current.file_id === stream.file_id && otherProvider(stream, current),
    );
    const compatibility = ctx.preferHdrFallback && isDolbyVisionOnly(stream) ? 1 : 0;
    return [...lead, compatibility, sameFile ? 0 : 1, otherProvider(stream, current) ? 0 : 1, index];
  }

  // Recovery ranking (native SourceFailoverSelector.rankedSourceIndexes, TV form).
  const tier = resolutionTier(stream);
  const rate = estimatedMbps(stream, ctx.runtimeSeconds);
  const currentRate = current ? estimatedMbps(current, ctx.runtimeSeconds) : null;
  let recovery = tier > (ctx.maximumTier ?? 3) ? 100 : 0;
  if (ctx.preferLowerTier && current) {
    const currentTier = resolutionTier(current);
    recovery += tier < currentTier ? currentTier - tier : 100 + (tier - currentTier);
  }
  if (ctx.preferLowerBitrate && currentRate != null) {
    recovery += rate == null ? 25 : rate < currentRate * 0.8 ? 0 : 50;
  }
  const budget = ctx.measuredMbps && ctx.measuredMbps > 0 ? ctx.measuredMbps * 0.75 : null;
  const bandwidthBand = budget == null ? 0 : rate == null ? 1 : rate <= budget ? 0 : 2;
  // When every known rate exceeds the estimate, try the lightest first.
  const overBudgetRate = bandwidthBand === 2 ? rate ?? 0 : 0;
  const passed = preflightState(stream, ctx.verdicts) === 'passed' ? 0 : 1;
  const size = fileSizeBytes(stream);
  const comparable = rate ?? (size ? size / 1_000_000_000 : null);
  return [
    ...lead,
    recovery,
    bandwidthBand,
    overBudgetRate,
    releasePriority(stream),
    passed,
    comparable == null ? 1 : 0,
    -(comparable ?? 0),
    -tier,
    stream.kind === 'library' ? 0 : 1,
    otherProvider(stream, current) ? 0 : 1,
    -qualityScore(stream),
    index,
  ];
}

function compareKeys(a: number[], b: number[]): number {
  for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
    if ((a[i] ?? 0) !== (b[i] ?? 0)) return (a[i] ?? 0) - (b[i] ?? 0);
  }
  return 0;
}

/** Native `isCandidate`: untried route, not probe-failed, playable here, provider not excluded. */
function isCandidate(stream: PlaybackSource, index: number, ctx: FailoverContext, caps: BrowserCaps): boolean {
  if (index === ctx.currentIndex) return false;
  if (ctx.tried.has(index) || ctx.triedRoutes?.has(routeIdentity(stream))) return false;
  if (!streamUrl(stream)) return false;
  if (preflightState(stream, ctx.verdicts) === 'failed') return false;
  const provider = providerIdentity(stream);
  if (provider && ctx.excludedProviders?.has(provider)) return false;
  if (ctx.planOf) {
    if (ctx.planOf(stream, index).kind === 'unplayable') return false;
  } else if (classifyStream(stream, caps).status === 'incompatible') return false;
  // A DV source already failed this session: its siblings will hit the same
  // MSE rejection, so don't spend a probe on each of them.
  if (isDolbyVisionDisabled() && isDolbyVision(stream)) return false;
  return true;
}

/** Every remaining candidate in pick order (best first). */
export function rankSources(ctx: FailoverContext): number[] {
  const caps = ctx.caps ?? getBrowserCaps();
  return ctx.streams
    .map((stream, index) => ({ stream, index }))
    .filter(({ stream, index }) => isCandidate(stream, index, ctx, caps))
    .map(({ stream, index }) => ({ index, key: failoverSortKey(stream, index, ctx) }))
    .sort((a, b) => compareKeys(a.key, b.key))
    .map(({ index }) => index);
}

/** Pick the next source to try, or `null` when every remaining candidate is exhausted. */
export function selectNextSource(ctx: FailoverContext): number | null {
  return rankSources(ctx)[0] ?? null;
}

/** Keep existing indices stable while appending genuinely renewed routes. */
export function mergeRecoveryStreams(existing: PlaybackSource[], refreshed: PlaybackSource[]): PlaybackSource[] {
  const merged = [...existing];
  const seen = new Set(existing.map(routeIdentity));
  for (const stream of refreshed) {
    const identity = routeIdentity(stream);
    if (!seen.has(identity)) {
      merged.push(stream);
      seen.add(identity);
    }
  }
  return merged;
}

/**
 * Native `shouldPreferLighterSource`: a device failure always, a rebuffering
 * failure only when the previous automatic failover was also rebuffering.
 */
export function shouldPreferLighterSource(reason: string | null, previousReason: string | null): boolean {
  if (reason === 'device') return true;
  return reason === 'rebuffer' && previousReason === 'rebuffer';
}
