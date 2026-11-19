// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiRequest, getDetails, requestSignal } from './accountClient';

afterEach(() => {
  vi.restoreAllMocks();
});

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

describe('apiRequest resilience', () => {
  it('retries a read after a 503', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(json(503, { detail: 'busy' }))
      .mockResolvedValueOnce(json(200, { ok: true }));
    await expect(apiRequest('/account/api/x')).resolves.toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('never retries a write', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(503, { detail: 'busy' }));
    await expect(apiRequest('/account/api/x', { method: 'POST', body: '{}' })).rejects.toThrow('busy');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('does not retry a 4xx', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(404, { detail: 'nope' }));
    await expect(apiRequest('/account/api/x')).rejects.toThrow('nope');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('builds a combined signal without AbortSignal.any', () => {
    const caller = new AbortController();
    const { signal, done } = requestSignal(caller.signal, 10_000);
    caller.abort();
    expect(signal.aborted).toBe(true);
    done();
  });

  it('shares one details request between callers', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json(200, { item: { title: 'X' } }));
    const item = { tmdb_id: 98765, media_type: 'movie' as const };
    await Promise.all([getDetails(item), getDetails(item)]);
    await getDetails(item);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
