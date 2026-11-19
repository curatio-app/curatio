import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import type { MediaKind, MediaTag, Profile, WebMediaItem } from '../api/types';
import type { SourceSelection } from '../api/playbackSources';
import { getPreferences } from '../api/preferences';
import { clearReadCache } from '../api/accountClient';
import { TopNav, type TopNavHandle } from '../components/TopNav';
import { ProfileGate } from '../components/ProfileGate';
import { MAX_PROFILES, startWithProfile } from '../api/startWith';
import { ProfileSwitcher } from '../components/ProfileSwitcher';
import { ProfileEditor } from '../components/ProfileEditor';
import { LoadingState } from '../components/LoadingState';
import { DashboardSkeleton } from '../components/DashboardSkeleton';
import { EmptyState } from '../components/EmptyState';
import { Home } from '../screens/Home';
import { Search } from '../screens/Search';
import { Library } from '../screens/Library';
import { Settings } from '../screens/Settings';
import { isPlayable, playbackProgressKey } from '../api/mediaUtils';
import { NavProvider, normalizeMediaItem } from './navigation';
import type { NavActions, NavState, Tab } from './navigation';
import { ErrorBoundary } from './ErrorBoundary';
import { lazyWithRetry } from './lazyWithRetry';
import { entryHash, useNavStack, type NavEntry } from './navStack';
import { clearNavMemory, useSpatialNav } from './spatialNav';
import { resetActiveCard } from './activeCard';
import { UserStateProvider } from '../api/userState';
import { Toaster } from '../components/Toaster';
import { useDashboard } from './useDashboard';
import styles from './AppShell.module.css';

const PROFILE_CHOSEN_KEY = 'curatio.profileChosen';
const NO_ITEMS: WebMediaItem[] = [];
// One player everywhere. In the desktop app it is the same web player; sources
// it can't open are prepared by the app's local engine (see api/desktopBridge).
const Player = lazyWithRetry(() => import('../player/Player').then((module) => ({ default: module.Player })));
const Detail = lazyWithRetry(() => import('../screens/Detail').then((module) => ({ default: module.Detail })));
const SourcePicker = lazyWithRetry(() => import('../screens/SourcePicker').then((module) => ({ default: module.SourcePicker })));
const Browse = lazyWithRetry(() => import('../screens/Browse').then((module) => ({ default: module.Browse })));

/** Fetch the overlay chunks once the browser is idle so the first Detail open
 * doesn't flash a loading state. */
function prefetchOverlays() {
  const run = () => {
    void Detail.preload();
    void SourcePicker.preload();
  };
  const idle = (window as Window & { requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number }).requestIdleCallback;
  if (idle) idle(run, { timeout: 4000 });
  else window.setTimeout(run, 2500);
}

/** Overlay layers keep their mount while their entry is unchanged; a different
 * title at the same depth (Forward, next item) gets a fresh page. The player
 * keeps one key so "next episode" swaps in place. */
function layerKey(entry: NavEntry, index: number) {
  return entry.kind === 'player' ? `${index}:player` : `${index}:${entryHash(entry)}`;
}

/** React 18 has no typed `inert`; an empty string attribute works everywhere. */
function inertWhen(on: boolean): Record<string, string> {
  return on ? { inert: '' } : {};
}

function readChosen(): boolean {
  try {
    return sessionStorage.getItem(PROFILE_CHOSEN_KEY) === '1';
  } catch {
    return false;
  }
}

function markChosen() {
  try {
    sessionStorage.setItem(PROFILE_CHOSEN_KEY, '1');
  } catch {
    /* sessionStorage may be unavailable (private mode) — gate just re-shows. */
  }
}

export function AppShell() {
  const { me, dashboard, rows, isLoading, error, reload, refreshMe, switchProfile } = useDashboard();
  const [activeTab, setActiveTab] = useState<Tab>('home');
  const [searchQuery, setSearchQuery] = useState('');
  const { stack, push, replaceTop, back, reset } = useNavStack();
  // Read the live stack inside callbacks so they keep one identity.
  const stackRef = useRef(stack);
  stackRef.current = stack;
  const profileId = me?.active_profile_id ?? undefined;
  const scrollRef = useRef<HTMLElement | null>(null);
  const topNavRef = useRef<TopNavHandle | null>(null);
  useSpatialNav();

  // Profile UI state.
  const [switcherOpen, setSwitcherOpen] = useState(false);
  const [gateDismissed, setGateDismissed] = useState<boolean>(() => readChosen());
  const [editorTarget, setEditorTarget] = useState<Profile | null | undefined>(undefined);
  const [profileBusy, setProfileBusy] = useState(false);

  const profiles = me?.profiles ?? [];

  useEffect(() => {
    const refreshLibrary = () => {
      clearReadCache();
      void reload({ silent: true });
    };
    window.addEventListener('curatio:library-changed', refreshLibrary);
    return () => window.removeEventListener('curatio:library-changed', refreshLibrary);
  }, [reload]);

  const setTab = useCallback((tab: Tab) => {
    setActiveTab((current) => {
      // Re-selecting the current tab scrolls it back to the top (tvOS).
      if (current === tab || tab !== 'search') {
        window.requestAnimationFrame(() => {
          scrollRef.current?.scrollTo({ top: 0, behavior: 'smooth' });
        });
      }
      return tab;
    });
    resetActiveCard();
  }, []);
  const focusSearch = useCallback(() => topNavRef.current?.focusSearch(), []);

  // A title picked on a Details page (More Like This) swaps that page in place,
  // as on tvOS, rather than stacking page on page: Back still returns to where
  // Details was opened from.
  const openDetails = useCallback((item: WebMediaItem) => {
    const current = stackRef.current;
    const entry: NavEntry = { kind: 'detail', item: normalizeMediaItem(item) };
    if (current[current.length - 1]?.kind === 'detail') replaceTop(entry);
    else push(entry);
  }, [push, replaceTop]);
  // Playback layers replace each other (player → next episode, player → picker)
  // so Back always returns to the page playback was started from.
  const showPlaybackLayer = useCallback((entry: NavEntry) => {
    const current = stackRef.current;
    const top = current[current.length - 1];
    if (top?.kind === 'player' || (top?.kind === 'picker' && entry.kind === 'picker')) replaceTop(entry);
    else push(entry);
  }, [push, replaceTop]);
  const openSourcePicker = useCallback((item: WebMediaItem) => {
    // Recommendation-only titles never reach playback: Details offers Watchlist.
    if (!isPlayable(item)) {
      openDetails(item);
      return;
    }
    // "Choose Source" from a player opened by the picker: go back to that picker.
    const current = stackRef.current;
    const below = current[current.length - 2];
    if (current[current.length - 1]?.kind === 'player' && below?.kind === 'picker' && playbackProgressKey(below.item) === playbackProgressKey(normalizeMediaItem(item))) {
      back();
      return;
    }
    showPlaybackLayer({ kind: 'picker', item: normalizeMediaItem(item) });
  }, [back, openDetails, showPlaybackLayer]);
  const openPlayer = useCallback((item: WebMediaItem, selection?: SourceSelection) => {
    if (!isPlayable(item)) {
      openDetails(item);
      return;
    }
    if (!selection && !getPreferences().autoplay) {
      openSourcePicker(item);
      return;
    }
    showPlaybackLayer({ kind: 'player', item: normalizeMediaItem(item), selection });
  }, [openDetails, openSourcePicker, showPlaybackLayer]);
  const openBrowse = useCallback(
    (tag: MediaTag, mediaType: MediaKind) => push({ kind: 'browse', tag, mediaType }),
    [push],
  );

  const navActions = useMemo<NavActions>(
    () => ({
      setTab,
      setSearchQuery,
      focusSearch,
      openDetails,
      openPlayer,
      openSourcePicker,
      openBrowse,
      closeDetails: back,
      closeBrowse: back,
    }),
    [setTab, focusSearch, openDetails, openPlayer, openSourcePicker, openBrowse, back],
  );
  const navState = useMemo<NavState>(
    () => ({ activeTab, searchQuery, overlayDepth: stack.length }),
    [activeTab, searchQuery, stack.length],
  );

  useEffect(prefetchOverlays, []);

  const chooseProfile = useCallback(
    async (profile: Profile) => {
      setProfileBusy(true);
      try {
        if (profile.id !== me?.active_profile_id) {
          // A failed switch keeps the current profile and leaves the picker open.
          if (!(await switchProfile(profile.id))) return;
          clearReadCache();
          reset();
          clearNavMemory();
          resetActiveCard();
          setSearchQuery('');
        }
        markChosen();
        setGateDismissed(true);
        setGateRequested(false);
        setSwitcherOpen(false);
      } finally {
        setProfileBusy(false);
      }
    },
    [me?.active_profile_id, switchProfile, reset],
  );

  // Asked for explicitly (Switch Profile, "Who's watching?"): shown even when
  // Start With opens a profile at launch.
  const [gateRequested, setGateRequested] = useState(false);
  const openGate = useCallback(() => {
    setSwitcherOpen(false);
    setGateRequested(true);
    setGateDismissed(false);
  }, []);

  const openCreate = useCallback(() => {
    setSwitcherOpen(false);
    setEditorTarget(null);
  }, []);

  const openEdit = useCallback((profile: Profile) => {
    setSwitcherOpen(false);
    setEditorTarget(profile);
  }, []);

  const onManage = useCallback(() => {
    setSwitcherOpen(false);
    setTab('settings');
  }, [setTab]);

  const onSaved = useCallback(async () => {
    await refreshMe();
    setEditorTarget(undefined);
  }, [refreshMe]);

  const onDeleted = useCallback(async () => {
    await reload({ silent: true });
    setEditorTarget(undefined);
  }, [reload]);

  // "Start With" (tvOS Settings): open the chosen profile at launch instead of asking.
  const startWithDone = useRef(false);
  const launchProfile = useMemo(() => startWithProfile(me, profiles), [me, profiles]);
  useEffect(() => {
    if (startWithDone.current || isLoading || !me || gateDismissed) return;
    startWithDone.current = true;
    if (launchProfile) void chooseProfile(launchProfile);
  }, [isLoading, me, launchProfile, gateDismissed, chooseProfile]);

  // Show the "Who's watching?" gate on load only for multi-profile accounts
  // that ask at launch.
  const showGate = !gateDismissed && !isLoading && Boolean(me) && profiles.length >= 2 && (gateRequested || !launchProfile);
  const modalOpen = switcherOpen || showGate || editorTarget !== undefined;

  // Escape (or Backspace outside a text field) pops exactly one layer; with no
  // layer open it returns focus to the top bar, like the Siri Remote's Menu.
  // Layers that consume Escape themselves (the player's settings panel, menus,
  // the profile editor, the search field) preventDefault first.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const editable = Boolean(
        target && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)),
      );
      if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === 'f') {
        // Not from under an open page, player or dialog: the field is behind it.
        if (stack.length || modalOpen) return;
        // A second press inside the field falls through to the browser's find.
        if (!(target instanceof HTMLInputElement && target.hasAttribute('data-nav-search'))) {
          event.preventDefault();
          focusSearch();
        }
        return;
      }
      const isBack = event.key === 'Escape' || (event.key === 'Backspace' && !editable);
      if (!isBack || event.defaultPrevented || modalOpen || event.altKey || event.metaKey || event.ctrlKey) return;
      if (stack.length) {
        event.preventDefault();
        back();
        return;
      }
      if (!topNavRef.current?.contains(document.activeElement)) {
        event.preventDefault();
        topNavRef.current?.focusActiveTab();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [modalOpen, stack.length, back, focusSearch]);

  function renderLayerContent(entry: NavEntry) {
    switch (entry.kind) {
      case 'detail':
        return <Suspense fallback={<LoadingState label="Loading details" />}><Detail item={entry.item} /></Suspense>;
      case 'browse':
        return <Suspense fallback={<LoadingState label="Loading browse" />}><Browse filter={{ tag: entry.tag, mediaType: entry.mediaType }} /></Suspense>;
      case 'picker':
        return (
          <Suspense fallback={<LoadingState label="Loading sources" />}>
            <SourcePicker item={entry.item} profileId={profileId} onSelect={(selection) => openPlayer(entry.item, selection)} onClose={back} />
          </Suspense>
        );
      case 'player':
        return (
          <Suspense fallback={<LoadingState label="Preparing player" />}>
            <Player item={entry.item} profileId={profileId} selection={entry.selection} onClose={back} onPlay={openPlayer} onChooseSource={openSourcePicker} />
          </Suspense>
        );
    }
  }

  function renderLayer(entry: NavEntry) {
    return (
      <ErrorBoundary fullscreen resetKey={entry} onDismiss={back} title={entry.kind === 'player' ? 'Playback stopped' : "This page couldn't load"}>
        {renderLayerContent(entry)}
      </ErrorBoundary>
    );
  }

  return (
    <NavProvider actions={navActions} state={navState}>
      <UserStateProvider
        library={dashboard?.library ?? NO_ITEMS}
        continueWatching={dashboard?.continue_watching ?? NO_ITEMS}
        profileId={me?.active_profile_id}
      >
      <div className={styles.app} data-nav-root>
        <TopNav ref={topNavRef} me={me} menuOpen={switcherOpen} onAvatar={() => setSwitcherOpen((open) => !open)} />
        <main className={styles.main} ref={scrollRef} data-nav-scroll {...inertWhen(stack.length > 0)}>
          {isLoading && !dashboard ? (
            <DashboardSkeleton />
          ) : error && !dashboard ? (
            <EmptyState
              tone="error"
              icon={RefreshCw}
              title="Home Isn't Available"
              message={error}
              actionLabel="Try again"
              onAction={() => void reload()}
            />
          ) : (
            <ErrorBoundary resetKey={activeTab}>
              {(activeTab === 'home' || activeTab === 'movies' || activeTab === 'series') && (
                <Home key={activeTab} dashboard={dashboard} rows={rows} tab={activeTab} />
              )}
              {activeTab === 'search' && <Search />}
              {activeTab === 'library' && <Library />}
              {activeTab === 'settings' && (
                <Settings me={me} onAddProfile={openCreate} onEditProfile={openEdit} onSwitchProfile={openGate} onProfileChanged={() => void refreshMe()} onScopeChanged={() => void reload({ silent: true })} />
              )}
            </ErrorBoundary>
          )}
        </main>

        {switcherOpen ? (
          <ProfileSwitcher
            profiles={profiles}
            activeProfileId={me?.active_profile_id}
            busy={profileBusy}
            onSelect={(p) => void chooseProfile(p)}
            onManage={onManage}
            onAddProfile={openCreate}
            onWhosWatching={openGate}
            onSettings={() => {
              setSwitcherOpen(false);
              setTab('settings');
            }}
            onClose={() => setSwitcherOpen(false)}
          />
        ) : null}

        {showGate ? (
          <ProfileGate
            profiles={profiles}
            activeProfileId={me?.active_profile_id}
            busy={profileBusy}
            onSelect={(p) => void chooseProfile(p)}
            onAddProfile={profiles.length < MAX_PROFILES ? openCreate : undefined}
            onManage={() => {
              markChosen();
              setGateDismissed(true);
              setGateRequested(false);
              setTab('settings');
            }}
          />
        ) : null}

        {editorTarget !== undefined ? (
          <ProfileEditor
            profile={editorTarget}
            canDelete={profiles.length > 1}
            onClose={() => setEditorTarget(undefined)}
            onSaved={() => void onSaved()}
            onDeleted={() => void onDeleted()}
          />
        ) : null}

        {/* Overlays render in stack order: the newest layer is always on top. */}
        {stack.map((entry, index) => (
          <div
            key={layerKey(entry, index)}
            style={{ position: 'relative', zIndex: 90 + index }}
            data-nav-layer={entry.kind}
            data-nav-mode={entry.kind === 'player' ? 'native' : undefined}
            {...inertWhen(index < stack.length - 1)}
          >
            {renderLayer(entry)}
          </div>
        ))}
        <Toaster />
      </div>
      </UserStateProvider>
    </NavProvider>
  );
}
