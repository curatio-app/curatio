import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PlaybackPresenceReporter, type PresencePayload } from './playbackPresence';

describe('live playback presence', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());
  function setup() {
    const reports: PresencePayload[] = [];
    const reporter = new PlaybackPresenceReporter({ media_id: 'tt123:1:2', media_type: 'series' }, (p) => { reports.push(p); });
    return { reporter, reports };
  }
  it('reports observed state and heartbeats independently of position, including while paused', () => {
    const { reporter, reports } = setup();
    reporter.transition('playing'); reporter.transition('paused');
    vi.advanceTimersByTime(30_000);
    expect(reports.map((p) => p.state)).toEqual(['starting', 'playing', 'paused', 'paused', 'paused']);
    expect(reports.map((p) => p.sequence)).toEqual([0, 1, 2, 3, 4]);
    reporter.stop();
  });
  it('retains a session across source changes and never sends a source URL', () => {
    const { reporter, reports } = setup(); const session = reporter.sessionId;
    reporter.beginAttempt(); reporter.transition('playing');
    reporter.updateContext({ source_id: 'opaque-route-id', selected_provider: 'Jellyfin' });
    reporter.beginAttempt(); reporter.transition('buffering'); reporter.transition('playing');
    expect(reports.every((p) => p.session_id === session)).toBe(true);
    expect(reports.at(-1)?.source_attempt).toBe(2);
    reporter.stop();
  });
  it('does not revive completed playback with heartbeat or late player callbacks', () => {
    const { reporter, reports } = setup();
    reporter.transition('ended'); reporter.transition('playing');
    vi.advanceTimersByTime(45_000); reporter.stop(); reporter.beginAttempt();
    expect(reports.map((p) => p.state)).toEqual(['starting', 'ended']);
  });
  it('creates a new session for explicit playback after terminal failure', () => {
    const { reporter, reports } = setup(); const first = reporter.sessionId;
    reporter.transition('failed'); reporter.beginAttempt();
    expect(reporter.sessionId).not.toBe(first);
    expect(reports.at(-1)?.sequence).toBe(0);
    reporter.stop();
  });
  it('allocates the retry session before discovery so the request can be correlated', () => {
    const { reporter, reports } = setup(); const first = reporter.sessionId;
    reporter.transition('failed'); reporter.beginDiscovery();
    const discoveryId = reporter.sessionId;
    expect(discoveryId).not.toBe(first);
    reporter.beginAttempt(); reporter.transition('playing');
    expect(reporter.sessionId).toBe(discoveryId);
    expect(reports.at(-1)?.source_attempt).toBe(1);
    reporter.stop();
  });
  it('isolates reporting errors from playback', () => {
    const reporter = new PlaybackPresenceReporter({ media_id: 'tt1', media_type: 'movie' }, () => { throw Error('offline'); });
    expect(() => { reporter.transition('playing'); vi.advanceTimersByTime(15_000); reporter.stop(); }).not.toThrow();
  });
});
