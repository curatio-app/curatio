/** Best-effort live state. Intentionally separate from durable progress queues. */
export type PresenceState = 'starting' | 'playing' | 'buffering' | 'paused' | 'stopped' | 'ended' | 'failed';
export interface PresenceContext {
  profile_id?: number;
  media_id: string; media_type: 'movie' | 'series'; title?: string; season?: number; episode?: number;
  tier?: 'low' | 'high'; position?: number; duration?: number; source_id?: string;
  selected_provider?: string; source_attempt?: number;
}
export interface PresencePayload extends PresenceContext {
  session_id: string; sequence: number; device_id: string; platform: string; state: PresenceState;
}
function uuid() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  // HTTP LAN installations do not expose randomUUID (a secure-context API).
  const values = globalThis.crypto.getRandomValues(new Uint8Array(16));
  values[6] = (values[6] & 15) | 64; values[8] = (values[8] & 63) | 128;
  const hex = Array.from(values, (v) => v.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
function deviceId() {
  try {
    const previous = localStorage.getItem('curatio.playback-device');
    if (previous) return previous;
    const value = uuid(); localStorage.setItem('curatio.playback-device', value); return value;
  } catch { return uuid(); }
}
async function deliver(payload: PresencePayload) {
  try {
    await fetch('/account/api/playback/presence', {
      method: 'POST', credentials: 'same-origin', keepalive: true,
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      signal: AbortSignal.timeout(4000),
    });
  } catch { /* A missed heartbeat must not interrupt playback or sign the user out. */ }
}
const terminal = (state: PresenceState) => ['stopped', 'ended', 'failed'].includes(state);

export class PlaybackPresenceReporter {
  sessionId = uuid();
  private sequence = 0;
  private state: PresenceState = 'starting';
  private disposed = false;
  private device = deviceId();
  private timer: ReturnType<typeof setInterval>;
  constructor(private context: PresenceContext, private send: (payload: PresencePayload) => void | Promise<void> = deliver) {
    this.emit();
    this.timer = setInterval(() => { if (!terminal(this.state)) this.emit(); }, 15_000);
  }
  updateContext(context: Partial<PresenceContext>) { this.context = { ...this.context, ...context }; }
  transition(state: PresenceState) {
    if (this.disposed || this.state === state || terminal(this.state)) return;
    this.state = state; this.emit();
  }
  beginDiscovery() {
    if (this.disposed) return;
    if (terminal(this.state)) {
      this.sessionId = uuid(); this.sequence = 0; this.context.source_attempt = 0;
      this.context.source_id = undefined; this.context.selected_provider = undefined;
    }
    this.state = 'starting'; this.emit();
  }
  beginAttempt() {
    if (this.disposed) return;
    // Explicit playback after a terminal failure/completion is a new viewing session.
    if (terminal(this.state)) { this.sessionId = uuid(); this.sequence = 0; this.context.source_attempt = 0; }
    this.context.source_attempt = (this.context.source_attempt ?? 0) + 1;
    this.state = 'starting'; this.emit();
  }
  stop() {
    if (this.disposed) return;
    if (!terminal(this.state)) { this.state = 'stopped'; this.emit(); }
    this.disposed = true; clearInterval(this.timer);
  }
  private emit() {
    if (this.disposed) return;
    const payload: PresencePayload = { ...this.context, session_id: this.sessionId,
      sequence: this.sequence++, device_id: this.device, platform: 'web', state: this.state };
    try { void Promise.resolve(this.send(payload)).catch(() => undefined); } catch { /* isolated */ }
  }
}
