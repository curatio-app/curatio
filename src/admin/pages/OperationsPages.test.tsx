// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { FingerprintsPage } from './OperationsPages';

const season = (number: number) => ({
  season: number, episodes_total: 2, episodes_fingerprinted: 1, episodes_failed: 1,
  episodes_pending: 0, episodes_processing: 0, markers_generated: true,
  marker_types: ['intro'], intro_detection_enabled: true, credits_detection_enabled: true,
  marker_records: [{ episode: 1, type: 'intro', start: 10, end: 40, confidence: 0.9, boundary_status: 'confirmed' }],
  fingerprint_records: [{ episode: 2, status: 'failed', error_message: 'Source unavailable' }],
});
const first = { imdb_id: 'tt0001', title: 'First Series', seasons: [season(1)] };
const last = { imdb_id: 'tt0026', title: 'Last Series', seasons: [season(0)] };

function jsonResponse(data: unknown, status = 200) {
  return Promise.resolve({ ok: status >= 200 && status < 300, json: () => Promise.resolve(data) } as Response);
}

function mockFingerprintApi() {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
    const url = String(input);
    if (init?.method === 'POST') return jsonResponse({ status: 'started' });
    if (url.includes('/fingerprint-series?')) {
      const params = new URL(url, 'https://curatio.test').searchParams;
      const imdbID = params.get('imdb_id');
      if (imdbID) {
        const match = [first, last].find((show) => show.imdb_id === imdbID);
        return jsonResponse({ series: match ? [match] : [], total: match ? 1 : 0, skip: 0, limit: 1, has_more: false });
      }
      return jsonResponse({ series: params.get('skip') === '25' ? [last] : [first], total: 26, skip: Number(params.get('skip')), limit: 25, has_more: params.get('skip') !== '25' });
    }
    if (url.endsWith('/fingerprint-status')) return jsonResponse({ queue: { pending: 1, processing: 2, done: 3, failed: 4 }, inflight: 2, total_markers: 10, recent_completions_24h: 3, stale_inflight_count: 0, failure_rate_pct: 1, seasons_no_stream: 0, seasons_fingerprinted_no_markers: 0, community_lookups: { introdb: { hit: 12, none: 3 }, theintrodb: { hit: 7, error: 1 } } });
    if (url.includes('/fingerprint-logs?')) return jsonResponse({ lines: [{ ts: '2026-10-04', level: 'info', msg: 'Analysis complete' }] });
    if (url.endsWith('/fingerprint/relearn-all/status')) return jsonResponse({ state: 'idle' });
    if (url.endsWith('/settings')) return jsonResponse({ fingerprint: { preset: 'BALANCED' } });
    return jsonResponse({ detail: 'Unexpected request' }, 404);
  });
}

function HistoryProbe() {
  const location = useLocation();
  const navigate = useNavigate();
  return <><output data-testid="location">{location.pathname}{location.search}</output><button onClick={() => navigate(1)}>Forward</button></>;
}

function renderFingerprint(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><HistoryProbe /><Routes><Route path="/fingerprints" element={<FingerprintsPage />} /></Routes></MemoryRouter></QueryClientProvider>);
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('fingerprinting workspace', () => {
  it('opens a season panel and restores list context through Back and Forward', async () => {
    const fetchMock = mockFingerprintApi();
    const user = userEvent.setup();
    renderFingerprint('/fingerprints?tab=series');

    await user.click(await screen.findByRole('button', { name: 'Inspect season' }));
    expect(await screen.findByRole('dialog', { name: 'First Series · Season 1' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Queue analysis' })).toBeTruthy();
    expect(screen.getByTestId('location').textContent).toContain('series=tt0001&season=1');
    await user.click(screen.getByRole('button', { name: 'Queue analysis' }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => String(input).endsWith('/fingerprint-series/tt0001/1/prelearn') && init?.method === 'POST')).toBe(true));

    await user.click(screen.getByRole('button', { name: 'Close panel' }));
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/fingerprints?tab=series'));
    await user.click(screen.getByRole('button', { name: 'Forward' }));
    expect(await screen.findByRole('dialog', { name: 'First Series · Season 1' })).toBeTruthy();
  });

  it('uses skip and limit paging, including a season zero detail link', async () => {
    const fetchMock = mockFingerprintApi();
    const user = userEvent.setup();
    renderFingerprint('/fingerprints?tab=series&page=2');

    expect(await screen.findByText('Last Series')).toBeTruthy();
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/fingerprint-series?skip=25&limit=25'))).toBe(true);
    await user.click(screen.getByRole('button', { name: 'Inspect season' }));
    expect(await screen.findByRole('dialog', { name: 'Last Series · Season 0' })).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Close panel' }));
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/fingerprints?tab=series&page=2'));
  });

  it('loads a direct season link and reports missing records clearly', async () => {
    const fetchMock = mockFingerprintApi();
    renderFingerprint('/fingerprints?tab=series&page=2&series=ttmissing&season=1');

    expect(await screen.findByText('Series not found')).toBeTruthy();
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('imdb_id=ttmissing'))).toBe(true);
    expect(screen.getByTestId('location').textContent).toContain('page=2');
  });

  it('shows how many episodes each intro database answered', async () => {
    mockFingerprintApi();
    renderFingerprint('/fingerprints');
    expect(await screen.findByText('Community lookups')).toBeTruthy();
    expect(screen.getByText('12 found · 3 not known · 0 errors')).toBeTruthy();
    expect(screen.getByText('7 found · 0 not known · 1 error')).toBeTruthy();
  });

  it('keeps the Logs tab addressable while the overview is loading', async () => {
    mockFingerprintApi();
    renderFingerprint('/fingerprints?tab=logs');
    expect(await screen.findByText(/Analysis complete/)).toBeTruthy();
    expect(screen.getByRole('tab', { name: 'Logs' }).getAttribute('aria-selected')).toBe('true');
  });
});
