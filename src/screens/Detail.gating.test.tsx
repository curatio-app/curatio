// @vitest-environment jsdom
// Recommendation-only titles (availability.playable !== true) never offer
// playback from Details: Watchlist is the primary action, with a note.

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { DetailsResponse, WebMediaItem } from '../api/types';

const nav = vi.hoisted(() => ({
  openPlayer: vi.fn(), openSourcePicker: vi.fn(), openDetails: vi.fn(), openBrowse: vi.fn(),
  closeDetails: vi.fn(), closeBrowse: vi.fn(), setTab: vi.fn(), setSearchQuery: vi.fn(), focusSearch: vi.fn(),
}));
const user = vi.hoisted(() => ({
  isSaved: vi.fn(() => false), savedItems: [], isWatched: vi.fn(() => false), continueWatching: [],
  toggleSaved: vi.fn(async () => undefined), markWatched: vi.fn(async () => undefined),
  markUnwatched: vi.fn(async () => undefined), removeFromContinueWatching: vi.fn(async () => undefined), profileId: 1,
}));
const getDetails = vi.hoisted(() => vi.fn());
const prefetchPlayback = vi.hoisted(() => vi.fn(() => () => undefined));

vi.mock('../app/navigation', async (importOriginal) => ({
  ...await importOriginal<typeof import('../app/navigation')>(),
  useNavActions: () => nav,
}));
vi.mock('../api/userState', async (importOriginal) => ({
  ...await importOriginal<typeof import('../api/userState')>(),
  useUserState: () => user,
  useOptionalUserState: () => user,
}));
vi.mock('../api/accountClient', async (importOriginal) => ({
  ...await importOriginal<typeof import('../api/accountClient')>(),
  getDetails,
}));
vi.mock('../player/playbackPlan', async (importOriginal) => ({
  ...await importOriginal<typeof import('../player/playbackPlan')>(),
  prefetchPlayback,
}));

import { Detail } from './Detail';

const notPlayable = { playable: false, in_library: false, sources: [] };
const playable = { playable: true, in_library: true, sources: [{ provider_id: 'plex', label: 'Plex', kind: 'library' as const }] };

async function renderDetail(item: WebMediaItem) {
  getDetails.mockResolvedValue({ item } satisfies DetailsResponse);
  render(<Detail item={item} />);
  await act(async () => { await Promise.resolve(); });
}

beforeEach(() => {
  vi.clearAllMocks();
  const storage = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => { storage.set(key, value); },
    removeItem: (key: string) => { storage.delete(key); },
  });
  // jsdom has no layout: scrolling and rail dragging are no-ops here.
  Element.prototype.scrollTo = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('Detail availability gating', () => {
  it('a title not in Plex or Jellyfin offers Watchlist instead of Play, and never warms playback', async () => {
    await renderDetail({ tmdb_id: 5, media_type: 'movie', title: 'Elsewhere', availability: notPlayable });
    expect(screen.queryByRole('button', { name: /^Play/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /^Resume/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /More playback actions/ })).toBeNull();
    expect(screen.getByText('Not in your Plex or Jellyfin')).toBeTruthy();
    const watchlist = screen.getByRole('button', { name: 'Watchlist' });
    fireEvent.click(watchlist);
    expect(user.toggleSaved).toHaveBeenCalledTimes(1);
    expect(prefetchPlayback).not.toHaveBeenCalled();
    expect(nav.openPlayer).not.toHaveBeenCalled();
  });

  it('treats a missing availability (older payload) as not playable', async () => {
    await renderDetail({ tmdb_id: 6, media_type: 'movie', title: 'Old cache' });
    expect(screen.queryByRole('button', { name: /^Play/ })).toBeNull();
    expect(screen.getByText('Not in your Plex or Jellyfin')).toBeTruthy();
  });

  it('a playable title keeps Play and warms playback', async () => {
    await renderDetail({ tmdb_id: 7, media_type: 'movie', title: 'Owned', availability: playable });
    const play = screen.getByRole('button', { name: /^Play/ });
    expect(screen.queryByText('Not in your Plex or Jellyfin')).toBeNull();
    expect(prefetchPlayback).toHaveBeenCalledTimes(1);
    fireEvent.click(play);
    expect(nav.openPlayer).toHaveBeenCalledTimes(1);
  });
});
