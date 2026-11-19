/**
 * tvOS `CatalogPlaybackCommand`: Play/Pause on a focused card starts playback
 * straight away, at the same target the Details Play button would choose,
 * instead of opening Details. On a keyboard that is the media Play/Pause key
 * or P (Space/Enter keep opening Details, as Select does on the remote).
 */
import { useCallback } from 'react';
import type { KeyboardEvent } from 'react';
import { getDetails } from '../api/accountClient';
import { isPlayable, itemKind, releaseBadge } from '../api/mediaUtils';
import type { WebMediaItem } from '../api/types';
import { useUserState } from '../api/userState';
import { useNavActions } from './navigation';

export function isPlayShortcut(event: Pick<KeyboardEvent, 'key' | 'metaKey' | 'ctrlKey' | 'altKey'>): boolean {
  if (event.metaKey || event.ctrlKey || event.altKey) return false;
  return event.key === 'MediaPlayPause' || event.key === 'MediaPlay' || event.key === 'p' || event.key === 'P';
}

/** Returns a keydown handler that plays the card's title on the Play shortcut; false when the key isn't one. */
export function usePlayFromCard(item: WebMediaItem) {
  const { openPlayer, openDetails } = useNavActions();
  const user = useUserState();
  return useCallback((event: KeyboardEvent): boolean => {
    if (!isPlayShortcut(event)) return false;
    event.preventDefault();
    event.stopPropagation();
    // Nothing to play yet, or a recommendation only (not in Plex/Jellyfin):
    // Details explains when it arrives or offers Watchlist instead.
    if (!isPlayable(item) || releaseBadge(item)?.upcoming) {
      openDetails(item);
      return true;
    }
    if (itemKind(item) !== 'series' || (item.season != null && item.episode != null)) {
      openPlayer(item);
      return true;
    }
    void (async () => {
      try {
        const [details, { primaryAction }] = await Promise.all([getDetails(item), import('../screens/Detail')]);
        const seasons = (details.seasons ?? []).filter((season) => season.episodes?.length);
        const action = primaryAction({ ...item, ...details.item }, seasons, true, user.isWatched, user.continueWatching);
        if (action.target) openPlayer(action.target);
        else openDetails(item);
      } catch {
        openDetails(item);
      }
    })();
    return true;
  }, [item, openDetails, openPlayer, user.continueWatching, user.isWatched]);
}
