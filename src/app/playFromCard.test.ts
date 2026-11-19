import { describe, expect, it } from 'vitest';
import { isPlayShortcut } from './playFromCard';

const key = (value: string, mods: Partial<{ metaKey: boolean; ctrlKey: boolean; altKey: boolean }> = {}) =>
  ({ key: value, metaKey: false, ctrlKey: false, altKey: false, ...mods });

describe('isPlayShortcut (tvOS Play/Pause on a card)', () => {
  it('takes the media key and P, never Select keys or modified P', () => {
    expect(isPlayShortcut(key('MediaPlayPause'))).toBe(true);
    expect(isPlayShortcut(key('p'))).toBe(true);
    expect(isPlayShortcut(key('P'))).toBe(true);
    expect(isPlayShortcut(key('Enter'))).toBe(false);
    expect(isPlayShortcut(key(' '))).toBe(false);
    expect(isPlayShortcut(key('p', { metaKey: true }))).toBe(false);
  });
});
