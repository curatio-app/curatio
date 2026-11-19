import type { TitleRating } from '../api/types';
import styles from './RatingRow.module.css';

/** Sources that report a percentage. Rendering 83% as "8.3" reads as a
 * different (and worse) score to anyone who knows the source. */
const PERCENT_SOURCES = new Set([
  'tomatoes',
  'tomatoesaudience',
  'metacritic',
  'metacriticuser',
  'popcorn',
]);

function formatValue(rating: TitleRating): string {
  const raw = rating.value;
  if (raw == null) return rating.normalized.toFixed(1);
  if (PERCENT_SOURCES.has(rating.source)) return `${Math.round(raw)}%`;
  return Number.isInteger(raw) && raw > 10 ? `${Math.round(raw)}%` : raw.toFixed(1);
}

function formatVotes(votes?: number | null): string | null {
  if (!votes || votes < 1000) return votes ? String(votes) : null;
  if (votes >= 1_000_000) return `${(votes / 1_000_000).toFixed(1)}M`;
  return `${Math.round(votes / 1000)}K`;
}

export interface RatingRowProps {
  ratings: TitleRating[];
}

/**
 * Multi-source ratings, from MDBList.
 *
 * Renders nothing when the list is empty rather than falling back to the TMDB
 * score: that number is already on the detail header, and showing it twice
 * under a "Ratings" heading implies a consensus that does not exist.
 */
export function RatingRow({ ratings }: RatingRowProps) {
  if (!ratings.length) return null;

  return (
    <div className={styles.row}>
      {ratings.map((rating) => {
        const votes = formatVotes(rating.votes);
        return (
          <div key={rating.source} className={styles.item}>
            <span className={styles.value}>{formatValue(rating)}</span>
            <span className={styles.label}>{rating.label}</span>
            {votes ? <span className={styles.votes}>{votes} votes</span> : null}
          </div>
        );
      })}
    </div>
  );
}
