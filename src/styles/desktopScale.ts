/**
 * The desktop scale model shared by the web, macOS and Windows clients
 * (DesktopContracts/design-tokens.json, version 2). tokens.css implements the
 * same formulas in CSS; this mirror is for layout maths in JS (rail scrolling,
 * row centring) and is asserted against the contract's parity table.
 */

function clampNumber(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max);
}

/** Layout unit: px per tvOS point. */
export function layoutUnit(width: number) {
  if (width <= 760) return 0.42;
  return clampNumber(width / 2560, 0.5, 0.8);
}

/** Type unit: px per tvOS point for text. */
export function typeUnit(width: number) {
  if (width <= 760) return 0.56;
  return clampNumber(0.36 + width / 6000, 0.56, 0.7);
}

export function pageMargin(width: number) {
  if (width <= 760) return 18;
  return Math.max(24, 88 * layoutUnit(width));
}

export function heroHeight(width: number, height: number) {
  if (width <= 760) return Math.min(0.7 * height, 1.32 * width);
  const byHeight = clampNumber(0.62 * height, 420, 760);
  return Math.min(byHeight, (width - 2 * pageMargin(width)) / 1.8);
}

export interface DesktopMetrics {
  u: number;
  t: number;
  pageMargin: number;
  heroHeight: number;
  posterRest: [number, number];
  posterExpanded: [number, number];
  continueCard: [number, number];
  shelfGap: number;
  rowTitle: number;
  navTab: number;
}

export function desktopMetrics(width: number, height: number): DesktopMetrics {
  const u = layoutUnit(width);
  const t = typeUnit(width);
  return {
    u,
    t,
    pageMargin: pageMargin(width),
    heroHeight: heroHeight(width, height),
    posterRest: [260 * u, 450 * u],
    posterExpanded: [660 * u, 450 * u],
    continueCard: [426 * u, 240 * u],
    shelfGap: 24 * u,
    rowTitle: 36 * t,
    navTab: 29 * t,
  };
}

/** Metrics for the current window (falls back to 1440×900 outside a browser). */
export function currentMetrics(): DesktopMetrics {
  if (typeof window === 'undefined') return desktopMetrics(1440, 900);
  const width = document.documentElement?.clientWidth || window.innerWidth || 1440;
  return desktopMetrics(width, window.innerHeight || 900);
}
