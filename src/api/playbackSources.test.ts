import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { resolvePlaybackSources } from './accountClient';
import { findRenewedRoute, loadPlaybackSources, peekPlaybackSources, resetPlaybackSourcesCache, sourcesRequest } from './playbackSources';
import { resetBrowserCaps } from './streamCompat';
import { makeSource } from './testFixtures';
import type { PlaybackSourcesResponse, WebMediaItem } from './types';

vi.mock('./accountClient', async (importOriginal) => ({
  ...await importOriginal<typeof import('./accountClient')>(),
  resolvePlaybackSources: vi.fn(),
}));

const resolveSources = vi.mocked(resolvePlaybackSources);
const movie: WebMediaItem = { tmdb_id: 7, media_type: 'movie', title: 'Movie', imdb_id: 'tt0000007' };
const response = (ids: string[], quality: 'original' | 'optimized' = 'original'): PlaybackSourcesResponse => ({
  quality,
  availability: { playable: true, in_library: true, sources: [{ provider_id: 'plex', label: 'Plex', kind: 'library' }] },
  // No codec fields: classified 'unknown' (attempt directly) whatever this test environment can decode.
  sources: ids.map((id) => makeSource({ id, label: id, url: `https://x/media/${id}`, route_id: id })),
});

beforeEach(() => {
  vi.useFakeTimers();
  resolveSources.mockReset();
  resetBrowserCaps();
  resetPlaybackSourcesCache();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('sourcesRequest', () => {
  it('sends the v2 body: type, ids, episode, quality (null = profile default)', () => {
    expect(sourcesRequest(movie, { tier: 'default', sessionId: 's1', profileId: 3 })).toEqual({
      type: 'movie', imdb_id: 'tt0000007', tmdb_id: 7, season: null, episode: null,
      quality: null, refresh: false, viewing_session_id: 's1', title: 'Movie', profile_id: 3,
    });
    const episode: WebMediaItem = { tmdb_id: '9', media_type: 'tv', title: 'Show', season: '2', episode: 5 };
    expect(sourcesRequest(episode, { tier: 'optimized', refresh: true })).toMatchObject({
      type: 'series', imdb_id: null, tmdb_id: 9, season: 2, episode: 5, quality: 'optimized', refresh: true,
    });
  });

  it('reads the IMDb id and episode from a structured video id', () => {
    const episode: WebMediaItem = { media_type: 'series', title: 'Show', video_id: 'tt1234567:3:4' };
    expect(sourcesRequest(episode, { tier: 'original' })).toMatchObject({ type: 'series', imdb_id: 'tt1234567', season: 3, episode: 4, quality: 'original' });
  });
});

describe('loadPlaybackSources', () => {
  it('serves a repeat lookup from cache within the TTL, keyed by requested quality', async () => {
    resolveSources.mockResolvedValue(response(['a', 'b']));
    const first = await loadPlaybackSources(movie, { tier: 'default' });
    const second = await loadPlaybackSources(movie, { tier: 'default' });
    expect(second).toBe(first);
    expect(first.quality).toBe('original');
    expect(first.availability?.playable).toBe(true);
    expect(resolveSources).toHaveBeenCalledTimes(1);
    expect(resolveSources.mock.calls[0][0]).toMatchObject({ quality: null });
    expect(peekPlaybackSources(movie, 'default')?.streams.map((s) => s.label)).toEqual(['a', 'b']);
    expect(peekPlaybackSources(movie, 'optimized')).toBeNull();
  });

  it('expires entries after the TTL', async () => {
    resolveSources.mockResolvedValue(response(['a']));
    await loadPlaybackSources(movie, { tier: 'default' });
    vi.advanceTimersByTime(30 * 60 * 1000);
    expect(peekPlaybackSources(movie, 'default')).toBeNull();
    await loadPlaybackSources(movie, { tier: 'default' });
    expect(resolveSources).toHaveBeenCalledTimes(2);
  });

  it('shares one in-flight request between concurrent callers', async () => {
    let finish!: (value: PlaybackSourcesResponse) => void;
    resolveSources.mockReturnValue(new Promise((done) => { finish = done; }));
    const picker = loadPlaybackSources(movie, { tier: 'default' });
    const player = loadPlaybackSources(movie, { tier: 'default' });
    finish(response(['a']));
    expect((await picker).streams).toEqual((await player).streams);
    expect(resolveSources).toHaveBeenCalledTimes(1);
  });

  it('lets one caller abort without cancelling the shared request', async () => {
    let finish!: (value: PlaybackSourcesResponse) => void;
    resolveSources.mockReturnValue(new Promise((done) => { finish = done; }));
    const controller = new AbortController();
    const leaving = loadPlaybackSources(movie, { tier: 'default', signal: controller.signal });
    const staying = loadPlaybackSources(movie, { tier: 'default' });
    controller.abort();
    await expect(leaving).rejects.toThrow();
    const sharedSignal = resolveSources.mock.calls[0][1]?.signal;
    expect(sharedSignal?.aborted).toBe(false);
    finish(response(['a']));
    expect((await staying).streams).toHaveLength(1);
  });

  it('bypasses the cache on refresh and caches clean empty answers but not errors', async () => {
    resolveSources.mockResolvedValueOnce(response([]));
    expect((await loadPlaybackSources(movie, { tier: 'default' })).streams).toEqual([]);
    expect(peekPlaybackSources(movie, 'default')).not.toBeNull();

    resolveSources.mockRejectedValueOnce(new Error('providers down'));
    await expect(loadPlaybackSources(movie, { tier: 'default', refresh: true })).rejects.toThrow('providers down');
    expect(resolveSources.mock.calls[1][0].refresh).toBe(true);
    expect(peekPlaybackSources(movie, 'default')?.streams).toEqual([]);

    resolveSources.mockResolvedValueOnce(response(['fresh']));
    const refreshed = await loadPlaybackSources(movie, { tier: 'default', refresh: true });
    expect(refreshed.streams.map((s) => s.label)).toEqual(['fresh']);
    expect(peekPlaybackSources(movie, 'default')?.streams.map((s) => s.label)).toEqual(['fresh']);
  });
});

describe('findRenewedRoute', () => {
  it('matches by route id, else the same file on the same provider', () => {
    const failed = makeSource({ route_id: 'r1', file_id: 'f1', url: 'https://x/media/old' });
    const renewed = makeSource({ route_id: 'r1', url: 'https://x/media/new' });
    expect(findRenewedRoute(failed, [makeSource({ route_id: 'r2' }), renewed])).toBe(renewed);
    const sameFile = makeSource({ route_id: '', file_id: 'f1', url: 'https://x/media/again' });
    expect(findRenewedRoute({ ...failed, route_id: '' }, [sameFile])).toBe(sameFile);
    expect(findRenewedRoute({ ...failed, route_id: '' }, [{ ...sameFile, provider_id: 'jellyfin' }])).toBeNull();
  });
});
