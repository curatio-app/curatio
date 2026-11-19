import { describe, expect, it } from 'vitest';

// playerEngine imports mediaUtils (safeUrl reads window.location) and accountClient;
// shim a minimal window for the node test environment, like sourceFailover.test.ts.
(globalThis as unknown as { window?: unknown }).window ??= { location: { origin: 'http://localhost' } };

// eslint-disable-next-line import/first
import {
  applySeek,
  attemptPlay,
  canRecoverFatalHls,
  heldPlaylistUrl,
  effectiveDuration,
  fileAudioOptions,
  HLS_MEDIA_RECOVERY_LIMIT,
  HLS_NETWORK_RECOVERY_LIMIT,
  updatePlaybackTimeline,
} from './playerEngine';

type FakeRanges = { length: number; end: (index: number) => number };

function ranges(end: number | null): FakeRanges {
  return end == null ? { length: 0, end: () => 0 } : { length: 1, end: () => end };
}

// A minimal HTMLVideoElement stand-in: only the members the timeline/seek helpers
// read. `duration` may be Infinity to model hls.js live/EVENT playlists (the
// server remux output), which is the regression these tests pin.
function fakeVideo(opts: {
  duration: number;
  currentTime?: number;
  buffered?: number | null;
  seekable?: number | null;
}): HTMLVideoElement {
  return {
    duration: opts.duration,
    currentTime: opts.currentTime ?? 0,
    buffered: ranges(opts.buffered ?? null),
    seekable: ranges(opts.seekable ?? null),
  } as unknown as HTMLVideoElement;
}

describe('effectiveDuration', () => {
  it('prefers a finite media duration', () => {
    expect(effectiveDuration(fakeVideo({ duration: 120 }), 999)).toBe(120);
  });

  it('falls back to the supplied total when duration is Infinity', () => {
    expect(effectiveDuration(fakeVideo({ duration: Infinity }), 7200)).toBe(7200);
  });

  it('falls back to the seekable end when no total is supplied', () => {
    expect(effectiveDuration(fakeVideo({ duration: Infinity, seekable: 300 }), 0)).toBe(300);
  });

  it('returns 0 when nothing is known', () => {
    expect(effectiveDuration(fakeVideo({ duration: Number.NaN }), 0)).toBe(0);
  });

  it('maps a finite remux duration back onto the absolute movie timeline', () => {
    expect(effectiveDuration(fakeVideo({ duration: 1800 }), 3600, 1800)).toBe(3600);
  });
});

describe('updatePlaybackTimeline with non-finite duration', () => {
  it('reports progress against the fallback total when video.duration is Infinity', () => {
    let duration = -1;
    let progress = -1;
    let buffered = -1;
    updatePlaybackTimeline(
      fakeVideo({ duration: Infinity, currentTime: 1800, buffered: 1900 }),
      (v) => (progress = v),
      (v) => (duration = v),
      (v) => (buffered = v),
      3600,
    );
    expect(duration).toBe(3600);
    expect(progress).toBeCloseTo(0.5, 5);
    expect(buffered).toBeCloseTo(1900 / 3600, 5);
  });

  it('stays at zero when neither duration nor fallback is known', () => {
    let duration = -1;
    let progress = -1;
    let buffered = -1;
    updatePlaybackTimeline(
      fakeVideo({ duration: Infinity, currentTime: 10 }),
      (v) => (progress = v),
      (v) => (duration = v),
      (v) => (buffered = v),
      0,
    );
    expect(duration).toBe(0);
    expect(progress).toBe(0);
    expect(buffered).toBe(0);
  });

  it('still works on a normal finite VOD source', () => {
    let duration = -1;
    let progress = -1;
    updatePlaybackTimeline(
      fakeVideo({ duration: 200, currentTime: 50 }),
      (v) => (progress = v),
      (v) => (duration = v),
      () => undefined,
      0,
    );
    expect(duration).toBe(200);
    expect(progress).toBeCloseTo(0.25, 5);
  });
});

describe('a prepared session (the playlist is still growing)', () => {
  // hls.js reports the prepared edge as the element's duration: 59 s, then 69 s...
  it('shows the length of the film, not how much has been prepared', () => {
    const video = fakeVideo({ duration: 59, currentTime: 30 });
    expect(effectiveDuration(video, 180, 0, true)).toBe(180);
    expect(effectiveDuration(video, 180, 0, false)).toBe(59); // a direct file: the element knows best
  });

  it('maps progress against the film after a seek restarted the session', () => {
    const video = fakeVideo({ duration: 12, currentTime: 6, buffered: 12 });
    let progress = -1, duration = -1, buffered = -1, exact: boolean | null = null;
    updatePlaybackTimeline(video, (v) => (progress = v), (v) => (duration = v), (v) => (buffered = v), 180, 84, (v) => (exact = v), { exact: true });
    expect(duration).toBe(180);
    expect(progress).toBeCloseTo(0.5, 5);
    expect(buffered).toBeCloseTo(96 / 180, 5);
    expect(exact).toBe(true);
  });

  it('a runtime guess from metadata is not trusted to end the title on', () => {
    let exact: boolean | null = null;
    updatePlaybackTimeline(fakeVideo({ duration: 59 }), () => undefined, () => undefined, () => undefined, 7200, 0, (v) => (exact = v), { exact: false });
    expect(exact).toBe(false);
  });

  it('falls back to what is prepared when the length is unknown', () => {
    expect(effectiveDuration(fakeVideo({ duration: 59 }), 0, 20, true)).toBe(79);
  });
});

describe('fileAudioOptions', () => {
  const tracks = [
    { index: 1, codec: 'truehd', channels: 6, language: 'eng', title: '' },
    { index: 2, codec: 'ac3', channels: 2, language: 'eng', title: 'Commentary' },
    { index: 3, codec: 'eac3', channels: 6, language: '', title: '' },
  ];

  it('lists every audio track of the file and marks the one being played', () => {
    const options = fileAudioOptions(tracks as never, 2);
    expect(options.map((o) => o.id)).toEqual(['file:1', 'file:2', 'file:3']);
    expect(options.map((o) => o.selected)).toEqual([false, true, false]);
    expect(options[0].label).toMatch(/English · TRUEHD 5\.1|English · TrueHD 5\.1/i);
    expect(options[1].label).toBe('English · Commentary');
    expect(options[2].label).toMatch(/E-AC-3 5\.1/);
  });

  it('marks nothing when the session did not say which track it used', () => {
    expect(fileAudioOptions(tracks as never, null).some((o) => o.selected)).toBe(false);
  });
});

describe('applySeek', () => {
  it('prefers fastSeek (keyframe-accurate) when the element supports it', () => {
    const calls: number[] = [];
    const video = {
      currentTime: 0,
      fastSeek: (t: number) => calls.push(t),
    } as unknown as HTMLVideoElement;
    applySeek(video, 123.4);
    expect(calls).toEqual([123.4]);
    expect(video.currentTime).toBe(0); // untouched — fastSeek handled it
  });

  it('falls back to currentTime when fastSeek is unavailable', () => {
    const video = { currentTime: 0 } as unknown as HTMLVideoElement;
    applySeek(video, 90);
    expect(video.currentTime).toBe(90);
  });

  it('never seeks before the start', () => {
    const video = { currentTime: 50 } as unknown as HTMLVideoElement;
    applySeek(video, -10);
    expect(video.currentTime).toBe(0);
  });
});

describe('updatePlaybackTimeline duration-source classification', () => {
  it('flags a fallback-derived duration as not media-derived', () => {
    let duration = -1;
    let mediaDerived: boolean | null = null;
    updatePlaybackTimeline(
      fakeVideo({ duration: Infinity, currentTime: 1800 }),
      () => undefined,
      (v) => (duration = v),
      () => undefined,
      3600,
      0,
      (v) => (mediaDerived = v),
    );
    // The timeline still uses the fallback total, but the Next Episode fallback
    // must know it's a guess.
    expect(duration).toBe(3600);
    expect(mediaDerived).toBe(false);
  });

  it('flags a finite element-reported duration as media-derived', () => {
    let mediaDerived: boolean | null = null;
    updatePlaybackTimeline(
      fakeVideo({ duration: 200, currentTime: 50 }),
      () => undefined,
      () => undefined,
      () => undefined,
      0,
      0,
      (v) => (mediaDerived = v),
    );
    expect(mediaDerived).toBe(true);
  });
});

describe('updatePlaybackTimeline with a remux time offset', () => {
  it('adds the offset so the bar reflects true movie time', () => {
    let duration = -1;
    let progress = -1;
    let buffered = -1;
    updatePlaybackTimeline(
      fakeVideo({ duration: Infinity, currentTime: 600, buffered: 700 }),
      (v) => (progress = v),
      (v) => (duration = v),
      (v) => (buffered = v),
      3600,
      1800, // remux base offset (movie seconds at currentTime 0)
    );
    expect(duration).toBe(3600);
    expect(progress).toBeCloseTo((1800 + 600) / 3600, 5);
    expect(buffered).toBeCloseTo((1800 + 700) / 3600, 5);
  });
});

describe('bounded HLS recovery', () => {
  it('eventually rejects fatal network and media recovery', () => {
    expect(canRecoverFatalHls('network', HLS_NETWORK_RECOVERY_LIMIT - 1)).toBe(true);
    expect(canRecoverFatalHls('network', HLS_NETWORK_RECOVERY_LIMIT)).toBe(false);
    expect(canRecoverFatalHls('media', HLS_MEDIA_RECOVERY_LIMIT - 1)).toBe(true);
    expect(canRecoverFatalHls('media', HLS_MEDIA_RECOVERY_LIMIT)).toBe(false);
  });
});

describe('autoplay policy handling', () => {
  it('marks NotAllowedError as user-action-required instead of source failure', async () => {
    let blockedEvents = 0;
    const video = {
      dataset: {},
      play: () => Promise.reject({ name: 'NotAllowedError' }),
      dispatchEvent: (event: Event) => {
        if (event.type === 'curatioautoplayblocked') blockedEvents += 1;
        return true;
      },
    } as unknown as HTMLVideoElement;
    await expect(attemptPlay(video)).resolves.toBe('blocked');
    expect(video.dataset.curatioAutoplayBlocked).toBe('true');
    expect(blockedEvents).toBe(1);
  });

  it('keeps non-policy play failures distinct', async () => {
    const video = {
      dataset: {},
      play: () => Promise.reject(new Error('decode failed')),
      dispatchEvent: () => true,
    } as unknown as HTMLVideoElement;
    await expect(attemptPlay(video)).resolves.toBe('failed');
    expect(video.dataset.curatioAutoplayBlocked).toBeUndefined();
  });
});

describe('heldPlaylistUrl', () => {
  it('asks prepared sessions (server or desktop engine) to long-poll, and nothing else', () => {
    expect(heldPlaylistUrl('/account/api/playback/remux/abc/index.m3u8')).toBe('/account/api/playback/remux/abc/index.m3u8?hold=1');
    expect(heldPlaylistUrl('https://c.example/account/api/playback/remux/local-x/index.m3u8')).toBe('https://c.example/account/api/playback/remux/local-x/index.m3u8?hold=1');
    expect(heldPlaylistUrl('/account/api/playback/remux/abc/index.m3u8?hold=1')).toBe('/account/api/playback/remux/abc/index.m3u8?hold=1');
    expect(heldPlaylistUrl('https://cdn.example/live/master.m3u8')).toBe('https://cdn.example/live/master.m3u8');
  });
});
