import { useEffect, useState } from 'react';
import styles from './Toaster.module.css';

// Transient banner, like the iPad app's `iOSTransientErrorBanner`. Anything can
// raise one with `showToast(message)`; the newest message replaces the last.

const EVENT = 'curatio:toast';
const VISIBLE_MS = 3200;

export function showToast(message: string) {
  window.dispatchEvent(new CustomEvent<string>(EVENT, { detail: message }));
}

export function Toaster() {
  const [message, setMessage] = useState('');

  useEffect(() => {
    let timer: number | undefined;
    const onToast = (event: Event) => {
      const text = (event as CustomEvent<string>).detail;
      if (!text) return;
      setMessage(text);
      window.clearTimeout(timer);
      timer = window.setTimeout(() => setMessage(''), VISIBLE_MS);
    };
    window.addEventListener(EVENT, onToast);
    return () => {
      window.removeEventListener(EVENT, onToast);
      window.clearTimeout(timer);
    };
  }, []);

  return (
    <div className={styles.region} role="status" aria-live="polite">
      {message ? <div className={styles.toast}>{message}</div> : null}
    </div>
  );
}
