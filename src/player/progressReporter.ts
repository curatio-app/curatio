import { reportPlaybackProgress } from '../api/accountClient';
import { displayTitle, playbackProgressKey } from '../api/mediaUtils';
import type { PlaybackProgressPayload, WebMediaItem } from '../api/types';
import { isCompletion } from './playbackMachine';

// ---------------------------------------------------------------------------
// Watch-progress reporting: every 10 s while playing, and immediately on pause,
// seek, tab hidden, pagehide and close. Hidden/pagehide/close use a keepalive
// request so the last position survives the tab going away.
// ---------------------------------------------------------------------------

export const PROGRESS_INTERVAL_MS = 10_000;

export type ProgressReason = 'interval' | 'pause' | 'seek' | 'hidden' | 'pagehide' | 'close' | 'ended' | 'switch';

export interface ProgressSnapshot {
  /** Movie time in seconds. */
  position: number;
  /** Best-known total runtime in seconds. */
  duration: number;
  creditsStart?: number | null;
}

export type ProgressSender = (payload: PlaybackProgressPayload, options: { keepalive: boolean }) => Promise<unknown>;

const PROGRESS_URL = '/account/api/progress';

/** Default transport: the API client normally, a keepalive fetch (or beacon) when the page may be going away. */
export const sendProgress: ProgressSender = async (payload, { keepalive }) => {
  if (!keepalive) return reportPlaybackProgress(payload);
  const body = JSON.stringify(payload);
  try {
    return await fetch(PROGRESS_URL, {
      method: 'POST',
      keepalive: true,
      credentials: 'same-origin',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body,
    });
  } catch {
    if (typeof navigator !== 'undefined' && typeof navigator.sendBeacon === 'function') {
      navigator.sendBeacon(PROGRESS_URL, new Blob([body], { type: 'application/json' }));
    }
    return undefined;
  }
};

export function progressPayload(item: WebMediaItem, position: number, duration: number): PlaybackProgressPayload {
  return {
    tmdb_id: item.tmdb_id,
    media_type: item.media_type,
    season: item.season ?? null,
    episode: item.episode ?? null,
    position: Math.max(0, Math.floor(position)),
    duration: Math.max(0, Math.floor(duration)),
    title: displayTitle(item),
  };
}

/** Local resume copy (fallback when server progress is unavailable). */
export function persistLocalProgress(item: WebMediaItem, snapshot: ProgressSnapshot) {
  try {
    const key = playbackProgressKey(item);
    if (isCompletion(snapshot.position, snapshot.duration, snapshot.creditsStart)) localStorage.removeItem(key);
    else {
      localStorage.setItem(key, JSON.stringify({
        position: Math.floor(snapshot.position),
        duration: Math.floor(snapshot.duration),
        updatedAt: Date.now(),
      }));
    }
  } catch {
    // Storage is a convenience only.
  }
}

export interface ProgressReporterOptions {
  item: WebMediaItem;
  /** Current position/duration, or null while nothing meaningful is loaded. */
  snapshot: () => ProgressSnapshot | null;
  /** Whether the element is currently playing (interval reports only then). */
  isPlaying: () => boolean;
  send?: ProgressSender;
  intervalMs?: number;
  /** Event targets (injectable for tests). */
  win?: Pick<Window, 'addEventListener' | 'removeEventListener'>;
  doc?: Pick<Document, 'addEventListener' | 'removeEventListener' | 'visibilityState'>;
}

export class ProgressReporter {
  private timer: ReturnType<typeof setInterval> | null = null;
  private lastSent: { position: number; at: number } | null = null;
  private readonly send: ProgressSender;
  private readonly onVisibility = () => {
    if (this.options.doc?.visibilityState === 'hidden') this.report('hidden');
  };
  private readonly onPageHide = () => this.report('pagehide');

  constructor(private readonly options: ProgressReporterOptions) {
    this.send = options.send ?? sendProgress;
  }

  private pending: Promise<void> | null = null;

  start() {
    if (this.timer) return;
    this.timer = setInterval(() => {
      if (this.options.isPlaying()) this.report('interval');
    }, this.options.intervalMs ?? PROGRESS_INTERVAL_MS);
    this.options.doc?.addEventListener('visibilitychange', this.onVisibility);
    this.options.win?.addEventListener('pagehide', this.onPageHide);
  }

  stop() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.options.doc?.removeEventListener('visibilitychange', this.onVisibility);
    this.options.win?.removeEventListener('pagehide', this.onPageHide);
  }

  /** Resolves once the latest report has reached the server (or failed); null when nothing was ever sent. */
  settled(): Promise<void> | null {
    return this.pending;
  }

  /** Report now. Returns whether anything was sent. */
  report(reason: ProgressReason): boolean {
    const snapshot = this.options.snapshot();
    if (!snapshot || !Number.isFinite(snapshot.position) || !Number.isFinite(snapshot.duration)) return false;
    if (snapshot.duration <= 0 || snapshot.position < 1) return false;
    const position = Math.floor(snapshot.position);
    // A paused title re-reported on the interval adds nothing.
    if (reason === 'interval' && this.lastSent && this.lastSent.position === position) return false;
    persistLocalProgress(this.options.item, snapshot);
    const keepalive = reason === 'hidden' || reason === 'pagehide' || reason === 'close';
    this.lastSent = { position, at: Date.now() };
    this.pending = this.send(progressPayload(this.options.item, snapshot.position, snapshot.duration), { keepalive }).then(() => undefined, () => undefined);
    return true;
  }
}
