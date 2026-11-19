import { useSyncExternalStore } from 'react';
import { hideTitle, unhideTitle } from './accountClient';
import { itemKind } from './mediaUtils';
import type { WebMediaItem } from './types';

// Titles hidden with "Not for me" in this session. Cards disappear at once;
// the server drops them from every row on the next Home build.

const hidden = new Set<string>();
const listeners = new Set<() => void>();
let version = 0;

function keyOf(item?: WebMediaItem | null): string | null {
  const id = Number(item?.tmdb_id);
  if (!Number.isFinite(id) || id <= 0) return null;
  return `${itemKind(item) === 'series' ? 'tv' : 'movie'}:${id}`;
}

function notify() {
  version += 1;
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function canHide(item?: WebMediaItem | null): boolean {
  return keyOf(item) !== null;
}

export function isHidden(item?: WebMediaItem | null): boolean {
  const key = keyOf(item);
  return key !== null && hidden.has(key);
}

export function useIsHidden(item?: WebMediaItem | null): boolean {
  useSyncExternalStore(subscribe, () => version, () => version);
  return isHidden(item);
}

/** Hide a title; rolls back and rethrows when the server refuses. */
export async function hideItem(item: WebMediaItem): Promise<void> {
  const key = keyOf(item);
  if (!key) return;
  const [mediaType, id] = key.split(':') as ['movie' | 'tv', string];
  hidden.add(key);
  notify();
  try {
    await hideTitle(Number(id), mediaType);
  } catch (error) {
    hidden.delete(key);
    notify();
    throw error;
  }
  window.dispatchEvent(new Event('curatio:library-changed'));
}

export async function unhide(tmdbId: number, mediaType: 'movie' | 'tv'): Promise<void> {
  await unhideTitle(tmdbId, mediaType);
  hidden.delete(`${mediaType}:${tmdbId}`);
  notify();
  window.dispatchEvent(new Event('curatio:library-changed'));
}

/** Test hook. */
export function resetHiddenTitles() {
  hidden.clear();
  notify();
}
