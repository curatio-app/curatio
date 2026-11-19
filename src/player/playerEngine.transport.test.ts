// @vitest-environment jsdom

import type Hls from 'hls.js';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { PlaybackSource } from '../api/types';
import { codecs, makeSource } from '../api/testFixtures';
import { cleanupPlayback, directionalSeekStep, loadStreamIntoVideo, MEDIA_SESSION_SEEK_SECONDS, proxiedUrl, SEEK_STEP_SECONDS, updateMediaSession } from './playerEngine';

const fetchBytes = vi.fn<typeof fetch>();

function stream(url = 'https://cdn.example/movie.mp4'): PlaybackSource {
  return codecs('mp4', 'h264', 'aac', { url, route_id: 'route', startup_timeout_seconds: 15 });
}

async function load(source: PlaybackSource, options: { preferProxy?: boolean; start?: number } = {}) {
  const video = document.createElement('video');
  const hlsRef = { current: null as Hls | null };
  const onFailure = vi.fn();
  const onReady = vi.fn();
  await loadStreamIntoVideo(source, video, hlsRef, false, 'direct', Boolean(options.preferProxy), options.start ?? 0, onFailure, onReady, vi.fn());
  return { video, hlsRef, onFailure, onReady };
}

beforeEach(() => {
  vi.stubGlobal('fetch', fetchBytes);
  vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('');
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined);
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('direct transport', () => {
  it('plays the source URL directly with no client-side byte probing or conversion', async () => {
    const source = stream();
    const { video, onFailure } = await load(source);
    expect(video.getAttribute('src')).toBe(source.url);
    expect(fetchBytes).not.toHaveBeenCalled();
    expect(onFailure).not.toHaveBeenCalled();
  });

  it('routes a proxy retry through the same-origin byte proxy', async () => {
    const source = stream();
    const { video } = await load(source, { preferProxy: true });
    expect(video.getAttribute('src')).toBe(proxiedUrl(source.url!));
  });

  it('reports a source with no URL as a no-url failure', async () => {
    const { onFailure } = await load(makeSource({ url: '' }));
    expect(onFailure).toHaveBeenCalledWith(expect.any(String), { kind: 'no-url' });
  });

  it('ignores ready events from a superseded load', async () => {
    const source = stream();
    const { video, hlsRef, onReady } = await load(source);
    cleanupPlayback(video, hlsRef);
    video.dispatchEvent(new Event('loadedmetadata'));
    expect(onReady).not.toHaveBeenCalled();
  });

  it('fires ready once for the current load', async () => {
    const { video, onReady } = await load(stream());
    video.dispatchEvent(new Event('loadedmetadata'));
    video.dispatchEvent(new Event('canplay'));
    expect(onReady).toHaveBeenCalledTimes(1);
  });
});

describe('Media Session', () => {
  it('skips 15 s from the lock screen and headset while the keyboard keeps 10 s', () => {
    const handlers = new Map<string, (details: MediaSessionActionDetails) => void>();
    vi.stubGlobal('MediaMetadata', class { constructor(public init: unknown) {} });
    Object.defineProperty(navigator, 'mediaSession', {
      configurable: true,
      value: { setActionHandler: (action: string, handler: (details: MediaSessionActionDetails) => void) => handlers.set(action, handler) },
    });
    const seek = vi.fn();
    const video = document.createElement('video');
    Object.defineProperty(video, 'currentTime', { configurable: true, writable: true, value: 100 });
    updateMediaSession({ tmdb_id: 1, media_type: 'movie', title: 'Movie' }, video, true, 0, seek);
    handlers.get('seekforward')?.({ action: 'seekforward', seekOffset: 10 });
    handlers.get('seekbackward')?.({ action: 'seekbackward' });
    expect(seek.mock.calls).toEqual([[115], [85]]);
    expect(MEDIA_SESSION_SEEK_SECONDS).toBe(15);
    expect(SEEK_STEP_SECONDS).toBe(10);
    delete (navigator as { mediaSession?: unknown }).mediaSession;
  });
});

describe('directionalSeekStep (tvOS remote acceleration)', () => {
  it('starts at 10 s, accelerates with quick repeats, and is capped', () => {
    expect(directionalSeekStep(0, 5400)).toBeCloseTo(32.4, 1); // 0.6% of 90 min
    expect(directionalSeekStep(0, 600)).toBe(10);
    expect(directionalSeekStep(2, 600)).toBeGreaterThan(15);
    expect(directionalSeekStep(8, 600)).toBe(45);
    expect(directionalSeekStep(8, 10_800)).toBe(270); // 2.5% of 3 h
    expect(directionalSeekStep(0, Number.NaN)).toBe(10);
  });
});
