import { desktopBridge, isLocalSession, localEngineAvailable, type EngineStartRequest } from './desktopBridge';
import type {
  HiddenTitle,
  MediaFacts,
  AccountUser,
  BrowseResponse,
  BootstrapResponse,
  ConfirmSourcePayload,
  DashboardResponse,
  DetailsResponse,
  FacetsResponse,
  InspectResponse,
  MediaKind,
  MediaTag,
  LibraryAddPayload,
  PlaybackProgressPayload,
  ApiVersionResponse,
  FailureReport,
  PlaybackSourcesRequest,
  PlaybackSourcesResponse,
  ProbeRequestPayload,
  ProbeResponse,
  Profile,
  ProfilePayload,
  ProfilesResponse,
  RemuxStartResponse,
  ServerLibraryResponse,
  ScrobblePayload,
  SegmentFeedbackPayload,
  SegmentsResponse,
  WatchedResponse,
  WebMediaItem,
} from './types';

const DASHBOARD_PATH = '/account/dashboard';

function redirectToLogin() {
  window.location.href = `/account/login?next=${encodeURIComponent(DASHBOARD_PATH)}`;
}

async function readJson<T>(response: Response): Promise<T> {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data && typeof data === 'object' ? data.detail ?? data.error : undefined;
    const message = typeof detail === 'string' ? detail
      : detail && typeof detail.message === 'string' ? detail.message : 'Request failed';
    throw Object.assign(new Error(message), { status: response.status });
  }
  return data as T;
}

/** Default per-request timeout. Browser fetch has none, so a hung/black-holed
 * upstream would otherwise leave the promise pending forever (permanent spinner). */
const DEFAULT_TIMEOUT_MS = 20000;

export interface ApiRequestOptions extends RequestInit {
  /** Override the default request timeout (ms). */
  timeoutMs?: number;
}

/** True for aborts caused by a caller signal (effect cleanup / navigation) — these
 * should be swallowed, not surfaced as an error. A timeout is a real failure. */
export function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}

/**
 * A signal that aborts when the caller's signal does (AbortError) or after
 * `timeoutMs` (TimeoutError). Built by hand because `AbortSignal.any` and
 * `AbortSignal.timeout` are missing on older Safari / iPadOS.
 */
export function requestSignal(callerSignal: AbortSignal | null | undefined, timeoutMs: number): { signal: AbortSignal; done: () => void } {
  const controller = new AbortController();
  const timer = setTimeout(() => {
    controller.abort(new DOMException('The request timed out.', 'TimeoutError'));
  }, timeoutMs);
  const onAbort = () => controller.abort(callerSignal?.reason ?? new DOMException('Aborted', 'AbortError'));
  if (callerSignal?.aborted) onAbort();
  else callerSignal?.addEventListener('abort', onAbort, { once: true });
  return {
    signal: controller.signal,
    done: () => {
      clearTimeout(timer);
      callerSignal?.removeEventListener('abort', onAbort);
    },
  };
}

/** Reads retry quietly: a dropped connection or a 502/503/504 from a busy
 * server gets two more tries with backoff before the screen shows an error. */
const READ_RETRIES = 2;
const RETRY_STATUS = new Set([502, 503, 504]);

function retryDelay(attempt: number) {
  return 500 * 2 ** attempt + Math.random() * 250;
}

function sleep(ms: number, signal?: AbortSignal | null) {
  return new Promise<void>((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener('abort', () => {
      clearTimeout(timer);
      reject(new DOMException('Aborted', 'AbortError'));
    }, { once: true });
  });
}

export async function apiRequest<T>(url: string, init: ApiRequestOptions = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase();
  const retries = method === 'GET' ? READ_RETRIES : 0;
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await apiRequestOnce<T>(url, init);
    } catch (err) {
      const status = (err as { status?: number }).status;
      const transient = err instanceof TypeError || (status != null && RETRY_STATUS.has(status));
      if (!transient || attempt >= retries || init.signal?.aborted) throw err;
      await sleep(retryDelay(attempt), init.signal);
    }
  }
}

async function apiRequestOnce<T>(url: string, init: ApiRequestOptions): Promise<T> {
  const { timeoutMs = DEFAULT_TIMEOUT_MS, signal: callerSignal, ...rest } = init;
  // Always bound the request with a timeout, and combine it with any caller signal
  // (e.g. an AbortController from an effect) so either can cancel the fetch.
  const { signal, done } = requestSignal(callerSignal, timeoutMs);

  let response: Response;
  try {
    response = await fetch(url, {
      ...rest,
      signal,
      headers: {
        Accept: 'application/json',
        ...(rest.body ? { 'Content-Type': 'application/json' } : {}),
        ...(rest.headers ?? {}),
      },
    });
  } catch (err) {
    // The timeout fires a TimeoutError; translate it to a friendly, retryable error.
    // A caller-initiated AbortError propagates as-is for the caller to swallow.
    if (err instanceof DOMException && err.name === 'TimeoutError') {
      throw new Error('The request timed out. Please try again.');
    }
    if (signal.reason instanceof DOMException && signal.reason.name === 'TimeoutError') {
      throw new Error('The request timed out. Please try again.');
    }
    throw err;
  } finally {
    done();
  }

  if (response.status === 401) {
    redirectToLogin();
    throw new Error('login required');
  }

  return readJson<T>(response);
}

export function getAccountMe(signal?: AbortSignal) {
  return apiRequest<AccountUser>('/account/api/me', { signal });
}

export function getBootstrap(signal?: AbortSignal) {
  return apiRequest<BootstrapResponse>('/account/api/bootstrap', { signal });
}

export function createProfile(payload: ProfilePayload) {
  return apiRequest<{ status: string; profile: Profile }>('/account/api/profiles', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function updateProfile(profileId: number, payload: ProfilePayload) {
  return apiRequest<{ status: string; profile: Profile }>(`/account/api/profiles/${profileId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
}

/** "Not for me": the title leaves every row and counts against similar ones. */
export function hideTitle(tmdbId: number, mediaType: 'movie' | 'tv') {
  return apiRequest<{ status: string; changed: boolean }>('/account/api/hidden', {
    method: 'POST',
    body: JSON.stringify({ tmdb_id: tmdbId, media_type: mediaType }),
  });
}

export function unhideTitle(tmdbId: number, mediaType: 'movie' | 'tv') {
  return apiRequest<{ status: string; changed: boolean }>(`/account/api/hidden/${mediaType}/${tmdbId}`, { method: 'DELETE' });
}

export function listHiddenTitles(signal?: AbortSignal) {
  return apiRequest<{ items: HiddenTitle[] }>('/account/api/hidden', { signal });
}

export type ServerLibrarySort = 'added' | 'title' | 'year' | 'popular';
export type ServerLibraryFilter = 'unwatched' | '4k';

/** Titles on the connected Plex/Jellyfin servers (owned only; addons never add any). */
export function getServerLibrary(
  options: { type?: 'movie' | 'tv'; sort?: ServerLibrarySort; filter?: ServerLibraryFilter | null; offset?: number; limit?: number },
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    sort: options.sort ?? 'added',
    offset: String(options.offset ?? 0),
    limit: String(options.limit ?? 60),
  });
  if (options.type) params.set('type', options.type);
  if (options.filter) params.set('filter', options.filter);
  return apiRequest<ServerLibraryResponse>(`/account/api/servers/library?${params.toString()}`, { signal });
}

export function deleteProfile(profileId: number) {
  return apiRequest<{ status: string }>(`/account/api/profiles/${profileId}`, {
    method: 'DELETE',
  });
}

/** Issue a new key for the profile; every app paired to it must pair again. */
export function rotateProfileKey(profileId: number) {
  return apiRequest<{ status: string; profile: Profile }>(`/account/api/profiles/${profileId}/rotate-key`, {
    method: 'POST',
  });
}

export function selectProfile(profileId: number) {
  return apiRequest<{ status: string; profile: Profile }>(`/account/api/profiles/${profileId}/select`, {
    method: 'POST',
  });
}

export interface SearchSection {
  id: string;
  title: string;
  items: WebMediaItem[];
}

export function searchDashboard(query: string, signal?: AbortSignal, tagIds: number[] = []) {
  const params = new URLSearchParams({ q: query });
  // Filtering happens in SQL, not on the returned page: a client-side filter
  // narrows the 40 rows that came back, which is a different (and wrong)
  // answer from narrowing the library.
  if (tagIds.length) params.set('tags', tagIds.join(','));
  return apiRequest<{
    items: WebMediaItem[];
    result_type?: string;
    tag?: MediaTag;
    /** Genre searches only: the genre ranked for this profile, as rows. */
    sections?: SearchSection[];
  }>(
    `/account/api/search?${params.toString()}`,
    { signal },
  );
}

// Short-lived read cache: reopening a title or the Browse page shows the last
// answer at once instead of a spinner. In-flight requests are shared.
const readCache = new Map<string, { at: number; value: Promise<unknown> }>();
const READ_CACHE_LIMIT = 60;

function cachedRead<T>(key: string, ttlMs: number, load: () => Promise<T>, signal?: AbortSignal): Promise<T> {
  const hit = readCache.get(key);
  if (hit && Date.now() - hit.at < ttlMs) return withAbort(hit.value as Promise<T>, signal);
  const value = load();
  readCache.set(key, { at: Date.now(), value });
  value.catch(() => {
    if (readCache.get(key)?.value === value) readCache.delete(key);
  });
  if (readCache.size > READ_CACHE_LIMIT) readCache.delete(readCache.keys().next().value as string);
  return withAbort(value, signal);
}

/** A shared request keeps running for others; this caller just stops waiting. */
function withAbort<T>(promise: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return promise;
  if (signal.aborted) return Promise.reject(new DOMException('Aborted', 'AbortError'));
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(new DOMException('Aborted', 'AbortError'));
    signal.addEventListener('abort', onAbort, { once: true });
    promise.then(resolve, reject).finally(() => signal.removeEventListener('abort', onAbort));
  });
}

/** Drop cached reads (after a profile switch or a library change). */
export function clearReadCache() {
  readCache.clear();
}

export function getFacets(mediaType: MediaKind, signal?: AbortSignal) {
  const url = `/account/api/facets?media_type=${encodeURIComponent(mediaType)}`;
  return cachedRead(url, 10 * 60_000, () => apiRequest<FacetsResponse>(url), signal);
}

export function browseByTags(
  tagIds: number[],
  options: { mediaType?: MediaKind; mode?: 'all' | 'any'; skip?: number; limit?: number } = {},
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    tags: tagIds.join(','),
    media_type: options.mediaType ?? 'movie',
    mode: options.mode ?? 'all',
  });
  if (options.skip) params.set('skip', String(options.skip));
  if (options.limit) params.set('limit', String(options.limit));
  return apiRequest<BrowseResponse>(`/account/api/browse?${params.toString()}`, { signal });
}

export function getDetails(item: WebMediaItem, signal?: AbortSignal) {
  // IMDb-keyed items have no tmdb_id; send imdb_id so the server resolves it. Omit
  // tmdb_id entirely when absent (an empty tmdb_id= fails the server's int parse).
  const params = new URLSearchParams();
  if (item.tmdb_id != null && item.tmdb_id !== '') {
    params.set('tmdb_id', String(item.tmdb_id));
  }
  if (item.imdb_id) {
    params.set('imdb_id', String(item.imdb_id));
  }
  params.set('media_type', String(item.media_type ?? 'movie'));
  const url = `/account/api/details?${params.toString()}`;
  return cachedRead(url, 5 * 60_000, () => apiRequest<DetailsResponse>(url), signal);
}

export function getSegments(videoId: string, signal?: AbortSignal) {
  return apiRequest<SegmentsResponse>(`/account/api/segments/${encodeURIComponent(videoId)}`, { signal });
}

/** Playback sources for one movie or episode (docs/playback-api.md). */
/**
 * Device hints for the sources request (see PlaybackHints on the server).
 * Browsers expose little: the screen's pixel height and, where supported, a
 * coarse downlink estimate (capped at 10 Mbps by the spec, so only a low value
 * is informative). Speakers are assumed stereo.
 */
export function playbackDeviceHints(
  env: { screenHeight?: number; pixelRatio?: number; downlinkMbps?: number | null } = {
    screenHeight: typeof window !== 'undefined' ? window.screen?.height : undefined,
    pixelRatio: typeof window !== 'undefined' ? window.devicePixelRatio : undefined,
    downlinkMbps:
      typeof navigator !== 'undefined'
        ? ((navigator as Navigator & { connection?: { downlink?: number } }).connection?.downlink ?? null)
        : null,
  },
): Pick<PlaybackSourcesRequest, 'max_bitrate_kbps' | 'max_resolution' | 'audio_channels'> {
  const pixels = (env.screenHeight ?? 0) * (env.pixelRatio ?? 1);
  const max_resolution = pixels >= 1800 ? '2160p' : pixels >= 1000 ? '1080p' : pixels > 0 ? '720p' : null;
  const downlink = env.downlinkMbps ?? null;
  // 10 is the spec's ceiling: it means "at least 10", not a measurement.
  const max_bitrate_kbps = downlink && downlink > 0 && downlink < 10 ? Math.max(500, Math.round(downlink * 750)) : null;
  return { max_bitrate_kbps, max_resolution, audio_channels: 2 };
}

export function resolvePlaybackSources(payload: PlaybackSourcesRequest, options: { signal?: AbortSignal } = {}) {
  return apiRequest<PlaybackSourcesResponse>('/account/api/playback/sources', {
    signal: options.signal,
    cache: 'no-store',
    method: 'POST',
    body: JSON.stringify({ ...playbackDeviceHints(), ...payload }),
  });
}

/** Best-effort provider-health signal emitted when playback abandons a source. */
export function reportStreamFailure(payload: FailureReport) {
  return apiRequest<{ status: string }>('/account/api/playback/failure', {
    method: 'POST',
    body: JSON.stringify(payload),
    timeoutMs: 5000,
  });
}

/** Unauthenticated client/server compatibility handshake. */
export async function fetchApiVersion(signal?: AbortSignal): Promise<ApiVersionResponse | null> {
  const response = await fetch('/api/version', { headers: { Accept: 'application/json' }, signal, credentials: 'same-origin' });
  if (response.status === 404) return null;
  if (!response.ok) throw Object.assign(new Error('Version check failed'), { status: response.status });
  return (await response.json()) as ApiVersionResponse;
}

/**
 * Server-side preflight (Phase A→B bridge). Asks the backend to Range-probe the
 * top candidate sources — the browser can't reliably probe media hosts itself
 * because of CORS. Supplying episode context also lets the backend feed a
 * verified-online source to the intro/credits fingerprint worker.
 */
export function probePlaybackSources(payload: ProbeRequestPayload, signal?: AbortSignal) {
  return apiRequest<ProbeResponse>('/account/api/playback/probe', {
    signal,
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

/**
 * Report the source that actually rendered a frame so the fingerprint worker
 * uses what the user truly watched (corrects the probe's pick after failover).
 * Fire-and-forget — failures must never disrupt playback.
 */
export function confirmActiveSource(payload: ConfirmSourcePayload) {
  return apiRequest<{ ok?: boolean }>('/account/api/playback/confirm-source', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export class RemuxBusyError extends Error {
  constructor(
    message = 'Compatibility playback is busy. Please try another source or retry shortly.',
    /** Server Retry-After, in milliseconds, when it sent one. */
    readonly retryAfterMs: number | null = null,
  ) {
    super(message);
    this.name = 'RemuxBusyError';
  }
}

export class RemuxUnsupportedError extends Error {
  constructor(
    message = 'This source requires full video transcoding, which is disabled.',
    /** Machine-readable hint, e.g. "desktop_app" when only a native player can play it. */
    readonly hint: string | null = null,
  ) {
    super(message);
    this.name = 'RemuxUnsupportedError';
  }
}

/** Server ffprobe facts for up to four source URLs (cached server-side for 6h). */
export function inspectPlaybackSources(urls: string[], signal?: AbortSignal) {
  return apiRequest<InspectResponse>('/account/api/playback/inspect', {
    signal,
    method: 'POST',
    body: JSON.stringify({ urls: urls.slice(0, 4) }),
    timeoutMs: 15000,
  });
}

/** The desktop engine's request for a source (shared by Play and the prewarm, so they match). */
export function localEngineRequest(
  sourceUrl: string,
  options: { title?: string; start?: number; audioLanguage?: string; audioIndex?: number | null; allowHevc?: boolean; allowTranscode?: boolean; headers?: Record<string, string>; facts?: MediaFacts | null },
): EngineStartRequest {
  return {
    url: sourceUrl,
    title: options.title ?? null,
    start: Math.max(0, options.start ?? 0),
    audio_language: options.audioLanguage ?? 'system',
    ...(Number.isInteger(options.audioIndex) ? { audio_index: options.audioIndex as number } : {}),
    allow_hevc: Boolean(options.allowHevc),
    allow_transcode: Boolean(options.allowTranscode),
    // Keep the source's channels: Chromium downmixes for stereo output itself,
    // and TrueHD/DTS-HD stay lossless (FLAC) only when not folded down here.
    surround: true,
    ...(options.headers && Object.keys(options.headers).length ? { headers: options.headers } : {}),
    ...(options.facts ? { facts: options.facts } : {}),
  };
}

export async function startRemuxSession(
  sourceUrl: string,
  options: {
    title?: string;
    start?: number;
    audioLanguage?: string;
    /** The viewer's pick in the Audio menu (a stream index from the probe facts). */
    audioIndex?: number | null;
    /** Copy HEVC (tagged hvc1) because this browser can decode it. */
    allowHevc?: boolean;
    /** Last resort: let the server transcode video to H.264 (≤1080p). */
    allowTranscode?: boolean;
    /** Keep up to 5.1 AAC channels. */
    surround?: boolean;
    /** Request headers the source needs (only the desktop engine reads the source itself). */
    headers?: Record<string, string>;
    /** The server's probe facts for this source, if known (the desktop engine then skips its own probe). */
    facts?: MediaFacts | null;
  } = {},
): Promise<RemuxStartResponse> {
  // Desktop app: the same session, prepared on this computer by the local engine.
  // The server is only used when the engine isn't installed.
  if (await localEngineAvailable()) {
    const result = await desktopBridge()!.engine.start(localEngineRequest(sourceUrl, options));
    if (result.ok) return result.value;
    if (result.code !== 'unavailable') {
      if (result.code === 'busy') throw new RemuxBusyError(result.message);
      if (result.code === 'unsupported') throw new RemuxUnsupportedError(result.message);
      throw new RemuxHttpError(result.message, result.code === 'timeout' ? 504 : 502);
    }
  }
  const response = await fetch('/account/api/playback/remux/start', {
    method: 'POST',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify({
      url: sourceUrl,
      title: options.title ?? null,
      start: Math.max(0, options.start ?? 0),
      audio_language: options.audioLanguage ?? 'system',
      ...(Number.isInteger(options.audioIndex) ? { audio_index: options.audioIndex } : {}),
      allow_hevc: Boolean(options.allowHevc),
      allow_transcode: Boolean(options.allowTranscode),
      surround: Boolean(options.surround),
    }),
    signal: AbortSignal.timeout(45000),
  });
  if (response.status === 401) {
    redirectToLogin();
    throw new Error('login required');
  }
  const data = await response.json().catch(() => ({})) as { detail?: unknown } & Partial<RemuxStartResponse>;
  // `detail` is a string, or {message, hint} for structured refusals.
  const detail = data.detail && typeof data.detail === 'object' ? data.detail as { message?: unknown; hint?: unknown } : null;
  const message = String((detail ? detail.message : data.detail) ?? 'Could not prepare compatible playback.');
  const hint = detail && typeof detail.hint === 'string' ? detail.hint : null;
  if (response.status === 429) {
    const retryAfter = Number(response.headers.get('Retry-After'));
    throw new RemuxBusyError(message, Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter * 1000 : null);
  }
  if (response.status === 422) throw new RemuxUnsupportedError(message, hint);
  if (!response.ok) throw new RemuxHttpError(message, response.status);
  return data as RemuxStartResponse;
}

/** Thrown with the HTTP status when a prepared-session request fails. */
export class RemuxHttpError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = 'RemuxHttpError';
  }
}

/**
 * Renew a prepared session's lease (sessions are reaped 90s after the last
 * asset request or keepalive). Resolves true while alive, false once the
 * server says the session is gone (404). Network failures resolve true: the
 * next tick retries and a real loss shows up as a fragment 404.
 */
export async function keepaliveRemuxSession(sessionId: string): Promise<boolean> {
  if (!sessionId) return false;
  if (isLocalSession(sessionId)) return Boolean(await desktopBridge()?.engine.keepalive(sessionId));
  try {
    const response = await fetch(`/account/api/playback/remux/${encodeURIComponent(sessionId)}/keepalive`, {
      method: 'POST',
      headers: { Accept: 'application/json' },
      signal: AbortSignal.timeout(10000),
    });
    return response.status !== 404;
  } catch {
    return true;
  }
}

export async function stopRemuxSession(sessionId: string): Promise<void> {
  if (!sessionId) return;
  if (isLocalSession(sessionId)) {
    await desktopBridge()?.engine.stop(sessionId).catch(() => undefined);
    return;
  }
  await fetch('/account/api/playback/remux/stop', {
    method: 'POST',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify({ session: sessionId }),
    keepalive: true,
  }).catch(() => undefined);
}

export function reportPlaybackProgress(payload: PlaybackProgressPayload) {
  return apiRequest<{ ok?: boolean } | unknown>('/account/api/progress', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

function pathId(value: string | number) {
  return encodeURIComponent(String(value));
}

/** Remove a saved title (`id` is a TMDB id or an IMDb `tt…` id). */
export function removeLibraryItem(mediaType: 'movie' | 'tv', id: string | number) {
  return apiRequest<{ status?: string }>(`/account/api/library/${mediaType}/${pathId(id)}`, { method: 'DELETE' });
}

/** Clear progress + completion: un-mark watched / remove from Continue Watching. */
export function removeProgress(mediaType: 'movie' | 'tv', id: string | number, season?: number | null, episode?: number | null) {
  const params = new URLSearchParams();
  if (season != null) params.set('season', String(season));
  if (episode != null) params.set('episode', String(episode));
  const suffix = params.toString() ? `?${params.toString()}` : '';
  return apiRequest<{ status?: string; removed?: boolean }>(`/account/api/progress/${mediaType}/${pathId(id)}${suffix}`, { method: 'DELETE' });
}

/** Mark a movie or an episode watched (the native scrobble). */
export function markWatched(payload: ScrobblePayload) {
  return apiRequest<{ status?: string }>('/account/api/scrobble', {
    method: 'POST',
    body: JSON.stringify({ ...payload, action: 'complete' }),
  });
}

export function getWatched(signal?: AbortSignal) {
  return apiRequest<WatchedResponse>('/account/api/watched', { signal });
}

export function sendSegmentFeedback(videoId: string, payload: SegmentFeedbackPayload) {
  return apiRequest<{ status?: string }>(`/account/api/segments/${pathId(videoId)}/feedback`, {
    method: 'POST',
    body: JSON.stringify(payload),
    timeoutMs: 5000,
  });
}

export function addLibraryItem(payload: LibraryAddPayload) {
  return apiRequest<{ ok?: boolean } | unknown>('/account/api/library', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}
