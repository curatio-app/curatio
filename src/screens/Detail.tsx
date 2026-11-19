import { useEffect, useMemo, useRef, useState } from 'react';
import { Bookmark, BookmarkCheck, Check, ChevronDown, ChevronLeft, ChevronRight, CircleCheck, Clapperboard, ListFilter, ListVideo, Play, RotateCcw } from 'lucide-react';
import { getDetails } from '../api/accountClient';
import type { CastMember, DetailsResponse, EpisodeItem, SeasonItem, WebMediaItem } from '../api/types';
import {
  asText,
  clamp,
  detailInitials,
  displayTitle,
  episodeComingText,
  episodeHasAired,
  episodePlayItem,
  flattenEpisodes,
  isPlayable,
  itemImage,
  itemKind,
  logoImage,
  playableEpisode,
  progressFraction,
  releaseBadge,
  remainingTimeLabel,
  runtimeLabel,
  safeUrl,
  seriesReleaseBadge,
  fullBleedSize,
  NOT_IN_LIBRARY_NOTE,
  notOnServerNote,
  playbackProgressKey,
} from '../api/mediaUtils';
import { detailInfoRows, episodeMarker, heroBadge, orderedScores, type PrimaryKind } from './detailPresentation';
import { defaultSourceTier, getPreferences } from '../api/preferences';
import { useUserState } from '../api/userState';
import { latestSeriesResume, markStartFromBeginning, resumeInfoFor, resumeStartFor, serverResumeFor } from '../player/playerEngine';
import { prefetchPlayback } from '../player/playbackPlan';
import type { ResumeInfo } from '../player/playerEngine';
import { useNavActions } from '../app/navigation';
import { ActionMenu, useMenuGestures, type ActionMenuHandle } from '../components/ActionMenu';
import { ShelfRow } from '../components/ShelfRow';
import { RatingRow } from '../components/RatingRow';
import { TagChips } from '../components/TagChips';
import { showToast } from '../components/Toaster';
import { useTitleActions } from '../components/titleActions';
import { useRailDrag } from '../components/useRailDrag';
import { useDialogFocus } from '../components/useDialogFocus';
import styles from './Detail.module.css';

const CAST_PAIRS: Array<[string, string]> = [
  ['#0d1f2e', '#6bb3eb'],
  ['#1f122e', '#ad8aeb'],
  ['#0d2414', '#6bc980'],
  ['#2c170a', '#eb9e61'],
  ['#290d0f', '#e66b73'],
];

function castColor(name: string) {
  let hash = 0;
  for (let i = 0; i < name.length; i += 1) hash = (hash * 31 + name.charCodeAt(i)) >>> 0;
  return CAST_PAIRS[hash % CAST_PAIRS.length];
}

/** tvOS `EpisodeFormatting.tag`: "S1:E2". */
function episodeCode(item: { season?: unknown; episode?: unknown }) {
  return `S${item.season}:E${item.episode}`;
}

export interface PrimaryAction {
  label: string;
  target: WebMediaItem | null;
  resume: ResumeInfo | null;
  /** What Play does (native PlaybackTarget): drives the hero badge and episode markers. */
  kind: PrimaryKind;
}

/**
 * The iPad primary pill: "Play", "Resume · 1h 3m", "Resume S1-E3 · 12m",
 * "Play S2-E1" (next unwatched episode), or "Episodes Unavailable". A title
 * that isn't playable here (`availability.playable !== true`) is a
 * recommendation only: its primary action is Watchlist, with no Play target.
 */
export function primaryAction(
  item: WebMediaItem,
  seasons: SeasonItem[],
  loaded: boolean,
  isWatched: (item: WebMediaItem, season?: unknown, episode?: unknown) => boolean,
  /** The server's Continue Watching list: progress made on another device. */
  continueWatching: WebMediaItem[] = [],
): PrimaryAction {
  if (!isPlayable(item)) return { label: 'Watchlist', target: null, resume: null, kind: 'watchlist' };
  if (itemKind(item) === 'movie') {
    const resume = resumeInfoFor(item) ?? serverResumeFor(item, continueWatching);
    return {
      // tvOS PlaybackTargetResolver: the time left shows under the progress bar.
      label: resume ? 'Resume' : 'Play',
      target: item,
      resume,
      kind: resume ? 'resume' : 'play',
    };
  }
  const resume = latestSeriesResume(item, seasons) ?? serverResumeFor(item, continueWatching, seasons);
  if (resume) {
    return { label: `Resume ${episodeCode(resume.item)}`, target: resume.item, resume, kind: 'resume' };
  }
  // Unaired episodes have no sources (native SeriesWatchState): never a Play target.
  const episodes = flattenEpisodes(seasons).filter((episode) => playableEpisode(episode) && episodeHasAired(episode));
  if (!episodes.length) return { label: loaded ? 'Episodes Unavailable' : 'Loading', target: null, resume: null, kind: loaded ? 'unavailable' : 'loading' };
  const unwatched = episodes.find((episode) => !isWatched(item, episode.season, episode.episode));
  const anyWatched = episodes.some((episode) => isWatched(item, episode.season, episode.episode));
  // Caught up: start the series over from the first episode.
  const target = episodePlayItem(item, unwatched ?? episodes[0]);
  const kind: PrimaryKind = !unwatched ? 'restart' : anyWatched ? 'next' : 'start';
  return { label: `Play ${episodeCode(target)}`, target, resume: null, kind };
}

export function Detail({ item }: { item: WebMediaItem }) {
  const { closeDetails, openPlayer, openBrowse, openSourcePicker } = useNavActions();
  const user = useUserState();
  const [details, setDetails] = useState<DetailsResponse | null>(null);
  const [error, setError] = useState('');
  const [reloadToken, setReloadToken] = useState(0);
  const [selectedSeason, setSelectedSeason] = useState<number | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [scrolled, setScrolled] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const episodesRef = useRef<HTMLElement | null>(null);
  const castRef = useRef<HTMLDivElement | null>(null);
  const episodeRailRef = useRef<HTMLDivElement | null>(null);
  const playMenuRef = useRef<ActionMenuHandle | null>(null);
  const playGestures = useMenuGestures(playMenuRef);
  const dialogRef = useDialogFocus<HTMLElement>();
  // Both rails only exist once the details have loaded.
  useRailDrag(castRef, styles.dragging, Boolean(details?.cast?.length));
  useRailDrag(episodeRailRef, styles.dragging, Boolean(details?.seasons?.length));

  const activeItem = details?.item ?? item;

  useEffect(() => {
    let cancelled = false;
    setDetails(null);
    setError('');
    setSelectedSeason(null);
    setExpanded(false);
    // A similar title swaps this page in place: start it from the top.
    scrollRef.current?.scrollTo({ top: 0 });
    getDetails(item)
      .then((data) => { if (!cancelled) setDetails(data); })
      .catch((err) => { if (!cancelled) setError(err instanceof Error ? err.message : 'Could not load details.'); });
    return () => {
      cancelled = true;
    };
  }, [item, reloadToken]);

  // Once the page scrolls past the hero the whole backdrop frosts (tvOS).
  useEffect(() => {
    const scroller = scrollRef.current;
    if (!scroller) return undefined;
    const onScroll = () => setScrolled(scroller.scrollTop > scroller.clientHeight * 0.45);
    scroller.addEventListener('scroll', onScroll, { passive: true });
    return () => scroller.removeEventListener('scroll', onScroll);
  }, []);

  const seasons = useMemo(() => (details?.seasons ?? []).filter((season) => season.episodes?.length), [details]);
  const isSeries = itemKind(activeItem) === 'series';
  const primary = useMemo(
    () => // A failed load is over too: say episodes are unavailable, not "Loading" forever.
      primaryAction(activeItem, seasons, Boolean(details) || Boolean(error), user.isWatched, user.continueWatching),
    [activeItem, seasons, details, error, user.isWatched, user.continueWatching],
  );
  // The dialog focused its first control while Play was still disabled
  // (a series loads its episodes first): move to Play once it can be pressed.
  const canPlay = Boolean(primary.target);
  const playable = primary.kind !== 'watchlist';
  // Keyed on ownership, not playability: an addon can play a title nobody owns.
  const ownershipNote = playable ? notOnServerNote(activeItem) : NOT_IN_LIBRARY_NOTE;
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!canPlay || !dialog) return;
    const active = document.activeElement;
    const first = dialog.querySelector<HTMLElement>('button:not([disabled])');
    if (active && active !== document.body && active !== first && dialog.contains(active)) return;
    dialog.querySelector<HTMLElement>('[data-autofocus]:not([disabled])')?.focus({ preventScroll: true });
  }, [canPlay, dialogRef]);

  // Open on the season of the episode Play would start.
  const seasonIndex = selectedSeason ?? Math.max(0, seasons.findIndex((entry) => String(entry.season) === String(primary.target?.season)));
  const season = seasons[seasonIndex] ?? seasons[0];
  const overview = asText(activeItem.overview) || asText(activeItem.description);
  // Series badges follow the loaded episode list so they advance as episodes air.
  const badge = useMemo(
    () => (isSeries && seasons.length
      ? seriesReleaseBadge(seasons.flatMap((entry) => entry.episodes ?? []), activeItem.comingSoonDate)
      : releaseBadge(activeItem)),
    [isSeries, seasons, activeItem],
  );
  // Play waits only for a title that isn't out at all: a running show with a
  // new episode on the way still plays the episodes already out.
  const upcoming = badge?.upcoming && badge.kind !== 'episode' ? badge : null;
  const hero = heroBadge(badge, primary.kind);
  const watched = !isSeries && user.isWatched(activeItem);
  const saved = user.isSaved(activeItem);
  const backdrop = itemImage(activeItem, false, fullBleedSize());
  // The frosted copy is blurred 42px, so a tiny image looks identical and
  // decodes in a fraction of the time.
  const frost = itemImage(activeItem, false, 'w300');
  const logo = logoImage(activeItem);
  const [failedArt, setFailedArt] = useState<Record<string, boolean>>({});
  const markFailed = (src: string) => setFailedArt((current) => (current[src] ? current : { ...current, [src]: true }));
  const title = displayTitle(activeItem);
  const trailer = useMemo(() => {
    const list = details?.trailers ?? [];
    return list.find((entry) => entry.official && entry.type === 'trailer') ?? list.find((entry) => entry.type === 'trailer') ?? list[0] ?? null;
  }, [details]);

  const target = primary.target;
  const targetSeason = target?.season != null ? Number(target.season) : null;
  const targetEpisode = target?.episode != null ? Number(target.episode) : null;
  const titleActions = useTitleActions(activeItem, {
    library: false,
    season: isSeries ? targetSeason : null,
    episode: isSeries ? targetEpisode : null,
  });
  // "Start over" when Play would resume (the player auto-resumes otherwise).
  // "Choose Source" is always one step away, whatever the Autoplay setting.
  const chooseSource = target && playable ? [{ label: 'Choose Source', icon: ListFilter, onSelect: () => openSourcePicker(target) }] : [];
  const playActions = primary.resume && target && playable
    ? [{ label: 'Start Over', icon: RotateCcw, onSelect: () => { markStartFromBeginning(target); openPlayer(target); } }, ...chooseSource, ...titleActions]
    : [...chooseSource, ...titleActions];

  // Warm Play: resolve sources and inspect the top candidate while Details is open.
  const prefetchKey = target ? `${target.media_type}:${target.tmdb_id ?? target.imdb_id}:${target.season ?? ''}:${target.episode ?? ''}` : '';
  useEffect(() => {
    if (!target || upcoming || !playable) return undefined;
    // Where Play will start, so the desktop app's engine can prepare exactly that.
    const serverEntry = user.continueWatching.find((entry) => playbackProgressKey(entry) === playbackProgressKey(target)) ?? null;
    return prefetchPlayback(target, {
      tier: defaultSourceTier(),
      profileId: user.profileId,
      start: resumeStartFor(target, serverEntry),
      audioLanguage: getPreferences().audioLanguage,
    });
  }, [prefetchKey, playable, user.profileId]); // eslint-disable-line react-hooks/exhaustive-deps

  const certification = details?.content_rating ?? activeItem.content_rating ?? '';
  const rottenTomatoes = useMemo(() => (details?.ratings ?? []).flatMap((rating) => {
    const source = String(rating.source).toLowerCase();
    const value = rating.value != null ? `${Math.round(rating.value)}%` : '';
    if (!value) return [];
    if (source === 'tomatoes') return [{ glyph: '🍅', label: 'Rotten Tomatoes critics', value }];
    if (source === 'popcorn' || source === 'tomatoesaudience') return [{ glyph: '🍿', label: 'Rotten Tomatoes audience', value }];
    return [];
  }), [details]);
  const facts = useMemo(() => {
    // tvOS heroSeriesContextText: what Play will do, else the season count.
    const seasonCount = seasons.length ? `${seasons.length} Season${seasons.length === 1 ? '' : 's'}` : '';
    const context = !isSeries
      ? runtimeLabel(activeItem)
      : primary.kind === 'resume' && primary.target ? episodeCode(primary.target)
        : primary.kind === 'next' && primary.target ? `Next: ${episodeCode(primary.target)}`
          : seasonCount;
    return [
      activeItem.year ? String(activeItem.year) : '',
      context,
      ...(activeItem.genres ?? []).slice(0, 2),
    ].filter(Boolean);
  }, [activeItem, isSeries, seasons.length, primary.kind, primary.target]);
  const starring = useMemo(() => {
    const names = (details?.cast ?? []).slice(0, 3).map((person) => person.name).filter(Boolean);
    if (names.length) return `Starring ${names.join(', ')}`;
    const directors = isSeries ? details?.crew?.creators : details?.crew?.directors;
    return directors?.length ? `${isSeries ? 'Created by' : 'Directed by'} ${directors.slice(0, 2).join(', ')}` : '';
  }, [details, isSeries]);
  const targetEpisodeInfo = useMemo(() => {
    if (!isSeries || targetSeason == null || targetEpisode == null) return null;
    return seasons.flatMap((entry) => entry.episodes ?? []).find(
      (episode) => Number(episode.season) === targetSeason && Number(episode.episode) === targetEpisode,
    ) ?? null;
  }, [isSeries, seasons, targetSeason, targetEpisode]);

  const toggleSaved = () => {
    user.toggleSaved(activeItem).catch(() => showToast(saved ? 'Could not remove from Watchlist.' : 'Could not add to Watchlist.'));
  };

  const showEpisodes = () => {
    const section = episodesRef.current;
    if (!section) return;
    section.scrollIntoView({ behavior: 'smooth', block: 'start' });
    section.querySelector<HTMLElement>('[data-nav-item][tabindex="0"], [data-nav-item]')?.focus({ preventScroll: true });
  };

  const infoRows = useMemo(() => detailInfoRows({
    isSeries,
    release: activeItem.year,
    certification,
    ageRating: details?.content_advisory?.age_rating,
    status: details?.status,
    networks: details?.networks,
    coming: badge,
    digitalReleaseInfo: activeItem.digitalReleaseInfo,
    runtime: runtimeLabel(activeItem),
    genres: activeItem.genres,
    directors: details?.crew?.directors,
    creators: details?.crew?.creators,
    writers: details?.crew?.writers,
    language: languageName(details?.original_language),
    countries: details?.countries,
    studios: details?.studios,
    themes: (details?.tags ?? []).map((tag) => tag.name),
  }), [details, activeItem, isSeries, certification, badge]);

  const loadFailed = Boolean(error) && !details;
  const resume = playable && primary.resume && primary.resume.duration > 0 ? primary.resume : null;

  return (
    <section
      ref={dialogRef}
      className={styles.overlay}
      role="dialog"
      aria-modal="true"
      aria-label={`${title} details`}
      data-nav-scope={`detail:${activeItem.media_type}:${activeItem.tmdb_id ?? activeItem.imdb_id ?? title}`}
    >
      <div className={styles.backdrop} aria-hidden="true">
        {backdrop && !failedArt[backdrop] ? (
          <img className={styles.backdropArt} src={backdrop} alt="" decoding="async" onError={() => markFailed(backdrop)} />
        ) : null}
        {frost && !failedArt[frost] ? (
          <>
            <img className={styles.frost} src={frost} alt="" decoding="async" onError={() => markFailed(frost)} />
            <img className={`${styles.frost} ${styles.frostCover} ${scrolled ? styles.frostFull : ''}`} src={frost} alt="" decoding="async" />
          </>
        ) : null}
        <div className={styles.shade} />
      </div>

      <div className={styles.topBar} data-nav-row="detail-top" data-nav-memory="off">
        <button className={styles.back} type="button" aria-label="Back" onClick={closeDetails} data-nav-item data-own-focus>
          <ChevronLeft size={26} />
        </button>
      </div>

      <div className={styles.scroll} ref={scrollRef} data-nav-scroll data-nav-clearance="104">
        <header className={styles.hero}>
          <div className={styles.heroContent}>
            {hero ? (
              <span className={`${styles.badge} ${hero.prominent ? styles.badgeProminent : ''}`}>{hero.text}</span>
            ) : null}

            {logo && !failedArt[logo] ? (
              <img className={styles.logo} src={logo} alt={title} decoding="async" onError={() => markFailed(logo)} />
            ) : (
              <h1 className={styles.title}>{title}</h1>
            )}

            <div className={styles.facts}>
              {/* A finished movie reads as watched at a glance (native "✓ Watched" first chip). */}
              {watched ? <span className={`${styles.fact} ${styles.watchedFact}`}><CircleCheck size={16} aria-hidden="true" />Watched</span> : null}
              {certification ? <span className={styles.certification}>{certification}</span> : null}
              {/* tvOS HeroRottenTomatoesView: critics and audience beside the certification. */}
              {rottenTomatoes.map((meter) => (
                <span key={meter.label} className={styles.fact} aria-label={`${meter.label} ${meter.value}`}>{`${meter.glyph} ${meter.value}`}</span>
              ))}
              {facts.map((part, index) => (
                <span key={`${part}:${index}`} className={styles.fact}>{part}</span>
              ))}
            </div>

            {targetEpisodeInfo ? (
              <p className={styles.episodeHeading}>
                {`${episodeCode(targetEpisodeInfo)} • ${displayTitle(targetEpisodeInfo)}`}
              </p>
            ) : null}

            {overview ? (
              <div className={styles.synopsis}>
                <p className={expanded ? undefined : styles.clamped}>{overview}</p>
                {/* Outside the clamped text, which would cut the button off. */}
                {overview.length > 260 ? (
                  <button className={styles.moreButton} type="button" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
                    {expanded ? 'Less' : 'More'}
                  </button>
                ) : null}
              </div>
            ) : null}

            {resume ? (
              <div className={styles.resume}>
                <span className={styles.resumeBar} aria-hidden="true">
                  <span style={{ width: `${clamp(resume.position / resume.duration, 0, 1) * 100}%` }} />
                </span>
                <span>{remainingTimeLabel(resume.duration - resume.position) || 'Pick up where you left off'}</span>
              </div>
            ) : null}

            <div className={styles.pills} data-nav-row="detail-actions" data-nav-memory="off">
              {!playable ? (
                <button
                  className={`${styles.pill} ${styles.pillPrimary}`}
                  type="button"
                  aria-pressed={saved}
                  onClick={toggleSaved}
                  data-autofocus
                  data-nav-default
                  data-nav-item
                  data-own-focus
                >
                  {saved ? <BookmarkCheck size={20} aria-hidden="true" /> : <Bookmark size={20} aria-hidden="true" />}
                  {saved ? 'Saved' : 'Watchlist'}
                </button>
              ) : upcoming ? (
                <button className={`${styles.pill} ${styles.pillPrimary}`} type="button" disabled data-nav-item data-own-focus>
                  {upcoming.chipText}
                </button>
              ) : (
                <button
                  className={`${styles.pill} ${styles.pillPrimary}`}
                  type="button"
                  disabled={!target}
                  data-autofocus
                  data-nav-default
                  data-nav-item
                  data-own-focus
                  onClick={() => {
                    if (!playGestures.consumeClick() && target) openPlayer(target);
                  }}
                  {...playGestures.props}
                >
                  {target ? <Play size={20} fill="currentColor" aria-hidden="true" /> : null}
                  {primary.label}
                </button>
              )}
              {isSeries && seasons.length ? (
                <button className={styles.pill} type="button" onClick={showEpisodes} data-nav-item data-own-focus>
                  <ListVideo size={20} aria-hidden="true" />
                  Episodes
                </button>
              ) : null}
              {trailer ? (
                <button
                  className={styles.pill}
                  type="button"
                  data-nav-item
                  data-own-focus
                  onClick={() => window.open(trailer.url, '_blank', 'noopener,noreferrer')}
                >
                  <Clapperboard size={20} aria-hidden="true" />
                  Trailer
                </button>
              ) : null}
              {playable ? (
                <button
                  className={styles.pill}
                  type="button"
                  aria-pressed={saved}
                  onClick={toggleSaved}
                  data-nav-item
                  data-own-focus
                >
                  {saved ? <BookmarkCheck size={20} aria-hidden="true" /> : <Bookmark size={20} aria-hidden="true" />}
                  {saved ? 'Saved' : 'Watchlist'}
                </button>
              ) : null}
              {!upcoming && target ? (
                <ActionMenu ref={playMenuRef} actions={playActions} label="More playback actions" className={styles.menuPill} navItem />
              ) : null}
            </div>
            {ownershipNote ? <p className={styles.availabilityNote}>{ownershipNote}</p> : null}
            {activeItem.reason ? <p className={styles.availabilityNote}>{activeItem.reason}</p> : null}

            {starring ? <p className={styles.starring}>{starring}</p> : null}

            {loadFailed ? (
              <div className={styles.failure} role="alert">
                <strong>Details unavailable</strong>
                <span>{error}</span>
                <span data-nav-row="detail-retry" data-nav-memory="off"><button className={styles.pill} type="button" data-nav-item onClick={() => setReloadToken((value) => value + 1)}>Try again</button></span>
              </div>
            ) : null}
          </div>
        </header>

        <div className={styles.sections}>
          {isSeries && details ? (
            <section className={styles.section} ref={episodesRef} aria-label="Episodes">
              <div className={styles.sectionHead}>
                <h2 className={styles.sectionTitle}>Episodes</h2>
                {seasons.length > 1 ? (
                  <SeasonPicker seasons={seasons} selected={seasonIndex} onSelect={setSelectedSeason} />
                ) : seasons.length === 1 ? (
                  <span className={styles.seasonStatic}>{seasons[0].title || seasons[0].name || `Season ${seasons[0].season}`}</span>
                ) : null}
                {seasons.length ? <RailButtons railRef={episodeRailRef} watch={season} /> : null}
              </div>
              {seasons.length ? (
                <div className={styles.episodeRail} ref={episodeRailRef} data-nav-row="episodes" data-nav-rail>
                  {(season?.episodes ?? []).filter(playableEpisode).map((episode) => (
                    <EpisodeCard
                      key={`${episode.season}:${episode.episode}`}
                      series={activeItem}
                      episode={episode}
                      isTarget={String(episode.season) === String(target?.season) && String(episode.episode) === String(target?.episode)}
                      targetKind={primary.kind}
                      onPlay={playable ? () => openPlayer(episodePlayItem(activeItem, episode)) : undefined}
                    />
                  ))}
                </div>
              ) : (
                <div className={styles.inlineFailure}>
                  <strong>Episodes unavailable</strong>
                  <span>Curatio couldn&rsquo;t load this show&rsquo;s episode list. Retry to refresh its series details.</span>
                  <span data-nav-row="detail-retry" data-nav-memory="off"><button className={styles.pill} type="button" data-nav-item onClick={() => setReloadToken((value) => value + 1)}>Retry</button></span>
                </div>
              )}
            </section>
          ) : null}

          {details?.cast?.length ? (
            <section className={styles.section} aria-label="Cast">
              <h2 className={styles.sectionTitle}>Cast</h2>
              <div className={styles.castRail} ref={castRef}>
                {details.cast.slice(0, 15).map((person) => (
                  <CastCard key={`${person.name}:${person.character}`} person={person} />
                ))}
              </div>
            </section>
          ) : null}

          {details?.similar?.length ? (
            <div className={styles.section}>
              <ShelfRow
                rowKey="similar"
                title={details.similar_source === 'tags' ? 'More Like This' : 'You May Also Like'}
                items={details.similar}
                variant="poster"
              />
            </div>
          ) : null}

          {overview || details?.tagline || details?.tags?.length ? (
            <section className={`${styles.section} ${styles.infoCard}`} aria-label="About">
              <h2 className={styles.sectionTitle}>About</h2>
              {details?.tagline ? <p className={styles.tagline}>{details.tagline}</p> : null}
              {overview ? <p className={styles.about}>{overview}</p> : null}
              {details?.tags?.length ? (
                <div data-nav-row="detail-tags">
                  <TagChips tags={details.tags} onSelect={(tag) => openBrowse(tag, isSeries ? 'tv' : 'movie')} />
                </div>
              ) : null}
            </section>
          ) : null}

          {details?.ratings?.length ? (
            <section className={`${styles.section} ${styles.infoCard}`} aria-label="Scores">
              <h2 className={styles.sectionTitle}>Scores</h2>
              <RatingRow ratings={orderedScores(details.ratings)} />
              {details.review_summary?.average_user_rating != null && details.review_summary.rated_review_count > 0 ? (
                <p className={styles.reviewers}>
                  Reviewers · {details.review_summary.average_user_rating.toFixed(1)}/10 from {details.review_summary.rated_review_count} review{details.review_summary.rated_review_count === 1 ? '' : 's'}
                </p>
              ) : null}
            </section>
          ) : null}

          {infoRows.length ? (
            <section className={`${styles.section} ${styles.infoCard}`} aria-label="Details">
              <h2 className={styles.sectionTitle}>Details</h2>
              <dl className={styles.infoGrid}>
                {infoRows.map(([label, value]) => (
                  <div key={label} className={styles.infoRow}>
                    <dt>{label}</dt>
                    <dd>{value}</dd>
                  </div>
                ))}
              </dl>
            </section>
          ) : null}
        </div>
      </div>
    </section>
  );
}

const LANGUAGE_NAMES: Record<string, string> = {
  en: 'English', es: 'Spanish', fr: 'French', de: 'German', it: 'Italian', ja: 'Japanese', ko: 'Korean',
  zh: 'Chinese', pt: 'Portuguese', ru: 'Russian', hi: 'Hindi', sv: 'Swedish', da: 'Danish', no: 'Norwegian',
  nl: 'Dutch', pl: 'Polish', tr: 'Turkish', ar: 'Arabic', th: 'Thai', fi: 'Finnish', he: 'Hebrew', id: 'Indonesian',
};

function languageName(code?: string | null) {
  if (!code) return '';
  return LANGUAGE_NAMES[code.toLowerCase()] ?? code.toUpperCase();
}

/** Previous / next page buttons for a rail, shown only while there is more that way. */
function RailButtons({ railRef, watch }: { railRef: React.RefObject<HTMLDivElement>; watch: unknown }) {
  const [can, setCan] = useState({ left: false, right: false });
  useEffect(() => {
    const rail = railRef.current;
    if (!rail) return undefined;
    const update = () => {
      const max = rail.scrollWidth - rail.clientWidth;
      const next = { left: rail.scrollLeft > 4, right: rail.scrollLeft < max - 4 };
      setCan((current) => (current.left === next.left && current.right === next.right ? current : next));
    };
    update();
    rail.addEventListener('scroll', update, { passive: true });
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update);
    observer?.observe(rail);
    return () => {
      rail.removeEventListener('scroll', update);
      observer?.disconnect();
    };
  }, [railRef, watch]);
  if (!can.left && !can.right) return null;
  const page = (direction: number) => {
    const rail = railRef.current;
    rail?.scrollBy({ left: direction * rail.clientWidth * 0.85, behavior: 'smooth' });
  };
  // Mouse-only helpers: arrow keys already walk the rail.
  return (
    <div className={styles.railButtons}>
      <button type="button" tabIndex={-1} disabled={!can.left} aria-label="Scroll back" onClick={() => page(-1)}><ChevronLeft size={20} /></button>
      <button type="button" tabIndex={-1} disabled={!can.right} aria-label="Scroll forward" onClick={() => page(1)}><ChevronRight size={20} /></button>
    </div>
  );
}

function SeasonPicker({ seasons, selected, onSelect }: { seasons: SeasonItem[]; selected: number; onSelect: (index: number) => void }) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const label = (entry?: SeasonItem) => entry?.title || entry?.name || `Season ${entry?.season ?? ''}`;

  useEffect(() => {
    if (!open) return undefined;
    const onPointer = (event: PointerEvent) => {
      if (!wrapRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      // Consume it so the overlay stack doesn't also go back.
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
      wrapRef.current?.querySelector<HTMLElement>('[aria-haspopup]')?.focus({ preventScroll: true });
    };
    window.addEventListener('pointerdown', onPointer, true);
    window.addEventListener('keydown', onKey, true);
    const current = wrapRef.current?.querySelector<HTMLElement>('[aria-selected="true"]');
    current?.scrollIntoView({ block: 'nearest' });
    current?.focus({ preventScroll: true });
    return () => {
      window.removeEventListener('pointerdown', onPointer, true);
      window.removeEventListener('keydown', onKey, true);
    };
  }, [open]);

  return (
    <div className={styles.seasonPicker} ref={wrapRef} data-nav-row="season" data-nav-memory="off">
      <button
        className={styles.seasonButton}
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        data-nav-item
        data-own-focus
        onClick={() => setOpen((value) => !value)}
      >
        {label(seasons[selected])}
        <ChevronDown size={15} />
      </button>
      {open ? (
        <div
          className={styles.seasonList}
          role="listbox"
          aria-label="Season"
          data-nav-layer="modal"
          data-nav-mode="native"
          onKeyDown={(event) => {
            if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
            event.preventDefault();
            const options = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('[role="option"]'));
            const index = options.indexOf(document.activeElement as HTMLElement);
            const next = options[Math.min(Math.max(index + (event.key === 'ArrowDown' ? 1 : -1), 0), options.length - 1)];
            next?.focus();
          }}
        >
          {seasons.map((entry, index) => (
            <button
              key={`${entry.season}:${entry.title}`}
              type="button"
              role="option"
              aria-selected={index === selected}
              onClick={() => {
                onSelect(index);
                setOpen(false);
                wrapRef.current?.querySelector<HTMLElement>('[aria-haspopup="listbox"]')?.focus();
              }}
            >
              <span>{label(entry)}</span>
              {index === selected ? <Check size={15} /> : null}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function EpisodeCard({ series, episode, isTarget, targetKind, onPlay }: {
  series: WebMediaItem;
  episode: EpisodeItem;
  isTarget: boolean;
  targetKind: PrimaryKind;
  /** Omitted for a recommendation-only title: the card shows the episode but never plays it. */
  onPlay?: () => void;
}) {
  const { isWatched, continueWatching } = useUserState();
  const menuRef = useRef<ActionMenuHandle | null>(null);
  const gestures = useMenuGestures(menuRef);
  const seasonNumber = episode.season != null ? Number(episode.season) : null;
  const episodeNumber = episode.episode != null ? Number(episode.episode) : null;
  const actions = useTitleActions(series, { library: false, season: seasonNumber, episode: episodeNumber });
  const still = safeUrl(episode.still) || itemImage(episode, false);
  const [stillFailed, setStillFailed] = useState(false);
  const playItem = episodePlayItem(series, episode);
  const info: ResumeInfo | null = resumeInfoFor(playItem);
  // This device's position, else another device's (Continue Watching), else the item's own.
  const elsewhere = info ? null : continueWatching.find((entry) => playbackProgressKey(entry) === playbackProgressKey(playItem));
  const fraction = info && info.duration > 0 ? info.position / info.duration : progressFraction(elsewhere ?? episode);
  const watched = isWatched(series, episode.season, episode.episode);
  const title = displayTitle(episode);
  const comingText = episodeComingText(episode);
  const marker = episodeMarker({ comingText, watched, fraction, isTarget, targetKind });
  // Unaired: dimmed and not playable (it has no sources yet), as on tvOS.
  const unavailable = marker.kind === 'comingSoon';
  const markerLabel = 'label' in marker ? marker.label : '';
  const eyebrow = [`S${episode.season ?? ''} · E${episode.episode ?? ''}`, episode.runtime ? `${episode.runtime}m` : ''].filter(Boolean).join(' · ');
  return (
    <div className={styles.episodeCell}>
      <button
        className={`${styles.episode} ${unavailable ? styles.episodeUnavailable : ''}`}
        type="button"
        aria-disabled={unavailable || undefined}
        aria-label={`S${episode.season} E${episode.episode}: ${title}${watched ? ', watched' : ''}${unavailable ? `, ${markerLabel.toLowerCase()}` : ''}`}
        data-nav-item
        data-own-focus
        onKeyDown={(event) => {
          if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
            event.preventDefault();
            menuRef.current?.open();
          }
        }}
        onClick={() => {
          if (!gestures.consumeClick() && !unavailable) onPlay?.();
        }}
        {...gestures.props}
      >
        <span className={styles.episodeArt}>
          {still && !stillFailed ? (
            <img src={still} alt="" loading="lazy" decoding="async" onError={() => setStillFailed(true)} />
          ) : (
            <span className={styles.episodeArtFallback}>{`E${episode.episode ?? ''}`}</span>
          )}
          {markerLabel ? <span className={styles.episodeMarker}>{markerLabel}</span> : null}
          {watched ? <span className={styles.episodeWatched}><CircleCheck size={22} /></span> : null}
          {fraction > 0 && !watched ? <span className={styles.episodeProgress}><span style={{ width: `${clamp(fraction, 0, 1) * 100}%` }} /></span> : null}
        </span>
        <span className={styles.episodeText}>
          <small>{eyebrow}</small>
          <strong>{title}</strong>
          <span>{asText(episode.overview) || asText(episode.description)}</span>
        </span>
      </button>
      <ActionMenu ref={menuRef} actions={actions} label={`More actions for S${episode.season} E${episode.episode}`} className={styles.episodeMenu} />
    </div>
  );
}

function CastCard({ person }: { person: CastMember }) {
  const name = person.name ?? '';
  const profile = safeUrl(person.profile);
  const [bg, tint] = castColor(name);
  const [photoFailed, setPhotoFailed] = useState(false);
  return (
    <div className={styles.castCard}>
      <span className={styles.castPhoto} style={{ background: bg, color: tint }}>
        {profile && !photoFailed ? (
          <img src={profile} alt="" loading="lazy" decoding="async" onError={() => setPhotoFailed(true)} />
        ) : (
          detailInitials(name)
        )}
      </span>
      <span className={styles.castText}>
        <strong>{name}</strong>
        <small>{person.character}</small>
      </span>
    </div>
  );
}
