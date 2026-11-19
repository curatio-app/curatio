import { afterEach, describe, expect, it } from 'vitest';
import {
  NOT_IN_LIBRARY_NOTE,
  addonSourceLabel,
  dimsForOwnership,
  isOnServer,
  isPlayable,
  notOnServerNote,
  setServerLibraryPresent,
} from './mediaUtils';
import type { WebMediaItem } from './types';

function item(owned: boolean | null, playable = false, addon?: string): WebMediaItem {
  if (owned === null) return { tmdb_id: 1, media_type: 'movie', title: 'Unknown' };
  const sources = [
    ...(owned ? [{ provider_id: 'plex', label: 'Plex', kind: 'library' as const }] : []),
    ...(addon ? [{ provider_id: 'aio', label: addon, kind: 'addon' as const }] : []),
  ];
  return {
    tmdb_id: 2,
    media_type: 'movie',
    title: 'Title',
    availability: { playable: owned || playable, in_library: owned, sources },
  };
}

afterEach(() => setServerLibraryPresent(false));

describe('ownership is not playability', () => {
  it('an addon makes a title playable but not owned', () => {
    const addonOnly = item(false, true, 'Example Sources');
    expect(isPlayable(addonOnly)).toBe(true);
    expect(isOnServer(addonOnly)).toBe(false);
    expect(addonSourceLabel(addonOnly)).toBe('Example Sources');
    expect(addonSourceLabel(item(true))).toBeNull();
  });

  it('dims only when a library exists and ownership is known', () => {
    const addonOnly = item(false, true, 'Example Sources');
    expect(dimsForOwnership(addonOnly)).toBe(false); // addon-only setup: never dim everything
    setServerLibraryPresent(true);
    expect(dimsForOwnership(addonOnly)).toBe(true);
    expect(dimsForOwnership(item(true))).toBe(false);
    expect(dimsForOwnership(item(null))).toBe(false);
  });

  it('words the Details note by ownership', () => {
    expect(notOnServerNote(item(false))).toBe(NOT_IN_LIBRARY_NOTE);
    expect(notOnServerNote(item(false, true, 'Example Sources'))).toBeNull();
    setServerLibraryPresent(true);
    expect(notOnServerNote(item(false, true, 'Example Sources'))).toBe(`${NOT_IN_LIBRARY_NOTE} · plays from Example Sources`);
    expect(notOnServerNote(item(true))).toBeNull();
  });
});
