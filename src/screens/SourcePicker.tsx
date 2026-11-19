import { ChevronLeft, Loader2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { isAbortError } from '../api/accountClient';
import { hasLocalEngine } from '../api/desktopBridge';
import { displayTitle, episodeLabel } from '../api/mediaUtils';
import { loadPlaybackSources, peekPlaybackSources, type PlaybackSources, type SourceSelection, type SourceTier } from '../api/playbackSources';
import { routeIdentity } from '../api/streamMeta';
import type { WebMediaItem } from '../api/types';
import { SourceRow } from '../components/SourceRow';
import { defaultSourceTier } from '../api/preferences';
import { useDialogFocus } from '../components/useDialogFocus';
import primitives from '../styles/primitives.module.css';
import styles from './SourcePicker.module.css';

// Pre-play source picker, shown when Autoplay is off — the web mirror of the
// tvOS `SourcePickerView`. Sources come from the shared 5-minute cache, so the
// player reuses this exact list (and route identity) without a second resolve.

export function SourcePicker({
  item,
  profileId,
  onSelect,
  onClose,
}: {
  item: WebMediaItem;
  profileId?: number;
  onSelect: (selection: SourceSelection) => void;
  onClose: () => void;
}) {
  const [tier, setTier] = useState<SourceTier>(defaultSourceTier);
  const [refreshToken, setRefreshToken] = useState(0);
  const [sources, setSources] = useState<PlaybackSources | null>(() => peekPlaybackSources(item, defaultSourceTier(), profileId));
  const [error, setError] = useState('');
  const dialogRef = useDialogFocus<HTMLElement>();

  useEffect(() => {
    const controller = new AbortController();
    setError('');
    const cached = refreshToken === 0 ? peekPlaybackSources(item, tier, profileId) : null;
    setSources(cached);
    if (cached) return undefined;
    loadPlaybackSources(item, { tier, profileId, refresh: refreshToken > 0, signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) setSources(value); })
      .catch((err) => {
        if (controller.signal.aborted || isAbortError(err)) return;
        setError(err instanceof Error ? err.message : 'Could not load sources.');
      });
    return () => controller.abort();
  }, [item, tier, profileId, refreshToken]);

  const streams = sources?.streams ?? [];
  const episode = episodeLabel(item);
  const loading = !sources && !error;
  const here = hasLocalEngine() ? 'in this app' : 'in this browser';
  // Offer the other Playback quality: Original when the server answered with
  // Optimized, Optimized (a server transcode) when nothing Original can play here.
  const alternate: SourceTier | null = !sources ? null
    : sources.quality === 'optimized' ? 'original'
      : !streams.length && sources.resolvedCount ? 'optimized' : null;
  const alternateLabel = alternate === 'original' ? 'Show Original Quality' : 'Try Optimized Quality';

  return (
    <section ref={dialogRef} className={styles.overlay} role="dialog" aria-modal="true" aria-label={`Choose a source for ${displayTitle(item)}`}>
      <div className={styles.scroll} data-nav-scroll>
        <header className={styles.header} data-nav-row="picker-head" data-nav-memory="off">
          <button className={styles.back} type="button" onClick={onClose} aria-label="Back" data-nav-item>
            <ChevronLeft size={22} />
          </button>
          <div className={styles.heading}>
            <h1 className={styles.title}>{displayTitle(item)}</h1>
            {episode ? <span className={styles.subtitle}>{episode}</span> : null}
          </div>
        </header>

        <div className={styles.body}>
          {loading ? (
            <div className={styles.state}>
              <Loader2 className={primitives.spin} size={28} />
              <span>Finding sources…</span>
            </div>
          ) : error || !streams.length ? (
            <div className={styles.state}>
              <strong>{error ? 'Sources Unavailable' : 'Not Available to Play'}</strong>
              <p>{error || `No playable sources were found for this ${episode ? 'episode' : 'title'}.`}</p>
              {!error && sources?.resolvedCount ? (
                <p>{`The sources found can't play ${here} at ${sources.quality === 'optimized' ? 'Optimized' : 'Original'} quality.`}</p>
              ) : null}
              <div className={styles.actions} data-nav-row="picker-actions" data-nav-memory="off">
                <button className={styles.action} type="button" data-nav-item onClick={() => setRefreshToken((value) => value + 1)}>Retry</button>
                {alternate ? <button className={styles.action} type="button" data-nav-item onClick={() => setTier(alternate)}>{alternateLabel}</button> : null}
              </div>
            </div>
          ) : (
            <>
              <div className={styles.countRow} data-nav-row="picker-tier" data-nav-memory="off">
                <span className={styles.count}>{streams.length} {streams.length === 1 ? 'source' : 'sources'} available</span>
                {alternate ? <button className={styles.link} type="button" data-nav-item onClick={() => setTier(alternate)}>{alternateLabel}</button> : null}
              </div>
              <div className={styles.list}>
                {streams.map((stream, index) => (
                  // One row each: Up/Down walk the list.
                  <div key={`${routeIdentity(stream)}:${index}`} data-nav-row={`source-${index}`} data-nav-memory="off">
                    <SourceRow stream={stream} onSelect={() => onSelect({ sourceKey: routeIdentity(stream), tier })} />
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </div>
    </section>
  );
}
