import { Ban, Bookmark, BookmarkMinus, CheckCircle, EyeOff, Info, XCircle } from 'lucide-react';
import { canHide, hideItem } from '../api/hiddenTitles';
import { itemKind } from '../api/mediaUtils';
import type { WebMediaItem } from '../api/types';
import { useUserState } from '../api/userState';
import { useNavActions } from '../app/navigation';
import type { MenuAction } from './ActionMenu';
import { showToast } from './Toaster';

// The iPad context-menu actions for a title or an episode, wired to the shared
// user state. Failures surface as a toast; state already rolled back.

function run(task: Promise<void>, failure: string) {
  task.catch(() => showToast(failure));
}

export function useTitleActions(
  item: WebMediaItem,
  options: {
    /** Offer "Remove from Continue Watching" (Continue Watching cards). */
    continueWatching?: boolean;
    /** Watchlist add/remove (titles, not episodes). */
    library?: boolean;
    /** Target one episode; mark watched applies to it. */
    season?: number | null;
    episode?: number | null;
    /** Offer "Show Details" (cards whose click plays instead of opening it). */
    details?: boolean;
  } = {},
): MenuAction[] {
  const state = useUserState();
  const { openDetails } = useNavActions();
  const actions: MenuAction[] = [];
  if (options.details) {
    actions.push({ label: 'Show Details', icon: Info, onSelect: () => openDetails(item) });
  }
  const isEpisode = options.season != null && options.episode != null;
  const watchable = itemKind(item) === 'movie' || isEpisode;

  if (watchable) {
    const watched = state.isWatched(item, options.season ?? undefined, options.episode ?? undefined);
    const label = isEpisode ? `S${options.season} E${options.episode}` : '';
    actions.push(watched
      ? {
        label: label ? `Mark ${label} as Unwatched` : 'Mark as Unwatched',
        icon: EyeOff,
        onSelect: () => run(state.markUnwatched(item, options.season, options.episode), 'Could not update watched status.'),
      }
      : {
        label: label ? `Mark ${label} as Watched` : 'Mark as Watched',
        icon: CheckCircle,
        onSelect: () => run(state.markWatched(item, options.season, options.episode), 'Could not update watched status.'),
      });
  }
  if (options.library !== false && !isEpisode) {
    const saved = state.isSaved(item);
    actions.push({
      label: saved ? 'Remove from Watchlist' : 'Add to Watchlist',
      icon: saved ? BookmarkMinus : Bookmark,
      onSelect: () => run(state.toggleSaved(item), saved ? 'Could not remove from Watchlist.' : 'Could not add to Watchlist.'),
    });
  }
  if (options.library !== false && !isEpisode && !options.continueWatching && canHide(item)) {
    actions.push({
      label: 'Not for Me',
      icon: Ban,
      onSelect: () => {
        hideItem(item)
          .then(() => showToast('Hidden. Restore it in Settings → Not for Me.'))
          .catch(() => showToast('Could not hide this title.'));
      },
    });
  }
  if (options.continueWatching) {
    actions.push({
      label: 'Remove from Continue Watching',
      icon: XCircle,
      destructive: true,
      onSelect: () => run(state.removeFromContinueWatching(item), 'Could not remove from Continue Watching.'),
    });
  }
  return actions;
}
