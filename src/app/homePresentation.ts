import type { CatalogRow, DashboardResponse, WebMediaItem } from '../api/types';
import { displayTitle, isTopTenRow, itemImage, itemKind, logoImage, releaseBadge } from '../api/mediaUtils';
import type { Tab } from './navigation';

/** Most titles the Home hero cycles through. */
export const HERO_LIMIT = 5;

export type ShelfStyle = 'standard' | 'continue' | 'topTen';

export interface PresentedRow {
  key: string;
  title: string;
  items: WebMediaItem[];
  style: ShelfStyle;
}

export interface HomePresentation {
  hero: WebMediaItem[];
  rows: PresentedRow[];
}

function itemIdentity(item: WebMediaItem) {
  return `${itemKind(item)}:${item.tmdb_id ?? item.imdb_id ?? displayTitle(item)}`;
}

function rowKey(row: CatalogRow) {
  const rowType = String(row.type ?? row.media_type ?? row.catalog_type ?? row.items?.[0]?.media_type ?? 'mixed');
  return `${row.id ?? row.name}:${rowType}:${row.name}`;
}

function isUpNextRow(row: CatalogRow) {
  return `${String(row.id ?? '')} ${row.name}`.toLowerCase().includes('up-next');
}

function hasBackdrop(item: WebMediaItem) {
  return Boolean(item.backdrop_path ?? item.backdrop) && itemImage(item, false) !== itemImage(item, true);
}

/**
 * tvOS `HeroSelection`: the server's hero plus the rest of the shelf it came
 * from, keeping only titles with backdrop art and preferring ones with a logo
 * lockup. Returns the picks and the source row they should be removed from.
 */
export function pickHero(dashboard: DashboardResponse | null, rows: CatalogRow[]) {
  const hero = dashboard?.hero ?? null;
  const heroId = hero ? itemIdentity(hero) : '';
  const source =
    rows.find((row) => hero?.hero_source_name && row.name === hero.hero_source_name) ??
    rows.find((row) => heroId && row.items.some((item) => itemIdentity(item) === heroId)) ??
    rows.find((row) => !isUpNextRow(row) && row.items.some(hasBackdrop)) ??
    null;
  const seen = new Set<string>();
  const candidates = [hero, ...(source?.items ?? [])]
    .filter((item): item is WebMediaItem => Boolean(item))
    .filter((item) => {
      const id = itemIdentity(item);
      if (seen.has(id)) return false;
      seen.add(id);
      return hasBackdrop(item);
    });
  // The billboard has a Play button: leave out titles that aren't out yet,
  // unless every candidate is pending (a headless Home is worse). Native HeroSelection.
  const available = candidates.filter((item) => !releaseBadge(item)?.upcoming);
  const pool = available.length ? available : candidates;
  const withLogo = pool.filter((item) => logoImage(item));
  const picks = (withLogo.length ? withLogo : pool).slice(0, HERO_LIMIT);
  return { picks, sourceKey: source ? rowKey(source) : null };
}

function matchesTab(item: WebMediaItem, tab: Tab) {
  if (tab === 'movies') return itemKind(item) === 'movie';
  if (tab === 'series') return itemKind(item) === 'series';
  return true;
}

/**
 * The shelves for Home, Movies or Series. Home gets the hero (whose titles are
 * removed from their source row so they don't show twice), Continue Watching,
 * then the server's rows in order. Movies and Series mirror tvOS
 * `FilteredFeedProjectionBuilder`: every row filtered to that kind, "up next"
 * rows dropped, empty rows dropped, and no hero.
 */
export function buildHomePresentation(
  dashboard: DashboardResponse | null,
  rows: CatalogRow[],
  continueWatching: WebMediaItem[],
  tab: Tab = 'home',
  /** Next episodes of shows the viewer finished (upNext.ts). Home only. */
  upNext: WebMediaItem[] = [],
): HomePresentation {
  const filtered = tab === 'movies' || tab === 'series';
  const { picks, sourceKey } = filtered ? { picks: [], sourceKey: null } : pickHero(dashboard, rows);
  const heroIds = new Set(picks.map(itemIdentity));

  const presented: PresentedRow[] = [];
  const resume = continueWatching.filter((item) => matchesTab(item, tab)).slice(0, 15);
  if (resume.length) {
    presented.push({ key: `continue:${tab}`, title: 'Continue Watching', items: resume, style: 'continue' });
  }
  // One Up Next row only (native collapsingExtraUpNextShelves): a server row wins.
  const serverUpNext = rows.some(isUpNextRow);
  if (tab === 'home' && upNext.length && !serverUpNext) {
    presented.push({ key: 'up-next', title: 'Up Next', items: upNext.slice(0, 15), style: 'continue' });
  }
  let keptUpNext = false;
  for (const row of rows) {
    if (filtered && isUpNextRow(row)) continue;
    if (isUpNextRow(row)) {
      if (keptUpNext) continue;
      keptUpNext = true;
    }
    const key = rowKey(row);
    let items = row.items.filter((item) => matchesTab(item, tab));
    if (key === sourceKey) items = items.filter((item) => !heroIds.has(itemIdentity(item)));
    if (!items.length) continue;
    presented.push({ key, title: row.name, items, style: isTopTenRow(row) ? 'topTen' : 'standard' });
  }
  return { hero: picks, rows: presented };
}
