/**
 * The Home "Up Next" row: the next episode of each show you finished an
 * episode of, ported from the native personal shelves (`makePersonalShelves`,
 * `SeriesWatchState`). Built on the client from watch history plus each show's
 * episode list, like tvOS/iOS.
 */
import { useEffect, useRef, useState } from 'react';
import { getDetails, isAbortError } from '../api/accountClient';
import { episodeHasAired, episodePlayItem, flattenEpisodes, itemKind } from '../api/mediaUtils';
import type { SeasonItem, WebMediaItem } from '../api/types';

/** Most shows checked, and most cards shown (native caps the row at 15). */
export const UP_NEXT_SHOWS = 15;
const CONCURRENCY = 3;

function seriesKey(item: WebMediaItem) {
  return String(item.tmdb_id ?? item.imdb_id ?? '');
}

/**
 * Shows with a finished episode in recent history, newest first, leaving out
 * any already in Continue Watching (that row already says what plays next).
 */
export function upNextCandidates(history: WebMediaItem[], continueWatching: WebMediaItem[], limit = UP_NEXT_SHOWS): WebMediaItem[] {
  const resuming = new Set(continueWatching.filter((item) => itemKind(item) === 'series').map(seriesKey));
  const seen = new Set<string>();
  const shows: WebMediaItem[] = [];
  for (const entry of history) {
    if (itemKind(entry) !== 'series' || entry.season == null || entry.episode == null) continue;
    if (entry.action != null && entry.action !== 'complete') continue;
    const key = seriesKey(entry);
    if (!key || seen.has(key) || resuming.has(key)) continue;
    seen.add(key);
    shows.push(entry);
    if (shows.length >= limit) break;
  }
  return shows;
}

/**
 * The card for a show you finished an episode of (native Up Next): the next
 * aired, unwatched episode after the one you finished, or null when there is
 * none yet (caught up). `after` is that finished episode.
 */
export function nextUnwatchedEpisode(
  series: WebMediaItem,
  seasons: SeasonItem[],
  isWatched: (item: WebMediaItem, season?: unknown, episode?: unknown) => boolean,
  now: Date = new Date(),
  after: { season?: unknown; episode?: unknown } = {},
): WebMediaItem | null {
  const afterSeason = Number(after.season ?? 0);
  const afterEpisode = Number(after.episode ?? 0);
  const next = flattenEpisodes(seasons)
    .filter((episode) => Number(episode.season) > 0 && episodeHasAired(episode, now))
    .filter((episode) => Number(episode.season) > afterSeason
      || (Number(episode.season) === afterSeason && Number(episode.episode) > afterEpisode))
    .find((episode) => !isWatched(series, episode.season, episode.episode));
  if (!next) return null;
  return { ...episodePlayItem(series, next), position: 0, up_next: true, episode_title: next.title ?? next.name ?? null };
}

/** Fetches episode lists for the candidates (a few at a time, cached reads) and returns the row's items. */
export function useUpNext(
  history: WebMediaItem[] | undefined,
  continueWatching: WebMediaItem[],
  isWatched: (item: WebMediaItem, season?: unknown, episode?: unknown) => boolean,
  enabled: boolean,
): WebMediaItem[] {
  const [items, setItems] = useState<WebMediaItem[]>([]);
  // Read at fetch time: a watched-state sync must not refetch every show.
  const isWatchedRef = useRef(isWatched);
  isWatchedRef.current = isWatched;
  const candidates = enabled ? upNextCandidates(history ?? [], continueWatching) : [];
  const signature = candidates.map(seriesKey).join(',');

  useEffect(() => {
    if (!signature) {
      setItems([]);
      return undefined;
    }
    const controller = new AbortController();
    const results: Array<WebMediaItem | null> = new Array(candidates.length).fill(null);
    let cursor = 0;
    const worker = async () => {
      while (cursor < candidates.length && !controller.signal.aborted) {
        const index = cursor++;
        const show = candidates[index];
        try {
          const details = await getDetails({ ...show, media_type: 'tv' }, controller.signal);
          results[index] = nextUnwatchedEpisode(
            { ...show, ...details.item, media_type: 'tv' }, details.seasons ?? [], isWatchedRef.current, new Date(),
            { season: show.season, episode: show.episode },
          );
        } catch (err) {
          if (isAbortError(err)) return;
          results[index] = null; // one show failing never empties the row
        }
      }
    };
    Promise.all(Array.from({ length: Math.min(CONCURRENCY, candidates.length) }, worker)).then(() => {
      if (!controller.signal.aborted) setItems(results.filter((item): item is WebMediaItem => Boolean(item)));
    });
    return () => controller.abort();
    // `candidates` is derived from `signature`.
  }, [signature]); // eslint-disable-line react-hooks/exhaustive-deps

  return items;
}
