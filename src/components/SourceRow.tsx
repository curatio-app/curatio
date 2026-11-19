import { CheckCircle, ChevronRight, Circle } from 'lucide-react';
import { sourceRowModel } from '../api/mediaUtils';
import type { PlaybackSource } from '../api/types';
import styles from './SourceRow.module.css';

// Mirrors the tvOS rows. Picker: resolution badge, the server's `label`, its
// `provider_label`, the monospaced `detail`, then the server's `badges` and the
// size as pills. In-player Sources menu: selection mark, `label`, then
// provider and size. Display strings are rendered verbatim, never parsed.

function badgeClass(resolution: string) {
  if (resolution === '4K') return styles.badge4k;
  if (resolution === '1080p') return styles.badge1080;
  return styles.badgeOther;
}

/** Split a filename so CSS can ellipsize the middle and keep the extension visible. */
function splitMiddle(filename: string): [string, string] {
  const tail = Math.min(16, Math.floor(filename.length / 2));
  return [filename.slice(0, filename.length - tail), filename.slice(filename.length - tail)];
}

export function SourceRow({
  stream,
  selected,
  failure,
  compatibilityMode,
  compatibilityAvailable,
  onSelect,
}: {
  stream: PlaybackSource;
  /** In-player only: show the selection circle. Omit in the picker. */
  selected?: boolean;
  failure?: string;
  compatibilityMode?: boolean;
  compatibilityAvailable?: boolean;
  onSelect: () => void;
}) {
  const model = sourceRowModel(stream);
  const [head, tail] = splitMiddle(model.detail);
  const showSelection = selected !== undefined;
  return (
    <button
      className={`${styles.row} ${selected ? styles.selected : ''}`}
      type="button"
      title={model.detail || model.name}
      aria-pressed={showSelection ? selected : undefined}
      data-nav-item
      onClick={onSelect}
    >
      {showSelection
        ? (selected ? <CheckCircle size={20} className={styles.check} /> : <Circle size={20} className={styles.circle} />)
        : null}
      {showSelection ? null : <span className={`${styles.badge} ${badgeClass(model.resolution)}`}>{model.resolution || '—'}</span>}
      <span className={styles.info}>
        {showSelection ? (
          // The in-player menu, as on tvOS: one summary line, one detail line.
          <>
            <strong className={styles.name}>{model.menuSummary}</strong>
            {model.menuDetails ? <small className={styles.tags}>{model.menuDetails}</small> : null}
          </>
        ) : (
          <>
            <strong className={styles.name}>{model.name}</strong>
            {model.provider ? <small className={styles.tags}>{model.provider}</small> : null}
            {model.detail ? (
              <small className={styles.filename}>
                <span className={styles.filenameHead}>{head}</span>
                <span className={styles.filenameTail}>{tail}</span>
              </small>
            ) : null}
          </>
        )}
        {compatibilityMode
          ? <small className={styles.mode}>Server compatibility mode</small>
          : compatibilityAvailable
            ? <small className={styles.tags}>Server compatibility fallback</small>
            : null}
        {failure ? <small className={styles.failure}>{failure}</small> : null}
      </span>
      {showSelection ? null : (
        <span className={styles.pills}>
          {model.badges.map((badge) => <span key={badge} className={styles.pill}>{badge}</span>)}
          {model.size ? <span className={styles.pill}>{model.size}</span> : null}
        </span>
      )}
      {showSelection ? null : <ChevronRight size={18} className={styles.chevron} />}
    </button>
  );
}
