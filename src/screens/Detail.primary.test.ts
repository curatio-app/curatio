// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { playbackProgressKey } from '../api/mediaUtils';
import type { SeasonItem, WebMediaItem } from '../api/types';
import { primaryAction } from './Detail';

const playable = { playable: true, in_library: true, sources: [{ provider_id: 'plex', label: 'Plex', kind: 'library' as const }] };
const movie: WebMediaItem = { tmdb_id: 5, media_type: 'movie', title: 'Movie', runtime: 120, availability: playable };
const show: WebMediaItem = { tmdb_id: 9, media_type: 'tv', title: 'Show', availability: playable };
const seasons: SeasonItem[] = [
  { season: 1, episodes: [{ season: 1, episode: 1, title: 'Pilot' }, { season: 1, episode: 2, title: 'Two' }] },
  { season: 2, episodes: [{ season: 2, episode: 1, title: 'Return' }] },
];
const never = () => false;

// Node can expose an unconfigured global Storage ahead of jsdom's; isolate it.
beforeEach(() => {
  const storage = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => { storage.set(key, value); },
    removeItem: (key: string) => { storage.delete(key); },
    key: (index: number) => [...storage.keys()][index] ?? null,
    get length() { return storage.size; },
  });
});
afterEach(() => vi.unstubAllGlobals());

describe('primaryAction (tvOS PlaybackTargetResolver labels)', () => {
  it('plays or resumes a movie', () => {
    expect(primaryAction(movie, [], true, never).label).toBe('Play');
    localStorage.setItem(playbackProgressKey(movie), JSON.stringify({ position: 3600, duration: 7200, updatedAt: 1 }));
    expect(primaryAction(movie, [], true, never).label).toBe('Resume');
  });

  it('resumes what another device was playing when this one has nothing saved', () => {
    const elsewhere: WebMediaItem[] = [
      { ...movie, position: 1800, duration: 7200 },
      { ...show, season: 1, episode: 2, position: 600, duration: 2400 },
    ];
    expect(primaryAction(movie, [], true, never, elsewhere)).toMatchObject({ label: 'Resume', resume: { position: 1800 } });
    expect(primaryAction(show, seasons, true, never, elsewhere)).toMatchObject({ label: 'Resume S1:E2', target: { season: 1, episode: 2 } });
    // This device's own position wins.
    localStorage.setItem(playbackProgressKey(movie), JSON.stringify({ position: 3600, duration: 7200, updatedAt: 1 }));
    expect(primaryAction(movie, [], true, never, elsewhere).label).toBe('Resume');
  });

  it('offers the next unwatched episode, or says episodes are unavailable', () => {
    const watchedFirstTwo = (_: WebMediaItem, s?: unknown, e?: unknown) => Number(s) === 1 && Number(e) <= 2;
    expect(primaryAction(show, seasons, true, never).label).toBe('Play S1:E1');
    expect(primaryAction(show, seasons, true, watchedFirstTwo).label).toBe('Play S2:E1');
    expect(primaryAction(show, [], true, never)).toMatchObject({ label: 'Episodes Unavailable', target: null });
    expect(primaryAction(show, [], false, never).label).toBe('Loading');
  });

  it('says what Play does, like native PlaybackTarget', () => {
    const all = () => true;
    const firstOnly = (_: WebMediaItem, s?: unknown, e?: unknown) => Number(s) === 1 && Number(e) === 1;
    expect(primaryAction(movie, [], true, never).kind).toBe('play');
    expect(primaryAction(show, seasons, true, never).kind).toBe('start');
    expect(primaryAction(show, seasons, true, firstOnly).kind).toBe('next');
    // Caught up: start over from the first episode.
    expect(primaryAction(show, seasons, true, all)).toMatchObject({ kind: 'restart', label: 'Play S1:E1' });
  });

  it('never targets an episode that has not aired', () => {
    const future = new Date(Date.now() + 10 * 86_400_000).toISOString().slice(0, 10);
    const upcoming: SeasonItem[] = [
      { season: 1, episodes: [{ season: 1, episode: 1, title: 'Pilot', air_date: '2020-01-01' }, { season: 1, episode: 2, title: 'Soon', air_date: future }] },
    ];
    const watchedPilot = (_: WebMediaItem, s?: unknown, e?: unknown) => Number(s) === 1 && Number(e) === 1;
    // Everything out is watched, so Play restarts rather than offering the unaired episode.
    expect(primaryAction(show, upcoming, true, watchedPilot)).toMatchObject({ kind: 'restart', label: 'Play S1:E1' });
  });

  it('makes Watchlist the primary action for a title that is not playable here', () => {
    const recommendation: WebMediaItem = { ...movie, availability: { playable: false, in_library: false, sources: [] } };
    expect(primaryAction(recommendation, [], true, never)).toEqual({ label: 'Watchlist', target: null, resume: null, kind: 'watchlist' });
    // Saved progress never turns a recommendation into Resume.
    localStorage.setItem(playbackProgressKey(recommendation), JSON.stringify({ position: 3600, duration: 7200, updatedAt: 1 }));
    expect(primaryAction(recommendation, [], true, never).kind).toBe('watchlist');
    // No availability (an older cached payload) counts as not playable.
    const { availability: _omit, ...legacy } = show;
    expect(primaryAction(legacy, seasons, true, never)).toMatchObject({ kind: 'watchlist', target: null });
  });
});
