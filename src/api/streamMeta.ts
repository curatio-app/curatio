import type { PlaybackSource } from './types';

// ---------------------------------------------------------------------------
// Readers over the server's structured `PlaybackSource` fields
// (docs/playback-api.md). Display strings (`label`, `detail`, `badges`) are
// never parsed: every decision here reads a structured field.
// Kept dependency-free so failover ranking (sourceFailover.ts) and the source
// rows (mediaUtils.ts) can share them without a cycle.
// ---------------------------------------------------------------------------

/** Provider display name ("Plex", "Jellyfin", an addon's label), or ''. */
export function providerLabel(source: PlaybackSource): string {
  return String(source.provider_label ?? '').trim();
}

/** Stable lowercased provider id, or '' when the source has none. */
export function providerIdentity(source: PlaybackSource): string {
  return String(source.provider_id ?? '').trim().toLowerCase();
}

/** Dolby Vision has no browser ladder: deprioritized in failover, badged by the server. */
export function isDolbyVision(source: PlaybackSource): boolean {
  return String(source.hdr ?? '').toLowerCase() === 'dv';
}

/** Server startup budget for this source, capped at 45 s; null for the client default. */
export function startupTimeoutSeconds(source: PlaybackSource): number | null {
  const hint = Number(source.startup_timeout_seconds);
  return source.startup_timeout_seconds != null && Number.isFinite(hint) && hint > 0 ? Math.min(hint, 45) : null;
}

/**
 * Addon sources may resolve on demand (each request can cost the addon work),
 * so speculative probes and server inspection never touch them. Library
 * sources are signed `/media/` URLs on this server and are safe to probe.
 */
export function isOpaqueRoute(source: PlaybackSource): boolean {
  return source.kind === 'addon';
}

/** Sources the client never probes speculatively: addon routes and ones with a startup budget. */
export function skipsSpeculativeProbe(source: PlaybackSource): boolean {
  return isOpaqueRoute(source) || startupTimeoutSeconds(source) != null;
}

/** Exact attempt identity: survives reorderings and refreshes of the same route. */
export function routeIdentity(source: PlaybackSource): string {
  return source.route_id || source.id || source.url || '';
}

export function startupTimeoutMs(source: PlaybackSource): number {
  const seconds = startupTimeoutSeconds(source);
  return Math.min(45000, Math.max(isOpaqueRoute(source) ? 15000 : 0, seconds != null ? seconds * 1000 : 0));
}
