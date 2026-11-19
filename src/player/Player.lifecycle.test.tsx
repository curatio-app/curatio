// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { probePlaybackSources, reportStreamFailure, resolvePlaybackSources, startRemuxSession } from '../api/accountClient';
import { resetMediaFactsCache } from '../api/mediaFacts';
import { resetPlaybackSourcesCache } from '../api/playbackSources';
import { resetBrowserCaps } from '../api/streamCompat';
import { codecs } from '../api/testFixtures';
import type { PlaybackSource, PlaybackSourcesResponse, WebMediaItem } from '../api/types';
import { resetPlaybackPlanState } from './playbackPlan';
import { loadStreamIntoVideo } from './playerEngine';
import { Player } from './Player';

// Same harness as Player.recovery.test.tsx: real planning/recovery/lifecycle,
// simulated transport and media element.
vi.mock('../api/accountClient', async (importOriginal) => ({
  ...await importOriginal<typeof import('../api/accountClient')>(),
  resolvePlaybackSources: vi.fn(),
  probePlaybackSources: vi.fn(),
  inspectPlaybackSources: vi.fn(async () => ({ results: {} })),
  reportStreamFailure: vi.fn(),
  confirmActiveSource: vi.fn(async () => undefined),
  reportPlaybackProgress: vi.fn(async () => undefined),
  startRemuxSession: vi.fn(),
  stopRemuxSession: vi.fn(async () => undefined),
  keepaliveRemuxSession: vi.fn(async () => true),
}));

vi.mock('./playerEngine', async (importOriginal) => ({
  ...await importOriginal<typeof import('./playerEngine')>(),
  loadStreamIntoVideo: vi.fn(),
  cleanupPlayback: vi.fn(),
  preparePlaybackMetadata: vi.fn(async () => ({ markers: {}, nextEpisode: null, segmentResponse: { fingerprint_status: 'complete', segments: [] } })),
  attachNativeTrackListeners: vi.fn(() => () => undefined),
  refreshTrackState: vi.fn(),
  updateMediaSession: vi.fn(),
}));

const resolveStreams = vi.mocked(resolvePlaybackSources);
const loadVideo = vi.mocked(loadStreamIntoVideo);
const reportFailure = vi.mocked(reportStreamFailure);
const startRemux = vi.mocked(startRemuxSession);
const movie: WebMediaItem = { tmdb_id: 11, media_type: 'movie', title: 'Lifecycle', duration: 6000 };

/** An addon (opaque resolver) route. */
function route(id: string): PlaybackSource {
  return codecs('mp4', 'h264', null, {
    id, label: `Route ${id}`, url: `https://media.example/${id}.mp4`, resolution: '1080p',
    kind: 'addon', provider_id: 'addon-x', provider_label: 'Addon X', route_id: id, startup_timeout_seconds: 1,
  });
}

function sources(list: PlaybackSource[]): PlaybackSourcesResponse {
  return { quality: 'original', availability: { playable: true, in_library: true, sources: [] }, sources: list };
}

async function flush() {
  await act(async () => { await Promise.resolve(); });
}
async function advance(ms: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

function setProp(video: HTMLVideoElement, key: string, value: unknown) {
  Object.defineProperty(video, key, { configurable: true, writable: true, value });
}

async function startPlaying() {
  const view = render(<Player item={movie} onClose={vi.fn()} onPlay={vi.fn()} />);
  await flush();
  await flush();
  const video = view.container.querySelector('video')!;
  setProp(video, 'videoWidth', 1920);
  setProp(video, 'paused', false);
  setProp(video, 'duration', 6000);
  fireEvent.loadedMetadata(video);
  fireEvent.playing(video);
  return { ...view, video };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  const storage = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => { storage.set(key, value); },
    removeItem: (key: string) => { storage.delete(key); },
  });
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 200 })));
  resetBrowserCaps();
  resetPlaybackSourcesCache();
  resetMediaFactsCache();
  resetPlaybackPlanState();
  vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('probably');
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined);
  vi.spyOn(console, 'debug').mockImplementation(() => undefined);
  vi.mocked(probePlaybackSources).mockResolvedValue({ results: {}, originLooksUnreachable: false });
  reportFailure.mockResolvedValue({ status: 'ok' });
  loadVideo.mockImplementation(async () => undefined);
});

afterEach(() => {
  cleanup();
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('Player lifecycle', () => {
  // tvOS/iOS parity: a saved position asks before anything plays.
  it('asks to resume or start from the beginning, and waits for the answer', async () => {
    localStorage.setItem('curatio-playback:movie:11:0:0', JSON.stringify({ position: 900, duration: 6000, updatedAt: Date.now() }));
    resolveStreams.mockResolvedValue(sources([route('a')]));
    render(<Player item={movie} onClose={vi.fn()} onPlay={vi.fn()} />);
    await flush();
    await flush();
    expect(screen.getByText(/resume or watch from the beginning/)).toBeTruthy();
    expect(loadVideo).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Resume Playing (15:00)' }));
    await flush();
    await flush();
    expect(screen.queryByText(/resume or watch from the beginning/)).toBeNull();
    expect(loadVideo.mock.calls[0][6]).toBe(900);
  });

  it('Start from Beginning plays from the start and forgets the saved position', async () => {
    localStorage.setItem('curatio-playback:movie:11:0:0', JSON.stringify({ position: 900, duration: 6000, updatedAt: Date.now() }));
    resolveStreams.mockResolvedValue(sources([route('a')]));
    render(<Player item={movie} onClose={vi.fn()} onPlay={vi.fn()} />);
    await flush();
    await flush();
    fireEvent.click(screen.getByRole('button', { name: 'Start from Beginning' }));
    await flush();
    await flush();
    expect(loadVideo.mock.calls[0][6]).toBe(0);
    expect(localStorage.getItem('curatio-playback:movie:11:0:0')).toBeNull();
  });

  it('pause followed by a very long wait before resuming never fails over', async () => {
    resolveStreams.mockResolvedValue(sources([route('a'), route('b')]));
    const { video } = await startPlaying();
    video.currentTime = 300;
    fireEvent.timeUpdate(video);
    setProp(video, 'paused', true);
    fireEvent.pause(video);
    await advance(20 * 60_000);
    setProp(video, 'paused', false);
    fireEvent.playing(video);
    fireEvent.waiting(video); // the resume wait is not a rebuffer
    await advance(5_000);
    expect(reportFailure).not.toHaveBeenCalled();
    expect(loadVideo).toHaveBeenCalledTimes(1);
  });

  it('after a long pause, reloads at the position when Play does not move the playhead', async () => {
    resolveStreams.mockResolvedValue(sources([route('a')]));
    const { video } = await startPlaying();
    video.currentTime = 300;
    fireEvent.timeUpdate(video);
    setProp(video, 'paused', true);
    fireEvent.pause(video);
    await advance(2 * 60_000);
    setProp(video, 'paused', false);
    fireEvent.play(video);
    await advance(5_000);
    expect(reportFailure).not.toHaveBeenCalled();
    expect(loadVideo).toHaveBeenCalledTimes(2);
    expect(loadVideo.mock.calls[1][6]).toBe(300);
  });

  it('a short pause, or a long one that resumes moving, never reloads', async () => {
    resolveStreams.mockResolvedValue(sources([route('a')]));
    const { video } = await startPlaying();
    video.currentTime = 300;
    fireEvent.timeUpdate(video);
    setProp(video, 'paused', true);
    fireEvent.pause(video);
    await advance(60_000);
    setProp(video, 'paused', false);
    fireEvent.play(video);
    await advance(5_000);
    expect(loadVideo).toHaveBeenCalledTimes(1);

    setProp(video, 'paused', true);
    fireEvent.pause(video);
    await advance(5 * 60_000);
    setProp(video, 'paused', false);
    fireEvent.play(video);
    video.currentTime = 302;
    fireEvent.timeUpdate(video);
    await advance(5_000);
    expect(loadVideo).toHaveBeenCalledTimes(1);
  });

  it('holds a network error that happens while paused until Play', async () => {
    let onFailure: ((message: string, signal?: unknown) => void) | null = null;
    loadVideo.mockImplementation(async (...args: unknown[]) => {
      onFailure = args[7] as typeof onFailure;
    });
    resolveStreams.mockResolvedValue(sources([route('a'), route('b')]));
    const { video } = await startPlaying();
    video.currentTime = 300;
    fireEvent.timeUpdate(video);
    setProp(video, 'paused', true);
    fireEvent.pause(video);
    await act(async () => { onFailure?.('Network error', { kind: 'hls', fatalType: 'network' }); });
    await advance(10 * 60_000);
    expect(loadVideo).toHaveBeenCalledTimes(1);
    expect(reportFailure).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: /Try Again|Retry/ })).toBeNull();
    setProp(video, 'paused', false);
    fireEvent.play(video);
    await advance(5_000);
    expect(loadVideo.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(loadVideo.mock.calls[1][6]).toBe(300);
  });

  it('a premature EOF reloads at the position instead of completing', async () => {
    resolveStreams.mockResolvedValue(sources([route('a')]));
    const { video } = await startPlaying();
    video.currentTime = 1200;
    fireEvent.timeUpdate(video);
    fireEvent.ended(video);
    await advance(1000);
    expect(loadVideo).toHaveBeenCalledTimes(2);
    expect(loadVideo.mock.calls[1][6]).toBe(1200);
    expect(reportFailure).not.toHaveBeenCalled();
    // Never marked watched: the saved position is the real one, not the end.
    await advance(10_000);
    expect(JSON.parse(localStorage.getItem('curatio-playback:movie:11:0:0') ?? 'null')).toMatchObject({ position: 1200 });
  });

  it('a decode error on a direct H.264 source switches to server-prepared HLS, not another source', async () => {
    resolveStreams.mockResolvedValue(sources([route('a'), route('b')]));
    startRemux.mockResolvedValue({ playlist_url: '/account/api/playback/remux/s1/index.m3u8', session: 's1', start: 296, duration: 6000, mode: 'remux' });
    const { video } = await startPlaying();
    video.currentTime = 300;
    fireEvent.timeUpdate(video);
    setProp(video, 'error', { code: 3, message: 'decode' });
    fireEvent.error(video);
    await flush();
    await flush();
    expect(startRemux).toHaveBeenCalledWith('https://media.example/a.mp4', expect.objectContaining({ start: 300 }));
    const last = loadVideo.mock.calls.at(-1)!;
    expect(last[0].url).toBe('/account/api/playback/remux/s1/index.m3u8');
    expect(last[4]).toBe('hls');
    expect(last[6]).toBe(4); // 300 movie time − 296 session start
    expect(reportFailure).not.toHaveBeenCalled();
  });

  it('a mid-playback stall with no bytes reloads the same source first', async () => {
    resolveStreams.mockResolvedValue(sources([route('a'), route('b')]));
    const { video } = await startPlaying();
    video.currentTime = 50;
    fireEvent.timeUpdate(video);
    // Native stall timeout: 30 s without progress.
    await advance(16_000);
    expect(loadedRouteIds()).toEqual(['a']);
    await advance(15_000);
    await advance(1_000);
    expect(loadedRouteIds()).toEqual(['a', 'a']);
    expect(reportFailure).not.toHaveBeenCalled();
  });

  it('checks the backups once playing (native preflighting), never the addon one playing', async () => {
    const direct = (id: string): PlaybackSource => ({
      ...route(id), kind: 'library', provider_id: 'plex', provider_label: 'Plex', startup_timeout_seconds: null,
    });
    resolveStreams.mockResolvedValue(sources([route('a'), direct('b'), direct('c')]));
    await startPlaying();
    await flush();
    const probed = vi.mocked(probePlaybackSources).mock.calls.map(([payload]) => payload.urls[0]);
    expect(probed).toEqual(expect.arrayContaining(['https://media.example/b.mp4', 'https://media.example/c.mp4']));
    expect(probed).not.toContain('https://media.example/a.mp4');
    expect(loadedRouteIds()).toEqual(['a']);
  });
});

function loadedRouteIds() {
  return loadVideo.mock.calls.map(([stream]) => stream.route_id);
}
