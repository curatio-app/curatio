import { useSyncExternalStore } from 'react';

/**
 * The one shelf card that is visibly "active" (expanded, lifted). Mouse hover
 * and keyboard focus share this single state, like the tvOS focus engine: the
 * next arrow key starts from whatever card is expanded, and hovering a card
 * moves the highlight away from the keyboard-focused one.
 *
 * Kept outside React so moving the highlight re-renders exactly two cards
 * (the old and the new) instead of every shelf on the page.
 */
export type ActiveSource = 'pointer' | 'focus';

interface ActiveState {
  id: string | null;
  source: ActiveSource | null;
}

let state: ActiveState = { id: null, source: null };
const listeners = new Set<() => void>();

function emit() {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getActiveCard(): ActiveState {
  return state;
}

export function setActiveCard(id: string, source: ActiveSource) {
  if (state.id === id && state.source === source) return;
  state = { id, source };
  emit();
}

/** Clear the highlight, optionally only if it still belongs to `id`. */
export function clearActiveCard(id?: string, source?: ActiveSource) {
  if (!state.id) return;
  if (id && state.id !== id) return;
  if (source && state.source !== source) return;
  state = { id: null, source: null };
  emit();
}

/** True while the card with this id is the active one. */
export function useIsActiveCard(id: string) {
  return useSyncExternalStore(
    subscribe,
    () => state.id === id,
    () => false,
  );
}

/** For tests. */
export function resetActiveCard() {
  state = { id: null, source: null };
  emit();
}
