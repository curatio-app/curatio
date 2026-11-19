import { useCallback, useEffect, useRef } from 'react';

/** tvOS-equivalent pointer focus: a card only expands after the pointer rests
 * on it for this long, so sweeping the mouse across a row doesn't ripple. */
export const HOVER_INTENT_MS = 150;

/**
 * Pointer handlers that call `onIntent` once the pointer has rested on the
 * element for `delay` ms, and `onLeave` when it leaves. Touch pointers are
 * ignored (a tap is a click, not a hover).
 */
export function useHoverIntent(onIntent: () => void, onLeave: () => void, delay = HOVER_INTENT_MS) {
  const timer = useRef<number | null>(null);
  const intentRef = useRef(onIntent);
  const leaveRef = useRef(onLeave);
  intentRef.current = onIntent;
  leaveRef.current = onLeave;

  const cancel = useCallback(() => {
    if (timer.current != null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  useEffect(() => cancel, [cancel]);

  const onPointerEnter = useCallback(
    (event: React.PointerEvent) => {
      if (event.pointerType === 'touch') return;
      cancel();
      timer.current = window.setTimeout(() => {
        timer.current = null;
        intentRef.current();
      }, delay);
    },
    [cancel, delay],
  );

  const onPointerLeave = useCallback(
    (event: React.PointerEvent) => {
      if (event.pointerType === 'touch') return;
      cancel();
      leaveRef.current();
    },
    [cancel],
  );

  return { onPointerEnter, onPointerLeave, cancel };
}
