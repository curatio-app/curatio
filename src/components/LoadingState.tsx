import { Loader2 } from 'lucide-react';
import primitives from '../styles/primitives.module.css';
import styles from './LoadingState.module.css';

export function LoadingState({ label = 'Loading' }: { label?: string }) {
  return (
    <div className={styles.wrap}>
      <Loader2 className={primitives.spin} size={32} />
      <span>{label}</span>
    </div>
  );
}
