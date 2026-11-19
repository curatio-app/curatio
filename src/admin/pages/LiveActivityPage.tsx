import { useQuery } from '@tanstack/react-query';
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { adminApi, formatDate, formatDuration } from '../api';
import { Button, EmptyPanel, ErrorPanel, LoadingPanel, PageHeader, Panel, ReconnectNotice, SidePanel, StatGrid, StatusPill } from '../components';

interface Playback {
  session_id: string; media_id: string; title?: string; viewer: string; device_id: string;
  platform: string; tier: string; state: string; position?: number; duration?: number;
  season?: number; episode?: number; selected_provider?: string; serving_provider?: string;
  source_id?: string; source_attempt: number; ago_seconds: number;
}
interface SourceRequest {
  request_id: string; media_id: string; title?: string; viewer: string; tier: string; kind: string;
  state: string; elapsed_ms: number; result_count?: number; cache_hit?: boolean; errors?: string[]; error?: string;
}
interface Transfer {
  id: string; filename?: string; username?: string; transport: string; bytes_per_second?: number;
  bytes_served?: number; request_count?: number; last_seen?: string | number;
}
/** One provider's own transfer view (e.g. an addon's proxy), when it reports one. */
export interface UpstreamView {
  provider_id: string; label: string; state: string; streams: Transfer[];
  updated_at?: string | number | null; error?: string; summary?: Record<string, unknown>;
}
interface Activity {
  playback: Playback[]; requests: SourceRequest[]; counts: Record<string, number>; updated_at: string;
  upstream?: UpstreamView[];
}
function bytes(value: number | undefined) {
  if (value == null || !Number.isFinite(value)) return 'Unknown';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  const index = Math.min(4, Math.max(0, Math.floor(Math.log(Math.max(1, value)) / Math.log(1024))));
  return `${(value / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
}
function timestamp(value: string | number | null | undefined) {
  return value == null ? 'Unknown' : formatDate(typeof value === 'number' ? new Date(value < 1e11 ? value * 1000 : value).toISOString() : value);
}
function stateTone(state: string) {
  return state === 'playing' || state === 'ready' ? 'good' : state === 'failed' ? 'bad' : state === 'stale' || state === 'buffering' ? 'warn' : 'neutral';
}

function UpstreamPanel({ view }: { view: UpstreamView }) {
  const streams = view.streams ?? [];
  return <Panel title={`${view.label} transfers`} description={`Reported by ${view.label}. Transfer activity is separate from player state.`}>
    <p><StatusPill tone={view.state === 'available' ? 'good' : 'warn'}>{view.state}</StatusPill> {view.error || ''}{view.updated_at ? ` · Updated ${timestamp(view.updated_at)}` : ''}</p>
    {streams.length ? <div className="tableWrap"><table><thead><tr><th>File</th><th>Account</th><th>Transport</th><th>Throughput</th><th>Bytes served</th><th>Requests</th><th>Last activity</th></tr></thead><tbody>{streams.map((row) => <tr key={row.id}>
      <td data-label="File">{row.filename || 'Unknown file'}</td><td data-label="Account">{row.username || 'Unknown'}</td><td data-label="Transport">{row.transport}</td><td data-label="Throughput">{row.bytes_per_second != null ? `${bytes(row.bytes_per_second)}/s` : 'Unknown'}</td><td data-label="Bytes served">{bytes(row.bytes_served)}</td><td data-label="Requests">{row.request_count ?? 'Unknown'}</td><td data-label="Last activity">{timestamp(row.last_seen)}</td>
    </tr>)}</tbody></table></div> : view.state === 'available' ? <EmptyPanel title="No reported transfers" message="Playback can still be active above." /> : null}
  </Panel>;
}

export function LiveActivityPage() {
  const query = useQuery({ queryKey: ['admin-live-activity'], queryFn: () => adminApi<Activity>('/live-activity'), refetchInterval: 5000, refetchIntervalInBackground: false });
  const data = query.data;
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const location = useLocation();
  const sessionId = params.get('session');
  const session = data?.playback.find((row) => row.session_id === sessionId);
  function openSession(id: string) {
    const next = new URLSearchParams(params);
    next.set('session', id);
    navigate({ search: next.toString() }, { state: { panelFromList: true } });
  }
  function closeSession() {
    if ((location.state as { panelFromList?: boolean } | null)?.panelFromList) navigate(-1);
    else { const next = new URLSearchParams(params); next.delete('session'); navigate({ search: next.toString() }, { replace: true }); }
  }
  return <>
    <PageHeader title="Live Activity" description="Player reports, source requests, and provider transfers. Updates every five seconds while this page is visible." />
    <ReconnectNotice reconnecting={Boolean(query.error && data)} onRetry={() => void query.refetch()} />
    {query.isLoading && !data ? <LoadingPanel label="Loading activity" /> : null}
    {query.error && !data ? <ErrorPanel error={query.error} onRetry={() => void query.refetch()} /> : null}
    {data ? <>
      <StatGrid items={['playing', 'paused', 'buffering', 'starting'].map((state) => ({ label: state[0].toUpperCase() + state.slice(1), value: data.counts[state] ?? 0 }))} />
      <Panel title="Now playing" description={`Reported by Curatio players. Last updated ${formatDate(data.updated_at)}. Sessions become stale after 60 seconds without contact.`}>
        {data.playback.length ? <div className="tableWrap"><table><thead><tr><th>Title</th><th>Viewer / device</th><th>State</th><th>Position</th><th>Source</th><th>Last report</th><th><span className="srOnly">Actions</span></th></tr></thead><tbody>{data.playback.map((row) => <tr key={row.session_id}>
          <td data-label="Title"><strong>{row.title || row.media_id}</strong><small>{row.season != null ? `S${row.season} E${row.episode ?? '?'}` : row.media_id} · {row.tier}</small></td>
          <td data-label="Viewer / device">{row.viewer}<small>{row.platform} · {row.device_id.slice(0, 8)}</small></td>
          <td data-label="State"><StatusPill tone={stateTone(row.state)}>{row.state}</StatusPill></td>
          <td data-label="Position">{row.position != null ? formatDuration(row.position) : 'Unknown'} / {row.duration ? formatDuration(row.duration) : 'Unknown'}</td>
          <td data-label="Source">{row.selected_provider || 'Not selected'}<small>Serving provider: {row.serving_provider || 'Unknown'}</small><small>Source attempt {row.source_attempt || 0}{row.source_id ? ` · ${row.source_id.slice(0, 12)}` : ''}</small></td>
          <td data-label="Last report">{formatDuration(row.ago_seconds)} ago</td>
          <td data-label="Actions"><Button variant="secondary" size="compact" onClick={() => openSession(row.session_id)} aria-label={`Inspect session for ${row.title || row.media_id}`}>Inspect</Button></td>
        </tr>)}</tbody></table></div> : <EmptyPanel title="No recent player reports" message="Updated Curatio apps report playback here. Older apps appear only under source requests." />}
      </Panel>
      <Panel title="Source requests" description="In-flight searches and requests from the last 15 minutes. A lookup does not mean playback started.">
        {data.requests.length ? <div className="tableWrap"><table><thead><tr><th>Title</th><th>Viewer / tier</th><th>Status</th><th>Time</th><th>Results</th><th>Diagnostics</th></tr></thead><tbody>{data.requests.map((row) => <tr key={row.request_id}>
          <td data-label="Title">{row.title || row.media_id}{row.kind === 'test' ? <small>Connection test</small> : null}</td><td data-label="Viewer / tier">{row.viewer}<small>{row.tier}</small></td>
          <td data-label="Status"><StatusPill tone={stateTone(row.state)}>{row.state}</StatusPill></td><td data-label="Time">{(row.elapsed_ms / 1000).toFixed(1)}s</td>
          <td data-label="Results">{row.result_count ?? 'Pending'}{row.cache_hit ? <small>Cache hit</small> : null}</td><td data-label="Diagnostics">{[row.error, ...(row.errors ?? [])].filter(Boolean).join(' · ') || '—'}</td>
        </tr>)}</tbody></table></div> : <EmptyPanel title="No recent requests" message="Source searches and connection tests will appear here." />}
      </Panel>
      {(data.upstream ?? []).map((view) => <UpstreamPanel key={view.provider_id} view={view} />)}
    </> : null}
    <SidePanel open={Boolean(sessionId)} title={session?.title || 'Playback session'} description={session ? `${session.viewer} · ${session.platform}` : undefined} onClose={closeSession}>
      {query.isLoading && !data ? <LoadingPanel label="Loading session" /> : null}
      {query.error && !data ? <ErrorPanel error={query.error} onRetry={() => void query.refetch()} /> : null}
      {data && !session ? <EmptyPanel title="Session no longer active" message="Player reports expire after inactivity. Close this panel to return to live activity." /> : null}
      {session ? <div className="detailStack"><Panel title="Playback"><div className="statusLine"><span>State</span><StatusPill tone={stateTone(session.state)}>{session.state}</StatusPill></div><div className="statusLine"><span>Position</span><strong>{session.position != null ? formatDuration(session.position) : 'Unknown'} / {session.duration ? formatDuration(session.duration) : 'Unknown'}</strong></div><div className="statusLine"><span>Last report</span><strong>{formatDuration(session.ago_seconds)} ago</strong></div><div className="statusLine"><span>Media ID</span><code>{session.media_id}</code></div></Panel><Panel title="Device & source"><div className="statusLine"><span>Device</span><code>{session.device_id}</code></div><div className="statusLine"><span>Bandwidth</span><strong>{session.tier}</strong></div><div className="statusLine"><span>Selected provider</span><strong>{session.selected_provider || 'Not selected'}</strong></div><div className="statusLine"><span>Serving provider</span><strong>{session.serving_provider || 'Unknown'}</strong></div><div className="statusLine"><span>Source attempts</span><strong>{session.source_attempt || 0}</strong></div>{session.source_id ? <div className="statusLine"><span>Source ID</span><code>{session.source_id}</code></div> : null}</Panel></div> : null}
    </SidePanel>
  </>;
}
