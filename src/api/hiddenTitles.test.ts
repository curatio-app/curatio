// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';

const hideTitle = vi.fn();
const unhideTitle = vi.fn();
vi.mock('./accountClient', () => ({
  hideTitle: (...args: unknown[]) => hideTitle(...args),
  unhideTitle: (...args: unknown[]) => unhideTitle(...args),
}));

const { canHide, hideItem, isHidden, resetHiddenTitles, unhide } = await import('./hiddenTitles');

describe('Not for me', () => {
  afterEach(() => {
    resetHiddenTitles();
    hideTitle.mockReset();
    unhideTitle.mockReset();
  });

  it('hides a title by its TMDB id and media type and tells Home to reload', async () => {
    hideTitle.mockResolvedValue({ status: 'ok', changed: true });
    const reload = vi.fn();
    window.addEventListener('curatio:library-changed', reload);
    await hideItem({ tmdb_id: 949, media_type: 'movie', title: 'Heat' });
    window.removeEventListener('curatio:library-changed', reload);
    expect(hideTitle).toHaveBeenCalledWith(949, 'movie');
    expect(isHidden({ tmdb_id: 949, media_type: 'movie' })).toBe(true);
    expect(isHidden({ tmdb_id: 949, media_type: 'tv' })).toBe(false);
    expect(reload).toHaveBeenCalledOnce();
  });

  it('maps series to tv and rolls back when the server refuses', async () => {
    hideTitle.mockRejectedValue(new Error('nope'));
    await expect(hideItem({ tmdb_id: 1399, media_type: 'series' })).rejects.toThrow('nope');
    expect(hideTitle).toHaveBeenCalledWith(1399, 'tv');
    expect(isHidden({ tmdb_id: 1399, media_type: 'series' })).toBe(false);
  });

  it('restores a hidden title', async () => {
    hideTitle.mockResolvedValue({});
    unhideTitle.mockResolvedValue({});
    await hideItem({ tmdb_id: 7, media_type: 'movie' });
    await unhide(7, 'movie');
    expect(isHidden({ tmdb_id: 7, media_type: 'movie' })).toBe(false);
  });

  it('cannot hide items without a TMDB id', () => {
    expect(canHide({ title: 'Home video' })).toBe(false);
    expect(canHide({ tmdb_id: '12', media_type: 'movie' })).toBe(true);
  });
});
