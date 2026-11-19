import { FormEvent, useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { RefreshCw, Save } from 'lucide-react';
import { adminApi, formatDate, postAdmin } from '../api';
import { Button, ErrorPanel, Field, LoadingPanel, PageHeader, Panel, ReconnectNotice, StatusPill } from '../components';

interface SettingsData { api_keys: Record<string, string>; app: Record<string, string | number>; features: Record<string, boolean>; schedule: Record<string, string | boolean>; fingerprint: Record<string, unknown>; segments?: Record<string, boolean>; }

const segmentLabels: Record<string, string> = { SKIP_INTRO_ENABLED: 'Skip Intro', SKIP_CREDITS_ENABLED: 'End credits (Next Episode prompt)', INTRODB_ENABLED: 'IntroDB', THEINTRODB_ENABLED: 'TheIntroDB', FINGERPRINT_SEGMENTS_ENABLED: 'Fingerprint learner (last fallback)' };

function titleFor(key: string) { return key.toLowerCase().replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase()); }

export function SettingsPage() {
  const client = useQueryClient(); const query = useQuery({ queryKey: ['admin-settings'], queryFn: () => adminApi<SettingsData>('/settings') });
  const [draft, setDraft] = useState<Record<string, string | number | boolean>>({}); const [dirty, setDirty] = useState<Set<string>>(new Set());
  useEffect(() => { if (query.data && dirty.size === 0) setDraft({ ...query.data.api_keys, ...query.data.app, ...query.data.features, ...query.data.segments }); }, [query.data, dirty.size]);
  const save = useMutation({ mutationFn: () => postAdmin('/settings', Object.fromEntries([...dirty].map((key) => [key, draft[key]]))), onSuccess: () => { setDirty(new Set()); void client.invalidateQueries({ queryKey: ['admin-settings'] }); void client.invalidateQueries({ queryKey: ['admin-overview'] }); } });
  const set = (key: string, value: string | number | boolean) => { setDraft((current) => ({ ...current, [key]: value })); setDirty((current) => new Set(current).add(key)); };
  const apiKeys = Object.keys(query.data?.api_keys ?? {}); const appKeys = Object.keys(query.data?.app ?? {}); const featureKeys = Object.keys(query.data?.features ?? {}); const segmentKeys = Object.keys(query.data?.segments ?? {});
  return <><PageHeader title="Settings" description="Configure Curatio’s integrations, catalog behavior, and viewer-facing features." actions={<Button disabled={!dirty.size || save.isPending} onClick={() => save.mutate()}><Save size={16} /> Save {dirty.size ? `${dirty.size} change${dirty.size === 1 ? '' : 's'}` : 'changes'}</Button>} />
    <ReconnectNotice reconnecting={Boolean(query.error && query.data)} onRetry={() => void query.refetch()} />
    {query.isLoading && !query.data ? <LoadingPanel label="Loading settings" /> : null}
    {query.error && !query.data ? <ErrorPanel error={query.error} onRetry={() => void query.refetch()} /> : null}
    {save.error ? <p className="formError" role="alert">{save.error.message}</p> : null}{save.isSuccess ? <p className="globalNotice" role="status">Settings saved.</p> : null}
    {query.data ? <form className="settingsStack" onSubmit={(event: FormEvent) => { event.preventDefault(); save.mutate(); }}>
      <Panel title="API credentials" description="Masked values stay unchanged unless you replace them."><div className="formGrid">{apiKeys.map((key) => <Field key={key} label={titleFor(key)}><input type="password" value={String(draft[key] ?? '')} onChange={(e) => set(key, e.target.value)} autoComplete="off" /></Field>)}</div></Panel>
      <Panel title="Catalog behavior" description="Quality floors and row sizes affect the next catalog regeneration."><div className="formGrid">{appKeys.map((key) => <Field key={key} label={titleFor(key)}><input type={typeof query.data.app[key] === 'number' ? 'number' : 'text'} step="any" value={String(draft[key] ?? '')} onChange={(e) => set(key, typeof query.data.app[key] === 'number' ? Number(e.target.value) : e.target.value)} /></Field>)}</div></Panel>
      <Panel title="Features" description="Turn product behavior on or off without redeploying."><div className="switchList">{featureKeys.map((key) => <label className="switchField" key={key}><span><strong>{titleFor(key)}</strong></span><input type="checkbox" checked={Boolean(draft[key])} onChange={(e) => set(key, e.target.checked)} /></label>)}</div></Panel>
      {segmentKeys.length ? <Panel title="Intro & credits" description="What the apps are offered, and where the times come from. Times are taken from IntroDB and TheIntroDB first, then the fingerprint learner; hand-set markers always win."><div className="switchList">{segmentKeys.map((key) => <label className="switchField" key={key}><span><strong>{segmentLabels[key] ?? titleFor(key)}</strong></span><input type="checkbox" checked={Boolean(draft[key])} onChange={(e) => set(key, e.target.checked)} /></label>)}</div></Panel> : null}
    </form> : null}
  </>;
}

interface Diagnostic { pipeline: Record<string, number>; categories: Array<Record<string, unknown>>; last_job: { type?: string; status?: string; error?: string }; issues: string[]; healthy: boolean; }
export function DiagnosticsPage() {
  const query = useQuery({ queryKey: ['admin-diagnostics'], queryFn: () => adminApi<Diagnostic>('/debug'), staleTime: 0 });
  const values = useMemo(() => Object.entries(query.data?.pipeline ?? {}), [query.data]);
  return <><PageHeader title="Diagnostics" description="Validate each catalog pipeline layer without modifying data." actions={<Button variant="secondary" onClick={() => void query.refetch()} disabled={query.isFetching}><RefreshCw size={16} /> Run checks</Button>} />
    <ReconnectNotice reconnecting={Boolean(query.error && query.data)} onRetry={() => void query.refetch()} />
    {query.isLoading && !query.data ? <LoadingPanel label="Running catalog diagnostics" /> : query.error && !query.data ? <ErrorPanel error={query.error} onRetry={() => void query.refetch()} /> : query.data ? <><Panel title="Pipeline status" actions={<StatusPill tone={query.data.healthy ? 'good' : 'bad'}>{query.data.healthy ? 'Healthy' : 'Needs attention'}</StatusPill>}><div className="jsonSummary">{values.map(([key, value]) => <div key={key}><span>{titleFor(key)}</span><strong>{Number(value).toLocaleString()}</strong></div>)}</div>{query.data.issues.length ? <ul className="issueList">{query.data.issues.map((issue) => <li key={issue}>{issue}</li>)}</ul> : <p className="formSuccess">All catalog pipeline stages passed.</p>}</Panel><div className="twoColumn"><Panel title="Last tagging job"><div className="statusLine"><span>Type</span><strong>{query.data.last_job.type || 'None'}</strong></div><div className="statusLine"><span>Status</span><StatusPill tone={query.data.last_job.status === 'completed' ? 'good' : query.data.last_job.status === 'failed' ? 'bad' : 'neutral'}>{query.data.last_job.status || 'Unknown'}</StatusPill></div>{query.data.last_job.error ? <p className="formError">{query.data.last_job.error}</p> : null}</Panel><Panel title="Catalog formulas"><div className="statusLine"><span>Rows checked</span><strong>{query.data.categories.length}</strong></div><div className="statusLine"><span>Checked at</span><strong>{formatDate(new Date().toISOString())}</strong></div><p className="panelFootnote">Diagnostics generate only a small preview for each formula and do not save it.</p></Panel></div></> : null}
  </>;
}
