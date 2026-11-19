import { afterEach, describe, expect, it, vi } from 'vitest';
import { parseVtt, SessionKeepalive, SidecarSubtitles } from './serverSession';

afterEach(() => {
  vi.useRealTimers();
});

describe('parseVtt', () => {
  it('reads cue timings and text, skipping headers and notes', () => {
    const cues = parseVtt('WEBVTT\n\nNOTE hi\n\n1\n00:01:02.500 --> 00:01:04.000 align:start\nHello\nthere\n\n01:00:00.000 --> 01:00:01.000\nLate\n');
    expect(cues).toEqual([
      { start: 62.5, end: 64, text: 'Hello\nthere' },
      { start: 3600, end: 3601, text: 'Late' },
    ]);
  });
});

describe('SessionKeepalive', () => {
  it('pings every 30 s and reports a gone session once', async () => {
    vi.useFakeTimers();
    const ping = vi.fn(async () => ping.mock.calls.length < 2);
    const gone = vi.fn();
    const keepalive = new SessionKeepalive('abc', gone, ping);
    keepalive.start();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(gone).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(gone).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(90_000);
    expect(ping).toHaveBeenCalledTimes(2);
  });
});

describe('SidecarSubtitles', () => {
  it('appends new cues shifted by the session start, without duplicates', async () => {
    const added: Array<{ start: number; end: number; text: string }> = [];
    const track = { mode: 'disabled', cues: null, addCue: (cue: { start: number; end: number; text: string }) => added.push(cue), removeCue: () => undefined };
    const video = { addTextTrack: vi.fn(() => track) } as unknown as HTMLVideoElement;
    let body = 'WEBVTT\n\n00:10:05.000 --> 00:10:06.000\nOne\n';
    const sidecars = new SidecarSubtitles(video, [{ index: 3, language: 'eng', url: '/sub_3.vtt' }], 600, {
      languageLabel: () => 'English',
      fetchText: async () => body,
      cueFactory: (start, end, text) => ({ start, end, text }) as unknown as TextTrackCue,
    });
    await sidecars.poll();
    body += '\n00:10:07.000 --> 00:10:08.000\nTwo\n';
    await sidecars.poll();
    expect(added).toEqual([{ start: 5, end: 6, text: 'One' }, { start: 7, end: 8, text: 'Two' }]);
    expect(video.addTextTrack).toHaveBeenCalledWith('subtitles', 'English', 'eng');
    sidecars.stop();
  });

  function fakeTrack() {
    const cues: Array<{ start: number; end: number; text: string }> = [];
    const track = {
      mode: 'disabled',
      get cues() { return cues; },
      addCue: (cue: { start: number; end: number; text: string }) => cues.push(cue),
      removeCue: (cue: { start: number; end: number; text: string }) => { cues.splice(cues.indexOf(cue), 1); },
    };
    return { track, cues };
  }
  const withTrack = (track: unknown, fetchText: () => Promise<string>, video: { currentTime: number } = { currentTime: 0 }) =>
    new SidecarSubtitles(Object.assign(video, { addTextTrack: () => track }) as unknown as HTMLVideoElement, [{ index: 3, language: 'eng', url: '/sub_3.vtt' }], 0, {
      languageLabel: () => 'English',
      fetchText,
      cueFactory: (start, end, text) => ({ start, end, text }) as unknown as TextTrackCue,
    });

  it('a cue first seen cut short (the file is still being written) is replaced, not doubled', async () => {
    const { track, cues } = fakeTrack();
    let body = 'WEBVTT\n\n00:00:05.000 --> 00:00:09.000\nI never said';
    const sidecars = withTrack(track, async () => body);
    await sidecars.poll();
    body = 'WEBVTT\n\n00:00:05.000 --> 00:00:09.000\nI never said that\nto anyone\n';
    await sidecars.poll();
    expect(cues).toEqual([{ start: 5, end: 9, text: 'I never said that\nto anyone' }]);
    sidecars.stop();
  });

  it('adds its cues again after the player cleared them (hls.js media recovery)', async () => {
    const { track, cues } = fakeTrack();
    const sidecars = withTrack(track, async () => 'WEBVTT\n\n00:00:05.000 --> 00:00:09.000\nOne\n');
    await sidecars.poll();
    cues.length = 0; // what hls.js does to every text track when it re-attaches the media
    await sidecars.poll();
    expect(cues).toEqual([{ start: 5, end: 9, text: 'One' }]);
    sidecars.stop();
  });

  it('refills a switched-off track that was cleared, as soon as the viewer turns it on', async () => {
    // A disabled text track reports no cue list, so only the cues can tell.
    const live: Array<{ text: string; track: unknown }> = [];
    const track = {
      mode: 'disabled',
      get cues() { return this.mode === 'disabled' ? null : live; },
      addCue(cue: { text: string; track: unknown }) { cue.track = track; live.push(cue); },
      removeCue: () => undefined,
    };
    const listeners: Array<() => void> = [];
    const video = { currentTime: 0, addTextTrack: () => track, textTracks: { addEventListener: (_: string, fn: () => void) => listeners.push(fn), removeEventListener: () => undefined } };
    const sidecars = new SidecarSubtitles(video as unknown as HTMLVideoElement, [{ index: 3, language: 'fra', url: '/sub_3.vtt' }], 0, {
      languageLabel: () => 'French',
      fetchText: async () => 'WEBVTT\n\n00:00:05.000 --> 00:00:09.000\nUn\n',
      cueFactory: (start, end, text) => ({ start, end, text, track: null }) as unknown as TextTrackCue,
    });
    sidecars.start();
    await vi.waitFor(() => expect(live).toHaveLength(1));
    live.splice(0).forEach((cue) => { cue.track = null; }); // hls.js attaching the media
    track.mode = 'showing';
    listeners.forEach((fn) => fn());
    await vi.waitFor(() => expect(live.map((cue) => cue.text)).toEqual(['Un']));
    sidecars.stop();
  });

  it('polls quickly until subtitles reach well ahead of the playhead, then slowly', async () => {
    const { track } = fakeTrack();
    const video = { currentTime: 100 };
    let body = 'WEBVTT\n\n00:01:45.000 --> 00:01:49.000\nNear\n';
    const sidecars = withTrack(track, async () => body, video);
    const delay = () => (sidecars as unknown as { nextDelay: () => number }).nextDelay();
    await sidecars.poll();
    expect(delay()).toBe(2000); // only 9 s of subtitles ahead
    body += '\n00:05:00.000 --> 00:05:04.000\nFar\n';
    await sidecars.poll();
    expect(delay()).toBe(20000);
    video.currentTime = 280; // a seek caught up with what was fetched
    expect(delay()).toBe(2000);
    sidecars.stop();
  });
});