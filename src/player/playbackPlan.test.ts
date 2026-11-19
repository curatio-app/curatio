import { describe, expect, it } from 'vitest';

(globalThis as unknown as { window?: unknown }).window ??= { location: { origin: 'http://localhost', href: 'http://localhost/' } };

// eslint-disable-next-line import/first
import type { BrowserCaps } from '../api/streamCompat';
// eslint-disable-next-line import/first
import { classifyStream } from '../api/streamCompat';
// eslint-disable-next-line import/first
import type { MediaAudioFacts, MediaFacts, PlaybackSource } from '../api/types';
import { codecs, makeSource } from '../api/testFixtures';
// eslint-disable-next-line import/first
import {
  decodeQueriesFor,
  orderByPlayability,
  planFromClassification,
  planFromFacts,
  videoCodecString,
  type DecodeLookup,
  type PlanOptions,
} from './playbackPlan';

function audio(codec: string, codecString: string | null, extra: Partial<MediaAudioFacts> = {}): MediaAudioFacts {
  return { index: 1, codec, codec_string: codecString, channels: 2, language: 'eng', title: '', default: true, ...extra };
}

function facts(overrides: Partial<MediaFacts> = {}): MediaFacts {
  return {
    container: 'mp4',
    duration: 6000,
    bitrate: 8_000_000,
    video: {
      index: 0, codec: 'h264', profile: 'High', level: 40, bit_depth: 8, width: 1920, height: 1080,
      frame_rate: 23.976, dynamic_range: 'sdr', codec_string: 'avc1.640028',
    },
    audio: [audio('aac', 'mp4a.40.2')],
    subtitles: [],
    ...overrides,
  };
}

const hevcMain10 = {
  index: 0, codec: 'hevc', profile: 'Main 10', level: 153, bit_depth: 10, width: 3840, height: 2160,
  frame_rate: 24, dynamic_range: 'hdr10', codec_string: 'hvc1.2.4.L153.B0',
};

/** A browser model: which codec strings decode (type-independent here). */
function browser(decodes: RegExp): DecodeLookup {
  return (query) => decodes.test(/codecs="([^"]+)"/.exec(query.contentType)?.[1] ?? '');
}

const chrome = browser(/^(avc1|mp4a|opus|flac|vp09|av01)/);
const safari = browser(/^(avc1|mp4a|hvc1|ac-3|ec-3)/);
const options = (decode: DecodeLookup, extra: Partial<PlanOptions> = {}): PlanOptions => ({ decode, nativeHls: false, ...extra });

describe('planFromFacts — codec × capability matrix', () => {
  it('MP4 / H.264 / AAC plays directly', () => {
    expect(planFromFacts(facts(), options(chrome)).kind).toBe('direct');
  });

  it('MKV / H.264 / E-AC-3 goes to server-prepared HLS (container and audio)', () => {
    const plan = planFromFacts(facts({ container: 'matroska', audio: [audio('eac3', 'ec-3', { channels: 6 })] }), options(chrome));
    expect(plan.kind).toBe('server-hls');
    expect(plan.allowTranscode).toBe(false);
    expect(plan.surround).toBe(true);
  });

  it('MP4 with audio the browser cannot decode goes to server-hls', () => {
    expect(planFromFacts(facts({ audio: [audio('ac3', 'ac-3')] }), options(chrome)).kind).toBe('server-hls');
    expect(planFromFacts(facts({ audio: [audio('ac3', 'ac-3')] }), options(safari)).kind).toBe('direct');
    expect(planFromFacts(facts({ audio: [audio('dts', 'dtsc')] }), options(safari)).kind).toBe('server-hls');
    expect(planFromFacts(facts({ audio: [audio('truehd', 'mlpa')] }), options(safari)).kind).toBe('server-hls');
  });

  it('HEVC Main10 without a decoder is a last-resort server transcode', () => {
    const plan = planFromFacts(facts({ video: hevcMain10 }), options(chrome));
    expect(plan.kind).toBe('server-transcode');
    expect(plan.allowTranscode).toBe(true);
    expect(plan.score).toBeLessThan(planFromFacts(facts({ container: 'matroska' }), options(chrome)).score);
  });

  it('HEVC the browser decodes in MKV is copied by the server (allowHevc)', () => {
    const plan = planFromFacts(facts({ container: 'matroska', video: hevcMain10 }), options(safari));
    expect(plan).toMatchObject({ kind: 'server-hls', allowHevc: true, allowTranscode: false });
  });

  it('Atmos on E-AC-3 is planned as E-AC-3, not TrueHD', () => {
    const atmos = facts({ audio: [audio('eac3', 'ec-3', { title: 'English Atmos', channels: 6 })] });
    expect(planFromFacts(atmos, options(safari)).kind).toBe('direct');
    expect(planFromFacts(atmos, options(chrome)).reason).toMatch(/EAC3 audio/);
  });

  it('an HLS source stays HLS (never escalated to server preparation)', () => {
    expect(planFromFacts(facts({ container: 'hls' }), options(chrome)).kind).toBe('hls');
  });

  it('unknown/legacy video codecs (mpeg4/xvid) need a transcode', () => {
    const xvid = facts({ container: 'avi', video: { index: 0, codec: 'mpeg4', profile: 'Advanced Simple', width: 720, height: 400, codec_string: null } });
    expect(planFromFacts(xvid, options(chrome)).kind).toBe('server-transcode');
  });

  it('a disabled codec+profile scope only affects that profile', () => {
    const disabled = new Set(['hevc:main 10']);
    expect(planFromFacts(facts({ video: hevcMain10 }), options(safari, { disabledScopes: disabled })).kind).toBe('server-transcode');
    const hevcMain = { ...hevcMain10, profile: 'Main', bit_depth: 8, codec_string: 'hvc1.1.6.L120.B0', width: 1920, height: 1080 };
    expect(planFromFacts(facts({ video: hevcMain }), options(safari, { disabledScopes: disabled })).kind).toBe('direct');
  });

  it('prefers server preparation when the preferred audio language is not the default track and the browser cannot switch', () => {
    const multi = facts({ audio: [audio('aac', 'mp4a.40.2', { language: 'jpn', default: true }), audio('aac', 'mp4a.40.2', { index: 2, language: 'eng', default: false })] });
    expect(planFromFacts(multi, options(chrome, { audioLanguage: 'en' })).kind).toBe('server-hls');
    expect(planFromFacts(multi, options(chrome, { audioLanguage: 'en', audioTrackSwitching: true })).kind).toBe('direct');
  });

  it('asks MediaCapabilities about the real stream: dimensions and codec strings', () => {
    const queries = decodeQueriesFor(facts({ video: hevcMain10 }), false);
    expect(queries[0]).toMatchObject({ kind: 'video', type: 'file', contentType: 'video/mp4; codecs="hvc1.2.4.L153.B0"', width: 3840, height: 2160 });
    expect(queries[1]).toMatchObject({ type: 'media-source' });
  });

  it('derives a conservative codec string when ffprobe gave none', () => {
    expect(videoCodecString({ index: 0, codec: 'hevc', bit_depth: 10 })).toBe('hvc1.2.4.L153.B0');
    expect(videoCodecString({ index: 0, codec: 'hevc', dynamic_range: 'dv', dv_profile: 5 })).toBe('dvh1.05.06');
  });
});

describe('planFromClassification (inspect failed)', () => {
  const caps: BrowserCaps = {
    h264: true, hevc: false, vp9: true, av1: true, aac: true, ac3: false, eac3: false, opus: true, nativeHls: false, mseAvailable: true,
  };
  const plan = (stream: PlaybackSource) => planFromClassification(stream, classifyStream(stream, caps), caps);

  it('maps codec-field classes onto plan kinds', () => {
    expect(plan(codecs('mp4', 'h264', 'aac')).kind).toBe('direct');
    expect(plan(codecs('mkv', 'h264', 'eac3', { audio_channels: 6 }))).toMatchObject({ kind: 'server-hls', surround: true });
    expect(plan(codecs('mkv', 'hevc', null, { resolution: '2160p', hdr: 'hdr10' })).kind).toBe('server-transcode');
    expect(plan(makeSource({ delivery: 'hls' })).kind).toBe('hls');
    expect(plan(makeSource({ url: '' })).kind).toBe('unplayable');
  });

  it('keeps server order except transcode-only sources, which go last', () => {
    const hevc = codecs('mkv', 'hevc', null, { resolution: '2160p' });
    const mkv = codecs('mkv', 'h264', 'aac');
    const mp4a = codecs('mp4', 'h264', 'aac');
    const mp4b = codecs('mp4', 'h264', 'aac');
    expect(orderByPlayability([hevc, mkv, mp4a, mp4b], plan)).toEqual([mkv, mp4a, mp4b, hevc]);
  });
});
