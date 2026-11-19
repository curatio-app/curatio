import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { getAccountMe, getBootstrap, isAbortError, selectProfile } from '../api/accountClient';
import type { AccountUser, CatalogRow, DashboardResponse } from '../api/types';
import { SERVERS_ROW_ID, displayTitle, setServerLibraryPresent } from '../api/mediaUtils';
import { showToast } from '../components/Toaster';

export function catalogKey(row: CatalogRow) {
  const rowType = String(
    row.type ?? row.media_type ?? row.catalog_type ?? row.items?.[0]?.media_type ?? 'mixed',
  );
  return `${row.id ?? row.name}:${rowType}:${row.name}`;
}

function uniqueCatalogRows(rows: CatalogRow[]) {
  const seen = new Set<string>();
  return rows.flatMap((row) => {
    const items = Array.isArray(row?.items)
      ? row.items.filter(
          (item) => item && (item.tmdb_id != null || item.id != null || displayTitle(item) !== 'Untitled'),
        )
      : [];
    if (!items.length) return [];
    const normalized = { ...row, items };
    const key = catalogKey(row);
    if (seen.has(key)) return [];
    seen.add(key);
    return [normalized];
  });
}

/**
 * The server's rows in order, de-duplicated. Home shows them all; the Movies and
 * Series tabs filter them per item (see homePresentation.ts).
 */
export function buildHomeRows(dashboard: DashboardResponse | null): CatalogRow[] {
  return uniqueCatalogRows([
    ...(dashboard?.catalogs ?? []),
    ...(dashboard?.movie_catalogs ?? []),
    ...(dashboard?.series_catalogs ?? []),
  ]);
}

export interface DashboardState {
  me: AccountUser | null;
  dashboard: DashboardResponse | null;
  rows: CatalogRow[];
  isLoading: boolean;
  error: string;
  reload: (options?: { silent?: boolean }) => Promise<void>;
  /** Refresh only the account/profile list (no full dashboard reload). */
  refreshMe: () => Promise<void>;
  /** Select a profile for this session, then reload its dashboard. Resolves
   * false (and keeps the current profile) when the switch fails. */
  switchProfile: (profileId: number) => Promise<boolean>;
}

export function useDashboard(): DashboardState {
  const [me, setMe] = useState<AccountUser | null>(null);
  const [dashboard, setDashboard] = useState<DashboardResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState('');

  // Monotonic id + abort so overlapping reloads (e.g. rapid profile switches)
  // can't render stale/other-profile data, and a hung fetch can't wedge the UI.
  const reloadIdRef = useRef(0);
  const reloadAbortRef = useRef<AbortController | null>(null);
  const lastToastRef = useRef(0);

  const reload = useCallback(async (options?: { silent?: boolean }) => {
    reloadAbortRef.current?.abort();
    const controller = new AbortController();
    reloadAbortRef.current = controller;
    const reqId = ++reloadIdRef.current;

    const silent = Boolean(options?.silent);
    if (!silent) {
      setIsLoading(true);
      setError('');
    }
    try {
      const bootstrap = await getBootstrap(controller.signal);
      if (reqId !== reloadIdRef.current) return; // superseded by a newer reload
      setError('');
      setMe(bootstrap.account);
      setServerLibraryPresent(
        (bootstrap.dashboard.rows ?? []).some((row) => row?.id === SERVERS_ROW_ID && (row.items?.length ?? 0) > 0),
      );
      setDashboard({
        ...bootstrap.dashboard,
        catalogs: bootstrap.dashboard.rows,
        movie_catalogs: [],
        series_catalogs: [],
      });
    } catch (err) {
      if (isAbortError(err) || reqId !== reloadIdRef.current) return; // cancelled
      const message = err instanceof Error ? err.message : 'Could not load the dashboard.';
      // A background refresh keeps what's on screen; only a first or explicit
      // load shows the full error page (AppShell shows it only without data).
      if (silent) {
        if (Date.now() - lastToastRef.current > 60_000) {
          lastToastRef.current = Date.now();
          showToast("Couldn't refresh. Showing your last update.");
        }
      }
      else setError(message);
    } finally {
      if (reqId === reloadIdRef.current) setIsLoading(false);
    }
  }, []);

  const refreshMe = useCallback(async () => {
    try {
      const account = await getAccountMe();
      setMe(account);
    } catch (err) {
      if (!isAbortError(err)) showToast("Couldn't refresh your profiles.");
    }
  }, []);

  const switchProfile = useCallback(
    async (profileId: number) => {
      try {
        await selectProfile(profileId);
      } catch (err) {
        if (!isAbortError(err)) showToast("Couldn't switch profile. Try again.");
        return false;
      }
      // Never leave the previous profile's rows on screen: if loading the new
      // profile fails, the error page (with Retry) shows instead.
      setDashboard(null);
      await reload();
      return true;
    },
    [reload],
  );

  useEffect(() => {
    void reload();
    return () => reloadAbortRef.current?.abort();
  }, [reload]);

  // Coming back to the tab after a while refreshes Continue Watching and rows
  // in the background (what react-query's refetchOnWindowFocus would do).
  useEffect(() => {
    let hiddenAt = 0;
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') {
        hiddenAt = Date.now();
      } else if (hiddenAt && Date.now() - hiddenAt > 60_000) {
        hiddenAt = 0;
        void reload({ silent: true });
      }
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, [reload]);

  // A freshly-created account shows "Preparing Your Rows" until its first
  // personalized sync lands. Poll (bounded) until the flag clears so the user
  // isn't stuck waiting for a manual refresh.
  useEffect(() => {
    if (!dashboard?.catalog_sync_pending) return;
    let attempts = 0;
    const maxAttempts = 20; // ~4 min at 12s
    const id = window.setInterval(() => {
      attempts += 1;
      if (attempts > maxAttempts) {
        window.clearInterval(id);
        return;
      }
      void reload({ silent: true });
    }, 12000);
    return () => window.clearInterval(id);
  }, [dashboard?.catalog_sync_pending, reload]);

  const rows = useMemo(() => buildHomeRows(dashboard), [dashboard]);

  return { me, dashboard, rows, isLoading, error, reload, refreshMe, switchProfile };
}

export function useDebouncedValue(value: string, delayMs: number) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(timer);
  }, [delayMs, value]);
  return debounced;
}
