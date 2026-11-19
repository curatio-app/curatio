// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { axe } from 'vitest-axe';
import AdminApp from './AdminApp';

const overview = {
  generated_at: '2026-08-04T12:00:00',
  ready: false,
  setup: {
    credentials: true,
    media_server: false,
    initial_build: false,
    first_user: false,
  },
  warnings: [],
  counts: { total_metadata: 10, catalog_items: 20, active_categories: 40, total_users: 0 },
  jobs: { running: false, recent: [] },
  streams: [],
  health: { caches: { catalog: { size: 1, max: 100 } }, user_sync: [] },
};

function jsonResponse(data: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(data),
  } as Response);
}

function renderAdmin(path = '/overview') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <AdminApp />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('admin application', () => {
  it('loads overview and the addon list at startup and fetches users on route entry', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith('/overview')) return jsonResponse(overview);
      if (url.endsWith('/users')) return jsonResponse([]);
      if (url.endsWith('/integrations')) return jsonResponse({ integrations: [] });
      return jsonResponse({ detail: 'unexpected request' }, 404);
    });
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
    const user = userEvent.setup();
    const view = renderAdmin();

    await screen.findByText('Connect Plex or Jellyfin');
    expect(fetchMock.mock.calls.map(([input]) => String(input)).sort()).toEqual([
      '/admin/api/integrations',
      '/admin/api/overview',
    ]);
    expect(screen.getByRole('link', { name: /Connect Plex or Jellyfin/ }).getAttribute('href')).toBe('/integrations');
    expect(screen.getByRole('link', { name: 'Integrations' })).toBeTruthy();

    await user.click(screen.getByRole('link', { name: 'Users' }));
    await screen.findByRole('heading', { name: 'Users' });
    await waitFor(() => expect(fetchMock.mock.calls.map(([input]) => String(input)).sort()).toEqual([
      '/admin/api/integrations',
      '/admin/api/overview',
      '/admin/api/users',
    ]));

    expect((await axe(view.container)).violations).toEqual([]);
  });

  it('supports keyboard-first navigation', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => jsonResponse(overview));
    const user = userEvent.setup();
    renderAdmin();
    await screen.findByRole('heading', { name: 'Overview' });

    await user.tab();
    expect(document.activeElement?.textContent).toContain('Skip to content');
    expect((document.activeElement as HTMLAnchorElement).getAttribute('href')).toBe('#admin-content');
  });

  it('redirects the retired legacy route into the React overview', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      if (String(input).endsWith('/overview')) return jsonResponse(overview);
      return jsonResponse({ detail: 'unexpected request' }, 404);
    });
    renderAdmin('/legacy');

    await screen.findByRole('heading', { name: 'Overview' });
    expect(screen.queryByText(/Legacy specialist/i)).toBeNull();
  });

  it('guides the final tagging pilot from the new admin center', async () => {
    const preflight = {
      library_total: 354439,
      eligible_titles: 105985,
      excluded_by_quality: 248454,
      keys: { tmdb: true, gemini: true, mdblist: true },
      ready: true,
      missing_required: [],
      remaining: { enrich: 0, mdblist: 0, retag: 105985, relevance: 1, catalogs: 1, users: 1 },
      est_cost_usd: 20.14,
      cost_ceiling_usd: 25,
      model: 'gemini-2.5-flash-lite',
      tagger_version: '3-37914a720e3a',
      running: false,
      steps: ['enrich', 'mdblist', 'retag', 'relevance', 'catalogs', 'users'].map((key) => ({ key, title: key, available: true, remaining: key === 'retag' ? 105985 : 1 })),
    };
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === 'POST' && url.endsWith('/tagging/upgrade/start')) return jsonResponse({ ok: true, started: true });
      if (url.includes('/tags/list')) return jsonResponse({ total: 0, offset: 0, limit: 50, has_more: false, items: [] });
      if (url.endsWith('/tags/overview')) return jsonResponse({ current_titles: 0 });
      if (url.endsWith('/tagging/upgrade/preflight')) return jsonResponse(preflight);
      if (url.endsWith('/tagging/upgrade/status')) return jsonResponse({ running: false, steps: [], done: 0, cost_usd: 0 });
      return jsonResponse({ detail: 'unexpected request' }, 404);
    });
    const user = userEvent.setup();
    renderAdmin('/tagging');

    await screen.findByRole('heading', { name: 'Final tagging upgrade' });
    expect((await screen.findAllByText('105,985')).length).toBe(2);
    expect(screen.getByText('248,454')).toBeTruthy();
    expect(screen.getByText('3-37914a720e3a')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: /Run 1,000-title pilot/ }));
    await user.click(screen.getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => {
      if (String(input) !== '/admin/api/tagging/upgrade/start' || init?.method !== 'POST') return false;
      const body = JSON.parse(String(init.body));
      return body.limit === 1000
        && JSON.stringify(body.skip) === JSON.stringify(['relevance', 'catalogs', 'users']);
    })).toBe(true));
  });

  it('lets an operator select OpenAI and save its API key', async () => {
    const providers = [
      { id: 'gemini', label: 'Google Gemini', key_set: true, key_hint: 'AIza…abc', model: 'gemini-2.5-flash-lite', models: [{ id: 'gemini-2.5-flash-lite', label: 'Gemini 2.5 Flash-Lite' }] },
      { id: 'anthropic', label: 'Anthropic (Claude API)', key_set: false, key_hint: '', model: 'claude-haiku-4-5-20251001', models: [{ id: 'claude-haiku-4-5-20251001', label: 'Claude Haiku 4.5' }] },
      { id: 'openai', label: 'OpenAI (ChatGPT API)', key_set: false, key_hint: '', model: 'gpt-5-nano', models: [{ id: 'gpt-5-nano', label: 'GPT-5 nano — pilot only', production_ready: false, warning: 'Semantic QA failed for job 221.' }] },
    ];
    const preflight = {
      library_total: 100,
      eligible_titles: 80,
      excluded_by_quality: 20,
      keys: { tmdb: true, gemini: true, anthropic: false, openai: false, tagging: true, mdblist: true },
      ready: true,
      missing_required: [],
      remaining: { enrich: 0, mdblist: 0, retag: 80, relevance: 1, catalogs: 1, users: 1 },
      est_cost_usd: 1,
      cost_ceiling_usd: 25,
      provider: 'gemini',
      provider_label: 'Google Gemini',
      providers,
      model: 'gemini-2.5-flash-lite',
      tagger_version: '3-current',
      running: false,
      steps: [],
    };
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === 'POST' && url.endsWith('/tagging/upgrade/keys')) return jsonResponse({ ok: true });
      if (url.includes('/tags/list')) return jsonResponse({ total: 0, offset: 0, limit: 50, has_more: false, items: [] });
      if (url.endsWith('/tags/overview')) return jsonResponse({});
      if (url.endsWith('/tagging/upgrade/preflight')) return jsonResponse(preflight);
      if (url.endsWith('/tagging/upgrade/status')) return jsonResponse({ running: false, steps: [], done: 0, cost_usd: 0 });
      return jsonResponse({ detail: 'unexpected request' }, 404);
    });
    const user = userEvent.setup();
    renderAdmin('/tagging');

    await screen.findByRole('heading', { name: 'Final tagging upgrade' });
    await user.selectOptions(await screen.findByLabelText('Tagging provider'), 'openai');
    expect(await screen.findByText(/Semantic QA failed for job 221/)).toBeTruthy();
    await user.type(await screen.findByLabelText(/OpenAI \(ChatGPT API\) API key/), 'sk-test-key');
    await user.click(screen.getByRole('button', { name: 'Save provider settings' }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => {
      if (String(input) !== '/admin/api/tagging/upgrade/keys' || init?.method !== 'POST') return false;
      const body = JSON.parse(String(init.body));
      return body.tagging_provider === 'openai'
        && body.model === 'gpt-5-nano'
        && body.openai_api_key === 'sk-test-key'
        && body.gemini_api_key === undefined;
    })).toBe(true));
  });

  it('allows a pilot but blocks full completion on a pilot-only model', async () => {
    const providers = [{
      id: 'openai',
      label: 'OpenAI (ChatGPT API)',
      key_set: true,
      key_hint: 'sk…key',
      model: 'gpt-5-nano',
      models: [{
        id: 'gpt-5-nano',
        label: 'GPT-5 nano — pilot only',
        production_ready: false,
        warning: 'Use it only for a comparison pilot.',
      }],
    }];
    const preflight = {
      library_total: 100,
      eligible_titles: 80,
      excluded_by_quality: 20,
      keys: { tmdb: true, gemini: false, anthropic: false, openai: true, tagging: true, mdblist: true },
      ready: true,
      missing_required: [],
      remaining: { enrich: 0, mdblist: 0, retag: 80, relevance: 1, catalogs: 1, users: 1 },
      est_cost_usd: 1,
      cost_ceiling_usd: 25,
      provider: 'openai',
      provider_label: 'OpenAI (ChatGPT API)',
      providers,
      model: 'gpt-5-nano',
      model_production_ready: false,
      tagger_version: 'qa-pilot',
      running: false,
      steps: [],
    };
    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const url = String(input);
      if (url.includes('/tags/list')) return jsonResponse({ total: 0, offset: 0, limit: 50, has_more: false, items: [] });
      if (url.endsWith('/tags/overview')) return jsonResponse({});
      if (url.endsWith('/tagging/upgrade/preflight')) return jsonResponse(preflight);
      if (url.endsWith('/tagging/upgrade/status')) return jsonResponse({ running: false, steps: [], done: 0, cost_usd: 0 });
      return jsonResponse({ detail: 'unexpected request' }, 404);
    });
    renderAdmin('/tagging');

    expect(await screen.findByText(/Use it only for a comparison pilot/)).toBeTruthy();
    expect((screen.getByRole('button', { name: /Run 1,000-title pilot/ }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole('button', { name: /Complete remaining titles/ }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('promotes an observed alias from the new tag-quality page', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === 'POST' && url.endsWith('/tags/alias')) return jsonResponse({ status: 'ok', tag: 'Neo-Noir' });
      if (url.includes('/tags/list')) return jsonResponse({ items: [{ id: 9, name: 'Neo-Noir', facet: 'subgenre', total_docs: 450 }] });
      if (url.includes('/tags/offvocab')) return jsonResponse({ total_observations: 1, total_occurrences: 4, offset: 0, limit: 50, has_more: false, items: [{ normalized: 'neo noir film', raw_name: 'Neo Noir Film', media_type: 'movie', occurrences: 4, resolution: 'unmapped', samples: [{ tmdb_id: 1, title: 'Sample Movie' }] }] });
      if (url.endsWith('/tagging/repair/status')) return jsonResponse({ stale_mdb_facts: 0, stale_tags: 0, mdb_version: 'current' });
      if (url.endsWith('/tagging/rebuild/status')) return jsonResponse({ job: null, paused: false });
      if (url.endsWith('/tags/dead')) return jsonResponse({ zero_assignments: [], zero_category_refs: [], orphan_rows: 0 });
      return jsonResponse({ detail: 'unexpected request' }, 404);
    });
    const user = userEvent.setup();
    renderAdmin('/tag-quality');

    await screen.findByRole('heading', { name: 'Tag quality' });
    await user.click(await screen.findByRole('button', { name: 'Map alias' }));
    await user.type(screen.getByLabelText('Canonical tag for Neo Noir Film'), 'neo');
    await user.click(await screen.findByRole('button', { name: /Neo-Noir/ }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => {
      if (String(input) !== '/admin/api/tags/alias' || init?.method !== 'POST') return false;
      const body = JSON.parse(String(init.body));
      return body.raw === 'Neo Noir Film' && body.tag_id === 9;
    })).toBe(true));
  });

  it('rebuilds synthesized and personal rows as one local job', async () => {
    const preflight = {
      library_total: 100,
      eligible_titles: 80,
      excluded_by_quality: 20,
      keys: { tmdb: true, gemini: true, mdblist: true },
      ready: true,
      missing_required: [],
      remaining: { enrich: 0, mdblist: 0, retag: 0, relevance: 0, catalogs: 0, users: 0 },
      est_cost_usd: 0,
      cost_ceiling_usd: 25,
      model: 'gemini-2.5-flash-lite',
      tagger_version: '3-current',
      running: false,
      steps: [],
    };
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === 'POST' && url.endsWith('/tagging/upgrade/start')) {
        return jsonResponse({ ok: true, started: true });
      }
      if (url.endsWith('/catalog/rows')) return jsonResponse({ count: 0, rows: [], personal_rows: 78, personal_profiles: 8 });
      if (url.endsWith('/tagging/upgrade/preflight')) return jsonResponse(preflight);
      if (url.endsWith('/tagging/upgrade/status')) return jsonResponse({ running: false, steps: [], done: 0, cost_usd: 0 });
      return jsonResponse({ detail: 'unexpected request' }, 404);
    });
    const user = userEvent.setup();
    renderAdmin('/catalogs');

    await screen.findByRole('heading', { name: 'Catalogs' });
    expect(await screen.findByText('78')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: /Rebuild discovery rows/ }));
    await user.click(screen.getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => {
      if (String(input) !== '/admin/api/tagging/upgrade/start' || init?.method !== 'POST') return false;
      const body = JSON.parse(String(init.body));
      return JSON.stringify(body.skip) === JSON.stringify(['enrich', 'mdblist', 'retag', 'relevance']);
    })).toBe(true));
  });

  it('submits invite forms and confirms destructive actions', async () => {
    const invitation = {
      code: 'abcdefghijk',
      invite_url: 'https://test/invite/abcdefghijk',
      label: 'Family',
      is_used: false,
    };
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const method = init?.method ?? 'GET';
      if (method === 'POST') return jsonResponse(invitation);
      if (method === 'DELETE') return jsonResponse({ status: 'ok' });
      return jsonResponse([invitation]);
    });
    const user = userEvent.setup();
    renderAdmin('/invites');

    await screen.findByRole('heading', { name: 'Invites' });
    await user.click(await screen.findByRole('button', { name: 'Create invite' }));
    await user.type(screen.getByPlaceholderText('Family, beta tester…'), 'Household');
    await user.click(screen.getByRole('button', { name: 'Create invite' }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(true));

    await user.click(await screen.findByRole('button', { name: 'Delete invitation Family' }));
    expect(screen.getByText('Delete this invitation?')).toBeTruthy();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(false);
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    await user.click(screen.getByRole('button', { name: 'Delete invitation Family' }));
    await user.click(screen.getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(true));
  });
});

describe('installed addons', () => {
  it('lists addons with a settings page in an Addons sidebar group', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith('/integrations')) return jsonResponse({ integrations: [
        { id: 'plex', label: 'Plex', kind: 'library', settings_url: null },
        { id: 'example', label: 'Example Sources', kind: 'addon', settings_url: '/admin/addons/example/' },
        { id: 'headless', label: 'Headless', kind: 'addon', settings_url: null },
      ] });
      return jsonResponse(overview);
    });
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
    renderAdmin();
    const link = await screen.findByRole('link', { name: 'Example Sources' });
    expect(link.getAttribute('href')).toBe('/integrations/example');
    expect(screen.queryByRole('link', { name: 'Headless' })).toBeNull();
    expect(screen.getByText('Addons')).toBeTruthy();
  });
});
