import { FormEvent, useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { ExternalLink, LogIn, RefreshCw, Settings, Trash2 } from 'lucide-react';
import { adminApi, formatDate, postAdmin } from '../api';
import { Button, ConfirmButton, EmptyPanel, ErrorPanel, Field, Input, LoadingPanel, Notice, PageHeader, Panel, ReconnectNotice, StatusPill } from '../components';

// Integrations: the owner's Plex and Jellyfin servers (the titles Curatio can
// play) plus any separately installed source addons. Addons serve their own
// settings UI at /admin/addons/{id}/; the admin shell frames it at
// /admin/integrations/{id} (see AddonPage).

export type Health = 'ok' | 'degraded' | 'offline' | string;

export interface LibraryStatus {
  id: string;
  label: string;
  kind: 'library' | 'addon';
  connected: boolean;
  enabled: boolean;
  health: Health;
  server_name?: string | null;
  server_url?: string | null;
  last_sync_at?: string | null;
  last_sync_error?: string | null;
  last_sync_counts?: { titles?: number; movies?: number; shows?: number; removed?: number } | null;
}

export interface Integration {
  id: string;
  label: string;
  kind: 'library' | 'addon';
  enabled: boolean;
  health: Health;
  searchable: boolean;
  settings_url: string | null;
  summary: Record<string, unknown>;
}

export interface IntegrationsResponse {
  sdk_version: string | number;
  integrations: Integration[];
  rejected_addons: Array<{ entry_point: string; reason: string }>;
  source_cache: { entries?: number; max?: number };
}

interface PlexConnection { uri: string; local: boolean }
interface PlexServer { id: string; name: string; owned: boolean; connections: PlexConnection[] }
type PinStatus = { status: 'pending' } | { status: 'signed_in'; servers: PlexServer[] };

const PIN_POLL_MS = 2000;

function healthTone(health: Health, enabled = true): 'good' | 'warn' | 'bad' | 'neutral' {
  if (!enabled) return 'neutral';
  return health === 'ok' ? 'good' : health === 'offline' ? 'bad' : 'warn';
}

function errorText(error: unknown) {
  return error instanceof Error ? error.message : 'Request failed';
}

function useInvalidateIntegrations() {
  const client = useQueryClient();
  return () => {
    void client.invalidateQueries({ queryKey: ['admin-integrations'] });
    void client.invalidateQueries({ queryKey: ['admin-integration'] });
    void client.invalidateQueries({ queryKey: ['admin-overview'] });
  };
}

/** Status, last sync, counts and the shared Sync now / Enable / Disconnect actions. */
function LibraryStatusBlock({ status, providerId }: { status: LibraryStatus; providerId: string }) {
  const invalidate = useInvalidateIntegrations();
  const [notice, setNotice] = useState<{ tone: 'good' | 'bad'; text: string } | null>(null);
  const action = useMutation({
    mutationFn: async (name: 'sync' | 'enable' | 'disable' | 'disconnect') => {
      setNotice(null);
      const base = `/integrations/${providerId}`;
      if (name === 'sync') return { name, result: await postAdmin<LibraryStatus & { result?: { titles?: number } }>(`${base}/sync`) };
      if (name === 'disconnect') return { name, result: await postAdmin<LibraryStatus>(`${base}/disconnect`) };
      return { name, result: await postAdmin<LibraryStatus>(`${base}/enabled`, { enabled: name === 'enable' }) };
    },
    onSuccess: ({ name }) => {
      const text = name === 'sync' ? 'Library synced.' : name === 'disconnect' ? `${status.label} disconnected.` : name === 'enable' ? `${status.label} enabled.` : `${status.label} disabled.`;
      setNotice({ tone: 'good', text });
      invalidate();
    },
    onError: (error) => setNotice({ tone: 'bad', text: errorText(error) }),
  });
  const counts = status.last_sync_counts;
  return <>
    <div className="statusLine"><span>Status</span><StatusPill tone={status.connected ? healthTone(status.health, status.enabled) : 'neutral'}>{!status.connected ? 'Not connected' : !status.enabled ? 'Disabled' : status.health}</StatusPill></div>
    {status.connected ? <>
      <div className="statusLine"><span>Server</span><strong>{status.server_name || 'Unnamed server'}{status.server_url ? <small> · {status.server_url}</small> : null}</strong></div>
      <div className="statusLine"><span>Last sync</span><strong>{formatDate(status.last_sync_at)}</strong></div>
      <div className="statusLine"><span>Titles</span><strong>{counts ? `${(counts.titles ?? 0).toLocaleString()} (${(counts.movies ?? 0).toLocaleString()} movies · ${(counts.shows ?? 0).toLocaleString()} shows)` : '—'}</strong></div>
      {status.last_sync_error ? <Notice tone="bad" role="alert">Last sync failed: {status.last_sync_error}</Notice> : null}
      <div className="formActions">
        <Button variant="secondary" disabled={action.isPending || !status.enabled} onClick={() => action.mutate('sync')}><RefreshCw size={16} /> Sync now</Button>
        <Button variant="secondary" disabled={action.isPending} onClick={() => action.mutate(status.enabled ? 'disable' : 'enable')}>{status.enabled ? 'Disable' : 'Enable'}</Button>
        <ConfirmButton variant="danger" disabled={action.isPending} prompt={`Disconnect ${status.label}? Curatio forgets this server and its titles stop being playable until you connect again.`} onConfirm={() => action.mutate('disconnect')}>Disconnect</ConfirmButton>
      </div>
    </> : null}
    {notice ? <Notice tone={notice.tone} role={notice.tone === 'bad' ? 'alert' : 'status'}>{notice.text}</Notice> : null}
  </>;
}

export function PlexCard() {
  const invalidate = useInvalidateIntegrations();
  const status = useQuery({ queryKey: ['admin-integration', 'plex'], queryFn: () => adminApi<LibraryStatus>('/integrations/plex') });
  const [pinActive, setPinActive] = useState(false);
  const [authUrl, setAuthUrl] = useState('');
  const [servers, setServers] = useState<PlexServer[] | null>(null);
  const [serverId, setServerId] = useState('');
  const [uri, setUri] = useState('');
  const [manualOpen, setManualOpen] = useState(false);
  const [manualUrl, setManualUrl] = useState('');
  const [manualToken, setManualToken] = useState('');
  const [error, setError] = useState('');

  const pin = useQuery({
    queryKey: ['admin-integration', 'plex', 'pin'],
    queryFn: () => adminApi<PinStatus>('/integrations/plex/pin'),
    enabled: pinActive,
    refetchInterval: pinActive ? PIN_POLL_MS : false,
    refetchIntervalInBackground: true,
    retry: false,
  });
  useEffect(() => {
    if (pin.data?.status === 'signed_in') {
      setPinActive(false);
      setServers(pin.data.servers);
    } else if (pin.error) {
      setPinActive(false);
      setError(errorText(pin.error));
    }
  }, [pin.data, pin.error]);

  const startPin = useMutation({
    mutationFn: () => postAdmin<{ pin_id: number; auth_url: string }>('/integrations/plex/pin'),
  });
  function signIn() {
    setError('');
    setServers(null);
    // Open the window inside the click so the browser doesn't block it, then point it at plex.tv.
    const popup = window.open('', '_blank');
    startPin.mutate(undefined, {
      onSuccess: (result) => {
        setAuthUrl(result.auth_url);
        if (popup) popup.location.href = result.auth_url;
        setPinActive(true);
      },
      onError: (err) => {
        popup?.close();
        setError(errorText(err));
      },
    });
  }

  const loadServers = useMutation({
    mutationFn: () => adminApi<{ servers: PlexServer[] }>('/integrations/plex/servers'),
    onSuccess: (result) => { setError(''); setServers(result.servers); },
    onError: (err) => setError(errorText(err)),
  });
  const server = servers?.find((entry) => entry.id === serverId) ?? null;
  useEffect(() => {
    if (servers?.length && !servers.some((entry) => entry.id === serverId)) setServerId(servers[0].id);
  }, [servers, serverId]);
  useEffect(() => {
    if (server && !server.connections.some((entry) => entry.uri === uri)) setUri(server.connections[0]?.uri ?? '');
  }, [server, uri]);

  const connect = useMutation({
    mutationFn: () => postAdmin<LibraryStatus>('/integrations/plex/connect', { server_id: serverId, uri }),
    onSuccess: () => { setError(''); setServers(null); setAuthUrl(''); invalidate(); },
    onError: (err) => setError(errorText(err)),
  });
  const connectManual = useMutation({
    mutationFn: () => postAdmin<LibraryStatus>('/integrations/plex/connect-manual', { server_url: manualUrl.trim(), token: manualToken.trim() }),
    onSuccess: () => { setError(''); setManualToken(''); setManualOpen(false); invalidate(); },
    onError: (err) => setError(errorText(err)),
  });
  function submitManual(event: FormEvent) {
    event.preventDefault();
    if (manualUrl.trim() && manualToken.trim()) connectManual.mutate();
  }

  const connected = Boolean(status.data?.connected);
  return <Panel title="Plex" description="Sign in with Plex, then choose the server whose movies and shows Curatio should play.">
    {status.isLoading && !status.data ? <LoadingPanel label="Loading Plex status" /> : null}
    {status.error && !status.data ? <ErrorPanel error={status.error} onRetry={() => void status.refetch()} /> : null}
    {status.data ? <LibraryStatusBlock status={status.data} providerId="plex" /> : null}
    <div className="formActions">
      <Button variant={connected ? 'secondary' : 'primary'} onClick={signIn} disabled={startPin.isPending || pinActive}><LogIn size={16} /> {connected ? 'Sign in again' : 'Sign in with Plex'}</Button>
      <Button variant="ghost" onClick={() => loadServers.mutate()} disabled={loadServers.isPending}>Choose server</Button>
      <Button variant="ghost" onClick={() => setManualOpen((open) => !open)} aria-expanded={manualOpen}>Enter URL and token</Button>
    </div>
    {pinActive ? <Notice role="status">Waiting for you to approve Curatio on plex.tv…{authUrl ? <> <a href={authUrl} target="_blank" rel="noreferrer">Open plex.tv <ExternalLink size={13} /></a></> : null}</Notice> : null}
    {servers ? (servers.length ? <form className="stackForm" onSubmit={(event) => { event.preventDefault(); if (serverId && uri) connect.mutate(); }}>
      <Field label="Server"><select aria-label="Plex server" value={serverId} onChange={(event) => setServerId(event.target.value)}>{servers.map((entry) => <option key={entry.id} value={entry.id}>{entry.name}{entry.owned ? '' : ' (shared)'}</option>)}</select></Field>
      <Field label="Connection" hint="Choose an address the Curatio server can reach.">
        <select aria-label="Plex connection" value={uri} onChange={(event) => setUri(event.target.value)}>{(server?.connections ?? []).map((entry) => <option key={entry.uri} value={entry.uri}>{entry.uri}{entry.local ? ' (local)' : ''}</option>)}</select>
      </Field>
      <div className="formActions"><Button type="submit" disabled={connect.isPending || !serverId || !uri}>{connect.isPending ? 'Connecting…' : 'Connect'}</Button></div>
    </form> : <EmptyPanel title="No servers found" message="This Plex account has no servers Curatio can use." />) : null}
    {manualOpen ? <form className="stackForm" onSubmit={submitManual}>
      <Field label="Server URL"><Input aria-label="Plex server URL" type="url" placeholder="http://192.168.1.10:32400" value={manualUrl} onChange={(event) => setManualUrl(event.target.value)} required /></Field>
      <Field label="Plex token"><Input aria-label="Plex token" type="password" autoComplete="off" value={manualToken} onChange={(event) => setManualToken(event.target.value)} required /></Field>
      <div className="formActions"><Button type="submit" disabled={connectManual.isPending}>{connectManual.isPending ? 'Connecting…' : 'Connect'}</Button></div>
    </form> : null}
    {error ? <Notice tone="bad" role="alert">{error}</Notice> : null}
  </Panel>;
}

export function JellyfinCard() {
  const invalidate = useInvalidateIntegrations();
  const status = useQuery({ queryKey: ['admin-integration', 'jellyfin'], queryFn: () => adminApi<LibraryStatus>('/integrations/jellyfin') });
  const [serverUrl, setServerUrl] = useState('');
  const [mode, setMode] = useState<'password' | 'api_key'>('password');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [error, setError] = useState('');
  const connect = useMutation({
    mutationFn: () => postAdmin<LibraryStatus>('/integrations/jellyfin/connect', mode === 'api_key'
      ? { server_url: serverUrl.trim(), api_key: apiKey.trim() }
      : { server_url: serverUrl.trim(), username: username.trim(), password }),
    onSuccess: () => { setError(''); setPassword(''); setApiKey(''); invalidate(); },
    onError: (err) => setError(errorText(err)),
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    connect.mutate();
  }
  return <Panel title="Jellyfin" description="Connect with a Jellyfin user, or an API key from Jellyfin's dashboard.">
    {status.isLoading && !status.data ? <LoadingPanel label="Loading Jellyfin status" /> : null}
    {status.error && !status.data ? <ErrorPanel error={status.error} onRetry={() => void status.refetch()} /> : null}
    {status.data ? <LibraryStatusBlock status={status.data} providerId="jellyfin" /> : null}
    <form className="stackForm" onSubmit={submit}>
      <Field label="Server URL"><Input aria-label="Jellyfin server URL" type="url" placeholder="http://192.168.1.10:8096" value={serverUrl} onChange={(event) => setServerUrl(event.target.value)} required /></Field>
      <Field label="Sign in with"><select aria-label="Jellyfin sign-in method" value={mode} onChange={(event) => setMode(event.target.value === 'api_key' ? 'api_key' : 'password')}><option value="password">Username and password</option><option value="api_key">API key</option></select></Field>
      {mode === 'password' ? <div className="formGrid">
        <Field label="Username"><Input aria-label="Jellyfin username" autoComplete="off" value={username} onChange={(event) => setUsername(event.target.value)} required /></Field>
        <Field label="Password"><Input aria-label="Jellyfin password" type="password" autoComplete="off" value={password} onChange={(event) => setPassword(event.target.value)} /></Field>
      </div> : <Field label="API key"><Input aria-label="Jellyfin API key" type="password" autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} required /></Field>}
      <div className="formActions"><Button type="submit" disabled={connect.isPending}>{connect.isPending ? 'Connecting…' : status.data?.connected ? 'Reconnect' : 'Connect'}</Button></div>
    </form>
    {error ? <Notice tone="bad" role="alert">{error}</Notice> : null}
  </Panel>;
}

function AddonCard({ addon }: { addon: Integration }) {
  return <Panel title={addon.label} description={`Source addon · ${addon.id}`} actions={addon.settings_url ? <Link className="button secondary" to={`/integrations/${encodeURIComponent(addon.id)}`}><Settings size={14} /> Settings</Link> : undefined}>
    <div className="statusLine"><span>Status</span><StatusPill tone={addon.enabled ? 'good' : 'neutral'}>{addon.enabled ? 'Enabled' : 'Disabled'}</StatusPill></div>
    <div className="statusLine"><span>Health</span><StatusPill tone={healthTone(addon.health, addon.enabled)}>{addon.health}</StatusPill></div>
    <div className="statusLine"><span>Search</span><strong>{addon.searchable ? 'Finds titles outside your libraries' : 'Library titles only'}</strong></div>
    {typeof addon.summary?.error === 'string' ? <Notice tone="bad" role="alert">Status unavailable: {addon.summary.error}</Notice> : null}
  </Panel>;
}

export default function IntegrationsPage() {
  const invalidate = useInvalidateIntegrations();
  const list = useQuery({ queryKey: ['admin-integrations'], queryFn: () => adminApi<IntegrationsResponse>('/integrations') });
  const [cacheNotice, setCacheNotice] = useState('');
  const clearCache = useMutation({
    mutationFn: () => postAdmin<{ status: string }>('/integrations/cache/clear'),
    onSuccess: () => { setCacheNotice('Source cache cleared.'); invalidate(); },
    onError: (err) => setCacheNotice(errorText(err)),
  });
  const addons = (list.data?.integrations ?? []).filter((entry) => entry.kind === 'addon');
  const rejected = list.data?.rejected_addons ?? [];
  const cache = list.data?.source_cache;
  return <>
    <PageHeader
      title="Integrations"
      description="Connect Plex or Jellyfin so viewers can play the titles you own. Titles outside your libraries stay recommendations."
      actions={<Button variant="secondary" disabled={clearCache.isPending} onClick={() => { setCacheNotice(''); clearCache.mutate(); }}><Trash2 size={16} /> Clear source cache</Button>}
    />
    <ReconnectNotice reconnecting={Boolean(list.error && list.data)} onRetry={() => void list.refetch()} />
    {cacheNotice ? <p className="globalNotice" role="status">{cacheNotice}</p> : null}
    <div className="twoColumn">
      <PlexCard />
      <JellyfinCard />
    </div>
    <Panel title="Source addons" description={`Separately installed addons that add playback sources. Each one has its own settings page.${cache?.entries != null ? ` Source cache: ${cache.entries}${cache.max ? ` / ${cache.max}` : ''} entries.` : ''}`}>
      {list.isLoading && !list.data ? <LoadingPanel label="Loading addons" /> : null}
      {list.error && !list.data ? <ErrorPanel error={list.error} onRetry={() => void list.refetch()} /> : null}
      {list.data && !addons.length ? <EmptyPanel title="No addons installed" message="Curatio plays titles from Plex and Jellyfin. Install an addon on the server to add other sources." /> : null}
      {addons.map((addon) => <AddonCard key={addon.id} addon={addon} />)}
    </Panel>
    {rejected.length ? <Panel title="Rejected addons" description="These addons were found but not loaded." className="warningPanel">
      <ul className="alertList">{rejected.map((entry) => <li key={`${entry.entry_point}:${entry.reason}`}><strong>{entry.entry_point}</strong>: {entry.reason}</li>)}</ul>
    </Panel> : null}
  </>;
}
