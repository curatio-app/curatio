import { memo, useEffect, useRef, useState } from 'react';
import { Bookmark, CircleCheck } from 'lucide-react';
import type { WebMediaItem } from '../api/types';
import { dimsForOwnership, displayTitle, hasServerLibrary, isOnServer, itemImage, itemKind, progressFraction, releaseBadge } from '../api/mediaUtils';
import { useUserState } from '../api/userState';
import { useNavActions } from '../app/navigation';
import { usePlayFromCard } from '../app/playFromCard';
import { ActionMenu, useMenuGestures, type ActionMenuHandle } from './ActionMenu';
import { useTitleActions } from './titleActions';
import { useIsHidden } from '../api/hiddenTitles';
import { placeholderColor } from './placeholderColor';
import styles from './PosterCard.module.css';

export interface PosterCardProps {
  item: WebMediaItem;
  /** 0–1; falls back to the item's own progress when omitted. */
  progress?: number;
  /** Render the title beneath the poster (used by "Similar Titles"). */
  caption?: boolean;
  /** Show a bookmark badge (used by the Library saved grid). */
  saved?: boolean;
  isNowPlaying?: boolean;
}

export const PosterCard = memo(function PosterCard({
  item,
  progress,
  caption = false,
  saved = false,
  isNowPlaying = false,
}: PosterCardProps) {
  const { openDetails } = useNavActions();
  const playFromCard = usePlayFromCard(item);
  const { isWatched } = useUserState();
  const menuRef = useRef<ActionMenuHandle | null>(null);
  const gestures = useMenuGestures(menuRef);
  const actions = useTitleActions(item, { library: true });
  const hidden = useIsHidden(item);
  const src = itemImage(item, true);
  const title = displayTitle(item);
  const fraction = progress ?? progressFraction(item);
  const watched = isWatched(item);
  const badge = releaseBadge(item);
  const dim = dimsForOwnership(item);
  const ownedLabel = hasServerLibrary() && isOnServer(item) ? ', on your server' : '';
  // A remote poster URL can 404 or be blocked; fall back
  // to the colored title tile instead of the browser's broken-image glyph. Reset when the
  // image source changes so a reused (memoized) card doesn't stay stuck on the fallback.
  const [imgFailed, setImgFailed] = useState(false);
  useEffect(() => setImgFailed(false), [src]);
  const showFallback = !src || imgFailed;
  if (hidden) return null;

  return (
    <div className={styles.cell}>
      <button
        className={`${styles.card} ${dim ? styles.notOnServer : ''}`}
        type="button"
        aria-label={`${title}${ownedLabel}${watched ? ', watched' : ''}${item.reason ? `, ${item.reason}` : ''}`}
        title={item.reason ?? undefined}
        data-nav-item
        data-own-focus
        onKeyDown={(event) => {
          if (playFromCard(event)) return;
          if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
            event.preventDefault();
            menuRef.current?.open();
          }
        }}
        onClick={() => {
          if (!gestures.consumeClick()) openDetails(item);
        }}
        {...gestures.props}
        style={showFallback ? { background: placeholderColor(title) } : undefined}
      >
        {showFallback ? (
          <span className={styles.fallback}>{title}</span>
        ) : (
          <img
            className={styles.image}
            src={src}
            alt=""
            loading="lazy"
            decoding="async"
            onError={() => setImgFailed(true)}
          />
        )}
        {badge ? (
          <span className={`${styles.releaseBadge} ${badge.upcoming ? styles.upcoming : ''}`} aria-hidden="true">
            {badge.badgeText}
          </span>
        ) : null}
        {saved ? (
          <span className={styles.savedBadge} aria-hidden="true">
            <Bookmark size={14} fill="currentColor" />
          </span>
        ) : watched ? (
          <span className={`${styles.savedBadge} ${styles.watchedBadge}`} aria-hidden="true">
            <CircleCheck size={15} />
          </span>
        ) : null}
        {isNowPlaying ? <span className={styles.nowPlaying} aria-hidden="true" /> : null}
        {fraction > 0 ? (
          <span className={styles.progress} aria-hidden="true">
            <span style={{ width: `${Math.min(Math.max(fraction, 0), 1) * 100}%` }} />
          </span>
        ) : null}
      </button>
      <ActionMenu ref={menuRef} actions={actions} label={`More actions for ${title}`} className={styles.menuTrigger} />
      {caption ? (
        <span className={styles.captionBlock}>
          <span className={styles.caption}>{title}</span>
          {/* tvOS Saved Titles footer: "Movie · 2019". */}
          <span className={styles.captionMeta}>{[itemKind(item) === 'series' ? 'Series' : 'Movie', item.year ? String(item.year) : ''].filter(Boolean).join(' · ')}</span>
        </span>
      ) : null}
    </div>
  );
});
