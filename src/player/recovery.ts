import type { PlanKind } from './playbackPlan';

// ---------------------------------------------------------------------------
// Playback error taxonomy and recovery ladder. Pure: the player feeds every
// failure (element error, engine error, watchdog, server) through ONE entry
// point, classifies it here, and executes the returned action.
//
//   ① reload the same URL at the last good position (backoff 1/2/4 s)
//   ② re-resolve the same route (refresh=true, matched by route identity),
//      at most 3 times per route per session
//   ③ fail over to the next compatible source, keeping position and the
//      audio/subtitle language, with a "Switched to …" toast
//
// At most 2 automatic failovers per incident, then the error screen. One
// exhaustion refresh per session (never clears exclusions). A codec is only
// disabled after a VERIFIED decode error, scoped to codec+profile. HLS source
// failures never escalate to server preparation.
// ---------------------------------------------------------------------------

/**
 * SOURCE: the element could not open the source before any frame — the bytes
 * were wrong (an HTML/JSON error page, a truncated file, a container the
 * element rejects). Never proof of a codec problem, so it never disables one.
 */
export type ErrorCategory = 'NET' | 'EXPIRED' | 'GONE' | 'DECODE' | 'SOURCE' | 'ENGINE' | 'AUTH';

export type FailureSignal =
  /** No signal of life (metadata, manifest, buffered bytes, advance) in the startup budget. */
  | { kind: 'startup-timeout' }
  /** No playhead progress AND no bytes arriving for the stall window. */
  | { kind: 'stall' }
  /** Repeated rebuffers during steady playback: the source can't keep up. */
  | { kind: 'rebuffer' }
  /** HTMLMediaElement error (code 1-4, or null when the element gave none). */
  | { kind: 'media-error'; code: number | null; message?: string }
  /** hls.js fatal error after its own bounded recovery. */
  | { kind: 'hls'; fatalType: 'network' | 'media' | 'other'; httpStatus?: number | null; details?: string; message?: string }
  /**
   * Starting a server-prepared session failed. `hint` carries the server's
   * machine-readable hint (e.g. "desktop_app" for transcode-only media when
   * server transcoding is off); `retryAfterMs` comes from a 429 Retry-After.
   */
  | { kind: 'server'; status: number | null; message?: string; hint?: string | null; retryAfterMs?: number | null }
  /** The prepared session or its segments are gone (keepalive / fragment 404). */
  | { kind: 'session-gone' }
  /** Playback advanced but no video frame ever presented. */
  | { kind: 'no-frames' }
  /** Playing directly with a picture but no audio decoded (e.g. TrueHD/DTS the browser silently drops). */
  | { kind: 'no-audio' }
  /** `ended`/EOF well before the real end of the title. */
  | { kind: 'premature-end'; position: number; duration: number }
  /** The source has no playable URL. */
  | { kind: 'no-url' }
  /** Engine/transport error with only a message. */
  | { kind: 'transport'; message: string };

export interface ClassifiedFailure {
  category: ErrorCategory;
  /** User-visible code, e.g. CU-NET-02. */
  code: string;
  /** Plain-language message for the error screen / source list. */
  message: string;
  /** A decode error proven by the element (not a guess from a format error). */
  verifiedDecode: boolean;
  signal: FailureSignal;
}

export interface FailureContext {
  /** This attempt rendered frames / advanced the playhead. */
  hadPlayed: boolean;
  /** Path the failing attempt used. */
  path: PlanKind;
  /** Direct attempt whose bytes did not yet go through the same-origin proxy. */
  proxyEligible: boolean;
  /** Video codec family of the failing source ("h264", "hevc", … or null). */
  videoCodec: string | null;
}

function failure(category: ErrorCategory, n: number, message: string, signal: FailureSignal, verifiedDecode = false): ClassifiedFailure {
  return { category, code: `CU-${category}-${String(n).padStart(2, '0')}`, message, signal, verifiedDecode };
}

const EXPIRED_TEXT = /expired|forbidden|\b403\b|\b410\b|signature/i;
/** The server's ffprobe of the upstream URL failed: almost always a dead/stale link. */
const PROBE_FAILED_TEXT = /probe failed/i;
/** An error page (HTML) or API error (JSON) came back where media bytes belonged. */
const ERROR_BODY_TEXT = /<!doctype|<html|text\/html|application\/json|returned (?:an? )?(?:html|json)|^\s*[{<]/i;

/** Default wait before the single retry of a rate-limited (429) request. */
export const RATE_LIMIT_RETRY_MS = 3000;

/** Map a raw failure signal onto the taxonomy. */
export function classifyFailure(signal: FailureSignal, ctx: FailureContext): ClassifiedFailure {
  switch (signal.kind) {
    case 'startup-timeout':
      return failure('NET', 1, 'The stream timed out. It may not be cached or the server is not responding.', signal);
    case 'stall':
      return failure('NET', 2, 'The stream timed out. It may not be cached or the server is not responding.', signal);
    case 'rebuffer':
      return failure('NET', 3, 'This source buffered repeatedly and could not keep up with playback.', signal);
    case 'premature-end':
      return failure('NET', 6, 'The stream ended early.', signal);
    case 'media-error':
      if (signal.code === 3) return failure('DECODE', 1, 'This browser could not decode the video.', signal, true);
      if (signal.code === 2) return failure('NET', 4, 'A network error interrupted playback.', signal);
      if (signal.code === 4) {
        // After it played, "not supported" is almost always the link going stale.
        // Before the first frame it is a source problem (error page, bad bytes,
        // rejected container) — never evidence against a codec.
        return ctx.hadPlayed
          ? failure('EXPIRED', 1, 'The source link expired.', signal)
          : failure('SOURCE', 1, 'The browser could not open this source.', signal);
      }
      return failure('NET', 5, signal.message || 'Playback failed.', signal);
    case 'no-frames':
      return failure('DECODE', 2, 'No video frames rendered — the codec is likely unsupported.', signal, true);
    case 'no-audio':
      // SOURCE on a direct attempt switches to the prepared path, which converts the audio.
      return failure('SOURCE', 4, "This source's audio can't be played directly.", signal);
    case 'hls':
      if (signal.httpStatus === 401) return failure('AUTH', 1, 'Your session ended. Sign in again.', signal);
      if (signal.httpStatus === 404) return failure('GONE', 1, 'Part of the stream is no longer available.', signal);
      if (signal.httpStatus === 403 || signal.httpStatus === 410) return failure('EXPIRED', 2, 'The source link expired.', signal);
      if (signal.httpStatus === 429) return failure('NET', 9, 'The source is rate limiting requests.', signal);
      // A 502 from the proxy/session means the upstream stopped serving media.
      if (signal.httpStatus === 502) return failure('EXPIRED', 6, 'The source stopped responding.', signal);
      if (signal.fatalType === 'media') return failure('ENGINE', 1, signal.message || 'The stream could not be decoded.', signal);
      return failure('NET', 5, signal.message || 'The stream could not be loaded.', signal);
    case 'server':
      if (signal.status === 401) return failure('AUTH', 1, 'Your session ended. Sign in again.', signal);
      if (signal.status === 429) return failure('ENGINE', 3, signal.message || 'The server is preparing too many streams right now.', signal);
      if (signal.status === 422) return failure('ENGINE', 4, signal.message || 'This source needs full transcoding.', signal);
      if (signal.message && (PROBE_FAILED_TEXT.test(signal.message) || ERROR_BODY_TEXT.test(signal.message))) {
        return failure('EXPIRED', 4, 'The source link expired.', signal);
      }
      if (signal.message && EXPIRED_TEXT.test(signal.message)) return failure('EXPIRED', 3, 'The source link expired.', signal);
      if (signal.status === 404 || signal.status === 410) return failure('GONE', 2, signal.message || 'The source is gone.', signal);
      return failure('ENGINE', 2, signal.message || 'The server could not prepare this source.', signal);
    case 'session-gone':
      return failure('GONE', 3, 'The prepared stream ended on the server.', signal);
    case 'no-url':
      return failure('ENGINE', 5, 'This source does not include a playable URL.', signal);
    case 'transport':
    default:
      return failure('NET', 5, (signal as { message?: string }).message || 'Playback failed.', signal);
  }
}

export const RELOAD_BACKOFF_MS = [1000, 2000, 4000] as const;
export const MAX_RERESOLVES_PER_ROUTE = 3;
/** Continuous healthy playback that closes an incident. */
export const INCIDENT_HEALTHY_MS = 30_000;

export type RecoveryAction =
  /** `rateLimit`: the one wait-and-retry granted to a 429 before failing over. */
  | { type: 'reload'; delayMs: number; viaProxy: boolean; reason: string; rateLimit?: boolean }
  | { type: 'reresolve'; reason: string }
  | { type: 'path-change'; to: Extract<PlanKind, 'server-hls' | 'server-transcode'>; reason: string }
  | { type: 'restart-session'; reason: string }
  | { type: 'failover'; delayMs: number; disableScope: boolean; reason: string }
  | { type: 'fatal'; reason: string };

/** Per-playback-session recovery bookkeeping (one per player item). */
export class RecoveryLedger {
  /** Same-source reloads in the current incident. */
  reloads = 0;
  /** Re-resolves per route identity (whole session). */
  readonly reResolves = new Map<string, number>();
  /** Source keys (attempt-independent) already retried through the proxy. */
  readonly proxyRetried = new Set<string>();
  /** Source keys whose path was already escalated after a failure. */
  readonly pathChanged = new Set<string>();
  /** Source keys that already used their single 429 wait-and-retry. */
  readonly rateLimited = new Set<string>();
  exhaustionUsed = false;

  /** The incident is over: playback has been healthy long enough. */
  closeIncident() {
    this.reloads = 0;
  }

  /** A deliberate user choice (manual source, Try Again) starts fresh budgets. */
  resetForUserAction() {
    this.closeIncident();
    this.exhaustionUsed = false;
  }

  reResolveCount(route: string) {
    return this.reResolves.get(route) ?? 0;
  }

  noteReResolve(route: string) {
    this.reResolves.set(route, this.reResolveCount(route) + 1);
  }
}

export interface DecideContext extends FailureContext {
  /** Stable key of the failing source (route identity). */
  sourceKey: string;
  /** Whether a route id exists to re-resolve against. */
  canReResolve: boolean;
  /** The source itself is HLS (never escalate to server preparation). */
  sourceIsHls: boolean;
}

// No cap on automatic switches: like tvOS/iOS, playback keeps moving through
// the remaining sources, then refreshes the list once, before it gives up.
function failover(_ledger: RecoveryLedger, reason: string, delayMs = 0, disableScope = false): RecoveryAction {
  return { type: 'failover', delayMs, disableScope, reason };
}

function reResolveOr(ledger: RecoveryLedger, ctx: DecideContext, reason: string, otherwise: () => RecoveryAction): RecoveryAction {
  if (ctx.canReResolve && ledger.reResolveCount(ctx.sourceKey) < MAX_RERESOLVES_PER_ROUTE) {
    return { type: 'reresolve', reason };
  }
  return otherwise();
}

function reloadOr(ledger: RecoveryLedger, ctx: DecideContext, reason: string): RecoveryAction {
  if (ledger.reloads < RELOAD_BACKOFF_MS.length) {
    return { type: 'reload', delayMs: RELOAD_BACKOFF_MS[ledger.reloads], viaProxy: false, reason };
  }
  return reResolveOr(ledger, ctx, reason, () => failover(ledger, reason));
}

/**
 * Choose the next recovery step. Does not mutate the ledger — the caller
 * records what it actually executed with {@link recordAction}.
 */
export function decideRecovery(ledger: RecoveryLedger, classified: ClassifiedFailure, ctx: DecideContext): RecoveryAction {
  const { signal, category } = classified;
  if (category === 'AUTH') return { type: 'fatal', reason: classified.message };

  // A direct source that never produced anything gets one retry through the
  // same-origin byte proxy first (Safari's strict byte-range handling, broken
  // CORS) before it is blamed for its format or abandoned.
  const elementOpenFailure = signal.kind === 'media-error' && signal.code !== 3;
  if (!ctx.hadPlayed && ctx.path === 'direct' && ctx.proxyEligible && elementOpenFailure
    && !ledger.proxyRetried.has(ctx.sourceKey)) {
    return { type: 'reload', delayMs: 0, viaProxy: true, reason: 'Retrying through the Curatio proxy' };
  }

  if (signal.kind === 'startup-timeout' || signal.kind === 'no-url') {
    return failover(ledger, classified.message);
  }

  if (signal.kind === 'rebuffer') return failover(ledger, classified.message, 0);

  // Rate limited (server busy or the media host throttling): wait once, then move on.
  const status = signal.kind === 'server' ? signal.status : signal.kind === 'hls' ? signal.httpStatus : null;
  if (status === 429) {
    if (!ledger.rateLimited.has(ctx.sourceKey)) {
      const wait = signal.kind === 'server' && signal.retryAfterMs && signal.retryAfterMs > 0
        ? Math.min(signal.retryAfterMs, 10_000)
        : RATE_LIMIT_RETRY_MS;
      return { type: 'reload', delayMs: wait, viaProxy: false, reason: classified.message, rateLimit: true };
    }
    return failover(ledger, classified.message);
  }

  if (signal.kind === 'session-gone') {
    if (ledger.reloads < RELOAD_BACKOFF_MS.length) return { type: 'restart-session', reason: classified.message };
    return failover(ledger, classified.message);
  }

  // HLS sources: hls.js already retried in place; never escalate to a server remux.
  if (ctx.sourceIsHls || ctx.path === 'hls') {
    if (category === 'EXPIRED' || category === 'GONE') {
      return reResolveOr(ledger, ctx, classified.message, () => failover(ledger, classified.message));
    }
    if (category === 'NET' && ctx.hadPlayed) return reloadOr(ledger, ctx, classified.message);
    return failover(ledger, classified.message, category === 'NET' ? 500 : 0);
  }

  switch (category) {
    case 'EXPIRED':
      return reResolveOr(ledger, ctx, classified.message, () => failover(ledger, classified.message));
    case 'GONE':
      if (ctx.path === 'server-hls' || ctx.path === 'server-transcode') {
        // A fragment/session 404 on a prepared stream: start a new session there.
        if (signal.kind === 'hls' && ledger.reloads < RELOAD_BACKOFF_MS.length) {
          return { type: 'restart-session', reason: classified.message };
        }
      }
      return reResolveOr(ledger, ctx, classified.message, () => failover(ledger, classified.message));
    case 'SOURCE':
      // The element rejected what it got. A direct container may still play once
      // the server remuxes it (the server's own probe then tells an expired link
      // apart); otherwise refresh the link before giving up on the source.
      if (ctx.path === 'direct' && !ledger.pathChanged.has(ctx.sourceKey)) {
        return { type: 'path-change', to: 'server-hls', reason: classified.message };
      }
      return reResolveOr(ledger, ctx, classified.message, () => failover(ledger, classified.message));
    case 'DECODE': {
      const h264ish = !ctx.videoCodec || ctx.videoCodec === 'h264' || ctx.videoCodec === 'unknown';
      if (ctx.path === 'direct' && !ledger.pathChanged.has(ctx.sourceKey) && (h264ish || !classified.verifiedDecode)) {
        // The container or audio is the likely culprit: let the server prepare it.
        return { type: 'path-change', to: 'server-hls', reason: classified.message };
      }
      // A verified decode failure of a GPU-gated codec: disable that codec+profile
      // so its siblings rank as transcode-last, and move on.
      return failover(ledger, classified.message, 0, classified.verifiedDecode && !h264ish);
    }
    case 'ENGINE':
      if (signal.kind === 'server' && signal.status === 422) {
        // Needs transcoding: other sources first; the transcode stays a last resort.
        return failover(ledger, classified.message);
      }
      return failover(ledger, classified.message, signal.kind === 'server' && signal.status === 429 ? 0 : 500);
    case 'NET':
    default:
      if (ctx.hadPlayed || signal.kind === 'stall' || signal.kind === 'premature-end') {
        return reloadOr(ledger, ctx, classified.message);
      }
      return failover(ledger, classified.message, 500);
  }
}

/** Record the action the player actually executed. */
export function recordAction(ledger: RecoveryLedger, action: RecoveryAction, sourceKey: string) {
  switch (action.type) {
    case 'reload':
      if (action.rateLimit) ledger.rateLimited.add(sourceKey);
      else if (action.viaProxy) ledger.proxyRetried.add(sourceKey);
      else ledger.reloads += 1;
      break;
    case 'restart-session':
      ledger.reloads += 1;
      break;
    case 'reresolve':
      ledger.noteReResolve(sourceKey);
      break;
    case 'path-change':
      ledger.pathChanged.add(sourceKey);
      break;
    default:
      break;
  }
}


/**
 * Whether a directly-played source is running silent: Chromium shows the picture
 * of an MKV/MP4 whose audio it can't decode (TrueHD, DTS, ...) and raises no
 * error. `decodedAudioBytes` is `webkitAudioDecodedByteCount` (undefined in
 * browsers without it: never flagged there). A source known to have no audio
 * track, or a muted element, is not a failure.
 */
export function playsSilently(
  video: { webkitAudioDecodedByteCount?: number; muted: boolean; volume: number },
  knownAudioTracks: number | null,
): boolean {
  if (typeof video.webkitAudioDecodedByteCount !== 'number') return false;
  if (video.muted || video.volume === 0 || knownAudioTracks === 0) return false;
  return video.webkitAudioDecodedByteCount === 0;
}
