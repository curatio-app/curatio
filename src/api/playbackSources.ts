import { hasLocalEngine } from './desktopBridge';
import { resolvePlaybackSources } from './accountClient';
import { displayTitle, itemKind, playbackId, streamUrl, mediaType } from './mediaUtils';
import { classifyStream, isDolbyVisionDisabled, playableStreams, primeBrowserCaps } from './streamCompat';
import { isDolbyVision, providerIdentity, routeIdentity } from './streamMeta';
import type { Availability, PlaybackQuality, PlaybackSource, PlaybackSourcesRequest, WebMediaItem } from './types';

// ---------------------------------------------------------------------------
// Shared, cached source discovery — the web mirror of the native app's
// `container.loadStreams` + `streams-cache` (5 min TTL here, 40 entries, keyed by
// identity + requested quality). The pre-play source picker and the player both
// read through here, so choosing a source never costs a second resolve, and
// reopening a title within the TTL starts without any discovery round trip.
// ---------------------------------------------------------------------------

/** Requested playback quality; `default` sends null (the profile's saved choice). */
export type SourceTier = 'default' | PlaybackQuality;

/** A source chosen in the pre-play picker; the player starts on it as-is. */
export interface SourceSelection {
  /** `routeIdentity` of the chosen source. */
  sourceKey: string;
  tier: SourceTier;
}

export interface PlaybackSources {
  /** Browser-playable sources in server order (capability buckets only). */
  streams: PlaybackSource[];
  /** How many sources the server returned before browser filtering. */
  resolvedCount: number;
  /** The quality the server actually resolved (the profile default for `default`). */
  quality?: PlaybackQuality;
  availability?: Availability;
}

export interface LoadSourcesOptions {
  tier: SourceTier;
  profileId?: number;
  sessionId?: string;
  /** Bypass (and overwrite) the cache — the native forced refresh. */
  refresh?: boolean;
  /** Every source with a URL, in server order, as the iOS/iPadOS picker does.
   * Implied in the desktop app, whose local engine prepares what the browser
   * can't open (see api/desktopBridge). */
  native?: boolean;
  signal?: AbortSignal;
}

/** Signed source URLs are short-lived; re-request rather than cache for long. */
const TTL_MS = 5 * 60 * 1000;
const MAX_ENTRIES = 40;
const STORAGE_KEY = 'curatio-sources-v2';

type Entry = { at: number; value: PlaybackSources };

const memory = new Map<string, Entry>();
const inFlight = new Map<string, { promise: Promise<PlaybackSources>; controller: AbortController; waiters: number }>();
let hydrated = false;

function hydrate() {
  if (hydrated) return;
  hydrated = true;
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    const parsed = raw ? (JSON.parse(raw) as Array<[string, Entry]>) : [];
    for (const [key, entry] of parsed) {
      if (entry && Date.now() - entry.at < TTL_MS) memory.set(key, entry);
    }
  } catch {
    // Session storage is a warm-start convenience only.
  }
}

function persist() {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify([...memory.entries()]));
  } catch {
    // Quota or privacy mode — the in-memory cache still works.
  }
}

function store(key: string, value: PlaybackSources) {
  memory.delete(key);
  memory.set(key, { at: Date.now(), value });
  while (memory.size > MAX_ENTRIES) {
    const oldest = memory.keys().next().value;
    if (oldest === undefined) break;
    memory.delete(oldest);
  }
  persist();
}

export function sourcesCacheKey(item: WebMediaItem, tier: SourceTier, profileId?: number, native = false): string {
  return [playbackId(item), mediaType(item), tier, profileId ?? 'current', ...(native ? ['native'] : [])].join('|');
}

/** Cached sources for a title, or null when cold/expired. Never fetches. */
export function peekPlaybackSources(item: WebMediaItem, tier: SourceTier, profileId?: number, native = false): PlaybackSources | null {
  hydrate();
  const key = sourcesCacheKey(item, tier, profileId, native || hasLocalEngine());
  const entry = memory.get(key);
  if (!entry) return null;
  if (Date.now() - entry.at >= TTL_MS) {
    memory.delete(key);
    return null;
  }
  return entry.value;
}

function positiveInt(value: unknown): number | null {
  const number = Number(value);
  return Number.isInteger(number) && number >= 0 && String(value ?? '').trim() !== '' ? number : null;
}

export function sourcesRequest(item: WebMediaItem, options: Pick<LoadSourcesOptions, 'tier' | 'refresh' | 'sessionId' | 'profileId'>): PlaybackSourcesRequest {
  const series = itemKind(item) === 'series' || mediaType(item) === 'series';
  // `video_id` is a structured id ("tt123", "tt123:1:2"), never a release name.
  const videoParts = String(item.video_id ?? '').split(':');
  const imdbCandidate = String(item.imdb_id ?? '') || (/^tt\d+$/.test(videoParts[0]) ? videoParts[0] : '');
  const tmdb = Number(item.tmdb_id);
  return {
    type: series ? 'series' : 'movie',
    imdb_id: /^tt\d{5,12}$/.test(imdbCandidate) ? imdbCandidate : null,
    tmdb_id: Number.isInteger(tmdb) && tmdb > 0 ? tmdb : null,
    season: series ? positiveInt(item.season) ?? positiveInt(videoParts[1]) ?? 1 : null,
    episode: series ? positiveInt(item.episode) ?? positiveInt(videoParts[2]) ?? 1 : null,
    quality: options.tier === 'default' ? null : options.tier,
    refresh: Boolean(options.refresh),
    viewing_session_id: options.sessionId ?? null,
    title: displayTitle(item) || null,
    profile_id: options.profileId ?? null,
  };
}

async function resolve(item: WebMediaItem, options: LoadSourcesOptions, signal: AbortSignal): Promise<PlaybackSources> {
  const data = await resolvePlaybackSources(sourcesRequest(item, options), { signal });
  const resolved = Array.isArray(data.sources) ? data.sources : [];
  const meta = { resolvedCount: resolved.length, quality: data.quality, availability: data.availability };
  if (options.native) {
    // Like the iOS/iPadOS picker: keep the server's order (library first, then
    // addons, then quality) and only drop entries without a URL. In the desktop
    // app the local engine copies any H.264/HEVC/AV1 video and converts any
    // audio, so the only sources moved back are those whose video would need a
    // full re-encode.
    const withUrl = resolved.filter((stream) => Boolean(streamUrl(stream)));
    if (!hasLocalEngine()) return { streams: withUrl, ...meta };
    await primeBrowserCaps();
    const reencode = withUrl.filter((stream) => classifyStream(stream).status === 'incompatible');
    return { streams: [...withUrl.filter((stream) => !reencode.includes(stream)), ...reencode], ...meta };
  }
  // Correct HEVC support against the real GPU decoder before classifying.
  // The server's order is the ranking, as on tvOS/iOS. A browser only moves
  // back what it can't show as-is: Dolby Vision, then sources whose video only
  // a server transcode can play, kept as the last resort instead of hidden.
  await primeBrowserCaps();
  const playable = playableStreams(resolved);
  const transcodeLast = resolved.filter((stream) => !playable.includes(stream)
    && Boolean(streamUrl(stream))
    && classifyStream(stream).status === 'incompatible'
    && !(isDolbyVisionDisabled() && isDolbyVision(stream)));
  const dolbyVision = playable.filter(isDolbyVision);
  return {
    streams: [...playable.filter((stream) => !isDolbyVision(stream)), ...dolbyVision, ...transcodeLast],
    ...meta,
  };
}

/** Wait for a shared promise, letting one caller give up without cancelling the others. */
function awaitShared<T>(promise: Promise<T>, signal: AbortSignal | undefined, release: () => void): Promise<T> {
  if (!signal) return promise.finally(release);
  if (signal.aborted) {
    release();
    return Promise.reject(new DOMException('Aborted', 'AbortError'));
  }
  return new Promise<T>((resolvePromise, reject) => {
    const onAbort = () => {
      release();
      reject(new DOMException('Aborted', 'AbortError'));
    };
    signal.addEventListener('abort', onAbort, { once: true });
    promise.then(
      (value) => { signal.removeEventListener('abort', onAbort); release(); resolvePromise(value); },
      (err) => { signal.removeEventListener('abort', onAbort); release(); reject(err); },
    );
  });
}

/**
 * Resolve a title's playable sources: from cache when warm, otherwise one
 * shared request per key no matter how many callers ask concurrently. A
 * successful empty answer is cached (the server answered cleanly); errors never are.
 */
export function loadPlaybackSources(item: WebMediaItem, requested: LoadSourcesOptions): Promise<PlaybackSources> {
  const options = { ...requested, native: requested.native || hasLocalEngine() };
  const key = sourcesCacheKey(item, options.tier, options.profileId, options.native);
  if (!options.refresh) {
    const cached = peekPlaybackSources(item, options.tier, options.profileId, options.native);
    if (cached) return Promise.resolve(cached);
  }
  let shared = inFlight.get(key);
  if (!shared || options.refresh) {
    shared?.controller.abort();
    const controller = new AbortController();
    const record = {
      controller,
      waiters: 0,
      promise: resolve(item, options, controller.signal).then((value) => {
        store(key, value);
        return value;
      }).finally(() => {
        if (inFlight.get(key) === record) inFlight.delete(key);
      }),
    };
    // Keep an unobserved rejection (every waiter aborted) from surfacing globally.
    record.promise.catch(() => undefined);
    inFlight.set(key, record);
    shared = record;
  }
  const active = shared;
  active.waiters += 1;
  let released = false;
  return awaitShared(active.promise, options.signal, () => {
    if (released) return;
    released = true;
    active.waiters -= 1;
    // Abort the network request only when nobody is left waiting for it.
    if (active.waiters <= 0 && inFlight.get(key) === active) {
      active.controller.abort();
      inFlight.delete(key);
    }
  });
}

/** Drop cached sources for a title (all qualities), e.g. after every source failed. */
export function invalidatePlaybackSources(item: WebMediaItem, profileId?: number) {
  hydrate();
  for (const tier of ['default', 'original', 'optimized'] as const) {
    for (const native of [false, true]) memory.delete(sourcesCacheKey(item, tier, profileId, native));
  }
  persist();
}

/**
 * The same route in a refreshed result: same route id, else the same file on
 * the same provider. Used to re-resolve an expired signed URL without
 * switching to a different file.
 */
export function sameRoute(a: PlaybackSource, b: PlaybackSource): boolean {
  if (a.route_id && b.route_id) return a.route_id === b.route_id;
  if (a.file_id && a.file_id === b.file_id) return providerIdentity(a) === providerIdentity(b);
  return routeIdentity(a) === routeIdentity(b);
}

/** Find the renewed copy of `failed` in a refreshed list (a new URL for the same route), or null. */
export function findRenewedRoute(failed: PlaybackSource, refreshed: PlaybackSource[]): PlaybackSource | null {
  const match = refreshed.find((candidate) => sameRoute(failed, candidate));
  return match ?? null;
}

/** Test-only: reset all cache state. */
export function resetPlaybackSourcesCache() {
  memory.clear();
  inFlight.clear();
  hydrated = false;
  try {
    sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
}
