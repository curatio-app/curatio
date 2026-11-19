import type { PlanKind } from './playbackPlan';
import type { FailureSignal } from './recovery';

// ---------------------------------------------------------------------------
// Playback state machine + watchdog timing. Pure and clock-injected so the
// player's lifecycle rules are unit-testable without a media element.
//
//   resolving → connecting → loading → playing ⇄ buffering
//                                 ↘ recovering ↗        ↘ postplay → ended
//   any → error
//
// `paused` is a flag, not a phase: a paused title is still "playing" from the
// machine's point of view, and pausing never counts against a source.
// ---------------------------------------------------------------------------

export type Phase =
  | 'idle'
  | 'resolving'
  | 'connecting'
  | 'loading'
  | 'playing'
  | 'buffering'
  | 'recovering'
  | 'postplay'
  | 'ended'
  | 'error';

export interface PlayerError {
  code: string;
  message: string;
}

export interface MachineState {
  phase: Phase;
  /** When the current phase began (ms). */
  since: number;
  paused: boolean;
  /** The path of the current attempt, for the phase label. */
  path: PlanKind | null;
  /** First frame of this item has been shown at least once. */
  startedOnce: boolean;
  error: PlayerError | null;
  /** Shown while recovering ("Reconnecting…", "Switching source…"). */
  note: string | null;
}

export type MachineEvent =
  | { type: 'RESET' }
  | { type: 'RESOLVE' }
  | { type: 'CONNECT'; path: PlanKind }
  | { type: 'MEDIA_ATTACHED' }
  | { type: 'FIRST_FRAME' }
  | { type: 'PLAYING' }
  | { type: 'PAUSE' }
  | { type: 'WAITING' }
  | { type: 'RECOVER'; note: string }
  | { type: 'POSTPLAY' }
  | { type: 'DISMISS_POSTPLAY' }
  | { type: 'ENDED' }
  | { type: 'FAIL'; error: PlayerError };

export function initialMachineState(now = 0): MachineState {
  return { phase: 'idle', since: now, paused: false, path: null, startedOnce: false, error: null, note: null };
}

function to(state: MachineState, phase: Phase, now: number, patch: Partial<MachineState> = {}): MachineState {
  if (state.phase === phase && !Object.keys(patch).length) return state;
  return { ...state, ...patch, phase, since: state.phase === phase ? state.since : now };
}

/** Transition function. Unknown/irrelevant events leave the state untouched. */
export function playbackReducer(state: MachineState, event: MachineEvent, now: number): MachineState {
  switch (event.type) {
    case 'RESET':
      return initialMachineState(now);
    case 'RESOLVE':
      return to(state, 'resolving', now, { error: null, note: null });
    case 'CONNECT':
      // A recovery keeps its note visible until the new attempt produces frames.
      return to(state, state.phase === 'recovering' ? 'recovering' : 'connecting', now, { path: event.path, error: null });
    case 'MEDIA_ATTACHED':
      if (state.phase === 'connecting') return to(state, 'loading', now);
      return state;
    case 'FIRST_FRAME':
    case 'PLAYING':
      if (state.phase === 'error' || state.phase === 'ended') return state;
      if (state.phase === 'postplay') return { ...state, paused: false };
      return to(state, 'playing', now, { startedOnce: true, note: null, paused: event.type === 'PLAYING' ? false : state.paused });
    case 'PAUSE':
      return state.paused ? state : { ...state, paused: true };
    case 'WAITING':
      // Startup and recovery waits are not rebuffers; only steady playback buffers.
      if (state.phase === 'playing') return to(state, 'buffering', now);
      return state;
    case 'RECOVER':
      return to(state, 'recovering', now, { note: event.note, error: null });
    case 'POSTPLAY':
      return state.phase === 'playing' || state.phase === 'buffering' ? to(state, 'postplay', now) : state;
    case 'DISMISS_POSTPLAY':
      return state.phase === 'postplay' ? to(state, 'playing', now) : state;
    case 'ENDED':
      return to(state, 'ended', now, { paused: true });
    case 'FAIL':
      return to(state, 'error', now, { error: event.error, note: null });
    default:
      return state;
  }
}

/** Phases in which the startup/loading UI is relevant. */
export function isLoadingPhase(phase: Phase): boolean {
  return phase === 'resolving' || phase === 'connecting' || phase === 'loading' || phase === 'recovering' || phase === 'buffering';
}

// ---- Loading UI timing (spec): spinner after 500 ms, label after 3 s, "Still loading" at 8 s.
export const SPINNER_DELAY_MS = 500;
export const PHASE_LABEL_DELAY_MS = 3000;
export const STILL_LOADING_MS = 8000;

export interface LoadingUi {
  spinner: boolean;
  label: string | null;
  stillLoading: boolean;
}

/** Mid-playback stall long enough for "Still buffering…" (native tier 1). */
export const STILL_BUFFERING_MS = 8000;
/** Native copy once the source keeps stalling (tier 2), see `isStruggling`. */
export const STRUGGLING_LABEL = 'This source is struggling — still trying…';
export const STILL_WORKING_LABEL = 'Still working — this stream is taking a moment…';

/** The tvOS/iOS wording for each loading phase. Mid-playback buffering stays silent until 8 s. */
export function phaseLabel(state: MachineState, elapsed = Number.POSITIVE_INFINITY): string | null {
  if (state.phase === 'recovering' && state.note) return state.note;
  if (state.phase === 'resolving') return 'Checking sources…';
  if (state.phase === 'buffering') return elapsed >= STILL_BUFFERING_MS ? 'Still buffering…' : null;
  return 'Loading…';
}

/** Native tier 2: at least 6 s stalled across 2+ stalls in the window. */
export function isStruggling(recent: { stalledMs: number; count: number }): boolean {
  return recent.count >= 2 && recent.stalledMs >= 6000;
}

export function loadingUi(state: MachineState, now: number): LoadingUi {
  if (!isLoadingPhase(state.phase)) return { spinner: false, label: null, stillLoading: false };
  const elapsed = Math.max(0, now - state.since);
  // A recovery note is shown at once: the viewer already saw playback stop.
  const immediate = state.phase === 'recovering';
  return {
    spinner: immediate || elapsed >= SPINNER_DELAY_MS,
    label: immediate || elapsed >= PHASE_LABEL_DELAY_MS ? phaseLabel(state, elapsed) : null,
    stillLoading: state.phase !== 'buffering' && elapsed >= STILL_LOADING_MS,
  };
}

// ---- End-of-file rule ----

/** Seconds before the end inside which a file end counts as completion. */
export function completionWindow(duration: number): number {
  return Math.max(30, duration * 0.02);
}

/**
 * A file end is completion only at/after duration − max(30 s, 2 %) or past the
 * credits marker. Anything earlier is a premature EOF: recover, never mark
 * watched, never autoplay.
 */
export function isCompletion(position: number, duration: number, creditsStart?: number | null): boolean {
  if (creditsStart != null && Number.isFinite(creditsStart) && position >= creditsStart) return true;
  if (!Number.isFinite(duration) || duration <= 0) return false;
  return position >= duration - completionWindow(duration);
}

// ---- Stall watchdog ----

// Native stall timeout (DevicePerformanceProfile, normal devices).
export const STALL_RECOVERY_MS = 30_000;
export const SEEK_GRACE_MS = 3_000;
const MIN_ADVANCE_SECONDS = 0.25;

/**
 * Declares a stall only after STALL_RECOVERY_MS with no playhead progress AND
 * no new bytes (buffered end not advancing). Pausing, resuming and seeking
 * reset the clock; time spent paused or hidden never counts.
 */
export class StallWatchdog {
  private lastLifeAt: number;
  private lastPosition = -1;
  private lastBufferedEnd = -1;
  private graceUntil = 0;

  constructor(now = 0) {
    this.lastLifeAt = now;
  }

  /** Pause, resume, a new attempt: start counting from now. */
  reset(now: number) {
    this.lastLifeAt = now;
    this.lastPosition = -1;
    this.lastBufferedEnd = -1;
  }

  /** A seek: reset and give the rebuffer a short grace. */
  noteSeek(now: number) {
    this.reset(now);
    this.graceUntil = now + SEEK_GRACE_MS;
  }

  /** Feed the latest playhead and buffered end (seconds). */
  observe(position: number, bufferedEnd: number, now: number) {
    if (this.lastPosition < 0 || Math.abs(position - this.lastPosition) >= MIN_ADVANCE_SECONDS) {
      if (this.lastPosition >= 0) this.lastLifeAt = now;
      this.lastPosition = position;
    }
    if (bufferedEnd > this.lastBufferedEnd + 0.01) {
      if (this.lastBufferedEnd >= 0) this.lastLifeAt = now;
      this.lastBufferedEnd = bufferedEnd;
    }
  }

  /** Bytes arrived (a `progress` event) even if buffered ranges didn't move yet. */
  noteBytes(now: number) {
    this.lastLifeAt = now;
  }

  /** True when the source should be treated as stalled. */
  isStalled(now: number, conditions: { paused: boolean; hidden: boolean; active: boolean }): boolean {
    if (!conditions.active || conditions.paused || conditions.hidden) {
      this.lastLifeAt = now;
      return false;
    }
    if (now < this.graceUntil) return false;
    return now - this.lastLifeAt >= STALL_RECOVERY_MS;
  }
}

// ---- Rebuffer counter ----

// Native count rule: 3 rebuffers within 90 s.
export const REBUFFER_WINDOW_MS = 90_000;
export const REBUFFER_LIMIT = 3;

/**
 * Counts genuine rebuffers: a `waiting` during steady playback. Startup,
 * resume-from-pause and seek waits are excluded by the caller passing
 * `steady: false` (the machine is not in `playing` then, or a grace is active).
 */
export class RebufferCounter {
  private times: number[] = [];

  reset() {
    this.times = [];
  }

  /** Returns true when the limit is reached (and resets the window). */
  note(now: number, steady: boolean): boolean {
    if (!steady) return false;
    this.times = this.times.filter((at) => now - at < REBUFFER_WINDOW_MS);
    this.times.push(now);
    if (this.times.length >= REBUFFER_LIMIT) {
      this.times = [];
      return true;
    }
    return false;
  }
}

// ---- Chronic buffering (native ChronicBufferingTracker) ----

/**
 * Normal-device policy from native `DevicePerformanceProfile`: within a 90 s
 * window a source may stall up to 18 s once watched for the whole window
 * (a 20% duty cycle), prorated for shorter spans with a 12 s floor; it must
 * have shown frames for 30 s and stalled at least 3 distinct times.
 */
export const CHRONIC_BUFFERING_POLICY = {
  windowMs: 90_000,
  steadyStalledMs: 18_000,
  minimumStalledMs: 12_000,
  minimumObservationMs: 30_000,
  minimumStallCount: 3,
} as const;

/**
 * Catches the stream that stutters all the time yet always moves before the
 * no-progress stall timer fires and never trips the count rule quickly.
 * Intervals open on a steady `waiting` and close when frames flow again; a
 * pause or seek discards the open one. Every clock input is passed in.
 */
export class ChronicBufferingTracker {
  private closed: Array<{ start: number; end: number }> = [];
  private openStart: number | null = null;

  reset() {
    this.closed = [];
    this.openStart = null;
  }

  open(now: number) {
    if (this.openStart == null) this.openStart = now;
  }

  close(now: number) {
    const start = this.openStart;
    this.openStart = null;
    if (start != null && now > start) this.closed.push({ start, end: now });
  }

  /** Stalled time and stall count inside the window, for the "struggling" copy. */
  recent(now: number, windowMs: number = CHRONIC_BUFFERING_POLICY.windowMs): { stalledMs: number; count: number } {
    const windowStart = now - windowMs;
    let stalledMs = 0;
    let count = 0;
    for (const interval of this.closed) {
      if (interval.end <= windowStart) continue;
      stalledMs += interval.end - Math.max(interval.start, windowStart);
      count += 1;
    }
    if (this.openStart != null) {
      stalledMs += Math.max(0, now - Math.max(this.openStart, windowStart));
      count += 1;
    }
    return { stalledMs, count };
  }

  /** The open stall has an innocent cause (pause, seek): drop it uncounted. */
  discardOpen() {
    this.openStart = null;
  }

  /** Whether the source has buffered chronically, given when it started showing frames. */
  shouldEscalate(now: number, observationStart: number | null, policy = CHRONIC_BUFFERING_POLICY): boolean {
    const windowStart = now - policy.windowMs;
    this.closed = this.closed.filter((interval) => interval.end > windowStart);
    let stalled = this.closed.reduce((sum, interval) => sum + Math.max(0, interval.end - Math.max(interval.start, windowStart)), 0);
    let count = this.closed.length;
    if (this.openStart != null) {
      stalled += Math.max(0, now - Math.max(this.openStart, windowStart));
      count += 1;
    }
    const observed = observationStart == null ? 0 : Math.min(policy.windowMs, Math.max(0, now - observationStart));
    const prorated = policy.steadyStalledMs * (observed / policy.windowMs);
    const required = Math.min(Math.max(prorated, policy.minimumStalledMs), policy.steadyStalledMs);
    return observed >= policy.minimumObservationMs && count >= policy.minimumStallCount && stalled >= required;
  }
}

// ---- Long pause ----

/** A pause at least this long may have outlived the source's connection or session. */
export const LONG_PAUSE_MS = 90_000;
/** After Play following a long pause, how long the playhead gets to move before a reload. */
export const LONG_PAUSE_PROBE_MS = 4_000;
const LONG_PAUSE_MIN_ADVANCE_SECONDS = 0.25;

/**
 * After a long pause and Play, reload the stream at the position when the
 * playhead hasn't moved: the connection or server session expired while paused.
 */
export function shouldReloadAfterLongPause(params: {
  pausedForMs: number;
  positionAtPlay: number;
  positionNow: number;
  stillPaused: boolean;
}): boolean {
  const { pausedForMs, positionAtPlay, positionNow, stillPaused } = params;
  if (stillPaused || !(pausedForMs >= LONG_PAUSE_MS)) return false;
  if (!Number.isFinite(positionNow) || !Number.isFinite(positionAtPlay)) return true;
  return Math.abs(positionNow - positionAtPlay) < LONG_PAUSE_MIN_ADVANCE_SECONDS;
}

/**
 * Network-type failures that arrive while the viewer has paused are held until
 * they press Play: an idle connection dropping is not a reason to switch
 * source or show an error over a paused frame. Decode and format errors are
 * never held — they say nothing about idleness.
 */
export function isHeldWhilePaused(signal: FailureSignal): boolean {
  switch (signal.kind) {
    case 'hls':
      return signal.fatalType === 'network';
    case 'transport':
    case 'session-gone':
    case 'stall':
    case 'rebuffer':
      return true;
    case 'server':
      return signal.status == null || signal.status >= 500;
    case 'media-error':
      return signal.code === 2; // MEDIA_ERR_NETWORK
    default:
      return false;
  }
}
