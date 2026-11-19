import { inspectPlaybackSources } from './accountClient';
import type { InspectResult, MediaFacts } from './types';

// ---------------------------------------------------------------------------
// Client cache of the server's ffprobe facts (POST /account/api/playback/inspect).
//
// The web player decides direct play vs server-prepared HLS from these facts
// instead of guessing codecs from file names. The server caches per URL for 6h;
// this layer adds an in-memory/sessionStorage copy so the Details prefetch makes
// Play instant, and shares in-flight requests between callers.
// ---------------------------------------------------------------------------

const TTL_MS = 2 * 60 * 60 * 1000;
const STORAGE_KEY = 'curatio-media-facts-v1';
const MAX_ENTRIES = 80;

type Entry = { at: number; result: InspectResult };

const memory = new Map<string, Entry>();
const inFlight = new Map<string, Promise<InspectResult | null>>();
let hydrated = false;

function hydrate() {
  if (hydrated) return;
  hydrated = true;
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    const parsed = raw ? (JSON.parse(raw) as Array<[string, Entry]>) : [];
    for (const [url, entry] of parsed) {
      if (entry && Date.now() - entry.at < TTL_MS) memory.set(url, entry);
    }
  } catch {
    // Warm-start convenience only.
  }
}

function persist() {
  try {
    // Only successful facts are worth keeping across reloads.
    const ok = [...memory.entries()].filter(([, entry]) => entry.result.state === 'ok').slice(-MAX_ENTRIES);
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(ok));
  } catch {
    // Quota or privacy mode.
  }
}

function remember(url: string, result: InspectResult) {
  memory.delete(url);
  memory.set(url, { at: Date.now(), result });
  while (memory.size > MAX_ENTRIES) {
    const oldest = memory.keys().next().value;
    if (oldest === undefined) break;
    memory.delete(oldest);
  }
  persist();
}

/** Cached inspect result for a URL (ok/failed/indeterminate), or null when unknown. */
export function peekInspect(url: string): InspectResult | null {
  if (!url) return null;
  hydrate();
  const entry = memory.get(url);
  if (!entry) return null;
  // Failures/timeouts are short-lived evidence; facts last the full TTL.
  const ttl = entry.result.state === 'ok' ? TTL_MS : 60_000;
  if (Date.now() - entry.at >= ttl) {
    memory.delete(url);
    return null;
  }
  return entry.result;
}

/** Cached facts for a URL, or null. Never fetches. */
export function peekMediaFacts(url: string): MediaFacts | null {
  const result = peekInspect(url);
  return result?.state === 'ok' ? result.media : null;
}

/**
 * Inspect up to four URLs, answering from cache when warm and sharing
 * in-flight requests. Never rejects: a failed request yields no entry, so the
 * caller falls back to filename classification.
 */
export async function inspectSources(urls: string[], signal?: AbortSignal): Promise<Map<string, InspectResult>> {
  const out = new Map<string, InspectResult>();
  const wanted = [...new Set(urls.filter(Boolean))].slice(0, 4);
  const missing: string[] = [];
  const pending: Array<Promise<void>> = [];
  for (const url of wanted) {
    const cached = peekInspect(url);
    if (cached) out.set(url, cached);
    else if (inFlight.has(url)) {
      pending.push(inFlight.get(url)!.then((result) => { if (result) out.set(url, result); }));
    } else missing.push(url);
  }
  if (missing.length) {
    // The shared request is not tied to one caller's signal: a Details prefetch
    // that unmounts must not cancel the facts the player is about to need.
    const request = inspectPlaybackSources(missing)
      .then((response) => response?.results ?? {})
      .catch(() => ({} as Record<string, InspectResult>));
    for (const url of missing) {
      const shared = request.then((results) => {
        const result = results[url];
        if (result && typeof result === 'object' && 'state' in result) {
          remember(url, result);
          return result;
        }
        return null;
      }).finally(() => inFlight.delete(url));
      inFlight.set(url, shared);
      pending.push(shared.then((result) => { if (result) out.set(url, result); }));
    }
  }
  if (!pending.length) return out;
  const all = Promise.all(pending).then(() => out);
  if (!signal) return all;
  if (signal.aborted) return out;
  return new Promise((resolve) => {
    const onAbort = () => resolve(out);
    signal.addEventListener('abort', onAbort, { once: true });
    void all.then((value) => {
      signal.removeEventListener('abort', onAbort);
      resolve(value);
    });
  });
}

/** Drop what we know about a URL (e.g. after it expired). */
export function forgetMediaFacts(url: string) {
  memory.delete(url);
  persist();
}

/** Test-only: reset the cache. */
export function resetMediaFactsCache() {
  memory.clear();
  inFlight.clear();
  hydrated = false;
  try {
    sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
}
