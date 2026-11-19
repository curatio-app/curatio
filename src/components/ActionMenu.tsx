import { MoreHorizontal } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { forwardRef, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useRef, useState } from 'react';
import type { PointerEvent as ReactPointerEvent } from 'react';
import { createPortal } from 'react-dom';
import styles from './ActionMenu.module.css';

// The iPad app's context menus (long-press on Play, episode rows, cards) as a
// "…" button that also opens on right-click or a touch long-press of its owner.

export interface MenuAction {
  label: string;
  icon?: LucideIcon;
  destructive?: boolean;
  onSelect: () => void;
}

export interface ActionMenuHandle {
  /** Open anchored to a point (right-click) or to the trigger button. */
  open: (point?: { x: number; y: number }) => void;
}

const MENU_WIDTH = 248;

export const ActionMenu = forwardRef<ActionMenuHandle, {
  actions: MenuAction[];
  label: string;
  className?: string;
  /** Hide the "…" trigger (menu opened only via the owner's right-click/long-press). */
  hiddenTrigger?: boolean;
  /** Reachable with the arrow keys as part of the row it sits in. */
  navItem?: boolean;
}>(function ActionMenu({ actions, label, className, hiddenTrigger = false, navItem = false }, ref) {
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const [position, setPosition] = useState<{ x: number; y: number } | null>(null);

  const openerRef = useRef<HTMLElement | null>(null);
  const open = useCallback((point?: { x: number; y: number }) => {
    if (!actions.length) return;
    // Focus goes back to whatever opened the menu (a card, via its context
    // menu key), not always to the "…" button.
    openerRef.current = document.activeElement instanceof HTMLElement && document.activeElement !== document.body ? document.activeElement : null;
    if (point) {
      setPosition(point);
      return;
    }
    const rect = triggerRef.current?.getBoundingClientRect();
    setPosition(rect ? { x: rect.right, y: rect.bottom + 6 } : { x: window.innerWidth / 2, y: window.innerHeight / 2 });
  }, [actions.length]);
  const close = useCallback((restoreFocus = true) => {
    setPosition(null);
    if (restoreFocus) (openerRef.current?.isConnected ? openerRef.current : triggerRef.current)?.focus({ preventScroll: true });
  }, []);

  useImperativeHandle(ref, () => ({ open }), [open]);

  // Keep the menu on screen.
  useLayoutEffect(() => {
    const menu = menuRef.current;
    if (!position || !menu) return;
    const height = menu.offsetHeight;
    const left = Math.min(Math.max(8, position.x - MENU_WIDTH), window.innerWidth - MENU_WIDTH - 8);
    const top = position.y + height > window.innerHeight - 8 ? Math.max(8, position.y - height - 12) : position.y;
    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
    menu.querySelector<HTMLButtonElement>('button')?.focus();
  }, [position]);

  useEffect(() => {
    if (!position) return undefined;
    const onPointer = (event: PointerEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) close(false);
    };
    const onKey = (event: KeyboardEvent) => {
      const items = Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>('button') ?? []);
      const index = items.indexOf(document.activeElement as HTMLButtonElement);
      if (event.key === 'Escape') {
        // Consume it so the overlay stack doesn't also go back.
        event.preventDefault();
        event.stopPropagation();
        close();
      } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const step = event.key === 'ArrowDown' ? 1 : -1;
        items[(index + step + items.length) % items.length]?.focus();
      } else if (event.key === 'Tab') {
        close(false);
      }
    };
    const onScroll = () => close(false);
    window.addEventListener('pointerdown', onPointer, true);
    window.addEventListener('keydown', onKey, true);
    window.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', onScroll);
    return () => {
      window.removeEventListener('pointerdown', onPointer, true);
      window.removeEventListener('keydown', onKey, true);
      window.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('resize', onScroll);
    };
  }, [position, close]);

  if (!actions.length) return null;

  return (
    <>
      <button
        ref={triggerRef}
        className={`${styles.trigger} ${hiddenTrigger ? styles.hiddenTrigger : ''} ${className ?? ''}`}
        type="button"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={Boolean(position)}
        tabIndex={hiddenTrigger ? -1 : undefined}
        data-nav-item={navItem ? '' : undefined}
        onClick={(event) => {
          event.stopPropagation();
          if (position) close();
          else open();
        }}
      >
        <MoreHorizontal size={18} />
      </button>
      {position ? createPortal(
        <div ref={menuRef} className={styles.menu} role="menu" aria-label={label} style={{ left: position.x, top: position.y }}>
          {actions.map((action) => {
            const Icon = action.icon;
            return (
              <button
                key={action.label}
                className={`${styles.item} ${action.destructive ? styles.destructive : ''}`}
                type="button"
                role="menuitem"
                onClick={(event) => {
                  event.stopPropagation();
                  close();
                  action.onSelect();
                }}
              >
                <span>{action.label}</span>
                {Icon ? <Icon size={17} /> : null}
              </button>
            );
          })}
        </div>,
        document.body,
      ) : null}
    </>
  );
});

const LONG_PRESS_MS = 500;

/**
 * Right-click and touch long-press handlers for a card that owns an ActionMenu.
 * Returns props to spread on the card plus `consumeClick()` to swallow the click
 * that follows a long-press.
 */
export function useMenuGestures(menu: React.RefObject<ActionMenuHandle | null>) {
  const timer = useRef<number | null>(null);
  const fired = useRef(false);
  const start = useRef<{ x: number; y: number } | null>(null);

  const cancel = () => {
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = null;
  };

  return {
    props: {
      onContextMenu: (event: React.MouseEvent) => {
        if (!menu.current) return;
        event.preventDefault();
        menu.current.open({ x: event.clientX + MENU_WIDTH / 2, y: event.clientY });
      },
      onPointerDown: (event: ReactPointerEvent) => {
        fired.current = false;
        if (event.pointerType !== 'touch') return;
        start.current = { x: event.clientX, y: event.clientY };
        cancel();
        timer.current = window.setTimeout(() => {
          fired.current = true;
          menu.current?.open({ x: (start.current?.x ?? 0) + MENU_WIDTH / 2, y: start.current?.y ?? 0 });
        }, LONG_PRESS_MS);
      },
      onPointerMove: (event: ReactPointerEvent) => {
        const origin = start.current;
        if (origin && Math.hypot(event.clientX - origin.x, event.clientY - origin.y) > 10) cancel();
      },
      onPointerUp: cancel,
      onPointerCancel: cancel,
    },
    /** True (and resets) when the pending click came from a long-press. */
    consumeClick: () => {
      const wasLongPress = fired.current;
      fired.current = false;
      return wasLongPress;
    },
  };
}
