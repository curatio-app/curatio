import { memo, useRef, useState } from 'react';
import type { WebMediaItem } from '../api/types';
import { displayTitle, itemImage, progressCaption, progressFraction } from '../api/mediaUtils';
import { useNavActions } from '../app/navigation';
import { ActionMenu, useMenuGestures, type ActionMenuHandle } from './ActionMenu';
import { useTitleActions } from './titleActions';
import styles from './ContinueWatchingCard.module.css';

/**
 * tvOS `EpisodeFormatting.dottedCaption`: "S2 · E5 · Title", "Special · E1",
 * with a generic "Episode N" title dropped. Up Next cards read "Next: …".
 */
export function episodeCaption(item: WebMediaItem): string {
  if (item.season == null || item.episode == null) return '';
  const season = Number(item.season) === 0 ? 'Special' : `S${item.season}`;
  const name = String(item.episode_title ?? '').trim();
  const parts = [season, `E${item.episode}`, /^episode\s+\d+$/i.test(name) ? '' : name].filter(Boolean);
  return `${item.up_next === true ? 'Next: ' : ''}${parts.join(' · ')}`;
}

export interface ContinueWatchingCardProps {
  item: WebMediaItem;
}

/**
 * tvOS Continue Watching / Up Next card: 16:9 art, a bottom fade, the title,
 * time remaining and an accent progress bar. It lifts on hover and focus but
 * never expands. Activating it resumes playback.
 */
export const ContinueWatchingCard = memo(function ContinueWatchingCard({ item }: ContinueWatchingCardProps) {
  const { openPlayer } = useNavActions();
  const menuRef = useRef<ActionMenuHandle | null>(null);
  const gestures = useMenuGestures(menuRef);
  const season = item.season != null ? Number(item.season) : null;
  const episode = item.episode != null ? Number(item.episode) : null;
  // An Up Next card has no progress to remove: it is the next episode of a show you finished.
  const upNext = item.up_next === true;
  const actions = useTitleActions(item, { continueWatching: !upNext, library: false, season, episode, details: true });
  const src = itemImage(item, false, 'w780');
  const [failedSrc, setFailedSrc] = useState('');
  const title = displayTitle(item);
  const fraction = progressFraction(item);
  const episodeLine = episodeCaption(item);
  const timeLine = progressCaption(item);
  const caption = [episodeLine, timeLine].filter(Boolean).join(', ');

  return (
    <div className={styles.cell}>
      <button
        className={styles.card}
        type="button"
        aria-label={`${upNext ? 'Play' : 'Resume'} ${title}${caption ? `, ${caption}` : ''}`}
        data-nav-item
        data-own-focus
        onKeyDown={(event) => {
          if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
            event.preventDefault();
            menuRef.current?.open();
          }
        }}
        onClick={() => {
          if (!gestures.consumeClick()) openPlayer(item);
        }}
        {...gestures.props}
      >
        {src && failedSrc !== src ? (
          <img className={styles.image} src={src} alt="" loading="lazy" decoding="async" onError={() => setFailedSrc(src)} />
        ) : null}
        <span className={styles.scrim} aria-hidden="true" />
        <span className={styles.text} aria-hidden="true">
          <span className={styles.title}>{title}</span>
          {episodeLine ? <span className={styles.caption}>{episodeLine}</span> : null}
          {timeLine ? <span className={styles.caption}>{timeLine}</span> : null}
        </span>
        {fraction > 0 ? (
          <span className={styles.progress} aria-hidden="true">
            <span style={{ width: `${Math.min(Math.max(fraction, 0), 1) * 100}%` }} />
          </span>
        ) : null}
      </button>
      <ActionMenu ref={menuRef} actions={actions} label={`More actions for ${title}`} className={styles.menuTrigger} />
    </div>
  );
});
