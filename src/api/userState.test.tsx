// @vitest-environment jsdom

import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { addLibraryItem, getWatched, markWatched, removeLibraryItem, removeProgress } from './accountClient';
import { UserStateProvider, useUserState } from './userState';
import type { WebMediaItem } from './types';

vi.mock('./accountClient', async (importOriginal) => ({
  ...await importOriginal<typeof import('./accountClient')>(),
  addLibraryItem: vi.fn(),
  removeLibraryItem: vi.fn(),
  markWatched: vi.fn(),
  removeProgress: vi.fn(),
  getWatched: vi.fn(),
}));

const movie: WebMediaItem = { tmdb_id: 11, imdb_id: 'tt0000011', media_type: 'movie', title: 'Saved Movie' };
const other: WebMediaItem = { tmdb_id: 22, media_type: 'movie', title: 'Other' };
const show: WebMediaItem = { tmdb_id: 33, media_type: 'tv', title: 'Show', season: 1, episode: 2 };

function setup(library: WebMediaItem[] = [movie], continueWatching: WebMediaItem[] = [show]) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <UserStateProvider library={library} continueWatching={continueWatching} profileId={1}>{children}</UserStateProvider>
  );
  return renderHook(() => useUserState(), { wrapper });
}

beforeEach(() => {
  vi.mocked(getWatched).mockResolvedValue({ items: [{ tmdb_id: 33, media_type: 'tv', season: 1, episode: 1 }] });
  vi.mocked(addLibraryItem).mockResolvedValue({});
  vi.mocked(removeLibraryItem).mockResolvedValue({});
  vi.mocked(markWatched).mockResolvedValue({});
  vi.mocked(removeProgress).mockResolvedValue({});
});

describe('UserStateProvider', () => {
  it('seeds saved and watched state from the server', async () => {
    const { result } = setup();
    expect(result.current.isSaved(movie)).toBe(true);
    expect(result.current.isSaved({ imdb_id: 'tt0000011', media_type: 'movie' })).toBe(true);
    expect(result.current.isSaved(other)).toBe(false);
    await waitFor(() => expect(result.current.isWatched(show, 1, 1)).toBe(true));
    expect(result.current.isWatched(show, 1, 2)).toBe(false);
  });

  it('removes a saved title optimistically and sends the ids', async () => {
    const { result } = setup();
    await act(async () => { await result.current.toggleSaved(movie); });
    expect(removeLibraryItem).toHaveBeenCalledWith('movie', 11);
    expect(result.current.isSaved(movie)).toBe(false);
    expect(result.current.savedItems).toEqual([]);
  });

  it('adds with both ids and rolls back when the request fails', async () => {
    vi.mocked(addLibraryItem).mockRejectedValueOnce(new Error('nope'));
    const { result } = setup([]);
    const item = { tmdb_id: 44, imdb_id: 'tt0000044', media_type: 'movie' as const, title: 'New' };
    await act(async () => {
      await expect(result.current.toggleSaved(item)).rejects.toThrow('nope');
    });
    expect(addLibraryItem).toHaveBeenCalledWith(expect.objectContaining({ tmdb_id: 44, imdb_id: 'tt0000044', media_type: 'movie' }));
    expect(result.current.isSaved(item)).toBe(false);
  });

  it('marks an episode watched/unwatched and hides removed Continue Watching items', async () => {
    const { result } = setup();
    await act(async () => { await result.current.markWatched(show, 1, 2); });
    expect(markWatched).toHaveBeenCalledWith(expect.objectContaining({ tmdb_id: 33, media_type: 'tv', season: 1, episode: 2 }));
    expect(result.current.isWatched(show, 1, 2)).toBe(true);

    await act(async () => { await result.current.markUnwatched(show, 1, 2); });
    expect(removeProgress).toHaveBeenCalledWith('tv', 33, 1, 2);
    expect(result.current.isWatched(show, 1, 2)).toBe(false);

    await act(async () => { await result.current.removeFromContinueWatching(show); });
    expect(result.current.continueWatching).toEqual([]);
  });
});
