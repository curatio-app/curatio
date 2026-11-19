import { afterEach, describe, expect, it } from 'vitest';
import {
  type BrowserCaps,
  classifyStream,
  disableDolbyVisionPlayback,
  isDolbyVisionDisabled,
  playableStreams,
  resetBrowserCaps,
  resetDolbyVisionDisabled,
  primeBrowserCaps,
  parseStreamMedia,
  resolutionScore,
  sizeScore,
  sortBrowserStreams,
  webFriendlyScore,
} from './streamCompat';
import { codecs, makeSource } from './testFixtures';

// Browsers can hardware-decode the video but only open a narrow set of
// containers/audio. These two profiles cover the load-bearing split: Chromium
// (no HEVC, no AC3, no native HLS) is the floor; Safari adds HEVC + AC3 + HLS.
const chrome: BrowserCaps = {
  h264: true, hevc: false, vp9: true, av1: true,
  aac: true, ac3: false, eac3: false, opus: true,
  nativeHls: false, mseAvailable: true,
};
const safari: BrowserCaps = {
  h264: true, hevc: true, vp9: false, av1: false,
  aac: true, ac3: true, eac3: true, opus: false,
  nativeHls: true, mseAvailable: true,
};
const iosSafari: BrowserCaps = { ...safari, mseAvailable: false, nativeHls: true };
const firefox: BrowserCaps = { ...chrome, nativeHls: false, mseAvailable: true };
const noMse: BrowserCaps = { ...chrome, mseAvailable: false, nativeHls: false };

describe('legacy video codecs', () => {
  it('marks VC-1 / MPEG-2 / MPEG-4 ASP as needing a re-encode in every browser', () => {
    for (const video of ['vc1', 'mpeg2', 'mpeg4']) {
      expect(classifyStream(codecs('mkv', video, 'dts'), safari).status).toBe('incompatible');
    }
    expect(classifyStream(codecs('mp4', 'h264', 'aac'), chrome).status).toBe('native');
  });
});

describe('classifyStream (structured fields only)', () => {
  it('plays browser-native containers/codecs directly', () => {
    expect(classifyStream(codecs('mp4', 'h264', 'aac'), chrome).status).toBe('native');
    expect(classifyStream(codecs('webm', 'vp9', 'opus'), chrome).status).toBe('native');
    expect(classifyStream(makeSource({ delivery: 'hls', url: 'https://x/media/abc' }), chrome).status).toBe('hls');
  });

  it('never reads codecs from display strings', () => {
    const labelled = makeSource({ container: 'mp4', label: '4K · HEVC · Dolby Vision · TrueHD', detail: 'Movie.2160p.x265.TrueHD.mkv' });
    const parsed = parseStreamMedia(labelled);
    expect(parsed.videoCodec).toBe('unknown');
    expect(parsed.audioCodec).toBe('unknown');
    expect(classifyStream(labelled, chrome).status).toBe('native');
  });

  it('routes MKV/AVI/TS with decodable codecs to server-prepared HLS (no client converters)', () => {
    expect(classifyStream(codecs('mkv', 'h264', 'aac'), chrome).status).toBe('server-remux');
    expect(classifyStream(codecs('avi', 'h264', 'aac'), chrome).status).toBe('server-remux');
    expect(classifyStream(codecs('mpegts', 'h264', 'aac'), chrome).status).toBe('server-remux');
  });

  it('routes AC3/E-AC-3 audio the browser cannot decode to the server (any container)', () => {
    expect(classifyStream(codecs('mkv', 'h264', 'ac3'), chrome).status).toBe('server-remux');
    expect(classifyStream(codecs('mp4', 'h264', 'eac3'), chrome).status).toBe('server-remux');
    expect(classifyStream(codecs('mp4', 'h264', 'eac3'), safari).status).toBe('native');
  });

  it('maps DTS-HD and TrueHD to codecs no browser decodes', () => {
    expect(parseStreamMedia(codecs('mkv', 'h264', 'dtshd')).audioCodec).toBe('dts');
    expect(parseStreamMedia(codecs('mkv', 'h264', 'truehd')).audioCodec).toBe('truehd');
  });

  it('requires Main10 decode for 2160p/HDR HEVC, not just 1080p 8-bit', () => {
    const mainOnly: BrowserCaps = { ...safari, hevc10: false };
    expect(classifyStream(codecs('mp4', 'hevc', 'aac', { resolution: '2160p' }), mainOnly).status).toBe('incompatible');
    expect(classifyStream(codecs('mp4', 'hevc', 'aac', { resolution: '1080p', hdr: 'hdr10' }), mainOnly).status).toBe('incompatible');
    expect(classifyStream(codecs('mp4', 'hevc', 'aac', { resolution: '1080p' }), mainOnly).status).toBe('native');
    expect(classifyStream(codecs('mp4', 'hevc', 'aac', { resolution: '2160p' }), { ...safari, hevc10: true }).status).toBe('native');
  });

  it('keeps video-transcode-only codecs unavailable', () => {
    expect(classifyStream(codecs('mkv', 'hevc', 'aac'), chrome).status).toBe('incompatible');
    expect(classifyStream(codecs('mkv', 'av1', 'aac'), safari).status).toBe('incompatible');
  });

  it('routes browser-unusable H.264 containers/audio to bounded server remux', () => {
    expect(classifyStream(codecs('mkv', 'h264', 'dtshd'), chrome).status).toBe('server-remux');
    expect(classifyStream(codecs('mkv', 'h264', 'truehd'), firefox).status).toBe('server-remux');
    expect(classifyStream(codecs('mkv', 'h264', 'aac'), noMse).status).toBe('server-remux');
    expect(classifyStream(codecs('avi', 'h264', 'aac'), iosSafari).status).toBe('server-remux');
    expect(classifyStream(codecs('ts', 'h264', 'dts'), safari).status).toBe('server-remux');
  });

  it('uses Safari/Edge native HEVC + AC3 support where present', () => {
    expect(classifyStream(codecs('mp4', 'hevc', 'ac3'), safari).status).toBe('native');
    expect(classifyStream(codecs('mkv', 'hevc', 'ac3'), safari).status).toBe('server-remux');
  });

  it('treats unknown-container sources as unknown (attempt + fail over), and a missing URL as unplayable', () => {
    expect(classifyStream(makeSource({ url: 'https://x/media/opaque' }), chrome).status).toBe('unknown');
    expect(classifyStream(makeSource({ url: '' }), chrome).status).toBe('incompatible');
  });

  it('falls back to the URL extension only when the server sent no container', () => {
    expect(parseStreamMedia(makeSource({ url: 'https://x/a.mkv' })).container).toBe('mkv');
    expect(parseStreamMedia(makeSource({ url: 'https://x/a.mkv', container: 'mp4' })).container).toBe('mp4');
  });
});

describe('resolution and size from structured fields', () => {
  it('reads resolution and height', () => {
    expect(resolutionScore(makeSource({ resolution: '2160p' }))).toBe(4);
    expect(resolutionScore(makeSource({ height: 1080 }))).toBe(3);
    expect(resolutionScore(makeSource({ resolution: '720p' }))).toBe(2);
    expect(resolutionScore(makeSource({ resolution: '480p' }))).toBe(1);
    expect(resolutionScore(makeSource())).toBe(0);
  });

  it('reads size_bytes as MB', () => {
    expect(sizeScore(makeSource({ size_bytes: 2 * 1024 * 1024 * 1024 }))).toBe(2048);
    expect(sizeScore(makeSource())).toBe(0);
  });
});

describe('ranking', () => {
  it('scores direct-play-reliable formats higher', () => {
    expect(webFriendlyScore(codecs('mp4', 'h264', 'aac'))).toBeGreaterThan(webFriendlyScore(codecs('mkv', 'hevc', 'ac3')));
  });

  it('orders direct play before server preparation before transcode-only, keeping server order within a bucket', () => {
    const native = codecs('mp4', 'h264', 'aac');
    const remux = codecs('mkv', 'h264', 'aac');
    const ac3 = codecs('mkv', 'h264', 'ac3');
    const hevc = codecs('mkv', 'hevc', 'aac', { resolution: '2160p' });
    expect(sortBrowserStreams([hevc, ac3, remux, native], chrome)).toEqual([native, ac3, remux, hevc]);
  });

  it('keeps server order within a capability bucket instead of re-ranking by resolution or size', () => {
    const smaller720 = codecs('mp4', 'h264', 'aac', { resolution: '720p', size_bytes: 2e9 });
    const bigger4k = codecs('mp4', 'h264', 'aac', { resolution: '2160p', size_bytes: 40e9 });
    expect(sortBrowserStreams([smaller720, bigger4k], chrome)).toEqual([smaller720, bigger4k]);
  });

  it('prefers an MP4 sibling over an MKV one with the same codecs', () => {
    const mp4 = codecs('mp4', 'h264', 'aac');
    const mkv = codecs('mkv', 'h264', 'aac');
    expect(sortBrowserStreams([mkv, mp4], chrome)[0]).toBe(mp4);
  });
});

describe('dolby vision handling', () => {
  afterEach(() => resetDolbyVisionDisabled());

  it('orders non-DV sources ahead of DV at equal status, even across resolutions', () => {
    const dv4k = codecs('mkv', 'hevc', 'aac', { resolution: '2160p', hdr: 'dv' });
    const plain1080 = codecs('mkv', 'h264', 'aac', { resolution: '1080p' });
    expect(sortBrowserStreams([dv4k, plain1080], safari)[0]).toBe(plain1080);
  });

  it('keeps DV sources selectable until one actually fails, then hides them', () => {
    const dv = codecs('mkv', 'hevc', 'aac', { hdr: 'dv' });
    const plain = codecs('mkv', 'h264', 'aac');
    expect(playableStreams([dv, plain], safari)).toContain(dv);
    expect(isDolbyVisionDisabled()).toBe(false);
    disableDolbyVisionPlayback();
    expect(isDolbyVisionDisabled()).toBe(true);
    const filtered = playableStreams([dv, plain], safari);
    expect(filtered).not.toContain(dv);
    expect(filtered).toContain(plain);
  });
});

describe('primeBrowserCaps', () => {
  it('resolves without throwing when mediaCapabilities is unavailable (node/test env)', async () => {
    resetBrowserCaps();
    const caps = await primeBrowserCaps();
    expect(typeof caps.hevc).toBe('boolean');
  });
});

it('preserves server order after device eligibility filtering', () => {
  const first = codecs('mp4', 'h264', 'aac', { resolution: '1080p' });
  const second = codecs('mp4', 'h264', 'aac', { resolution: '2160p' });
  expect(sortBrowserStreams(playableStreams([first, second], chrome), chrome)).toEqual([first, second]);
});
