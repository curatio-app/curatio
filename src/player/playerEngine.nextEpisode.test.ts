import { describe, expect, it } from 'vitest';

(globalThis as unknown as { window?: unknown }).window ??= { location: { origin: 'http://localhost' } };

// eslint-disable-next-line import/first
import {
  airedNextEpisode,
  markersFromSegments,
  nextEpisodeAcceptFeedback,
  nextEpisodePromptStart,
  shouldShowNextEpisode,
  UP_NEXT_COUNTDOWN_SECONDS,
} from './playerEngine';

describe('shouldShowNextEpisode', () => {
  it('never shows when there is no next episode', () => {
    expect(
      shouldShowNextEpisode({
        hasNextEpisode: false,
        current: 1780,
        creditsStart: 1700,
        mediaEnded: true,
      }),
    ).toBe(false);
  });

  it('keeps a fused marker authoritative until its onset', () => {
    expect(
      shouldShowNextEpisode({
        hasNextEpisode: true,
        current: 1770,
        creditsStart: 1785,
        mediaEnded: false,
      }),
    ).toBe(false);
    expect(
      shouldShowNextEpisode({
        hasNextEpisode: true,
        current: 1785,
        creditsStart: 1785,
        mediaEnded: false,
      }),
    ).toBe(true);
  });

  it('never guesses from an unmeasured duration when no marker exists', () => {
    expect(
      shouldShowNextEpisode({
        hasNextEpisode: true,
        current: 1_800,
        creditsStart: undefined,
        mediaEnded: false,
      }),
    ).toBe(false);
  });

  it('falls back only after the media ended event', () => {
    expect(
      shouldShowNextEpisode({
        hasNextEpisode: true,
        current: 1_800,
        creditsStart: undefined,
        mediaEnded: true,
      }),
    ).toBe(true);
  });
});

describe('Up Next rules (PlaybackPromptRules parity)', () => {
  it('counts down for 30 s', () => {
    expect(UP_NEXT_COUNTDOWN_SECONDS).toBe(30);
  });

  it('prefers the next-episode marker, then credits, then 30 s before a measured end', () => {
    expect(nextEpisodePromptStart({ nextEpisodeStart: 1750, creditsStart: 1700, duration: 1800, durationReliable: true })).toBe(1750);
    expect(nextEpisodePromptStart({ creditsStart: 1700, duration: 1800, durationReliable: true })).toBe(1700);
    expect(nextEpisodePromptStart({ duration: 1800, durationReliable: true })).toBe(1770);
    expect(nextEpisodePromptStart({ duration: 1800, durationReliable: false })).toBeNull();
    expect(nextEpisodePromptStart({ duration: 20, durationReliable: true })).toBeNull();
  });

  it('shows from the 30 s fallback only with a measured duration', () => {
    const base = { hasNextEpisode: true, duration: 1800, mediaEnded: false };
    expect(shouldShowNextEpisode({ ...base, current: 1769, durationReliable: true })).toBe(false);
    expect(shouldShowNextEpisode({ ...base, current: 1770, durationReliable: true })).toBe(true);
    expect(shouldShowNextEpisode({ ...base, current: 1790, durationReliable: false })).toBe(false);
  });

  it('a next-episode marker wins over an earlier credits marker', () => {
    const base = { hasNextEpisode: true, creditsStart: 1600, nextEpisodeStart: 1750, mediaEnded: false };
    expect(shouldShowNextEpisode({ ...base, current: 1700 })).toBe(false);
    expect(shouldShowNextEpisode({ ...base, current: 1750 })).toBe(true);
  });

  it('confirms the credits marker only when it triggered the prompt', () => {
    expect(nextEpisodeAcceptFeedback({ creditsStart: 1700 }, 1710)).toBe('credits');
    expect(nextEpisodeAcceptFeedback({ creditsStart: 1700 }, 1690)).toBeNull();
    expect(nextEpisodeAcceptFeedback({}, 1790)).toBeNull();
    expect(nextEpisodeAcceptFeedback({ creditsStart: 1700, nextEpisodeStart: 1750 }, 1760)).toBeNull();
  });

  it('never offers an unaired episode', () => {
    const now = new Date('2026-11-10T12:00:00Z');
    expect(airedNextEpisode({ episode: 2, air_date: '2026-12-01' }, now)).toBeNull();
    expect(airedNextEpisode({ episode: 2, air_date: '2026-11-01' }, now)).toEqual({ episode: 2, air_date: '2026-11-01' });
    expect(airedNextEpisode({ episode: 2 }, now)).toEqual({ episode: 2 });
    expect(airedNextEpisode(null, now)).toBeNull();
  });
});

describe('markersFromSegments', () => {
  const segments = [
    { type: 'intro', start: 10, end: 70 },
    { type: 'recap', start: 0, end: 9 },
    { type: 'credits', start_sec: 1700, end_sec: 1790 },
    { type: 'next_episode', start: 1750 },
  ];

  it('reads intro, credits and a next-episode point marker', () => {
    expect(markersFromSegments(segments)).toEqual({
      introStart: 10, introEnd: 70, creditsStart: 1700, creditsEnd: 1790, nextEpisodeStart: 1750,
    });
  });

  it('parses recap only when the recap flag is on', () => {
    expect(markersFromSegments(segments, { recap: true })).toMatchObject({ recapStart: 0, recapEnd: 9 });
    expect(markersFromSegments(segments)).not.toHaveProperty('recapStart');
  });
});
