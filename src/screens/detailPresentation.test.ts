import { describe, expect, it } from 'vitest';
import type { ReleaseBadge } from '../api/mediaUtils';
import { detailInfoRows, episodeMarker, heroBadge, orderedScores, ratedValue } from './detailPresentation';

const coming: ReleaseBadge = { badgeText: 'Friday', chipText: 'Coming Friday', upcoming: true, kind: 'title' };
const justOut: ReleaseBadge = { badgeText: 'New', chipText: 'Just released', upcoming: false, kind: 'title' };

describe('heroBadge (native DetailsView.heroBadge)', () => {
  it('ranks a pending release above Continue Watching / Up Next, and Just released below them', () => {
    expect(heroBadge(coming, 'resume')).toEqual({ text: 'Coming Friday', prominent: true });
    expect(heroBadge(justOut, 'resume')).toEqual({ text: 'Continue Watching', prominent: false });
    expect(heroBadge(null, 'next')).toEqual({ text: 'Up Next', prominent: false });
    expect(heroBadge(justOut, 'play')).toEqual({ text: 'Just released', prominent: true });
    expect(heroBadge(null, 'start')).toBeNull();
  });
});

describe('episodeMarker (native EpisodeShelfPresentationBuilder)', () => {
  const base = { comingText: null, watched: false, fraction: 0, isTarget: false, targetKind: 'play' as const };
  it('marks unaired episodes first', () => {
    expect(episodeMarker({ ...base, comingText: 'Friday', watched: true })).toEqual({ kind: 'comingSoon', label: 'Coming Friday' });
  });
  it('then the episode’s own progress', () => {
    expect(episodeMarker({ ...base, watched: true, isTarget: true, targetKind: 'next' })).toEqual({ kind: 'watched' });
    expect(episodeMarker({ ...base, fraction: 0.4 })).toMatchObject({ kind: 'inProgress', label: 'In Progress' });
  });
  it('then its role as the Play target', () => {
    expect(episodeMarker({ ...base, isTarget: true, targetKind: 'next' })).toMatchObject({ label: 'Next Up' });
    expect(episodeMarker({ ...base, isTarget: true, targetKind: 'start' })).toMatchObject({ label: 'Start Here' });
    expect(episodeMarker({ ...base, isTarget: true, targetKind: 'restart' })).toMatchObject({ label: 'Start Here' });
    expect(episodeMarker({ ...base, isTarget: false, targetKind: 'next' })).toEqual({ kind: 'none' });
  });
});

describe('orderedScores (native displayOrder)', () => {
  it('uses the fixed product order and puts unknown sources last', () => {
    const scores = ['letterboxd', 'imdb', 'mystery', 'tomatoes', 'metacriticuser', 'popcorn', 'metacritic', 'trakt', 'tmdb', 'rogerebert']
      .map((source) => ({ source, label: source, normalized: 5 }));
    expect(orderedScores(scores).map((score) => score.source)).toEqual([
      'tomatoes', 'popcorn', 'metacritic', 'metacriticuser', 'rogerebert', 'imdb', 'tmdb', 'trakt', 'letterboxd', 'mystery',
    ]);
  });
});

describe('detailInfoRows (native movieInfoDetails / seriesInfoDetails)', () => {
  it('appends the advisory age to the certification', () => {
    expect(ratedValue('R', 17)).toBe('R (17+)');
    expect(ratedValue('PG', null)).toBe('PG');
    expect(ratedValue('', 17)).toBe('');
  });

  it('lists movie rows in native order with Available instead of Digital while upcoming', () => {
    const rows = detailInfoRows({
      isSeries: false, release: 2026, certification: 'R', ageRating: 17, coming,
      digitalReleaseInfo: '2026-11-06', runtime: '2h 4m', genres: ['Drama', 'Crime', 'Thriller', 'War'],
      directors: ['A', 'B', 'C'], themes: ['heist', 'slow burn'], language: 'English',
    });
    expect(rows).toEqual([
      ['Release', '2026'], ['Rated', 'R (17+)'], ['Available', 'Friday'], ['Runtime', '2h 4m'],
      ['Genres', 'Drama • Crime • Thriller'], ['Director', 'A, B'], ['Language', 'English'], ['Themes', 'heist • slow burn'],
    ]);
  });

  it('shows Digital for a recent movie and Next Episode for a running series', () => {
    const now = new Date('2026-11-10T12:00:00Z');
    expect(detailInfoRows({ isSeries: false, digitalReleaseInfo: '2026-11-01', now })).toEqual([['Digital', 'Nov 1']]);
    const episode: ReleaseBadge = { ...coming, chipText: 'New episode Friday', kind: 'episode' };
    expect(detailInfoRows({ isSeries: true, status: 'Returning Series', coming: episode, creators: [], directors: ['D'] })).toEqual([
      ['Status', 'Returning Series'], ['Next Episode', 'Friday'], ['Created By', 'D'],
    ]);
  });
});
