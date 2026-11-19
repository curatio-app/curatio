import { keepaliveRemuxSession } from '../api/accountClient';
import type { RemuxSubtitleTrack } from '../api/types';

// ---------------------------------------------------------------------------
// Server-prepared HLS session helpers: the lease keepalive (sessions are reaped
// 90 s after the last request, so we renew every 30 s even while paused) and
// the progressive WebVTT subtitle sidecars, appended as cues on TextTracks.
// ---------------------------------------------------------------------------

export const KEEPALIVE_INTERVAL_MS = 30_000;
export const SIDECAR_POLL_MS = 20_000;
/** Until subtitles are this far ahead of the playhead, poll quickly (start, seeks). */
export const SIDECAR_FAST_POLL_MS = 2_000;
const SIDECAR_AHEAD_S = 60;

/** Renews a session lease until stopped; calls `onGone` once if the server dropped it. */
export class SessionKeepalive {
  private timer: ReturnType<typeof setInterval> | null = null;
  private stopped = false;

  constructor(
    private readonly sessionId: string,
    private readonly onGone: () => void,
    private readonly ping: (id: string) => Promise<boolean> = keepaliveRemuxSession,
    private readonly intervalMs = KEEPALIVE_INTERVAL_MS,
  ) {}

  start() {
    if (this.timer || this.stopped) return;
    this.timer = setInterval(() => {
      void this.ping(this.sessionId).then((alive) => {
        if (!alive && !this.stopped) {
          this.stop();
          this.onGone();
        }
      });
    }, this.intervalMs);
  }

  stop() {
    this.stopped = true;
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }
}

export interface ParsedCue {
  start: number;
  end: number;
  text: string;
}

function parseTimestamp(value: string): number | null {
  const match = /^(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})$/.exec(value.trim());
  if (!match) return null;
  const [, hours, minutes, seconds, fraction] = match;
  return (Number(hours ?? 0) * 3600) + Number(minutes) * 60 + Number(seconds) + Number(fraction.padEnd(3, '0')) / 1000;
}

/** Minimal WebVTT cue parser (timings + text; settings and styling are ignored). */
export function parseVtt(source: string): ParsedCue[] {
  const cues: ParsedCue[] = [];
  const blocks = source.replace(/\r\n?/g, '\n').split(/\n{2,}/);
  for (const block of blocks) {
    const lines = block.split('\n').filter((line) => line.length > 0);
    const timingIndex = lines.findIndex((line) => line.includes('-->'));
    if (timingIndex < 0) continue;
    const [rawStart, rest] = lines[timingIndex].split('-->');
    const start = parseTimestamp(rawStart);
    const end = parseTimestamp((rest ?? '').trim().split(/\s+/)[0] ?? '');
    const text = lines.slice(timingIndex + 1).join('\n').trim();
    if (start == null || end == null || end <= start || !text) continue;
    cues.push({ start, end, text });
  }
  return cues;
}

/** Text tracks the player created for a previous session; hidden from menus. */
export const retiredTextTracks = new WeakSet<TextTrack>();
const trackRegistry = new WeakMap<HTMLVideoElement, Map<string, TextTrack>>();

function sidecarLabel(track: RemuxSubtitleTrack, languageLabel: (code?: string | null) => string): string {
  const parts = [languageLabel(track.language) || track.title || `Subtitle ${track.index}`];
  if (track.title && languageLabel(track.language) && !/forced|sdh/i.test(track.title)) parts.push(`(${track.title})`);
  if (track.forced) parts.push('Forced');
  return parts.join(' ');
}

function clearCues(track: TextTrack) {
  const cues = track.cues;
  if (!cues) return;
  for (const cue of Array.from(cues)) {
    try {
      track.removeCue(cue);
    } catch {
      // Cue already gone.
    }
  }
}

type CueFactory = (start: number, end: number, text: string) => TextTrackCue | null;

const defaultCueFactory: CueFactory = (start, end, text) => {
  if (typeof VTTCue === 'undefined') return null;
  return new VTTCue(start, end, text);
};

/**
 * Exposes a session's subtitle sidecars as selectable text tracks and keeps
 * appending cues as the server writes them. Cue times are movie time; the
 * element's local timeline starts at `base`, so cues are shifted by −base.
 */
export class SidecarSubtitles {
  private timer: ReturnType<typeof setTimeout> | null = null;
  /** Per track: cues added so far, by timing (a growing file can show a cue's text cut short first). */
  private readonly seen = new Map<TextTrack, Map<string, Array<{ text: string; cue: TextTrackCue | null }>>>();
  /** Local end time of the latest cue added (how far ahead subtitles reach). */
  private latestEnd = 0;
  private readonly entries: Array<{ meta: RemuxSubtitleTrack; track: TextTrack }> = [];
  private stopped = false;
  private controller = new AbortController();

  constructor(
    private readonly video: HTMLVideoElement,
    tracks: RemuxSubtitleTrack[],
    private readonly base: number,
    private readonly options: {
      languageLabel: (code?: string | null) => string;
      onTracksChanged?: () => void;
      fetchText?: (url: string, signal: AbortSignal) => Promise<string>;
      cueFactory?: CueFactory;
      intervalMs?: number;
    },
  ) {
    if (typeof video.addTextTrack !== 'function') return;
    let registry = trackRegistry.get(video);
    if (!registry) {
      registry = new Map();
      trackRegistry.set(video, registry);
    }
    for (const meta of tracks) {
      const label = sidecarLabel(meta, options.languageLabel);
      const key = `${meta.index}|${meta.language ?? ''}|${label}`;
      let track = registry.get(key);
      if (!track) {
        track = video.addTextTrack('subtitles', label, meta.language ?? '');
        registry.set(key, track);
      }
      clearCues(track);
      retiredTextTracks.delete(track);
      track.mode = 'disabled';
      this.entries.push({ meta, track });
      this.seen.set(track, new Map());
    }
  }

  get tracks(): TextTrack[] {
    return this.entries.map((entry) => entry.track);
  }

  start() {
    if (!this.entries.length || this.timer || this.stopped) return;
    const tick = async () => {
      await this.poll();
      if (this.stopped) return;
      this.timer = setTimeout(() => void tick(), this.nextDelay());
    };
    void tick();
    this.video.textTracks?.addEventListener?.('change', this.onTrackModeChange);
    this.options.onTracksChanged?.();
  }

  /**
   * The sidecar files grow as the session is prepared. Right after a start or a
   * seek they hold nothing for the playhead yet, so poll quickly until cues
   * reach well ahead of it, then settle to the slow cadence.
   */
  private nextDelay(): number {
    if (this.options.intervalMs != null) return this.options.intervalMs;
    const ahead = this.latestEnd - (Number.isFinite(this.video.currentTime) ? this.video.currentTime : 0);
    return ahead < SIDECAR_AHEAD_S ? SIDECAR_FAST_POLL_MS : SIDECAR_POLL_MS;
  }

  /** Fetch every sidecar once and append cues not seen before. */
  async poll() {
    const fetchText = this.options.fetchText ?? (async (url: string, signal: AbortSignal) => {
      const response = await fetch(url, { signal, cache: 'no-store' });
      return response.ok ? response.text() : '';
    });
    const makeCue = this.options.cueFactory ?? defaultCueFactory;
    await Promise.all(this.entries.map(async ({ meta, track }) => {
      let text = '';
      try {
        text = await fetchText(meta.url, this.controller.signal);
      } catch {
        return;
      }
      if (this.stopped) return;
      const seen = this.seen.get(track)!;
      // hls.js clears every text track's cues when it attaches or re-attaches
      // the media: add ours again.
      if (this.lostCues(track)) seen.clear();
      for (const cue of parseVtt(text)) {
        const key = `${cue.start}|${cue.end}`;
        const known = seen.get(key) ?? [];
        if (known.some((entry) => entry.text === cue.text)) continue;
        const start = cue.start - this.base;
        const end = cue.end - this.base;
        // The file is still being written: the last cue may have arrived with
        // its text cut short. The complete one replaces it (not a second cue).
        const partial = known.find((entry) => cue.text.startsWith(entry.text));
        if (partial) {
          if (partial.cue) {
            try {
              track.removeCue(partial.cue);
            } catch {
              // Already gone.
            }
          }
          known.splice(known.indexOf(partial), 1);
        }
        const entry: { text: string; cue: TextTrackCue | null } = { text: cue.text, cue: null };
        known.push(entry);
        seen.set(key, known);
        if (end <= 0) continue;
        this.latestEnd = Math.max(this.latestEnd, end);
        entry.cue = makeCue(Math.max(0, start), end, cue.text);
        if (entry.cue) {
          try {
            track.addCue(entry.cue);
          } catch {
            // Malformed cue; skip it.
          }
        }
      }
    }));
  }

  /**
   * True when cues added to `track` are no longer on it. A disabled track
   * reports no cue list at all, so ask the cues themselves.
   */
  private lostCues(track: TextTrack): boolean {
    const seen = this.seen.get(track);
    if (!seen || seen.size === 0) return false;
    if (track.cues) return track.cues.length === 0;
    for (const known of seen.values()) {
      for (const { cue } of known) {
        if (cue && 'track' in cue) return cue.track == null;
      }
    }
    return false;
  }

  /** A track was switched on or off: fill it at once if its cues were cleared meanwhile. */
  private readonly onTrackModeChange = () => {
    if (this.stopped || !this.entries.some(({ track }) => this.lostCues(track))) return;
    void this.poll();
  };

  stop() {
    this.stopped = true;
    this.video.textTracks?.removeEventListener?.('change', this.onTrackModeChange);
    this.controller.abort();
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    for (const { track } of this.entries) {
      track.mode = 'disabled';
      clearCues(track);
      retiredTextTracks.add(track);
    }
    this.options.onTracksChanged?.();
  }
}
