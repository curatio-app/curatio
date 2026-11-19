// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CatalogsPage, TaggingPage } from './DiscoveryPages';
import TagQualityPage from './TagQualityPage';

function jsonResponse(data: unknown, status = 200) {
  return Promise.resolve({ ok: status >= 200 && status < 300, status, json: () => Promise.resolve(data) } as Response);
}

function NavigationProbe() {
  const location = useLocation();
  const navigate = useNavigate();
  return <><output data-testid="location">{location.pathname}{location.search}</output><button onClick={() => navigate(1)}>Forward</button></>;
}

function renderPage(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><NavigationProbe /><Routes>
    <Route path="/catalogs" element={<CatalogsPage />} />
    <Route path="/tagging" element={<TaggingPage />} />
    <Route path="/tag-quality" element={<TagQualityPage />} />
  </Routes></MemoryRouter></QueryClientProvider>);
}

const preflight = {
  library_total: 10, eligible_titles: 10, excluded_by_quality: 0,
  keys: { tmdb: true, gemini: true, mdblist: true }, ready: true,
  missing_required: [], remaining: { retag: 0 }, est_cost_usd: 0,
  cost_ceiling_usd: 25, model: 'gemini', tagger_version: 'current', steps: [], running: false,
};

function catalogFetch(input: RequestInfo | URL) {
  const url = String(input);
  if (url.endsWith('/catalog/rows')) return jsonResponse({
    count: 1, personal_rows: 0, personal_profiles: 0, rows: [{
      id: 'row-1', name: 'Quiet Mysteries', media_type: 'movie', source: 'generated',
      items: 30, servable: true, mandatory: ['mystery'], required: [], optional: [],
    }], personal: [],
  });
  if (url.endsWith('/catalog/rows/row-1/preview')) return jsonResponse({
    name: 'Quiet Mysteries', formula: { mandatory: ['mystery'] },
    items: [{ tmdb_id: 11, title: 'Sample Film', year: '2025', votes: 12, score: 0.92, matched: ['mystery'] }],
  });
  if (url.includes('/catalog/rows/') && url.endsWith('/preview')) return jsonResponse({ detail: 'Unknown row' }, 404);
  if (url.endsWith('/tagging/upgrade/preflight')) return jsonResponse(preflight);
  if (url.endsWith('/tagging/upgrade/status')) return jsonResponse({ running: false, steps: [], done: 0, cost_usd: 0 });
  if (url.endsWith('/catalog/quality/preview')) return jsonResponse({ applied: {}, counts: { movie: { total: 1, kept: 1, pct: 100 }, tv: { total: 0, kept: 0, pct: 0 } } });
  if (url.endsWith('/categories/synth/config')) return jsonResponse({ excluded_tags: [], excluded_words: [], rotation_days: 7, editor_enabled: false, editor_provider: 'gemini', editor_model: 'gemini', editor_cost_ceiling_usd: 0.25, providers: [], known_tags: [] });
  return jsonResponse({ detail: 'Unexpected request' }, 404);
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('discovery navigation', () => {
  it('preserves catalog search while a preview panel opens and browser history closes it', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(catalogFetch);
    const user = userEvent.setup();
    renderPage('/catalogs?q=Quiet');

    await user.click(await screen.findByRole('button', { name: 'Preview Quiet Mysteries' }));
    expect(await screen.findByText('Sample Film')).toBeTruthy();
    expect(screen.getByTestId('location').textContent).toContain('q=Quiet&preview=synth%3Arow-1');

    await user.click(screen.getByRole('button', { name: 'Close panel' }));
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/catalogs?q=Quiet'));
    expect((screen.getByRole('searchbox', { name: 'Search catalog rows' }) as HTMLInputElement).value).toBe('Quiet');
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Preview Quiet Mysteries' })));

    await user.click(screen.getByRole('button', { name: 'Forward' }));
    expect(await screen.findByText('Sample Film')).toBeTruthy();
  });

  it('shows a missing catalog row on a direct preview link', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(catalogFetch);
    renderPage('/catalogs?preview=synth%3Aremoved');
    expect(await screen.findByText('Catalog row not found')).toBeTruthy();
  });

  it('keeps catalog quality and editorial sections addressable in the URL', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(catalogFetch);
    const user = userEvent.setup();
    renderPage('/catalogs?tab=quality');
    expect(await screen.findByRole('heading', { name: 'Recommendation quality floor' })).toBeTruthy();
    await user.click(screen.getByRole('tab', { name: 'Editorial' }));
    expect(screen.getByTestId('location').textContent).toBe('/catalogs?tab=editorial');
    expect(await screen.findByRole('heading', { name: 'Synthesized-row editorial controls' })).toBeTruthy();
  });

  it('uses URL tabs and server paging for the tagging vocabulary', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const url = String(input);
      if (url.includes('/tags/list?')) return jsonResponse({ total: 101, offset: 50, limit: 50, has_more: true, items: [] });
      if (url.endsWith('/tags/overview')) return jsonResponse({ active: 101 });
      if (url.endsWith('/tagging/upgrade/preflight')) return jsonResponse(preflight);
      if (url.endsWith('/tagging/upgrade/status')) return jsonResponse({ running: false, steps: [], done: 0, cost_usd: 0 });
      return jsonResponse({ detail: 'Unexpected request' }, 404);
    });
    renderPage('/tagging?tab=vocabulary&q=genre&page=2');
    expect(await screen.findByRole('searchbox', { name: 'Search tags' })).toHaveProperty('value', 'genre');
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/tags/list?limit=50&offset=50&q=genre'))).toBe(true));
    expect(screen.getByTestId('location').textContent).toContain('tab=vocabulary&q=genre&page=2');
  });

  it('loads an observation by exact ID even when its list page differs', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const url = String(input);
      if (url.includes('/tags/offvocab?resolution=all')) return jsonResponse({ total_observations: 1, total_occurrences: 8, offset: 0, limit: 1, has_more: false, items: [{ normalized: 'deep tag', raw_name: 'Deep Tag', media_type: 'tv', occurrences: 8, resolution: 'unmapped', samples: [] }] });
      if (url.includes('/tags/offvocab?limit=50')) return jsonResponse({ total_observations: 200, total_occurrences: 500, offset: 100, limit: 50, has_more: true, items: [] });
      if (url.endsWith('/tagging/repair/status')) return jsonResponse({ stale_mdb_facts: 0, stale_tags: 0, mdb_version: 'current' });
      if (url.endsWith('/tags/dead')) return jsonResponse({ zero_assignments: [], zero_category_refs: [], orphan_rows: 0 });
      if (url.endsWith('/tags/alias/sample-ids')) return jsonResponse({ tmdb_ids: 0, movie: [], tv: [], est_cost_usd: 0 });
      if (url.endsWith('/tagging/rebuild/status')) return jsonResponse({ job: null, paused: false });
      return jsonResponse({ detail: 'Unexpected request' }, 404);
    });
    const user = userEvent.setup();
    renderPage('/tag-quality?tab=observations&page=3&observation=deep+tag&type=tv');
    expect(await screen.findByRole('dialog', { name: 'Deep Tag' })).toBeTruthy();
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).includes('normalized=deep%20tag&media_type=tv'))).toBe(true));
    await user.click(screen.getByRole('button', { name: 'Close panel' }));
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/tag-quality?tab=observations&page=3'));
  });

  it('reports a missing observation instead of offering an alias editor', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const url = String(input);
      if (url.includes('/tags/offvocab?resolution=all')) return jsonResponse({ total_observations: 0, total_occurrences: 0, offset: 0, limit: 1, has_more: false, items: [] });
      if (url.includes('/tags/offvocab?limit=50')) return jsonResponse({ total_observations: 0, total_occurrences: 0, offset: 0, limit: 50, has_more: false, items: [] });
      if (url.endsWith('/tagging/repair/status')) return jsonResponse({ stale_mdb_facts: 0, stale_tags: 0, mdb_version: 'current' });
      if (url.endsWith('/tags/dead')) return jsonResponse({ zero_assignments: [], zero_category_refs: [], orphan_rows: 0 });
      if (url.endsWith('/tags/alias/sample-ids')) return jsonResponse({ tmdb_ids: 0, movie: [], tv: [], est_cost_usd: 0 });
      if (url.endsWith('/tagging/rebuild/status')) return jsonResponse({ job: null, paused: false });
      return jsonResponse({ detail: 'Unexpected request' }, 404);
    });
    renderPage('/tag-quality?observation=missing&type=movie');
    expect(await screen.findByText('Observation not found')).toBeTruthy();
    expect(screen.queryByRole('textbox', { name: /Canonical tag/ })).toBeNull();
  });
});
