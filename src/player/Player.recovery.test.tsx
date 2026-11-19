// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { probePlaybackSources, reportStreamFailure, resolvePlaybackSources, startRemuxSession } from '../api/accountClient';
import { resetPlaybackSourcesCache } from '../api/playbackSources';
import { resetBrowserCaps } from '../api/streamCompat';
import { codecs } from '../api/testFixtures';
import type { PlaybackSource, PlaybackSourcesResponse, WebMediaItem } from '../api/types';
import { loadStreamIntoVideo, preparePlaybackMetadata } from './playerEngine';
import { Player } from './Player';

// Keep the real classification, ordering, route identities, recovery selection,
// and React lifecycle. Only transport and actual media playback are simulated.
vi.mock('../api/accountClient', async (importOriginal) => ({
  ...await importOriginal<typeof import('../api/accountClient')>(),
  resolvePlaybackSources: vi.fn(),
  probePlaybackSources: vi.fn(),
  reportStreamFailure: vi.fn(),
  confirmActiveSource: vi.fn(async () => undefined),
  startRemuxSession: vi.fn(async () => { throw new Error('Unexpected server remux in native playback test'); }),
  stopRemuxSession: vi.fn(async () => undefined),
}));

vi.mock('./playerEngine', async (importOriginal) => ({
  ...await importOriginal<typeof import('./playerEngine')>(),
  loadStreamIntoVideo: vi.fn(),
  cleanupPlayback: vi.fn((video: HTMLVideoElement | null) => {
    if (video) video.currentTime = 0;
  }),
  preparePlaybackMetadata: vi.fn(async () => ({
    markers: {}, nextEpisode: null,
    segmentResponse: { fingerprint_status: 'complete', segments: [] },
  })),
  attachNativeTrackListeners: vi.fn(() => () => undefined),
  refreshTrackState: vi.fn(),
  updateMediaSession: vi.fn(),
}));

const resolveStreams = vi.mocked(resolvePlaybackSources);
const loadVideo = vi.mocked(loadStreamIntoVideo);
const probeSources = vi.mocked(probePlaybackSources);
const reportFailure = vi.mocked(reportStreamFailure);
const movie: WebMediaItem = { tmdb_id: 1, media_type: 'movie', title: 'First movie' };

/** `addon`: an opaque resolver route; `library`: a signed /media/ file. */
function route(id: string, kind: 'addon' | 'library' = 'addon'): PlaybackSource {
  return codecs('mp4', 'h264', null, {
    id, label: `Route ${id}`, url: `https://media.example/${id}.mp4`, resolution: '1080p',
    provider_id: kind === 'addon' ? 'addon-x' : 'plex', provider_label: kind === 'addon' ? 'Addon X' : 'Plex', kind,
    route_id: id, file_id: 'same-file',
    // Even a malformed/short backend budget must not cut off the resolver early.
    startup_timeout_seconds: 1,
  });
}

function sources(list: PlaybackSource[]): PlaybackSourcesResponse {
  return { quality: 'original', availability: { playable: true, in_library: true, sources: [] }, sources: list };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

async function flush() {
  await act(async () => { await Promise.resolve(); });
}

async function advance(milliseconds: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(milliseconds); });
}

async function renderPlayer(item = movie) {
  const props = { item, onClose: vi.fn(), onPlay: vi.fn() };
  const view = render(<Player {...props} />);
  await flush();
  const video = view.container.querySelector('video');
  if (!video) throw new Error('Player did not render a video');
  return { ...view, props, video };
}

function loadedRoutes() {
  return loadVideo.mock.calls.map(([stream]) => stream.route_id);
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  // Node 25 can expose an unconfigured global Storage ahead of jsdom's one.
  // Use isolated storage so resume prompts never depend on host state.
  const storage = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => { storage.set(key, value); },
    removeItem: (key: string) => { storage.delete(key); },
  });
  resetBrowserCaps();
  resetPlaybackSourcesCache();
  vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('probably');
  vi.spyOn(console, 'debug').mockImplementation(() => undefined);
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
  resolveStreams.mockReset();
  probeSources.mockResolvedValue({ results: {}, originLooksUnreachable: false });
  reportFailure.mockResolvedValue({ status: 'ok' });
  // A new source resets the native element before producing frames. This catches
  // recovery that accidentally reads the reset element instead of saved position.
  loadVideo.mockImplementation(async (_stream, video) => {
    if (video) video.currentTime = 0;
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('Player source recovery lifecycle', () => {
  it.each(['stalled', 'waiting'])('keeps paused presence when a paused element emits %s', async (event) => {
    const send = vi.fn(async () => new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', send);
    resolveStreams.mockResolvedValue(sources([route('a')]));
    const { video } = await renderPlayer();
    Object.defineProperty(video, 'videoWidth', { configurable: true, value: 1920 });
    fireEvent.loadedMetadata(video);
    fireEvent.playing(video);
    Object.defineProperty(video, 'paused', { configurable: true, value: true });
    fireEvent.pause(video);
    fireEvent(video, new Event(event));
    await advance(15_000);
    const reports = (send.mock.calls as unknown as Array<[string, RequestInit]>).filter(([url]) => url === '/account/api/playback/presence').map(([, options]) => JSON.parse(String(options.body)));
    expect(reports.at(-1).state).toBe('paused');
    expect(reports.some((report) => report.state === 'buffering')).toBe(false);
  });

  it('never preflights addon routes and gives both first and later resolver attempts at least 15 seconds', async () => {
    const direct = route('direct', 'library');
    // Two addons and a library: after a failure every other provider ranks equally, so server order decides.
    resolveStreams.mockResolvedValue(sources([route('a'), { ...route('b'), provider_id: 'addon-y' }, direct]));
    await renderPlayer();

    expect(loadedRoutes()).toEqual(['a']);
    // The best route is an addon route, so it starts at once (native skipsSpeculativePlaybackProbe)
    // and nothing is probed speculatively.
    expect(probeSources).not.toHaveBeenCalled();
    await advance(14_999);
    expect(reportFailure).not.toHaveBeenCalled();
    await advance(1);
    expect(loadedRoutes()).toEqual(['a', 'b']);
    await advance(14_999);
    expect(reportFailure).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(loadedRoutes()).toEqual(['a', 'b', 'direct']);
    expect(resolveStreams).toHaveBeenCalledTimes(1);
  });

  it('probes direct sources before starting and skips one the server saw dead', async () => {
    // No startup hint: a hinted route starts without a speculative probe (native).
    const dead = { ...route('dead', 'library'), startup_timeout_seconds: null };
    const alive = { ...route('alive', 'library'), startup_timeout_seconds: null };
    probeSources.mockImplementation(async ({ urls }) => ({
      results: { [urls[0]]: urls[0] === dead.url ? { state: 'failed', reason: 'http_error', status: 404 } : { state: 'passed', reason: null, status: 206 } },
      originLooksUnreachable: false,
    }));
    resolveStreams.mockResolvedValue(sources([dead, alive]));
    await renderPlayer();
    await flush();
    expect(loadedRoutes()).toEqual(['alive']);
  });

  it('starts on a source chosen in the picker without probing or re-resolving', async () => {
    const a = route('a', 'library');
    const b = route('b', 'library');
    resolveStreams.mockResolvedValue(sources([a, b]));
    const view = render(<Player item={movie} selection={{ sourceKey: 'b', tier: 'default' }} onClose={vi.fn()} onPlay={vi.fn()} />);
    await flush();
    await flush();
    expect(view.container.querySelector('video')).not.toBeNull();
    expect(loadedRoutes()).toEqual(['b']);
    expect(probeSources).not.toHaveBeenCalled();
    expect(resolveStreams).toHaveBeenCalledTimes(1);
  });

  it('does one fresh lookup at exhaustion, ignores repeated old routes, and exposes an explicit retry', async () => {
    const a = route('a');
    resolveStreams.mockResolvedValueOnce(sources([a]))
      .mockResolvedValueOnce(sources([a, { ...a, label: 'Same URL renamed' }]))
      .mockResolvedValueOnce(sources([route('retry')]));
    await renderPlayer();
    expect(probeSources).not.toHaveBeenCalled();
    await advance(15_000);

    expect(resolveStreams).toHaveBeenCalledTimes(2);
    expect(resolveStreams.mock.calls[0][0]).toMatchObject({ refresh: false, quality: null });
    expect(resolveStreams.mock.calls[1][0]).toMatchObject({ refresh: true, quality: null });
    expect(loadedRoutes()).toEqual(['a']);
    expect(screen.getByText(/All available sources failed, including the refreshed results/)).toBeTruthy();
    await advance(60_000);
    expect(resolveStreams).toHaveBeenCalledTimes(2);
    expect(reportFailure).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await flush();
    expect(resolveStreams).toHaveBeenCalledTimes(3);
    expect(loadedRoutes()).toEqual(['a', 'retry']);
  });

  it('tries a renewed route at the failed playback position without giving it another automatic lookup', async () => {
    const refresh = deferred<PlaybackSourcesResponse>();
    resolveStreams.mockResolvedValueOnce(sources([route('expired')]))
      .mockReturnValueOnce(refresh.promise);
    const { video } = await renderPlayer();
    video.currentTime = 123;
    await advance(15_000);
    expect(resolveStreams).toHaveBeenCalledTimes(2);
    await act(async () => refresh.resolve(sources([route('renewed'), route('expired')])));

    expect(loadedRoutes()).toEqual(['expired', 'renewed']);
    expect(loadVideo.mock.calls[1][6]).toBe(123);
    await advance(15_000);
    expect(screen.getByText(/None of the available sources played/)).toBeTruthy();
    await advance(60_000);
    expect(resolveStreams).toHaveBeenCalledTimes(2);
    expect(reportFailure.mock.calls.map(([payload]) => payload.route_id)).toEqual(['expired', 'renewed']);
  });

  it('aborts a pending refresh on item change and ignores its late result', async () => {
    const refresh = deferred<PlaybackSourcesResponse>();
    resolveStreams.mockResolvedValueOnce(sources([route('old')]))
      .mockReturnValueOnce(refresh.promise)
      .mockResolvedValueOnce(sources([route('new-title')]));
    const view = await renderPlayer();
    await advance(15_000);
    const refreshSignal = resolveStreams.mock.calls[1][1]?.signal;
    const other: WebMediaItem = { tmdb_id: 2, media_type: 'movie', title: 'Second movie' };
    view.rerender(<Player {...view.props} item={other} />);
    await flush();

    expect(refreshSignal?.aborted).toBe(true);
    expect(loadedRoutes()).toEqual(['old', 'new-title']);
    await act(async () => refresh.resolve(sources([route('stale')])));
    expect(loadedRoutes()).toEqual(['old', 'new-title']);
    expect(resolveStreams.mock.calls[2][0]).toMatchObject({ tmdb_id: 2 });
    expect(screen.queryByText('Playback Failed')).toBeNull();
  });

  it('retains the original position when a replacement fails before producing any frames', async () => {
    resolveStreams.mockResolvedValueOnce(sources([route('a'), route('b')]))
      .mockResolvedValueOnce(sources([route('c')]));
    const { video } = await renderPlayer();
    video.currentTime = 456;
    await advance(15_000);
    expect(loadedRoutes()).toEqual(['a', 'b']);
    expect(loadVideo.mock.calls[1][6]).toBe(456);
    expect(video.currentTime).toBe(0);
    await advance(15_000);

    expect(loadedRoutes()).toEqual(['a', 'b', 'c']);
    expect(loadVideo.mock.calls[2][6]).toBe(456);
    expect(resolveStreams).toHaveBeenCalledTimes(2);
  });

  it('aborts a pending refresh on unmount and never loads the late route', async () => {
    const refresh = deferred<PlaybackSourcesResponse>();
    resolveStreams.mockResolvedValueOnce(sources([route('a')]))
      .mockReturnValueOnce(refresh.promise);
    const view = await renderPlayer();
    await advance(15_000);
    const refreshSignal = resolveStreams.mock.calls[1][1]?.signal;
    view.unmount();
    expect(refreshSignal?.aborted).toBe(true);
    await act(async () => refresh.resolve(sources([route('stale')])));
    await advance(60_000);
    expect(loadedRoutes()).toEqual(['a']);
    expect(resolveStreams).toHaveBeenCalledTimes(2);
  });

  it('manual selection cancels an exhausted refresh, resumes the route, and keeps the one refresh spent (native)', async () => {
    const refresh = deferred<PlaybackSourcesResponse>();
    resolveStreams.mockResolvedValueOnce(sources([route('a')]))
      .mockReturnValueOnce(refresh.promise)
      .mockResolvedValueOnce(sources([]));
    const { video } = await renderPlayer();
    video.currentTime = 234;
    await advance(15_000);
    const refreshSignal = resolveStreams.mock.calls[1][1]?.signal;
    fireEvent.click(screen.getByRole('button', { name: 'Playback settings' }));
    fireEvent.click(screen.getByRole('button', { name: /Route a/ }));
    await flush();

    expect(refreshSignal?.aborted).toBe(true);
    expect(loadedRoutes()).toEqual(['a', 'a']);
    expect(loadVideo.mock.calls[1][6]).toBe(234);
    await act(async () => refresh.resolve(sources([route('stale')])));
    expect(loadedRoutes()).toEqual(['a', 'a']);
    await advance(15_000);
    expect(reportFailure).toHaveBeenCalledTimes(2);
    // The refresh was already used this playback: no second discovery.
    expect(resolveStreams).toHaveBeenCalledTimes(2);
    expect(screen.getByText(/None of the available sources played/)).toBeTruthy();
  });

  it('manual selection cancels a scheduled automatic source change', async () => {
    resolveStreams.mockResolvedValue(sources([route('a'), route('b')]));
    const { video } = await renderPlayer();
    // The first native error retries through the proxy. The second reports a
    // terminal network failure, which schedules the next source after 800 ms.
    fireEvent.error(video);
    await flush();
    video.currentTime = 345;
    fireEvent.error(video);
    expect(reportFailure).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Playback settings' }));
    fireEvent.click(screen.getByRole('button', { name: /Route a/ }));
    await flush();

    expect(loadVideo.mock.calls[loadVideo.mock.calls.length - 1][6]).toBe(345);
    await advance(800);
    expect(loadedRoutes()).toEqual(['a', 'a', 'a']);
    expect(resolveStreams).toHaveBeenCalledTimes(1);
  });

  it('ignores old media callbacks after a manual source change without disarming the new startup timer', async () => {
    resolveStreams.mockResolvedValue(sources([route('a'), route('b')]));
    await renderPlayer();
    const oldFailure = loadVideo.mock.calls[0][7];
    const oldReady = loadVideo.mock.calls[0][8];
    fireEvent.click(screen.getByRole('button', { name: 'Playback settings' }));
    fireEvent.click(screen.getByRole('button', { name: /Route b/ }));
    await flush();
    act(() => {
      oldFailure('Late failure from the previous transport');
      oldReady();
    });

    expect(startRemuxSession).not.toHaveBeenCalled();
    expect(reportFailure).not.toHaveBeenCalled();
    expect(loadedRoutes()).toEqual(['a', 'b']);
    await advance(15_000);
    expect(reportFailure.mock.calls.map(([payload]) => payload.route_id)).toEqual(['b']);
    expect(loadedRoutes()).toEqual(['a', 'b', 'a']);
  });
});

describe('Player iPad parity', () => {
  const episode: WebMediaItem = { tmdb_id: 9, media_type: 'tv', title: 'Show', season: 1, episode: 1 };
  const next: WebMediaItem = { ...episode, episode: 2 };

  it('counts down on the Next Episode pill from the credits marker and auto-advances', async () => {
    vi.mocked(preparePlaybackMetadata).mockResolvedValue({
      markers: { creditsStart: 100 }, nextEpisode: next,
      segmentResponse: { fingerprint_status: 'complete', segments: [] },
    } as Awaited<ReturnType<typeof preparePlaybackMetadata>>);
    resolveStreams.mockResolvedValue(sources([route('a')]));
    const { props, video } = await renderPlayer(episode);
    await flush();
    video.currentTime = 101;
    fireEvent.timeUpdate(video);
    await flush();
    expect(screen.getByRole('button', { name: /Next Episode/ })).toBeTruthy();
    expect(props.onPlay).not.toHaveBeenCalled();
    await advance(31_000);
    expect(props.onPlay).toHaveBeenCalledWith(next);
  });

  it('holds the 30 s Up Next countdown while paused instead of cancelling it', async () => {
    vi.mocked(preparePlaybackMetadata).mockResolvedValue({
      markers: { creditsStart: 100 }, nextEpisode: next,
      segmentResponse: { fingerprint_status: 'complete', segments: [] },
    } as Awaited<ReturnType<typeof preparePlaybackMetadata>>);
    resolveStreams.mockResolvedValue(sources([route('a')]));
    const { props, video } = await renderPlayer(episode);
    await flush();
    Object.defineProperty(video, 'videoWidth', { configurable: true, value: 1920 });
    Object.defineProperty(video, 'paused', { configurable: true, value: false });
    fireEvent.loadedMetadata(video);
    fireEvent.playing(video);
    video.currentTime = 101;
    fireEvent.timeUpdate(video);
    await flush();
    await advance(10_000);
    Object.defineProperty(video, 'paused', { configurable: true, value: true });
    fireEvent.pause(video);
    await advance(120_000);
    expect(props.onPlay).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: /Next Episode/ })).toBeTruthy();
    Object.defineProperty(video, 'paused', { configurable: true, value: false });
    fireEvent.playing(video);
    await advance(15_000);
    expect(props.onPlay).not.toHaveBeenCalled();
    await advance(6_000);
    expect(props.onPlay).toHaveBeenCalledWith(next);
  });

  it('toggles Fit and Fill from the top bar', async () => {
    resolveStreams.mockResolvedValue(sources([route('a')]));
    const { video } = await renderPlayer();
    expect(video.className).not.toMatch(/videoFill/);
    fireEvent.click(screen.getByRole('button', { name: 'Fill screen' }));
    expect(video.className).toMatch(/videoFill/);
    fireEvent.click(screen.getByRole('button', { name: 'Fit to screen' }));
    expect(video.className).not.toMatch(/videoFill/);
  });
});
