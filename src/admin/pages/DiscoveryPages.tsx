import { useDeferredValue, useEffect, useRef, useState } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CircleStop, DatabaseZap, Play, RefreshCw } from 'lucide-react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { AdminApiError, adminApi, formatDate, postAdmin } from '../api';
import { LibraryTaggingPanel } from './LibraryTaggingPanel';
import {
  Button,
  ConfirmButton,
  EmptyPanel,
  ErrorPanel,
  Field,
  LoadingPanel,
  PageHeader,
  PageTabs,
  Panel,
  ReconnectNotice,
  SidePanel,
  StatGrid,
  StatusPill,
} from '../components';

interface CatalogRow {
  id: string;
  name: string;
  media_type: string;
  source: string;
  items: number;
  servable?: boolean;
  mandatory: string[];
  required: string[];
  optional: string[];
}

interface CatalogPreview {
  name?: string;
  media_type?: string;
  formula?: { mandatory?: string[]; required?: string[]; optional?: string[] };
  items: Array<{
    tmdb_id: number;
    title: string;
    year?: string;
    quality?: number;
    votes: number;
    score: number;
    matched: string[];
  }>;
}

interface QualityPreview {
  applied: Record<string, number>;
  counts: Record<'movie' | 'tv', { total: number; kept: number; pct: number }>;
}

interface SynthConfig {
  excluded_tags: string[];
  excluded_words: string[];
  rotation_days: number;
  editor_enabled: boolean;
  editor_provider: TaggingProviderOption['id'];
  editor_model: string;
  editor_cost_ceiling_usd: number;
  providers: TaggingProviderOption[];
  known_tags: string[];
}

function CatalogRowPreview({ id }: { id: string }) {
  const query = useQuery({
    queryKey: ['admin-catalog-row-preview', id],
    queryFn: () => adminApi<CatalogPreview>(`/catalog/rows/${encodeURIComponent(id)}/preview`),
  });
  if (query.isLoading) return <LoadingPanel label="Checking row contents" />;
  if (!query.data) return query.error instanceof AdminApiError && query.error.status === 404
    ? <EmptyPanel title="Catalog row not found" message="This row may have been removed. Close the panel to return to the list." />
    : <ErrorPanel error={query.error} onRetry={() => void query.refetch()} />;
  return <><ReconnectNotice reconnecting={query.isError && !!query.data} onRetry={() => void query.refetch()} />
    {query.data.formula ? <div className="formula"><span>Must: {query.data.formula.mandatory?.join(', ') || '—'}</span><span>Required: {query.data.formula.required?.join(', ') || '—'}</span><span>Optional: {query.data.formula.optional?.join(', ') || '—'}</span></div> : null}
    {query.data.items.length ? <div className="previewList">{query.data.items.slice(0, 15).map((item) => <div key={item.tmdb_id}><span><strong>{item.title}</strong><small>{item.year || 'Unknown year'} · {item.votes.toLocaleString()} votes · quality {item.quality ?? '—'}</small></span><span><small>{item.matched.join(', ') || 'Formula match'}</small><code>{item.score.toFixed(3)}</code></span></div>)}</div> : <EmptyPanel title="This row is empty" message="Review its formula and the current quality floor before publishing." />}</>;
}

function PersonalRowPreview({ id }: { id: number }) {
  const query = useQuery({
    queryKey: ['admin-personal-row-preview', id],
    queryFn: () => adminApi<{
      seed?: { tmdb_id: number; media_type: string; title: string };
      basis_tags: string[];
      algorithm_version?: string;
      revision: string;
      items: Array<{ tmdb_id: number; title: string; year: string; rank: number; similarity: number }>;
    }>(`/catalog/personal/${id}/preview`),
  });
  if (query.isLoading) return <LoadingPanel label="Loading personal row" />;
  if (!query.data) return query.error instanceof AdminApiError && query.error.status === 404
    ? <EmptyPanel title="Personal row not found" message="This row may have been removed. Close the panel to return to the list." />
    : <ErrorPanel error={query.error} onRetry={() => void query.refetch()} />;
  return <><ReconnectNotice reconnecting={query.isError && !!query.data} onRetry={() => void query.refetch()} /><p><strong>Seed:</strong> {query.data.seed?.title ?? '—'} · <strong>Basis:</strong> {query.data.basis_tags.join(', ') || '—'} · <strong>Algorithm:</strong> {query.data.algorithm_version ?? '—'} · <strong>Revision:</strong> {query.data.revision}</p><div className="previewList">{query.data.items.map((item) => <div key={item.tmdb_id}><span><strong>{item.title}</strong><small>{item.year || 'Unknown year'} · rank {item.rank}</small></span><code>similarity {item.similarity.toFixed(3)}</code></div>)}</div></>;
}

interface TagRow {
  id: number;
  name: string;
  facet?: string;
  description?: string;
  display: boolean;
  is_active: boolean;
  total_docs: number;
  category_refs: string[];
}

interface TagResponse {
  total: number;
  offset: number;
  limit: number;
  has_more: boolean;
  items: TagRow[];
}

interface UpgradeStep {
  key: string;
  title: string;
  detail?: string;
  available?: boolean;
  remaining?: number;
  status?: string;
  stats?: Record<string, unknown>;
}

interface TaggingProviderOption {
  id: 'gemini' | 'anthropic' | 'openai' | 'local';
  label: string;
  key_set: boolean;
  key_hint: string;
  model: string;
  /** Any model name is accepted (a local server has no fixed list). */
  free_model?: boolean;
  base_url?: string;
  models: Array<{
    id: string;
    label: string;
    production_ready?: boolean;
    recommended?: boolean;
    warning?: string;
  }>;
}

interface UpgradePreflight {
  library_total: number;
  eligible_titles: number;
  excluded_by_quality: number;
  keys: Record<string, boolean>;
  ready: boolean;
  missing_required: string[];
  remaining: Record<string, number>;
  est_cost_usd: number;
  estimated_ai_titles?: number;
  estimated_local_only?: number;
  cost_ceiling_usd: number;
  provider?: TaggingProviderOption['id'];
  provider_label?: string;
  providers?: TaggingProviderOption[];
  model: string;
  model_production_ready?: boolean;
  tagger_version: string;
  steps: UpgradeStep[];
  running: boolean;
}

interface UpgradeStatus {
  running: boolean;
  started_at?: string;
  finished_at?: string;
  error?: string;
  steps: UpgradeStep[];
  done: number;
  cost_usd: number;
}

interface RetagStats {
  processed?: number;
  requeued?: number;
  failed?: number;
  model_skips?: number;
  model_omissions?: number;
  provider_failures?: number;
  low_tag_retry_calls?: number;
  recovered_low_tag_results?: number;
  low_tag_diagnostics?: Array<{
    tmdb_id: number;
    title?: string;
    returned?: number;
    surviving_model_tags?: number;
    rejected_reasons?: Record<string, number>;
  }>;
}

export function CatalogsPage() {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const search = searchParams.get('q') ?? '';
  const preview = searchParams.get('preview');
  const openedPreviewHere = useRef(false);
  const previewKind = preview?.startsWith('synth:') ? 'synth' : preview?.startsWith('personal:') ? 'personal' : null;
  const previewId = previewKind ? preview!.slice(previewKind.length + 1) : '';
  const personalPreviewId = previewKind === 'personal' ? Number(previewId) : NaN;
  const validPreview = previewKind === 'synth' ? !!previewId : previewKind === 'personal' && Number.isSafeInteger(personalPreviewId) && personalPreviewId > 0;
  function openPreview(value: string) {
    openedPreviewHere.current = true;
    setSearchParams((current) => { const next = new URLSearchParams(current); next.set('preview', value); return next; });
  }
  function closePreview() {
    if (openedPreviewHere.current) {
      navigate(-1);
    } else {
      setSearchParams((current) => { const next = new URLSearchParams(current); next.delete('preview'); return next; }, { replace: true });
    }
  }
  const [synthDraft, setSynthDraft] = useState<{
    excluded_tags: string;
    excluded_words: string;
    rotation_days: number;
    editor_enabled: boolean;
    editor_provider: TaggingProviderOption['id'];
    editor_model: string;
    editor_cost_ceiling_usd: number;
  } | null>(null);
  const [qualityDraft, setQualityDraft] = useState<Record<string, number> | null>(null);
  const query = useQuery({
    queryKey: ['admin-catalog-rows'],
    queryFn: () => adminApi<{
      count: number;
      rows: CatalogRow[];
      personal_rows: number;
      personal_profiles: number;
      suppressed_rows?: number;
      personal?: Array<{ id: number; user_id: number; profile_name: string; slot_id: string; name: string; media_type: string; generation_method: string; items: number; servable?: boolean; revision: string; seed?: { title: string }; basis_tags: string[] }>;
    }>('/catalog/rows'),
  });
  const preflight = useQuery({
    queryKey: ['admin-upgrade-preflight'],
    queryFn: () => adminApi<UpgradePreflight>('/tagging/upgrade/preflight'),
    refetchInterval: 5000,
    refetchIntervalInBackground: false,
  });
  const upgradeStatus = useQuery({
    queryKey: ['admin-upgrade-status'],
    queryFn: () => adminApi<UpgradeStatus>('/tagging/upgrade/status'),
    refetchInterval: 3000,
    refetchIntervalInBackground: false,
  });
  const quality = useQuery({
    queryKey: ['admin-catalog-quality'],
    queryFn: () => adminApi<QualityPreview>('/catalog/quality/preview'),
  });
  useEffect(() => {
    if (quality.data && !qualityDraft) setQualityDraft({ ...quality.data.applied });
  }, [quality.data, qualityDraft]);
  const synthConfig = useQuery({
    queryKey: ['admin-synth-config'],
    queryFn: () => adminApi<SynthConfig>('/categories/synth/config'),
  });
  useEffect(() => {
    if (synthConfig.data && !synthDraft) setSynthDraft({
      excluded_tags: synthConfig.data.excluded_tags.join(', '),
      excluded_words: synthConfig.data.excluded_words.join(', '),
      rotation_days: synthConfig.data.rotation_days,
      editor_enabled: synthConfig.data.editor_enabled,
      editor_provider: synthConfig.data.editor_provider,
      editor_model: synthConfig.data.editor_model,
      editor_cost_ceiling_usd: synthConfig.data.editor_cost_ceiling_usd,
    });
  }, [synthConfig.data, synthDraft]);
  const regenerate = useMutation({
    mutationFn: () =>
      postAdmin('/tagging/upgrade/start', {
        skip: ['enrich', 'mdblist', 'retag', 'relevance'],
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['admin-upgrade-status'] });
      void client.invalidateQueries({ queryKey: ['admin-catalog-rows'] });
    },
  });
  const saveSynthConfig = useMutation({
    mutationFn: () => postAdmin('/categories/synth/config', {
      excluded_tags: synthDraft?.excluded_tags.split(',').map((value) => value.trim()).filter(Boolean) ?? [],
      excluded_words: synthDraft?.excluded_words.split(',').map((value) => value.trim()).filter(Boolean) ?? [],
      rotation_days: synthDraft?.rotation_days ?? 7,
      editor_enabled: synthDraft?.editor_enabled ?? false,
      editor_provider: synthDraft?.editor_provider ?? 'gemini',
      editor_model: synthDraft?.editor_model ?? '',
      editor_cost_ceiling_usd: synthDraft?.editor_cost_ceiling_usd ?? 0.25,
    }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['admin-synth-config'] }),
  });
  const synthPreview = useMutation({
    mutationFn: () => adminApi<{
      count: number;
      cost_usd: number;
      estimated_cost_usd: number;
      cached: number;
      reviewed: number;
      rows: Array<{
        id: string;
        deterministic_name: string;
        name: string;
        media_type: string;
        support: number;
        lift: number;
        actual_item_count: number;
        editorial_score: number;
        cache_status: string;
        sample_titles: Array<{ tmdb_id: number; title: string; year: string }>;
      }>;
      rejected: Array<{ id?: string; candidate_id?: string; name?: string; deterministic_name?: string; reason_codes: string[]; actual_item_count?: number }>;
    }>('/categories/synth/preview?limit=70'),
  });
  const relevance = useMutation({
    mutationFn: () => postAdmin('/relevance/refresh'),
  });
  const previewQuality = useMutation({
    mutationFn: () => {
      const values = qualityDraft ?? {};
      const params = new URLSearchParams({
        min_quality: String(values.catalog_min_quality ?? ''),
        min_vote_count: String(values.catalog_min_vote_count ?? ''),
        min_rating_sources: String(values.catalog_min_rating_sources ?? ''),
        niche_min_quality: String(values.catalog_niche_min_quality ?? ''),
        niche_min_vote_count: String(values.catalog_niche_min_vote_count ?? ''),
        niche_min_age_days: String(values.catalog_niche_min_age_days ?? ''),
      });
      return adminApi<QualityPreview>(`/catalog/quality/preview?${params.toString()}`);
    },
  });
  const saveQuality = useMutation({
    mutationFn: () => postAdmin('/settings', {
      CATALOG_MIN_QUALITY: qualityDraft?.catalog_min_quality,
      CATALOG_MIN_VOTE_COUNT: qualityDraft?.catalog_min_vote_count,
      CATALOG_MIN_RATING_SOURCES: qualityDraft?.catalog_min_rating_sources,
      CATALOG_NICHE_MIN_QUALITY: qualityDraft?.catalog_niche_min_quality,
      CATALOG_NICHE_MIN_VOTE_COUNT: qualityDraft?.catalog_niche_min_vote_count,
      CATALOG_NICHE_MIN_AGE_DAYS: qualityDraft?.catalog_niche_min_age_days,
    }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['admin-catalog-quality'] }),
  });

  // The POST only starts the background job. Refreshing rows in onSuccess
  // races the worker and cached the old empty response until a manual reload.
  // A finished_at transition is the actual publication boundary.
  useEffect(() => {
    if (upgradeStatus.data?.finished_at) {
      void client.invalidateQueries({ queryKey: ['admin-catalog-rows'] });
      void client.invalidateQueries({ queryKey: ['admin-upgrade-preflight'] });
    }
  }, [client, upgradeStatus.data?.finished_at]);

  const rows = (query.data?.rows ?? []).filter((row) =>
    row.name.toLowerCase().includes(search.toLowerCase()),
  );
  const personalRows = query.data?.personal ?? [];
  const selectedSynth = query.data?.rows.find((row) => row.id === previewId);
  const selectedPersonal = personalRows.find((row) => row.id === personalPreviewId);
  const pendingTags = Number(preflight.data?.remaining.retag ?? 0);
  const running = Boolean(preflight.data?.running || upgradeStatus.data?.running);
  const canPublish = !preflight.isLoading && !preflight.error && pendingTags === 0;
  const rebuildFailed = Boolean(
    upgradeStatus.data?.steps.some((step) => step.status === 'failed'),
  );
  return (
    <>
      <PageHeader
        title="Catalogs"
        description="See what each discovery row contains and rebuild synthesized and personal discovery rows."
        actions={
          <ConfirmButton
            prompt="Build and atomically publish qualified synthesized rows, then refresh personal rows? If the shelf editor is enabled, unchanged decisions are cached and new reviews are capped at the configured ceiling."
            onConfirm={() => regenerate.mutate()}
            disabled={regenerate.isPending || running || !canPublish}
          >
            <DatabaseZap size={16} />
            {regenerate.isPending ? 'Starting…' : 'Rebuild discovery rows'}
          </ConfirmButton>
        }
      />
      {pendingTags > 0 ? (
        <p className="formError" role="alert">
          Catalog publication is locked while {pendingTags.toLocaleString()} quality-eligible titles still need the current tag version. Complete tagging first; pilots never replace the existing rows.
        </p>
      ) : null}
      {regenerate.isSuccess ? (
        <p className="globalNotice" role="status">
          {running
            ? 'Synthesized and personal row rebuild is running. This page will refresh when it finishes.'
            : rebuildFailed
              ? 'The rebuild needs attention. Open Tagging to see the failed step.'
              : 'Synthesized and personal rows were rebuilt and this list has been refreshed.'}
        </p>
      ) : null}
      {regenerate.error ? (
        <p className="formError" role="alert">{regenerate.error.message}</p>
      ) : null}
      <ReconnectNotice reconnecting={query.isError && !!query.data} onRetry={() => void query.refetch()} />
      {query.isLoading ? <LoadingPanel label="Loading catalogs" /> : query.error && !query.data ? <ErrorPanel error={query.error} onRetry={() => void query.refetch()} /> : null}
      {query.data ? <>
      <StatGrid items={[
        { label: 'Synthesized rows', value: query.data.count.toLocaleString() },
        { label: 'Personal rows', value: Number(query.data.personal_rows ?? 0).toLocaleString() },
        { label: 'Profiles with personal rows', value: Number(query.data.personal_profiles ?? 0).toLocaleString() },
        { label: 'Stored rows suppressed', value: Number(query.data.suppressed_rows ?? 0).toLocaleString() },
      ]} />
      </> : null}
      <PageTabs tabs={[{ value: 'rows', label: 'Rows' }, { value: 'quality', label: 'Quality' }, { value: 'editorial', label: 'Editorial' }]} defaultTab="rows">
        {(tab) => <>
      {tab === 'rows' && query.data ? <>
      <Panel
        actions={
          <input
            className="searchInput"
            type="search"
            value={search}
            onChange={(event) => setSearchParams((current) => { const next = new URLSearchParams(current); if (event.target.value) next.set('q', event.target.value); else next.delete('q'); return next; }, { replace: true })}
            placeholder="Search catalog rows"
            aria-label="Search catalog rows"
          />
        }
      >
        <div className="tableWrap">
          <table>
            <thead><tr><th>Row</th><th>Type</th><th>Source</th><th>Items</th><th>Serving</th><th>Formula</th></tr></thead>
            <tbody>{rows.map((row) => (
              <tr key={row.id}>
                <td data-label="Row">
                  <button
                    className="textButton"
                    onClick={() => openPreview(`synth:${row.id}`)}
                    aria-label={`Preview ${row.name}`}
                  >{row.name}</button>
                </td>
                <td data-label="Type">{row.media_type}</td>
                <td data-label="Source"><StatusPill>{row.source}</StatusPill></td>
                <td data-label="Items"><strong>{row.items.toLocaleString()}</strong></td>
                <td data-label="Serving"><StatusPill>{row.servable === false ? 'Suppressed (<24)' : 'Serving'}</StatusPill></td>
                <td data-label="Formula">{[...row.mandatory, ...row.required, ...row.optional].length} tags</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
        {!rows.length ? (
          <EmptyPanel
            title={search ? 'No matching rows' : 'No synthesized rows are published'}
            message={
              search
                ? 'Clear the search to see all catalogs.'
                : pendingTags
                  ? 'Finish the current tagging version, then publish discovery rows.'
                  : 'Run the local discovery-row rebuild to publish them.'
            }
          />
        ) : null}
      </Panel>
      <Panel title="Personal-row inspection" description="Rows with the same name belong to different profiles. Verify profile scope, BYW seed identity, basis tags, revision, and persisted similarity scores.">
        <div className="tableWrap"><table><thead><tr><th>Row</th><th>Profile</th><th>Method</th><th>Items</th><th>Serving</th><th>Seed</th></tr></thead><tbody>{personalRows.map((row) => <tr key={row.id}><td data-label="Row"><button className="textButton" onClick={() => openPreview(`personal:${row.id}`)} aria-label={`Preview ${row.name} for ${row.profile_name || `Profile ${row.user_id}`}`}>{row.name}</button></td><td data-label="Profile">{row.profile_name || `Profile ${row.user_id}`}</td><td data-label="Method">{row.generation_method}</td><td data-label="Items">{row.items}</td><td data-label="Serving"><StatusPill>{row.servable === false ? 'Suppressed (<24)' : 'Serving'}</StatusPill></td><td data-label="Seed">{row.seed?.title ?? '—'}</td></tr>)}</tbody></table></div>
        {!personalRows.length ? <EmptyPanel title="No personal rows" message="Personal rows appear after a profile has completed playback and its catalogs rebuild." /> : null}
      </Panel>
      </> : null}
      {tab === 'quality' ? <>
      <div className="twoColumn">
        <Panel title="Recommendation quality floor" description="Preview how many titles survive before saving; changes apply to the next row rebuild.">
          {quality.isLoading && !quality.data ? <LoadingPanel /> : !quality.data ? <ErrorPanel error={quality.error} onRetry={() => void quality.refetch()} /> : !qualityDraft ? <LoadingPanel /> : <>
            <ReconnectNotice reconnecting={quality.isError} onRetry={() => void quality.refetch()} />
            <div className="formGrid">
              <Field label="Minimum quality"><input type="number" step="0.1" value={qualityDraft.catalog_min_quality} onChange={(event) => setQualityDraft({ ...qualityDraft, catalog_min_quality: Number(event.target.value) })} /></Field>
              <Field label="Minimum votes"><input type="number" min="0" value={qualityDraft.catalog_min_vote_count} onChange={(event) => setQualityDraft({ ...qualityDraft, catalog_min_vote_count: Number(event.target.value) })} /></Field>
              <Field label="Minimum rating sources"><input type="number" min="0" value={qualityDraft.catalog_min_rating_sources} onChange={(event) => setQualityDraft({ ...qualityDraft, catalog_min_rating_sources: Number(event.target.value) })} /></Field>
              <Field label="Older niche quality"><input type="number" step="0.1" value={qualityDraft.catalog_niche_min_quality} onChange={(event) => setQualityDraft({ ...qualityDraft, catalog_niche_min_quality: Number(event.target.value) })} /></Field>
              <Field label="Older niche votes"><input type="number" min="0" value={qualityDraft.catalog_niche_min_vote_count} onChange={(event) => setQualityDraft({ ...qualityDraft, catalog_niche_min_vote_count: Number(event.target.value) })} /></Field>
              <Field label="Older niche age (days)"><input type="number" min="0" value={qualityDraft.catalog_niche_min_age_days} onChange={(event) => setQualityDraft({ ...qualityDraft, catalog_niche_min_age_days: Number(event.target.value) })} /></Field>
            </div>
            {(() => { const result = previewQuality.data ?? quality.data; return <><div className="statusLine"><span>Movies kept</span><strong>{result.counts.movie.kept.toLocaleString()} <small>({result.counts.movie.pct}%)</small></strong></div><div className="statusLine"><span>Shows kept</span><strong>{result.counts.tv.kept.toLocaleString()} <small>({result.counts.tv.pct}%)</small></strong></div></>; })()}
            <div className="formActions"><Button variant="secondary" disabled={previewQuality.isPending} onClick={() => previewQuality.mutate()}>Preview floor</Button><ConfirmButton disabled={saveQuality.isPending} prompt="Save this recommendation quality floor? Catalog contents change only after the next discovery-row rebuild." onConfirm={() => saveQuality.mutate()}>Save floor</ConfirmButton></div>
            {previewQuality.error || saveQuality.error ? <p className="formError">{(previewQuality.error || saveQuality.error)?.message}</p> : null}
          </>}
        </Panel>
        <Panel title="Tag relevance" description="Recompute normalized tag weights after taxonomy or alias changes. No AI calls.">
          <ConfirmButton variant="secondary" disabled={relevance.isPending || running} prompt="Recompute tag relevance across the current tagged corpus?" onConfirm={() => relevance.mutate()}><RefreshCw size={16} /> Compute tag relevance</ConfirmButton>
          {relevance.isSuccess ? <p className="formSuccess">Tag relevance was recomputed. Rebuild discovery rows to publish the new ordering.</p> : null}
          {relevance.error ? <p className="formError">{relevance.error.message}</p> : null}
        </Panel>
      </div>
      </> : null}
      {tab === 'editorial' ? <>
      <Panel title="Synthesized-row editorial controls" description="Exclude noisy concepts, set row rotation, and preview the next generated set before publishing.">
        {synthConfig.isLoading && !synthConfig.data ? <LoadingPanel /> : !synthConfig.data ? <ErrorPanel error={synthConfig.error} onRetry={() => void synthConfig.refetch()} /> : !synthDraft ? <LoadingPanel /> : <>
          <ReconnectNotice reconnecting={synthConfig.isError} onRetry={() => void synthConfig.refetch()} />
          <div className="formGrid">
            <Field label="Excluded tags" hint="Comma-separated canonical tag names."><input value={synthDraft.excluded_tags} onChange={(event) => setSynthDraft({ ...synthDraft, excluded_tags: event.target.value })} placeholder="Tag A, Tag B" /></Field>
            <Field label="Excluded words" hint="Generated row names containing these words are rejected."><input value={synthDraft.excluded_words} onChange={(event) => setSynthDraft({ ...synthDraft, excluded_words: event.target.value })} placeholder="Word A, Word B" /></Field>
            <Field label="Rotation period (days)" hint="Use 0 to disable time-based rotation."><input type="number" min="0" value={synthDraft.rotation_days} onChange={(event) => setSynthDraft({ ...synthDraft, rotation_days: Number(event.target.value) })} /></Field>
            <Field label="AI shelf editor" hint="Reviews universal shelf summaries only; never profile history."><select value={synthDraft.editor_enabled ? 'enabled' : 'disabled'} onChange={(event) => setSynthDraft({ ...synthDraft, editor_enabled: event.target.value === 'enabled' })}><option value="disabled">Disabled — deterministic names</option><option value="enabled">Enabled — gate and rename</option></select></Field>
            <Field label="Shelf-editor provider"><select value={synthDraft.editor_provider} onChange={(event) => { const provider = synthConfig.data?.providers.find((item) => item.id === event.target.value); if (provider) setSynthDraft({ ...synthDraft, editor_provider: provider.id, editor_model: provider.model }); }}>{(synthConfig.data?.providers ?? []).map((provider) => <option key={provider.id} value={provider.id}>{provider.label}{provider.key_set ? '' : ' — key missing'}</option>)}</select></Field>
            <Field label="Shelf-editor model"><select value={synthDraft.editor_model} onChange={(event) => setSynthDraft({ ...synthDraft, editor_model: event.target.value })}>{(synthConfig.data?.providers.find((provider) => provider.id === synthDraft.editor_provider)?.models ?? []).map((model) => <option key={model.id} value={model.id}>{model.label}</option>)}</select></Field>
            <Field label="Editorial ceiling (USD)" hint="Hard reserved and measured ceiling per complete rebuild."><input type="number" min="0.01" max="5" step="0.01" value={synthDraft.editor_cost_ceiling_usd} onChange={(event) => setSynthDraft({ ...synthDraft, editor_cost_ceiling_usd: Number(event.target.value) })} /></Field>
          </div>
          <div className="formActions"><Button variant="secondary" disabled={saveSynthConfig.isPending} onClick={() => saveSynthConfig.mutate()}>Save editorial controls</Button><Button variant="secondary" disabled={synthPreview.isPending || running} onClick={() => synthPreview.mutate()}>{synthPreview.isPending ? 'Computing preview…' : 'Preview next rows'}</Button></div>
          {saveSynthConfig.isSuccess ? <p className="formSuccess">Editorial controls saved.</p> : null}
          {saveSynthConfig.error ? <p className="formError">{saveSynthConfig.error.message}</p> : null}
          {synthPreview.error ? <p className="formError">{synthPreview.error.message}</p> : null}
          {synthPreview.data ? <><p className="globalNotice">{synthPreview.data.count} qualified · {synthPreview.data.rejected.length} rejected · {synthPreview.data.cached} cached decisions · {synthPreview.data.reviewed} reviewed · ${synthPreview.data.cost_usd.toFixed(4)} actual / ${synthPreview.data.estimated_cost_usd.toFixed(4)} reserved</p><div className="previewList">{synthPreview.data.rows.map((row) => <div key={row.id}><span><strong>{row.name}</strong><small>{row.deterministic_name !== row.name ? `Template: ${row.deterministic_name} · ` : ''}{row.media_type} · {row.actual_item_count} post-filter items · score {row.editorial_score} · {row.cache_status}</small><small>{row.sample_titles.slice(0, 5).map((item) => `${item.title}${item.year ? ` (${item.year})` : ''}`).join(' · ')}</small></span><code>support {row.support} · lift {row.lift.toFixed(2)}</code></div>)}</div>{synthPreview.data.rejected.length ? <div className="previewList">{synthPreview.data.rejected.slice(0, 30).map((row, index) => <div key={row.id ?? row.candidate_id ?? index}><span><strong>{row.name ?? row.deterministic_name ?? row.id ?? row.candidate_id}</strong><small>{row.actual_item_count ?? 0} post-filter items</small></span><StatusPill>{row.reason_codes.join(', ')}</StatusPill></div>)}</div> : null}</> : null}
        </>}
      </Panel>
      </> : null}
        </>}
      </PageTabs>
      <SidePanel open={preview !== null} title={selectedSynth?.name ?? selectedPersonal?.name ?? 'Catalog preview'} description={selectedPersonal ? `${selectedPersonal.profile_name} · ${selectedPersonal.media_type}` : selectedSynth ? `${selectedSynth.media_type} · ${selectedSynth.source}` : undefined} onClose={closePreview}>
        {!validPreview ? <EmptyPanel title="Invalid catalog link" message="This preview link does not identify a catalog row." /> : previewKind === 'synth' ? <CatalogRowPreview id={previewId} /> : <PersonalRowPreview id={personalPreviewId} />}
      </SidePanel>
    </>
  );
}

export function TaggingPage() {
  const client = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const search = searchParams.get('q') ?? '';
  const deferredSearch = useDeferredValue(search);
  const pageValue = Number(searchParams.get('page') ?? '1');
  const page = Number.isSafeInteger(pageValue) && pageValue > 0 ? pageValue : 1;
  const offset = (page - 1) * 50;
  function setPage(nextPage: number) {
    setSearchParams((current) => { const next = new URLSearchParams(current); if (nextPage > 1) next.set('page', String(nextPage)); else next.delete('page'); return next; });
  }
  const [skipped, setSkipped] = useState<string[]>([]);
  const [minVotes, setMinVotes] = useState('');
  const [tmdbKey, setTmdbKey] = useState('');
  const [mdbListKey, setMdbListKey] = useState('');
  const [providerChoice, setProviderChoice] = useState<TaggingProviderOption['id'] | ''>('');
  const [modelChoice, setModelChoice] = useState('');
  const [providerKey, setProviderKey] = useState('');
  const [localBaseUrl, setLocalBaseUrl] = useState('');
  const limit = 50;

  const query = useQuery({
    queryKey: ['admin-tags', deferredSearch, offset],
    queryFn: () =>
      adminApi<TagResponse>(
        `/tags/list?limit=${limit}&offset=${offset}&q=${encodeURIComponent(deferredSearch)}`,
      ),
    placeholderData: keepPreviousData,
  });
  const overview = useQuery({
    queryKey: ['admin-tags-overview'],
    queryFn: () => adminApi<Record<string, unknown>>('/tags/overview'),
  });
  const preflight = useQuery({
    queryKey: ['admin-upgrade-preflight'],
    queryFn: () => adminApi<UpgradePreflight>('/tagging/upgrade/preflight'),
    refetchInterval: 5000,
    refetchIntervalInBackground: false,
  });
  const upgradeStatus = useQuery({
    queryKey: ['admin-upgrade-status'],
    queryFn: () => adminApi<UpgradeStatus>('/tagging/upgrade/status'),
    refetchInterval: 3000,
    refetchIntervalInBackground: false,
  });
  const toggle = useMutation({
    mutationFn: (id: number) => postAdmin(`/tags/${id}/toggle`),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['admin-tags'] });
      void client.invalidateQueries({ queryKey: ['admin-tags-overview'] });
    },
  });
  const startUpgrade = useMutation({
    mutationFn: (pilot: boolean) =>
      postAdmin('/tagging/upgrade/start', {
        ...(pilot ? { limit: 1000 } : {}),
        ...(minVotes ? { min_votes: Number(minVotes) } : {}),
        skip: pilot
          ? Array.from(new Set([...skipped, 'relevance', 'catalogs', 'users']))
          : skipped,
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['admin-upgrade-status'] });
      void client.invalidateQueries({ queryKey: ['admin-upgrade-preflight'] });
    },
  });
  const stopUpgrade = useMutation({
    mutationFn: () => postAdmin('/tagging/upgrade/stop', {}),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['admin-upgrade-status'] }),
  });
  const plan = preflight.data;
  const providers: TaggingProviderOption[] = plan?.providers ?? [{
    id: 'gemini',
    label: 'Google Gemini',
    key_set: Boolean(plan?.keys.gemini),
    key_hint: '',
    model: plan?.model ?? 'gemini-2.5-flash-lite',
    models: [{ id: plan?.model ?? 'gemini-2.5-flash-lite', label: plan?.model ?? 'Gemini' }],
  }];
  const selectedProvider = providerChoice || plan?.provider || 'gemini';
  const selectedProviderSpec = providers.find((item) => item.id === selectedProvider) ?? providers[0];
  const configuredModel = selectedProviderSpec?.model || plan?.model || '';
  const configuredModelSupported = Boolean(selectedProviderSpec?.models.some((model) => model.id === configuredModel));
  const freeModel = Boolean(selectedProviderSpec?.free_model);
  const selectedModel = modelChoice || (configuredModelSupported || freeModel ? configuredModel : selectedProviderSpec?.models[0]?.id) || '';
  const selectedModelSpec = selectedProviderSpec?.models.find((model) => model.id === selectedModel);
  const selectedModelProductionReady = selectedModelSpec?.production_ready
    ?? (selectedModel === plan?.model ? plan?.model_production_ready : undefined)
    ?? true;
  const providerKeyField = `${selectedProvider}_api_key`;

  const saveKeys = useMutation({
    mutationFn: () =>
      postAdmin('/tagging/upgrade/keys', {
        ...(tmdbKey ? { tmdb_api_key: tmdbKey } : {}),
        ...(mdbListKey ? { mdblist_api_key: mdbListKey } : {}),
        tagging_provider: selectedProvider,
        model: selectedModel,
        ...(providerKey ? { [providerKeyField]: providerKey } : {}),
        ...(selectedProvider === 'local' && localBaseUrl ? { local_base_url: localBaseUrl } : {}),
      }),
    onSuccess: () => {
      setTmdbKey('');
      setMdbListKey('');
      setProviderKey('');
      setLocalBaseUrl('');
      setProviderChoice('');
      setModelChoice('');
      void client.invalidateQueries({ queryKey: ['admin-upgrade-preflight'] });
    },
  });

  const live = upgradeStatus.data;
  const running = Boolean(plan?.running || live?.running);
  const remaining = Number(plan?.remaining.retag ?? 0);
  const providerSettingsDirty = Boolean(providerChoice || modelChoice || providerKey || localBaseUrl || tmdbKey || mdbListKey);
  const canRun = Boolean(plan?.ready && !running && !providerSettingsDirty && remaining > 0);
  const hasFailed = Boolean(live?.steps.some((step) => step.status === 'failed'));
  const retagStats = live?.steps.find((step) => step.key === 'retag')?.stats as RetagStats | undefined;
  const lowTagDiagnostics = retagStats?.low_tag_diagnostics ?? [];
  const tagData = query.data;

  return (
    <>
      <PageHeader
        title="Tagging"
        description="Inspect vocabulary coverage and run the final quality-corpus tagging upgrade."
        actions={
          <Button variant="secondary" onClick={() => {
            void query.refetch();
            void overview.refetch();
            void preflight.refetch();
            void upgradeStatus.refetch();
          }}><RefreshCw size={16} /> Refresh</Button>
        }
      />
      <ReconnectNotice reconnecting={(query.isError && !!query.data) || (preflight.isError && !!plan) || (upgradeStatus.isError && !!live) || (overview.isError && !!overview.data)} onRetry={() => { void query.refetch(); void preflight.refetch(); void upgradeStatus.refetch(); void overview.refetch(); }} />
      <PageTabs tabs={[{ value: 'upgrade', label: 'Upgrade' }, { value: 'library', label: 'Library & tag pack' }, { value: 'vocabulary', label: 'Vocabulary' }]} defaultTab="upgrade">
        {(tab) => <>
      {tab === 'library' ? <LibraryTaggingPanel /> : null}
      {tab === 'upgrade' ? <>

      <Panel
        title="Final tagging upgrade"
        description="One resumable pipeline repairs source facts, tags only quality-eligible titles, computes relevance, publishes synthesized rows, and refreshes personal rows."
      >
        {preflight.isLoading ? <LoadingPanel label="Checking the upgrade plan" /> : null}
        {(preflight.error && !plan) || (!preflight.isLoading && !plan) ? (
          <ErrorPanel error={preflight.error} onRetry={() => void preflight.refetch()} />
        ) : null}
        {plan ? (
          <>
            <StatGrid items={[
              { label: 'Quality eligible', value: plan.eligible_titles.toLocaleString() },
              { label: 'Excluded from recommendations', value: plan.excluded_by_quality.toLocaleString() },
              { label: 'Still to tag', value: remaining.toLocaleString() },
              {
                label: 'Estimated remaining cost',
                value: `$${plan.est_cost_usd.toFixed(2)}`,
                hint: `${Number(plan.estimated_ai_titles ?? remaining).toLocaleString()} may need AI · ${Number(plan.estimated_local_only ?? 0).toLocaleString()} local-only · $${plan.cost_ceiling_usd.toFixed(2)} ceiling`,
              },
            ]} />
            <div className="upgradeIdentity">
              <span>Target <code>{plan.tagger_version}</code></span>
              <span>Provider <code>{plan.provider_label ?? 'Google Gemini'}</code></span>
              <span>Model <code>{plan.model}</code></span>
            </div>
            {plan.est_cost_usd > plan.cost_ceiling_usd ? (
              <p className="formError" role="alert">
                The estimate exceeds the spending ceiling. Do not start until the ceiling or scope is reviewed.
              </p>
            ) : null}
            {!selectedModelProductionReady ? (
              <p className="formError" role="alert">
                <strong>Pilot-only model.</strong>{' '}
                {selectedModelSpec?.warning ?? 'Run at most 1,000 titles and review the export before choosing a production-ready model for completion.'}
              </p>
            ) : null}
            <div className="upgradeKeys">
              {!plan.ready ? <p className="formError">Missing required credentials: {plan.missing_required.join(', ')}.</p> : null}
              <div className="formGrid">
                <Field label="Tagging provider">
                  <select
                    aria-label="Tagging provider"
                    value={selectedProvider}
                    disabled={running}
                    onChange={(event) => {
                      const next = event.target.value as TaggingProviderOption['id'];
                      const nextSpec = providers.find((item) => item.id === next);
                      setProviderChoice(next);
                      setModelChoice(nextSpec?.model ?? '');
                      setProviderKey('');
                    }}
                  >
                    {providers.map((provider) => <option key={provider.id} value={provider.id}>{provider.label}</option>)}
                  </select>
                </Field>
                {selectedProvider === 'local' ? (
                  <Field label="Local model address" hint={selectedProviderSpec?.base_url ? `Saved ${selectedProviderSpec.base_url}. Leave blank to keep it.` : 'The OpenAI-compatible base URL, e.g. http://localhost:11434/v1 for Ollama.'}>
                    <input
                      type="url"
                      aria-label="Local model address"
                      value={localBaseUrl}
                      disabled={running}
                      onChange={(event) => setLocalBaseUrl(event.target.value)}
                      placeholder={selectedProviderSpec?.base_url || 'http://localhost:11434/v1'}
                    />
                  </Field>
                ) : null}
                <Field label="Model" hint={freeModel ? 'The model name on your local server. Small models tag less accurately.' : undefined}>
                  {freeModel ? (
                    <input
                      aria-label="Tagging model"
                      value={selectedModel}
                      disabled={running}
                      onChange={(event) => setModelChoice(event.target.value)}
                      placeholder="qwen2.5:14b"
                    />
                  ) : (
                    <select
                      aria-label="Tagging model"
                      value={selectedModel}
                      disabled={running}
                      onChange={(event) => setModelChoice(event.target.value)}
                    >
                      {(selectedProviderSpec?.models ?? []).map((model) => <option key={model.id} value={model.id}>{model.label}</option>)}
                    </select>
                  )}
                </Field>
                <Field
                  label={`${selectedProviderSpec?.label ?? 'AI provider'} API key`}
                  hint={selectedProviderSpec?.key_set ? `Saved ${selectedProviderSpec.key_hint || '(hidden)'}. Leave blank to keep it.` : selectedProvider === 'local' ? 'Optional: most local servers need no key.' : 'Required before re-tagging.'}
                >
                  <input
                    type="password"
                    value={providerKey}
                    disabled={running}
                    autoComplete="new-password"
                    onChange={(event) => setProviderKey(event.target.value)}
                    placeholder={selectedProviderSpec?.key_set ? 'Keep saved key' : 'Paste API key'}
                  />
                </Field>
                {!plan.keys.tmdb ? <Field label="TMDB API key"><input type="password" value={tmdbKey} onChange={(event) => setTmdbKey(event.target.value)} /></Field> : null}
                {!plan.keys.mdblist ? <Field label="MDBList API key"><input type="password" value={mdbListKey} onChange={(event) => setMdbListKey(event.target.value)} /></Field> : null}
              </div>
              <p className="fieldHint">Claude/Claude Code and ChatGPT subscriptions do not include Anthropic or OpenAI API usage; use a provider API key with billing enabled.</p>
              <Button
                variant="secondary"
                disabled={saveKeys.isPending || running || (!tmdbKey && !mdbListKey && !providerKey && !providerChoice && !modelChoice)}
                onClick={() => saveKeys.mutate()}
              >Save provider settings</Button>
              {saveKeys.error ? <p className="formError">{saveKeys.error.message}</p> : null}
              {saveKeys.isSuccess ? <p className="formSuccess">Provider settings saved. Review the updated estimate before starting.</p> : null}
            </div>
            <fieldset className="upgradeSteps">
              <legend>Pipeline steps</legend>
              {plan.steps.map((step) => (
                <label key={step.key}>
                  <input
                    type="checkbox"
                    checked={!skipped.includes(step.key)}
                    disabled={!step.available || running}
                    onChange={() => setSkipped((current) =>
                      current.includes(step.key)
                        ? current.filter((key) => key !== step.key)
                        : [...current, step.key]
                    )}
                  />
                  <span><strong>{step.title}</strong><small>{step.remaining ? `${step.remaining.toLocaleString()} to do` : 'ready'}</small></span>
                </label>
              ))}
            </fieldset>
            <div className="upgradeControls">
              <Field label="Additional minimum TMDB votes" hint="Leave blank to use Curatio's quality gate.">
                <input type="number" min="0" value={minVotes} onChange={(event) => setMinVotes(event.target.value)} placeholder="No additional minimum" />
              </Field>
              <div className="formActions">
                <ConfirmButton
                  prompt="Run a 1,000-title validation pilot? Successful titles will be kept, but catalogs will not publish until the completion run."
                  onConfirm={() => startUpgrade.mutate(true)}
                  disabled={!canRun || startUpgrade.isPending}
                ><Play size={16} /> Run 1,000-title pilot</ConfirmButton>
                <ConfirmButton
                  prompt={`Complete the remaining ${remaining.toLocaleString()} quality-eligible titles? Successful current-version titles will be skipped.`}
                  onConfirm={() => startUpgrade.mutate(false)}
                  disabled={!canRun || startUpgrade.isPending || !plan.model_production_ready}
                ><DatabaseZap size={16} /> Complete remaining titles</ConfirmButton>
                {running ? (
                  <ConfirmButton
                    variant="danger"
                    prompt="Stop after the current chunk? Completed titles will be kept."
                    onConfirm={() => stopUpgrade.mutate()}
                    disabled={stopUpgrade.isPending}
                  ><CircleStop size={16} /> Stop safely</ConfirmButton>
                ) : null}
              </div>
            </div>
            {remaining === 0 ? (
              <p className="formSuccess">The current tagging version is complete. Catalog and personal-row regeneration remain local and make no AI API calls.</p>
            ) : null}
            {startUpgrade.error ? <p className="formError" role="alert">{startUpgrade.error.message}</p> : null}
          </>
        ) : null}
      </Panel>

      {live?.steps.length ? (
        <Panel
          title={live.running ? 'Upgrade running' : 'Latest upgrade'}
          description={live.running ? `Started ${formatDate(live.started_at)}` : `Finished ${formatDate(live.finished_at)}`}
          actions={<StatusPill tone={live.running ? 'warn' : hasFailed ? 'bad' : 'good'}>{live.running ? 'Running' : hasFailed ? 'Needs attention' : 'Complete'}</StatusPill>}
        >
          <div className="upgradeProgress">
            {live.steps.map((step) => (
              <div key={step.key}>
                <StatusPill tone={step.status === 'completed' ? 'good' : step.status === 'failed' ? 'bad' : step.status === 'running' ? 'warn' : 'neutral'}>{step.status || 'pending'}</StatusPill>
                <span><strong>{step.title}</strong>{step.detail ? <small>{step.detail}</small> : null}</span>
              </div>
            ))}
          </div>
          {retagStats ? (
            <StatGrid items={[
              { label: 'Tagged', value: Number(retagStats.processed ?? 0).toLocaleString() },
              { label: 'Excluded', value: Number(retagStats.requeued ?? 0).toLocaleString() },
              { label: 'Hard failed', value: Number(retagStats.failed ?? 0).toLocaleString() },
              { label: 'No AI needed', value: Number(retagStats.model_skips ?? 0).toLocaleString(), hint: 'Factual tags written locally' },
              { label: 'AI omissions', value: Number(retagStats.model_omissions ?? 0).toLocaleString(), hint: 'Title still kept with factual tags' },
              { label: 'Provider errors', value: Number(retagStats.provider_failures ?? 0).toLocaleString(), hint: 'Optional nuance unavailable; title still kept' },
            ]} />
          ) : null}
          {lowTagDiagnostics.length ? (
            <details className="advancedDisclosure">
              <summary>{lowTagDiagnostics.length.toLocaleString()} sampled low-tag diagnostics</summary>
              <ul className="issueList">
                {lowTagDiagnostics.slice(0, 10).map((diagnostic) => (
                  <li key={`${diagnostic.tmdb_id}-${diagnostic.title ?? ''}`}>
                    <strong>{diagnostic.title || `TMDB ${diagnostic.tmdb_id}`}</strong>
                    {`: ${Number(diagnostic.returned ?? 0)} returned, ${Number(diagnostic.surviving_model_tags ?? 0)} survived`}
                    {diagnostic.rejected_reasons
                      ? ` (${Object.entries(diagnostic.rejected_reasons).map(([reason, count]) => `${reason}: ${count}`).join(', ')})`
                      : ''}
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
          {live.error ? <p className="formError">{live.error}</p> : null}
        </Panel>
      ) : null}
      </> : null}
      {tab === 'vocabulary' ? <>

      <Panel title="Vocabulary health" description="A concise snapshot of the current tag system.">
        {overview.isLoading ? <LoadingPanel /> : overview.error && !overview.data ? <ErrorPanel error={overview.error} onRetry={() => void overview.refetch()} /> : (
          <div className="jsonSummary">{Object.entries(overview.data ?? {})
            .filter(([, value]) => ['number', 'string', 'boolean'].includes(typeof value))
            .slice(0, 8)
            .map(([key, value]) => <div key={key}><span>{key.replaceAll('_', ' ')}</span><strong>{String(value)}</strong></div>)}</div>
        )}
      </Panel>

      <Panel actions={<div className="tableToolbar"><input className="searchInput" type="search" value={search} onChange={(event) => { const value = event.target.value; setSearchParams((current) => { const next = new URLSearchParams(current); if (value) next.set('q', value); else next.delete('q'); next.delete('page'); return next; }, { replace: true }); }} placeholder="Search tags" aria-label="Search tags" />{tagData ? <span>{tagData.total.toLocaleString()} tags</span> : null}</div>}>
        {query.isLoading ? <LoadingPanel label="Loading tags" /> : !tagData ? <ErrorPanel error={query.error} onRetry={() => void query.refetch()} /> : (
          <>
            <ReconnectNotice reconnecting={query.isError} onRetry={() => void query.refetch()} />
            {query.isPlaceholderData ? <p className="muted" role="status">Updating tags…</p> : null}
            {tagData.items.length ? (
              <div className="tableWrap"><table><thead><tr><th>Tag</th><th>Facet</th><th>Documents</th><th>Catalog rows</th><th>Status</th><th>Actions</th></tr></thead><tbody>{tagData.items.map((tag) => <tr key={tag.id}><td data-label="Tag"><strong>{tag.name}</strong>{tag.description ? <small>{tag.description}</small> : null}</td><td data-label="Facet">{tag.facet || '—'}</td><td data-label="Documents">{tag.total_docs.toLocaleString()}</td><td data-label="Catalog rows">{tag.category_refs.length}</td><td data-label="Status"><StatusPill tone={tag.is_active ? 'good' : 'neutral'}>{tag.is_active ? 'Active' : 'Inactive'}</StatusPill></td><td data-label="Actions"><Button variant="secondary" size="compact" disabled={toggle.isPending} onClick={() => toggle.mutate(tag.id)}>{tag.is_active ? 'Disable' : 'Enable'}</Button></td></tr>)}</tbody></table></div>
            ) : <EmptyPanel title="No tags found" message="Try a broader search." />}
            <div className="pagination"><Button variant="secondary" disabled={offset === 0 || query.isPlaceholderData} onClick={() => setPage(page - 1)}>Previous</Button><span>Showing {tagData.total ? offset + 1 : 0}–{Math.min(offset + limit, tagData.total)} of {tagData.total}</span><Button variant="secondary" disabled={!tagData.has_more || query.isPlaceholderData} onClick={() => setPage(page + 1)}>Next</Button></div>
          </>
        )}
      </Panel>
      </> : null}
        </>}
      </PageTabs>
    </>
  );
}
