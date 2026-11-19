// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { UsersPage } from './AccessPages';

const users = Array.from({ length: 55 }, (_, index) => ({
  id: index + 1,
  display_name: `Viewer ${index + 1}`,
  email: `viewer${index + 1}@example.test`,
  is_active: true,
  is_admin: false,
  auth_source: 'local',
  passkey_count: 1,
  totp_enabled: true,
  bandwidth_tier: 'high',
  catalog_count: 2,
}));

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname}{location.search}</output>;
}

function renderUsers(path: string) {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: true, status: 200, json: async () => users } as Response);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><UsersPage /><LocationProbe /></MemoryRouter></QueryClientProvider>);
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('opens a direct user link and closes to its filtered list', async () => {
  const user = userEvent.setup();
  renderUsers('/users?q=viewer&page=2&user=51');
  expect(await screen.findByRole('dialog', { name: 'Viewer 51' })).toBeTruthy();
  await user.click(screen.getByRole('button', { name: 'Close panel' }));
  expect(screen.getByTestId('location').textContent).toBe('/users?q=viewer&page=2');
  expect((screen.getByRole('searchbox', { name: 'Search viewers' }) as HTMLInputElement).value).toBe('viewer');
  expect(screen.getByText('Page 2 of 2 · 55 viewers')).toBeTruthy();
});

it('uses browser history for a user panel opened from page two', async () => {
  const user = userEvent.setup();
  renderUsers('/users?q=viewer&page=2');
  await screen.findByText('Page 2 of 2 · 55 viewers');
  await user.click(screen.getByRole('button', { name: 'Inspect Viewer 51' }));
  expect(screen.getByTestId('location').textContent).toContain('user=51');
  await user.click(screen.getByRole('button', { name: 'Close panel' }));
  expect(screen.getByTestId('location').textContent).toBe('/users?q=viewer&page=2');
});
