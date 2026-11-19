import type { MediaFacts, RemuxStartResponse } from './types';

// ---------------------------------------------------------------------------
// `window.curatioDesktop`, exposed by the desktop app's preload
// (the Curatio desktop app's Electron preload). Absent in a browser.
//
// The desktop app plays with this same web player. All it adds is a local
// playback engine: sources the player would send to the server's remux service
// (MKV, TrueHD/DTS/AC-3 audio, ...) are prepared on this computer instead, with
// the same session API (see startRemuxSession).
// ---------------------------------------------------------------------------

export type EngineErrorCode = 'busy' | 'unsupported' | 'prepare' | 'timeout' | 'unavailable';

export type EngineResult<T> = { ok: true; value: T } | { ok: false; code: EngineErrorCode; message: string };

export interface EngineStartRequest {
  url: string;
  title: string | null;
  start: number;
  audio_language: string;
  /** The viewer's pick in the Audio menu: a source stream index (wins over the language). */
  audio_index?: number;
  allow_hevc: boolean;
  allow_transcode: boolean;
  surround: boolean;
  /** Request headers the source needs (the playback source's `headers`). */
  headers?: Record<string, string>;
  /** The server's probe of this source, when the player has it: the engine then skips its own. */
  facts?: MediaFacts;
}

export interface DesktopBridge {
  info: () => Promise<{ localEngine: boolean; platform: string; version: string; server: string | null }>;
  engine: {
    start: (request: EngineStartRequest) => Promise<EngineResult<RemuxStartResponse>>;
    keepalive: (session: string) => Promise<boolean>;
    stop: (session: string) => Promise<void>;
    /** Start preparing a source before Play is pressed; `start` adopts it. (Newer apps only.) */
    prepare?: (request: EngineStartRequest) => Promise<boolean>;
    /** Append a line to the app's playback log, beside the engine's own. (Newer apps only.) */
    log?: (line: string) => Promise<void>;
  };
}

declare global {
  interface Window {
    curatioDesktop?: Partial<DesktopBridge> & Record<string, unknown>;
  }
}

/** The bridge, when this page runs in a desktop app that has the local engine API. */
export function desktopBridge(): DesktopBridge | null {
  const bridge = typeof window !== 'undefined' ? window.curatioDesktop : undefined;
  return bridge && typeof bridge.info === 'function' && bridge.engine && typeof bridge.engine.start === 'function'
    ? (bridge as DesktopBridge)
    : null;
}

let engineReady: Promise<boolean> | null = null;
let engineKnown: boolean | null = null;

/** Whether the desktop app's local engine is installed and usable (asked once). */
export function localEngineAvailable(): Promise<boolean> {
  const bridge = desktopBridge();
  if (!bridge) return Promise.resolve(false);
  engineReady ??= bridge.info().then(
    (info) => (engineKnown = Boolean(info?.localEngine)),
    () => (engineKnown = false),
  );
  return engineReady;
}

/**
 * Synchronous answer for source ordering: true in the desktop app with the
 * local engine. Before the app has answered, an app that exposes the engine
 * API is assumed to have it (the engine ships inside the app; when it is
 * missing, prepared sessions fall back to the server anyway).
 */
export function hasLocalEngine(): boolean {
  if (engineKnown != null) return engineKnown;
  if (!desktopBridge()) return false;
  void localEngineAvailable();
  return true;
}

/** Local engine sessions are prefixed so keepalive/stop go back to the engine. */
export function isLocalSession(session: string): boolean {
  return session.startsWith('local-');
}

/** For tests. */
export function resetDesktopBridge() {
  engineReady = null;
  engineKnown = null;
}

/**
 * Prewarm the local engine for a source the player is about to open (Details is
 * showing, or the next episode is near). Costs nothing in a browser.
 */
export function prewarmLocalSession(request: EngineStartRequest): void {
  if (!hasLocalEngine()) return;
  void desktopBridge()?.engine.prepare?.(request).catch(() => undefined);
}

/** A player trace line (`[curatio] ...`): to the console and, in the desktop app, its playback log. */
export function playbackTrace(event: string, detail: Record<string, unknown>): void {
  console.debug(`[curatio] ${event}`, detail);
  try {
    void desktopBridge()?.engine.log?.(`${event} ${JSON.stringify(detail)}`)?.catch(() => undefined);
  } catch {
    /* logging must never affect playback */
  }
}
