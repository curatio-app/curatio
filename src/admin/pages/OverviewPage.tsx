import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { Check, CircleAlert, ExternalLink, RefreshCw } from 'lucide-react';
import { adminApi, formatDate, formatDuration } from '../api';
import { Button, EmptyPanel, ErrorPanel, LoadingPanel, PageHeader, Panel, ReconnectNotice, StatGrid, StatusPill } from '../components';

interface Overview {
  generated_at: string;
  ready: boolean;
  setup: Record<string, boolean>;
  warnings: string[];
  counts: Record<string, number>;
  jobs: { running: boolean; recent: Array<Record<string, unknown>> };
  streams: Array<Record<string, unknown>>;
  health: { caches: Record<string, { size: number; max: number }>; user_sync: Array<Record<string, unknown>> };
}

const setupLabels: Record<string, { label: string; href: string }> = {
  credentials: { label: 'Add API credentials', href: '/admin/settings' },
  media_server: { label: 'Connect Plex or Jellyfin', href: '/admin/integrations' },
  initial_build: { label: 'Build the first catalog', href: '/admin/jobs' },
  first_user: { label: 'Invite the first viewer', href: '/admin/invites' },
};

export default function OverviewPage() {
  const query = useQuery({
    queryKey: ['admin-overview'], queryFn: () => adminApi<Overview>('/overview'),
    refetchInterval: 15_000, refetchIntervalInBackground: false,
  });
  const data = query.data;
  return <>
    <PageHeader title="Overview" description="Setup progress, service health, and the work that needs attention now." actions={<Button variant="secondary" onClick={() => void query.refetch()}><RefreshCw size={16} /> Refresh</Button>} />
    <ReconnectNotice reconnecting={Boolean(query.error && data)} onRetry={() => void query.refetch()} />
    {query.isLoading && !data ? <LoadingPanel label="Loading operational overview" /> : null}
    {query.error && !data ? <ErrorPanel error={query.error} onRetry={() => void query.refetch()} /> : null}
    {data ? <>
    {!data.ready ? <Panel title="Finish setup" description="Complete these steps to make Curatio ready for viewers." className="accentPanel"><div className="checklist">{Object.entries(data.setup).map(([key, done]) => <Link key={key} className={done ? 'done' : ''} to={setupLabels[key]?.href.replace('/admin', '') ?? '/overview'}>{done ? <Check /> : <span className="stepDot" /> }<span>{setupLabels[key]?.label ?? key}</span>{!done ? <ExternalLink size={15} /> : null}</Link>)}</div></Panel> : null}
    {data.warnings.length ? <Panel title="Needs attention" className="warningPanel"><ul className="alertList">{data.warnings.map((warning) => <li key={warning}><CircleAlert size={17} />{warning}</li>)}</ul></Panel> : null}
    <StatGrid items={[
      { label: 'Metadata', value: Number(data.counts.total_metadata ?? 0).toLocaleString() },
      { label: 'Catalog items', value: Number(data.counts.catalog_items ?? 0).toLocaleString() },
      { label: 'Active categories', value: Number(data.counts.active_categories ?? 0).toLocaleString() },
      { label: 'Viewers', value: Number(data.counts.total_users ?? 0).toLocaleString() },
    ]} />
    <div className="twoColumn">
      <Panel title="Current operations" description={`Updated ${formatDate(data.generated_at)}`}>
        <div className="statusLine"><span>Build pipeline</span><StatusPill tone={data.jobs.running ? 'warn' : 'good'}>{data.jobs.running ? 'Running' : 'Idle'}</StatusPill></div>
        <div className="statusLine"><span>Live playback sessions</span><strong>{data.streams.length}</strong></div>
        {data.jobs.recent.length ? <div className="compactList">{data.jobs.recent.slice(0, 5).map((job) => <div key={String(job.id)}><span><strong>{String(job.job_type ?? 'Job')}</strong><small>{formatDate(job.started_at as string)}</small></span><StatusPill tone={job.status === 'completed' ? 'good' : job.status === 'failed' ? 'bad' : 'neutral'}>{String(job.status ?? 'unknown')}</StatusPill></div>)}</div> : <EmptyPanel title="No recent jobs" message="Build and maintenance activity will appear here." />}
      </Panel>
      <Panel title="Cache health" description="In-process cache utilization."><div className="cacheList">{Object.entries(data.health.caches).map(([name, cache]) => { const pct = cache.max ? Math.round(cache.size / cache.max * 100) : 0; return <div key={name}><div><span>{name}</span><small>{cache.size} / {cache.max} · {pct}%</small></div><progress value={cache.size} max={cache.max || 1} /></div>; })}</div>{data.health.user_sync.length ? <p className="panelFootnote">{data.health.user_sync.length} active profile sync record{data.health.user_sync.length === 1 ? '' : 's'}.</p> : null}</Panel>
    </div>
    {data.streams.length ? <Panel title="Live playback"><div className="tableWrap"><table><thead><tr><th>Title</th><th>Viewer</th><th>Type</th><th>Tier</th><th>Age</th></tr></thead><tbody>{data.streams.slice(0, 8).map((stream, index) => <tr key={`${String(stream.video_id)}-${index}`}><td data-label="Title">{String(stream.title ?? stream.video_id ?? 'Unknown')}</td><td data-label="Viewer">{String(stream.user ?? 'Unknown')}</td><td data-label="Type">{String(stream.type ?? '—')}</td><td data-label="Tier">{String(stream.tier ?? '—')}</td><td data-label="Age">{formatDuration(stream.ago_seconds as number)}</td></tr>)}</tbody></table></div></Panel> : null}
    </> : null}
  </>;
}
