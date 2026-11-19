// @vitest-environment jsdom
// The desktop app picks like iOS/iPadOS: the server's order, with only sources
// that would need a full video re-encode moved back. Browsers are unchanged.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { PlaybackSource } from '../api/types';
import { codecs, makeSource } from '../api/testFixtures';
import type { PlaybackPlan } from './playbackPlan';

const resolveMock = vi.hoisted(() => vi.fn());
// What Electron's Chromium reports on a Mac (measured): HEVC Main10, AV1, H.264, AAC/Opus/FLAC; no AC-3/E-AC-3/DTS/TrueHD.
const electronCaps = vi.hoisted(() => ({ h264: true, hevc: true, hevc10: true, vp9: true, av1: true, aac: true, ac3: false, eac3: false, opus: true, nativeHls: false, mseAvailable: true }));
vi.mock('../api/streamCompat', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/streamCompat')>();
  return { ...actual, getBrowserCaps: () => electronCaps, primeBrowserCaps: async () => electronCaps, classifyStream: (stream: PlaybackSource, caps = electronCaps) => actual.classifyStream(stream, caps), playableStreams: (streams: PlaybackSource[], caps = electronCaps) => actual.playableStreams(streams, caps), sortBrowserStreams: (streams: PlaybackSource[], caps = electronCaps) => actual.sortBrowserStreams(streams, caps) };
});
const inspectMock = vi.hoisted(() => vi.fn());
vi.mock('../api/accountClient', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/accountClient')>()),
  resolvePlaybackSources: resolveMock,
  inspectPlaybackSources: inspectMock,
}));

import { loadPlaybackSources, resetPlaybackSourcesCache } from '../api/playbackSources';
import { resetDesktopBridge } from '../api/desktopBridge';
import { orderByPlayability, planForStream, prefetchPlayback } from './playbackPlan';
import { selectNextSource } from './sourceFailover';

const item = { tmdb_id: 1, media_type: 'movie' as const, title: 'Movie', imdb_id: 'tt0000001', availability: { playable: true, in_library: true, sources: [] } };
// Server order: best first.
const SERVER_ORDER: PlaybackSource[] = [
  codecs('mkv', 'hevc', 'truehd', { url: 'https://cdn/a.mkv', resolution: '2160p', hdr: 'dv', audio_channels: 8 }),
  codecs('mkv', 'hevc', 'eac3', { url: 'https://cdn/b.mkv', resolution: '2160p', hdr: 'hdr10', audio_channels: 6 }),
  codecs('mp4', 'h264', 'aac', { url: 'https://cdn/c.mp4', resolution: '1080p', audio_channels: 2 }),
  codecs('mkv', 'vc1', 'dtshd', { url: 'https://cdn/d.mkv', resolution: '1080p', audio_channels: 6 }),
  makeSource({ url: '', resolution: '720p' }),
];

function installDesktop() {
  const engine = { start: vi.fn(), keepalive: vi.fn(), stop: vi.fn(), prepare: vi.fn(async () => true), log: vi.fn(async () => undefined) };
  window.curatioDesktop = { info: async () => ({ localEngine: true, platform: 'darwin', version: '1', server: null }), engine };
  return engine;
}

beforeEach(() => {
  resetPlaybackSourcesCache();
  resetDesktopBridge();
  resolveMock.mockResolvedValue({ quality: 'original', availability: item.availability, sources: SERVER_ORDER });
});
afterEach(() => {
  delete window.curatioDesktop;
  resetDesktopBridge();
});

const titles = (streams: PlaybackSource[]) => streams.map((s) => s.url);
const plan = (kind: PlaybackPlan['kind'], score: number): PlaybackPlan => ({ kind, score, reason: '', basis: 'facts', allowHevc: true, allowTranscode: false, surround: false, videoScope: null });

describe('desktop app (local engine)', () => {
  it('keeps the server order like iOS; only a VC-1 source (needs a re-encode) moves back', async () => {
    installDesktop();
    const { streams } = await loadPlaybackSources(item, { tier: 'default' });
    expect(titles(streams)).toEqual(['https://cdn/a.mkv', 'https://cdn/b.mkv', 'https://cdn/c.mp4', 'https://cdn/d.mkv']);
  });

  it('does not reorder by "plays directly in the browser" when ordering by plan', () => {
    installDesktop();
    const plans = new Map<string, PlaybackPlan>([
      ['https://cdn/a.mkv', plan('server-hls', 200)],
      ['https://cdn/b.mkv', plan('server-hls', 200)],
      ['https://cdn/c.mp4', plan('direct', 400)],
      ['https://cdn/d.mkv', plan('server-transcode', 40)],
    ]);
    const ordered = orderByPlayability(SERVER_ORDER.slice(0, 4), (s) => plans.get(String(s.url))!);
    expect(titles(ordered)).toEqual(['https://cdn/a.mkv', 'https://cdn/b.mkv', 'https://cdn/c.mp4', 'https://cdn/d.mkv']);
  });

  it('does not push Dolby Vision to the back when failing over', () => {
    installDesktop();
    const streams = [
      codecs('mkv', 'hevc', 'eac3', { url: 'https://cdn/x.mkv', resolution: '2160p', hdr: 'hdr10' }),
      codecs('mkv', 'hevc', 'eac3', { url: 'https://cdn/dv.mkv', resolution: '2160p', hdr: 'dv', provider_id: 'jellyfin' }),
      codecs('mkv', 'hevc', 'eac3', { url: 'https://cdn/hdr.mkv', resolution: '2160p', hdr: 'hdr10', provider_id: 'jellyfin' }),
    ];
    const next = selectNextSource({ streams, tried: new Set([0]), currentIndex: 0, verdicts: new Map(), planOf: () => plan('server-hls', 200) });
    expect(next).toBe(1);
  });
});

describe('desktop app: never play directly on a guess', () => {
  const mp4 = codecs('mp4', 'h264', null, { url: 'https://cdn/c.mp4', resolution: '1080p' });
  const mystery = makeSource({ url: 'https://resolver.example/play/abc123', resolution: '2160p' });

  it('prepares a source whose format is not verified (it may carry audio Chromium drops silently)', () => {
    installDesktop();
    expect(planForStream(mp4).kind).toBe('server-hls');
    expect(planForStream(mystery)).toMatchObject({ kind: 'server-hls', allowHevc: true, basis: 'fields' });
  });

  it('a browser still tries them directly (the silent-audio guard catches the bad ones)', () => {
    expect(planForStream(mp4).kind).toBe('direct');
    expect(planForStream(mystery).kind).toBe('direct');
  });
});

describe('desktop app: prewarm when Details opens', () => {
  const flush = () => new Promise((resolve) => setTimeout(resolve, 20));

  it('asks the engine to prepare the source Play will open, at the resume position', async () => {
    const engine = installDesktop();
    inspectMock.mockResolvedValue({ results: {} });
    prefetchPlayback(item, { tier: 'default', start: 1968, audioLanguage: 'en' });
    await flush();
    expect(engine.prepare).toHaveBeenCalledTimes(1);
    expect(engine.prepare).toHaveBeenCalledWith(expect.objectContaining({ url: 'https://cdn/a.mkv', start: 1968, audio_language: 'en', allow_hevc: true, surround: true }));
  });

  it('does nothing in a browser, and nothing once cancelled', async () => {
    inspectMock.mockResolvedValue({ results: {} });
    prefetchPlayback(item, { tier: 'default', start: 0 });
    await flush();
    const engine = installDesktop();
    resetPlaybackSourcesCache();
    const cancel = prefetchPlayback(item, { tier: 'default', start: 0 });
    cancel();
    await flush();
    expect(engine.prepare).not.toHaveBeenCalled();
  });
});

describe('browser (no engine)', () => {
  it('keeps the server order too, moving back only Dolby Vision a browser cannot show', async () => {
    const { streams } = await loadPlaybackSources(item, { tier: 'default' });
    expect(streams[0].url).toBe('https://cdn/b.mkv');
    expect(titles(streams).indexOf('https://cdn/a.mkv')).toBeGreaterThan(titles(streams).indexOf('https://cdn/c.mp4'));
  });

  it('does not reorder playable plans by score (native: server order wins)', () => {
    const plans = new Map<string, PlaybackPlan>([
      ['https://cdn/a.mkv', plan('server-hls', 200)],
      ['https://cdn/c.mp4', plan('direct', 400)],
      ['https://cdn/d.mkv', plan('server-transcode', 100)],
    ]);
    const ordered = orderByPlayability([SERVER_ORDER[3], SERVER_ORDER[0], SERVER_ORDER[2]], (s) => plans.get(String(s.url))!);
    expect(titles(ordered)).toEqual(['https://cdn/a.mkv', 'https://cdn/c.mp4', 'https://cdn/d.mkv']);
  });
});
