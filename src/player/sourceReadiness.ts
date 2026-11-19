import { probePlaybackSources } from '../api/accountClient';
import { streamUrl } from '../api/mediaUtils';
import { providerIdentity, skipsSpeculativeProbe } from '../api/streamMeta';
import type { PlaybackSource, ProbeVerdict } from '../api/types';

// ---------------------------------------------------------------------------
// Probe-before-start — the web port of the native
// `PlayerSourceReadinessCoordinator.selectInitialSource`.
//
// Rather than attaching source #1 blind and failing over after a 10s fast-fail,
// probe a small shortlist in parallel and start the best-ranked source the
// moment it is known good. A probe that is merely slow is not a failure: at the
// deadline the preferred source still gets its full player startup budget.
// ---------------------------------------------------------------------------

/**
 * Source class from structured fields (lower = better): an original file
 * before a server-optimized copy, and a library source before an addon one.
 */
export function releasePriority(stream: PlaybackSource): number {
  return (stream.variant === 'optimized' ? 2 : 0) + (stream.kind === 'addon' ? 1 : 0);
}

/** A probe that says the provider/service itself is unavailable blames the whole provider (native). */
export function isProviderUnavailable(verdict: ProbeVerdict): boolean {
  return verdict.state === 'failed' && verdict.reason === 'provider_unavailable';
}

/**
 * Probe shortlist: the best source, then another provider of the same source
 * class, then a different source class, then ranked order — so one slow
 * preferred group cannot monopolize startup.
 */
export function probeTargets(ranked: number[], streams: PlaybackSource[], limit = 4): number[] {
  if (limit <= 0 || !ranked.length) return [];
  const first = ranked[0];
  const chosen = [first];
  const rest = ranked.slice(1);
  const otherProvider = rest.find((index) =>
    providerIdentity(streams[index]) !== providerIdentity(streams[first])
    && releasePriority(streams[index]) === releasePriority(streams[first]));
  if (otherProvider !== undefined) chosen.push(otherProvider);
  const fallback = rest.find((index) => releasePriority(streams[index]) !== releasePriority(streams[first]));
  if (fallback !== undefined && !chosen.includes(fallback)) chosen.push(fallback);
  for (const index of ranked) if (!chosen.includes(index)) chosen.push(index);
  return chosen.slice(0, limit);
}

export type ProbeFn = (url: string, signal: AbortSignal, withFingerprint: boolean) => Promise<ProbeVerdict>;

export interface SelectInitialOptions {
  signal?: AbortSignal;
  /** Overall wait budget; capped at 5s like the native coordinator. */
  budgetMs?: number;
  /** Episode context for the server's intro/credits fingerprint hand-off. */
  fingerprintContext?: { imdb_id: string; season: number; episode: number } | null;
  /** Called with every verdict as it lands, so failover can reuse it. */
  onVerdict?: (url: string, verdict: ProbeVerdict) => void;
  /** Called when a probe blames a directly attributed provider; it is excluded for the session. */
  onProviderUnavailable?: (provider: string) => void;
  /** Injectable for tests. */
  probe?: ProbeFn;
}

const INDETERMINATE: ProbeVerdict = { state: 'indeterminate', reason: 'network', status: null };

function defaultProbe(context: SelectInitialOptions['fingerprintContext']): ProbeFn {
  // One URL per request so each verdict lands independently instead of
  // waiting on the slowest member of a batch.
  return async (url, signal, withFingerprint) => {
    const response = await probePlaybackSources({ urls: [url], ...(withFingerprint && context ? context : {}) }, signal);
    return response.results[url] ?? INDETERMINATE;
  };
}

/**
 * Resolve the index to start on, or null when there is nothing left to try
 * (native returns nil and playback fails over to the error state). Server
 * order is the ranking; probe-failed sources and excluded providers are skipped.
 */
export function selectInitialSource(streams: PlaybackSource[], options: SelectInitialOptions = {}): Promise<number | null> {
  if (!streams.length) return Promise.resolve(null);
  const budgetMs = Math.min(Math.max(options.budgetMs ?? 5000, 0), 5000);
  const probe = options.probe ?? defaultProbe(options.fingerprintContext);
  const verdicts = new Map<number, ProbeVerdict>();
  const started = new Set<number>();
  const controller = new AbortController();
  let fingerprintSent = false;

  return new Promise<number | null>((resolve) => {
    let settled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const finish = (value: number | null) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      options.signal?.removeEventListener('abort', onAbort);
      // Pending probes are not evidence of failure; just stop waiting on them.
      controller.abort();
      resolve(value);
    };
    const onAbort = () => finish(null);
    if (options.signal?.aborted) return finish(null);
    options.signal?.addEventListener('abort', onAbort, { once: true });

    const excluded = new Set<string>();
    const ranked = () => streams.map((_, index) => index).filter((index) => {
      if (verdicts.get(index)?.state === 'failed') return false;
      const provider = providerIdentity(streams[index]);
      return !(provider && excluded.has(provider));
    });

    const evaluate = () => {
      if (settled) return;
      const order = ranked();
      // Every candidate failed its probe: nothing to start (native).
      if (!order.length) return finish(null);
      const best = order[0];
      const verdict = verdicts.get(best);
      const url = streamUrl(streams[best]);
      if (!url || skipsSpeculativeProbe(streams[best]) || verdict) return finish(best);
      const pending = order.filter((index) => !verdicts.has(index));
      for (const index of probeTargets(pending, streams)) start(index);
    };

    const start = (index: number) => {
      if (started.has(index)) return;
      const url = streamUrl(streams[index]);
      // Opaque or slow-start resolver routes must not be exercised speculatively.
      if (!url || skipsSpeculativeProbe(streams[index])) return;
      started.add(index);
      const withFingerprint = !fingerprintSent;
      fingerprintSent = true;
      probe(url, controller.signal, withFingerprint)
        .catch(() => INDETERMINATE)
        .then((verdict) => {
          if (settled) return;
          verdicts.set(index, verdict);
          options.onVerdict?.(url, verdict);
          const provider = providerIdentity(streams[index]);
          if (provider && isProviderUnavailable(verdict) && !excluded.has(provider)) {
            excluded.add(provider);
            options.onProviderUnavailable?.(provider);
          }
          evaluate();
        });
    };

    timer = setTimeout(() => finish(ranked()[0] ?? null), budgetMs);
    evaluate();
  });
}
