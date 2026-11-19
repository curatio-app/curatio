import { describe, expect, it } from 'vitest';
import { asText, digitalReleaseDisplay, episodeComingText, episodeHasAired, isPlayable, releaseBadge, resultKey, seriesReleaseBadge, sourceRowModel } from './mediaUtils';
import { makeSource } from './testFixtures';
import type { WebMediaItem } from './types';

describe('asText', () => {
  it('returns strings unchanged', () => {
    expect(asText('hello')).toBe('hello');
    expect(asText('')).toBe('');
  });

  it('returns empty string for non-strings (guards against "[object Object]")', () => {
    expect(asText({ toString: () => 'x' })).toBe('');
    expect(asText(['a', 'b'])).toBe('');
    expect(asText(null)).toBe('');
    expect(asText(undefined)).toBe('');
    expect(asText(42)).toBe('');
  });
});

describe('resultKey', () => {
  it('produces distinct keys for two IMDb-keyed (tmdb_id: null) items with the same title', () => {
    const a = { media_type: 'movie', tmdb_id: null, imdb_id: 'tt0041386', title: 'The Fountainhead' } as WebMediaItem;
    const b = { media_type: 'movie', tmdb_id: null, imdb_id: 'tt9999999', title: 'The Fountainhead' } as WebMediaItem;
    expect(resultKey(a, 0)).not.toBe(resultKey(b, 1));
  });

  it('stays unique even when tmdb_id and imdb_id are both absent (index tiebreaker)', () => {
    const item = { media_type: 'movie', tmdb_id: null, title: 'Untitled' } as WebMediaItem;
    expect(resultKey(item, 0)).not.toBe(resultKey(item, 1));
  });

  it('uses the numeric tmdb_id when present', () => {
    const item = { media_type: 'movie', tmdb_id: 27205, title: 'Inception' } as WebMediaItem;
    expect(resultKey(item, 0)).toContain('27205');
  });
});

describe('isPlayable (availability)', () => {
  it('is true only when the server says playable', () => {
    expect(isPlayable({ availability: { playable: true, in_library: true, sources: [] } })).toBe(true);
    expect(isPlayable({ availability: { playable: false, in_library: false, sources: [] } })).toBe(false);
    // An older cached payload without availability is not playable.
    expect(isPlayable({ title: 'Old payload' })).toBe(false);
    expect(isPlayable(null)).toBe(false);
  });
});

describe('digitalReleaseDisplay (native DigitalReleaseFormatter)', () => {
  // Fixed "today" so the countdown is deterministic regardless of the runner's clock/timezone.
  const NOW = new Date('2024-06-01T12:00:00Z');
  const chip = (raw?: string | null) => digitalReleaseDisplay(raw, NOW)?.chipText ?? '';

  it('shows a recent release as a short date and an old one not at all (native DigitalReleaseFormatter)', () => {
    expect(chip('2024-05-20')).toBe('Digital · May 20');
    expect(chip('2020-03-10')).toBe('');
  });

  it('says "Digital today" on the release day', () => {
    expect(chip('2024-06-01')).toBe('Digital today');
  });

  it('says "Digital tomorrow" the day before', () => {
    expect(chip('2024-06-02')).toBe('Digital tomorrow');
  });

  it('counts down in days for the near term', () => {
    expect(chip('2024-06-10')).toBe('Digital in 9 days');
    expect(chip('2024-07-01')).toBe('Digital in 30 days');
  });

  it('shows the full date for upcoming dates beyond 30 days', () => {
    expect(chip('2024-08-15')).toBe('Digital · Aug 15, 2024');
  });

  it('tolerates a full ISO-8601 string with a time suffix', () => {
    expect(chip('2024-06-10T00:00:00.000Z')).toBe('Digital in 9 days');
  });

  it('renders nothing for missing or unparseable dates', () => {
    expect(chip(undefined)).toBe('');
    expect(chip(null)).toBe('');
    expect(chip('not-a-date')).toBe('');
    expect(chip('2024-13-40')).toBe('');
  });
});

describe('sourceRowModel (server display strings, verbatim)', () => {
  it('renders label, detail, provider_label and badges without parsing them', () => {
    const row = sourceRowModel(makeSource({
      label: '4K · HEVC · Dolby Vision · 5.1',
      detail: 'The.Movie.2019.2160p.mkv',
      provider_label: 'Plex',
      badges: ['Original', 'HDR'],
      resolution: '2160p',
      size_bytes: 42_000_000_000,
    }));
    expect(row).toEqual({
      resolution: '4K',
      name: '4K · HEVC · Dolby Vision · 5.1',
      provider: 'Plex',
      detail: 'The.Movie.2019.2160p.mkv',
      badges: ['Original', 'HDR'],
      size: '42.0 GB',
      menuSummary: '4K · HEVC · Dolby Vision · 5.1',
      menuDetails: 'Plex · 42.0 GB',
    });
  });

  it('falls back to the provider label when the server sent no label', () => {
    const row = sourceRowModel(makeSource({ label: '', provider_label: 'Jellyfin', resolution: '1080p' }));
    expect(row.name).toBe('Jellyfin');
    expect(row.resolution).toBe('1080p');
    expect(row.detail).toBe('');
    expect(row.size).toBe('');
    expect(row.menuDetails).toBe('');
  });

  it('a source with nothing known still gets a name', () => {
    expect(sourceRowModel(makeSource({ label: '', provider_label: '' })).name).toBe('Source');
  });
});

describe('releaseBadge (native ComingSoonFormatter)', () => {
  const now = new Date(2026, 10, 3, 12); // Tue Nov 3 2026, local noon
  it('reads upcoming releases by weekday within a week, else by date', () => {
    expect(releaseBadge({ comingSoonDate: '2026-11-04' }, now)).toEqual({ badgeText: 'Tomorrow', chipText: 'Coming tomorrow', upcoming: true, kind: 'title' });
    expect(releaseBadge({ comingSoonDate: '2026-11-06' }, now)?.chipText).toBe('Coming Friday');
    expect(releaseBadge({ comingSoonDate: '2026-12-25' }, now)?.badgeText).toBe('December 25');
    expect(releaseBadge({ comingSoonDate: '2027-01-02' }, now)?.badgeText).toBe('Jan 2, 2027');
  });
  it('reads a recent release as New for 14 days', () => {
    expect(releaseBadge({ digitalReleaseInfo: '2026-11-03' }, now)).toEqual({ badgeText: 'New', chipText: 'Just released', upcoming: false, kind: 'title' });
    expect(releaseBadge({ digitalReleaseInfo: '2026-10-25' }, now)?.badgeText).toBe('New');
    expect(releaseBadge({ digitalReleaseInfo: '2026-10-01' }, now)).toBeNull();
  });
});

describe('seriesReleaseBadge (native ComingSoonFormatter.seriesBadge)', () => {
  const now = new Date(2026, 10, 3, 12); // Tue Nov 3 2026
  it('says "New episode Friday" for a running show and "Coming Friday" before the premiere', () => {
    const running = [{ air_date: '2026-10-01' }, { air_date: '2026-11-06' }, { air_date: '2026-11-13' }];
    expect(seriesReleaseBadge(running, null, now)).toMatchObject({ chipText: 'New episode Friday', upcoming: true, kind: 'episode' });
    const unpremiered = [{ air_date: '2026-11-06' }];
    expect(seriesReleaseBadge(unpremiered, null, now)).toMatchObject({ chipText: 'Coming Friday', kind: 'title' });
  });
  it('marks an episode that just aired, then falls back to the cached date', () => {
    expect(seriesReleaseBadge([{ air_date: '2026-11-03' }], null, now)?.chipText).toBe('New episode today');
    expect(seriesReleaseBadge([{ air_date: '2025-01-01' }], null, now)).toBeNull();
    expect(seriesReleaseBadge([], '2026-11-06', now)?.chipText).toBe('Coming Friday');
  });
  it('treats an episode with no date as aired (fail-open)', () => {
    expect(episodeHasAired({ air_date: '' }, now)).toBe(true);
    expect(episodeHasAired({ air_date: '2026-11-03' }, now)).toBe(true);
    expect(episodeHasAired({ air_date: '2026-11-04' }, now)).toBe(false);
    expect(episodeComingText({ air_date: '2026-11-06' }, now)).toBe('Friday');
  });
});

describe('saved positions belong to a profile', () => {
  it('two profiles never share a key, and the default key is unchanged', async () => {
    const { playbackProgressKey, setProgressProfile } = await import('./mediaUtils');
    const film = { tmdb_id: 5, media_type: 'movie', title: 'Film' } as never;
    expect(playbackProgressKey(film)).toBe('curatio-playback:movie:5:0:0');
    setProgressProfile(2);
    const second = playbackProgressKey(film);
    setProgressProfile(3);
    expect(playbackProgressKey(film)).not.toBe(second);
    setProgressProfile(null);
    expect(playbackProgressKey(film)).toBe('curatio-playback:movie:5:0:0');
  });
});
