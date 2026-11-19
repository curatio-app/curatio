// @vitest-environment jsdom

import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { entryFromHash, entryHash, initialStack, useNavStack, type NavEntry } from './navStack';

const movie = { tmdb_id: 7, media_type: 'movie' as const, title: 'Movie' };
const detail: NavEntry = { kind: 'detail', item: movie };
const browse: NavEntry = { kind: 'browse', tag: { id: 3, name: 'Noir' }, mediaType: 'movie' };

afterEach(() => {
  window.history.replaceState(null, '', '/');
});

describe('nav stack URLs', () => {
  it('maps entries to hashes and restores a title deep link', () => {
    expect(entryHash(detail)).toBe('#/title/movie/7');
    expect(entryHash(browse)).toBe('#/browse/movie/3');
    expect(entryFromHash('#/title/tv/tt0903747')).toEqual({ kind: 'detail', item: { imdb_id: 'tt0903747', media_type: 'tv' } });
    expect(entryFromHash('#/play/movie/7')).toBeNull();
  });

  it('never restores playback layers on reload', () => {
    const saved = { curatioStack: [detail, { kind: 'player', item: movie }, browse] };
    expect(initialStack(saved, '')).toEqual([detail]);
    expect(initialStack(null, '#/title/movie/7')).toEqual([{ kind: 'detail', item: { tmdb_id: 7, media_type: 'movie' } }]);
  });
});

describe('useNavStack', () => {
  it('pushes history entries and pops exactly one layer on Back', async () => {
    const { result } = renderHook(() => useNavStack());
    act(() => result.current.push(detail));
    act(() => result.current.push(browse));
    expect(result.current.stack.map((entry) => entry.kind)).toEqual(['detail', 'browse']);
    expect(window.location.hash).toBe('#/browse/movie/3');

    await act(async () => {
      result.current.back();
      await new Promise((resolve) => window.addEventListener('popstate', resolve, { once: true }));
    });
    expect(result.current.stack.map((entry) => entry.kind)).toEqual(['detail']);
    expect(window.location.hash).toBe('#/title/movie/7');
  });

  it('replaces the top layer in place', () => {
    const { result } = renderHook(() => useNavStack());
    act(() => result.current.push(detail));
    act(() => result.current.push({ kind: 'picker', item: movie }));
    act(() => result.current.replaceTop({ kind: 'player', item: movie }));
    expect(result.current.stack.map((entry) => entry.kind)).toEqual(['detail', 'player']);
  });
});

describe('history safety', () => {
  it('never reopens a player on browser Forward', async () => {
    const { stackFromPop } = await import('./navStack');
    const player: NavEntry = { kind: 'player', item: movie };
    const restored = stackFromPop({ curatioStack: [detail, player] }, [detail]);
    expect(restored.stack).toEqual([detail]);
    expect(restored.trimmed).toBe(true);
  });

  it('keeps a player that is already open', async () => {
    const { stackFromPop } = await import('./navStack');
    const player: NavEntry = { kind: 'player', item: movie };
    const current = [detail, { kind: 'picker', item: movie } as NavEntry, player];
    const restored = stackFromPop({ curatioStack: current.slice(0, 2) }, current);
    expect(restored.stack.map((entry) => entry.kind)).toEqual(['detail', 'picker']);
    expect(restored.stack[0]).toBe(current[0]);
  });

  it('reset() replaces history so Back cannot restore the old profile pages', () => {
    const { result } = renderHook(() => useNavStack());
    act(() => result.current.push(detail));
    const lengthBefore = window.history.length;
    act(() => result.current.reset());
    expect(result.current.stack).toEqual([]);
    expect(window.history.length).toBe(lengthBefore);
    // The entry behind it belongs to an older generation and restores empty.
    window.dispatchEvent(new PopStateEvent('popstate', { state: { curatioStack: [detail], curatioGen: -1 } }));
    expect(result.current.stack).toEqual([]);
  });
});

describe('start from the beginning', () => {
  it('survives the copy navigation makes of an item', async () => {
    const { normalizeMediaItem } = await import('./navigation');
    const { markStartFromBeginning, resumeStartFor } = await import('../player/playerEngine');
    const episode = { tmdb_id: 9, media_type: 'tv' as const, season: 1, episode: 2, position: 600, duration: 1400 };
    expect(resumeStartFor(normalizeMediaItem(episode))).toBe(600);
    markStartFromBeginning(episode);
    expect(resumeStartFor(normalizeMediaItem(episode))).toBe(0);
  });
});
