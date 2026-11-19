import type Hls from 'hls.js';
import { describe, expect, it, vi } from 'vitest';

(globalThis as unknown as { window?: unknown }).window ??= { location: { origin: 'http://localhost', href: 'http://localhost/' } };

// eslint-disable-next-line import/first
import { refreshTrackState, selectAudioTrack, type TrackOption } from './playerEngine';

function fakeHls(overrides: Partial<Hls> = {}): Hls {
  return {
    audioTracks: [
      { lang: 'spa', name: 'Spanish' },
      { lang: 'eng', name: 'English 5.1 E-AC-3' },
    ],
    audioTrack: 1,
    subtitleTracks: [],
    subtitleTrack: -1,
    ...overrides,
  } as unknown as Hls;
}

describe('selectAudioTrack (hls.js)', () => {
  it('routes an hls id to the engine and re-syncs the menu', () => {
    const hls = fakeHls();
    const setAudio = vi.fn();
    selectAudioTrack('hls:0', null, hls, setAudio, vi.fn());
    expect(hls.audioTrack).toBe(0);
    expect(setAudio).toHaveBeenCalled();
  });
});

describe('refreshTrackState (hls.js)', () => {
  it('surfaces audio tracks English first with language + detail', () => {
    const setAudio = vi.fn();
    refreshTrackState(null, fakeHls(), setAudio, vi.fn());
    const audio = setAudio.mock.calls[0][0] as TrackOption[];
    expect(audio.map((track) => track.id)).toEqual(['hls:1', 'hls:0']);
    expect(audio[0].label).toBe('English');
    expect(audio[0].detail).toContain('5.1');
    expect(audio[0].selected).toBe(true);
  });

  it('always offers an Off subtitle entry', () => {
    const setSubs = vi.fn();
    refreshTrackState(null, fakeHls(), vi.fn(), setSubs);
    const subs = setSubs.mock.calls[0][0] as TrackOption[];
    expect(subs[0]).toMatchObject({ id: 'off', label: 'Off', selected: true });
  });
});
