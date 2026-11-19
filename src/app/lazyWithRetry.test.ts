// @vitest-environment jsdom

import { describe, expect, it, vi } from 'vitest';
import { isChunkLoadError, lazyWithRetry } from './lazyWithRetry';

describe('lazyWithRetry', () => {
  it('recognises chunk load failures', () => {
    expect(isChunkLoadError(new TypeError('Failed to fetch dynamically imported module: /x.js'))).toBe(true);
    expect(isChunkLoadError(new Error('Importing a module script failed.'))).toBe(true);
    expect(isChunkLoadError(new Error('Cannot read properties of undefined'))).toBe(false);
  });

  it('retries a failed import once before giving up', async () => {
    const Component = () => null;
    const factory = vi
      .fn<() => Promise<{ default: typeof Component }>>()
      .mockRejectedValueOnce(new Error('network blip'))
      .mockResolvedValueOnce({ default: Component });
    const lazy = lazyWithRetry(factory, { retryDelayMs: 1 });
    await lazy.preload();
    expect(factory).toHaveBeenCalledTimes(2);
  });
});
