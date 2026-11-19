import { useEffect, useMemo, useState } from 'react';
import { Search as SearchIcon, SearchX } from 'lucide-react';
import { getFacets, isAbortError, searchDashboard, type SearchSection } from '../api/accountClient';
import type { MediaKind, MediaTag, WebMediaItem } from '../api/types';
import { resultKey } from '../api/mediaUtils';
import { searchWithFallbacks } from '../api/searchQuery';
import { useNav } from '../app/navigation';
import { useDebouncedValue } from '../app/useDashboard';
import { PosterCard } from '../components/PosterCard';
import { ShelfRow } from '../components/ShelfRow';
import { LoadingState } from '../components/LoadingState';
import { EmptyState } from '../components/EmptyState';
import primitives from '../styles/primitives.module.css';
import styles from './Search.module.css';

/** How many facet tags the pill row offers. The full vocabulary lives in
 * Browse; this is the shortlist a user scans without scrolling. */
const PILL_COUNT = 10;

const TYPES: Array<{ value: MediaKind; label: string }> = [
  { value: 'movie', label: 'Movies' },
  { value: 'tv', label: 'Series' },
];

/**
 * tvOS Search, driven by the top bar's search field: a title block, the
 * Movies/Series switch and genre pills, then results as a poster grid (title
 * searches) or ranked shelves (genre searches).
 */
export function Search() {
  const { openBrowse, searchQuery: query } = useNav();
  const [mediaType, setMediaType] = useState<MediaKind>('movie');
  const [retryToken, setRetryToken] = useState(0);
  const [activeTag, setActiveTag] = useState<MediaTag | null>(null);
  const [pills, setPills] = useState<MediaTag[]>([]);
  const [results, setResults] = useState<WebMediaItem[]>([]);
  const [resultType, setResultType] = useState<string | undefined>();
  const [sections, setSections] = useState<SearchSection[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const [error, setError] = useState('');
  const debounced = useDebouncedValue(query, 260);

  // The pill row used to be a hardcoded array of eight genre strings, filtered
  // client-side over whatever 40 rows the search happened to return. It is now
  // the real vocabulary, and the filter is applied in SQL over the library --
  // filtering a page is a different, and wrong, answer from filtering a search.
  useEffect(() => {
    const controller = new AbortController();
    getFacets(mediaType, controller.signal)
      .then((data) => {
        const subgenres = data.facets.find((group) => group.facet === 'subgenre');
        const fallback = data.facets.flatMap((group) => group.tags);
        setPills((subgenres?.tags ?? fallback).slice(0, PILL_COUNT));
      })
      .catch((err) => {
        if (!controller.signal.aborted && !isAbortError(err)) setPills([]);
      });
    return () => controller.abort();
  }, [mediaType]);

  useEffect(() => {
    const controller = new AbortController();
    async function run() {
      const trimmed = debounced.trim();
      setError('');
      if (!trimmed) {
        setResults([]);
        setResultType(undefined);
        setSections([]);
        setIsSearching(false);
        return;
      }
      setIsSearching(true);
      try {
        // Pass the signal so a superseded keystroke actually cancels the in-flight
        // (and server-side) search instead of just ignoring its result.
        // Apple's query cleanup: the year only ranks, articles and
        // conversational filler are fallbacks, and an empty answer retries
        // title prefixes so a typo still finds the title.
        const tagIds = activeTag ? [activeTag.id] : [];
        const { page: data } = await searchWithFallbacks(trimmed, (q) =>
          searchDashboard(q, controller.signal, tagIds),
        );
        if (!controller.signal.aborted) {
          setResults(data.items ?? []);
          setResultType(data.result_type);
          setSections(data.sections ?? []);
        }
      } catch (err) {
        if (controller.signal.aborted || isAbortError(err)) return;
        // Not the previous query's results under this query's heading.
        setResults([]);
        setSections([]);
        setResultType(undefined);
        setError(err instanceof Error ? err.message : 'Search failed.');
      } finally {
        if (!controller.signal.aborted) setIsSearching(false);
      }
    }
    void run();
    return () => controller.abort();
  }, [debounced, activeTag, retryToken]);

  const hasQuery = query.trim().length > 0;
  // Typed but not searched yet (the debounce): still working, not "No results".
  const pending = isSearching || query.trim() !== debounced.trim();
  const emptyMessage = useMemo(
    () =>
      activeTag
        ? `Nothing matching "${query.trim()}" is tagged ${activeTag.name}.`
        : 'Try a different title, person, or genre.',
    [activeTag, query],
  );

  return (
    <div className={styles.page} data-nav-scope="search">
      <header className={styles.header}>
        <h1 className={styles.heading}>{hasQuery ? `Results for “${query.trim()}”` : 'Search'}</h1>
        {/* tvOS: the subtitle only while there is no query. */}
        {hasQuery ? null : <p className={styles.subtitle}>Find movies and shows.</p>}
      </header>

      {/* Kept visible while searching. The filter narrows an active query, so
          hiding it the moment the user typed made it unreachable. */}
      <div className={styles.typeRow} role="group" aria-label="Genres for" data-nav-row="search-types" data-nav-memory="off">
        {TYPES.map((type) => (
          <button
            key={type.value}
            type="button"
            className={`${primitives.pill} ${mediaType === type.value ? primitives.pillActive : ''}`}
            aria-pressed={mediaType === type.value}
            data-nav-item
            data-own-focus
            onClick={() => { setMediaType(type.value); setActiveTag(null); }}
          >
            {type.label}
          </button>
        ))}
      </div>
      {pills.length ? (
        <div className={styles.pills} data-nav-row="search-pills">
          {pills.map((tag) => {
            const active = activeTag?.id === tag.id;
            return (
              <button
                key={tag.id}
                type="button"
                className={`${primitives.pill} ${active ? primitives.pillActive : ''}`}
                aria-pressed={active}
                data-nav-item
                data-own-focus
                onClick={() => {
                  // With no query, a genre pill browses that genre; with a
                  // query, it narrows the results.
                  if (!hasQuery && !active) openBrowse(tag, mediaType);
                  else setActiveTag(active ? null : tag);
                }}
              >
                {tag.name}
              </button>
            );
          })}
        </div>
      ) : null}

      {hasQuery || activeTag ? (
        <div className={styles.resultSummary} aria-live="polite" data-nav-row="search-summary" data-nav-memory="off">
          <span>{pending ? 'Searching…' : `${results.length} result${results.length === 1 ? '' : 's'}${resultType === 'tag' ? ` tagged ${query.trim()}` : ''}`}</span>
          {activeTag ? <button type="button" data-nav-item onClick={() => setActiveTag(null)}>Clear {activeTag.name} filter</button> : null}
        </div>
      ) : null}

      {pending && !results.length && !error ? <LoadingState label="Searching" /> : null}
      {error ? (
        <EmptyState
          tone="error"
          icon={SearchX}
          title="Search Failed"
          message={error}
          actionLabel="Try Again"
          onAction={() => setRetryToken((value) => value + 1)}
        />
      ) : null}
      {!pending && hasQuery && !results.length && !error ? (
        <EmptyState icon={SearchX} title="No Results" message={emptyMessage} />
      ) : null}
      {!hasQuery && !results.length && !pending && !error ? (
        <EmptyState icon={SearchIcon} title="Search Movies &amp; TV Shows" message="Type to search, or pick a genre above." />
      ) : null}

      {/* A genre search ("horror", "action movies") comes back as rows ranked
          for this profile; a title search stays a grid. */}
      {sections.length ? (
        sections.map((section) => (
          <ShelfRow key={section.id} rowKey={`search:${section.id}`} title={section.title} items={section.items} variant="catalog" />
        ))
      ) : results.length ? (
        <div className={styles.grid} data-nav-row="search-results" data-nav-grid>
          {results.map((item, index) => (
            <PosterCard key={resultKey(item, index)} item={item} caption />
          ))}
        </div>
      ) : null}
    </div>
  );
}
