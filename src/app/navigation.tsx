import { createContext, useContext, useMemo, type ReactNode } from 'react';
import type { MediaKind, MediaTag, WebMediaItem } from '../api/types';
import type { SourceSelection } from '../api/playbackSources';
import { displayTitle } from '../api/mediaUtils';
import { carryStartFromBeginning } from '../player/startFromBeginning';

export type Tab = 'home' | 'movies' | 'series' | 'library' | 'search' | 'settings';

/** The tabs in the top bar's glass capsule, in tvOS order. Search and Settings
 * are reached from the search field and the avatar menu. */
export const BROWSE_TABS: Array<{ key: Tab; label: string }> = [
  { key: 'home', label: 'Home' },
  { key: 'movies', label: 'Movies' },
  { key: 'series', label: 'Series' },
  { key: 'library', label: 'My Library' },
];

/** What the Browse overlay opens with: a seed tag and the type to browse. */
export interface BrowseFilter {
  tag: MediaTag;
  mediaType: MediaKind;
}

/** Navigation actions. Their identities never change, so cards that only open
 * things don't re-render when an overlay opens or the search query changes. */
export interface NavActions {
  setTab: (tab: Tab) => void;
  setSearchQuery: (query: string) => void;
  /** Move keyboard focus to the top bar's search field. */
  focusSearch: () => void;
  /** Overlays (Detail, Browse, source picker, player) form one history-backed
   * stack (navStack.ts): each open pushes a layer, Back/Escape pops one. */
  openDetails: (item: WebMediaItem) => void;
  /** Play a title. Without a `selection` this follows the Autoplay setting:
   * on → start the best source, off → open the source picker first. From the
   * player itself (next episode) it replaces the current playback layer. */
  openPlayer: (item: WebMediaItem, selection?: SourceSelection) => void;
  openSourcePicker: (item: WebMediaItem) => void;
  openBrowse: (tag: MediaTag, mediaType: MediaKind) => void;
  /** Pop the top overlay. */
  closeDetails: () => void;
  closeBrowse: () => void;
}

/** Navigation state that changes as the user moves around. */
export interface NavState {
  activeTab: Tab;
  /** The query typed into the top bar's search field (drives the Search page). */
  searchQuery: string;
  /** Number of overlays (Detail, Browse, picker, player) currently stacked. */
  overlayDepth: number;
}

export type NavContextValue = NavActions & NavState;

const NavActionsContext = createContext<NavActions | null>(null);
const NavStateContext = createContext<NavState | null>(null);

export function NavProvider({ actions, state, children }: { actions: NavActions; state: NavState; children: ReactNode }) {
  return (
    <NavActionsContext.Provider value={actions}>
      <NavStateContext.Provider value={state}>{children}</NavStateContext.Provider>
    </NavActionsContext.Provider>
  );
}

/** Stable navigation actions only: use this in cards and rows. */
export function useNavActions(): NavActions {
  const value = useContext(NavActionsContext);
  if (!value) throw new Error('useNavActions must be used within a NavProvider');
  return value;
}

export function useNavState(): NavState {
  const value = useContext(NavStateContext);
  if (!value) throw new Error('useNavState must be used within a NavProvider');
  return value;
}

/** Actions and state together (re-renders on every navigation change). */
export function useNav(): NavContextValue {
  const actions = useNavActions();
  const state = useNavState();
  return useMemo(() => ({ ...actions, ...state }), [actions, state]);
}

/** Ensure every item carries a stable `id` for React keys + dedupe. */
export function normalizeMediaItem(item: WebMediaItem): WebMediaItem {
  // The copy keeps a "start from the beginning" mark (next episode, Start over).
  return carryStartFromBeginning(item, {
    ...item,
    id: item.id ?? `${item.media_type ?? 'media'}:${item.tmdb_id ?? displayTitle(item)}`,
  });
}
