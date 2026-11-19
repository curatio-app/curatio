import { FormEvent, useEffect, useState } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { CircleStop, Pause, Play, RefreshCw, RotateCcw, Save } from 'lucide-react';
import { adminApi, formatDuration, postAdmin } from '../api';
import { Button, ConfirmButton, EmptyPanel, ErrorPanel, Field, LoadingPanel, PageHeader, PageTabs, Panel, ReconnectNotice, SidePanel, StatGrid, StatusPill } from '../components';

interface BuildStatus { running: boolean; paused: boolean; job_type?: string; elapsed_seconds?: number; movies_tagged?: number; shows_tagged?: number; }

export function JobsPage() {
  const client = useQueryClient(); const [movies, setMovies] = useState(100000); const [shows, setShows] = useState(50000);
  const status = useQuery({ queryKey: ['admin-build-status'], queryFn: () => adminApi<BuildStatus>('/build/status'), refetchInterval: 5000, refetchIntervalInBackground: false });
  const logs = useQuery({ queryKey: ['admin-build-logs'], queryFn: () => adminApi<{ lines: string[]; total: number }>('/build/logs'), refetchInterval: status.data?.running ? 3000 : false, refetchIntervalInBackground: false });
  const action = useMutation({ mutationFn: ({ path, body }: { path: string; body?: unknown }) => postAdmin(path, body), onSuccess: () => { void client.invalidateQueries({ queryKey: ['admin-build-status'] }); void client.invalidateQueries({ queryKey: ['admin-overview'] }); } });
  const running = Boolean(status.data?.running);
  return <><PageHeader title="Jobs & build" description="Run acquisition and catalog maintenance without losing sight of current progress." actions={<StatusPill tone={running ? 'warn' : 'good'}>{running ? status.data?.paused ? 'Paused' : 'Running' : 'Idle'}</StatusPill>} />
    <ReconnectNotice reconnecting={Boolean(status.error && status.data)} onRetry={() => void status.refetch()} />
    {status.isLoading && !status.data ? <LoadingPanel label="Loading job status" /> : null}
    {status.error && !status.data ? <ErrorPanel error={status.error} onRetry={() => void status.refetch()} /> : null}
    {status.data ? <>
    {running ? <Panel title={status.data.job_type || 'Build in progress'} description={`Running for ${formatDuration(status.data.elapsed_seconds)}`}><StatGrid items={[{ label: 'Movies tagged', value: Number(status.data.movies_tagged ?? 0).toLocaleString() }, { label: 'Shows tagged', value: Number(status.data.shows_tagged ?? 0).toLocaleString() }]} /><div className="formActions"><Button variant="secondary" onClick={() => action.mutate({ path: status.data.paused ? '/build/resume' : '/build/pause' })}>{status.data.paused ? <Play size={16} /> : <Pause size={16} />}{status.data.paused ? 'Resume' : 'Pause'}</Button><ConfirmButton variant="danger" prompt="Stop the current build? It can be restarted later." onConfirm={() => action.mutate({ path: '/build/stop' })}><CircleStop size={16} /> Stop</ConfirmButton></div></Panel> : <Panel title="Start a database build" description="Defaults suit a full installation. Use fetch-only when widening metadata coverage without Gemini tagging."><form className="inlineForm" onSubmit={(event) => { event.preventDefault(); action.mutate({ path: '/build/start', body: { movies, shows } }); }}><Field label="Movie target"><input type="number" min="0" value={movies} onChange={(e) => setMovies(Number(e.target.value))} /></Field><Field label="Show target"><input type="number" min="0" value={shows} onChange={(e) => setShows(Number(e.target.value))} /></Field><Button disabled={action.isPending}><Play size={16} /> Start build</Button></form><div className="secondaryActions"><Button variant="secondary" onClick={() => action.mutate({ path: '/build/start', body: { movies, shows, fetch_only: true } })}>Fetch metadata only</Button><ConfirmButton variant="secondary" prompt="Run the daily update now?" onConfirm={() => action.mutate({ path: '/build/daily' })}><RefreshCw size={16} /> Run daily update</ConfirmButton><Button variant="secondary" onClick={() => action.mutate({ path: '/sync-users' })}>Sync viewer rows</Button></div></Panel>}
    {action.error ? <p className="formError" role="alert">{action.error.message}</p> : null}
    <Panel title="Build log" description="The latest server messages from the active or most recent build."><pre className="logViewer" tabIndex={0}>{logs.data?.lines.slice(-150).join('\n') || 'No build log entries yet.'}</pre></Panel>
    </> : null}
  </>;
}

interface SettingsResponse { schedule: { DAILY_UPDATE_ENABLED: boolean; DAILY_UPDATE_TIME: string } }
export function SchedulePage() {
  const client = useQueryClient(); const query = useQuery({ queryKey: ['admin-settings'], queryFn: () => adminApi<SettingsResponse>('/settings') }); const [draft, setDraft] = useState<SettingsResponse['schedule'] | null>(null);
  useEffect(() => { if (query.data && !draft) setDraft(query.data.schedule); }, [query.data, draft]);
  const save = useMutation({ mutationFn: () => postAdmin('/settings', draft), onSuccess: () => { setDraft(null); void client.invalidateQueries({ queryKey: ['admin-settings'] }); } });
  return <><PageHeader title="Schedule" description="Choose whether Curatio performs its incremental maintenance automatically." /><ReconnectNotice reconnecting={Boolean(query.error && query.data)} onRetry={() => void query.refetch()} />{(query.isLoading || !draft) && !query.data ? <LoadingPanel label="Loading schedule" /> : null}{query.error && !query.data ? <ErrorPanel error={query.error} onRetry={() => void query.refetch()} /> : null}{draft ? <Panel title="Daily update"><form className="stackForm" onSubmit={(event: FormEvent) => { event.preventDefault(); save.mutate(); }}><label className="switchField"><span><strong>Enable daily updates</strong><small>Fetch changes, refresh tags, and update materialized rows.</small></span><input type="checkbox" checked={draft.DAILY_UPDATE_ENABLED} onChange={(e) => setDraft({ ...draft, DAILY_UPDATE_ENABLED: e.target.checked })} /></label><Field label="Run time (UTC)" hint="Use 24-hour HH:MM format."><input type="time" value={draft.DAILY_UPDATE_TIME} onChange={(e) => setDraft({ ...draft, DAILY_UPDATE_TIME: e.target.value })} /></Field>{save.error ? <p className="formError" role="alert">{save.error.message}</p> : null}{save.isSuccess ? <p className="formSuccess" role="status">Schedule saved.</p> : null}<div className="formActions"><Button disabled={save.isPending}><Save size={16} /> Save schedule</Button></div></form></Panel> : null}</>;
}

interface FingerprintStatus { queue: Record<string, number>; inflight: number; total_markers: number; recent_completions_24h: number; stale_inflight_count: number; failure_rate_pct: number; seasons_no_stream: number; seasons_fingerprinted_no_markers: number; community_lookups?: Record<string, Record<string, number>>; }
interface MarkerRecord { episode: number; type: 'intro' | 'credits'; start: number; end: number; confidence: number; source?: string; detector_version?: string; boundary_status: string; }
interface SeasonRecord { season: number; episodes_total: number; episodes_fingerprinted: number; episodes_failed: number; episodes_pending: number; episodes_processing: number; markers_generated: boolean; marker_types: string[]; intro_detection_enabled: boolean; credits_detection_enabled: boolean; marker_records: MarkerRecord[]; fingerprint_records: Array<{ episode: number; status: string; error_message?: string }> }
interface FingerprintSeries { series: Array<{ imdb_id: string; title: string; seasons: SeasonRecord[] }>; total: number; has_more: boolean; skip?: number; limit?: number; }
interface RelearnStatus { state: string; seasons_total?: number; seasons_done?: number; markers_deleted?: number; failures?: number; error?: string; }

function markerTime(value: number) { const minutes = Math.floor(value / 60); const seconds = value - minutes * 60; return `${minutes}:${seconds.toFixed(1).padStart(4, '0')}`; }

function SeasonDiagnostics({ imdbID, title, season, busy, act }: { imdbID: string; title: string; season: SeasonRecord; busy: boolean; act: (path: string, body?: unknown) => void }) {
  return <div className="seasonRow"><div className="seasonDiagnostics"><span><strong>Season {season.season}</strong><small>{season.episodes_fingerprinted}/{season.episodes_total} fingerprinted · {season.marker_types.join(', ') || 'no markers'}</small></span><div className="markerGrid">{season.marker_records.length ? season.marker_records.map((marker) => <div key={`${marker.episode}-${marker.type}`}><strong>E{marker.episode} {marker.type}</strong><span>{markerTime(marker.start)}–{markerTime(marker.end)}</span><StatusPill tone={marker.boundary_status === 'confirmed' ? 'good' : 'warn'}>{Math.round(marker.confidence * 100)}% · {marker.source || 'unknown'}</StatusPill></div>) : <small>No confirmed boundaries</small>}</div></div><div className="rowActions"><Button variant="secondary" size="compact" disabled={busy} onClick={() => act(`/fingerprint-series/${imdbID}/${season.season}/prelearn`, { mode: 'conservative' })}>Queue analysis</Button><ConfirmButton variant="secondary" size="compact" disabled={busy} prompt={`Relearn automatic markers for ${title} season ${season.season}?`} onConfirm={() => act(`/fingerprint-series/${imdbID}/${season.season}/reset-learning`)}>Relearn markers</ConfirmButton><Button variant="secondary" size="compact" disabled={busy} onClick={() => act(`/fingerprint-series/${imdbID}/${season.season}/detection`, { intro_enabled: !season.intro_detection_enabled })}>{season.intro_detection_enabled ? 'Disable intros' : 'Enable intros'}</Button><Button variant="secondary" size="compact" disabled={busy} onClick={() => act(`/fingerprint-series/${imdbID}/${season.season}/detection`, { credits_enabled: !season.credits_detection_enabled })}>{season.credits_detection_enabled ? 'Disable credits' : 'Enable credits'}</Button>{season.fingerprint_records.filter((record) => record.status === 'failed').map((record) => <Button variant="secondary" size="compact" key={record.episode} title={record.error_message || 'Retry failed fingerprint'} disabled={busy} onClick={() => act(`/fingerprint-series/${imdbID}/${season.season}/${record.episode}/retry`)}>Retry E{record.episode}</Button>)}</div></div>;
}
export function FingerprintsPage() {
  const client = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const requestedPage = Number(params.get('page') ?? '1');
  const page = Number.isSafeInteger(requestedPage) && requestedPage > 0 ? requestedPage : 1;
  const skip = (page - 1) * 25;
  const selectedSeriesId = params.get('series');
  const seasonParam = params.get('season');
  const selectedSeasonNumber = seasonParam === null ? NaN : Number(seasonParam);
  const validSeasonLink = Boolean(selectedSeriesId) && Number.isSafeInteger(selectedSeasonNumber) && selectedSeasonNumber >= 0;
  const query = useQuery({ queryKey: ['admin-fingerprints'], queryFn: () => adminApi<FingerprintStatus>('/fingerprint-status'), refetchInterval: 15_000, refetchIntervalInBackground: false });
  const logs = useQuery({ queryKey: ['admin-fingerprint-logs'], queryFn: () => adminApi<{ lines: Array<{ ts?: string; level?: string; msg?: string }> }>('/fingerprint-logs?after=0&limit=100'), refetchInterval: 10_000, refetchIntervalInBackground: false });
  const series = useQuery({ queryKey: ['admin-fingerprint-series', skip], queryFn: () => adminApi<FingerprintSeries>(`/fingerprint-series?skip=${skip}&limit=25`), placeholderData: keepPreviousData });
  const selectedSeriesQuery = useQuery({ queryKey: ['admin-fingerprint-series-detail', selectedSeriesId], queryFn: () => adminApi<FingerprintSeries>(`/fingerprint-series?skip=0&limit=1&imdb_id=${encodeURIComponent(selectedSeriesId ?? '')}`), enabled: Boolean(selectedSeriesId) });
  const relearn = useQuery({ queryKey: ['admin-fingerprint-relearn'], queryFn: () => adminApi<RelearnStatus>('/fingerprint/relearn-all/status'), refetchInterval: 5000, refetchIntervalInBackground: false });
  const settings = useQuery({ queryKey: ['admin-settings'], queryFn: () => adminApi<{ fingerprint: { preset: string; description?: string } }>('/settings') });
  const action = useMutation({ mutationFn: ({ path, body }: { path: string; body?: unknown }) => postAdmin(path, body), onSuccess: () => { void client.invalidateQueries({ queryKey: ['admin-fingerprint-series'] }); void client.invalidateQueries({ queryKey: ['admin-fingerprint-series-detail'] }); void client.invalidateQueries({ queryKey: ['admin-fingerprints'] }); void client.invalidateQueries({ queryKey: ['admin-fingerprint-relearn'] }); } });
  const savePreset = useMutation({ mutationFn: (preset: string) => postAdmin('/settings', { FINGERPRINT_PRESET: preset }), onSuccess: () => void client.invalidateQueries({ queryKey: ['admin-settings'] }) });
  const relearnRunning = ['starting', 'running'].includes(relearn.data?.state ?? '');
  const selectedShow = selectedSeriesQuery.data?.series[0];
  const selectedSeason = selectedShow?.seasons.find((season) => season.season === selectedSeasonNumber);
  function setPage(nextPage: number) { const next = new URLSearchParams(params); if (nextPage <= 1) next.delete('page'); else next.set('page', String(nextPage)); setParams(next, { preventScrollReset: true }); }
  function openSeason(imdbId: string, season: number) { const next = new URLSearchParams(params); next.set('tab', 'series'); next.set('series', imdbId); next.set('season', String(season)); navigate({ search: next.toString() }, { state: { panelFromList: true } }); }
  function closeSeason() { if ((location.state as { panelFromList?: boolean } | null)?.panelFromList) navigate(-1); else { const next = new URLSearchParams(params); next.delete('series'); next.delete('season'); navigate({ search: next.toString() }, { replace: true }); } }
  const status = query.data;
  const seriesPage = series.data;

  return <>
    <PageHeader title="Fingerprinting" description="Monitor skip-marker analysis, tune automatic coverage, and repair individual seasons." actions={<Button variant="secondary" onClick={() => { void query.refetch(); void series.refetch(); void logs.refetch(); void relearn.refetch(); void settings.refetch(); }}><RefreshCw size={16} /> Refresh</Button>} />
    {action.error || savePreset.error ? <p className="formError" role="alert">{(action.error || savePreset.error)?.message}</p> : null}
    <PageTabs tabs={[{ value: 'overview', label: 'Overview' }, { value: 'series', label: 'Series' }, { value: 'logs', label: 'Logs' }]} defaultTab="overview">
      {(tab) => <>
        {tab === 'overview' ? <>
          <ReconnectNotice reconnecting={Boolean(query.isError && status) || Boolean(settings.isError && settings.data) || Boolean(relearn.isError && relearn.data)} onRetry={() => { void query.refetch(); void settings.refetch(); void relearn.refetch(); }} />
          {query.isLoading && !status ? <LoadingPanel label="Loading fingerprint status" /> : null}
          {!status && query.error ? <ErrorPanel error={query.error} onRetry={() => void query.refetch()} /> : null}
          {status ? <>
            <StatGrid items={[
              { label: 'Pending', value: status.queue.pending ?? 0 },
              { label: 'Processing', value: status.queue.processing ?? 0 },
              { label: 'Completed', value: status.queue.done ?? 0 },
              { label: 'Failed', value: status.queue.failed ?? 0 },
            ]} />
            <div className="twoColumn">
              <Panel title="Analysis health">
                <div className="statusLine"><span>Markers learned</span><strong>{status.total_markers.toLocaleString()}</strong></div>
                <div className="statusLine"><span>Completed in 24h</span><strong>{status.recent_completions_24h}</strong></div>
                <div className="statusLine"><span>Failure rate</span><StatusPill tone={status.failure_rate_pct > 10 ? 'bad' : status.failure_rate_pct > 3 ? 'warn' : 'good'}>{status.failure_rate_pct}%</StatusPill></div>
                <div className="statusLine"><span>Stale in flight</span><strong>{status.stale_inflight_count}</strong></div>
              </Panel>
              <Panel title="Coverage exceptions">
                <div className="statusLine"><span>Seasons without streams</span><strong>{status.seasons_no_stream}</strong></div>
                <div className="statusLine"><span>Fingerprinted without markers</span><strong>{status.seasons_fingerprinted_no_markers}</strong></div>
                <p className="panelFootnote">Use scoped relearning only after diagnosing stream or matching quality.</p>
              </Panel>
            </div>
            <Panel title="Community lookups" description="Episodes looked up in each intro database. Hits are kept 7 days, nothing-known 24 hours, errors 15 minutes.">
              {[['introdb', 'IntroDB'], ['theintrodb', 'TheIntroDB']].map(([source, label]) => {
                const counts = status.community_lookups?.[source] ?? {};
                return <div className="statusLine" key={source}><span>{label}</span><strong>{(counts.hit ?? 0).toLocaleString()} found · {(counts.none ?? 0).toLocaleString()} not known · {(counts.error ?? 0).toLocaleString()} error{counts.error === 1 ? '' : 's'}</strong></div>;
              })}
            </Panel>
          </> : null}
          <div className="twoColumn">
            <Panel title="Fingerprint behavior" description={settings.data?.fingerprint.description || 'Choose how broadly Curatio learns adjacent episodes.'}>
              {settings.isLoading && !settings.data ? <LoadingPanel label="Loading fingerprint settings" /> : null}
              {!settings.data && settings.error ? <ErrorPanel error={settings.error} onRetry={() => void settings.refetch()} /> : null}
              {settings.data ? <Field label="Preset"><select value={settings.data.fingerprint.preset} onChange={(event) => savePreset.mutate(event.target.value)} disabled={savePreset.isPending}><option value="CONSERVATIVE">Conservative — explicit requests only</option><option value="BALANCED">Balanced — nearby episodes</option><option value="AGGRESSIVE">Aggressive — maximize coverage</option></select></Field> : null}
              {savePreset.isSuccess ? <p className="formSuccess" role="status">Fingerprint preset saved.</p> : null}
            </Panel>
            <Panel title="Global marker relearn" description="Re-derive markers from stored fingerprints; no media is downloaded.">
              {relearn.isLoading && !relearn.data ? <LoadingPanel label="Loading relearn status" /> : null}
              {!relearn.data && relearn.error ? <ErrorPanel error={relearn.error} onRetry={() => void relearn.refetch()} /> : null}
              {relearn.data ? <>
                <div className="statusLine"><span>Status</span><StatusPill tone={relearn.data.state === 'failed' ? 'bad' : relearnRunning ? 'warn' : relearn.data.state === 'complete' ? 'good' : 'neutral'}>{relearn.data.state || 'idle'}</StatusPill></div>
                {relearnRunning ? <progress aria-label="Global relearn progress" value={relearn.data.seasons_done ?? 0} max={relearn.data.seasons_total || 1} /> : null}
                <ConfirmButton variant="secondary" disabled={relearnRunning || action.isPending} prompt="Re-derive automatic markers for every fingerprinted season? Manual and user-provided markers are preserved." onConfirm={() => action.mutate({ path: '/fingerprint/relearn-all' })}><RotateCcw size={16} /> Relearn all markers</ConfirmButton>
                {relearn.data.error ? <p className="formError">{relearn.data.error}</p> : null}
              </> : null}
            </Panel>
          </div>
        </> : null}

        {tab === 'series' ? <Panel title="Series and season operations" description="Inspect boundaries, queue analysis, or change detection for a season." actions={seriesPage ? <span className="muted">{seriesPage.total.toLocaleString()} series</span> : null}>
          <ReconnectNotice reconnecting={Boolean(series.isError && seriesPage)} onRetry={() => void series.refetch()} />
          {series.isPlaceholderData ? <p className="muted" role="status">Updating series…</p> : null}
          {series.isLoading && !seriesPage ? <LoadingPanel label="Loading series" /> : null}
          {!seriesPage && series.error ? <ErrorPanel error={series.error} onRetry={() => void series.refetch()} /> : null}
          {seriesPage?.series.length ? <>
            <div className="tableWrap"><table><thead><tr><th>Series</th><th>Season</th><th>Fingerprint coverage</th><th>Markers</th><th>Actions</th></tr></thead><tbody>
              {seriesPage.series.flatMap((show) => show.seasons.map((season) => <tr key={`${show.imdb_id}-${season.season}`}>
                <td data-label="Series"><strong>{show.title}</strong><small>{show.imdb_id}</small></td>
                <td data-label="Season">{season.season}</td>
                <td data-label="Fingerprint coverage">{season.episodes_fingerprinted}/{season.episodes_total} episodes</td>
                <td data-label="Markers"><StatusPill tone={season.markers_generated ? 'good' : 'neutral'}>{season.marker_types.join(', ') || 'None'}</StatusPill></td>
                <td data-label="Actions"><Button variant="secondary" size="compact" onClick={() => openSeason(show.imdb_id, season.season)}>Inspect season</Button></td>
              </tr>))}
            </tbody></table></div>
          </> : seriesPage ? <EmptyPanel title={page > 1 ? 'No series on this page' : 'No fingerprinted series'} message={page > 1 ? 'Go to an earlier page to continue inspection.' : 'Series appear after playback begins collecting fingerprints.'} /> : null}
          {seriesPage ? <div className="pagination"><Button variant="secondary" disabled={page === 1 || series.isPlaceholderData} onClick={() => setPage(page - 1)}>Previous</Button><span>Page {page} · {seriesPage.total ? Math.min(skip + 1, seriesPage.total) : 0}–{Math.min(skip + 25, seriesPage.total)} of {seriesPage.total}</span><Button variant="secondary" disabled={!seriesPage.has_more || series.isPlaceholderData} onClick={() => setPage(page + 1)}>Next</Button></div> : null}
        </Panel> : null}

        {tab === 'logs' ? <Panel title="Recent fingerprint log" description="The latest analysis and marker events.">
          <ReconnectNotice reconnecting={Boolean(logs.isError && logs.data)} onRetry={() => void logs.refetch()} />
          {logs.isLoading && !logs.data ? <LoadingPanel label="Loading fingerprint log" /> : null}
          {!logs.data && logs.error ? <ErrorPanel error={logs.error} onRetry={() => void logs.refetch()} /> : null}
          {logs.data ? <pre className="logViewer" tabIndex={0}>{logs.data.lines.map((line) => `${line.ts ?? ''} ${line.level ?? ''} ${line.msg ?? ''}`).join('\n') || 'No fingerprint events yet.'}</pre> : null}
        </Panel> : null}
      </>}
    </PageTabs>
    <SidePanel open={selectedSeriesId !== null || seasonParam !== null} title={selectedShow ? `${selectedShow.title} · Season ${selectedSeasonNumber}` : 'Season details'} description={selectedShow?.imdb_id} onClose={closeSeason}>
      {!validSeasonLink ? <EmptyPanel title="Invalid season link" message="A series ID and season number are required to inspect a season." />
        : selectedSeriesQuery.isLoading && !selectedSeriesQuery.data ? <LoadingPanel label="Loading season" />
          : selectedSeriesQuery.error && !selectedSeriesQuery.data ? <ErrorPanel error={selectedSeriesQuery.error} onRetry={() => void selectedSeriesQuery.refetch()} />
            : !selectedShow ? <EmptyPanel title="Series not found" message="This series may no longer have fingerprint records." />
              : !selectedSeason ? <EmptyPanel title="Season not found" message="This season may no longer have fingerprint records." />
                : <><ReconnectNotice reconnecting={selectedSeriesQuery.isError} onRetry={() => void selectedSeriesQuery.refetch()} /><SeasonDiagnostics imdbID={selectedShow.imdb_id} title={selectedShow.title} season={selectedSeason} busy={action.isPending} act={(path, body) => action.mutate({ path, body })} /></>}
    </SidePanel>
  </>;
}
