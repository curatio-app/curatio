import type { WebMediaItem } from '../api/types';

// Items the player must start from 0 regardless of saved progress: the next
// episode after an auto-advance, and "Start over". Kept apart from the player
// engine so navigation can carry the mark onto the copy it makes of an item
// (normalizeMediaItem) without loading the engine.
const marked = new WeakSet<WebMediaItem>();

/** Mark an item object so the player starts it at 0 (next episode, Start over). */
export function markStartFromBeginning(item: WebMediaItem) {
  marked.add(item);
}

export function isMarkedStartFromBeginning(item: WebMediaItem) {
  return marked.has(item);
}

/** Give `copy` the same mark as `original` (used when an item is cloned). */
export function carryStartFromBeginning(original: WebMediaItem, copy: WebMediaItem) {
  if (marked.has(original)) marked.add(copy);
  return copy;
}
