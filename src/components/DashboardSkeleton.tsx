import styles from './DashboardSkeleton.module.css';

export function DashboardSkeleton() {
  return <div className={styles.shell} role="status" aria-label="Loading your dashboard">
    <div className={styles.hero}><span /><span /><span /></div>
    <div className={styles.rails}>{[0, 1, 2].map((row) => <section key={row}><div className={styles.heading} /><div className={styles.cards}>{[0, 1, 2, 3, 4, 5].map((card) => <div key={card} className={styles.card} />)}</div></section>)}</div>
  </div>;
}
