import { useRef, useState } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CircleStop, Pause, Play, RefreshCw, RotateCcw, WandSparkles } from 'lucide-react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { adminApi, formatDate, postAdmin } from '../api';
import {
  Button,
  ConfirmButton,
  EmptyPanel,
  ErrorPanel,
  LoadingPanel,
  PageHeader,
  PageTabs,
  Panel,
  ReconnectNotice,
  SidePanel,
  StatGrid,
  StatusPill,
} from '../components';

interface Observation {
  normalized: string;
  raw_name: string;
  media_type: string;
  occurrences: number;
  resolution: string;
  samples: Array<{ tmdb_id: number; title?: string }>;
}

interface ObservationPage {
  total_observations: number;
  total_occurrences: number;
  offset: number;
  limit: number;
  has_more: boolean;
  items: Observation[];
}

interface CanonicalTag {
  id: number;
  name: string;
  facet?: string;
  total_docs: number;
}

interface RepairStatus {
  stale_mdb_facts: number;
  stale_tags: number;
  mdb_version: string;
}

interface ManualRetagStatus {
  paused?: boolean;
  job?: {
    id: number;
    status: string;
    started_at?: string;
    items_processed: number;
    items_failed: number;
    api_calls: number;
    cost_usd: number;
    error_message?: string;
  };
}

interface DeadTags {
  zero_assignments: Array<{ id: number; name: string; facet: string }>;
  zero_category_refs: Array<{ id: number; name: string; facet: string }>;
  orphan_rows: number;
}

function AliasEditor({ observation, onDone }: { observation: Observation; onDone: () => void }) {
  const [search, setSearch] = useState('');
  const matches = useQuery({
    queryKey: ['admin-alias-tag-search', search],
    queryFn: () => adminApi<{ items: CanonicalTag[] }>(`/tags/list?limit=12&q=${encodeURIComponent(search)}`),
    enabled: search.trim().length > 1,
  });
  const promote = useMutation({
    mutationFn: (tag: CanonicalTag) => postAdmin('/tags/alias', { raw: observation.raw_name, tag_id: tag.id }),
    onSuccess: onDone,
  });

  return <div className="inlineEditor" style={{ width: '100%', maxWidth: 620 }}>
    <input
      aria-label={`Canonical tag for ${observation.raw_name}`}
      value={search}
      onChange={(event) => setSearch(event.target.value)}
      placeholder="Search the canonical vocabulary"
      autoFocus
    />
    {matches.isError && matches.data ? <ReconnectNotice reconnecting onRetry={() => void matches.refetch()} /> : null}
    {matches.isError && !matches.data ? <p className="formError" role="alert">Could not search canonical tags. Try again.</p> : null}
    {matches.data?.items.length ? <div className="choiceList">{matches.data.items.map((tag) => (
      <Button variant="secondary" size="compact" key={tag.id} disabled={promote.isPending} onClick={() => promote.mutate(tag)}>
        {tag.name}<small>{tag.facet || 'tag'} · {tag.total_docs.toLocaleString()} titles</small>
      </Button>
    ))}</div> : search.length > 1 && !matches.isLoading && !matches.isError ? <small className="muted">No canonical tags match.</small> : null}
    {promote.error ? <p className="formError">{promote.error.message}</p> : null}
    <p className="panelFootnote">Promotion fixes future runs. Use the validation sample below to confirm it before paying for any broader re-tag.</p>
  </div>;
}

export default function TagQualityPage() {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const openedObservationHere = useRef(false);
  const pageValue = Number(searchParams.get('page') ?? '1');
  const page = Number.isSafeInteger(pageValue) && pageValue > 0 ? pageValue : 1;
  const offset = (page - 1) * 50;
  const selectedNormalized = searchParams.get('observation');
  const selectedType = searchParams.get('type');
  const validObservationLink = !!selectedNormalized && (selectedType === 'movie' || selectedType === 'tv');
  function setPage(nextPage: number) {
    setSearchParams((current) => { const next = new URLSearchParams(current); if (nextPage > 1) next.set('page', String(nextPage)); else next.delete('page'); return next; });
  }
  function openObservation(item: Observation) {
    openedObservationHere.current = true;
    setSearchParams((current) => { const next = new URLSearchParams(current); next.set('observation', item.normalized); next.set('type', item.media_type); return next; });
  }
  function closeObservation() {
    if (openedObservationHere.current) navigate(-1);
    else setSearchParams((current) => { const next = new URLSearchParams(current); next.delete('observation'); next.delete('type'); return next; }, { replace: true });
  }
  const limit = 50;
  const observations = useQuery({
    queryKey: ['admin-tag-observations', offset],
    queryFn: () => adminApi<ObservationPage>(`/tags/offvocab?limit=${limit}&offset=${offset}`),
    placeholderData: keepPreviousData,
  });
  const observationDetail = useQuery({
    queryKey: ['admin-tag-observation', selectedNormalized, selectedType],
    queryFn: () => adminApi<ObservationPage>(`/tags/offvocab?resolution=all&limit=1&normalized=${encodeURIComponent(selectedNormalized!)}&media_type=${encodeURIComponent(selectedType!)}`),
    enabled: validObservationLink,
  });
  const selectedObservation = observationDetail.data?.items.find((item) => item.normalized === selectedNormalized && item.media_type === selectedType);
  const repair = useQuery({
    queryKey: ['admin-tag-repair'],
    queryFn: () => adminApi<RepairStatus>('/tagging/repair/status'),
  });
  const dead = useQuery({
    queryKey: ['admin-dead-tags'],
    queryFn: () => adminApi<DeadTags>('/tags/dead'),
  });
  const aliasSample = useQuery({
    queryKey: ['admin-alias-sample'],
    queryFn: () => adminApi<{ tmdb_ids: number; movie: number[]; tv: number[]; est_cost_usd: number }>('/tags/alias/sample-ids'),
  });
  const retag = useQuery({
    queryKey: ['admin-manual-retag'],
    queryFn: () => adminApi<ManualRetagStatus>('/tagging/rebuild/status'),
    refetchInterval: 4000,
    refetchIntervalInBackground: false,
  });
  const repairStale = useMutation({
    mutationFn: () => postAdmin('/tagging/repair/retag-stale', {}),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['admin-tag-repair'] });
      void client.invalidateQueries({ queryKey: ['admin-manual-retag'] });
    },
  });
  const validateAliases = useMutation({
    mutationFn: async () => {
      const ids = [...(aliasSample.data?.movie ?? []), ...(aliasSample.data?.tv ?? [])];
      if (!ids.length) throw new Error('No promoted observations have validation sample IDs yet. Promote an alias first.');
      return postAdmin('/tagging/rebuild/start', { force: true, tmdb_ids: ids });
    },
    onSuccess: () => void client.invalidateQueries({ queryKey: ['admin-manual-retag'] }),
  });
  const control = useMutation({
    mutationFn: (action: 'pause' | 'resume' | 'stop') => postAdmin(`/tagging/rebuild/${action}`, {}),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['admin-manual-retag'] }),
  });
  const job = retag.data?.job;
  const running = job?.status === 'running';

  return <>
    <PageHeader
      title="Tag quality"
      description="Triage model vocabulary, validate promoted aliases cheaply, and repair only the titles whose source facts changed."
      actions={<Button variant="secondary" onClick={() => {
        void observations.refetch(); void repair.refetch(); void dead.refetch(); void retag.refetch();
      }}><RefreshCw size={16} /> Refresh</Button>}
    />
    <ReconnectNotice reconnecting={(observations.isError && !!observations.data) || (retag.isError && !!retag.data)} onRetry={() => { void observations.refetch(); void retag.refetch(); }} />
    <PageTabs tabs={[{ value: 'observations', label: 'Observations' }, { value: 'repairs', label: 'Repairs' }, { value: 'integrity', label: 'Integrity' }]} defaultTab="observations">
      {(tab) => <>
    {tab === 'repairs' ? <>

    <div className="twoColumn">
      <Panel title="Targeted repair" description="Avoid a full re-tag when only stale MDBList facts need attention.">
        {repair.isLoading ? <LoadingPanel /> : !repair.data ? <ErrorPanel error={repair.error} onRetry={() => void repair.refetch()} /> : <>
          <ReconnectNotice reconnecting={repair.isError} onRetry={() => void repair.refetch()} />
          <div className="statusLine"><span>Stale MDBList facts</span><strong>{repair.data.stale_mdb_facts.toLocaleString()}</strong></div>
          <div className="statusLine"><span>Titles with stale tags</span><strong>{repair.data.stale_tags.toLocaleString()}</strong></div>
          <div className="statusLine"><span>Current MDB version</span><code>{repair.data.mdb_version}</code></div>
          <ConfirmButton variant="secondary" disabled={!repair.data.stale_tags || repairStale.isPending || running} prompt={`Re-tag only ${repair.data.stale_tags.toLocaleString()} titles whose tags predate their source facts?`} onConfirm={() => repairStale.mutate()}><RotateCcw size={16} /> Re-tag stale titles</ConfirmButton>
        </>}
        {repairStale.error ? <p className="formError">{repairStale.error.message}</p> : null}
      </Panel>
      <Panel title="Alias validation" description="Re-tag only the saved sample IDs from aliases you promoted.">
        <div className="statusLine"><span>Saved sample titles</span><strong>{aliasSample.isLoading ? '…' : Number(aliasSample.data?.tmdb_ids ?? 0).toLocaleString()}</strong></div>
        <div className="statusLine"><span>Estimated maximum cost</span><strong>${Number(aliasSample.data?.est_cost_usd ?? 0).toFixed(2)}</strong></div>
        <p className="panelFootnote">This is a small paid validation pass, not the full affected set. It confirms that aliases resolve correctly before any larger run.</p>
        <ConfirmButton variant="secondary" disabled={validateAliases.isPending || running || !aliasSample.data?.tmdb_ids} prompt={`Start a forced validation re-tag of ${Number(aliasSample.data?.tmdb_ids ?? 0).toLocaleString()} saved titles for an estimated maximum of $${Number(aliasSample.data?.est_cost_usd ?? 0).toFixed(2)}?`} onConfirm={() => validateAliases.mutate()}><WandSparkles size={16} /> Validate promoted aliases</ConfirmButton>
        {!aliasSample.isLoading && !aliasSample.data?.tmdb_ids ? <p className="panelFootnote">Promote an observation with saved sample titles before running validation.</p> : null}
        {aliasSample.error ? <p className="formError">{aliasSample.error.message}</p> : null}
        {validateAliases.error ? <p className="formError">{validateAliases.error.message}</p> : null}
      </Panel>
    </div>

    {job ? <Panel title={`Manual re-tag job ${job.id}`} description={`Started ${formatDate(job.started_at)}`} actions={<StatusPill tone={job.status === 'completed' ? 'good' : job.status === 'failed' ? 'bad' : 'warn'}>{job.status}</StatusPill>}>
      <StatGrid items={[
        { label: 'Processed', value: Number(job.items_processed ?? 0).toLocaleString() },
        { label: 'Failed', value: Number(job.items_failed ?? 0).toLocaleString() },
        { label: 'API calls', value: Number(job.api_calls ?? 0).toLocaleString() },
        { label: 'Cost', value: `$${Number(job.cost_usd ?? 0).toFixed(2)}` },
      ]} />
      {running ? <div className="formActions"><Button variant="secondary" onClick={() => control.mutate(retag.data?.paused ? 'resume' : 'pause')}>{retag.data?.paused ? <Play size={16} /> : <Pause size={16} />}{retag.data?.paused ? 'Resume' : 'Pause'}</Button><ConfirmButton variant="danger" prompt="Stop this validation re-tag after the current batch?" onConfirm={() => control.mutate('stop')}><CircleStop size={16} /> Stop safely</ConfirmButton></div> : null}
      {job.error_message ? <p className="formError">{job.error_message}</p> : null}
    </Panel> : null}
    </> : null}

    {tab === 'observations' ? <>
    <Panel title="Unmapped model observations" description="Frequent proposals may be useful aliases; one-off suggestions should remain unmapped." actions={observations.data ? <span className="muted">{observations.data.total_occurrences.toLocaleString()} occurrences</span> : null}>
      {observations.isLoading ? <LoadingPanel label="Loading observations" /> : observations.error && !observations.data ? <ErrorPanel error={observations.error} onRetry={() => void observations.refetch()} /> : observations.data?.items.length ? <>
        <ReconnectNotice reconnecting={observations.isError && !!observations.data} onRetry={() => void observations.refetch()} />
        {observations.isPlaceholderData ? <p className="muted" role="status">Updating observations…</p> : null}
        <div className="tableWrap"><table><thead><tr><th>Proposed concept</th><th>Type</th><th>Occurrences</th><th>Sample titles</th><th>Actions</th></tr></thead><tbody>{observations.data.items.map((item) => <tr key={`${item.normalized}-${item.media_type}`}><td data-label="Proposed concept"><strong>{item.raw_name}</strong></td><td data-label="Type">{item.media_type}</td><td data-label="Occurrences">{item.occurrences.toLocaleString()}</td><td data-label="Sample titles">{item.samples.map((sample) => sample.title || `TMDB ${sample.tmdb_id}`).join(', ') || 'No samples saved'}</td><td data-label="Actions"><Button variant="secondary" size="compact" disabled={item.occurrences < 2} onClick={() => openObservation(item)}>Map alias</Button></td></tr>)}</tbody></table></div>
        <div className="pagination"><Button variant="secondary" disabled={!offset || observations.isPlaceholderData} onClick={() => setPage(page - 1)}>Previous</Button><span>Showing {offset + 1}–{Math.min(offset + limit, observations.data.total_observations)} of {observations.data.total_observations}</span><Button variant="secondary" disabled={!observations.data.has_more || observations.isPlaceholderData} onClick={() => setPage(page + 1)}>Next</Button></div>
      </> : page > 1 ? <EmptyPanel title="No observations on this page" message="Go to an earlier page to continue triage." /> : <EmptyPanel title="No unresolved observations" message="The current model output resolves entirely to the canonical vocabulary." />}
      {page > 1 && observations.data && !observations.data.items.length ? <Button variant="secondary" onClick={() => setPage(page - 1)}>Previous page</Button> : null}
    </Panel>
    </> : null}

    {tab === 'integrity' ? <>
    <Panel title="Vocabulary integrity" description="Signals that a canonical tag is unreachable or no longer contributes to discovery.">
      {dead.isLoading ? <LoadingPanel /> : !dead.data ? <ErrorPanel error={dead.error} onRetry={() => void dead.refetch()} /> : <>
        <ReconnectNotice reconnecting={dead.isError} onRetry={() => void dead.refetch()} />
        <StatGrid items={[
          { label: 'Never assigned', value: dead.data.zero_assignments.length.toLocaleString() },
          { label: 'No row references', value: dead.data.zero_category_refs.length.toLocaleString() },
          { label: 'Orphan assignments', value: dead.data.orphan_rows.toLocaleString() },
        ]} />
        {dead.data.zero_assignments.length ? <details className="advancedDisclosure"><summary>Tags with no assignments</summary><p>{dead.data.zero_assignments.slice(0, 50).map((tag) => tag.name).join(', ')}</p></details> : null}
      </>}
    </Panel>
    </> : null}
      </>}
    </PageTabs>
    <SidePanel open={selectedNormalized !== null || selectedType !== null} title={selectedObservation?.raw_name ?? 'Map observation'} description={selectedObservation ? `${selectedObservation.media_type} · ${selectedObservation.occurrences.toLocaleString()} occurrences` : undefined} onClose={closeObservation}>
      {!validObservationLink ? <EmptyPanel title="Invalid observation link" message="The link needs both an observation and a movie or TV type." />
        : observationDetail.isLoading ? <LoadingPanel label="Loading observation" />
          : observationDetail.error && !observationDetail.data ? <ErrorPanel error={observationDetail.error} onRetry={() => void observationDetail.refetch()} />
            : !selectedObservation ? <EmptyPanel title="Observation not found" message="This proposal may have been resolved or removed. Close this panel to return to the list." />
              : <><ReconnectNotice reconnecting={observationDetail.isError && !!observationDetail.data} onRetry={() => void observationDetail.refetch()} /><p className="panelFootnote">Samples: {selectedObservation.samples.map((sample) => sample.title || `TMDB ${sample.tmdb_id}`).join(', ') || 'None saved'}</p><AliasEditor observation={selectedObservation} onDone={() => { closeObservation(); void client.invalidateQueries({ queryKey: ['admin-tag-observations'] }); void client.invalidateQueries({ queryKey: ['admin-tag-observation'] }); void client.invalidateQueries({ queryKey: ['admin-alias-sample'] }); }} /></>}
    </SidePanel>
  </>;
}
