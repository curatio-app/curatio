import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { WebMediaItem } from '../api/types';

(globalThis as unknown as { window?: unknown }).window ??= { location: { origin: 'http://localhost' } };

const accountMocks = vi.hoisted(() => ({
  getDetails: vi.fn(),
  getSegments: vi.fn(),
  reportPlaybackProgress: vi.fn(),
}));

vi.mock('../api/accountClient', () => accountMocks);

// eslint-disable-next-line import/first
import { preparePlaybackMetadata, refreshPlaybackSegments } from './playerEngine';

const episode = {
  imdb_id: 'tt1234567',
  video_id: 'tt1234567',
  media_type: 'series',
  season: 1,
  episode: 4,
  intro_start: 90,
  intro_end: 150,
  title: 'Episode 4',
} as unknown as WebMediaItem;

describe('series playback metadata', () => {
  beforeEach(() => {
    accountMocks.getDetails.mockReset();
    accountMocks.getSegments.mockReset();
    accountMocks.reportPlaybackProgress.mockReset();
    accountMocks.getDetails.mockResolvedValue({ seasons: [] });
  });

  it('uses server segments as the authoritative series markers', async () => {
    accountMocks.getSegments.mockResolvedValue({
      fingerprint_status: 'partial',
      retry_after_sec: 5,
      segments: [{ type: 'intro', start: 12, end: 72 }],
    });

    const metadata = await preparePlaybackMetadata(episode);

    expect(metadata.markers).toEqual({ introStart: 12, introEnd: 72 });
    expect(metadata.segmentResponse?.fingerprint_status).toBe('partial');
    expect(accountMocks.getSegments).toHaveBeenCalledWith('tt1234567:1:4', undefined);
  });

  it('does not resurrect stale item markers when the server has none', async () => {
    accountMocks.getSegments.mockResolvedValue({
      fingerprint_status: 'complete',
      segments: [],
    });

    const metadata = await preparePlaybackMetadata(episode);
    expect(metadata.markers).toEqual({});
  });

  it('propagates status transitions and the request cancellation signal', async () => {
    const controller = new AbortController();
    accountMocks.getSegments
      .mockResolvedValueOnce({ fingerprint_status: 'unavailable', segments: [] })
      .mockResolvedValueOnce({
        fingerprint_status: 'partial',
        retry_after_sec: 15,
        segments: [{ type: 'intro', start: 20, end: 80 }],
      })
      .mockResolvedValueOnce({
        fingerprint_status: 'complete',
        segments: [{ type: 'intro', start: 20, end: 80 }],
      });

    expect((await refreshPlaybackSegments(episode, controller.signal)).fingerprint_status).toBe('unavailable');
    expect((await refreshPlaybackSegments(episode, controller.signal)).fingerprint_status).toBe('partial');
    expect((await refreshPlaybackSegments(episode, controller.signal)).fingerprint_status).toBe('complete');
    expect(accountMocks.getSegments.mock.calls.every((call) => call[1] === controller.signal)).toBe(true);
  });
});
