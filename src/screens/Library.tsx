import { useEffect, useMemo, useRef, useState } from 'react';
import { Bookmark, Server } from 'lucide-react';
import type { WebMediaItem } from '../api/types';
import { displayTitle, hasServerLibrary, itemKind } from '../api/mediaUtils';
import { getServerLibrary, isAbortError, type ServerLibraryFilter, type ServerLibrarySort } from '../api/accountClient';
import { useUserState } from '../api/userState';
import { PosterCard } from '../components/PosterCard';
import { ShelfRow } from '../components/ShelfRow';
import { EmptyState } from '../components/EmptyState';
import primitives from '../styles/primitives.module.css';
import styles from './Library.module.css';

const FILTERS = ['All', 'Movies', 'Series'] as const;
type Filter = (typeof FILTERS)[number];

const SORTS = {
  title: 'Title (A–Z)',
  recent: 'Recently Saved',
  year: 'Release Year — Newest',
} as const;
type Sort = keyof typeof SORTS;

function matchesFilter(item: WebMediaItem, filter: Filter) {
  if (filter === 'All') return true;
  const kind = itemKind(item);
  return filter === 'Movies' ? kind === 'movie' : kind === 'series';
}

const byTitle = (a: WebMediaItem, b: WebMediaItem) => displayTitle(a).localeCompare(displayTitle(b), undefined, { sensitivity: 'base' });

/** tvOS LibraryView sorting: saved order (newest first) as served; year newest first, title tiebreak, undated last. */
export function sortItems(items: WebMediaItem[], sort: Sort) {
  if (sort === 'recent') return items;
  const copy = [...items];
  if (sort === 'title') copy.sort(byTitle);
  else {
    const year = (item: WebMediaItem) => Number(item.year) || 0;
    copy.sort((a, b) => (year(a) ? 0 : 1) - (year(b) ? 0 : 1) || year(b) - year(a) || byTitle(a, b));
  }
  return copy;
}

const SOURCES = { servers: 'On Your Servers', watchlist: 'Watchlist' } as const;
type Source = keyof typeof SOURCES;

const SERVER_SORTS: Record<ServerLibrarySort, string> = {
  added: 'Recently Added',
  title: 'Title (A–Z)',
  year: 'Release Year — Newest',
  popular: 'Popular',
};

const SERVER_FILTERS: Record<'all' | ServerLibraryFilter, string> = {
  all: 'All Titles',
  unwatched: 'Unwatched',
  '4k': '4K',
};

/** Titles on the connected Plex/Jellyfin servers, sorted and paged by the server. */
function useServerLibrary(enabled: boolean, filter: Filter, sort: ServerLibrarySort, show: 'all' | ServerLibraryFilter) {
  const [items, setItems] = useState<WebMediaItem[]>([]);
  const [total, setTotal] = useState(0);
  const [next, setNext] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const type = filter === 'Movies' ? 'movie' : filter === 'Series' ? 'tv' : undefined;

  const load = (offset: number) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    setFailed(false);
    getServerLibrary({ type, sort, filter: show === 'all' ? null : show, offset }, controller.signal)
      .then((page) => {
        setItems((current) => (offset === 0 ? page.items : [...current, ...page.items]));
        setTotal(page.total);
        setNext(page.next_offset);
      })
      .catch((err) => {
        if (isAbortError(err)) return;
        if (offset === 0) setItems([]);
        setFailed(true);
      })
      .finally(() => {
        if (abortRef.current === controller) setLoading(false);
      });
  };

  useEffect(() => {
    if (!enabled) return undefined;
    load(0);
    return () => abortRef.current?.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, type, sort, show]);

  return { items, total, next, loading, failed, loadMore: () => (next != null ? load(next) : undefined) };
}

/**
 * tvOS My Library: "On Your Servers" (what Plex/Jellyfin own) or the saved
 * Watchlist, with All / Movies / Series filters and a sort; Continue Watching
 * leads the Watchlist.
 */
export function Library() {
  const [source, setSource] = useState<Source>(() => (hasServerLibrary() ? 'servers' : 'watchlist'));
  const [filter, setFilter] = useState<Filter>('All');
  const [sort, setSort] = useState<Sort>('title');
  const [serverSort, setServerSort] = useState<ServerLibrarySort>('added');
  const [serverShow, setServerShow] = useState<'all' | ServerLibraryFilter>('all');
  const servers = useServerLibrary(source === 'servers', filter, serverSort, serverShow);
  // Shared state, so a remove/mark-watched from any menu shows here at once.
  const { savedItems: library, continueWatching } = useUserState();

  const saved = useMemo(
    () => sortItems(library.filter((item) => matchesFilter(item, filter)), sort),
    [filter, library, sort],
  );
  const resume = useMemo(() => continueWatching.filter((item) => matchesFilter(item, filter)), [filter, continueWatching]);
  const isEmpty = !library.length && !continueWatching.length;
  const onServers = source === 'servers';
  const serverNoun = filter === 'Movies' ? 'movie' : filter === 'Series' ? 'show' : 'title';

  return (
    <div className={styles.page} data-nav-scope="library">
      <header className={styles.head}>
        <h1 className={styles.heading}>My Library</h1>
        <p className={styles.count}>
          {onServers
            ? `${servers.total} ${serverNoun}${servers.total === 1 ? '' : 's'} on your servers`
            : <>{filter !== 'All' && library.length ? `${saved.length} of ` : ''}{library.length} saved title{library.length === 1 ? '' : 's'}</>}
        </p>
        <div className={styles.controls} data-nav-row="library-source" data-nav-memory="off">
          {(Object.keys(SOURCES) as Source[]).map((entry) => (
            <button
              key={entry}
              type="button"
              className={`${primitives.pill} ${source === entry ? primitives.pillActive : ''}`}
              aria-pressed={source === entry}
              data-nav-item
              data-own-focus
              onClick={() => setSource(entry)}
            >
              {SOURCES[entry]}
            </button>
          ))}
        </div>
        {onServers ? <div className={styles.controls} data-nav-row="library-controls" data-nav-memory="off">
          {FILTERS.map((entry) => (
            <button
              key={entry}
              type="button"
              className={`${primitives.pill} ${filter === entry ? primitives.pillActive : ''}`}
              aria-pressed={filter === entry}
              data-nav-item
              data-own-focus
              onClick={() => setFilter(entry)}
            >
              {entry}
            </button>
          ))}
          <label className={styles.sort}>
            <span>Sort:</span>
            <select value={serverSort} onChange={(event) => setServerSort(event.target.value as ServerLibrarySort)} aria-label="Sort server titles" data-nav-item>
              {(Object.keys(SERVER_SORTS) as ServerLibrarySort[]).map((key) => (
                <option key={key} value={key}>{SERVER_SORTS[key]}</option>
              ))}
            </select>
          </label>
          <label className={styles.sort}>
            <span>Show:</span>
            <select value={serverShow} onChange={(event) => setServerShow(event.target.value as 'all' | ServerLibraryFilter)} aria-label="Filter server titles" data-nav-item>
              {(Object.keys(SERVER_FILTERS) as Array<'all' | ServerLibraryFilter>).map((key) => (
                <option key={key} value={key}>{SERVER_FILTERS[key]}</option>
              ))}
            </select>
          </label>
        </div> : null}
        {/* Nothing saved: no filters or sort to offer (tvOS). */}
        {!onServers && library.length ? <div className={styles.controls} data-nav-row="library-controls" data-nav-memory="off">
          {FILTERS.map((entry) => (
            <button
              key={entry}
              type="button"
              className={`${primitives.pill} ${filter === entry ? primitives.pillActive : ''}`}
              aria-pressed={filter === entry}
              data-nav-item
              data-own-focus
              onClick={() => setFilter(entry)}
            >
              {entry}
            </button>
          ))}
          <label className={styles.sort}>
            <span>Sort:</span>
            <select value={sort} onChange={(event) => setSort(event.target.value as Sort)} aria-label="Sort saved titles" data-nav-item>
              {(Object.keys(SORTS) as Sort[]).map((key) => (
                <option key={key} value={key}>{SORTS[key]}</option>
              ))}
            </select>
          </label>
        </div> : null}
      </header>

      {onServers ? (
        servers.items.length ? (
          <section className={styles.section} aria-label="Titles on your servers">
            <div className={styles.grid} data-nav-row="library-servers" data-nav-grid>
              {servers.items.map((item) => (
                <PosterCard key={`${item.media_type}:${item.tmdb_id}:${displayTitle(item)}`} item={item} caption />
              ))}
            </div>
            {servers.next != null ? (
              <div className={styles.controls} data-nav-row="library-more" data-nav-memory="off">
                <button
                  type="button"
                  className={primitives.pill}
                  data-nav-item
                  data-own-focus
                  disabled={servers.loading}
                  onClick={servers.loadMore}
                >
                  {servers.loading ? 'Loading…' : 'Show More'}
                </button>
              </div>
            ) : null}
          </section>
        ) : servers.loading ? null : (
          <EmptyState
            icon={Server}
            title={servers.failed ? 'Library Unavailable' : serverShow !== 'all' ? 'Nothing Matches' : 'Nothing Here Yet'}
            message={servers.failed
              ? "Couldn't load your server library. Try again in a moment."
              : serverShow !== 'all'
                ? 'Try a different filter.'
                : 'Titles on your Plex and Jellyfin servers appear here once your server syncs them.'}
          />
        )
      ) : null}

      {!onServers && resume.length ? (
        <ShelfRow title="Continue Watching" rowKey="library-continue" items={resume} variant="continue" />
      ) : null}

      {!onServers && saved.length ? (
        <section className={styles.section} aria-label="Saved titles">
          <h2 className={styles.sectionTitle}>Saved Titles</h2>
          <div className={styles.grid} data-nav-row="library-saved" data-nav-grid>
            {saved.map((item) => (
              <PosterCard key={`${item.media_type}:${item.tmdb_id}:${displayTitle(item)}`} item={item} caption />
            ))}
          </div>
        </section>
      ) : null}

      {!onServers && (isEmpty || (!saved.length && !resume.length)) ? (
        <EmptyState
          icon={Bookmark}
          title={isEmpty || filter === 'All' ? 'Library Is Empty' : `No Saved ${filter}`}
          message={isEmpty || filter === 'All'
            ? 'Save a movie or series to find it here.'
            : `Choose All or ${filter === 'Movies' ? 'Series' : 'Movies'} to keep browsing.`}
        />
      ) : null}
    </div>
  );
}
