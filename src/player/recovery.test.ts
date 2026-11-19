import { describe, expect, it } from 'vitest';
import {
  playsSilently,
  classifyFailure,
  decideRecovery,
  MAX_RERESOLVES_PER_ROUTE,
  recordAction,
  RATE_LIMIT_RETRY_MS,
  RecoveryLedger,
  RELOAD_BACKOFF_MS,
  type DecideContext,
  type FailureSignal,
} from './recovery';

function ctx(overrides: Partial<DecideContext> = {}): DecideContext {
  return {
    hadPlayed: true, path: 'direct', proxyEligible: false, videoCodec: 'h264',
    sourceKey: 'route-a', canReResolve: true, sourceIsHls: false, ...overrides,
  };
}

function step(ledger: RecoveryLedger, signal: FailureSignal, context: DecideContext) {
  const action = decideRecovery(ledger, classifyFailure(signal, context), context);
  recordAction(ledger, action, context.sourceKey);
  return action;
}

describe('error taxonomy', () => {
  it('assigns categories and user-facing codes', () => {
    expect(classifyFailure({ kind: 'startup-timeout' }, ctx()).code).toBe('CU-NET-01');
    expect(classifyFailure({ kind: 'stall' }, ctx()).code).toBe('CU-NET-02');
    expect(classifyFailure({ kind: 'media-error', code: 3 }, ctx())).toMatchObject({ category: 'DECODE', verifiedDecode: true });
    // Code 4 after playing is an expired link, not a codec problem.
    expect(classifyFailure({ kind: 'media-error', code: 4 }, ctx()).category).toBe('EXPIRED');
    // Code 4 before the first frame is a source problem, never a decode verdict.
    expect(classifyFailure({ kind: 'media-error', code: 4 }, ctx({ hadPlayed: false }))).toMatchObject({ category: 'SOURCE', verifiedDecode: false });
    expect(classifyFailure({ kind: 'hls', fatalType: 'network', httpStatus: 404 }, ctx()).category).toBe('GONE');
    expect(classifyFailure({ kind: 'server', status: 401 }, ctx()).category).toBe('AUTH');
    expect(classifyFailure({ kind: 'server', status: 502, message: 'upstream 403 Forbidden' }, ctx()).category).toBe('EXPIRED');
  });
});

describe('recovery ladder', () => {
  it('reloads with 1/2/4 s backoff, then re-resolves the route, then fails over', () => {
    const ledger = new RecoveryLedger();
    const delays = [0, 1, 2].map(() => step(ledger, { kind: 'stall' }, ctx()));
    expect(delays.map((action) => (action.type === 'reload' ? action.delayMs : -1))).toEqual([...RELOAD_BACKOFF_MS]);
    expect(step(ledger, { kind: 'stall' }, ctx()).type).toBe('reresolve');
    expect(step(ledger, { kind: 'stall' }, ctx()).type).toBe('reresolve');
    expect(step(ledger, { kind: 'stall' }, ctx()).type).toBe('reresolve');
    expect(ledger.reResolveCount('route-a')).toBe(MAX_RERESOLVES_PER_ROUTE);
    expect(step(ledger, { kind: 'stall' }, ctx()).type).toBe('failover');
  });

  it('re-resolves an expired link directly (no pointless reloads)', () => {
    const ledger = new RecoveryLedger();
    expect(step(ledger, { kind: 'media-error', code: 4 }, ctx()).type).toBe('reresolve');
  });

  it('keeps failing over without a cap, like tvOS/iOS (the source list itself is the limit)', () => {
    const ledger = new RecoveryLedger();
    const start = ctx({ hadPlayed: false });
    for (let i = 0; i < 6; i += 1) expect(step(ledger, { kind: 'startup-timeout' }, start).type).toBe('failover');
  });

  it('retries a never-played direct source once through the proxy before blaming it', () => {
    const ledger = new RecoveryLedger();
    const fresh = ctx({ hadPlayed: false, proxyEligible: true });
    expect(step(ledger, { kind: 'media-error', code: null }, fresh)).toMatchObject({ type: 'reload', viaProxy: true, delayMs: 0 });
    expect(step(ledger, { kind: 'media-error', code: null }, { ...fresh, proxyEligible: false })).toMatchObject({ type: 'failover' });
  });

  it('a decode error on a direct H.264 source changes path to server-hls, once', () => {
    const ledger = new RecoveryLedger();
    expect(step(ledger, { kind: 'media-error', code: 3 }, ctx())).toMatchObject({ type: 'path-change', to: 'server-hls' });
    expect(step(ledger, { kind: 'media-error', code: 3 }, ctx({ path: 'server-hls' }))).toMatchObject({ type: 'failover', disableScope: false });
  });

  it('only a verified decode error of a GPU-gated codec disables that codec scope', () => {
    const ledger = new RecoveryLedger();
    expect(step(ledger, { kind: 'media-error', code: 3 }, ctx({ videoCodec: 'hevc' }))).toMatchObject({ type: 'failover', disableScope: true });
    // A format error before playback is unverified: the server gets a chance instead.
    const unverified = new RecoveryLedger();
    expect(step(unverified, { kind: 'media-error', code: 4 }, ctx({ videoCodec: 'hevc', hadPlayed: false }))).toMatchObject({ type: 'path-change' });
  });

  it('HLS source failures never escalate to server preparation', () => {
    const ledger = new RecoveryLedger();
    const hls = ctx({ path: 'hls', sourceIsHls: true, hadPlayed: false });
    const action = step(ledger, { kind: 'hls', fatalType: 'media' }, hls);
    expect(action.type).toBe('failover');
    expect(step(new RecoveryLedger(), { kind: 'media-error', code: 3 }, hls).type).not.toBe('path-change');
  });

  it('a pruned prepared segment restarts the server session', () => {
    const ledger = new RecoveryLedger();
    expect(step(ledger, { kind: 'hls', fatalType: 'network', httpStatus: 404 }, ctx({ path: 'server-hls' })).type).toBe('restart-session');
    expect(step(ledger, { kind: 'session-gone' }, ctx({ path: 'server-hls' })).type).toBe('restart-session');
  });

  it('premature EOF enters the ladder (reload at position), never completion', () => {
    const action = step(new RecoveryLedger(), { kind: 'premature-end', position: 1200, duration: 6000 }, ctx());
    expect(action).toMatchObject({ type: 'reload', delayMs: 1000 });
  });

  it('a user action resets incident budgets and the exhaustion refresh', () => {
    const ledger = new RecoveryLedger();
    ledger.reloads = 2;
    ledger.exhaustionUsed = true;
    ledger.resetForUserAction();
    expect(ledger.reloads).toBe(0);
    expect(ledger.exhaustionUsed).toBe(false);
  });
});

describe('phase B classification', () => {
  it('treats a server 502 "Source probe failed" as an expired link and re-resolves', () => {
    const signal: FailureSignal = { kind: 'server', status: 502, message: 'Source probe failed: Invalid data found when processing input' };
    expect(classifyFailure(signal, ctx({ path: 'server-hls' })).category).toBe('EXPIRED');
    expect(step(new RecoveryLedger(), signal, ctx({ path: 'server-hls' })).type).toBe('reresolve');
  });

  it('treats an HTML or JSON body where media belonged as expired', () => {
    for (const message of [
      '<!DOCTYPE html><html><body>Link expired</body></html>',
      'Upstream returned text/html instead of media',
      'Upstream returned application/json instead of media',
      '{"error":"infringing_file"}',
    ]) {
      expect(classifyFailure({ kind: 'server', status: 502, message }, ctx()).category).toBe('EXPIRED');
    }
    // A hls.js 502 from the proxy/session is the upstream failing too.
    expect(classifyFailure({ kind: 'hls', fatalType: 'network', httpStatus: 502 }, ctx({ path: 'server-hls' })).category).toBe('EXPIRED');
    // A generic server failure without those markers stays an engine error.
    expect(classifyFailure({ kind: 'server', status: 500, message: 'ffmpeg crashed' }, ctx()).category).toBe('ENGINE');
  });

  it('waits and retries a 429 once, then fails over', () => {
    const ledger = new RecoveryLedger();
    const signal: FailureSignal = { kind: 'server', status: 429, message: 'busy' };
    const first = step(ledger, signal, ctx({ path: 'server-hls' }));
    expect(first).toMatchObject({ type: 'reload', rateLimit: true, delayMs: RATE_LIMIT_RETRY_MS, viaProxy: false });
    // The retry does not spend the reload backoff budget.
    expect(ledger.reloads).toBe(0);
    expect(step(ledger, signal, ctx({ path: 'server-hls' })).type).toBe('failover');
  });

  it('honours (and caps) the server Retry-After on a 429', () => {
    expect(step(new RecoveryLedger(), { kind: 'server', status: 429, retryAfterMs: 5000 }, ctx())).toMatchObject({ type: 'reload', delayMs: 5000 });
    expect(step(new RecoveryLedger(), { kind: 'server', status: 429, retryAfterMs: 120_000 }, ctx())).toMatchObject({ delayMs: 10_000 });
    const hls = step(new RecoveryLedger(), { kind: 'hls', fatalType: 'network', httpStatus: 429 }, ctx({ path: 'server-hls' }));
    expect(hls).toMatchObject({ type: 'reload', rateLimit: true });
  });

  it('media error 4 before the first frame never disables a codec', () => {
    const ledger = new RecoveryLedger();
    const fresh = ctx({ hadPlayed: false, videoCodec: 'hevc' });
    // Direct: the server gets a chance to remux (its probe spots an expired link).
    expect(step(ledger, { kind: 'media-error', code: 4 }, fresh)).toMatchObject({ type: 'path-change', to: 'server-hls' });
    // Already prepared on the server: refresh the link, never disable the scope.
    const again = step(ledger, { kind: 'media-error', code: 4 }, { ...fresh, path: 'server-hls' });
    expect(again.type).toBe('reresolve');
    const noRoute = step(new RecoveryLedger(), { kind: 'media-error', code: 4 }, { ...fresh, path: 'server-hls', canReResolve: false });
    expect(noRoute).toMatchObject({ type: 'failover', disableScope: false });
  });

  it('a transcode-only refusal (422 desktop hint) fails over without a retry', () => {
    const action = step(new RecoveryLedger(), { kind: 'server', status: 422, message: 'Needs transcoding', hint: 'desktop_app' }, ctx({ path: 'server-hls' }));
    expect(action).toMatchObject({ type: 'failover', disableScope: false });
  });
});

describe('silent direct play', () => {
  it('flags a playing source that decoded no audio (TrueHD/DTS the browser drops without an error)', () => {
    expect(playsSilently({ webkitAudioDecodedByteCount: 0, muted: false, volume: 1 }, null)).toBe(true);
    expect(playsSilently({ webkitAudioDecodedByteCount: 0, muted: false, volume: 1 }, 2)).toBe(true);
    expect(playsSilently({ webkitAudioDecodedByteCount: 48000, muted: false, volume: 1 }, null)).toBe(false);
  });

  it('is not fooled by a muted player, a source with no audio track, or a browser without the counter', () => {
    expect(playsSilently({ webkitAudioDecodedByteCount: 0, muted: true, volume: 1 }, null)).toBe(false);
    expect(playsSilently({ webkitAudioDecodedByteCount: 0, muted: false, volume: 0 }, null)).toBe(false);
    expect(playsSilently({ webkitAudioDecodedByteCount: 0, muted: false, volume: 1 }, 0)).toBe(false);
    expect(playsSilently({ muted: false, volume: 1 }, null)).toBe(false);
  });

  it('moves the source to the prepared path, which converts its audio (once per source)', () => {
    const ledger = new RecoveryLedger();
    const direct = ctx({ path: 'direct' });
    const first = step(ledger, { kind: 'no-audio' }, direct);
    expect(first).toMatchObject({ type: 'path-change', to: 'server-hls' });
    expect(classifyFailure({ kind: 'no-audio' }, direct).code).toBe('CU-SOURCE-04');
    // Already switched once: don't loop.
    expect(step(ledger, { kind: 'no-audio' }, direct).type).not.toBe('path-change');
  });
});
