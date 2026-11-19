import { describe, expect, it } from 'vitest';
import {
  ChronicBufferingTracker,
  isStruggling,
  phaseLabel,
  initialMachineState,
  isCompletion,
  isHeldWhilePaused,
  LONG_PAUSE_MS,
  loadingUi,
  playbackReducer,
  RebufferCounter,
  REBUFFER_LIMIT,
  REBUFFER_WINDOW_MS,
  SEEK_GRACE_MS,
  shouldReloadAfterLongPause,
  STALL_RECOVERY_MS,
  StallWatchdog,
  type MachineEvent,
  type MachineState,
} from './playbackMachine';

function run(events: Array<[MachineEvent, number]>, start = initialMachineState(0)): MachineState {
  return events.reduce((state, [event, at]) => playbackReducer(state, event, at), start);
}

describe('playback machine', () => {
  it('walks resolving → connecting → loading → playing ⇄ buffering', () => {
    let state = run([[{ type: 'RESOLVE' }, 0], [{ type: 'CONNECT', path: 'direct' }, 10], [{ type: 'MEDIA_ATTACHED' }, 20]]);
    expect(state.phase).toBe('loading');
    state = playbackReducer(state, { type: 'FIRST_FRAME' }, 30);
    expect(state).toMatchObject({ phase: 'playing', startedOnce: true });
    state = playbackReducer(state, { type: 'WAITING' }, 40);
    expect(state.phase).toBe('buffering');
    expect(playbackReducer(state, { type: 'PLAYING' }, 50).phase).toBe('playing');
  });

  it('startup waits are not buffering, and pause is a flag', () => {
    const loading = run([[{ type: 'CONNECT', path: 'server-hls' }, 0], [{ type: 'MEDIA_ATTACHED' }, 1]]);
    expect(playbackReducer(loading, { type: 'WAITING' }, 2).phase).toBe('loading');
    const paused = playbackReducer(playbackReducer(loading, { type: 'FIRST_FRAME' }, 3), { type: 'PAUSE' }, 4);
    expect(paused).toMatchObject({ phase: 'playing', paused: true });
  });

  it('loading UI: spinner after 500 ms, tvOS label after 3 s, still-working note at 8 s', () => {
    const state = run([[{ type: 'CONNECT', path: 'server-hls' }, 1000]]);
    expect(loadingUi(state, 1400)).toEqual({ spinner: false, label: null, stillLoading: false });
    expect(loadingUi(state, 1500).spinner).toBe(true);
    expect(loadingUi(state, 4000).label).toBe('Loading…');
    expect(loadingUi(state, 9000).stillLoading).toBe(true);
  });

  it('mid-playback buffering is silent until "Still buffering…" at 8 s; repeated stalls read as struggling', () => {
    expect(phaseLabel({ ...initialMachineState(0), phase: 'buffering' }, 4000)).toBeNull();
    expect(phaseLabel({ ...initialMachineState(0), phase: 'buffering' }, 8000)).toBe('Still buffering…');
    expect(phaseLabel({ ...initialMachineState(0), phase: 'resolving' })).toBe('Checking sources…');
    expect(isStruggling({ stalledMs: 6000, count: 2 })).toBe(true);
    expect(isStruggling({ stalledMs: 9000, count: 1 })).toBe(false);
  });

  it('errors and recovery notes', () => {
    const failed = playbackReducer(initialMachineState(0), { type: 'FAIL', error: { code: 'CU-NET-01', message: 'x' } }, 1);
    expect(failed.phase).toBe('error');
    expect(playbackReducer(failed, { type: 'PLAYING' }, 2).phase).toBe('error');
    const recovering = playbackReducer(initialMachineState(0), { type: 'RECOVER', note: 'Reconnecting' }, 1);
    expect(loadingUi(recovering, 1)).toMatchObject({ spinner: true, label: 'Reconnecting' });
  });
});

describe('end-of-file rule', () => {
  it('counts completion only within max(30 s, 2 %) of the end or past credits', () => {
    expect(isCompletion(5900, 6000)).toBe(true); // 2 % of 6000 = 120 s
    expect(isCompletion(5870, 6000)).toBe(false);
    expect(isCompletion(1180, 1200)).toBe(true); // 30 s floor
    expect(isCompletion(1100, 1200)).toBe(false);
    expect(isCompletion(1100, 1200, 1090)).toBe(true);
    expect(isCompletion(100, 0)).toBe(false);
  });
});

describe('stall watchdog', () => {
  it('pause, then a long resume wait, does NOT count as a stall', () => {
    const dog = new StallWatchdog(0);
    dog.observe(100, 120, 0);
    // Paused for ten minutes: never a stall, and the clock keeps resetting.
    for (let t = 1000; t <= 600_000; t += 1000) expect(dog.isStalled(t, { paused: true, hidden: false, active: true })).toBe(false);
    dog.reset(600_000); // resume
    expect(dog.isStalled(600_000 + STALL_RECOVERY_MS - 1, { paused: false, hidden: false, active: true })).toBe(false);
  });

  it('stalls only with no progress AND no bytes for the full window', () => {
    const dog = new StallWatchdog(0);
    dog.observe(10, 20, 0);
    dog.observe(10, 25, 10_000); // bytes still arriving
    expect(dog.isStalled(20_000, { paused: false, hidden: false, active: true })).toBe(false);
    expect(dog.isStalled(10_000 + STALL_RECOVERY_MS, { paused: false, hidden: false, active: true })).toBe(true);
  });

  it('hidden tabs and seek grace never stall', () => {
    const dog = new StallWatchdog(0);
    expect(dog.isStalled(60_000, { paused: false, hidden: true, active: true })).toBe(false);
    dog.noteSeek(60_000);
    expect(dog.isStalled(60_000 + SEEK_GRACE_MS - 1, { paused: false, hidden: false, active: true })).toBe(false);
  });
});

describe('rebuffer counter', () => {
  it('ignores startup/resume/seek waits and trips on repeated steady rebuffers', () => {
    const counter = new RebufferCounter();
    for (let i = 0; i < 10; i += 1) expect(counter.note(i * 1000, false)).toBe(false);
    const results = Array.from({ length: REBUFFER_LIMIT }, (_, i) => counter.note(i * 5000, true));
    expect(results.at(-1)).toBe(true);
    expect(results.slice(0, -1).every((value) => !value)).toBe(true);
  });
});

describe('rebuffer threshold', () => {
  it('trips at 3 rebuffers within 90 s (native), not when they are spread out', () => {
    expect(REBUFFER_LIMIT).toBe(3);
    expect(REBUFFER_WINDOW_MS).toBe(90_000);
    const counter = new RebufferCounter();
    expect(counter.note(0, true)).toBe(false);
    expect(counter.note(91_000, true)).toBe(false);
    expect(counter.note(182_000, true)).toBe(false);
    expect(counter.note(210_000, true)).toBe(false);
    expect(counter.note(240_000, true)).toBe(true);
  });
});

describe('long pause', () => {
  it('reloads after Play only when a pause of at least 90 s left the playhead stuck', () => {
    expect(LONG_PAUSE_MS).toBe(90_000);
    const stuck = { positionAtPlay: 600, positionNow: 600.1, stillPaused: false };
    expect(shouldReloadAfterLongPause({ ...stuck, pausedForMs: 90_000 })).toBe(true);
    expect(shouldReloadAfterLongPause({ ...stuck, pausedForMs: 89_999 })).toBe(false);
    expect(shouldReloadAfterLongPause({ ...stuck, pausedForMs: 300_000, positionNow: 603 })).toBe(false);
    expect(shouldReloadAfterLongPause({ ...stuck, pausedForMs: 300_000, stillPaused: true })).toBe(false);
  });

  it('holds network failures while paused, never decode failures', () => {
    expect(isHeldWhilePaused({ kind: 'hls', fatalType: 'network' })).toBe(true);
    expect(isHeldWhilePaused({ kind: 'transport', message: 'socket closed' })).toBe(true);
    expect(isHeldWhilePaused({ kind: 'session-gone' })).toBe(true);
    expect(isHeldWhilePaused({ kind: 'media-error', code: 2 })).toBe(true);
    expect(isHeldWhilePaused({ kind: 'server', status: 503 })).toBe(true);
    expect(isHeldWhilePaused({ kind: 'hls', fatalType: 'media' })).toBe(false);
    expect(isHeldWhilePaused({ kind: 'media-error', code: 3 })).toBe(false);
    expect(isHeldWhilePaused({ kind: 'server', status: 422 })).toBe(false);
    expect(isHeldWhilePaused({ kind: 'premature-end', position: 1, duration: 2 })).toBe(false);
  });
});

describe('chronic buffering (native ChronicBufferingTracker)', () => {
  it('escalates a source that keeps stalling briefly, once watched long enough', () => {
    const tracker = new ChronicBufferingTracker();
    const frames = 0;
    // Three 5 s stalls between 30 s and 60 s of viewing: 15 s ≥ the 12 s floor.
    for (const start of [31_000, 41_000, 51_000]) {
      tracker.open(start);
      tracker.close(start + 5_000);
    }
    expect(tracker.shouldEscalate(56_000, frames)).toBe(true);
  });

  it('leaves a source alone before 30 s of frames, with fewer than 3 stalls, or under budget', () => {
    const early = new ChronicBufferingTracker();
    for (const start of [1_000, 8_000, 15_000]) { early.open(start); early.close(start + 5_000); }
    expect(early.shouldEscalate(20_000, 0)).toBe(false);

    const twice = new ChronicBufferingTracker();
    for (const start of [31_000, 45_000]) { twice.open(start); twice.close(start + 10_000); }
    expect(twice.shouldEscalate(56_000, 0)).toBe(false);

    const light = new ChronicBufferingTracker();
    for (const start of [31_000, 41_000, 51_000]) { light.open(start); light.close(start + 2_000); }
    expect(light.shouldEscalate(56_000, 0)).toBe(false);
  });

  it('a pause or seek discards the open stall, and old stalls age out of the window', () => {
    const tracker = new ChronicBufferingTracker();
    for (const start of [31_000, 41_000]) { tracker.open(start); tracker.close(start + 6_000); }
    tracker.open(51_000);
    tracker.discardOpen();
    expect(tracker.shouldEscalate(60_000, 0)).toBe(false);
    tracker.open(61_000);
    tracker.close(67_000);
    expect(tracker.shouldEscalate(67_000, 0)).toBe(true);
    expect(tracker.shouldEscalate(67_000 + 200_000, 0)).toBe(false);
  });
});
