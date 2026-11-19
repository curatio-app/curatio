// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { makeSource } from '../api/testFixtures';
import type { PlaybackSource, ProbeVerdict } from '../api/types';
import { probeTargets, releasePriority, selectInitialSource, type ProbeFn } from './sourceReadiness';

type Variant = 'original' | 'optimized';
const source = (id: string, variant: Variant = 'original', provider = 'plex', kind: 'library' | 'addon' = 'library'): PlaybackSource => makeSource({
  id, label: id, url: `https://media.example/${id}`, variant, provider_id: provider, kind, route_id: id,
});

const passed: ProbeVerdict = { state: 'passed', reason: null, status: 206 };
const failed: ProbeVerdict = { state: 'failed', reason: 'http_error', status: 404 };

/** Probe stub: per-id verdict after a per-id delay. */
function probeWith(plan: Record<string, { verdict: ProbeVerdict; delay?: number }>): ProbeFn & { calls: string[] } {
  const calls: string[] = [];
  const fn = ((url: string) => {
    const id = url.split('/').pop() ?? '';
    calls.push(id);
    const step = plan[id] ?? { verdict: passed };
    return new Promise<ProbeVerdict>((done) => setTimeout(() => done(step.verdict), step.delay ?? 0));
  }) as ProbeFn & { calls: string[] };
  fn.calls = calls;
  return fn;
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe('releasePriority', () => {
  it('ranks original before optimized, and library before addon, from structured fields', () => {
    expect([
      source('a', 'original', 'plex', 'library'),
      source('b', 'original', 'addon-x', 'addon'),
      source('c', 'optimized', 'plex', 'library'),
      source('d', 'optimized', 'addon-x', 'addon'),
    ].map(releasePriority)).toEqual([0, 1, 2, 3]);
  });
});

describe('probeTargets', () => {
  it('includes another provider and a different source class (native parity)', () => {
    const sources = [source('a'), source('b'), source('c', 'original', 'jellyfin'), source('d'), source('web', 'optimized')];
    expect(probeTargets([0, 1, 2, 3, 4], sources)).toEqual([0, 2, 4, 1]);
  });
});

describe('selectInitialSource', () => {
  it('waits for the preferred source within budget instead of taking a faster fallback', async () => {
    const sources = [source('preferred'), source('fallback', 'optimized')];
    const probe = probeWith({ preferred: { verdict: passed, delay: 75 }, fallback: { verdict: passed, delay: 0 } });
    const result = selectInitialSource(sources, { probe, budgetMs: 300 });
    await vi.advanceTimersByTimeAsync(100);
    await expect(result).resolves.toBe(0);
  });

  it('skips a source whose probe failed', async () => {
    const sources = [source('dead'), source('alive', 'optimized')];
    const probe = probeWith({ dead: { verdict: failed } });
    const result = selectInitialSource(sources, { probe });
    await vi.advanceTimersByTimeAsync(10);
    await expect(result).resolves.toBe(1);
  });

  it('keeps the preferred source at the deadline when its probe is merely slow', async () => {
    const sources = [source('slow'), source('fast', 'optimized')];
    const probe = probeWith({ slow: { verdict: passed, delay: 10_000 } });
    const result = selectInitialSource(sources, { probe, budgetMs: 80 });
    await vi.advanceTimersByTimeAsync(80);
    await expect(result).resolves.toBe(0);
  });

  it('starts an addon best route immediately without probing it', async () => {
    const sources = [source('opaque', 'original', 'addon-x', 'addon'), source('direct')];
    const probe = probeWith({});
    await expect(selectInitialSource(sources, { probe })).resolves.toBe(0);
    expect(probe.calls).toEqual([]);
  });

  it('reports every verdict so failover can reuse it', async () => {
    const sources = [source('a'), source('b', 'optimized')];
    const onVerdict = vi.fn();
    const probe = probeWith({ a: { verdict: failed }, b: { verdict: passed, delay: 5 } });
    const result = selectInitialSource(sources, { probe, onVerdict });
    await vi.advanceTimersByTimeAsync(10);
    await expect(result).resolves.toBe(1);
    expect(onVerdict).toHaveBeenCalledWith('https://media.example/a', failed);
    expect(onVerdict).toHaveBeenCalledWith('https://media.example/b', passed);
  });

  it('has nothing to start when every source failed its probe (native returns nil)', async () => {
    const sources = [source('a'), source('b', 'optimized')];
    const probe = probeWith({ a: { verdict: failed }, b: { verdict: failed } });
    const result = selectInitialSource(sources, { probe });
    await vi.advanceTimersByTimeAsync(10);
    await expect(result).resolves.toBeNull();
  });

  it('starts a route with a startup hint without probing it', async () => {
    const hinted: PlaybackSource = { ...source('slow'), startup_timeout_seconds: 20 };
    const probe = probeWith({});
    await expect(selectInitialSource([hinted, source('other')], { probe })).resolves.toBe(0);
    expect(probe.calls).toEqual([]);
  });

  it('drops every source of a provider that says it is unavailable', async () => {
    const down: ProbeVerdict = { state: 'failed', reason: 'provider_unavailable', status: 503 };
    const sources = [source('a', 'original', 'plex'), source('b', 'optimized', 'plex'), source('c', 'original', 'jellyfin')];
    const onProviderUnavailable = vi.fn();
    const probe = probeWith({ a: { verdict: down }, b: { verdict: passed, delay: 50 }, c: { verdict: passed, delay: 5 } });
    const result = selectInitialSource(sources, { probe, onProviderUnavailable });
    await vi.advanceTimersByTimeAsync(60);
    await expect(result).resolves.toBe(2);
    expect(onProviderUnavailable).toHaveBeenCalledWith('plex');
  });
});
