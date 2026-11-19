import { afterEach, describe, expect, it } from 'vitest';
import { disableDolbyVisionPlayback, resetDolbyVisionDisabled, type BrowserCaps } from '../api/streamCompat';
import type { PlaybackSource, ProbeVerdict } from '../api/types';
import { makeSource } from '../api/testFixtures';
import { isDolbyVision, mergeRecoveryStreams, preflightState, selectNextSource } from './sourceFailover';

// streamUrl() -> safeUrl() reads window.location.origin; the failover logic runs
// in the browser, so shim a minimal window for the node test environment.
(globalThis as unknown as { window?: unknown }).window ??= { location: { origin: 'http://localhost' } };

// All-capable browser so classifyStream never marks a normal source incompatible
// for reasons unrelated to the case under test.
const CAPS: BrowserCaps = {
  h264: true,
  hevc: true,
  vp9: true,
  av1: true,
  aac: true,
  ac3: true,
  eac3: true,
  opus: true,
  nativeHls: true,
  mseAvailable: true,
};

// HLS source — classifies as 'hls' regardless of caps, so it is never filtered.
function hls(id: string, overrides: Partial<PlaybackSource> = {}): PlaybackSource {
  return makeSource({ url: `http://host/${id}.m3u8`, delivery: 'hls', resolution: '1080p', route_id: id, ...overrides });
}
const GB = 1024 ** 3;

const PASSED: ProbeVerdict = { state: 'passed', reason: null, status: 206 };
const FAILED: ProbeVerdict = { state: 'failed', reason: 'http_error', status: 404 };

function verdicts(entries: Array<[PlaybackSource, ProbeVerdict]>): Map<string, ProbeVerdict> {
  const map = new Map<string, ProbeVerdict>();
  for (const [stream, verdict] of entries) map.set(String(stream.url), verdict);
  return map;
}

function pick(streams: PlaybackSource[], opts: Partial<Parameters<typeof selectNextSource>[0]> = {}) {
  return selectNextSource({
    streams,
    tried: new Set(),
    currentIndex: 0,
    verdicts: new Map(),
    caps: CAPS,
    ...opts,
  });
}

describe('selectNextSource', () => {
  it('rotates away from the failed provider before anything else', () => {
    const streams = [
      hls('cur', { provider_id: 'plex' }),
      hls('same', { provider_id: 'plex' }),
      hls('other', { provider_id: 'jellyfin' }),
    ];
    expect(pick(streams)).toBe(2); // different provider wins over the same provider
  });

  it('keeps server order for an ordinary failure: no preflight reordering (native)', () => {
    const streams = [hls('cur'), hls('pending'), hls('passed')];
    expect(pick(streams, { verdicts: verdicts([[streams[2], PASSED]]) })).toBe(1);
  });

  it('never picks a probe-failed source (native isCandidate)', () => {
    const streams = [hls('cur'), hls('failed'), hls('ok')];
    expect(pick(streams, { verdicts: verdicts([[streams[1], FAILED]]) })).toBe(2);
    expect(pick([hls('cur'), hls('failed')], { verdicts: verdicts([[hls('failed'), FAILED]]) })).toBeNull();
  });

  it('skips every source of a provider that reported itself unavailable', () => {
    const streams = [hls('cur', { provider_id: 'plex' }), hls('a', { provider_id: 'jellyfin' }), hls('b', { provider_id: 'addon-x', kind: 'addon' })];
    expect(pick(streams, { excludedProviders: new Set(['jellyfin']) })).toBe(2);
  });

  it('deprioritizes Dolby-Vision-only sources', () => {
    const streams = [hls('cur'), hls('dv', { hdr: 'dv' }), hls('sdr')];
    expect(pick(streams)).toBe(2); // the non-DV source
  });

  describe('after a DV source has failed this session', () => {
    afterEach(() => resetDolbyVisionDisabled());

    it('skips remaining DV candidates entirely instead of probing each', () => {
      disableDolbyVisionPlayback();
      const streams = [hls('cur'), hls('dv', { hdr: 'dv' }), hls('sdr')];
      expect(pick(streams, { tried: new Set([2]) })).toBeNull(); // dv is not a candidate
      expect(pick(streams)).toBe(2); // non-DV still selectable
    });
  });

  it('skips already-tried and incompatible sources', () => {
    const streams = [
      hls('cur'),
      hls('tried'),
      makeSource({ url: 'http://host/hevc.mp4', container: 'mp4', video_codec: 'hevc', resolution: '2160p' }),
      hls('ok'),
    ];
    const caps: BrowserCaps = { ...CAPS, hevc: false }; // makes index 2 incompatible
    const next = selectNextSource({
      streams,
      tried: new Set([1]),
      currentIndex: 0,
      verdicts: new Map(),
      caps,
    });
    expect(next).toBe(3);
  });

  it('follows the server order rather than resolution for ordinary failures', () => {
    const streams = [
      hls('cur', { resolution: '2160p' }),
      hls('lower', { resolution: '1080p' }),
      hls('same', { resolution: '2160p' }),
    ];
    expect(pick(streams)).toBe(1);
  });

  it('falls back to the lowest index on a total tie', () => {
    const streams = [hls('cur'), hls('a'), hls('b')];
    expect(pick(streams)).toBe(1);
  });

  it('returns null when every candidate has been tried', () => {
    const streams = [hls('cur'), hls('a')];
    expect(selectNextSource({ streams, tried: new Set([1]), currentIndex: 0, verdicts: new Map(), caps: CAPS })).toBeNull();
  });
});

describe('preflightState', () => {
  it('maps verdicts and treats a missing verdict as pending', () => {
    const passed = hls('p');
    const failed = hls('f');
    const unknown = hls('u');
    const map = verdicts([
      [passed, PASSED],
      [failed, FAILED],
    ]);
    expect(preflightState(passed, map)).toBe('passed');
    expect(preflightState(failed, map)).toBe('failed');
    expect(preflightState(unknown, map)).toBe('pending');
  });
});

describe('isDolbyVision', () => {
  it('reads the structured hdr field only', () => {
    expect(isDolbyVision(hls('a', { hdr: 'dv' }))).toBe(true);
    expect(isDolbyVision(hls('b', { hdr: 'hdr10', label: '4K · Dolby Vision' }))).toBe(false);
    expect(isDolbyVision(hls('c'))).toBe(false);
  });
});

describe('route and file identity recovery', () => {
  it('prefers the same exact file on another provider only for transport failures', () => {
    const streams = [
      hls('a', { provider_id: 'plex', file_id: 'file1' }),
      hls('b', { provider_id: 'jellyfin', file_id: 'file2' }),
      hls('c', { provider_id: 'jellyfin', file_id: 'file1' }),
    ];
    expect(pick(streams, { preferSameFile: true })).toBe(2);
    // Not asked for (e.g. rebuffering): the next other provider in server order.
    expect(pick(streams)).toBe(1);
  });

  it('does not retry a reordered route and accepts a renewed signed URL once', () => {
    const a = hls('a', { route_id: 'route-a', file_id: 'file', provider_id: 'plex' });
    const b = hls('b', { route_id: 'route-b', file_id: 'file', provider_id: 'jellyfin' });
    const renewed = hls('renewed', { route_id: 'route-renewed', file_id: 'file', provider_id: 'plex' });
    const merged = mergeRecoveryStreams([a, b], [b, a, renewed, renewed]);
    expect(merged).toEqual([a, b, renewed]);
    expect(pick(merged, { currentIndex: 1, triedRoutes: new Set(['route-a', 'route-b']) })).toBe(2);
    expect(pick([b, a], { currentIndex: null, triedRoutes: new Set(['route-a', 'route-b']) })).toBeNull();
  });

  it('treats speculative timeouts and addon routes as pending', () => {
    const slow = hls('slow');
    const addon = hls('addon', { kind: 'addon', provider_id: 'addon-x', startup_timeout_seconds: 15 });
    expect(preflightState(slow, verdicts([[slow, { state: 'indeterminate', reason: 'timeout', status: null }]]))).toBe('pending');
    expect(preflightState(addon, verdicts([[addon, FAILED]]))).toBe('pending');
  });
});

describe('bandwidth-aware failover', () => {
  const ladder = () => [
    hls('cur', { resolution: '1080p', provider_id: 'plex' }),
    hls('uhd', { resolution: '2160p', provider_id: 'jellyfin' }),
    hls('same', { resolution: '1080p', provider_id: 'jellyfin' }),
    hls('sd', { resolution: '480p', provider_id: 'jellyfin' }),
    hls('hd', { resolution: '720p', provider_id: 'jellyfin' }),
  ];

  it('keeps server order for ordinary failures', () => {
    expect(pick(ladder())).toBe(1);
  });

  it('a device failure prefers the nearest lower tier, then the same tier, then higher', () => {
    const streams = ladder();
    expect(pick(streams, { preferLowerTier: true })).toBe(4);
    expect(pick(streams, { preferLowerTier: true, tried: new Set([4]) })).toBe(3);
    expect(pick(streams, { preferLowerTier: true, tried: new Set([3, 4]) })).toBe(2);
    expect(pick(streams, { preferLowerTier: true, tried: new Set([2, 3, 4]) })).toBe(1);
  });
});

describe('repeated rebuffering (native preferLowerBitrate)', () => {
  it('picks a source under 80% of the failing bitrate that fits 75% of measured bandwidth', () => {
    const sized = (id: string, gb: number, resolution = '1080p') => hls(id, { resolution, size_bytes: gb * GB, provider_id: id });
    const streams = [sized('cur', 20), sized('heavy', 25), sized('light', 6), sized('lighter', 3, '720p')];
    // 2 h runtime: 6 GB ≈ 7.2 Mbps, 3 GB ≈ 3.6 Mbps; budget 0.75 × 8 = 6 Mbps.
    expect(pick(streams, { preferLowerBitrate: true, runtimeSeconds: 7200, measuredMbps: 8 })).toBe(3);
    // Without a measurement the heaviest sustainable lighter source wins (TV order).
    expect(pick(streams, { preferLowerBitrate: true, runtimeSeconds: 7200 })).toBe(2);
  });

  it("prefers the server's bitrate over a size estimate", () => {
    const streams = [
      hls('cur', { bitrate: 20_000_000, provider_id: 'a' }),
      hls('big-but-light', { size_bytes: 40 * GB, bitrate: 4_000_000, provider_id: 'b' }),
      hls('small-but-heavy', { size_bytes: 1 * GB, bitrate: 18_000_000, provider_id: 'c' }),
    ];
    expect(pick(streams, { preferLowerBitrate: true, runtimeSeconds: 7200, measuredMbps: 8 })).toBe(1);
  });
});
