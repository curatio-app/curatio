import { memo, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import type { WebMediaItem } from '../api/types';
import { displayTitle } from '../api/mediaUtils';
import { syncRovingTabIndex } from '../app/spatialNav';
import { PosterCard } from './PosterCard';
import { CatalogCard } from './CatalogCard';
import { ContinueWatchingCard } from './ContinueWatchingCard';
import { useRailDrag } from './useRailDrag';
import styles from './ShelfRow.module.css';

/** Most cards one shelf renders (tvOS rows page in more as you go). */
const MAX_ITEMS = 40;

export interface ShelfRowProps {
  title: string;
  items: WebMediaItem[];
  /**
   * catalog: tvOS expanding cards. topTen: the same with ranks. continue:
   * 16:9 resume cards. poster: plain lift-only posters (Details, Search).
   */
  variant?: 'catalog' | 'topTen' | 'continue' | 'poster';
  /** Stable key for focus memory; defaults to the title. */
  rowKey?: string;
}

function itemKey(item: WebMediaItem, index: number) {
  return `${item.media_type}:${item.tmdb_id ?? item.imdb_id ?? displayTitle(item)}:${item.season ?? ''}:${item.episode ?? ''}:${index}`;
}

/**
 * One tvOS shelf: a row title and a horizontal rail. Arrow keys move within it
 * (spatialNav), the mouse can hover-expand, drag, or use the edge arrows.
 */
export const ShelfRow = memo(function ShelfRow({ title, items, variant = 'poster', rowKey }: ShelfRowProps) {
  const sectionRef = useRef<HTMLElement | null>(null);
  const railRef = useRef<HTMLDivElement | null>(null);
  const rafRef = useRef<number | null>(null);
  const [canLeft, setCanLeft] = useState(false);
  const [canRight, setCanRight] = useState(false);
  const key = rowKey ?? title;
  const expanding = variant === 'catalog' || variant === 'topTen';
  const visible = items.slice(0, MAX_ITEMS);
  useRailDrag(railRef, styles.dragging, visible.length > 0);

  const updateArrows = useCallback(() => {
    const el = railRef.current;
    if (!el) return;
    const max = el.scrollWidth - el.clientWidth;
    setCanLeft(el.scrollLeft > 4);
    setCanRight(el.scrollLeft < max - 4);
  }, []);

  const onScroll = useCallback(() => {
    if (rafRef.current != null) return;
    rafRef.current = window.requestAnimationFrame(() => {
      rafRef.current = null;
      updateArrows();
    });
  }, [updateArrows]);

  useEffect(() => {
    updateArrows();
    const el = railRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(updateArrows);
    observer.observe(el);
    return () => {
      observer.disconnect();
      if (rafRef.current != null) window.cancelAnimationFrame(rafRef.current);
    };
  }, [updateArrows, items]);

  useLayoutEffect(() => {
    if (sectionRef.current) syncRovingTabIndex(sectionRef.current);
  }, [items]);

  const scrollByDir = useCallback((direction: number) => {
    const el = railRef.current;
    if (!el) return;
    el.scrollBy({ left: direction * el.clientWidth * 0.85, behavior: 'smooth' });
  }, []);

  if (!visible.length) return null;

  return (
    <section
      className={`${styles.shelf} ${variant === 'continue' ? styles.continue : ''}`}
      ref={sectionRef}
      data-nav-row={key}
      aria-label={title || undefined}
    >
      {title ? <h2 className={styles.title}>{title}</h2> : null}
      <div className={styles.railWrap}>
        <button
          type="button"
          className={`${styles.arrow} ${styles.left} ${canLeft ? styles.arrowShown : ''}`}
          aria-label="Scroll left"
          tabIndex={-1}
          onClick={() => scrollByDir(-1)}
        >
          <ChevronLeft size={28} />
        </button>
        <div
          className={`${styles.rail} ${expanding ? styles.expandingRail : ''}`}
          ref={railRef}
          onScroll={onScroll}
          data-nav-rail
          data-nav-expands={expanding ? '' : undefined}
        >
          {visible.map((item, index) => {
            const id = itemKey(item, index);
            if (variant === 'continue') return <ContinueWatchingCard key={id} item={item} />;
            if (expanding) {
              return (
                <CatalogCard
                  key={id}
                  item={item}
                  cardId={`${key}::${id}`}
                  rank={variant === 'topTen' && index < 10 ? index + 1 : undefined}
                />
              );
            }
            return <PosterCard key={id} item={item} />;
          })}
        </div>
        <button
          type="button"
          className={`${styles.arrow} ${styles.right} ${canRight ? styles.arrowShown : ''}`}
          aria-label="Scroll right"
          tabIndex={-1}
          onClick={() => scrollByDir(1)}
        >
          <ChevronRight size={28} />
        </button>
      </div>
    </section>
  );
});

