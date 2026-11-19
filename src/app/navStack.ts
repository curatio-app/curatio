import { useCallback, useEffect, useRef, useState } from 'react';
import type { SourceSelection } from '../api/playbackSources';
import type { MediaKind, MediaTag, WebMediaItem } from '../api/types';

// ---------------------------------------------------------------------------
// History-backed overlay stack — the web counterpart of the iPad navigation
// stack. Detail, Browse, the source picker and the player are pushed in order,
// render in that order (so the newest is always on top), and every push is a
// browser history entry: Back (button, gesture or Escape) pops exactly one.
// The whole stack lives in `history.state`, so Forward works too.
// ---------------------------------------------------------------------------

export type NavEntry =
  | { kind: 'detail'; item: WebMediaItem }
  | { kind: 'browse'; tag: MediaTag; mediaType: MediaKind }
  | { kind: 'picker'; item: WebMediaItem }
  | { kind: 'player'; item: WebMediaItem; selection?: SourceSelection };

type HistoryState = { curatioStack?: NavEntry[]; curatioGen?: number };

// Bumped by reset() (profile switch): history entries from an older generation
// belong to another profile and restore as the bare tab view.
const GEN_KEY = 'curatio.navGen';
let generation = readGeneration();

function readGeneration(): number {
  try {
    return Number(sessionStorage.getItem(GEN_KEY) || 0);
  } catch {
    return 0;
  }
}

function bumpGeneration() {
  generation += 1;
  try {
    sessionStorage.setItem(GEN_KEY, String(generation));
  } catch {
    /* in-memory generation still guards this page load */
  }
}

const sameEntry = (a: NavEntry | undefined, b: NavEntry) => Boolean(a) && JSON.stringify(a) === JSON.stringify(b);

/**
 * The stack a popstate restores. Unchanged layers keep their live objects so
 * they aren't remounted. A player or picker is only kept when it is already
 * open: browser Forward must never start playback on its own.
 */
export function stackFromPop(state: unknown, current: NavEntry[]): { stack: NavEntry[]; trimmed: boolean } {
  const saved = (state as HistoryState | null)?.curatioStack;
  if (!Array.isArray(saved)) return { stack: [], trimmed: false };
  if (((state as HistoryState).curatioGen ?? 0) !== generation) return { stack: [], trimmed: true };
  const next: NavEntry[] = [];
  for (let index = 0; index < saved.length; index += 1) {
    const entry = saved[index];
    if (sameEntry(current[index], entry)) {
      next.push(current[index]);
      continue;
    }
    if (entry.kind === 'player' || entry.kind === 'picker') return { stack: next, trimmed: true };
    next.push(entry);
  }
  return { stack: next, trimmed: false };
}

function itemId(item: WebMediaItem): string {
  return String(item.tmdb_id ?? item.imdb_id ?? item.id ?? '');
}

function itemType(item: WebMediaItem): string {
  return item.media_type === 'tv' || item.type === 'series' ? 'tv' : 'movie';
}

/** Hash URL for the top entry (empty for the bare tab view). */
export function entryHash(entry: NavEntry | undefined): string {
  if (!entry) return '';
  switch (entry.kind) {
    case 'detail':
      return `#/title/${itemType(entry.item)}/${encodeURIComponent(itemId(entry.item))}`;
    case 'browse':
      return `#/browse/${entry.mediaType}/${encodeURIComponent(String(entry.tag.id))}`;
    case 'picker':
      return `#/sources/${itemType(entry.item)}/${encodeURIComponent(itemId(entry.item))}`;
    case 'player': {
      const episode = entry.item.season != null && entry.item.episode != null ? `/${entry.item.season}/${entry.item.episode}` : '';
      return `#/play/${itemType(entry.item)}/${encodeURIComponent(itemId(entry.item))}${episode}`;
    }
  }
}

/** A `#/title/{type}/{id}` deep link becomes a Detail entry. */
export function entryFromHash(hash: string): NavEntry | null {
  const match = /^#\/title\/(movie|tv)\/([^/]+)$/.exec(hash);
  if (!match) return null;
  const id = decodeURIComponent(match[2]);
  const item: WebMediaItem = /^tt\d+$/.test(id)
    ? { imdb_id: id, media_type: match[1] as MediaKind }
    : { tmdb_id: Number(id), media_type: match[1] as MediaKind };
  return { kind: 'detail', item };
}

function urlFor(stack: NavEntry[]): string {
  const { pathname, search } = window.location;
  return `${pathname}${search}${entryHash(stack[stack.length - 1])}`;
}

/** Restore on load: keep detail/browse layers; never resume playback on reload. */
export function initialStack(state: unknown, hash: string): NavEntry[] {
  const saved = (state as HistoryState | null)?.curatioStack;
  if (Array.isArray(saved) && ((state as HistoryState).curatioGen ?? 0) !== generation) return [];
  if (Array.isArray(saved)) {
    const kept: NavEntry[] = [];
    for (const entry of saved) {
      if (entry.kind === 'player' || entry.kind === 'picker') break;
      kept.push(entry);
    }
    return kept;
  }
  const deepLink = entryFromHash(hash);
  return deepLink ? [deepLink] : [];
}

export interface NavStack {
  stack: NavEntry[];
  push: (entry: NavEntry) => void;
  /** Swap the top entry in place (picker → player, next episode). */
  replaceTop: (entry: NavEntry) => void;
  back: () => void;
  /** Pop every layer back to the tab view. */
  reset: () => void;
}

export function useNavStack(): NavStack {
  const [stack, setStackState] = useState<NavEntry[]>(() => {
    try {
      return initialStack(window.history.state, window.location.hash);
    } catch {
      return [];
    }
  });
  // Live mirror so history side effects stay outside state updaters.
  const stackRef = useRef(stack);
  const setStack = useCallback((next: NavEntry[]) => {
    stackRef.current = next;
    setStackState(next);
  }, []);

  // Record the restored stack so the first Back has somewhere to land.
  useEffect(() => {
    writeHistory('replace', stackRef.current);
  }, []);

  useEffect(() => {
    const onPop = (event: PopStateEvent) => {
      const current = stackRef.current;
      const { stack: next, trimmed } = stackFromPop(event.state, current);
      const unchanged = next.length === current.length && next.every((entry, index) => entry === current[index]);
      if (trimmed && unchanged && next.length) {
        // Forward into a closed player: step straight back to where we were
        // instead of leaving a duplicate entry that makes Back do nothing.
        window.history.back();
        return;
      }
      if (trimmed) writeHistory('replace', next);
      setStack(next);
    };
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, [setStack]);

  const push = useCallback((entry: NavEntry) => {
    const next = [...stackRef.current, entry];
    writeHistory('push', next);
    setStack(next);
  }, [setStack]);

  const replaceTop = useCallback((entry: NavEntry) => {
    const next = [...stackRef.current.slice(0, -1), entry];
    writeHistory('replace', next);
    setStack(next);
  }, [setStack]);

  const back = useCallback(() => {
    if (!stackRef.current.length) return;
    const saved = (window.history.state as HistoryState | null)?.curatioStack;
    if (Array.isArray(saved) && saved.length === stackRef.current.length) {
      window.history.back();
      return;
    }
    // History is out of step (or unavailable): pop locally and record it.
    const next = stackRef.current.slice(0, -1);
    writeHistory('replace', next);
    setStack(next);
  }, [setStack]);

  const reset = useCallback(() => {
    // A new generation: Back can no longer reopen the previous profile's pages.
    bumpGeneration();
    writeHistory('replace', []);
    if (stackRef.current.length) setStack([]);
  }, [setStack]);

  return { stack, push, replaceTop, back, reset };
}

function writeHistory(mode: 'push' | 'replace', next: NavEntry[]) {
  try {
    const state: HistoryState = { curatioStack: next, curatioGen: generation };
    if (mode === 'push') window.history.pushState(state, '', urlFor(next));
    else window.history.replaceState(state, '', urlFor(next));
  } catch {
    // History is a convenience; the in-memory stack still works.
  }
}
