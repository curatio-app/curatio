import { useEffect } from 'react';
import { currentMetrics } from '../styles/desktopScale';
import { getActiveCard } from './activeCard';

/**
 * Remote-style arrow-key navigation (tvOS focus engine, adapted for a keyboard).
 *
 * The DOM declares the structure; this module only moves focus:
 *
 *   data-nav-root            the app (browse pages live directly under it)
 *   data-nav-layer           an overlay (Detail, Browse, picker, player);
 *                            ="modal" for dialogs, which win over everything
 *   data-nav-mode="native"   on a layer: arrows belong to the layer (player,
 *                            forms, menus), the engine stays out
 *   data-nav-scope="<id>"    namespaces row memory (one per page/tab)
 *   data-nav-row="<key>"     a focus row: the nav bar, hero buttons, a shelf…
 *   data-nav-grid            on a row: arrows move in two dimensions
 *   data-nav-memory="off"    on a row: enter at the item nearest the origin
 *                            instead of the remembered one (nav bar, pills)
 *   data-nav-rail            the horizontal scroller inside a shelf row
 *   data-nav-expands         on a rail whose cards widen when active
 *   data-nav-item            a focus stop (always a real button/link/input)
 *   data-nav-default         where the first arrow press lands in a layer
 *   data-nav-scroll          the vertical scroller that owns rows
 *   data-nav-clearance="px"  on a scroller: space kept above a focused row
 *   data-nav-search          the top bar search field (↓ leaves it)
 *
 * Left/right move within a row and never wrap; at an edge the row receives a
 * `nav-edge` event (the hero uses it to change titles). Up/down move between
 * rows, restoring each row's last focused item while it is still on screen.
 * One window keydown listener in the bubble phase means anything that handled
 * the key first (menus, the player, text fields) wins by calling
 * preventDefault.
 */

export type NavDirection = 'left' | 'right' | 'up' | 'down';

const ROW = '[data-nav-row]';
const ITEM = '[data-nav-item]';
const LAYER = '[data-nav-layer]';

const KEY_DIRECTIONS: Record<string, NavDirection> = {
  ArrowLeft: 'left',
  ArrowRight: 'right',
  ArrowUp: 'up',
  ArrowDown: 'down',
};

const memory = new Map<string, number>();

/** Forget every row's remembered item (profile switch, sign-out). */
export function clearNavMemory() {
  memory.clear();
}

function hasLayout() {
  return !(typeof navigator !== 'undefined' && /jsdom/i.test(navigator.userAgent));
}

function isVisible(element: Element) {
  return !hasLayout() || element.getClientRects().length > 0;
}

function prefersReducedMotion() {
  return typeof window !== 'undefined' && Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)').matches);
}

function belongsTo(element: Element, layer: Element) {
  const owner = element.closest(LAYER);
  return layer.hasAttribute('data-nav-layer') ? owner === layer : owner === null;
}

/** The layer arrow keys currently operate in. */
export function activeLayer(root: ParentNode = document): HTMLElement | null {
  const modals = root.querySelectorAll<HTMLElement>('[data-nav-layer="modal"]');
  if (modals.length) return modals[modals.length - 1];
  const layers = root.querySelectorAll<HTMLElement>(LAYER);
  if (layers.length) return layers[layers.length - 1];
  return root.querySelector<HTMLElement>('[data-nav-root]');
}

/** Rows the arrows can land on. Only the row and its first usable item are
 * measured: reading every card's layout on each key press forced a full
 * reflow while cards were still animating. */
export function rowsIn(layer: HTMLElement): HTMLElement[] {
  return Array.from(layer.querySelectorAll<HTMLElement>(ROW)).filter(
    (row) => belongsTo(row, layer) && isVisible(row) && hasUsableItem(row),
  );
}

function hasUsableItem(row: HTMLElement) {
  for (const item of row.querySelectorAll<HTMLElement>(ITEM)) {
    if (item.closest(ROW) === row && !(item as HTMLButtonElement).disabled) return isVisible(item);
  }
  return false;
}

export function itemsIn(row: HTMLElement): HTMLElement[] {
  return Array.from(row.querySelectorAll<HTMLElement>(ITEM)).filter(
    (item) => item.closest(ROW) === row && !(item as HTMLButtonElement).disabled && isVisible(item),
  );
}

function rowMemoryKey(row: HTMLElement) {
  const scope = row.closest('[data-nav-scope]')?.getAttribute('data-nav-scope') ?? 'root';
  return `${scope}:${row.getAttribute('data-nav-row') ?? ''}`;
}

/** Make only one item per row tabbable, so Tab moves between rows. */
export function syncRovingTabIndex(row: HTMLElement, focusedIndex?: number) {
  const items = itemsIn(row);
  if (!items.length) return;
  const wanted = focusedIndex ?? memory.get(rowMemoryKey(row)) ?? 0;
  const current = Math.min(Math.max(wanted, 0), items.length - 1);
  items.forEach((item, index) => {
    const value = index === current ? '0' : '-1';
    if (item.getAttribute('tabindex') !== value) item.setAttribute('tabindex', value);
  });
}

/** Record which item of its row an element is (called on every focusin). */
export function rememberFocus(target: EventTarget | null) {
  if (!(target instanceof HTMLElement) || !target.matches(ITEM)) return;
  const row = target.closest<HTMLElement>(ROW);
  if (!row) return;
  const index = itemsIn(row).indexOf(target);
  if (index < 0) return;
  memory.set(rowMemoryKey(row), index);
  syncRovingTabIndex(row, index);
}

function originItem(layer: HTMLElement): HTMLElement | null {
  const active = getActiveCard();
  if (active.id) {
    const card = layer.querySelector<HTMLElement>(`[data-card-id="${cssEscape(active.id)}"]`);
    if (card && card.matches(ITEM) && belongsTo(card, layer)) return card;
  }
  const focused = document.activeElement;
  if (focused instanceof HTMLElement && focused.matches(ITEM) && layer.contains(focused)) {
    const row = focused.closest<HTMLElement>(ROW);
    if (row && belongsTo(row, layer)) return focused;
  }
  return null;
}

function cssEscape(value: string) {
  return typeof CSS !== 'undefined' && CSS.escape ? CSS.escape(value) : value.replace(/["\\]/g, '\\$&');
}

function centerX(element: Element) {
  const rect = element.getBoundingClientRect();
  return rect.left + rect.width / 2;
}

function nearestByX(items: HTMLElement[], origin: HTMLElement | null) {
  if (!origin || !hasLayout()) return items[0];
  const x = centerX(origin);
  let best = items[0];
  let distance = Infinity;
  for (const item of items) {
    const delta = Math.abs(centerX(item) - x);
    if (delta < distance) {
      best = item;
      distance = delta;
    }
  }
  return best;
}

function railOf(row: HTMLElement) {
  return row.hasAttribute('data-nav-rail') ? row : row.querySelector<HTMLElement>('[data-nav-rail]');
}

function isInRailView(item: HTMLElement, rail: HTMLElement | null) {
  if (!rail || !hasLayout()) return true;
  const view = rail.getBoundingClientRect();
  const rect = item.getBoundingClientRect();
  return rect.right > view.left + 1 && rect.left < view.right - 1;
}

/** The item a row is entered at, coming vertically from `origin`. */
function entryItem(row: HTMLElement, origin: HTMLElement | null) {
  const items = itemsIn(row);
  if (!items.length) return null;
  if (row.getAttribute('data-nav-memory') === 'off') return nearestByX(items, origin);
  const rail = railOf(row);
  const remembered = memory.get(rowMemoryKey(row));
  if (remembered != null) {
    const item = items[Math.min(remembered, items.length - 1)];
    // A rail the user has scrolled with the mouse since doesn't jump back.
    if (isInRailView(item, rail)) return item;
  }
  if (rail && hasLayout()) {
    const first = items.find((item) => isInRailView(item, rail));
    if (first) return first;
  }
  if (!rail) return nearestByX(items, origin);
  return items[0];
}

function gridNeighbor(items: HTMLElement[], index: number, direction: 'up' | 'down') {
  if (!hasLayout()) return null;
  const origin = items[index];
  // Measure each item once.
  const tops = new Map(items.map((item) => [item, item.getBoundingClientRect().top]));
  const top = tops.get(origin)!;
  const candidates = items.filter((item) => {
    const itemTop = tops.get(item)!;
    return direction === 'down' ? itemTop > top + 4 : itemTop < top - 4;
  });
  if (!candidates.length) return null;
  const candidateTops = candidates.map((item) => tops.get(item)!);
  const lineTop = direction === 'down' ? Math.min(...candidateTops) : Math.max(...candidateTops);
  const line = candidates.filter((item) => Math.abs(tops.get(item)! - lineTop) < 4);
  return nearestByX(line, origin);
}

// --- Scrolling ---------------------------------------------------------------

function bezier(x1: number, y1: number, x2: number, y2: number) {
  const sample = (a: number, b: number, t: number) => ((1 - 3 * b + 3 * a) * t + (3 * b - 6 * a)) * t * t + 3 * a * t;
  return (x: number) => {
    let lo = 0;
    let hi = 1;
    let t = x;
    for (let i = 0; i < 24; i += 1) {
      const value = sample(x1, x2, t);
      if (Math.abs(value - x) < 1e-4) break;
      if (value < x) lo = t;
      else hi = t;
      t = (lo + hi) / 2;
    }
    return sample(y1, y2, t);
  };
}

/** tvOS page move: 560 ms, cubic-bezier(.35,0,.2,1). */
const pageEase = bezier(0.35, 0, 0.2, 1);
const PAGE_SCROLL_MS = 560;
const scrollAnimations = new WeakMap<HTMLElement, number>();

export function animateScrollTop(scroller: HTMLElement, target: number) {
  const max = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
  const end = Math.min(Math.max(0, target), max || target);
  const previous = scrollAnimations.get(scroller);
  if (previous != null) window.cancelAnimationFrame(previous);
  const start = scroller.scrollTop;
  if (Math.abs(end - start) < 1) return;
  if (prefersReducedMotion() || !hasLayout()) {
    scroller.scrollTop = end;
    return;
  }
  const began = performance.now();
  const step = (now: number) => {
    const progress = Math.min(1, (now - began) / PAGE_SCROLL_MS);
    scroller.scrollTop = start + (end - start) * pageEase(progress);
    if (progress < 1) scrollAnimations.set(scroller, window.requestAnimationFrame(step));
    else scrollAnimations.delete(scroller);
  };
  scrollAnimations.set(scroller, window.requestAnimationFrame(step));
}

function topClearance(scroller: HTMLElement) {
  // A scroller can reserve its own space (Details keeps its Back button clear).
  const declared = Number(scroller.getAttribute('data-nav-clearance'));
  if (Number.isFinite(declared) && declared > 0) return declared;
  const nav = scroller.closest(LAYER) ? null : document.querySelector('[data-top-nav]');
  const navHeight = nav ? nav.getBoundingClientRect().height : 0;
  return navHeight + 18;
}

/** Scroll the page so a row's title sits just under the top bar. */
export function scrollRowIntoView(row: HTMLElement) {
  const scroller = row.closest<HTMLElement>('[data-nav-scroll]');
  if (!scroller) return;
  if (row.hasAttribute('data-nav-top')) {
    animateScrollTop(scroller, 0);
    return;
  }
  if (!hasLayout()) return;
  const rowTop = row.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
  animateScrollTop(scroller, rowTop - topClearance(scroller));
}

/**
 * Keep a rail item fully visible inside the page margins. Cards in an
 * expanding rail are placed arithmetically (only one card is ever expanded),
 * so this never reads layout mid-transition.
 */
export function ensureVisibleInRail(item: HTMLElement, row: HTMLElement) {
  const rail = railOf(row);
  if (!rail || !hasLayout()) return;
  const metrics = currentMetrics();
  const margin = metrics.pageMargin;
  let left: number;
  let width: number;
  if (rail.hasAttribute('data-nav-expands')) {
    const index = itemsIn(row).indexOf(item);
    left = margin + index * (metrics.posterRest[0] + metrics.shelfGap);
    width = metrics.posterExpanded[0];
  } else {
    // Measured against the rail itself: `offsetLeft` is relative to the nearest
    // positioned ancestor, which is not the rail for every row.
    left = item.getBoundingClientRect().left - rail.getBoundingClientRect().left + rail.scrollLeft;
    width = item.offsetWidth;
  }
  let target = rail.scrollLeft;
  if (left - margin < rail.scrollLeft) target = left - margin;
  else if (left + width + margin > rail.scrollLeft + rail.clientWidth) target = left + width + margin - rail.clientWidth;
  if (Math.abs(target - rail.scrollLeft) < 1) return;
  rail.scrollTo({ left: Math.max(0, target), behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
}

// --- Moving ------------------------------------------------------------------

function focusItem(item: HTMLElement, row: HTMLElement, vertical: boolean) {
  item.focus({ preventScroll: true });
  ensureVisibleInRail(item, row);
  if (vertical) scrollRowIntoView(row);
}

/** Focus where the first arrow press in a layer should land. */
export function focusDefault(layer: HTMLElement) {
  const preferred = Array.from(layer.querySelectorAll<HTMLElement>('[data-nav-default]')).find(
    (element) => belongsTo(element, layer) && isVisible(element),
  );
  const target = preferred ?? (rowsIn(layer)[0] ? itemsIn(rowsIn(layer)[0])[0] : null);
  if (!target) return false;
  const row = target.closest<HTMLElement>(ROW);
  target.focus({ preventScroll: true });
  if (row) ensureVisibleInRail(target, row);
  return true;
}

/** Move focus one step. Returns true when the key was consumed. */
export function moveFocus(direction: NavDirection, layer = activeLayer()): boolean {
  if (!layer) return false;
  const origin = originItem(layer);
  if (!origin) return focusDefault(layer);
  const row = origin.closest<HTMLElement>(ROW);
  if (!row) return false;
  const items = itemsIn(row);
  const index = items.indexOf(origin);

  if (direction === 'left' || direction === 'right') {
    const next = index + (direction === 'right' ? 1 : -1);
    if (next < 0 || next >= items.length) {
      row.dispatchEvent(new CustomEvent('nav-edge', { detail: { direction } }));
      return true;
    }
    focusItem(items[next], row, false);
    return true;
  }

  if (row.hasAttribute('data-nav-grid')) {
    const neighbor = gridNeighbor(items, index, direction);
    if (neighbor) {
      neighbor.focus({ preventScroll: true });
      neighbor.scrollIntoView?.({ block: 'nearest', behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
      return true;
    }
  }

  const rows = rowsIn(layer);
  const rowIndex = rows.indexOf(row);
  const nextRow = rows[rowIndex + (direction === 'down' ? 1 : -1)];
  if (!nextRow) return true;
  const target = entryItem(nextRow, origin);
  if (!target) return true;
  focusItem(target, nextRow, true);
  return true;
}

function isEditable(element: HTMLElement) {
  if (element.isContentEditable) return true;
  if (element instanceof HTMLTextAreaElement || element instanceof HTMLSelectElement) return true;
  if (element instanceof HTMLInputElement) {
    return !['button', 'checkbox', 'radio', 'submit', 'reset', 'range', 'color', 'file'].includes(element.type);
  }
  return false;
}

function isSliderLike(element: HTMLElement) {
  return (element instanceof HTMLInputElement && element.type === 'range') || element.getAttribute('role') === 'slider';
}

/** The keydown handler. Exported for tests. */
export function handleNavKey(event: KeyboardEvent): boolean {
  if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return false;
  const direction = KEY_DIRECTIONS[event.key];
  if (!direction) return false;
  const layer = activeLayer();
  if (!layer || layer.getAttribute('data-nav-mode') === 'native') return false;
  if (document.querySelector('[role="menu"]')) return false;
  const focused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  if (focused && isSliderLike(focused)) return false;
  // A closed <select> changes its value with Up/Down; Left/Right leave it.
  const leavesSelect = focused instanceof HTMLSelectElement && (direction === 'left' || direction === 'right');
  // The search field gives up Left/Right once the caret is at that end, so the
  // controls beside it (profile, tabs) stay reachable.
  const leavesSearch = focused instanceof HTMLInputElement && focused.hasAttribute('data-nav-search') && (
    direction === 'down'
    || (direction === 'left' && focused.selectionStart === 0 && focused.selectionEnd === 0)
    || (direction === 'right' && focused.selectionStart === focused.value.length && focused.selectionEnd === focused.value.length)
  );
  if (focused && isEditable(focused) && !leavesSelect && !leavesSearch) {
    return false;
  }
  const handled = moveFocus(direction, layer);
  if (handled) event.preventDefault();
  return handled;
}

/** Install the engine (once, in AppShell). */
export function useSpatialNav() {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      handleNavKey(event);
    };
    const onFocusIn = (event: FocusEvent) => rememberFocus(event.target);
    window.addEventListener('keydown', onKey);
    document.addEventListener('focusin', onFocusIn);
    return () => {
      window.removeEventListener('keydown', onKey);
      document.removeEventListener('focusin', onFocusIn);
    };
  }, []);
}
