import { useEffect } from 'react';
import { useDialogFocus } from './useDialogFocus';
import styles from './ConfirmDialog.module.css';

// A small confirmation alert for destructive actions (the iPad
// `confirmationDialog`): Cancel is first in focus order, the action is red.

export function ConfirmDialog({
  title,
  message,
  confirmLabel,
  onConfirm,
  onCancel,
}: {
  title: string;
  message: string;
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const dialogRef = useDialogFocus<HTMLDivElement>();

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopPropagation();
      onCancel();
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [onCancel]);

  return (
    <div className={styles.backdrop} onClick={onCancel}>
      <div
        ref={dialogRef}
        className={styles.dialog}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
        aria-describedby="confirm-message"
        data-nav-layer="modal"
        onClick={(event) => event.stopPropagation()}
      >
        <strong id="confirm-title">{title}</strong>
        <p id="confirm-message">{message}</p>
        <div className={styles.actions} data-nav-row="confirm" data-nav-memory="off">
          <button type="button" className={styles.cancel} onClick={onCancel} data-nav-item data-own-focus>Cancel</button>
          <button type="button" className={styles.confirm} onClick={onConfirm} data-nav-item data-own-focus>{confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}
