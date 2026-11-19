import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { WebMediaItem } from '../api/types';
import { ProgressReporter, type ProgressSender } from './progressReporter';

const movie: WebMediaItem = { tmdb_id: 7, media_type: 'movie', title: 'Seven' };

function events() {
  const listeners = new Map<string, () => void>();
  return {
    target: {
      addEventListener: (type: string, fn: () => void) => listeners.set(type, fn),
      removeEventListener: (type: string) => listeners.delete(type),
      visibilityState: 'visible' as DocumentVisibilityState,
    },
    fire: (type: string) => listeners.get(type)?.(),
    has: (type: string) => listeners.has(type),
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  const storage = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => { storage.set(key, value); },
    removeItem: (key: string) => { storage.delete(key); },
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function setup(position = { value: 120 }, playing = { value: true }) {
  const send = vi.fn<ProgressSender>(async () => undefined);
  const win = events();
  const doc = events();
  const reporter = new ProgressReporter({
    item: movie,
    snapshot: () => ({ position: position.value, duration: 6000 }),
    isPlaying: () => playing.value,
    send,
    win: win.target,
    doc: doc.target,
  });
  reporter.start();
  return { send, win, doc, reporter, position, playing };
}

describe('progress reporting', () => {
  it('reports every 10 s while playing, and skips unchanged paused positions', () => {
    const { send, position, playing } = setup();
    vi.advanceTimersByTime(10_000);
    expect(send).toHaveBeenCalledTimes(1);
    position.value = 130;
    vi.advanceTimersByTime(10_000);
    expect(send).toHaveBeenCalledTimes(2);
    playing.value = false;
    vi.advanceTimersByTime(30_000);
    expect(send).toHaveBeenCalledTimes(2);
  });

  it('tells when the last report has reached the server, even a failed one', async () => {
    const { send, reporter } = setup({ value: 0 });
    reporter.report('switch');
    expect(reporter.settled()).toBeNull(); // nothing was played: nothing to refresh
    const played = setup();
    played.send.mockRejectedValueOnce(new Error('offline'));
    played.reporter.report('switch');
    await expect(played.reporter.settled()).resolves.toBeUndefined();
    expect(send).not.toHaveBeenCalled();
  });

  it('reports immediately on pause and seek', () => {
    const { send, reporter } = setup();
    reporter.report('pause');
    reporter.report('seek');
    expect(send).toHaveBeenCalledTimes(2);
    expect(send.mock.calls[0][0]).toMatchObject({ tmdb_id: 7, position: 120, duration: 6000 });
    expect(send.mock.calls[0][1]).toEqual({ keepalive: false });
  });

  it('uses a keepalive request when the tab is hidden or the page is going away', () => {
    const { send, win, doc } = setup();
    doc.target.visibilityState = 'hidden';
    doc.fire('visibilitychange');
    win.fire('pagehide');
    expect(send.mock.calls.map(([, options]) => options.keepalive)).toEqual([true, true]);
  });

  it('stops listening on stop()', () => {
    const { send, win, reporter } = setup();
    reporter.stop();
    expect(win.has('pagehide')).toBe(false);
    vi.advanceTimersByTime(30_000);
    expect(send).not.toHaveBeenCalled();
  });

  it('keeps a local resume copy, cleared at completion', () => {
    const { reporter, position } = setup();
    reporter.report('pause');
    expect(JSON.parse(localStorage.getItem('curatio-playback:movie:7:0:0')!)).toMatchObject({ position: 120 });
    position.value = 5990;
    reporter.report('ended');
    expect(localStorage.getItem('curatio-playback:movie:7:0:0')).toBeNull();
  });
});
