import { useEffect, useRef } from 'react';

const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

export function useDialogFocus<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return undefined;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const focusables = () => Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((element) => !element.hidden);
    // Land on the dialog's primary control (e.g. Details' Play) when it names one.
    window.requestAnimationFrame(() => {
      const preferred = dialog.querySelector<HTMLElement>('[data-autofocus]');
      (preferred && !preferred.hasAttribute('disabled') ? preferred : focusables()[0])?.focus({ preventScroll: true });
    });
    const trap = (event: KeyboardEvent) => {
      if (event.key !== 'Tab') return;
      const nodes = focusables();
      if (!nodes.length) return;
      const first = nodes[0]; const last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    dialog.addEventListener('keydown', trap);
    return () => { dialog.removeEventListener('keydown', trap); if (previous?.isConnected) previous.focus({ preventScroll: true }); };
  }, []);
  return ref;
}
