import { useMemo, useRef } from 'react';
import { Rss } from 'lucide-react';
import type { CatalogRow, DashboardResponse } from '../api/types';
import { useUserState } from '../api/userState';
import { useUpNext } from '../app/upNext';
import { buildHomePresentation, type PresentedRow, type ShelfStyle } from '../app/homePresentation';
import type { Tab } from '../app/navigation';
import { HeroCarousel } from '../components/HeroCarousel';
import { ShelfRow } from '../components/ShelfRow';
import { EmptyState } from '../components/EmptyState';
import styles from './Home.module.css';

export interface HomeProps {
  dashboard: DashboardResponse | null;
  rows: CatalogRow[];
  /** home shows the hero; movies and series are the same feed filtered. */
  tab?: Extract<Tab, 'home' | 'movies' | 'series'>;
}

const VARIANT: Record<ShelfStyle, 'catalog' | 'topTen' | 'continue'> = {
  standard: 'catalog',
  topTen: 'topTen',
  continue: 'continue',
};

function itemSignature(items: PresentedRow['items']) {
  return items.map((item) => `${item.media_type}:${item.tmdb_id ?? item.imdb_id ?? item.id}:${item.season ?? ''}:${item.episode ?? ''}:${item.progress_percent ?? item.progressPercent ?? ''}:${item.position ?? ''}:${item.duration ?? ''}:${item.poster_path ?? ''}`).join('|');
}

/**
 * Background refreshes rebuild every row; reuse the previous items array when a
 * row's content didn't change so its memoized ShelfRow (and every card in it)
 * skips the re-render.
 */
function useStableRows(rows: PresentedRow[]): PresentedRow[] {
  const previous = useRef(new Map<string, { signature: string; items: PresentedRow['items'] }>());
  return useMemo(() => {
    const next = new Map<string, { signature: string; items: PresentedRow['items'] }>();
    const stable = rows.map((row) => {
      const signature = itemSignature(row.items);
      const before = previous.current.get(row.key);
      const items = before && before.signature === signature ? before.items : row.items;
      next.set(row.key, { signature, items });
      return items === row.items ? row : { ...row, items };
    });
    previous.current = next;
    return stable;
  }, [rows]);
}

const EMPTY_COPY = {
  home: { title: 'Nothing here yet', message: 'Curatio will refresh this screen when catalog data is available.' },
  movies: { title: 'No movies yet', message: 'Movie rows appear here once your catalogs include films.' },
  series: { title: 'No series yet', message: 'Series rows appear here once your catalogs include shows.' },
};

/**
 * tvOS Home (and Movies / Series, which are the same feed filtered by kind and
 * without a hero): the inset hero card, Continue Watching, then the server's
 * shelves in order.
 */
export function Home({ dashboard, rows, tab = 'home' }: HomeProps) {
  const { continueWatching, isWatched } = useUserState();
  const upNext = useUpNext(dashboard?.recent_history, continueWatching, isWatched, tab === 'home');
  const presentation = useMemo(
    () => buildHomePresentation(dashboard, rows, continueWatching, tab, upNext),
    [dashboard, rows, continueWatching, tab, upNext],
  );
  const shelves = useStableRows(presentation.rows);
  const hasContent = shelves.length > 0 || presentation.hero.length > 0;

  return (
    <div className={`${styles.home} ${tab !== 'home' ? styles.filtered : ''}`} data-nav-scope={tab}>
      {tab === 'home' ? <HeroCarousel items={presentation.hero} /> : null}
      <div className={styles.rails}>
        {shelves.map((row) => (
          <ShelfRow key={row.key} rowKey={row.key} title={row.title} items={row.items} variant={VARIANT[row.style]} />
        ))}
        {!hasContent && dashboard?.catalog_sync_pending ? (
          <EmptyState
            title="Preparing your rows"
            message="This screen fills in once your personalized catalogs finish syncing."
          />
        ) : null}
        {!hasContent && !dashboard?.catalog_sync_pending ? (
          <EmptyState icon={Rss} title={EMPTY_COPY[tab].title} message={EMPTY_COPY[tab].message} />
        ) : null}
      </div>
    </div>
  );
}
