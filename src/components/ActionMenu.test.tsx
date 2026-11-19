// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ActionMenu } from './ActionMenu';

afterEach(cleanup);

describe('ActionMenu', () => {
  it('opens from the trigger, runs an action and closes', () => {
    const onSelect = vi.fn();
    render(<ActionMenu actions={[{ label: 'Mark as Watched', onSelect }]} label="More actions" />);
    fireEvent.click(screen.getByRole('button', { name: 'More actions' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Mark as Watched' }));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('consumes Escape so the overlay stack does not also go back', () => {
    const outer = vi.fn();
    window.addEventListener('keydown', outer);
    render(<ActionMenu actions={[{ label: 'Remove', onSelect: vi.fn() }]} label="More actions" />);
    fireEvent.click(screen.getByRole('button', { name: 'More actions' }));
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' });
    expect(screen.queryByRole('menu')).toBeNull();
    expect(outer).not.toHaveBeenCalled();
    window.removeEventListener('keydown', outer);
  });
});
