// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import AddonPage, { embedUrl } from './AddonPage';

const integrations = {
  sdk_version: 1,
  integrations: [
    { id: 'plex', label: 'Plex', kind: 'library', enabled: true, health: 'ok', searchable: false, settings_url: null, summary: {} },
    { id: 'example', label: 'Example Sources', kind: 'addon', enabled: true, health: 'ok', searchable: true, settings_url: '/admin/addons/example/', summary: {} },
  ],
  rejected_addons: [],
  source_cache: {},
};

function renderAt(path: string) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(() => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(integrations) } as Response));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><Routes><Route path="/integrations/:addonId" element={<AddonPage />} /></Routes></MemoryRouter></QueryClientProvider>);
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('frames the addon settings page in embed mode under the addon label', async () => {
  renderAt('/integrations/example');
  expect(await screen.findByRole('heading', { name: 'Example Sources' })).toBeTruthy();
  const frame = screen.getByTitle('Example Sources settings') as HTMLIFrameElement;
  expect(frame.getAttribute('src')).toBe('/admin/addons/example/?embed=1');
});

it('grows the frame only for height messages from its own frame', async () => {
  renderAt('/integrations/example');
  const frame = await screen.findByTitle('Example Sources settings') as HTMLIFrameElement;
  window.dispatchEvent(new MessageEvent('message', { origin: window.location.origin, source: window, data: { type: 'curatio:addon-height', height: 2000 } }));
  expect(frame.style.height).toBe('480px');
  window.dispatchEvent(new MessageEvent('message', { origin: window.location.origin, source: frame.contentWindow, data: { type: 'curatio:addon-height', height: 2000 } }));
  expect((await screen.findByTitle('Example Sources settings')).style.height).toBe('2000px');
});

it('explains when no installed addon matches', async () => {
  renderAt('/integrations/plex');
  expect(await screen.findByText('No installed addon matches this address.')).toBeTruthy();
});

it('appends the embed flag to existing query strings', () => {
  expect(embedUrl('/admin/addons/x/?a=1')).toBe('/admin/addons/x/?a=1&embed=1');
});
