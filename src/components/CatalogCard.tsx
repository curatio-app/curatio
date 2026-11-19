import { memo, useEffect, useRef, useState } from 'react';
import { CircleCheck } from 'lucide-react';
import type { WebMediaItem } from '../api/types';
import {
  cardMetaParts,
  dimsForOwnership,
  displayTitle,
  hasServerLibrary,
  isOnServer,
  itemImage,
  landscapeImage,
  progressFraction,
  releaseBadge,
} from '../api/mediaUtils';
import { useUserState } from '../api/userState';
import { useNavActions } from '../app/navigation';
import { usePlayFromCard } from '../app/playFromCard';
import { clearActiveCard, getActiveCard, setActiveCard, useIsActiveCard } from '../app/activeCard';
import { useHoverIntent } from '../app/useHoverIntent';
import { ActionMenu, useMenuGestures, type ActionMenuHandle } from './ActionMenu';
import { useTitleActions } from './titleActions';
import { useIsHidden } from '../api/hiddenTitles';
import { placeholderColor } from './placeholderColor';
import styles from './CatalogCard.module.css';

export interface CatalogCardProps {
  item: WebMediaItem;
  /** Unique within the page; the shared active-card store keys on it. */
  cardId: string;
  /** 1-based rank for Top 10 rows. */
  rank?: number;
}

/**
 * The tvOS catalog card: a poster at rest that widens into a landscape card
 * (wide art, title, metadata, release badge, rank, progress) when it becomes
 * the active card — immediately on keyboard focus, or after a short hover.
 * It expands in place; the cards after it slide right.
 */
export const CatalogCard = memo(function CatalogCard({ item, cardId, rank }: CatalogCardProps) {
  const { openDetails } = useNavActions();
  const playFromCard = usePlayFromCard(item);
  const { isWatched } = useUserState();
  const active = useIsActiveCard(cardId);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<ActionMenuHandle | null>(null);
  const gestures = useMenuGestures(menuRef);
  const actions = useTitleActions(item, { library: true });
  const hidden = useIsHidden(item);

  const title = displayTitle(item);
  const poster = itemImage(item, true, 'w500');
  const wide = landscapeImage(item, 'w780');
  const badge = releaseBadge(item);
  const fraction = progressFraction(item);
  const watched = isWatched(item);
  const meta = cardMetaParts(item);

  const [posterFailed, setPosterFailed] = useState(false);
  const [wideRequested, setWideRequested] = useState(false);
  const [wideReady, setWideReady] = useState(false);
  useEffect(() => {
    setPosterFailed(false);
    setWideReady(false);
  }, [poster, wide]);
  useEffect(() => {
    if (active) setWideRequested(true);
  }, [active]);
  // Light the row title. A data attribute on the row is far cheaper than a
  // `.shelf:has([data-active])` selector re-checked on every highlight move.
  useEffect(() => {
    if (!active) return undefined;
    const row = buttonRef.current?.closest('[data-nav-row]');
    row?.setAttribute('data-row-active', '');
    return () => row?.removeAttribute('data-row-active');
  }, [active]);

  const hover = useHoverIntent(
    () => setActiveCard(cardId, 'pointer'),
    () => {
      // Keep the card open while it holds keyboard focus.
      if (document.activeElement === buttonRef.current) setActiveCard(cardId, 'focus');
      else clearActiveCard(cardId, 'pointer');
    },
  );

  if (hidden) return null;
  const showFallback = !poster || posterFailed;
  const dim = dimsForOwnership(item);
  const label = [
    title,
    hasServerLibrary() && isOnServer(item) ? 'on your server' : '',
    watched ? 'watched' : '',
    item.reason ?? '',
    badge?.chipText ?? '',
  ].filter(Boolean).join(', ');

  return (
    <div className={`${styles.cell} ${active ? styles.active : ''} ${dim ? styles.notOnServer : ''}`}>
      <button
        ref={buttonRef}
        type="button"
        className={styles.card}
        aria-label={label}
        data-nav-item
        data-own-focus
        data-card-id={cardId}
        data-active={active ? 'true' : undefined}
        onPointerEnter={(event) => {
          if (event.pointerType !== 'touch') setWideRequested(true);
          hover.onPointerEnter(event);
        }}
        onPointerLeave={hover.onPointerLeave}
        onFocus={(event) => {
          // Mouse clicks focus too; only keyboard focus should take over.
          if (event.currentTarget.matches(':focus-visible')) setActiveCard(cardId, 'focus');
        }}
        onBlur={() => {
          if (getActiveCard().source === 'focus') clearActiveCard(cardId, 'focus');
        }}
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
          <span className={styles.fallback} aria-hidden="true">{title}</span>
        ) : (
          <img
            className={styles.poster}
            src={poster}
            alt=""
            loading="lazy"
            decoding="async"
            onError={() => setPosterFailed(true)}
          />
        )}
        {wideRequested && wide ? (
          <img
            className={`${styles.wide} ${wideReady ? styles.wideReady : ''}`}
            src={wide}
            alt=""
            decoding="async"
            onLoad={() => setWideReady(true)}
          />
        ) : null}
        <span className={styles.scrim} aria-hidden="true" />
        <span className={styles.info} aria-hidden="true">
          <span className={styles.title}>{title}</span>
          {item.reason ? <span className={styles.reason}>{item.reason}</span> : null}
          {meta.length ? (
            <span className={styles.meta}>
              {meta.map((part, index) => (
                <span key={`${part}:${index}`} className={styles.metaPart}>{part}</span>
              ))}
            </span>
          ) : null}
        </span>
        {/* Release badge top-left and watched mark top-right can both show (iOS PosterCardView). */}
        {badge ? <span className={styles.badge} aria-hidden="true">{badge.badgeText}</span> : null}
        {watched ? <span className={styles.watched} aria-hidden="true"><CircleCheck size={16} /></span> : null}
        {rank ? (
          <>
            <span className={styles.rankNumeral} aria-hidden="true">{rank}</span>
            <span className={styles.rankChip} aria-hidden="true">#{rank}</span>
          </>
        ) : null}
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
