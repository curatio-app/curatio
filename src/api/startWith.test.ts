import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { getStartWith, setStartWith, startWithProfile } from './startWith';
import type { AccountUser, Profile } from './types';

const profile = (id: number): Profile => ({ id, display_name: `P${id}`, user_key: `k${id}`, api_base: '' });

beforeEach(() => {
  const storage = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => { storage.set(key, value); },
    removeItem: (key: string) => { storage.delete(key); },
  });
});
afterEach(() => vi.unstubAllGlobals());

describe('Start With (tvOS Settings)', () => {
  const me: AccountUser = { id: 7 };
  const other: AccountUser = { id: 8 };
  it('asks by default and remembers a profile per account', () => {
    expect(getStartWith(me)).toBe('ask');
    setStartWith(me, 2);
    expect(getStartWith(me)).toBe(2);
    expect(getStartWith(other)).toBe('ask');
    setStartWith(me, 'ask');
    expect(getStartWith(me)).toBe('ask');
  });
  it('falls back to asking when the saved profile was deleted', () => {
    setStartWith(me, 3);
    expect(startWithProfile(me, [profile(1), profile(3)])?.id).toBe(3);
    expect(startWithProfile(me, [profile(1)])).toBeNull();
  });
});
