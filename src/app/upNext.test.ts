import { describe, expect, it } from 'vitest';
import type { CatalogRow, SeasonItem, WebMediaItem } from '../api/types';
import { buildHomePresentation, pickHero } from './homePresentation';
import { nextUnwatchedEpisode, upNextCandidates } from './upNext';

const show = (tmdb_id: number, season = 1, episode = 1, action = 'complete'): WebMediaItem =>
  ({ tmdb_id, media_type: 'tv', title: `Show ${tmdb_id}`, season, episode, action });

describe('upNextCandidates', () => {
  it('takes each show once, newest first, skipping shows already in Continue Watching and non-completions', () => {
    const history = [show(1, 1, 3), show(2), show(1, 1, 2), show(3, 1, 1, 'imported'), { tmdb_id: 9, media_type: 'movie', title: 'M' } as WebMediaItem, show(4)];
    const resuming = [show(2, 1, 2)];
    expect(upNextCandidates(history, resuming).map((item) => item.tmdb_id)).toEqual([1, 4]);
  });
});

describe('nextUnwatchedEpisode (native SeriesWatchState)', () => {
  const now = new Date(2026, 10, 10, 12);
  const seasons: SeasonItem[] = [
    { season: 1, episodes: [{ season: 1, episode: 1, air_date: '2026-01-01' }, { season: 1, episode: 2, air_date: '2026-01-08' }] },
    { season: 2, episodes: [{ season: 2, episode: 1, air_date: '2026-10-01' }, { season: 2, episode: 2, air_date: '2026-12-01' }] },
    { season: 0, episodes: [{ season: 0, episode: 1, air_date: '2025-01-01' }] },
  ];
  const watchedThrough = (s: number, e: number) => (_: WebMediaItem, season?: unknown, episode?: unknown) =>
    Number(season) < s || (Number(season) === s && Number(episode) <= e);

  const never = () => false;
  it('rolls over a season finale into the next season premiere', () => {
    expect(nextUnwatchedEpisode(show(1), seasons, watchedThrough(1, 2), now, { season: 1, episode: 2 })).toMatchObject({ season: 2, episode: 1, up_next: true, position: 0 });
  });
  it('continues after the finished episode, not from the first unwatched one', () => {
    expect(nextUnwatchedEpisode(show(1), seasons, never, now, { season: 1, episode: 2 })).toMatchObject({ season: 2, episode: 1 });
  });
  it('skips specials and episodes that have not aired, and is null when caught up', () => {
    expect(nextUnwatchedEpisode(show(1), seasons, watchedThrough(2, 1), now, { season: 2, episode: 1 })).toBeNull();
  });
});

describe('Home presentation', () => {
  const row = (name: string, items: WebMediaItem[], id?: string): CatalogRow => ({ name, id, items } as CatalogRow);
  const art = (tmdb_id: number, extra: Partial<WebMediaItem> = {}): WebMediaItem =>
    ({ tmdb_id, media_type: 'movie', title: `T${tmdb_id}`, backdrop_path: `/b${tmdb_id}.jpg`, poster_path: `/p${tmdb_id}.jpg`, logo: `https://img/l${tmdb_id}.png`, ...extra });

  it('puts Up Next after Continue Watching, and lets a server Up Next row win', () => {
    const upNext = [{ ...show(5, 2, 1), up_next: true }];
    const rows = [row('Trending', [art(1), { tmdb_id: 2, media_type: 'movie', title: 'No art' }])];
    const home = buildHomePresentation(null, rows, [show(7, 1, 1)], 'home', upNext);
    expect(home.rows.map((entry) => entry.title)).toEqual(['Continue Watching', 'Up Next', 'Trending']);
    const withServer = buildHomePresentation(null, [...rows, row('Up Next', [show(8)], 'personal-up-next'), row('Up Next 2', [show(9)], 'up-next-2')], [], 'home', upNext);
    expect(withServer.rows.map((entry) => entry.title)).toEqual(['Trending', 'Up Next']);
  });

  it('keeps unreleased titles out of the hero unless nothing else is out', () => {
    const future = new Date(Date.now() + 9 * 86_400_000).toISOString().slice(0, 10);
    const rows = [row('Trending', [art(1, { comingSoonDate: future }), art(2)])];
    expect(pickHero({ hero: art(1, { comingSoonDate: future, hero_source_name: 'Trending' }) } as never, rows).picks.map((item) => item.tmdb_id)).toEqual([2]);
    const allPending = [row('Coming Soon', [art(3, { comingSoonDate: future })])];
    expect(pickHero(null, allPending).picks.map((item) => item.tmdb_id)).toEqual([3]);
  });
});
