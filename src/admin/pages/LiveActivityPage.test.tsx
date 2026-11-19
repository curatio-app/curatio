// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { adminApi } from '../api';
import { LiveActivityPage } from './LiveActivityPage';
vi.mock('../api', async (original) => ({ ...await original<typeof import('../api')>(), adminApi: vi.fn() }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });
function HistoryControls() {
  const location = useLocation();
  const navigate = useNavigate();
  return <><output data-testid="location">{location.search}</output><button onClick={() => navigate(1)}>Forward</button></>;
}
function renderActivity(upstream: Array<Record<string, unknown>>, path = '/streams') {
  vi.mocked(adminApi).mockResolvedValue({
    updated_at: '2026-09-24T12:00:00Z', counts: { playing: 1, paused: 0, buffering: 0, starting: 0 },
    playback: [{ session_id: 'one', media_id: 'tt123', title: 'A playing title', viewer: 'Viewer', device_id: 'device', platform: 'ios', tier: 'low', state: 'playing', source_attempt: 2, ago_seconds: 1, selected_provider: 'Jellyfin' }],
    requests: [{ request_id: 'test', media_id: 'tt456', title: 'A requested title', viewer: 'Admin test', kind: 'test', tier: 'high', state: 'searching', elapsed_ms: 1000 }], upstream,
  });
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={[path]}><LiveActivityPage /><HistoryControls /></MemoryRouter></QueryClientProvider>);
}
const addon = (view: Record<string, unknown>) => ({ provider_id: 'addon-x', label: 'Addon X', ...view });
it('shows actual playback separately from requests when a provider view is unavailable', async () => {
  renderActivity([addon({ state: 'unavailable', streams: [], error: 'Administrator login unavailable' })]);
  expect(await screen.findByText('A playing title')).toBeTruthy();
  expect(screen.getByText('A requested title')).toBeTruthy();
  expect(screen.getByText('Connection test')).toBeTruthy();
  expect(screen.getByText('Serving provider: Unknown')).toBeTruthy();
  expect(screen.getByText(/Administrator login unavailable/)).toBeTruthy();
  expect(screen.getByRole('heading', { name: 'Addon X transfers' })).toBeTruthy();
  expect(screen.queryByText('No reported transfers')).toBeNull();
});
it('shows zero provider transfers only after a successful snapshot', async () => {
  renderActivity([addon({ state: 'available', streams: [], updated_at: 1790241600 })]);
  expect(await screen.findByText('No reported transfers')).toBeTruthy();
  expect(screen.getByText('A playing title')).toBeTruthy();
});
it('renders one panel per provider and none when no provider reports transfers', async () => {
  renderActivity([
    addon({ state: 'available', streams: [{ id: 't1', filename: 'Movie.mkv', transport: 'http', bytes_served: 2048 }] }),
    { provider_id: 'addon-y', label: 'Addon Y', state: 'available', streams: [] },
  ]);
  expect(await screen.findByRole('heading', { name: 'Addon X transfers' })).toBeTruthy();
  expect(screen.getByRole('heading', { name: 'Addon Y transfers' })).toBeTruthy();
  expect(screen.getByText('Movie.mkv')).toBeTruthy();
  cleanup();
  renderActivity([]);
  expect(await screen.findByText('A playing title')).toBeTruthy();
  expect(screen.queryByText(/transfers$/)).toBeNull();
});

it('loads a session link directly and reports a missing session clearly', async () => {
  renderActivity([], '/streams?session=missing');
  const panel = await screen.findByRole('dialog', { name: 'Playback session' });
  expect(await within(panel).findByText('Session no longer active')).toBeTruthy();
  await userEvent.setup().click(within(panel).getByRole('button', { name: 'Close panel' }));
  expect(screen.getByTestId('location').textContent).toBe('');
});

it('opens session inspection in history and restores the live list on Back and Forward', async () => {
  renderActivity([]);
  const user = userEvent.setup();
  await screen.findByRole('button', { name: 'Inspect session for A playing title' });
  await user.click(screen.getByRole('button', { name: 'Inspect session for A playing title' }));
  expect(screen.getByTestId('location').textContent).toBe('?session=one');
  const panel = screen.getByRole('dialog', { name: 'A playing title' });
  expect(within(panel).getByText('Jellyfin')).toBeTruthy();
  await user.click(within(panel).getByRole('button', { name: 'Close panel' }));
  expect(screen.getByTestId('location').textContent).toBe('');
  await user.click(screen.getByRole('button', { name: 'Forward' }));
  expect(screen.getByRole('dialog', { name: 'A playing title' })).toBeTruthy();
});
