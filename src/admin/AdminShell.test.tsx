// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import AdminApp from './AdminApp';
import { Button, PageTabs, ReconnectNotice } from './components';

function jsonResponse(data: unknown) {
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(data) } as Response);
}

function renderAdmin(path = '/invites') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><AdminApp /></MemoryRouter></QueryClientProvider>);
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('admin shell', () => {
  it('is dark-only and offers no appearance selector', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => jsonResponse([]));
    renderAdmin();
    await screen.findByRole('heading', { name: 'Invites' });
    expect(screen.queryByRole('combobox', { name: 'Appearance' })).toBeNull();
  });

  it('searches destinations with Ctrl+K and navigates with the keyboard', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => jsonResponse([]));
    const user = userEvent.setup();
    renderAdmin();
    await screen.findByRole('heading', { name: 'Invites' });

    await user.keyboard('{Control>}k{/Control}');
    const palette = screen.getByRole('dialog', { name: 'Search admin pages' });
    const input = within(palette).getByRole('combobox', { name: 'Search admin pages' });
    expect(document.activeElement).toBe(input);
    await user.type(input, 'users');
    expect(within(palette).getAllByRole('option')).toHaveLength(1);
    await user.keyboard('{Enter}');
    await screen.findByText('No viewers yet');
    expect(screen.queryByRole('dialog', { name: 'Search admin pages' })).toBeNull();
    expect(screen.getByRole('link', { name: 'Users' }).getAttribute('aria-current')).toBe('page');
  });

  it('traps focus in the mobile navigation drawer and restores its trigger on Escape', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => jsonResponse([]));
    const user = userEvent.setup();
    renderAdmin();
    const trigger = screen.getByRole('button', { name: 'Open navigation' });
    await user.click(trigger);
    const drawer = screen.getByRole('dialog', { name: 'Admin navigation' });
    expect(drawer.contains(document.activeElement)).toBe(true);
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Admin navigation' })).toBeNull());
    expect(document.activeElement).toBe(trigger);
  });

  it('restores a filtered list and its scroll position after visiting another page', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => jsonResponse([]));
    let position = 0;
    vi.spyOn(window, 'scrollY', 'get').mockImplementation(() => position);
    const scrollTo = vi.spyOn(window, 'scrollTo').mockImplementation((_x, y) => { position = Number(y); });
    function HistoryBackButton() { const navigate = useNavigate(); return <button onClick={() => navigate(-1)}>History Back</button>; }
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/users']}><AdminApp /><HistoryBackButton /></MemoryRouter></QueryClientProvider>);

    await screen.findByText('No viewers yet');
    await user.type(screen.getByRole('searchbox', { name: 'Search viewers' }), 'ann');
    position = 475;
    await user.click(screen.getByRole('link', { name: 'Invites' }));
    await screen.findByRole('heading', { name: 'Invitation history' });
    expect(scrollTo).toHaveBeenLastCalledWith(0, 0);
    await user.click(screen.getByRole('button', { name: 'History Back' }));
    await waitFor(() => expect((screen.getByRole('searchbox', { name: 'Search viewers' }) as HTMLInputElement).value).toBe('ann'));
    expect(scrollTo).toHaveBeenLastCalledWith(0, 475);
  });

  it('keeps tabs in URL history and announces reconnecting data', async () => {
    function Example() {
      const location = useLocation();
      const navigate = useNavigate();
      return <><PageTabs tabs={[{ value: 'overview', label: 'Overview' }, { value: 'history', label: 'History' }]} defaultTab="overview">{(tab) => <p>{tab} content</p>}</PageTabs><output data-testid="path">{location.search}</output><Button onClick={() => navigate(-1)}>Back</Button><ReconnectNotice reconnecting onRetry={() => undefined} /></>;
    }
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={['/example']}><Example /></MemoryRouter>);
    expect(screen.getByRole('tabpanel')).toBeTruthy();
    await user.click(screen.getByRole('tab', { name: 'History' }));
    expect(screen.getByTestId('path').textContent).toBe('?tab=history');
    expect(screen.getByText('history content')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Back' }));
    expect(screen.getByTestId('path').textContent).toBe('');
    expect(screen.getByText('overview content')).toBeTruthy();
    expect(screen.getByText(/Connection interrupted/)).toBeTruthy();
  });
});
