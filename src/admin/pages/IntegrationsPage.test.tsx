// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import IntegrationsPage, { type IntegrationsResponse, type LibraryStatus } from './IntegrationsPage';

const disconnected = (id: string, label: string): LibraryStatus => ({
  id, label, kind: 'library', connected: false, enabled: false, health: 'ok',
  server_name: null, server_url: null, last_sync_at: null, last_sync_error: null, last_sync_counts: null,
});
const plexConnected: LibraryStatus = {
  ...disconnected('plex', 'Plex'), connected: true, enabled: true, server_name: 'Basement', server_url: 'http://10.0.0.2:32400',
  last_sync_at: '2026-11-01T10:00:00Z', last_sync_counts: { titles: 1200, movies: 900, shows: 300, removed: 0 },
};
const list: IntegrationsResponse = {
  sdk_version: 1,
  integrations: [
    { id: 'plex', label: 'Plex', kind: 'library', enabled: true, health: 'ok', searchable: false, settings_url: null, summary: {} },
    { id: 'jellyfin', label: 'Jellyfin', kind: 'library', enabled: false, health: 'ok', searchable: false, settings_url: null, summary: {} },
    { id: 'example', label: 'Example Sources', kind: 'addon', enabled: true, health: 'degraded', searchable: true, settings_url: '/admin/addons/example/', summary: {} },
  ],
  rejected_addons: [{ entry_point: 'old_addon', reason: 'built for SDK 0, server has 1' }],
  source_cache: { entries: 3, max: 500 },
};

type Handler = (url: string, init?: RequestInit) => unknown;
let routes: Record<string, Handler>;
let calls: Array<{ url: string; method: string; body?: unknown }>;

beforeEach(() => {
  calls = [];
  routes = {
    'GET /admin/api/integrations': () => list,
    'GET /admin/api/integrations/plex': () => disconnected('plex', 'Plex'),
    'GET /admin/api/integrations/jellyfin': () => disconnected('jellyfin', 'Jellyfin'),
  };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = String(input);
    const method = init?.method ?? 'GET';
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const handler = routes[`${method} ${url}`];
    if (!handler) return { ok: false, status: 404, json: async () => ({ detail: `unexpected ${method} ${url}` }) } as Response;
    return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(handler(url, init))) } as Response;
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/integrations']}><IntegrationsPage /></MemoryRouter></QueryClientProvider>);
}

const panel = (title: string) => screen.getByRole('heading', { name: title }).closest('section') as HTMLElement;

it('lists Plex, Jellyfin, addons with a settings link, rejected addons, and clears the source cache', async () => {
  routes['POST /admin/api/integrations/cache/clear'] = () => ({ status: 'ok' });
  renderPage();
  expect(await screen.findByRole('heading', { name: 'Example Sources' })).toBeTruthy();
  expect(screen.getByRole('heading', { name: 'Plex' })).toBeTruthy();
  expect(screen.getByRole('heading', { name: 'Jellyfin' })).toBeTruthy();
  const addon = panel('Example Sources');
  expect(within(addon).getByRole('link', { name: /Settings/ }).getAttribute('href')).toBe('/integrations/example');
  expect(within(addon).getByText('degraded')).toBeTruthy();
  expect(screen.getByText('old_addon')).toBeTruthy();
  await userEvent.setup().click(screen.getByRole('button', { name: /Clear source cache/ }));
  expect(await screen.findByText('Source cache cleared.')).toBeTruthy();
  expect(calls.some((call) => call.method === 'POST' && call.url === '/admin/api/integrations/cache/clear')).toBe(true);
});

it('signs in with Plex: opens plex.tv, polls the PIN every 2 s, then connects the chosen server', async () => {
  const popup = { location: { href: '' }, close: vi.fn() };
  vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window);
  let polls = 0;
  routes['POST /admin/api/integrations/plex/pin'] = () => ({ pin_id: 42, auth_url: 'https://app.plex.tv/auth#?code=abc' });
  routes['GET /admin/api/integrations/plex/pin'] = () => {
    polls += 1;
    return polls < 2 ? { status: 'pending' } : {
      status: 'signed_in',
      servers: [{ id: 'srv1', name: 'Basement', owned: true, connections: [{ uri: 'http://10.0.0.2:32400', local: true }, { uri: 'https://remote.example:32400', local: false }] }],
    };
  };
  routes['POST /admin/api/integrations/plex/connect'] = () => { routes['GET /admin/api/integrations/plex'] = () => plexConnected; return plexConnected; };
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole('button', { name: /Sign in with Plex/ }));
  await waitFor(() => expect(popup.location.href).toBe('https://app.plex.tv/auth#?code=abc'));
  expect(await screen.findByText(/Waiting for you to approve Curatio/)).toBeTruthy();
  const server = await screen.findByRole('combobox', { name: 'Plex server' }, { timeout: 5000 });
  expect(polls).toBe(2);
  expect((server as HTMLSelectElement).value).toBe('srv1');
  await user.selectOptions(screen.getByRole('combobox', { name: 'Plex connection' }), 'https://remote.example:32400');
  await user.click(within(panel('Plex')).getByRole('button', { name: 'Connect' }));
  await waitFor(() => expect(calls.find((call) => call.url.endsWith('/plex/connect'))?.body).toEqual({ server_id: 'srv1', uri: 'https://remote.example:32400' }));
  expect(await screen.findByText('Basement')).toBeTruthy();
  expect(screen.getByText(/1,200 \(900 movies · 300 shows\)/)).toBeTruthy();
}, 10_000);

it('connects Plex manually with a URL and token', async () => {
  routes['POST /admin/api/integrations/plex/connect-manual'] = () => plexConnected;
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole('button', { name: 'Enter URL and token' }));
  await user.type(screen.getByLabelText('Plex server URL'), 'http://10.0.0.2:32400');
  await user.type(screen.getByLabelText('Plex token'), 'tok');
  await user.click(within(panel('Plex')).getByRole('button', { name: 'Connect' }));
  await waitFor(() => expect(calls.find((call) => call.url.endsWith('/plex/connect-manual'))?.body).toEqual({ server_url: 'http://10.0.0.2:32400', token: 'tok' }));
});

it('connects Jellyfin with an API key, and offers Sync now, Disable and Disconnect once connected', async () => {
  const jellyfin: LibraryStatus = { ...plexConnected, id: 'jellyfin', label: 'Jellyfin', server_name: 'Den' };
  routes['POST /admin/api/integrations/jellyfin/connect'] = () => { routes['GET /admin/api/integrations/jellyfin'] = () => jellyfin; return jellyfin; };
  routes['POST /admin/api/integrations/jellyfin/sync'] = () => jellyfin;
  routes['POST /admin/api/integrations/jellyfin/enabled'] = () => ({ ...jellyfin, enabled: false });
  const user = userEvent.setup();
  renderPage();
  await screen.findByLabelText('Jellyfin server URL');
  await user.type(screen.getByLabelText('Jellyfin server URL'), 'http://10.0.0.3:8096');
  await user.selectOptions(screen.getByLabelText('Jellyfin sign-in method'), 'api_key');
  await user.type(screen.getByLabelText('Jellyfin API key'), 'key123');
  await user.click(within(panel('Jellyfin')).getByRole('button', { name: 'Connect' }));
  await waitFor(() => expect(calls.find((call) => call.url.endsWith('/jellyfin/connect'))?.body).toEqual({ server_url: 'http://10.0.0.3:8096', api_key: 'key123' }));
  const card = panel('Jellyfin');
  await user.click(await within(card).findByRole('button', { name: /Sync now/ }));
  expect(await within(card).findByText('Library synced.')).toBeTruthy();
  await act(async () => { await user.click(within(card).getByRole('button', { name: 'Disable' })); });
  await waitFor(() => expect(calls.find((call) => call.url.endsWith('/jellyfin/enabled'))?.body).toEqual({ enabled: false }));
  expect(within(card).getByRole('button', { name: 'Disconnect' })).toBeTruthy();
});
