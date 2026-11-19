import type { MediaTag } from '../api/types';
import primitives from '../styles/primitives.module.css';
import styles from './TagChips.module.css';

export interface TagChipsProps {
  tags: MediaTag[];
  /** Optional: makes the chips clickable, opening browse for that tag. */
  onSelect?: (tag: MediaTag) => void;
  max?: number;
}

/**
 * The tag row on a detail page.
 *
 * Server-side ordering is by IDF weight, so the leading chips are the ones
 * that actually distinguish this title. Re-sorting here would throw that away
 * and put "Drama" first on every card in the library.
 */
export function TagChips({ tags, onSelect, max = 12 }: TagChipsProps) {
  const shown = tags.slice(0, max);
  if (!shown.length) return null;

  return (
    <div className={styles.row}>
      {shown.map((tag) =>
        onSelect ? (
          <button
            key={tag.id}
            type="button"
            className={primitives.pill}
            title={tag.facet ?? undefined}
            data-nav-item
            data-own-focus
            onClick={() => onSelect(tag)}
          >
            {tag.name}
          </button>
        ) : (
          <span key={tag.id} className={primitives.pill} title={tag.facet ?? undefined}>
            {tag.name}
          </span>
        ),
      )}
    </div>
  );
}
