import { memo, useCallback, useEffect, useRef, useState } from 'react';
import { Info, Play } from 'lucide-react';
import type { WebMediaItem } from '../api/types';
import { asText, displayTitle, isPlayable, itemImage, itemKind, logoImage, runtimeLabel, fullBleedSize } from '../api/mediaUtils';
import { useUserState } from '../api/userState';
import { useNavActions, useNavState } from '../app/navigation';
import styles from './HeroCarousel.module.css';

/** tvOS HomeHeroCyclingPolicy.dwell and HeroRotationPolicy.crossfadeDuration. */
export const HERO_DWELL_MS = 15000;
export const HERO_CROSSFADE_MS = 900;

export interface HeroCarouselProps {
  items: WebMediaItem[];
}

function heroKey(item: WebMediaItem | undefined) {
  return item ? `${item.media_type}:${item.tmdb_id ?? item.imdb_id ?? displayTitle(item)}` : '';
}

/** "Film • Drama • 2024 • 2h 10m" — the genre only when there is exactly one. */
function metaLine(item: WebMediaItem) {
  const genres = (item.genres ?? []).filter(Boolean);
  return [
    itemKind(item) === 'series' ? 'Series' : 'Film',
    genres.length === 1 ? genres[0] : '',
    item.year ? String(item.year) : '',
    runtimeLabel(item),
  ].filter(Boolean);
}

function prefersReducedMotion() {
  return typeof window !== 'undefined' && Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)').matches);
}

/**
 * The tvOS Home billboard: an inset rounded card with the title's backdrop,
 * logo lockup, metadata, genre pills, a two-line synopsis, Play and More Info,
 * and page dots. It advances every 15 s while nobody is looking at it closely
 * (paused on hover, focus, when scrolled away, behind an overlay, or with
 * reduced motion). Left from Play and right from More Info change titles.
 */
export const HeroCarousel = memo(function HeroCarousel({ items }: HeroCarouselProps) {
  const { openDetails, openPlayer } = useNavActions();
  const { overlayDepth } = useNavState();
  const { continueWatching } = useUserState();
  const sectionRef = useRef<HTMLElement | null>(null);
  const actionsRef = useRef<HTMLDivElement | null>(null);
  const [index, setIndex] = useState(0);
  // Two art layers: the committed one stays opaque under the incoming one.
  const [layers, setLayers] = useState<{ base: string; incoming: string; fading: boolean }>({
    base: '',
    incoming: '',
    fading: false,
  });
  const [brokenLogo, setBrokenLogo] = useState('');
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [onScreen, setOnScreen] = useState(true);
  const [hidden, setHidden] = useState(typeof document !== 'undefined' ? document.hidden : false);

  const count = items.length;
  const item = items[Math.min(index, Math.max(count - 1, 0))];
  const itemsKey = items.map(heroKey).join('|');

  useEffect(() => setIndex(0), [itemsKey]);

  const go = useCallback((step: number) => {
    if (count < 2) return;
    setIndex((value) => (value + step + count) % count);
  }, [count]);

  // Prepare-then-commit crossfade: decode the next backdrop before fading it in.
  const art = item ? itemImage(item, false, fullBleedSize()) : '';
  useEffect(() => {
    if (!art) return undefined;
    let cancelled = false;
    const image = new Image();
    image.src = art;
    const commit = () => {
      if (cancelled) return;
      setLayers((current) => {
        if (!current.base) return { base: art, incoming: '', fading: false };
        if (current.base === art) return current;
        return { base: current.base, incoming: art, fading: true };
      });
    };
    if (typeof image.decode === 'function') image.decode().then(commit, commit);
    else commit();
    return () => {
      cancelled = true;
    };
  }, [art]);

  useEffect(() => {
    if (!layers.fading) return undefined;
    const timer = window.setTimeout(
      () => setLayers((current) => ({ base: current.incoming || current.base, incoming: '', fading: false })),
      prefersReducedMotion() ? 0 : HERO_CROSSFADE_MS,
    );
    return () => window.clearTimeout(timer);
  }, [layers.fading, layers.incoming]);

  // Pause when scrolled (nearly) out of view.
  useEffect(() => {
    const node = sectionRef.current;
    if (!node || typeof IntersectionObserver === 'undefined') return undefined;
    const observer = new IntersectionObserver(
      ([entry]) => setOnScreen(entry.intersectionRatio >= 0.05),
      { threshold: [0, 0.05, 0.2] },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const onVisibility = () => setHidden(document.hidden);
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, []);

  const paused = count < 2 || hovered || focused || !onScreen || hidden || overlayDepth > 0 || prefersReducedMotion();

  // Warm the following slide while this one is on screen, so the next
  // rotation crossfades immediately instead of waiting on the network.
  const nextArt = count > 1 ? itemImage(items[(index + 1) % count], false, fullBleedSize()) : '';
  useEffect(() => {
    if (!nextArt || paused) return undefined;
    const timer = window.setTimeout(() => {
      const image = new Image();
      image.decoding = 'async';
      image.src = nextArt;
    }, 1500);
    return () => window.clearTimeout(timer);
  }, [nextArt, paused]);

  useEffect(() => {
    if (paused) return undefined;
    const timer = window.setTimeout(() => go(1), HERO_DWELL_MS);
    return () => window.clearTimeout(timer);
  }, [paused, index, go]);

  // Arrow keys at the button row's edges change titles (tvOS HeroNativeControls).
  useEffect(() => {
    const row = actionsRef.current;
    if (!row) return undefined;
    const onEdge = (event: Event) => {
      const direction = (event as CustomEvent<{ direction: string }>).detail?.direction;
      if (direction === 'left') go(-1);
      if (direction === 'right') go(1);
    };
    row.addEventListener('nav-edge', onEdge);
    return () => row.removeEventListener('nav-edge', onEdge);
  }, [go]);

  if (!item) {
    return (
      <section className={styles.hero} aria-hidden="true">
        <div className={styles.placeholder} />
      </section>
    );
  }

  const logo = logoImage(item);
  const title = displayTitle(item);
  const genres = (item.genres ?? []).filter(Boolean);
  const synopsis = asText(item.overview) || asText(item.description);

  // Play resumes where the profile left off. A series with no Continue Watching
  // entry has no known episode yet, so it opens Details (which picks one).
  const playable = isPlayable(item);
  const play = () => {
    if (!playable) {
      openDetails(item);
      return;
    }
    if (itemKind(item) === 'movie') {
      openPlayer(item);
      return;
    }
    const resume = continueWatching.find(
      (entry) => itemKind(entry) === 'series' && entry.season != null && entry.episode != null
        && ((item.tmdb_id != null && String(entry.tmdb_id) === String(item.tmdb_id)) || (item.imdb_id != null && entry.imdb_id === item.imdb_id)),
    );
    if (resume) openPlayer(resume);
    else openDetails(item);
  };

  return (
    <section
      ref={sectionRef}
      className={styles.hero}
      aria-roledescription="carousel"
      aria-label="Featured title"
      onPointerEnter={() => setHovered(true)}
      onPointerLeave={() => setHovered(false)}
      onFocus={() => setFocused(true)}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setFocused(false);
      }}
    >
      <div className={styles.frame}>
        {layers.base ? <img className={styles.art} src={layers.base} alt="" decoding="async" /> : null}
        {layers.incoming ? (
          <img
            key={layers.incoming}
            className={`${styles.art} ${styles.incoming}`}
            src={layers.incoming}
            alt=""
            decoding="async"
          />
        ) : null}
        <div className={styles.radial} aria-hidden="true" />
        <div className={styles.bottomFade} aria-hidden="true" />
        <div className={styles.topVeil} aria-hidden="true" />

        <div className={styles.content} key={heroKey(item)} aria-live="polite">
          {logo && brokenLogo !== logo ? (
            <img className={styles.logo} src={logo} alt={title} decoding="async" onError={() => setBrokenLogo(logo)} />
          ) : (
            <h1 className={styles.title}>{title}</h1>
          )}
          <p className={styles.meta}>
            {metaLine(item).map((part, partIndex) => (
              <span key={`${part}:${partIndex}`} className={styles.metaPart}>{part}</span>
            ))}
          </p>
          {genres.length >= 2 ? (
            <div className={styles.pills}>
              {genres.slice(0, 3).map((genre) => (
                <span key={genre} className={styles.pill}>{genre}</span>
              ))}
            </div>
          ) : null}
          {synopsis ? <p className={styles.synopsis}>{synopsis}</p> : null}
        </div>

        <div className={styles.actions} ref={actionsRef} data-nav-row="hero" data-nav-top data-nav-memory="off">
          {/* A recommendation-only title has no Play: More Info offers Watchlist. */}
          {playable ? (
            <button
              className={`${styles.action} ${styles.play}`}
              type="button"
              onClick={play}
              data-nav-item
              data-nav-default
              data-own-focus
            >
              <Play size={22} fill="currentColor" aria-hidden="true" />
              Play
            </button>
          ) : null}
          <button
            className={styles.action}
            type="button"
            onClick={() => openDetails(item)}
            data-nav-item
            data-nav-default={playable ? undefined : true}
            data-own-focus
          >
            <Info size={22} aria-hidden="true" />
            More Info
          </button>
        </div>

        {count > 1 ? (
          <div className={styles.dots} role="group" aria-label="Featured titles">
            {items.map((entry, dotIndex) => (
              <button
                key={`${heroKey(entry)}:${dotIndex}`}
                type="button"
                tabIndex={-1}
                className={`${styles.dot} ${dotIndex === index ? styles.dotActive : ''}`}
                aria-label={`Show ${displayTitle(entry)}`}
                aria-current={dotIndex === index ? 'true' : undefined}
                onClick={() => setIndex(dotIndex)}
              >
                <span />
              </button>
            ))}
          </div>
        ) : null}
      </div>
    </section>
  );
});
