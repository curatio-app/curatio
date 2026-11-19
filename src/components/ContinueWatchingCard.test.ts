import { describe, expect, it } from 'vitest';
import { episodeCaption } from './ContinueWatchingCard';

describe('episodeCaption (tvOS EpisodeFormatting.dottedCaption)', () => {
  it('reads S · E · title, Special for season 0, and drops generic titles', () => {
    expect(episodeCaption({ season: 2, episode: 5, episode_title: 'The Hunt' })).toBe('S2 · E5 · The Hunt');
    expect(episodeCaption({ season: 0, episode: 1 })).toBe('Special · E1');
    expect(episodeCaption({ season: 1, episode: 3, episode_title: 'Episode 3' })).toBe('S1 · E3');
    expect(episodeCaption({ season: 3, episode: 6, episode_title: 'Return', up_next: true })).toBe('Next: S3 · E6 · Return');
    expect(episodeCaption({ title: 'A movie' })).toBe('');
  });
});
