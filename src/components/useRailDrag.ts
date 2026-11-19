import { useEffect } from 'react';

/**
 * Mouse drag-to-scroll for horizontal rails. Locks to the horizontal axis once a
 * drag is detected and suppresses the click that would otherwise fire on the
 * child it started from. Trackpad/touch use native momentum scrolling — we only
 * handle the mouse-drag case (and never hijack the wheel), so scrolling stays
 * smooth.
 */
/** `enabled` re-attaches once a rail that rendered empty gets its items. */
export function useRailDrag(ref: React.RefObject<HTMLDivElement>, draggingClass?: string, enabled = true) {
  useEffect(() => {
    const rail = ref.current;
    if (!rail || !enabled) return undefined;
    let down = false;
    let locked: 'x' | 'y' | null = null;
    let moved = false;
    let startX = 0;
    let startY = 0;
    let startScroll = 0;
    let pointerId: number | null = null;

    const reset = () => {
      down = false;
      locked = null;
      pointerId = null;
      if (draggingClass) rail.classList.remove(draggingClass);
      window.setTimeout(() => {
        moved = false;
      }, 0);
    };

    const onClick = (event: MouseEvent) => {
      if (moved) {
        event.preventDefault();
        event.stopPropagation();
      }
    };
    const onPointerDown = (event: PointerEvent) => {
      // Only mouse drag — touch/pen/trackpad scroll natively (smoother).
      if (event.pointerType !== 'mouse' || event.button !== 0) return;
      down = true;
      locked = null;
      moved = false;
      pointerId = event.pointerId;
      startX = event.clientX;
      startY = event.clientY;
      startScroll = rail.scrollLeft;
    };
    const onPointerMove = (event: PointerEvent) => {
      if (!down || event.pointerId !== pointerId) return;
      const dx = event.clientX - startX;
      const dy = event.clientY - startY;
      if (!locked) {
        if (Math.max(Math.abs(dx), Math.abs(dy)) < 8) return;
        locked = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
        if (locked === 'x') {
          if (draggingClass) rail.classList.add(draggingClass);
          try {
            rail.setPointerCapture(event.pointerId);
          } catch {
            // Pointer capture is best-effort for older browsers.
          }
        }
      }
      if (locked === 'x') {
        rail.scrollLeft = startScroll - dx;
        moved = true;
        event.preventDefault();
      }
    };

    rail.addEventListener('click', onClick, true);
    rail.addEventListener('pointerdown', onPointerDown);
    rail.addEventListener('pointermove', onPointerMove, { passive: false });
    rail.addEventListener('pointerup', reset);
    rail.addEventListener('pointercancel', reset);
    return () => {
      rail.removeEventListener('click', onClick, true);
      rail.removeEventListener('pointerdown', onPointerDown);
      rail.removeEventListener('pointermove', onPointerMove);
      rail.removeEventListener('pointerup', reset);
      rail.removeEventListener('pointercancel', reset);
    };
  }, [draggingClass, ref, enabled]);
}

