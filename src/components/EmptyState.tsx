import { Info } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import primitives from '../styles/primitives.module.css';
import styles from './EmptyState.module.css';

export interface EmptyStateProps {
  icon?: LucideIcon;
  title: string;
  message: string;
  actionLabel?: string;
  onAction?: () => void;
  tone?: 'neutral' | 'error';
}

export function EmptyState({ icon: Icon = Info, title, message, actionLabel, onAction, tone = 'neutral' }: EmptyStateProps) {
  return (
    <div className={`${primitives.emptyState} ${tone === 'error' ? styles.error : ''}`}>
      <Icon size={28} />
      <strong>{title}</strong>
      <p>{message}</p>
      {actionLabel && onAction ? (
        // Its own row, so arrow keys can reach it.
        <span data-nav-row="empty-action" data-nav-memory="off">
          <button className={styles.action} type="button" data-nav-item onClick={onAction}>
            {actionLabel}
          </button>
        </span>
      ) : null}
    </div>
  );
}
