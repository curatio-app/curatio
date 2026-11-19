import { createContext, useCallback, useContext, useEffect, useMemo, useState, useRef } from 'react';
import type { ReactNode } from 'react';
import { addLibraryItem, getWatched, isAbortError, markWatched, removeLibraryItem, removeProgress } from './accountClient';
import { displayTitle, itemKind, playbackProgressKey, setProgressProfile } from './mediaUtils';
import type { WatchedEntry, WebMediaItem } from './types';

// ---------------------------------------------------------------------------
// Per-profile library / watched / Continue Watching state — the web side of
// the native app's `toggleSaved`, `recordProgress` and progress removal. Seeded
// from the dashboard bootstrap plus `/account/api/watched`; every action is
// optimistic, rolls back on failure, and announces `curatio:library-changed`
// so the dashboard refreshes in the background.
// ---------------------------------------------------------------------------

type ServerKind = 'movie' | 'tv';

function serverKind(item: WebMediaItem): ServerKind {
  return itemKind(item) === 'series' ? 'tv' : 'movie';
}

function tmdbOf(item: { tmdb_id?: unknown }): number | null {
  const value = Number(item.tmdb_id);
  return Number.isFinite(value) && value > 0 ? value : null;
}

function imdbOf(item: { imdb_id?: unknown }): string | null {
  const value = String(item.imdb_id ?? '');
  return /^tt\d+$/.test(value) ? value : null;
}

/** Every identity a title can be matched by (TMDB and IMDb keys). */
function titleKeys(item: { tmdb_id?: unknown; imdb_id?: unknown }, kind: ServerKind, season?: unknown, episode?: unknown): string[] {
  const suffix = season != null && episode != null ? `:${Number(season)}:${Number(episode)}` : '';
  const keys: string[] = [];
  const tmdb = tmdbOf(item);
  const imdb = imdbOf(item);
  if (tmdb) keys.push(`${kind}:tmdb:${tmdb}${suffix}`);
  if (imdb) keys.push(`${kind}:imdb:${imdb}${suffix}`);
  return keys;
}

/** The id the server routes accept: TMDB when known, else IMDb. */
function routeId(item: WebMediaItem): string | number | null {
  return tmdbOf(item) ?? imdbOf(item);
}

function clearLocalPosition(item: WebMediaItem) {
  try {
    localStorage.removeItem(playbackProgressKey(item));
  } catch {
    // Saved positions are a convenience only.
  }
}

let announcing = false;

function announce() {
  announcing = true;
  try {
    window.dispatchEvent(new Event('curatio:library-changed'));
  } finally {
    announcing = false;
  }
}

export interface UserState {
  isSaved: (item: WebMediaItem) => boolean;
  /** Saved titles: the server list with local adds/removes applied. */
  savedItems: WebMediaItem[];
  /** Movies, or one episode when `season`/`episode` are given. */
  isWatched: (item: WebMediaItem, season?: unknown, episode?: unknown) => boolean;
  /** Continue Watching with locally removed entries hidden. */
  continueWatching: WebMediaItem[];
  toggleSaved: (item: WebMediaItem) => Promise<void>;
  markWatched: (item: WebMediaItem, season?: number | null, episode?: number | null) => Promise<void>;
  markUnwatched: (item: WebMediaItem, season?: number | null, episode?: number | null) => Promise<void>;
  removeFromContinueWatching: (item: WebMediaItem) => Promise<void>;
  /** Active profile (matches the player's source-cache key). */
  profileId?: number;
}

const UserStateContext = createContext<UserState | null>(null);

type Override<T> = Map<string, T>;

export function UserStateProvider({
  library,
  continueWatching,
  profileId,
  children,
}: {
  library: WebMediaItem[];
  continueWatching: WebMediaItem[];
  /** Watched state is per profile; refetched when it changes. */
  profileId?: number | null;
  children: ReactNode;
}) {
  // Before anything below reads a saved position.
  setProgressProfile(profileId);
  const [watched, setWatched] = useState<WatchedEntry[]>([]);
  const [savedOverrides, setSavedOverrides] = useState<Override<WebMediaItem | null>>(() => new Map());
  const [watchedOverrides, setWatchedOverrides] = useState<Override<boolean>>(() => new Map());
  const [hiddenContinue, setHiddenContinue] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    let controller = new AbortController();
    setSavedOverrides(new Map());
    setWatchedOverrides(new Map());
    setHiddenContinue(new Set());
    getWatched(controller.signal)
      .then((data) => setWatched(Array.isArray(data.items) ? data.items : []))
      .catch((err) => {
        if (!controller.signal.aborted && !isAbortError(err)) setWatched([]);
      });
    // Playback finishing a title (or an action here) changes what is watched;
    // a failed refresh keeps the list already shown.
    const refresh = () => {
      // Our own actions keep their optimistic marks; a change made elsewhere
      // (playback finishing a title) is the server's to tell.
      const fromPlayback = !announcing;
      controller.abort();
      controller = new AbortController();
      getWatched(controller.signal)
        .then((data) => {
          if (!Array.isArray(data.items)) return;
          setWatched(data.items);
          // Or a title marked unwatched here stays unwatched after being watched.
          if (fromPlayback) setWatchedOverrides(new Map());
        })
        .catch(() => undefined);
    };
    window.addEventListener('curatio:library-changed', refresh);
    return () => {
      window.removeEventListener('curatio:library-changed', refresh);
      controller.abort();
    };
  }, [profileId]);

  const savedBase = useMemo(() => {
    const keys = new Set<string>();
    for (const item of library) for (const key of titleKeys(item, serverKind(item))) keys.add(key);
    return keys;
  }, [library]);

  const watchedBase = useMemo(() => {
    const keys = new Set<string>();
    for (const entry of watched) {
      for (const key of titleKeys(entry, entry.media_type, entry.media_type === 'tv' ? entry.season : undefined, entry.media_type === 'tv' ? entry.episode : undefined)) keys.add(key);
    }
    return keys;
  }, [watched]);

  const isSaved = useCallback((item: WebMediaItem) => {
    const keys = titleKeys(item, serverKind(item));
    for (const key of keys) if (savedOverrides.has(key)) return savedOverrides.get(key) !== null;
    return keys.some((key) => savedBase.has(key));
  }, [savedBase, savedOverrides]);

  const isWatched = useCallback((item: WebMediaItem, season?: unknown, episode?: unknown) => {
    const keys = titleKeys(item, serverKind(item), season ?? (serverKind(item) === 'tv' ? item.season : undefined), episode ?? (serverKind(item) === 'tv' ? item.episode : undefined));
    for (const key of keys) if (watchedOverrides.has(key)) return Boolean(watchedOverrides.get(key));
    return keys.some((key) => watchedBase.has(key));
  }, [watchedBase, watchedOverrides]);

  const savedItems = useMemo(() => {
    const removed = new Set<string>();
    const added: WebMediaItem[] = [];
    for (const [key, value] of savedOverrides) {
      if (value === null) removed.add(key);
      else if (!added.includes(value)) added.push(value);
    }
    const base = library.filter((item) => !titleKeys(item, serverKind(item)).some((key) => removed.has(key)));
    const extra = added.filter((item) => !titleKeys(item, serverKind(item)).some((key) => savedBase.has(key)));
    return [...extra, ...base];
  }, [library, savedOverrides, savedBase]);

  const continueList = useMemo(
    () => continueWatching.filter((item) => !hiddenContinue.has(playbackProgressKey(item))),
    [continueWatching, hiddenContinue],
  );

  // A fresh list from the server replaces what was hidden here: a title
  // removed from Continue Watching comes back once it is watched again.
  useEffect(() => {
    setHiddenContinue((current) => (current.size ? new Set() : current));
  }, [continueWatching]);

  const savingRef = useRef(new Set<string>());

  const setSavedFor = (item: WebMediaItem, value: WebMediaItem | null | undefined) => {
    setSavedOverrides((current) => {
      const next = new Map(current);
      for (const key of titleKeys(item, serverKind(item))) {
        if (value === undefined) next.delete(key);
        else next.set(key, value);
      }
      return next;
    });
  };

  const toggleSaved = useCallback(async (item: WebMediaItem) => {
    const wasSaved = isSaved(item);
    const kind = serverKind(item);
    // One change per title at a time: a double click sent add and remove together.
    const pendingKey = titleKeys(item, kind)[0] ?? displayTitle(item);
    if (savingRef.current.has(pendingKey)) return;
    savingRef.current.add(pendingKey);
    setSavedFor(item, wasSaved ? null : item);
    try {
      if (wasSaved) {
        const id = routeId(item);
        if (id == null) throw new Error('This title has no id to remove.');
        await removeLibraryItem(kind, id);
      } else {
        await addLibraryItem({
          tmdb_id: tmdbOf(item),
          imdb_id: imdbOf(item),
          media_type: kind,
          title: displayTitle(item),
          poster_path: (item.poster_path ?? item.poster ?? null) as string | null,
          year: item.year ?? null,
        });
      }
      announce();
    } catch (err) {
      setSavedFor(item, undefined);
      throw err;
    } finally {
      savingRef.current.delete(pendingKey);
    }
  }, [isSaved]);

  const setWatchedFor = (item: WebMediaItem, season: unknown, episode: unknown, value: boolean | undefined) => {
    setWatchedOverrides((current) => {
      const next = new Map(current);
      for (const key of titleKeys(item, serverKind(item), season, episode)) {
        if (value === undefined) next.delete(key);
        else next.set(key, value);
      }
      return next;
    });
  };

  const episodeItem = (item: WebMediaItem, season?: number | null, episode?: number | null): WebMediaItem =>
    season != null && episode != null ? { ...item, season, episode } : item;

  const doMarkWatched = useCallback(async (item: WebMediaItem, season?: number | null, episode?: number | null) => {
    const kind = serverKind(item);
    const s = kind === 'tv' ? season ?? null : null;
    const e = kind === 'tv' ? episode ?? null : null;
    setWatchedFor(item, s ?? undefined, e ?? undefined, true);
    try {
      await markWatched({ tmdb_id: tmdbOf(item), imdb_id: imdbOf(item), media_type: kind, season: s, episode: e, title: displayTitle(item) });
      clearLocalPosition(episodeItem(item, s, e));
      announce();
    } catch (err) {
      setWatchedFor(item, s ?? undefined, e ?? undefined, undefined);
      throw err;
    }
  }, []);

  const doMarkUnwatched = useCallback(async (item: WebMediaItem, season?: number | null, episode?: number | null) => {
    const kind = serverKind(item);
    const s = kind === 'tv' ? season ?? null : null;
    const e = kind === 'tv' ? episode ?? null : null;
    const id = routeId(item);
    if (id == null) throw new Error('This title has no id.');
    setWatchedFor(item, s ?? undefined, e ?? undefined, false);
    try {
      await removeProgress(kind, id, s, e);
      clearLocalPosition(episodeItem(item, s, e));
      announce();
    } catch (err) {
      setWatchedFor(item, s ?? undefined, e ?? undefined, undefined);
      throw err;
    }
  }, []);

  const removeFromContinueWatching = useCallback(async (item: WebMediaItem) => {
    const key = playbackProgressKey(item);
    const id = routeId(item);
    if (id == null) throw new Error('This title has no id.');
    setHiddenContinue((current) => new Set(current).add(key));
    try {
      const kind = serverKind(item);
      const season = kind === 'tv' && item.season != null ? Number(item.season) : null;
      const episode = kind === 'tv' && item.episode != null ? Number(item.episode) : null;
      await removeProgress(kind, id, season, episode);
      clearLocalPosition(item);
      announce();
    } catch (err) {
      setHiddenContinue((current) => {
        const next = new Set(current);
        next.delete(key);
        return next;
      });
      throw err;
    }
  }, []);

  const value = useMemo<UserState>(() => ({
    isSaved,
    savedItems,
    isWatched,
    continueWatching: continueList,
    toggleSaved,
    markWatched: doMarkWatched,
    markUnwatched: doMarkUnwatched,
    removeFromContinueWatching,
    profileId: profileId ?? undefined,
  }), [isSaved, savedItems, isWatched, continueList, toggleSaved, doMarkWatched, doMarkUnwatched, removeFromContinueWatching, profileId]);

  return <UserStateContext.Provider value={value}>{children}</UserStateContext.Provider>;
}

export function useUserState(): UserState {
  const value = useContext(UserStateContext);
  if (!value) throw new Error('useUserState must be used within a UserStateProvider');
  return value;
}

/** The user state when rendered inside a provider, else null (e.g. isolated player tests). */
export function useOptionalUserState(): UserState | null {
  return useContext(UserStateContext);
}
