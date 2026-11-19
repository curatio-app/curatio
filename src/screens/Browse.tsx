import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Filter, SearchX, X } from 'lucide-react';
import { browseByTags, getFacets, isAbortError } from '../api/accountClient';
import type { FacetGroup, MediaKind, MediaTag, WebMediaItem } from '../api/types';
import { resultKey } from '../api/mediaUtils';
import { useNavActions } from '../app/navigation';
import type { BrowseFilter } from '../app/navigation';
import { EmptyState } from '../components/EmptyState';
import { LoadingState } from '../components/LoadingState';
import { useDialogFocus } from '../components/useDialogFocus';
import { PosterCard } from '../components/PosterCard';
import primitives from '../styles/primitives.module.css';
import styles from './Browse.module.css';

const PAGE_SIZE = 60;

export function Browse({ filter }: { filter: BrowseFilter }) {
  const { closeBrowse } = useNavActions();
  const [mediaType, setMediaType] = useState<MediaKind>(filter.mediaType);
  const [selected, setSelected] = useState<MediaTag[]>([filter.tag]);
  const [facets, setFacets] = useState<FacetGroup[]>([]);
  const [items, setItems] = useState<WebMediaItem[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [skip, setSkip] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const dialogRef = useDialogFocus<HTMLElement>();

  const selectedIds = useMemo(() => selected.map((tag) => tag.id), [selected]);
  // Stable across renders that don't change the actual selection: the results
  // effect depends on it, and an array identity would refetch on every render.
  const selectionKey = selectedIds.join(',');

  // Switching type invalidates the selection: tag ids are shared, but a tag
  // with no titles of the new type yields an empty grid that looks like a bug.
  const changeType = useCallback(
    (next: MediaKind) => {
      setMediaType(next);
      setSelected((current) => current.slice(0, 1));
      setSkip(0);
    },
    [],
  );

  const [retry, setRetry] = useState(0);
  const pageFailedRef = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    getFacets(mediaType, controller.signal)
      .then((data) => setFacets(data.facets ?? []))
      .catch((err) => {
        if (!controller.signal.aborted && !isAbortError(err)) setFacets([]);
      });
    return () => controller.abort();
  }, [mediaType]);

  useEffect(() => {
    const controller = new AbortController();
    if (!selectedIds.length) {
      setItems([]);
      setHasMore(false);
      setError('');
      setLoading(false);
      return () => controller.abort();
    }
    setLoading(true);
    setError('');
    browseByTags(
      selectedIds,
      { mediaType, mode: 'all', skip, limit: PAGE_SIZE },
      controller.signal,
    )
      .then((data) => {
        if (controller.signal.aborted) return;
        // skip > 0 is an infinite-scroll page, so append; otherwise the
        // selection changed and the previous results are the wrong answer.
        setItems((current) => (skip ? [...current, ...(data.items ?? [])] : data.items ?? []));
        setHasMore(Boolean(data.has_more));
        pageFailedRef.current = false;
      })
      .catch((err) => {
        if (controller.signal.aborted || isAbortError(err)) return;
        pageFailedRef.current = true;
        // A failed new selection must not keep the previous one's posters.
        if (!skip) setItems([]);
        setError(err instanceof Error ? err.message : 'Browse failed.');
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectionKey, mediaType, skip, retry]);

  const toggleTag = useCallback((tag: MediaTag) => {
    setSkip(0);
    setSelected((current) =>
      current.some((entry) => entry.id === tag.id)
        ? current.filter((entry) => entry.id !== tag.id)
        : [...current, tag],
    );
  }, []);

  const title = selected.map((tag) => tag.name).join(' + ') || 'Browse';

  return (
    <section ref={dialogRef} className={styles.overlay} role="dialog" aria-modal="true" aria-label={`Browse ${title}`} data-nav-scope="browse">
      <div className={styles.scroll} data-nav-scroll>
        <header className={styles.head} data-nav-row="browse-head" data-nav-memory="off">
          <button type="button" className={styles.close} onClick={closeBrowse} aria-label="Close browse" data-nav-item data-own-focus>
            <X size={20} />
          </button>
          <h1 className={styles.title}>{title}</h1>
          <button
            type="button"
            className={styles.filterToggle}
            aria-expanded={sidebarOpen}
            data-nav-item
            onClick={() => setSidebarOpen((open) => !open)}
          >
            <Filter size={16} />
            Filters
          </button>
        </header>

        <div className={styles.types} data-nav-row="browse-types" data-nav-memory="off">
          {(['movie', 'tv'] as MediaKind[]).map((kind) => (
            <button
              key={kind}
              type="button"
              className={`${primitives.pill} ${kind === mediaType ? primitives.pillActive : ''}`}
              aria-pressed={kind === mediaType}
              data-nav-item
              data-own-focus
              onClick={() => changeType(kind)}
            >
              {kind === 'movie' ? 'Movies' : 'Series'}
            </button>
          ))}
        </div>

        <div className={styles.layout}>
          <aside className={`${styles.sidebar} ${sidebarOpen ? styles.sidebarOpen : ''}`}>
            {facets.map((group) => (
              <div key={group.facet} className={styles.facet} data-nav-row={`facet:${group.facet}`}>
                <h2 className={styles.facetName}>{group.facet.replace(/_/g, ' ')}</h2>
                <div className={styles.facetTags}>
                  {group.tags.map((tag) => {
                    const active = selectedIds.includes(tag.id);
                    return (
                      <button
                        key={tag.id}
                        type="button"
                        className={`${primitives.pill} ${active ? primitives.pillActive : ''}`}
                        aria-pressed={active}
                        data-nav-item
                        data-own-focus
                        onClick={() => toggleTag(tag)}
                      >
                        {tag.name}
                        <span className={styles.count}>{tag.count}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
          </aside>

          <div className={styles.results}>
            {error ? (
              <EmptyState tone="error" icon={SearchX} title="Browse failed" message={error} actionLabel="Try again" onAction={() => setRetry((n) => n + 1)} />
            ) : null}
            {!error && !items.length && !loading ? (
              <EmptyState
                icon={SearchX}
                title={selected.length ? 'Nothing matches' : 'Pick a tag to browse'}
                message={selected.length ? 'Every selected tag has to be present. Try removing one.' : 'Choose one or more tags from Filters.'}
              />
            ) : null}
            {items.length ? (
              <div className={styles.grid} data-nav-row="browse-results" data-nav-grid>
                {items.map((item, index) => (
                  <PosterCard key={resultKey(item, index)} item={item} caption />
                ))}
              </div>
            ) : null}
            {loading ? <LoadingState label="Loading" /> : null}
            {hasMore && !error && items.length ? (
              <div data-nav-row="browse-more">
              <button
                type="button"
                className={primitives.ghostButton}
                data-nav-item
                data-own-focus
                // After a failed page, ask for that page again rather than the one after it.
                // Stays mounted while loading so keyboard focus is not dropped.
                aria-disabled={loading}
                onClick={() => {
                  if (loading) return;
                  if (pageFailedRef.current) setRetry((n) => n + 1);
                  else setSkip((current) => current + PAGE_SIZE);
                }}
              >
                {loading ? 'Loading…' : 'Load more'}
              </button>
              </div>
            ) : null}
          </div>
        </div>
      </div>
    </section>
  );
}
