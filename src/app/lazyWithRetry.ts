import { lazy, type ComponentType, type LazyExoticComponent } from 'react';

const RELOAD_KEY = 'curatio.chunkReloadAt';
const RELOAD_COOLDOWN_MS = 30_000;

/** True for the errors browsers raise when a hashed chunk is gone (a deploy
 * replaced it) or the network dropped mid-download. */
export function isChunkLoadError(error: unknown): boolean {
  const message = error instanceof Error ? `${error.name} ${error.message}` : String(error);
  return /ChunkLoadError|Loading chunk|dynamically imported module|Importing a module script failed|error loading dynamically imported module|Failed to fetch/i.test(
    message,
  );
}

function reloadOnce(): boolean {
  try {
    const last = Number(sessionStorage.getItem(RELOAD_KEY) || 0);
    if (Date.now() - last < RELOAD_COOLDOWN_MS) return false;
    sessionStorage.setItem(RELOAD_KEY, String(Date.now()));
  } catch {
    return false;
  }
  window.location.reload();
  return true;
}

/**
 * `React.lazy` that survives a flaky network and a redeploy: the import is
 * retried once after a short pause, and if the chunk is truly gone the page
 * reloads (at most once per 30s) to pick up the new build's file names.
 */
export function lazyWithRetry<T extends ComponentType<any>>( // eslint-disable-line @typescript-eslint/no-explicit-any
  factory: () => Promise<{ default: T }>,
  { retryDelayMs = 800 }: { retryDelayMs?: number } = {},
): LazyExoticComponent<T> & { preload: () => Promise<unknown> } {
  let pending: Promise<{ default: T }> | null = null;
  const load = () => {
    if (!pending) {
      pending = factory().catch(async (first) => {
        await new Promise((resolve) => window.setTimeout(resolve, retryDelayMs));
        try {
          return await factory();
        } catch (second) {
          pending = null;
          if (isChunkLoadError(second) && reloadOnce()) {
            // Keep Suspense showing its fallback while the page reloads.
            return new Promise<never>(() => {});
          }
          throw second ?? first;
        }
      });
    }
    return pending;
  };
  const component = lazy(load) as LazyExoticComponent<T> & { preload: () => Promise<unknown> };
  component.preload = () => load().catch(() => undefined);
  return component;
}
