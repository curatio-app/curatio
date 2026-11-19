import { ChangeEvent, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Download, Play, RefreshCw, Upload } from 'lucide-react';
import { AdminApiError, adminApi, formatDate, postAdmin } from '../api';
import { Button, ErrorPanel, Field, LoadingPanel, Panel, StatGrid, StatusPill } from '../components';

export interface LibraryTaggingStatus {
  titles: number;
  tagged: number;
  waiting: number;
  rule_tagged_awaiting_ai: number;
  ai_ready: boolean;
  catalog_beyond_library: boolean;
  monthly_cap_usd: number;
  spent_this_month_usd: number;
  running: boolean;
  last_run_at?: string | null;
  last_result?: { processed?: number; mode?: string; capped?: boolean } | null;
  pack: { installed_version?: string | null; titles: number; url: string };
}

interface RuleOnlyStatus {
  waiting: { movie: number; tv: number };
  min_votes: number;
  cost_ceiling_usd: number;
  running: boolean;
  total: number;
  done: number;
  ai_tagged: number;
  still_rule_only: number;
  cost_usd: number;
  first_error: string;
  stopped_reason: string;
  finished_at?: string | null;
}

interface PackImportResult { version: string; titles: number; stored: number; kept_local_ai: number; unknown_tags: number }

export interface ExportedPack { name: string; sha256: string; titles: string; version: string; size: number }

/** Downloads the tag pack and reports its checksum (to publish next to the file). */
export async function exportTagPack(minVotes: number): Promise<ExportedPack> {
  const response = await fetch(`/admin/api/tag-pack/export?min_votes=${encodeURIComponent(String(minVotes))}`, { credentials: 'same-origin' });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new AdminApiError(String((data as { detail?: unknown }).detail ?? 'Export failed'), response.status);
  }
  const blob = await response.blob();
  const disposition = response.headers.get('Content-Disposition') ?? '';
  const name = /filename="?([^";]+)"?/.exec(disposition)?.[1] ?? 'curatio-tag-pack.jsonl.gz';
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
  return {
    name,
    sha256: response.headers.get('X-Curatio-Pack-Sha256') ?? '',
    titles: response.headers.get('X-Curatio-Pack-Titles') ?? '',
    version: response.headers.get('X-Curatio-Pack-Version') ?? '',
    size: blob.size,
  };
}

export function LibraryTaggingPanel() {
  const client = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [minVotes, setMinVotes] = useState(50);
  const status = useQuery({
    queryKey: ['library-tagging'],
    queryFn: () => adminApi<LibraryTaggingStatus>('/library-tagging/status'),
    refetchInterval: (query) => (query.state.data?.running ? 5000 : false),
  });
  const refresh = () => void client.invalidateQueries({ queryKey: ['library-tagging'] });
  const run = useMutation({ mutationFn: () => postAdmin('/library-tagging/run'), onSuccess: refresh });
  const exportPack = useMutation({ mutationFn: () => exportTagPack(minVotes) });
  const importPack = useMutation({
    mutationFn: (file: File) => adminApi<PackImportResult>('/tag-pack/import', { method: 'POST', body: file, headers: { 'Content-Type': 'application/gzip' } }),
    onSuccess: refresh,
  });
  const [retryVotes, setRetryVotes] = useState(5);
  const ruleOnly = useQuery({
    queryKey: ['rule-only', retryVotes],
    queryFn: () => adminApi<RuleOnlyStatus>(`/tagging/repair/rule-only?min_votes=${retryVotes}`),
    refetchInterval: (query) => (query.state.data?.running ? 5000 : false),
  });
  const retry = useMutation({
    mutationFn: () => postAdmin('/tagging/repair/rule-only', { min_votes: retryVotes }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['rule-only'] }),
  });
  const update = useMutation({ mutationFn: () => postAdmin<{ status: string; version?: string }>('/tag-pack/update'), onSuccess: refresh });

  function onFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (file) importPack.mutate(file);
    event.target.value = '';
  }

  if (status.isLoading && !status.data) return <LoadingPanel label="Loading library tagging" />;
  if (status.error && !status.data) return <ErrorPanel error={status.error} onRetry={() => void status.refetch()} />;
  const data = status.data;
  if (!data) return null;
  const spend = data.monthly_cap_usd > 0
    ? `$${data.spent_this_month_usd.toFixed(2)} of $${data.monthly_cap_usd.toFixed(2)}`
    : `$${data.spent_this_month_usd.toFixed(2)} (no cap)`;

  return <>
    <Panel
      title="Library tagging"
      description="Only titles on your Plex/Jellyfin servers and titles saved to a Watchlist are tagged. Titles are sent to the selected AI provider; without a key, rule-based tags are used and upgraded once a key is added."
      actions={<StatusPill tone={data.ai_ready ? 'good' : 'warn'}>{data.ai_ready ? 'AI tagging' : 'Rule-based (no AI key)'}</StatusPill>}
    >
      <StatGrid items={[
        { label: 'Owned & Watchlist titles', value: data.titles.toLocaleString() },
        { label: 'Tagged', value: data.tagged.toLocaleString() },
        { label: 'Waiting', value: data.waiting.toLocaleString() },
        { label: 'Rule-tagged, waiting for AI', value: data.rule_tagged_awaiting_ai.toLocaleString() },
        { label: 'AI spend this month', value: spend },
      ]} />
      <p className="panelFootnote">
        Last run: {formatDate(data.last_run_at)}
        {data.last_result?.processed != null ? ` · ${data.last_result.processed} tagged (${data.last_result.mode ?? 'rules'})` : ''}
        {data.last_result?.capped ? ' · stopped at the monthly cap' : ''}.
        {' '}Catalog titles nobody owns are {data.catalog_beyond_library ? 'also tagged nightly' : 'not tagged'} (Settings → Tag Catalog Beyond Library).
      </p>
      <div className="formActions">
        <Button disabled={data.running || run.isPending} onClick={() => run.mutate()}><Play size={16} /> {data.running ? 'Tagging…' : 'Tag waiting titles now'}</Button>
        <Button variant="secondary" onClick={() => void status.refetch()}><RefreshCw size={16} /> Refresh</Button>
      </div>
      {run.error ? <p className="formError">{run.error.message}</p> : null}
    </Panel>

    <Panel
      title="Titles with rule-based tags only"
      description="The tagger asks the AI only when a title's description gives it something to judge, such as mood, themes or style. Most titles are complete with rule-based tags, so this number stays high. Retrying is cheap and picks up titles whose earlier AI call failed or that have new details since."
    >
      {ruleOnly.data ? <>
        <StatGrid items={[
          { label: 'Movies waiting', value: ruleOnly.data.waiting.movie.toLocaleString() },
          { label: 'Shows waiting', value: ruleOnly.data.waiting.tv.toLocaleString() },
          { label: 'Spending ceiling', value: `$${ruleOnly.data.cost_ceiling_usd.toFixed(2)}` },
        ]} />
        {ruleOnly.data.running || ruleOnly.data.finished_at ? (
          <p className="panelFootnote" role="status">
            {ruleOnly.data.running ? 'Running: ' : 'Last run: '}
            {ruleOnly.data.done.toLocaleString()} of {ruleOnly.data.total.toLocaleString()} checked, {ruleOnly.data.ai_tagged.toLocaleString()} gained AI tags, {ruleOnly.data.still_rule_only.toLocaleString()} complete with rule-based tags, ${ruleOnly.data.cost_usd.toFixed(2)} spent.
            {ruleOnly.data.stopped_reason ? ` ${ruleOnly.data.stopped_reason}.` : ''}
          </p>
        ) : null}
        {ruleOnly.data.first_error ? <p className="formError">First AI error: {ruleOnly.data.first_error}</p> : null}
      </> : ruleOnly.isLoading ? <LoadingPanel label="Counting rule-only titles" /> : null}
      <div className="inlineForm">
        <Field label="Minimum TMDB votes"><input type="number" min="0" value={retryVotes} onChange={(event) => setRetryVotes(Number(event.target.value))} /></Field>
        <Button disabled={!data.ai_ready || retry.isPending || Boolean(ruleOnly.data?.running)} onClick={() => retry.mutate()}><Play size={16} /> {ruleOnly.data?.running ? 'Retrying…' : 'Retry AI tags'}</Button>
      </div>
      {!data.ai_ready ? <p className="panelFootnote">Set up an AI provider first.</p> : null}
      {retry.error ? <p className="formError">{retry.error.message}</p> : null}
    </Panel>

    <Panel
      title="Tag pack"
      description="A tag pack holds only Curatio tags keyed by TMDB/IMDb id. Installs download it whole, so it never reveals what anyone owns. Titles with rule-based tags only are left out: every install works those out itself, free."
    >
      <div className="statusLine"><span>Installed pack</span><strong>{data.pack.installed_version ?? 'None'}</strong></div>
      <div className="statusLine"><span>Titles tagged from a pack</span><strong>{data.pack.titles.toLocaleString()}</strong></div>
      <div className="statusLine"><span>Pack URL</span><strong>{data.pack.url || 'Not set'}</strong></div>

      <h3>Export this server’s tags</h3>
      <p className="panelFootnote">Includes every title with AI-made tags and at least this many TMDB votes. No titles, overviews, artwork, keywords or ratings are included.</p>
      <div className="inlineForm">
        <Field label="Minimum TMDB votes"><input type="number" min="0" value={minVotes} onChange={(event) => setMinVotes(Number(event.target.value))} /></Field>
        <Button disabled={exportPack.isPending} onClick={() => exportPack.mutate()}><Download size={16} /> {exportPack.isPending ? 'Building pack…' : 'Export tag pack'}</Button>
      </div>
      {exportPack.data ? <p className="formSuccess" role="status">Saved {exportPack.data.name}: {Number(exportPack.data.titles).toLocaleString()} titles, {(exportPack.data.size / 1_048_576).toFixed(1)} MB. SHA-256 <code>{exportPack.data.sha256}</code></p> : null}
      {exportPack.error ? <p className="formError">{exportPack.error.message}</p> : null}

      <h3>Import a pack</h3>
      <p className="panelFootnote">Pack tags replace rule-based tags. Tags this server made with its own AI are kept.</p>
      <div className="formActions">
        <input ref={fileInput} type="file" accept=".gz,application/gzip" hidden onChange={onFile} aria-label="Tag pack file" />
        <Button variant="secondary" disabled={importPack.isPending} onClick={() => fileInput.current?.click()}><Upload size={16} /> {importPack.isPending ? 'Importing…' : 'Import from file'}</Button>
        <Button variant="secondary" disabled={!data.pack.url || update.isPending} onClick={() => update.mutate()}><RefreshCw size={16} /> Download from pack URL</Button>
      </div>
      {importPack.data ? <p className="formSuccess" role="status">Imported pack {importPack.data.version}: {importPack.data.stored.toLocaleString()} titles stored, {importPack.data.kept_local_ai.toLocaleString()} kept with local AI tags{importPack.data.unknown_tags ? `, ${importPack.data.unknown_tags} unknown tags skipped` : ''}.</p> : null}
      {importPack.error ? <p className="formError">{importPack.error.message}</p> : null}
      {update.data ? <p className="formSuccess" role="status">Pack {update.data.status}{update.data.version ? ` (${update.data.version})` : ''}.</p> : null}
      {update.error ? <p className="formError">{update.error.message}</p> : null}
    </Panel>
  </>;
}
