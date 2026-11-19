import type { PlaybackSource } from './types';

let counter = 0;

/**
 * Test helper: a complete `PlaybackSource` (docs/playback-api.md) with
 * overrides. Library (Plex) by default; ids derive from the URL so two
 * fixtures for the same URL are the same route.
 */
export function makeSource(overrides: Partial<PlaybackSource> = {}): PlaybackSource {
  counter += 1;
  const url = overrides.url ?? `https://media.example/media/file-${counter}`;
  return {
    id: `src_${url}`,
    provider_id: 'plex',
    provider_label: 'Plex',
    kind: 'library',
    variant: 'original',
    delivery: 'file',
    url,
    headers: {},
    label: '',
    detail: null,
    badges: [],
    container: null,
    video_codec: null,
    audio_codec: null,
    hdr: null,
    resolution: null,
    audio_channels: null,
    languages: [],
    size_bytes: null,
    subtitles: [],
    route_id: `route:${url}`,
    file_id: null,
    startup_timeout_seconds: null,
    ...overrides,
  };
}

/** Shorthand: container + video + audio codecs, e.g. `codecs('mkv', 'hevc', 'eac3')`. */
export function codecs(container: string | null, video: string | null, audio: string | null, extra: Partial<PlaybackSource> = {}): PlaybackSource {
  return makeSource({ container, video_codec: video, audio_codec: audio, ...extra });
}
