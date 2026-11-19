import { hasLocalEngine, playbackTrace } from '../api/desktopBridge';
import type Hls from 'hls.js';
import {
  Captions,
  CheckCircle,
  Circle,
  Copy,
  Expand,
  Gauge,
  Library,
  ListVideo,
  Maximize,
  Minimize,
  Monitor,
  MoreHorizontal,
  Pause,
  PictureInPicture2,
  Play,
  RotateCcw,
  RotateCw,
  TriangleAlert,
  Shrink,
  SkipForward,
  Volume2,
  VolumeX,
  X,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';
import type { ChangeEvent, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from 'react';
import {
  confirmActiveSource,
  probePlaybackSources,
  RemuxBusyError,
  RemuxHttpError,
  RemuxUnsupportedError,
  reportStreamFailure,
  sendSegmentFeedback,
  startRemuxSession,
  stopRemuxSession,
} from '../api/accountClient';
import { forgetMediaFacts, peekMediaFacts } from '../api/mediaFacts';
import {
  clamp,
  displayTitle,
  episodeLabel,
  episodePlayItem,
  formatTime,
  itemImage,
  logoImage,
  playbackId,
  playbackProgressKey,
  runtimeSecondsOf,
  streamUrl,
  mediaType,
} from '../api/mediaUtils';
import {
  findRenewedRoute,
  invalidatePlaybackSources,
  loadPlaybackSources,
  type SourceSelection,
  type SourceTier,
} from '../api/playbackSources';
import { defaultSourceTier, getPreferences, trackMatchesLanguage } from '../api/preferences';
import { isOpaqueRoute, providerIdentity, providerLabel, routeIdentity, skipsSpeculativeProbe, startupTimeoutMs } from '../api/streamMeta';
import type { EpisodeItem, FailureCategory, MediaAudioFacts, PlaybackQuality, PlaybackSource, ProbeVerdict, SeasonItem, WebMediaItem } from '../api/types';
import { useOptionalUserState } from '../api/userState';
import { SourceRow } from '../components/SourceRow';
import {
  initialMachineState,
  isCompletion,
  isHeldWhilePaused,
  isLoadingPhase,
  LONG_PAUSE_MS,
  LONG_PAUSE_PROBE_MS,
  loadingUi,
  playbackReducer,
  RebufferCounter,
  ChronicBufferingTracker,
  isStruggling,
  STILL_WORKING_LABEL,
  STRUGGLING_LABEL,
  SEEK_GRACE_MS,
  shouldReloadAfterLongPause,
  StallWatchdog,
  type MachineEvent,
  type MachineState,
} from './playbackMachine';
import {
  disableDecodeScope,
  inspectCandidates,
  orderByPlayability,
  planForStream,
  prefetchPlayback,
  sourceRequestHeaders,
  type PlanKind,
  type PlaybackPlan,
} from './playbackPlan';
import { PlaybackPresenceReporter } from './playbackPresence';
import {
  applySeek,
  attachNativeTrackListeners,
  cleanupPlayback,
  effectiveDuration,
  fileAudioOptions,
  isSameOriginUrl,
  languageLabel,
  loadStreamIntoVideo,
  markersFromSegments,
  markStartFromBeginning,
  nextEpisodeAcceptFeedback,
  preparePlaybackMetadata,
  refreshPlaybackSegments,
  refreshTrackState,
  resumeStartFor,
  selectAudioTrack,
  selectSubtitleTrack,
  shouldShowNextEpisode,
  toggleFullscreen,
  togglePlay,
  updateMediaSession,
  updatePlaybackTimeline,
  clearSavedPlaybackState,
  SEEK_STEP_SECONDS,
  SEEK_REPEAT_WINDOW_MS,
  directionalSeekStep,
  UP_NEXT_COUNTDOWN_SECONDS,
  type PlayerMarkers,
  type SettingsTab,
  type TrackOption,
} from './playerEngine';
import { ProgressReporter } from './progressReporter';
import {
  classifyFailure,
  decideRecovery,
  INCIDENT_HEALTHY_MS,
  playsSilently,
  recordAction,
  RecoveryLedger,
  type ClassifiedFailure,
  type FailureSignal,
  type RecoveryAction,
} from './recovery';
import { SessionKeepalive, SidecarSubtitles } from './serverSession';
import { mergeRecoveryStreams, rankSources, selectNextSource, shouldPreferLighterSource } from './sourceFailover';
import { isProviderUnavailable, probeTargets, selectInitialSource } from './sourceReadiness';
import styles from './Player.module.css';

// ---------------------------------------------------------------------------
// The web player. Responsibilities are split so the lifecycle stays readable:
//   playbackPlan.ts     which path a source takes (direct / HLS / server-prepared)
//   playbackMachine.ts  phases, loading-UI timing, end-of-file rule, watchdogs
//   recovery.ts         error taxonomy + recovery ladder and budgets
//   playerEngine.ts     the three engines (<video>, hls.js, native HLS) + tracks
// This component owns the React state, the single failure entry point
// (`handleSourceFailure`) and executes the ladder's actions.
// ---------------------------------------------------------------------------

function itemRuntimeSeconds(item: WebMediaItem): number {
  const value = Number(item.duration ?? item.runtime_seconds ?? item.runtimeSeconds ?? 0);
  return Number.isFinite(value) && value > 0 ? value : 0;
}

const VOLUME_KEY = 'curatio-player-volume';
const CHROME_HIDE_MS = 3000;
const STILL_WATCHING_AFTER = 3;
const NEXT_PREFETCH_LEAD_S = 120;
const SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 2] as const;
/** Server prunes segments more than 10 min behind the newest one requested. */
const SERVER_BACK_WINDOW_S = 9 * 60;

// Initial-load fast-fail ("signal of life"): generous on the first source and on
// a resume seek, quicker on later sources, with an absolute ceiling.
const INITIAL_FAILFAST_FIRST_MS = 10000;
const INITIAL_FAILFAST_LATER_MS = 6000;
const RESUME_SEEK_FAILFAST_MS = 15000;
/** Longest a new server session waits for the previous one's stop request. */
const SESSION_STOP_WAIT_MS = 3000;
const DESKTOP_APP_MESSAGE = "None of the available sources can play in this browser without server transcoding. Play in the desktop app.";
const FAILFAST_CEILING_MS = 45000;
/** Longest wait for server facts on a cold start before using filename rules. */
const COLD_INSPECT_WAIT_MS = 3500;

// Consecutive auto-advanced episodes with no control interaction (Still watching?).
let autoAdvanceStreak = 0;

function getStoredVolume(): { volume: number; muted: boolean } {
  try {
    const raw = localStorage.getItem(VOLUME_KEY);
    if (!raw) return { volume: 1, muted: false };
    const data = JSON.parse(raw) as { volume?: number; muted?: boolean };
    const volume = Number(data.volume);
    return { volume: Number.isFinite(volume) ? clamp(volume, 0, 1) : 1, muted: Boolean(data.muted) };
  } catch {
    return { volume: 1, muted: false };
  }
}

function setStoredVolume(volume: number, muted: boolean) {
  try {
    localStorage.setItem(VOLUME_KEY, JSON.stringify({ volume, muted }));
  } catch {
    // Convenience only.
  }
}

function BufferRing() {
  return (
    <svg className={styles.bufferRing} viewBox="0 0 50 50" width={64} height={64} aria-hidden="true">
      <circle className={styles.bufferRingTrack} cx="25" cy="25" r="21" />
      <circle className={styles.bufferRingArc} cx="25" cy="25" r="21" />
    </svg>
  );
}

// Intro/credits fingerprinting is keyed by IMDb id + season + episode.
function episodeFingerprintContext(item: WebMediaItem): { imdb_id: string; season: number; episode: number } | null {
  if (mediaType(item) !== 'series') return null;
  const imdb = typeof item.imdb_id === 'string' && item.imdb_id.startsWith('tt') ? item.imdb_id : null;
  const season = Number(item.season);
  const episode = Number(item.episode);
  if (!imdb || !Number.isFinite(season) || !Number.isFinite(episode)) return null;
  return { imdb_id: imdb, season, episode };
}

function pillLabel(track: TrackOption, all: TrackOption[]) {
  const duplicated = all.filter((entry) => entry.label === track.label).length > 1;
  return duplicated && track.detail ? `${track.label} ${track.detail}` : track.label;
}

/** The server's failure categories (docs/playback-api.md). */
export function failureCategory(classified: ClassifiedFailure): FailureCategory {
  if (classified.signal.kind === 'startup-timeout') return 'timeout';
  switch (classified.category) {
    case 'DECODE':
    case 'SOURCE': return 'decode';
    case 'NET': return 'network';
    case 'EXPIRED':
    case 'AUTH': return 'auth';
    case 'GONE': return 'not_found';
    default: return 'other';
  }
}

/** Presence still reports the profile tier: Optimized is `low`, Original is `high`. */
function presenceTier(tier: SourceTier): 'low' | 'high' | undefined {
  return tier === 'optimized' ? 'low' : tier === 'original' ? 'high' : undefined;
}

function bufferedEnd(video: HTMLVideoElement | null): number {
  try {
    return video && video.buffered.length ? video.buffered.end(video.buffered.length - 1) : 0;
  } catch {
    return 0;
  }
}

function seekableCovers(video: HTMLVideoElement, time: number): boolean {
  try {
    for (let i = 0; i < video.seekable.length; i += 1) {
      // No slack past the end: a position beyond what is prepared makes hls.js
      // snap playback back to its "live edge"; that case starts a new session.
      if (time >= video.seekable.start(i) - 0.5 && time <= video.seekable.end(i) - 0.5) return true;
    }
  } catch {
    return false;
  }
  return false;
}

/** One attempt to put a source on screen. */
interface Attempt {
  token: number;
  itemKey: string;
  index: number;
  /** Movie time to start at. */
  position: number;
  path: PlanKind;
  viaProxy: boolean;
  autoplay: boolean;
  reason: string;
}

/** Mutable per-attempt runtime facts (read by event handlers and timers). */
interface LiveAttempt {
  token: number;
  index: number;
  path: PlanKind;
  viaProxy: boolean;
  sawSignal: boolean;
  playingSeen: boolean;
  hadPlayed: boolean;
  failed: boolean;
  firstFrame: boolean;
  /** Movie time of the element's local 0 (non-zero for a prepared session). */
  base: number;
  session: string | null;
  maxLocal: number;
  startPosition: number;
  framesChecked: boolean;
  audioChecked: boolean;
  /** A prepared session made on this computer (desktop engine), not by the server. */
  local: boolean;
  startedAt: number;
}

function idleLive(): LiveAttempt {
  return {
    token: 0, index: 0, path: 'direct', viaProxy: false, sawSignal: false, playingSeen: false, hadPlayed: false,
    failed: false, firstFrame: false, base: 0, session: null, maxLocal: 0, startPosition: 0, framesChecked: false, audioChecked: false, local: false, startedAt: 0,
  };
}

/** Per-item playback session bookkeeping. */
interface PlaybackSession {
  itemKey: string;
  ledger: RecoveryLedger;
  /** Index → paths that failed for it (a path change makes it eligible again). */
  triedPaths: Map<number, Set<PlanKind>>;
  failureReasons: Map<number, string>;
  verdicts: Map<string, ProbeVerdict>;
  planOverride: Map<number, PlanKind>;
  lastGood: number;
  attempts: number;
  healthySince: number | null;
  discovery: AbortController | null;
  recovery: AbortController | null;
  retryTimer: number | null;
  /** The server refused a transcode-only source and pointed at the desktop app. */
  desktopHint: boolean;
  /** Providers whose probe said the provider itself is down (native excludedProviders). */
  excludedProviders: Set<string>;
  /** Why the last automatic failover happened, until 60 s of steady playback (native). */
  lastFailoverReason: string | null;
  /** The backups behind the playing source were already probed (native beginPreflighting). */
  backupsProbed: boolean;
}

function newSession(itemKey: string): PlaybackSession {
  return {
    itemKey, ledger: new RecoveryLedger(), triedPaths: new Map(), failureReasons: new Map(), verdicts: new Map(),
    planOverride: new Map(), lastGood: 0, attempts: 0, healthySince: null, discovery: null, recovery: null, retryTimer: null,
    desktopHint: false, excludedProviders: new Set(), lastFailoverReason: null, backupsProbed: false,
  };
}

/** Native `retainedFailoverReason`: steady playback this long makes the last failure history. */
const FAILOVER_REASON_MEMORY_MS = 60_000;

/** The native failure family behind a failover, for `shouldPreferLighterSource`. */
function failoverReason(classified: ClassifiedFailure): string {
  return classified.signal.kind === 'rebuffer' ? 'rebuffer' : classified.category === 'DECODE' ? 'decode' : classified.category.toLowerCase();
}

/** Network, server and file failures: the same file elsewhere is the best bet. Never bandwidth (native). */
function prefersSameFile(classified: ClassifiedFailure): boolean {
  if (classified.signal.kind === 'rebuffer') return false;
  return classified.category === 'NET' || classified.category === 'EXPIRED' || classified.category === 'GONE';
}

export function Player({
  item,
  profileId,
  selection,
  onClose,
  onPlay,
  onChooseSource,
}: {
  item: WebMediaItem | null;
  profileId?: number;
  selection?: SourceSelection;
  onClose: () => void;
  onPlay: (item: WebMediaItem) => void;
  onChooseSource?: (item: WebMediaItem) => void;
}) {
  const userState = useOptionalUserState();
  const playerRef = useRef<HTMLElement | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const hlsRef = useRef<Hls | null>(null);
  const itemRef = useRef(item);
  itemRef.current = item;
  const itemKey = item ? playbackProgressKey(item) : '';
  const selectionRef = useRef<SourceSelection | undefined>(selection);

  const [machine, dispatchMachine] = useReducer(
    (state: MachineState, event: MachineEvent) => playbackReducer(state, event, Date.now()),
    undefined,
    () => initialMachineState(Date.now()),
  );
  const machineRef = useRef(machine);
  machineRef.current = machine;
  const dispatch = dispatchMachine;

  const [streams, setStreamsState] = useState<PlaybackSource[]>([]);
  const streamsRef = useRef<PlaybackSource[]>([]);
  const setStreams = useCallback((next: PlaybackSource[]) => {
    streamsRef.current = next;
    setStreamsState(next);
  }, []);
  const [sourceIndex, setSourceIndex] = useState(0);
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [tier, setTier] = useState<SourceTier>(selection?.tier ?? defaultSourceTier());
  /** The quality the server resolved for this title (the profile default for `default`). */
  const [resolvedQuality, setResolvedQuality] = useState<PlaybackQuality | null>(null);
  const tierRef = useRef(tier);
  tierRef.current = tier;
  const [lookupToken, setLookupToken] = useState(0);

  const tokenRef = useRef(0);
  const liveRef = useRef<LiveAttempt>(idleLive());
  const sessionRef = useRef<PlaybackSession>(newSession(itemKey));
  const knownDurationRef = useRef(0);
  /** Whether `knownDurationRef` is the probed length of the playing file (not a runtime guess). */
  const durationProbedRef = useRef(false);
  const startupTimerRef = useRef<number | null>(null);
  const keepaliveRef = useRef<SessionKeepalive | null>(null);
  /** The last remux stop request; the next session waits for it so two never overlap. */
  const pendingStopRef = useRef<Promise<void> | null>(null);
  const sidecarsRef = useRef<SidecarSubtitles | null>(null);
  const watchdogRef = useRef(new StallWatchdog(Date.now()));
  const rebufferRef = useRef(new RebufferCounter());
  const chronicRef = useRef(new ChronicBufferingTracker());
  /** When the current source started showing frames (chronic-buffering observation span). */
  const framesSinceRef = useRef<number | null>(null);
  const seekGraceUntilRef = useRef(0);
  const resumeGraceUntilRef = useRef(0);
  const userPausedRef = useRef(false);
  /** When the viewer paused (ms), for the long-pause check on Play. */
  const pausedAtRef = useRef<number | null>(null);
  /** A network failure that arrived while paused, replayed when the viewer presses Play. */
  const heldFailureRef = useRef<{ token: number; signal: FailureSignal } | null>(null);
  const longPauseTimerRef = useRef<number | null>(null);
  const autoplayBlockedRef = useRef(false);
  const pendingSeekRef = useRef<number | null>(null);
  const seekFlushTimerRef = useRef<number | null>(null);
  const seekHintTimerRef = useRef<number | null>(null);
  const reporterRef = useRef<ProgressReporter | null>(null);
  const presenceRef = useRef<PlaybackPresenceReporter | null>(null);
  const wakeLockRef = useRef<{ release: () => Promise<void>; released?: boolean } | null>(null);
  const confirmedSourceRef = useRef(false);
  const ttffStartRef = useRef(0);
  const prefetchedNextRef = useRef<string>('');
  const audioPrefAppliedRef = useRef(false);
  const subtitlePrefAppliedRef = useRef(false);
  const advancingRef = useRef(false);
  const interactionRef = useRef(false);

  const [isPlaying, setIsPlaying] = useState(false);
  const [autoplayBlocked, setAutoplayBlocked] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [progress, setProgress] = useState(0);
  const [duration, setDuration] = useState(0);
  const [durationIsMediaDerived, setDurationIsMediaDerived] = useState(false);
  const [buffered, setBuffered] = useState(0);
  const [mediaEnded, setMediaEnded] = useState(false);
  const [markers, setMarkers] = useState<PlayerMarkers>({});
  const [nextEpisode, setNextEpisode] = useState<WebMediaItem | null>(null);
  const [seasons, setSeasons] = useState<SeasonItem[]>([]);
  const [audioTracks, setAudioTracks] = useState<TrackOption[]>([]);
  // A prepared session carries one audio track; the menu comes from the file's facts.
  const [preparedLocally, setPreparedLocally] = useState(false);
  const [preparedAudio, setPreparedAudio] = useState<{ tracks: MediaAudioFacts[]; selected: number | null } | null>(null);
  /** The viewer's audio pick for a file (kept across seeks and re-preparations of that file). */
  const audioChoiceRef = useRef<{ url: string; index: number } | null>(null);
  /** While a (re)started attempt loads, show the position it will start at, not the element's 0. */
  const holdPositionRef = useRef<{ token: number; position: number } | null>(null);
  const [subtitleTracks, setSubtitleTracks] = useState<TrackOption[]>([]);
  const [fit, setFit] = useState<'fit' | 'fill'>('fit');
  const [speed, setSpeed] = useState(1);
  const [chromeVisible, setChromeVisible] = useState(true);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState<SettingsTab>('sources');
  const [dismissedPrompts, setDismissedPrompts] = useState<Set<string>>(() => new Set());
  const [countdown, setCountdown] = useState<number | null>(null);
  const [countdownCancelled, setCountdownCancelled] = useState(false);
  const [stillWatching, setStillWatching] = useState(false);
  const [pipActive, setPipActive] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [seekHint, setSeekHint] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [volume, setVolume] = useState(() => getStoredVolume().volume);
  const [muted, setMuted] = useState(() => getStoredVolume().muted);
  const [now, setNow] = useState(() => Date.now());
  const [failureReasonsVersion, setFailureReasonsVersion] = useState(0);
  // tvOS/iOS resume gate: a title with a saved position asks before it plays.
  const [resumePrompt, setResumePrompt] = useState<number | null>(null);
  const resumeGateRef = useRef<{ promise: Promise<number>; resolve: (position: number) => void } | null>(null);
  const resumePromptRef = useRef<number | null>(null);
  resumePromptRef.current = resumePrompt;
  const answerResume = useCallback((position: number) => {
    const current = itemRef.current;
    if (position === 0 && current) clearSavedPlaybackState(current);
    setResumePrompt(null);
    resumeGateRef.current?.resolve(position);
  }, []);
  const settingsOpenRef = useRef(false);
  settingsOpenRef.current = settingsOpen;
  const hoveringControlsRef = useRef(false);
  const scrubbingRef = useRef(false);
  const hideTimer = useRef<number | null>(null);
  const toastTimer = useRef<number | null>(null);

  const showToast = useCallback((message: string) => {
    setToast(message);
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 4000);
  }, []);

  // ---- position helpers (all in movie time) ----

  const elementMovieTime = useCallback((): number => {
    const video = videoRef.current;
    const hold = holdPositionRef.current;
    if (hold && hold.token === liveRef.current.token) return hold.position;
    if (!video || !Number.isFinite(video.currentTime)) return 0;
    return liveRef.current.base + video.currentTime;
  }, []);

  /** Where playback "is": the element when it has a position, else the last good one. */
  const currentPosition = useCallback((): number => {
    const video = videoRef.current;
    if (video && Number.isFinite(video.currentTime) && video.currentTime > 0) return elementMovieTime();
    return sessionRef.current.lastGood;
  }, [elementMovieTime]);

  const bestDuration = useCallback((): number => {
    const video = videoRef.current;
    return effectiveDuration(video, knownDurationRef.current, liveRef.current.base, Boolean(liveRef.current.session));
  }, []);

  /** Refresh the timeline state from the element (movie time; see effectiveDuration). */
  const syncTimeline = useCallback(() => {
    const live = liveRef.current;
    if (holdPositionRef.current) return;
    updatePlaybackTimeline(
      videoRef.current, setProgress, setDuration, setBuffered, knownDurationRef.current, live.base, setDurationIsMediaDerived,
      live.session ? { exact: durationProbedRef.current } : null,
    );
  }, []);

  // ---- presence (live viewing state) ----

  const presenceKey = item ? `${profileId ?? 'current'}:${itemKey}` : '';
  useEffect(() => {
    const current = itemRef.current;
    if (!current) return undefined;
    const reporter = new PlaybackPresenceReporter({
      profile_id: profileId,
      media_id: playbackId(current),
      media_type: mediaType(current) === 'series' ? 'series' : 'movie',
      title: displayTitle(current),
      season: current.season == null ? undefined : Number(current.season),
      episode: current.episode == null ? undefined : Number(current.episode),
      tier: presenceTier(tierRef.current),
      source_attempt: 0,
    });
    presenceRef.current = reporter;
    return () => {
      reporter.stop();
      if (presenceRef.current === reporter) presenceRef.current = null;
    };
  }, [presenceKey]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const source = streams[sourceIndex];
    presenceRef.current?.updateContext({
      tier: presenceTier(tier),
      position: Math.max(0, currentTime),
      duration: Number.isFinite(duration) && duration > 0 ? duration : undefined,
      source_id: source?.id,
      selected_provider: source ? providerLabel(source) || undefined : undefined,
    });
    if (machine.phase === 'error') presenceRef.current?.transition('failed');
  }, [streams, sourceIndex, tier, currentTime, duration, machine.phase]);

  // ---- volume / speed ----

  useEffect(() => {
    const video = videoRef.current;
    if (video) {
      video.volume = clamp(volume, 0, 1);
      video.muted = muted;
    }
    setStoredVolume(volume, muted);
  }, [volume, muted]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    // Loading a source resets playbackRate to defaultPlaybackRate: set both, so
    // the chosen speed survives seeks, source switches and re-preparations.
    video.defaultPlaybackRate = speed;
    video.playbackRate = speed;
  }, [speed, attempt]);

  // ---- wake lock ----

  const releaseWakeLock = useCallback(async () => {
    const lock = wakeLockRef.current;
    wakeLockRef.current = null;
    if (!lock || lock.released) return;
    await lock.release().catch(() => undefined);
  }, []);

  const requestWakeLock = useCallback(async () => {
    const nav = navigator as Navigator & { wakeLock?: { request: (type: 'screen') => Promise<{ release: () => Promise<void>; released?: boolean }> } };
    if (!nav.wakeLock || wakeLockRef.current) return;
    wakeLockRef.current = await nav.wakeLock.request('screen').catch(() => null);
  }, []);

  useEffect(() => {
    if (!item || !isPlaying) {
      void releaseWakeLock();
      return undefined;
    }
    void requestWakeLock();
    const onVisibility = () => {
      if (document.hidden) void releaseWakeLock();
      else void requestWakeLock();
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, [isPlaying, item, releaseWakeLock, requestWakeLock]);

  // ---- server session lifecycle ----

  const stopServerSession = useCallback(() => {
    keepaliveRef.current?.stop();
    keepaliveRef.current = null;
    sidecarsRef.current?.stop();
    sidecarsRef.current = null;
    const session = liveRef.current.session;
    liveRef.current.session = null;
    if (session) {
      const stopping = stopRemuxSession(session).catch(() => undefined);
      pendingStopRef.current = stopping;
      void stopping.finally(() => {
        if (pendingStopRef.current === stopping) pendingStopRef.current = null;
      });
    }
    return pendingStopRef.current ?? Promise.resolve();
  }, []);

  /**
   * Release the current server session and wait (bounded) until the server
   * has stopped it, so a seek-restart or failover never runs two ffmpeg
   * processes for one viewer or trips the concurrency cap.
   */
  const releaseServerSession = useCallback(async () => {
    const stopping = stopServerSession();
    let timer: number | null = null;
    await Promise.race([
      stopping,
      new Promise<void>((resolve) => { timer = window.setTimeout(resolve, SESSION_STOP_WAIT_MS); }),
    ]);
    if (timer) window.clearTimeout(timer);
  }, [stopServerSession]);

  const clearStartupTimer = useCallback(() => {
    if (startupTimerRef.current) window.clearTimeout(startupTimerRef.current);
    startupTimerRef.current = null;
  }, []);

  const clearRetryTimer = useCallback(() => {
    const session = sessionRef.current;
    if (session.retryTimer) window.clearTimeout(session.retryTimer);
    session.retryTimer = null;
  }, []);

  const syncTracks = useCallback(() => {
    refreshTrackState(videoRef.current, hlsRef.current, setAudioTracks, setSubtitleTracks);
  }, []);

  // ---- plan + candidate helpers ----

  const planOf = useCallback((index: number): PlaybackPlan => {
    const stream = streamsRef.current[index];
    const base = stream ? planForStream(stream, getPreferences().audioLanguage) : null;
    const plan: PlaybackPlan = base ?? {
      kind: 'unplayable', reason: 'No source', basis: 'fields', allowHevc: false, allowTranscode: false, surround: false, videoScope: null, score: 0,
    };
    const override = sessionRef.current.planOverride.get(index);
    if (!override || override === plan.kind) return plan;
    return { ...plan, kind: override, allowTranscode: override === 'server-transcode' || plan.allowTranscode, reason: `${plan.reason} (escalated)` };
  }, []);

  /** Indices/routes excluded from automatic selection for their current path. */
  const exclusions = useCallback(() => {
    const tried = new Set<number>();
    const triedRoutes = new Set<string>();
    sessionRef.current.triedPaths.forEach((paths, index) => {
      const stream = streamsRef.current[index];
      if (!stream || paths.has(planOf(index).kind)) {
        tried.add(index);
        if (stream) triedRoutes.add(routeIdentity(stream));
      }
    });
    return { tried, triedRoutes };
  }, [planOf]);

  const markTried = useCallback((index: number, path: PlanKind, reason: string) => {
    const session = sessionRef.current;
    const paths = session.triedPaths.get(index) ?? new Set<PlanKind>();
    paths.add(path);
    session.triedPaths.set(index, paths);
    session.failureReasons.set(index, reason);
    setFailureReasonsVersion((value) => value + 1);
  }, []);

  const reportAbandoned = useCallback((index: number, classified: ClassifiedFailure) => {
    const failed = streamsRef.current[index];
    if (!failed) return;
    void reportStreamFailure({
      source_id: failed.id,
      provider_id: failed.provider_id,
      route_id: failed.route_id,
      file_id: failed.file_id ?? null,
      failure_category: failureCategory(classified),
      reason: `${classified.code} ${classified.message}`.slice(0, 500),
    })?.catch?.(() => undefined);
  }, []);

  // ---- attempt control ----

  const begin = useCallback((index: number, options: {
    position: number;
    reason: string;
    path?: PlanKind;
    viaProxy?: boolean;
    autoplay?: boolean;
  }) => {
    const session = sessionRef.current;
    const stream = streamsRef.current[index];
    if (!stream) return;
    clearRetryTimer();
    clearStartupTimer();
    tokenRef.current += 1;
    session.attempts += 1;
    // Each newly started source gets its backups checked (native beginPreflighting).
    session.backupsProbed = false;
    const position = Math.max(0, options.position);
    if (position > 0 || options.reason === 'start' || options.reason === 'start-over') session.lastGood = position;
    userPausedRef.current = false;
    setSourceIndex(index);
    setAttempt({
      token: tokenRef.current,
      itemKey: session.itemKey,
      index,
      position,
      path: options.path ?? planOf(index).kind,
      viaProxy: Boolean(options.viaProxy),
      autoplay: options.autoplay ?? true,
      reason: options.reason,
    });
  }, [clearRetryTimer, clearStartupTimer, planOf]);

  const fail = useCallback((code: string, message: string) => {
    clearStartupTimer();
    clearRetryTimer();
    cleanupPlayback(videoRef.current, hlsRef);
    stopServerSession();
    tokenRef.current += 1;
    liveRef.current = { ...idleLive(), token: tokenRef.current };
    const current = itemRef.current;
    if (current) invalidatePlaybackSources(current, profileId);
    dispatch({ type: 'FAIL', error: { code, message } });
  }, [clearRetryTimer, clearStartupTimer, dispatch, profileId, stopServerSession]);

  // One fresh discovery per playback session when every candidate is spent.
  // Old indices and exclusions are kept; only genuinely renewed routes are added.
  const refreshExhausted = useCallback(async (position: number, classified: ClassifiedFailure) => {
    const current = itemRef.current;
    const session = sessionRef.current;
    if (!current) return;
    session.ledger.exhaustionUsed = true;
    session.recovery?.abort();
    const controller = new AbortController();
    session.recovery = controller;
    cleanupPlayback(videoRef.current, hlsRef);
    stopServerSession();
    dispatch({ type: 'RECOVER', note: 'Looking for more sources' });
    try {
      const refreshed = await loadPlaybackSources(current, {
        tier: tierRef.current, profileId, sessionId: presenceRef.current?.sessionId, refresh: true, signal: controller.signal,
      });
      if (controller.signal.aborted || session !== sessionRef.current) return;
      // Native: every source from the first discovery counts as attempted, and
      // probe verdicts (provider exclusions too) start over for the new routes.
      const previousCount = streamsRef.current.length;
      const merged = mergeRecoveryStreams(streamsRef.current, refreshed.streams);
      setStreams(merged);
      session.verdicts.clear();
      session.excludedProviders.clear();
      const { tried, triedRoutes } = exclusions();
      for (let index = 0; index < previousCount; index += 1) tried.add(index);
      const next = selectNextSource({
        streams: merged, tried, triedRoutes, currentIndex: liveRef.current.index, verdicts: session.verdicts,
        planOf: (_stream, index) => planOf(index),
        excludedProviders: session.excludedProviders,
      });
      if (next !== null) {
        recordAction(session.ledger, { type: 'failover', delayMs: 0, disableScope: false, reason: 'refreshed' }, '');
        begin(next, { position, reason: 'failover' });
        return;
      }
      fail(classified.code, session.desktopHint
        ? DESKTOP_APP_MESSAGE
        : 'All available sources failed, including the refreshed results. Try again or select another title.');
    } catch {
      if (controller.signal.aborted) return;
      fail(classified.code, 'Could not refresh playback sources. Try again.');
    }
  }, [begin, dispatch, exclusions, fail, planOf, profileId, setStreams, stopServerSession]);

  const failoverFrom = useCallback((index: number, position: number, classified: ClassifiedFailure, delayMs: number) => {
    const session = sessionRef.current;
    const { tried, triedRoutes } = exclusions();
    // Native SourceFailoverSelector flags: a lighter bitrate only after two
    // rebuffering failovers in a row; the HDR fallback after a decode failure.
    const reason = failoverReason(classified);
    const lighter = shouldPreferLighterSource(reason, session.lastFailoverReason);
    session.lastFailoverReason = reason;
    const current = itemRef.current;
    const next = selectNextSource({
      streams: streamsRef.current, tried, triedRoutes, currentIndex: index, verdicts: session.verdicts,
      planOf: (_stream, candidate) => planOf(candidate),
      excludedProviders: session.excludedProviders,
      preferSameFile: prefersSameFile(classified),
      preferLowerBitrate: lighter,
      preferHdrFallback: reason === 'decode',
      runtimeSeconds: Number.isFinite(videoRef.current?.duration) && (videoRef.current?.duration ?? 0) > 0
        ? videoRef.current!.duration
        : runtimeSecondsOf(current),
    });
    if (next === null) {
      if (!session.ledger.exhaustionUsed) {
        void refreshExhausted(position, classified);
        return;
      }
      fail(classified.code, hasLocalEngine()
        ? "None of the available sources played. They may be unavailable right now, or use formats Curatio can't play yet (Dolby Vision profile 5)."
        : session.desktopHint
          ? DESKTOP_APP_MESSAGE
          : "None of the available sources played in this browser. They may use formats this browser can't decode.");
      return;
    }
    recordAction(session.ledger, { type: 'failover', delayMs, disableScope: false, reason: classified.message }, '');
    cleanupPlayback(videoRef.current, hlsRef);
    stopServerSession();
    const wasPlaying = machineRef.current.startedOnce;
    dispatch({ type: 'RECOVER', note: wasPlaying ? 'Switching source' : 'Trying another source' });
    const go = () => {
      session.retryTimer = null;
      if (session !== sessionRef.current) return;
      const target = streamsRef.current[next];
      if (wasPlaying && target) showToast(`Switched to ${target.label || providerLabel(target) || 'another source'}`);
      begin(next, { position, reason: 'failover', autoplay: !userPausedRef.current });
    };
    if (delayMs > 0) session.retryTimer = window.setTimeout(go, delayMs);
    else go();
  }, [begin, dispatch, exclusions, fail, planOf, refreshExhausted, showToast, stopServerSession]);

  // Re-resolve the same route (refresh=true) and retry it with the renewed URL.
  const reResolve = useCallback(async (index: number, position: number, classified: ClassifiedFailure) => {
    const current = itemRef.current;
    const session = sessionRef.current;
    const failed = streamsRef.current[index];
    if (!current || !failed) return;
    session.recovery?.abort();
    const controller = new AbortController();
    session.recovery = controller;
    cleanupPlayback(videoRef.current, hlsRef);
    stopServerSession();
    dispatch({ type: 'RECOVER', note: 'Refreshing the link' });
    try {
      const refreshed = await loadPlaybackSources(current, {
        tier: tierRef.current, profileId, sessionId: presenceRef.current?.sessionId, refresh: true, signal: controller.signal,
      });
      if (controller.signal.aborted || session !== sessionRef.current) return;
      const renewed = findRenewedRoute(failed, refreshed.streams);
      if (renewed && streamUrl(renewed) && streamUrl(renewed) !== streamUrl(failed)) {
        forgetMediaFacts(streamUrl(failed));
        const next = [...streamsRef.current];
        next[index] = renewed;
        setStreams(next);
        begin(index, { position, reason: 'reresolve', autoplay: !userPausedRef.current });
        return;
      }
    } catch {
      if (controller.signal.aborted) return;
    }
    markTried(index, liveRef.current.path, classified.message);
    reportAbandoned(index, classified);
    failoverFrom(index, position, classified, 0);
  }, [begin, dispatch, failoverFrom, markTried, profileId, reportAbandoned, setStreams, stopServerSession]);

  /**
   * THE failure entry point. Element errors, engine errors, watchdogs, server
   * preparation and premature EOF all land here exactly once per attempt.
   */
  const handleSourceFailure = useCallback((token: number, signal: FailureSignal) => {
    const live = liveRef.current;
    if (token !== live.token || live.failed) return;
    // An idle connection dropping under a paused frame is not a failure yet:
    // hold it until the viewer presses Play (see onPlay).
    if (userPausedRef.current && live.playingSeen && videoRef.current?.paused !== false && isHeldWhilePaused(signal)) {
      heldFailureRef.current = { token, signal };
      playbackTrace('failure held while paused', { kind: signal.kind });
      return;
    }
    heldFailureRef.current = null;
    live.failed = true;
    clearStartupTimer();
    const session = sessionRef.current;
    session.healthySince = null;
    const index = live.index;
    const stream = streamsRef.current[index];
    const elementTime = elementMovieTime();
    const position = elementTime > 0 ? elementTime : session.lastGood;
    if (position > 0) session.lastGood = position;
    const plan = planOf(index);
    const scopeCodec = plan.videoScope ? plan.videoScope.split(':')[0] : null;
    const ctx = {
      hadPlayed: live.hadPlayed,
      path: live.path,
      // Same-origin signed /media/ URLs gain nothing from the byte proxy.
      proxyEligible: live.path === 'direct' && !live.viaProxy && !(stream && isSameOriginUrl(streamUrl(stream))),
      videoCodec: scopeCodec,
      sourceKey: stream ? routeIdentity(stream) : String(index),
      canReResolve: Boolean(stream?.route_id || stream?.file_id),
      sourceIsHls: plan.kind === 'hls',
    };
    const classified = classifyFailure(signal, ctx);
    const action: RecoveryAction = decideRecovery(session.ledger, classified, ctx);
    playbackTrace('playback failure', { code: classified.code, message: classified.message, action: action.type, index, path: live.path });
    rebufferRef.current.reset();
    chronicRef.current.reset();

    switch (action.type) {
      case 'reload':
      case 'restart-session': {
        recordAction(session.ledger, action, ctx.sourceKey);
        if (live.hadPlayed) dispatch({ type: 'RECOVER', note: 'Reconnecting' });
        if (action.type === 'restart-session') stopServerSession();
        const viaProxy = action.type === 'reload' ? action.viaProxy || live.viaProxy : false;
        const delay = action.type === 'reload' ? action.delayMs : 0;
        const go = () => {
          session.retryTimer = null;
          if (session !== sessionRef.current) return;
          begin(index, { position, reason: action.type, path: live.path, viaProxy, autoplay: !userPausedRef.current });
        };
        if (delay > 0) {
          cleanupPlayback(videoRef.current, hlsRef);
          session.retryTimer = window.setTimeout(go, delay);
        } else go();
        return;
      }
      case 'reresolve':
        recordAction(session.ledger, action, ctx.sourceKey);
        void reResolve(index, position, classified);
        return;
      case 'path-change':
        recordAction(session.ledger, action, ctx.sourceKey);
        session.planOverride.set(index, action.to);
        session.failureReasons.set(index, `${classified.message} — retrying with server preparation`);
        setFailureReasonsVersion((value) => value + 1);
        stopServerSession();
        begin(index, { position, reason: 'path-change', path: action.to, autoplay: !userPausedRef.current });
        return;
      case 'failover':
        if (action.disableScope) disableDecodeScope(plan.videoScope);
        markTried(index, live.path, classified.message);
        reportAbandoned(index, classified);
        failoverFrom(index, position, classified, action.delayMs);
        return;
      case 'fatal':
      default:
        markTried(index, live.path, classified.message);
        reportAbandoned(index, classified);
        fail(classified.code, classified.category === 'AUTH'
          ? classified.message
          : `${classified.message} Playback stopped after trying several sources.`);
    }
  }, [begin, clearStartupTimer, dispatch, elementMovieTime, fail, failoverFrom, markTried, planOf, reResolve, reportAbandoned, stopServerSession]);

  const noteSignalOfLife = useCallback((token: number) => {
    const live = liveRef.current;
    if (token !== live.token || live.sawSignal) return;
    live.sawSignal = true;
    clearStartupTimer();
    watchdogRef.current.reset(Date.now());
  }, [clearStartupTimer]);

  // Native chronic-buffering rule, judged on stall edges, and only when there
  // is another source to switch to (a switch costs a black screen).
  const checkChronicBuffering = useCallback((token: number, at: number) => {
    const live = liveRef.current;
    if (token !== live.token || live.failed) return;
    if (!chronicRef.current.shouldEscalate(at, framesSinceRef.current)) return;
    const session = sessionRef.current;
    const { tried, triedRoutes } = exclusions();
    const alternate = selectNextSource({
      streams: streamsRef.current, tried, triedRoutes, currentIndex: live.index, verdicts: session.verdicts,
      planOf: (_stream, index) => planOf(index), excludedProviders: session.excludedProviders,
    });
    if (alternate == null) return;
    chronicRef.current.reset();
    playbackTrace('chronic buffering', { index: live.index });
    handleSourceFailure(token, { kind: 'rebuffer' });
  }, [exclusions, handleSourceFailure, planOf]);

  // Once a source plays, quietly check the next few candidates (native
  // beginPreflighting) so a later switch skips dead links and down providers.
  // Already-checked sources are skipped, so a re-run after a switch is cheap.
  const preflightBackups = useCallback(() => {
    const session = sessionRef.current;
    if (session.backupsProbed) return;
    session.backupsProbed = true;
    const { tried, triedRoutes } = exclusions();
    const ranked = rankSources({
      streams: streamsRef.current, tried, triedRoutes, currentIndex: liveRef.current.index, verdicts: session.verdicts,
      planOf: (_stream, index) => planOf(index), excludedProviders: session.excludedProviders,
    });
    const targets = probeTargets(ranked, streamsRef.current).filter((index) => {
      const stream = streamsRef.current[index];
      return stream && !skipsSpeculativeProbe(stream) && !session.verdicts.has(streamUrl(stream));
    });
    for (const index of targets) {
      const stream = streamsRef.current[index];
      const url = streamUrl(stream);
      void probePlaybackSources({ urls: [url] })
        .then((response) => {
          if (session !== sessionRef.current) return;
          const verdict = response.results[url];
          if (!verdict) return;
          session.verdicts.set(url, verdict);
          const provider = providerIdentity(stream);
          if (provider && isProviderUnavailable(verdict)) session.excludedProviders.add(provider);
        })
        .catch(() => undefined);
    }
  }, [exclusions, planOf]);

  const noteFirstFrame = useCallback((token: number) => {
    const live = liveRef.current;
    if (token !== live.token || live.firstFrame) return;
    live.firstFrame = true;
    framesSinceRef.current = Date.now();
    noteSignalOfLife(token);
    sessionRef.current.healthySince = Date.now();
    dispatch({ type: 'FIRST_FRAME' });
    if (ttffStartRef.current) {
      playbackTrace('time to first frame', { ms: Math.round(performance.now() - ttffStartRef.current), path: live.path, reason: attempt?.reason });
      ttffStartRef.current = 0;
    }
    const current = itemRef.current;
    if (!confirmedSourceRef.current && current) {
      const ctx = episodeFingerprintContext(current);
      const url = streamUrl(streamsRef.current[live.index]);
      if (ctx && url) {
        confirmedSourceRef.current = true;
        void confirmActiveSource({ url, ...ctx })?.catch?.(() => undefined);
      }
    }
    preflightBackups();
  }, [attempt?.reason, dispatch, noteSignalOfLife, preflightBackups]);

  // ---- item lifecycle: reset, metadata, discovery ----

  useEffect(() => {
    const current = itemRef.current;
    if (!current) return undefined;
    const session = newSession(itemKey);
    sessionRef.current?.discovery?.abort();
    sessionRef.current?.recovery?.abort();
    if (sessionRef.current?.retryTimer) window.clearTimeout(sessionRef.current.retryTimer);
    sessionRef.current = session;
    selectionRef.current = selection;
    tokenRef.current += 1;
    liveRef.current = { ...idleLive(), token: tokenRef.current };
    knownDurationRef.current = itemRuntimeSeconds(current);
    durationProbedRef.current = false;
    audioChoiceRef.current = null;
    setPreparedAudio(null);
    confirmedSourceRef.current = false;
    prefetchedNextRef.current = '';
    advancingRef.current = false;
    pausedAtRef.current = null;
    heldFailureRef.current = null;
    if (longPauseTimerRef.current != null) window.clearTimeout(longPauseTimerRef.current);
    longPauseTimerRef.current = null;
    setStreams([]);
    setAttempt(null);
    setSourceIndex(0);
    setTier(selection?.tier ?? defaultSourceTier());
    setResolvedQuality(null);
    setIsPlaying(false);
    setAutoplayBlocked(false);
    autoplayBlockedRef.current = false;
    setCurrentTime(0);
    setProgress(0);
    setDuration(0);
    setDurationIsMediaDerived(false);
    setBuffered(0);
    setMediaEnded(false);
    setMarkers({});
    setNextEpisode(null);
    setSeasons([]);
    setDismissedPrompts(new Set());
    setCountdown(null);
    setCountdownCancelled(false);
    setStillWatching(false);
    setSettingsOpen(false);
    setSettingsTab('sources');
    dispatch({ type: 'RESET' });
    // Auto-resume: start at the saved position (server progress first, local fallback).
    const serverEntry = userState?.continueWatching.find((entry) => playbackProgressKey(entry) === itemKey) ?? null;
    session.lastGood = resumeStartFor(current, serverEntry);
    // Sources are found while the question is on screen; playback waits for the answer.
    if (session.lastGood > 1) {
      let resolve: (position: number) => void = () => undefined;
      const promise = new Promise<number>((done) => { resolve = done; });
      resumeGateRef.current = { promise, resolve };
      setResumePrompt(session.lastGood);
    } else {
      resumeGateRef.current = null;
      setResumePrompt(null);
    }

    const reporter = new ProgressReporter({
      item: current,
      snapshot: () => {
        const position = currentPosition();
        return position > 0 ? { position, duration: bestDuration(), creditsStart: markersRef.current.creditsStart } : null;
      },
      isPlaying: () => Boolean(videoRef.current && !videoRef.current.paused),
      win: window,
      doc: document,
    });
    reporterRef.current = reporter;
    reporter.start();
    return () => {
      reporter.report('switch');
      reporter.stop();
      if (reporterRef.current === reporter) reporterRef.current = null;
      // Continue Watching, watched ticks and "Play S1 · E2" are drawn from the
      // server: refresh them once it has this title's final position.
      void reporter.settled()?.then(() => window.dispatchEvent(new Event('curatio:library-changed')));
      session.discovery?.abort();
      session.recovery?.abort();
      if (session.retryTimer) window.clearTimeout(session.retryTimer);
      clearStartupTimer();
      cleanupPlayback(videoRef.current, hlsRef);
      stopServerSession();
      void releaseWakeLock();
    };
  }, [itemKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const markersRef = useRef(markers);
  markersRef.current = markers;

  // Markers, next episode and seasons; intro/credits keep polling until complete.
  useEffect(() => {
    const requested = itemRef.current;
    if (!requested || mediaEnded) return undefined;
    const controller = new AbortController();
    let active = true;
    let pollCount = 0;
    let timer: number | null = null;
    const scheduleNext = (retryAfterSec?: number) => {
      if (!active) return;
      const delay = pollCount >= 4 ? 60_000 : Math.max(1, Number(retryAfterSec) || 5) * 1000;
      timer = window.setTimeout(() => {
        timer = null;
        void refreshPlaybackSegments(requested, controller.signal)
          .then((response) => {
            if (!active) return;
            setMarkers(markersFromSegments(response.segments));
            if (response.fingerprint_status !== 'complete') {
              pollCount += 1;
              scheduleNext(response.retry_after_sec);
            }
          })
          .catch(() => scheduleNext(5));
      }, delay);
    };
    void preparePlaybackMetadata(requested, controller.signal)
      .then((metadata) => {
        if (!active) return;
        setMarkers(metadata.markers);
        setNextEpisode(metadata.nextEpisode);
        setSeasons((metadata as { seasons?: SeasonItem[] }).seasons ?? []);
        if (metadata.segmentResponse?.fingerprint_status !== 'complete') {
          pollCount += 1;
          scheduleNext(metadata.segmentResponse?.retry_after_sec);
        }
      })
      .catch(() => scheduleNext(5));
    return () => {
      active = false;
      controller.abort();
      if (timer) window.clearTimeout(timer);
    };
  }, [itemKey, mediaEnded]);

  // Discovery: resolve sources, pick a start candidate, learn its facts, begin.
  useEffect(() => {
    const requested = itemRef.current;
    if (!requested) return undefined;
    const session = sessionRef.current;
    if (session.itemKey !== itemKey) return undefined;
    session.discovery?.abort();
    session.recovery?.abort();
    const controller = new AbortController();
    session.discovery = controller;
    let cancelled = false;
    dispatch({ type: 'RESOLVE' });
    presenceRef.current?.beginDiscovery();
    async function discover() {
      try {
        const { streams: resolved, resolvedCount, quality: answeredQuality } = await loadPlaybackSources(requested!, {
          tier,
          profileId,
          sessionId: presenceRef.current?.sessionId,
          refresh: lookupToken > 0,
          signal: controller.signal,
        });
        if (cancelled) return;
        setResolvedQuality(answeredQuality ?? null);
        // Direct play first, then remux, transcode last (server order within a
        // bucket), so the probe shortlist and the start candidate follow what
        // this browser plays best. Cached facts sharpen the plan when present.
        const audioLanguage = getPreferences().audioLanguage;
        const playable = orderByPlayability(resolved, (stream) => planForStream(stream, audioLanguage));
        setStreams(playable);
        if (!playable.length) {
          fail('CU-GONE-04', resolvedCount
            ? 'None of the available sources can play in this browser.'
            : 'Not available to play. It is not in your Plex or Jellyfin, and no source could find it.');
          return;
        }
        const chosen = selectionRef.current;
        selectionRef.current = undefined;
        const chosenIndex = chosen && chosen.tier === tier
          ? playable.findIndex((stream) => routeIdentity(stream) === chosen.sourceKey)
          : -1;
        let startIndex = Math.max(chosenIndex, 0);
        if (chosenIndex < 0 && playable.length > 1) {
          // Probe a shortlist (never opaque routes) while the server inspects
          // the top non-opaque candidates; both are bounded.
          const [picked] = await Promise.all([
            selectInitialSource(playable, {
              signal: controller.signal,
              fingerprintContext: episodeFingerprintContext(requested!),
              onVerdict: (url, verdict) => session.verdicts.set(url, verdict),
              onProviderUnavailable: (provider) => session.excludedProviders.add(provider),
            }),
            inspectCandidates(playable.slice(0, 6), { signal: controller.signal }),
          ]);
          if (cancelled) return;
          // Every source failed its check: start on the server's first anyway
          // (a probe can false-negative) and let failover reach the one
          // refresh=true re-discovery, which renews expired links.
          startIndex = picked ?? 0;
        }
        // The start candidate's facts decide its path. Wait briefly when cold.
        const startUrl = streamUrl(playable[startIndex]);
        if (startUrl && !peekMediaFacts(startUrl)) {
          let capTimer: number | null = null;
          await Promise.race([
            inspectCandidates([], { includeOpaque: [playable[startIndex]], limit: 1, signal: controller.signal }),
            new Promise<void>((resolve) => { capTimer = window.setTimeout(resolve, COLD_INSPECT_WAIT_MS); }),
          ]);
          if (capTimer) window.clearTimeout(capTimer);
          if (cancelled) return;
        }
        // Never start on a transcode-only source while a decodable one exists.
        const startPlan = planOf(startIndex);
        if (chosenIndex < 0 && (startPlan.kind === 'server-transcode' || startPlan.kind === 'unplayable')) {
          const better = playable.findIndex((stream, index) => {
            const verdict = session.verdicts.get(streamUrl(stream));
            const kind = planOf(index).kind;
            const provider = providerIdentity(stream);
            return verdict?.state !== 'failed' && kind !== 'server-transcode' && kind !== 'unplayable'
              && !(provider && session.excludedProviders.has(provider));
          });
          if (better >= 0) startIndex = better;
        }
        const gate = resumeGateRef.current;
        if (gate) {
          session.lastGood = await gate.promise;
          if (cancelled) return;
        }
        begin(startIndex, { position: session.lastGood, reason: 'start' });
      } catch (err) {
        if (cancelled || controller.signal.aborted) return;
        fail('CU-NET-07', err instanceof Error ? err.message : 'This title is not ready to play in this browser yet.');
      }
    }
    void discover();
    return () => {
      cancelled = true;
      controller.abort();
      session.recovery?.abort();
    };
  }, [itemKey, tier, lookupToken]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- run an attempt ----

  useEffect(() => {
    if (!attempt || attempt.itemKey !== sessionRef.current.itemKey || attempt.token !== tokenRef.current) return undefined;
    const current = itemRef.current;
    const stream = streamsRef.current[attempt.index];
    if (!current || !stream) return undefined;
    const token = attempt.token;
    const session = sessionRef.current;
    const live: LiveAttempt = {
      ...idleLive(),
      token,
      index: attempt.index,
      path: attempt.path,
      viaProxy: attempt.viaProxy,
      startPosition: attempt.position,
      startedAt: Date.now(),
    };
    liveRef.current = live;
    let cancelled = false;
    const stale = () => cancelled || tokenRef.current !== token;
    // Loading resets the element to 0 and a prepared session begins at a keyframe
    // before the target: keep showing where this attempt will start until it is there.
    holdPositionRef.current = attempt.position > 0 ? { token, position: attempt.position } : null;
    if (attempt.position > 0) {
      setCurrentTime(attempt.position);
      const total = knownDurationRef.current;
      if (total > 0) setProgress(clamp(attempt.position / total, 0, 1));
    }
    ttffStartRef.current = performance.now();
    audioPrefAppliedRef.current = false;
    subtitlePrefAppliedRef.current = false;
    rebufferRef.current.reset();
    chronicRef.current.reset();
    framesSinceRef.current = null;
    watchdogRef.current.reset(Date.now());
    setIsPlaying(false);
    setAutoplayBlocked(false);
    autoplayBlockedRef.current = false;
    setAudioTracks([]);
    setSubtitleTracks([]);
    setMediaEnded(false);
    dispatch({ type: 'CONNECT', path: attempt.path });
    presenceRef.current?.updateContext({ source_id: stream.id, selected_provider: providerLabel(stream) || undefined });
    presenceRef.current?.beginAttempt();
    const facts = peekMediaFacts(streamUrl(stream));
    // The probed length of this file beats the catalogue's runtime (a guess).
    durationProbedRef.current = Number(facts?.duration) > 0;
    knownDurationRef.current = durationProbedRef.current ? Number(facts?.duration) : itemRuntimeSeconds(current);
    const plan = planOf(attempt.index);
    playbackTrace('loading source', { index: attempt.index, path: attempt.path, basis: plan.basis, reason: attempt.reason, why: plan.reason });

    const armStartupTimer = () => {
      clearStartupTimer();
      const resuming = attempt.position > 0;
      const base = session.attempts <= 1 ? (resuming ? RESUME_SEEK_FAILFAST_MS : INITIAL_FAILFAST_FIRST_MS) : INITIAL_FAILFAST_LATER_MS;
      const delayMs = Math.min(Math.max(base, startupTimeoutMs(stream)), FAILFAST_CEILING_MS);
      startupTimerRef.current = window.setTimeout(() => {
        startupTimerRef.current = null;
        if (!liveRef.current.sawSignal) handleSourceFailure(token, { kind: 'startup-timeout' });
      }, delayMs);
    };

    async function run() {
      const url = streamUrl(stream);
      if (!url || attempt!.path === 'unplayable') {
        handleSourceFailure(token, { kind: 'no-url' });
        return;
      }
      let playable: PlaybackSource = stream;
      let startSeconds = attempt!.position;
      let mode: 'direct' | 'hls' = attempt!.path === 'hls' ? 'hls' : 'direct';
      if (attempt!.path === 'server-hls' || attempt!.path === 'server-transcode') {
        cleanupPlayback(videoRef.current, hlsRef);
        await releaseServerSession();
        if (stale()) return;
        try {
          const result = await startRemuxSession(url, {
            title: displayTitle(current),
            start: attempt!.position,
            audioLanguage: getPreferences().audioLanguage,
            audioIndex: audioChoiceRef.current?.url === url ? audioChoiceRef.current.index : null,
            allowHevc: plan.allowHevc,
            allowTranscode: attempt!.path === 'server-transcode' || plan.allowTranscode,
            surround: plan.surround,
            headers: sourceRequestHeaders(stream),
            facts: peekMediaFacts(url),
          });
          if (stale()) {
            void stopRemuxSession(result.session);
            return;
          }
          live.session = result.session;
          live.base = Math.max(0, Number.isFinite(Number(result.start)) ? Number(result.start) : attempt!.position);
          if (Number(result.duration) > 0) {
            knownDurationRef.current = Number(result.duration);
            durationProbedRef.current = true;
          }
          const fileAudio = peekMediaFacts(url)?.audio ?? [];
          setPreparedAudio(fileAudio.length ? { tracks: fileAudio, selected: Number.isInteger(result.audio_index) ? Number(result.audio_index) : null } : null);
          live.local = Boolean(result.local);
          setPreparedLocally(Boolean(result.local));
          keepaliveRef.current = new SessionKeepalive(result.session, () => handleSourceFailure(token, { kind: 'session-gone' }));
          keepaliveRef.current.start();
          const video = videoRef.current;
          if (video && result.subtitles?.length) {
            sidecarsRef.current = new SidecarSubtitles(video, result.subtitles, live.base, { languageLabel, onTracksChanged: syncTracks });
            sidecarsRef.current.start();
          }
          playable = { ...stream, url: result.playlist_url, delivery: 'hls' };
          mode = 'hls';
          startSeconds = Math.max(0, attempt!.position - live.base);
        } catch (err) {
          if (stale()) return;
          const status = err instanceof RemuxBusyError ? 429
            : err instanceof RemuxUnsupportedError ? 422
              : err instanceof RemuxHttpError ? err.status : null;
          const hint = err instanceof RemuxUnsupportedError ? err.hint : null;
          if (hint === 'desktop_app') session.desktopHint = true;
          handleSourceFailure(token, {
            kind: 'server',
            status,
            message: err instanceof Error ? err.message : undefined,
            hint,
            retryAfterMs: err instanceof RemuxBusyError ? err.retryAfterMs : null,
          });
          return;
        }
      } else {
        stopServerSession();
        setPreparedAudio(null);
        setPreparedLocally(false);
      }
      if (stale()) return;
      armStartupTimer();
      const video = videoRef.current;
      if (video && 'requestVideoFrameCallback' in video) {
        (video as HTMLVideoElement & { requestVideoFrameCallback: (cb: () => void) => number })
          .requestVideoFrameCallback(() => { if (!stale()) noteFirstFrame(token); });
      }
      await loadStreamIntoVideo(
        playable,
        video,
        hlsRef,
        attempt!.autoplay,
        mode,
        attempt!.viaProxy,
        startSeconds,
        (message, signal) => {
          if (stale() || !message) return;
          handleSourceFailure(token, signal ?? { kind: 'transport', message });
        },
        () => {
          if (stale()) return;
          noteSignalOfLife(token);
          syncTracks();
        },
        syncTracks,
      );
      if (!stale()) dispatch({ type: 'MEDIA_ATTACHED' });
    }
    void run();
    return () => {
      cancelled = true;
    };
  }, [attempt]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!item) return undefined;
    return attachNativeTrackListeners(videoRef.current, syncTracks);
  }, [item, attempt, syncTracks]);

  // Preferred audio/subtitle language once a source's tracks are known.
  useEffect(() => {
    const prefs = getPreferences();
    if (!audioPrefAppliedRef.current && prefs.audioLanguage !== 'system' && audioTracks.length) {
      audioPrefAppliedRef.current = true;
      const match = audioTracks.find((track) => trackMatchesLanguage(`${track.label} ${track.detail ?? ''}`, prefs.audioLanguage));
      if (match && !match.selected) selectAudioTrack(match.id, videoRef.current, hlsRef.current, setAudioTracks, setSubtitleTracks);
    }
    if (!subtitlePrefAppliedRef.current && prefs.subtitleLanguage !== 'system' && prefs.subtitleLanguage !== 'off' && subtitleTracks.length > 1) {
      const match = subtitleTracks.find((track) => track.id !== 'off' && trackMatchesLanguage(`${track.label} ${track.detail ?? ''}`, prefs.subtitleLanguage));
      if (match) {
        subtitlePrefAppliedRef.current = true;
        if (!match.selected) selectSubtitleTrack(match.id, videoRef.current, hlsRef.current, setAudioTracks, setSubtitleTracks);
      }
    }
  }, [audioTracks, subtitleTracks]);

  // ---- stall watchdog + loading clock ----

  useEffect(() => {
    if (!item) return undefined;
    const interval = window.setInterval(() => {
      const live = liveRef.current;
      const video = videoRef.current;
      const at = Date.now();
      if (isLoadingPhase(machineRef.current.phase)) setNow(at);
      if (!video || !live.sawSignal || live.failed) return;
      const watchdog = watchdogRef.current;
      watchdog.observe(Number.isFinite(video.currentTime) ? video.currentTime : 0, bufferedEnd(video), at);
      const phase = machineRef.current.phase;
      const active = phase !== 'error' && phase !== 'ended';
      if (watchdog.isStalled(at, {
        paused: userPausedRef.current || autoplayBlockedRef.current,
        hidden: typeof document !== 'undefined' && document.hidden,
        active,
      })) {
        handleSourceFailure(live.token, { kind: 'stall' });
      }
      // Healthy long enough: the incident is over and budgets refill.
      const healthy = sessionRef.current.healthySince;
      if (healthy && at - healthy >= FAILOVER_REASON_MEMORY_MS && video && !video.paused) {
        sessionRef.current.lastFailoverReason = null;
      }
      if (healthy && at - healthy >= INCIDENT_HEALTHY_MS && video && !video.paused) {
        sessionRef.current.ledger.closeIncident();
        sessionRef.current.healthySince = null;
      }
    }, 1000);
    return () => window.clearInterval(interval);
  }, [item, handleSourceFailure]);

  // ---- chrome visibility ----

  const scheduleHide = useCallback(() => {
    if (hideTimer.current) window.clearTimeout(hideTimer.current);
    hideTimer.current = window.setTimeout(() => {
      const video = videoRef.current;
      if (!video || video.paused || settingsOpenRef.current || hoveringControlsRef.current || scrubbingRef.current) return;
      setChromeVisible(false);
    }, CHROME_HIDE_MS);
  }, []);

  const showChrome = useCallback(() => {
    setChromeVisible(true);
    scheduleHide();
  }, [scheduleHide]);

  const noteInteraction = useCallback(() => {
    interactionRef.current = true;
    autoAdvanceStreak = 0;
  }, []);

  useEffect(() => () => {
    if (hideTimer.current) window.clearTimeout(hideTimer.current);
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    if (seekHintTimerRef.current) window.clearTimeout(seekHintTimerRef.current);
    if (seekFlushTimerRef.current) window.clearTimeout(seekFlushTimerRef.current);
  }, []);

  useEffect(() => {
    const onFullscreen = () => setIsFullscreen(Boolean(document.fullscreenElement));
    document.addEventListener('fullscreenchange', onFullscreen);
    return () => document.removeEventListener('fullscreenchange', onFullscreen);
  }, []);

  // Autoplay rejection is browser policy, not a broken source.
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return undefined;
    const blocked = () => {
      setAutoplayBlocked(true);
      autoplayBlockedRef.current = true;
      setChromeVisible(true);
    };
    video.addEventListener('curatioautoplayblocked', blocked);
    return () => video.removeEventListener('curatioautoplayblocked', blocked);
  }, [item]);

  // ---- seeking ----

  const showSeekHint = useCallback((deltaSeconds: number, targetMovie: number) => {
    const arrow = deltaSeconds >= 0 ? '»' : '«';
    setSeekHint(`${arrow} ${formatTime(Math.abs(deltaSeconds))} · ${formatTime(targetMovie)}`);
    if (seekHintTimerRef.current) window.clearTimeout(seekHintTimerRef.current);
    seekHintTimerRef.current = window.setTimeout(() => setSeekHint(null), 1000);
  }, []);

  const seekToMovieTime = useCallback((target: number) => {
    const video = videoRef.current;
    if (!video) return;
    const live = liveRef.current;
    const total = bestDuration();
    const clamped = total > 0 ? clamp(target, 0, Math.max(0, total - 0.5)) : Math.max(0, target);
    const at = Date.now();
    sessionRef.current.lastGood = clamped;
    watchdogRef.current.noteSeek(at);
    seekGraceUntilRef.current = at + SEEK_GRACE_MS;
    setMediaEnded(false);
    if (machineRef.current.phase === 'postplay' && clamped < currentTime) dispatch({ type: 'DISMISS_POSTPLAY' });
    if (live.session) {
      const local = clamped - live.base;
      const inWindow = local >= 0 && seekableCovers(video, local) && local >= live.maxLocal - SERVER_BACK_WINDOW_S;
      if (inWindow) applySeek(video, local);
      else {
        // Outside what the server has (or still keeps): a new session there.
        begin(live.index, { position: clamped, reason: 'seek-restart', path: live.path, autoplay: !video.paused || !userPausedRef.current });
      }
      return;
    }
    applySeek(video, clamped);
  }, [begin, bestDuration, currentTime, dispatch]);

  // Arrow keys accelerate like the tvOS remote; the on-screen buttons stay 10 s.
  const arrowSeekRef = useRef({ at: 0, repeats: 0 });
  const arrowSeekStep = useCallback(() => {
    const at = Date.now();
    const state = arrowSeekRef.current;
    state.repeats = at - state.at < SEEK_REPEAT_WINDOW_MS ? Math.min(state.repeats + 1, 8) : 0;
    state.at = at;
    return directionalSeekStep(state.repeats, bestDuration());
  }, [bestDuration]);

  const flushSeek = useCallback(() => {
    seekFlushTimerRef.current = null;
    const target = pendingSeekRef.current;
    pendingSeekRef.current = null;
    if (target != null) seekToMovieTime(target);
  }, [seekToMovieTime]);

  // Coalesce rapid skip presses into one seek.
  const nudgeSeek = useCallback((delta: number) => {
    const video = videoRef.current;
    if (!video) return;
    const total = bestDuration();
    const movieNow = elementMovieTime();
    const base = pendingSeekRef.current ?? movieNow;
    const target = total > 0 ? clamp(base + delta, 0, total) : Math.max(0, base + delta);
    pendingSeekRef.current = target;
    if (total > 0) setProgress(clamp(target / total, 0, 1));
    setCurrentTime(target);
    showSeekHint(Math.round(target - movieNow), target);
    if (seekFlushTimerRef.current) window.clearTimeout(seekFlushTimerRef.current);
    seekFlushTimerRef.current = window.setTimeout(flushSeek, 250);
  }, [bestDuration, elementMovieTime, flushSeek, showSeekHint]);

  const requestPlay = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    noteInteraction();
    void togglePlay(video);
  }, [noteInteraction]);

  // ---- close / navigation ----

  const close = useCallback(() => {
    reporterRef.current?.report('close');
    presenceRef.current?.stop();
    sessionRef.current.discovery?.abort();
    sessionRef.current.recovery?.abort();
    clearRetryTimer();
    clearStartupTimer();
    cleanupPlayback(videoRef.current, hlsRef);
    stopServerSession();
    void releaseWakeLock();
    onClose();
  }, [clearRetryTimer, clearStartupTimer, onClose, releaseWakeLock, stopServerSession]);

  // An explicit retry: fresh discovery and budgets, same position.
  const retryFromError = useCallback(() => {
    noteInteraction();
    const session = sessionRef.current;
    session.recovery?.abort();
    session.triedPaths.clear();
    session.failureReasons.clear();
    session.ledger.resetForUserAction();
    stopServerSession();
    cleanupPlayback(videoRef.current, hlsRef);
    setStreams([]);
    setLookupToken((value) => value + 1);
  }, [noteInteraction, setStreams, stopServerSession]);

  const retryOtherQuality = useCallback((target: PlaybackQuality) => {
    noteInteraction();
    const session = sessionRef.current;
    session.triedPaths.clear();
    session.failureReasons.clear();
    session.planOverride.clear();
    session.ledger.resetForUserAction();
    stopServerSession();
    setStreams([]);
    setTier(target);
  }, [noteInteraction, setStreams, stopServerSession]);
  // The other Playback quality: Original when the server answered Optimized,
  // Optimized (a server transcode) when Original is what failed.
  const otherQuality: PlaybackQuality | null = resolvedQuality === 'optimized' ? 'original' : resolvedQuality === 'original' ? 'optimized' : null;
  const otherQualityAction = otherQuality
    ? { label: otherQuality === 'original' ? 'Try Original Quality' : 'Try Optimized Quality', run: () => retryOtherQuality(otherQuality) }
    : undefined;

  const selectSourceManually = useCallback((index: number) => {
    const stream = streamsRef.current[index];
    if (!stream) return;
    // Picking the source that is already playing fine is not a request to restart it.
    if (index === liveRef.current.index && liveRef.current.firstFrame && !liveRef.current.failed) return;
    noteInteraction();
    const session = sessionRef.current;
    session.recovery?.abort();
    // A manual pick starts a fresh incident but keeps the one refresh per
    // playback (native switchToSource); only Try Again resets that.
    session.ledger.closeIncident();
    session.lastFailoverReason = null;
    clearRetryTimer();
    clearStartupTimer();
    const position = currentPosition();
    session.triedPaths.delete(index);
    session.failureReasons.delete(index);
    session.planOverride.delete(index);
    session.verdicts.delete(streamUrl(stream));
    setFailureReasonsVersion((value) => value + 1);
    cleanupPlayback(videoRef.current, hlsRef);
    stopServerSession();
    dispatch({ type: 'RECOVER', note: 'Switching source' });
    begin(index, { position, reason: 'manual' });
  }, [begin, clearRetryTimer, clearStartupTimer, currentPosition, dispatch, noteInteraction, stopServerSession]);

  /** "Try another source": skip the current one now (a user action, not a budgeted failover). */
  const skipToNextSource = useCallback(() => {
    noteInteraction();
    const session = sessionRef.current;
    const live = liveRef.current;
    session.ledger.closeIncident();
    const position = currentPosition();
    markTried(live.index, live.path, 'Skipped manually');
    const { tried, triedRoutes } = exclusions();
    const next = selectNextSource({
      streams: streamsRef.current, tried, triedRoutes, currentIndex: live.index, verdicts: session.verdicts,
      planOf: (_stream, index) => planOf(index),
      excludedProviders: session.excludedProviders,
    });
    if (next === null) {
      setSettingsTab('sources');
      setSettingsOpen(true);
      return;
    }
    cleanupPlayback(videoRef.current, hlsRef);
    stopServerSession();
    dispatch({ type: 'RECOVER', note: 'Trying another source' });
    begin(next, { position, reason: 'manual' });
  }, [begin, currentPosition, dispatch, exclusions, markTried, noteInteraction, planOf, stopServerSession]);

  const startOver = useCallback(() => {
    const current = itemRef.current;
    if (!current) return;
    noteInteraction();
    clearSavedPlaybackState(current);
    setSettingsOpen(false);
    const live = liveRef.current;
    if (live.session || !videoRef.current) {
      begin(live.index, { position: 0, reason: 'start-over', path: live.path });
      return;
    }
    sessionRef.current.lastGood = 0;
    applySeek(videoRef.current, 0);
    void videoRef.current.play?.()?.catch?.(() => undefined);
  }, [begin, noteInteraction]);

  // ---- next episode / post-play ----

  const autoplayNext = useMemo(() => getPreferences().autoplayNext, [itemKey]); // eslint-disable-line react-hooks/exhaustive-deps
  const pipAllowed = useMemo(
    () => getPreferences().pip && typeof document !== 'undefined' && Boolean(document.pictureInPictureEnabled),
    [itemKey], // eslint-disable-line react-hooks/exhaustive-deps
  );
  const skipVideoId = useMemo(() => {
    const context = item ? episodeFingerprintContext(item) : null;
    return context ? `${context.imdb_id}:${context.season}:${context.episode}` : null;
  }, [item]);

  const remaining = duration > 0 ? duration - currentTime : Number.POSITIVE_INFINITY;
  // PlaybackPromptRules: next-episode marker, else credits marker, else 30 s before the end.
  const nextDue = shouldShowNextEpisode({
    hasNextEpisode: Boolean(nextEpisode),
    current: currentTime,
    nextEpisodeStart: markers.nextEpisodeStart,
    creditsStart: markers.creditsStart,
    duration,
    durationReliable: durationIsMediaDerived,
    mediaEnded,
  });
  const acceptFeedback = nextEpisodeAcceptFeedback(markers, currentTime);
  const showPostPlay = nextDue && !dismissedPrompts.has('next') && machine.phase !== 'error';

  const advanceToNext = useCallback((auto: boolean) => {
    const current = itemRef.current;
    if (!current || !nextEpisode || advancingRef.current) return;
    advancingRef.current = true;
    if (acceptFeedback && skipVideoId) {
      void sendSegmentFeedback(skipVideoId, { segment_type: acceptFeedback, feedback: 'correct' })?.catch?.(() => undefined);
    }
    reporterRef.current?.report('switch');
    autoAdvanceStreak = auto ? autoAdvanceStreak + 1 : 0;
    stopServerSession();
    markStartFromBeginning(nextEpisode);
    onPlay(nextEpisode);
  }, [acceptFeedback, nextEpisode, onPlay, skipVideoId, stopServerSession]);

  const playEpisode = useCallback((episode: EpisodeItem) => {
    const current = itemRef.current;
    if (!current) return;
    noteInteraction();
    reporterRef.current?.report('switch');
    stopServerSession();
    setSettingsOpen(false);
    onPlay(episodePlayItem(current, episode));
  }, [noteInteraction, onPlay, stopServerSession]);

  useEffect(() => {
    if (showPostPlay) dispatch({ type: 'POSTPLAY' });
    else if (machineRef.current.phase === 'postplay') dispatch({ type: 'DISMISS_POSTPLAY' });
  }, [showPostPlay, dispatch]);

  // 30-second countdown that holds while paused (PlaybackPromptRules); Watch
  // credits cancels it; Still watching? after 3 auto-advances.
  const countdownHeld = machine.paused && !mediaEnded;
  useEffect(() => {
    if (!showPostPlay || !autoplayNext || countdownCancelled) {
      setCountdown(null);
      return undefined;
    }
    setCountdown((value) => value ?? UP_NEXT_COUNTDOWN_SECONDS);
    if (countdownHeld) return undefined;
    const timer = window.setInterval(() => {
      setCountdown((value) => (value == null ? value : Math.max(0, value - 1)));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [showPostPlay, autoplayNext, countdownCancelled, countdownHeld]);

  useEffect(() => {
    if (countdown !== 0) return;
    if (autoAdvanceStreak >= STILL_WATCHING_AFTER) {
      setStillWatching(true);
      setCountdownCancelled(true);
      videoRef.current?.pause();
      return;
    }
    advanceToNext(true);
  }, [countdown, advanceToNext]);

  // Resolve + inspect the next episode's sources ahead of the end.
  useEffect(() => {
    if (!nextEpisode || !(remaining <= NEXT_PREFETCH_LEAD_S)) return undefined;
    const key = playbackProgressKey(nextEpisode);
    if (prefetchedNextRef.current === key) return undefined;
    prefetchedNextRef.current = key;
    // The next episode starts from its beginning: the desktop engine prewarms it there.
    const cancel = prefetchPlayback(nextEpisode, { tier, profileId, start: 0, audioLanguage: getPreferences().audioLanguage });
    return () => { if (prefetchedNextRef.current !== key) cancel(); };
  }, [nextEpisode, remaining, tier, profileId]);

  useEffect(() => () => {
    if (longPauseTimerRef.current != null) window.clearTimeout(longPauseTimerRef.current);
  }, []);

  // ---- media session ----

  useEffect(() => {
    if (!item) return undefined;
    updateMediaSession(item, videoRef.current, isPlaying, liveRef.current.base, seekToMovieTime, nextEpisode ? () => advanceToNext(false) : null);
    return () => {
      if ('mediaSession' in navigator) navigator.mediaSession.metadata = null;
    };
  }, [isPlaying, item, seekToMovieTime, nextEpisode, advanceToNext, attempt]);

  // ---- picture in picture ----

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return undefined;
    const enter = () => setPipActive(true);
    const leave = () => setPipActive(false);
    video.addEventListener('enterpictureinpicture', enter);
    video.addEventListener('leavepictureinpicture', leave);
    return () => {
      video.removeEventListener('enterpictureinpicture', enter);
      video.removeEventListener('leavepictureinpicture', leave);
    };
  }, [item]);

  const togglePip = useCallback(async () => {
    const video = videoRef.current;
    try {
      if (document.pictureInPictureElement) await document.exitPictureInPicture();
      else if (video) await video.requestPictureInPicture();
    } catch {
      // Not allowed for this source yet.
    }
  }, []);

  // ---- skip intro / credits ----

  const current = currentTime;
  const insideIntro = markers.introStart != null && markers.introEnd != null
    && current >= Math.max(0, markers.introStart - 2) && current < markers.introEnd;
  const showSkipIntro = insideIntro && !dismissedPrompts.has('intro');
  // Recap markers only exist while ENABLE_RECAP_SEGMENTS is on.
  const showSkipRecap = !showSkipIntro && markers.recapStart != null && markers.recapEnd != null
    && current >= markers.recapStart && current < markers.recapEnd && !dismissedPrompts.has('recap');
  const showSkipCredits = !nextEpisode && markers.creditsStart != null && markers.creditsEnd != null
    && current >= markers.creditsStart && current < markers.creditsEnd - 1 && !dismissedPrompts.has('credits');
  const dismissPrompt = useCallback((key: string) => setDismissedPrompts((set) => new Set(set).add(key)), []);
  const skipSegment = useCallback((type: 'intro' | 'credits' | 'recap', end: number | undefined) => {
    if (end == null) return;
    noteInteraction();
    seekToMovieTime(end);
    // The server takes feedback for intro and credits only.
    if (skipVideoId && type !== 'recap') void sendSegmentFeedback(skipVideoId, { segment_type: type, feedback: 'correct' })?.catch?.(() => undefined);
  }, [noteInteraction, seekToMovieTime, skipVideoId]);

  // ---- keyboard ----

  // What the Audio menu lists: the file's tracks for a prepared session, else
  // whatever hls.js / the browser exposes.
  const audioOptions = useMemo(
    () => (preparedAudio ? fileAudioOptions(preparedAudio.tracks, preparedAudio.selected) : audioTracks),
    [preparedAudio, audioTracks],
  );

  // The file has subtitles, but only image-based ones (PGS/VobSub), which can't be shown yet.
  const imageSubtitlesOnly = useMemo(() => {
    const stream = streams[sourceIndex];
    const known = stream ? peekMediaFacts(streamUrl(stream))?.subtitles ?? [] : [];
    return known.length > 0 && known.every((track) => track.text === false) && subtitleTracks.length <= 1;
  }, [streams, sourceIndex, subtitleTracks]);

  const chooseAudio = useCallback((id: string) => {
    if (!id.startsWith('file:')) {
      selectAudioTrack(id, videoRef.current, hlsRef.current, setAudioTracks, setSubtitleTracks);
      return;
    }
    const live = liveRef.current;
    const stream = streamsRef.current[live.index];
    const index = Number(id.slice(5));
    if (!stream || !Number.isInteger(index) || preparedAudio?.selected === index) return;
    noteInteraction();
    // The session carries one audio track: prepare it again with the chosen one, from here.
    audioChoiceRef.current = { url: streamUrl(stream), index };
    setPreparedAudio((value) => (value ? { ...value, selected: index } : value));
    const video = videoRef.current;
    begin(live.index, { position: currentPosition(), reason: 'audio', path: live.path, autoplay: !video?.paused || !userPausedRef.current });
  }, [begin, currentPosition, noteInteraction, preparedAudio]);

  const cycleTrack = useCallback((kind: 'audio' | 'subtitles') => {
    const list = kind === 'audio' ? audioOptions : subtitleTracks;
    if (list.length < 2) return;
    const index = list.findIndex((track) => track.selected);
    const next = list[(index + 1) % list.length];
    if (kind === 'audio') chooseAudio(next.id);
    else selectSubtitleTrack(next.id, videoRef.current, hlsRef.current, setAudioTracks, setSubtitleTracks);
    showToast(`${kind === 'audio' ? 'Audio' : 'Subtitles'}: ${pillLabel(next, list)}`);
  }, [audioOptions, chooseAudio, subtitleTracks, showToast]);

  const changeSpeed = useCallback((direction: 1 | -1) => {
    setSpeed((value) => {
      const index = SPEEDS.findIndex((entry) => entry === value);
      const next = SPEEDS[clamp((index < 0 ? 2 : index) + direction, 0, SPEEDS.length - 1)];
      showToast(`Speed ${next}×`);
      return next;
    });
  }, [showToast]);

  useEffect(() => {
    if (!item) return undefined;
    const onKey = (event: KeyboardEvent) => {
      const video = videoRef.current;
      if (event.key === 'Escape') {
        event.preventDefault();
        if (settingsOpenRef.current) setSettingsOpen(false);
        else if (document.fullscreenElement) void document.exitFullscreen?.().catch?.(() => undefined);
        else close();
        return;
      }
      if (settingsOpenRef.current || resumePromptRef.current != null) return;
      if (['INPUT', 'SELECT', 'TEXTAREA'].includes((document.activeElement?.tagName ?? '').toUpperCase())) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      let handled = true;
      const key = event.key;
      if (key === ' ' || key === 'k' || key === 'K') requestPlay();
      else if (key === 'ArrowLeft' || key === 'j' || key === 'J') nudgeSeek(-arrowSeekStep());
      else if (key === 'ArrowRight' || key === 'l' || key === 'L') nudgeSeek(arrowSeekStep());
      else if (key === 'ArrowUp') { setVolume((value) => clamp(Math.round((value + 0.1) * 10) / 10, 0, 1)); setMuted(false); }
      else if (key === 'ArrowDown') setVolume((value) => clamp(Math.round((value - 0.1) * 10) / 10, 0, 1));
      else if (key === 'f' || key === 'F') void toggleFullscreen(playerRef.current);
      else if (key === 'm' || key === 'M') setMuted((value) => !value);
      else if (key === 'c' || key === 'C') cycleTrack('subtitles');
      else if (key === 'a' || key === 'A') cycleTrack('audio');
      else if ((key === 's' || key === 'S') && (showSkipIntro || showSkipCredits)) {
        if (showSkipIntro) skipSegment('intro', markers.introEnd);
        else skipSegment('credits', markers.creditsEnd);
      } else if (key === 'N' && event.shiftKey && nextEpisode) advanceToNext(false);
      else if (/^[0-9]$/.test(key)) {
        const total = bestDuration();
        if (total > 0) seekToMovieTime(total * (Number(key) / 10));
      } else if ((key === ',' || key === '.') && video?.paused) {
        applySeek(video, Math.max(0, video.currentTime + (key === '.' ? 1 / 24 : -1 / 24)));
      } else if (key === '<') changeSpeed(-1);
      else if (key === '>') changeSpeed(1);
      else if ((key === 'i' || key === 'I') && pipAllowed) void togglePip();
      else handled = false;
      if (handled) {
        event.preventDefault();
        noteInteraction();
        showChrome();
      }
    };
    // Capture phase: the player owns its keys ahead of the app's stack handler.
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [
    item, close, requestPlay, nudgeSeek, cycleTrack, showSkipIntro, showSkipCredits, skipSegment, markers,
    nextEpisode, advanceToNext, bestDuration, seekToMovieTime, changeSpeed, pipAllowed, togglePip, noteInteraction, showChrome,
  ]);

  // Click toggles play at once (a double-click's second click undoes it) and
  // a double-click toggles fullscreen.
  const onVideoClick = useCallback(() => {
    requestPlay();
    showChrome();
  }, [requestPlay, showChrome]);
  const onVideoDoubleClick = useCallback(() => {
    void toggleFullscreen(playerRef.current);
  }, []);

  if (!item) return null;

  // ---- derived UI ----

  const baseUi = loadingUi(machine, now);
  // Native tier 2: a source that keeps stalling says so while it buffers.
  const ui = machine.phase === 'buffering' && baseUi.spinner && isStruggling(chronicRef.current.recent(now))
    ? { ...baseUi, label: STRUGGLING_LABEL }
    : baseUi;
  const loadingVisible = isLoadingPhase(machine.phase);
  const startup = loadingVisible && !machine.startedOnce;
  const backdrop = itemImage(item, false, 'original');
  const logo = logoImage(item);
  const sourceMenuLabel = (stream: PlaybackSource | undefined) => (stream ? stream.label || providerLabel(stream) || 'Source' : 'Source');
  void failureReasonsVersion;
  const isSeries = mediaType(item) === 'series';
  const episodesInSeason = (seasons.find((entry) => String(entry.season) === String(item.season))?.episodes ?? []);
  const titleLine = displayTitle(item);
  const episodeLine = episodeLabel(item);

  return (
    <section
      ref={playerRef}
      className={[
        styles.player,
        chromeVisible || machine.paused || settingsOpen ? '' : styles.chromeHidden,
        settingsOpen ? styles.settingsOpen : '',
        showPostPlay ? styles.postplay : '',
      ].join(' ')}
      role="dialog"
      aria-modal="true"
      aria-label={`Playing ${displayTitle(item)}`}
      onMouseMove={showChrome}
      onMouseDown={showChrome}
    >
      <video
        ref={videoRef}
        className={`${styles.video} ${fit === 'fill' ? styles.videoFill : ''}`}
        playsInline
        preload="auto"
        onClick={onVideoClick}
        onDoubleClick={onVideoDoubleClick}
        onTimeUpdate={() => {
          const video = videoRef.current;
          const live = liveRef.current;
          if (!video || !Number.isFinite(video.currentTime)) return;
          const movieTime = live.base + video.currentTime;
          const hold = holdPositionRef.current;
          if (hold && (hold.token !== live.token || (video.readyState >= 2 && video.currentTime > 0 && movieTime >= hold.position - 1))) {
            holdPositionRef.current = null;
          }
          if (pendingSeekRef.current == null && !holdPositionRef.current) {
            setCurrentTime(movieTime);
            syncTimeline();
          }
          if (video.currentTime > 0) noteSignalOfLife(live.token);
          live.maxLocal = Math.max(live.maxLocal, video.currentTime);
          if (!video.paused && movieTime > 0) {
            sessionRef.current.lastGood = movieTime;
            if (movieTime - live.startPosition > 0.5) live.hadPlayed = true;
          }
          // Silent-picture guard: a direct source whose audio the browser can't
          // decode plays with no sound and no error; switch it to the prepared path.
          if (live.path === 'direct' && live.playingSeen && !live.audioChecked && movieTime - live.startPosition > 3) {
            live.audioChecked = true;
            const known = peekMediaFacts(streamUrl(streams[live.index]));
            if (playsSilently(video as HTMLVideoElement & { webkitAudioDecodedByteCount?: number }, known ? known.audio.length : null)) {
              handleSourceFailure(live.token, { kind: 'no-audio' });
            }
          }
          // Audio-only guard: playing for a while with no video frame at all.
          if (live.playingSeen && !live.framesChecked && movieTime - live.startPosition > 1.5) {
            live.framesChecked = true;
            if (video.videoWidth === 0 && video.videoHeight === 0) {
              handleSourceFailure(live.token, { kind: 'no-frames' });
            }
          }
        }}
        onProgress={() => {
          watchdogRef.current.noteBytes(Date.now());
          if (pendingSeekRef.current == null) {
            syncTimeline();
          }
        }}
        onDurationChange={() => {
          if (pendingSeekRef.current == null) {
            syncTimeline();
          }
        }}
        onLoadedMetadata={() => {
          const live = liveRef.current;
          noteSignalOfLife(live.token);
          syncTracks();
          syncTimeline();
          if (!holdPositionRef.current) setCurrentTime(live.base + (videoRef.current?.currentTime ?? 0));
        }}
        onCanPlay={() => {
          const video = videoRef.current;
          noteSignalOfLife(liveRef.current.token);
          syncTracks();
          if (video && (video.videoWidth > 0 || video.videoHeight > 0) && !video.paused) noteFirstFrame(liveRef.current.token);
        }}
        onWaiting={() => {
          const video = videoRef.current;
          const live = liveRef.current;
          if (!video || video.paused) return;
          if (live.sawSignal) presenceRef.current?.transition('buffering');
          const at = Date.now();
          // Startup, resume-from-pause and seek waits are not rebuffers.
          const steady = machineRef.current.phase === 'playing' && live.hadPlayed
            && at > seekGraceUntilRef.current && at > resumeGraceUntilRef.current;
          dispatch({ type: 'WAITING' });
          if (rebufferRef.current.note(at, steady)) {
            handleSourceFailure(live.token, { kind: 'rebuffer' });
            return;
          }
          if (steady) {
            chronicRef.current.open(at);
            checkChronicBuffering(live.token, at);
          }
        }}
        onStalled={() => {
          const video = videoRef.current;
          if (video && !video.paused && liveRef.current.sawSignal) presenceRef.current?.transition('buffering');
        }}
        onSeeking={() => {
          const at = Date.now();
          chronicRef.current.discardOpen();
          watchdogRef.current.noteSeek(at);
          seekGraceUntilRef.current = at + SEEK_GRACE_MS;
          const video = videoRef.current;
          if (video && !video.paused && liveRef.current.sawSignal) presenceRef.current?.transition('buffering');
          setCurrentTime(liveRef.current.base + (video?.currentTime ?? 0));
        }}
        onSeeked={() => {
          watchdogRef.current.reset(Date.now());
          reporterRef.current?.report('seek');
        }}
        onPlay={() => {
          const live = liveRef.current;
          const pausedAt = pausedAtRef.current;
          pausedAtRef.current = null;
          const held = heldFailureRef.current;
          heldFailureRef.current = null;
          if (!userPausedRef.current) return;
          // Play was pressed: from here on failures are handled, not held.
          userPausedRef.current = false;
          if (held && held.token === live.token) {
            handleSourceFailure(held.token, held.signal);
            return;
          }
          if (pausedAt == null) return;
          const pausedForMs = Date.now() - pausedAt;
          if (pausedForMs < LONG_PAUSE_MS) return;
          // A long pause can outlive the connection or server session: if the
          // playhead doesn't move shortly after Play, reload at the position.
          const token = live.token;
          const positionAtPlay = currentPosition();
          if (longPauseTimerRef.current != null) window.clearTimeout(longPauseTimerRef.current);
          longPauseTimerRef.current = window.setTimeout(() => {
            longPauseTimerRef.current = null;
            const now = liveRef.current;
            const video = videoRef.current;
            if (now.token !== token || now.failed || !video) return;
            if (!shouldReloadAfterLongPause({
              pausedForMs,
              positionAtPlay,
              positionNow: currentPosition(),
              stillPaused: video.paused,
            })) return;
            playbackTrace('long pause: no progress after Play, reloading', { position: positionAtPlay });
            begin(now.index, { position: positionAtPlay, reason: 'long-pause', path: now.path, viaProxy: now.viaProxy, autoplay: true });
          }, LONG_PAUSE_PROBE_MS);
        }}
        onPlaying={() => {
          const video = videoRef.current;
          const live = liveRef.current;
          live.playingSeen = true;
          userPausedRef.current = false;
          autoplayBlockedRef.current = false;
          setAutoplayBlocked(false);
          setIsPlaying(true);
          const at = Date.now();
          watchdogRef.current.reset(at);
          resumeGraceUntilRef.current = at + SEEK_GRACE_MS;
          // A stall ending is the other edge the chronic rule is judged on.
          chronicRef.current.close(at);
          checkChronicBuffering(live.token, at);
          presenceRef.current?.transition('playing');
          if (video && (video.videoWidth > 0 || video.videoHeight > 0)) noteFirstFrame(live.token);
          dispatch({ type: 'PLAYING' });
          scheduleHide();
        }}
        onPause={() => {
          const live = liveRef.current;
          // Pauses issued while tearing a source down are not the viewer's.
          if (!live.playingSeen || live.failed) return;
          userPausedRef.current = true;
          chronicRef.current.discardOpen();
          watchdogRef.current.reset(Date.now());
          setIsPlaying(false);
          dispatch({ type: 'PAUSE' });
          presenceRef.current?.transition('paused');
          reporterRef.current?.report('pause');
          pausedAtRef.current = Date.now();
          if (longPauseTimerRef.current != null) {
            window.clearTimeout(longPauseTimerRef.current);
            longPauseTimerRef.current = null;
          }
          setChromeVisible(true);
        }}
        onEnded={() => {
          const live = liveRef.current;
          const video = videoRef.current;
          const position = live.base + (video?.currentTime ?? 0);
          const total = bestDuration();
          const complete = total > 0 ? isCompletion(position, total, markers.creditsStart) : true;
          if (!complete) {
            // Premature EOF: recover from here; never mark watched or autoplay.
            handleSourceFailure(live.token, { kind: 'premature-end', position, duration: total });
            return;
          }
          setCurrentTime(position);
          setIsPlaying(false);
          setMediaEnded(true);
          dispatch({ type: 'ENDED' });
          presenceRef.current?.transition('ended');
          reporterRef.current?.report('ended');
          stopServerSession();
          setChromeVisible(true);
          if (autoplayNext && nextEpisode && !countdownCancelled) setCountdown(0);
        }}
        onError={() => {
          // hls.js owns error handling (and recovery) for the sources it drives.
          if (hlsRef.current) return;
          const code = videoRef.current?.error?.code ?? null;
          handleSourceFailure(liveRef.current.token, { kind: 'media-error', code, message: videoRef.current?.error?.message });
        }}
      />

      {startup ? (
        <div className={styles.startup} aria-live="polite">
          {backdrop ? <img className={styles.startupArt} src={backdrop} alt="" decoding="async" /> : null}
          <div className={styles.startupShade} />
          <div className={styles.startupContent}>
            {logo ? <img className={styles.startupLogo} src={logo} alt={titleLine} /> : <h2 className={styles.startupTitle}>{titleLine}</h2>}
            {episodeLine ? <span className={styles.startupEpisode}>{episodeLine}</span> : null}
          </div>
        </div>
      ) : null}

      {loadingVisible && ui.spinner ? (
        <div className={styles.spinner} role="status">
          <div className={styles.bufferRingWrap}><BufferRing /></div>
          {ui.label ? <span className={styles.spinnerLabel}>{ui.label}</span> : null}
          {ui.stillLoading ? (
            <div className={styles.stillLoading}>
              <span>{STILL_WORKING_LABEL}</span>
              <button className={styles.ghostButton} type="button" onClick={skipToNextSource}>Try another source</button>
            </div>
          ) : null}
        </div>
      ) : null}

      {autoplayBlocked && !isPlaying && !loadingVisible ? <div className={styles.autoplayHint}>Tap play to start</div> : null}
      {seekHint ? <div className={styles.seekIndicator}>{seekHint}</div> : null}
      {toast ? <div className={styles.toast} role="status">{toast}</div> : null}

      {machine.phase === 'error' && machine.error ? (
        <ErrorScreen
          backdrop={backdrop}
          message={machine.error.message}
          code={machine.error.code}
          playbackRef={presenceRef.current?.sessionId ?? ''}
          onRetry={retryFromError}
          onAnother={() => { setSettingsTab('sources'); setSettingsOpen(true); }}
          onBack={close}
          otherQuality={otherQualityAction}
          onChooseSource={onChooseSource && streamsRef.current.length > 1 ? () => { stopServerSession(); onChooseSource(item); } : undefined}
        />
      ) : null}

      {resumePrompt != null && machine.phase !== 'error' ? (
        <div
          className={styles.resumeGate}
          role="dialog"
          aria-modal="true"
          aria-labelledby="resume-gate-title"
          onKeyDown={(event) => {
            if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
            event.preventDefault();
            const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>(`.${styles.resumeCard} button`));
            const next = buttons.indexOf(document.activeElement as HTMLButtonElement) + (event.key === 'ArrowDown' ? 1 : -1);
            buttons[clamp(next, 0, buttons.length - 1)]?.focus();
          }}
        >
          <button type="button" className={styles.resumeClose} aria-label="Close" onClick={close}><X size={18} /></button>
          <div className={styles.resumeCard}>
            <strong id="resume-gate-title">Would you like to resume or watch from the beginning?</strong>
            <button type="button" className={styles.resumePrimary} autoFocus onClick={() => answerResume(resumePrompt)}>
              Resume Playing ({formatTime(resumePrompt)})
            </button>
            <button type="button" onClick={() => answerResume(0)}>Start from Beginning</button>
          </div>
        </div>
      ) : null}

      <div className={styles.prompts}>
        {showSkipIntro ? (
          <div className={styles.promptGroup}>
            <button className={styles.actionChip} type="button" onClick={() => skipSegment('intro', markers.introEnd)}><SkipForward size={18} />Skip Intro</button>
            <button className={styles.promptDismiss} type="button" onClick={() => dismissPrompt('intro')} aria-label="Dismiss Skip Intro"><X size={16} /></button>
          </div>
        ) : null}
        {showSkipRecap ? (
          <div className={styles.promptGroup}>
            <button className={styles.actionChip} type="button" onClick={() => skipSegment('recap', markers.recapEnd)}><SkipForward size={18} />Skip Recap</button>
            <button className={styles.promptDismiss} type="button" onClick={() => dismissPrompt('recap')} aria-label="Dismiss Skip Recap"><X size={16} /></button>
          </div>
        ) : null}
        {showSkipCredits ? (
          <div className={styles.promptGroup}>
            <button className={styles.actionChip} type="button" onClick={() => skipSegment('credits', markers.creditsEnd)}><SkipForward size={18} />Skip Credits</button>
            <button className={styles.promptDismiss} type="button" onClick={() => dismissPrompt('credits')} aria-label="Dismiss Skip Credits"><X size={16} /></button>
          </div>
        ) : null}
      </div>

      {showPostPlay && nextEpisode ? (
        <PostPlayCard
          next={nextEpisode}
          countdown={autoplayNext && !countdownCancelled ? countdown : null}
          stillWatching={stillWatching}
          onNext={() => { noteInteraction(); advanceToNext(false); }}
          onWatchCredits={() => { noteInteraction(); dismissPrompt('next'); }}
          onContinue={() => { noteInteraction(); setStillWatching(false); advanceToNext(false); }}
          onBack={close}
        />
      ) : null}

      <div
        className={styles.top}
        onMouseEnter={() => { hoveringControlsRef.current = true; }}
        onMouseLeave={() => { hoveringControlsRef.current = false; scheduleHide(); }}
      >
        <button className={styles.roundButton} type="button" onClick={close} aria-label="Close player"><X size={22} /></button>
        <div className={styles.topActions}>
          {pipAllowed ? (
            <button className={styles.roundButton} type="button" onClick={() => void togglePip()} aria-label={pipActive ? 'Exit Picture in Picture' : 'Picture in Picture'} aria-pressed={pipActive}>
              <PictureInPicture2 size={20} />
            </button>
          ) : null}
          <button
            className={styles.roundButton}
            type="button"
            onClick={() => setFit((value) => (value === 'fit' ? 'fill' : 'fit'))}
            aria-label={fit === 'fit' ? 'Fill screen' : 'Fit to screen'}
          >
            {fit === 'fit' ? <Expand size={19} /> : <Shrink size={19} />}
          </button>
          <button className={styles.roundButton} type="button" onClick={() => { setSettingsTab('sources'); setSettingsOpen(true); }} aria-label="Playback settings"><MoreHorizontal size={22} /></button>
        </div>
      </div>

      <div
        className={styles.bottom}
        onMouseEnter={() => { hoveringControlsRef.current = true; }}
        onMouseLeave={() => { hoveringControlsRef.current = false; scheduleHide(); }}
      >
        <div className={styles.titleLine}>
          {episodeLine ? <span>{episodeLine}</span> : null}
          <strong>{titleLine}</strong>
        </div>
        <ScrubBar
          progress={progress}
          buffered={buffered}
          current={current}
          duration={duration}
          markers={markers}
          onScrubbing={(active) => { scrubbingRef.current = active; if (!active) scheduleHide(); }}
          onCommit={(ratio) => {
            noteInteraction();
            const total = bestDuration();
            if (total > 0) seekToMovieTime(ratio * total);
          }}
        />
        <div className={styles.controlRow}>
          <div className={styles.controlGroup}>
            <button className={styles.controlButton} type="button" onClick={requestPlay} aria-label={isPlaying ? 'Pause' : 'Play'}>
              {isPlaying ? <Pause size={26} fill="currentColor" /> : <Play size={26} fill="currentColor" />}
            </button>
            <button className={styles.controlButton} type="button" onClick={() => { noteInteraction(); nudgeSeek(-SEEK_STEP_SECONDS); }} aria-label="Rewind 10 seconds"><RotateCcw size={24} /></button>
            <button className={styles.controlButton} type="button" onClick={() => { noteInteraction(); nudgeSeek(SEEK_STEP_SECONDS); }} aria-label="Forward 10 seconds"><RotateCw size={24} /></button>
            <div className={styles.volume}>
              <button className={styles.controlButton} type="button" onClick={() => setMuted((value) => !value)} aria-label={muted ? 'Unmute' : 'Mute'}>
                {muted || volume === 0 ? <VolumeX size={22} /> : <Volume2 size={22} />}
              </button>
              <input
                className={styles.volumeSlider}
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={muted ? 0 : volume}
                onChange={(event: ChangeEvent<HTMLInputElement>) => {
                  const next = Number(event.target.value);
                  setVolume(next);
                  setMuted(next <= 0);
                }}
                aria-label="Volume"
              />
            </div>
            <span className={styles.timeLabel}>{formatTime(current)} / {duration > 0 ? formatTime(duration) : '--:--'}</span>
          </div>
          <div className={styles.controlGroup}>
            {nextEpisode ? (
              <button className={styles.controlButton} type="button" onClick={() => { noteInteraction(); advanceToNext(false); }} aria-label="Play next episode" title="Next episode (Shift+N)"><SkipForward size={22} /></button>
            ) : null}
            {isSeries && episodesInSeason.length ? (
              <button className={styles.controlButton} type="button" onClick={() => { setSettingsTab('episodes'); setSettingsOpen(true); }} aria-label="Episodes"><ListVideo size={22} /></button>
            ) : null}
            <button className={styles.controlButton} type="button" onClick={() => { setSettingsTab('audio'); setSettingsOpen(true); }} aria-label="Audio and subtitles"><Captions size={22} /></button>
            <button className={styles.controlButton} type="button" onClick={() => { setSettingsTab('speed'); setSettingsOpen(true); }} aria-label={`Playback speed ${speed}×`}><Gauge size={22} /></button>
            <button className={styles.controlButton} type="button" onClick={() => void toggleFullscreen(playerRef.current)} aria-label={isFullscreen ? 'Exit full screen' : 'Full screen'}>
              {isFullscreen ? <Minimize size={22} /> : <Maximize size={22} />}
            </button>
          </div>
        </div>
      </div>

      {settingsOpen ? (
        <PlaybackSettings
          heading={[titleLine, episodeLine].filter(Boolean).join(' - ')}
          streams={streams}
          sourceIndex={sourceIndex}
          activePath={attempt?.path ?? null}
          setSourceIndex={selectSourceManually}
          failureReasons={sessionRef.current.failureReasons}
          planKindOf={(index) => planOf(index).kind}
          tab={settingsTab}
          setTab={setSettingsTab}
          onClose={() => setSettingsOpen(false)}
          audioTracks={audioOptions}
          preparedLocally={preparedLocally}
          imageSubtitlesOnly={imageSubtitlesOnly}
          subtitleTracks={subtitleTracks}
          fit={fit}
          setFit={setFit}
          speed={speed}
          setSpeed={setSpeed}
          episodes={isSeries ? episodesInSeason : []}
          currentEpisode={item.episode == null ? null : Number(item.episode)}
          onEpisode={playEpisode}
          onStartOver={startOver}
          otherQuality={otherQualityAction}
          sourceLabel={sourceMenuLabel(streams[sourceIndex])}
          onAudioTrack={chooseAudio}
          onSubtitleTrack={(id) => selectSubtitleTrack(id, videoRef.current, hlsRef.current, setAudioTracks, setSubtitleTracks)}
        />
      ) : null}
    </section>
  );
}

function ErrorScreen({
  backdrop,
  message,
  code,
  playbackRef,
  onRetry,
  onAnother,
  onBack,
  otherQuality,
  onChooseSource,
}: {
  backdrop: string;
  message: string;
  code: string;
  playbackRef: string;
  onRetry: () => void;
  onAnother: () => void;
  onBack: () => void;
  otherQuality?: { label: string; run: () => void };
  onChooseSource?: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const playbackIdLabel = playbackRef ? playbackRef.slice(0, 8).toUpperCase() : '';
  return (
    <div className={styles.errorScreen} role="alert">
      {backdrop ? <img className={styles.startupArt} src={backdrop} alt="" decoding="async" /> : null}
      <div className={styles.errorShade} />
      <div className={styles.errorContent}>
        <strong className={styles.errorTitle}><TriangleAlert size={28} aria-hidden="true" /> Playback Failed</strong>
        <p className={styles.errorMessage}>{message}</p>
        <span className={styles.errorCode}>{code}</span>
        <div className={styles.errorActions}>
          {/* tvOS: Retry, Choose Source (with more than one source), Close. */}
          <button className={styles.primaryButton} type="button" onClick={onRetry}><RotateCw size={18} aria-hidden="true" /> Retry</button>
          {onChooseSource ? <button className={styles.ghostButton} type="button" onClick={onChooseSource}>Choose Source</button> : null}
          <button className={styles.ghostButton} type="button" onClick={onBack}>Close</button>
        </div>
        <div className={styles.errorLinks}>
          {otherQuality ? <button className={styles.linkButton} type="button" onClick={otherQuality.run}>{otherQuality.label}</button> : null}
          {playbackIdLabel ? (
            <button
              className={styles.linkButton}
              type="button"
              aria-label={`Copy playback ID ${playbackIdLabel}`}
              onClick={() => {
                void navigator.clipboard?.writeText(`${code} ${playbackRef}`).then(() => setCopied(true)).catch(() => undefined);
              }}
            >
              <Copy size={14} /> {copied ? 'Copied' : `Playback ID ${playbackIdLabel}`}
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function PostPlayCard({
  next,
  countdown,
  stillWatching,
  onNext,
  onWatchCredits,
  onContinue,
  onBack,
}: {
  next: WebMediaItem;
  countdown: number | null;
  stillWatching: boolean;
  onNext: () => void;
  onWatchCredits: () => void;
  onContinue: () => void;
  onBack: () => void;
}) {
  const art = itemImage(next, false, 'w780');
  const synopsis = String(next.overview ?? next.description ?? '');
  if (stillWatching) {
    return (
      <div className={styles.postPlayCard} role="dialog" aria-label="Still watching?">
        <div className={styles.postPlayBody}>
          <strong className={styles.postPlayTitle}>Are you still watching?</strong>
          <div className={styles.postPlayActions}>
            <button className={styles.primaryButton} type="button" onClick={onContinue}>Continue Watching</button>
            <button className={styles.ghostButton} type="button" onClick={onBack}>Back</button>
          </div>
        </div>
      </div>
    );
  }
  return (
    <div className={styles.postPlayCard} aria-label="Up next">
      {art ? <img className={styles.postPlayArt} src={art} alt="" decoding="async" /> : null}
      <div className={styles.postPlayBody}>
        <span className={styles.postPlayEyebrow}>Up Next · {episodeLabel(next)}</span>
        <strong className={styles.postPlayTitle}>{String(next.name ?? next.subtitle ?? displayTitle(next))}</strong>
        {synopsis ? <p className={styles.postPlaySynopsis}>{synopsis}</p> : null}
        <div className={styles.postPlayActions}>
          <button className={styles.primaryButton} type="button" onClick={onNext}>
            {countdown != null ? <CountdownRing seconds={countdown} total={UP_NEXT_COUNTDOWN_SECONDS} /> : <Play size={18} fill="currentColor" />}
            Next Episode
          </button>
          <button className={styles.ghostButton} type="button" onClick={onWatchCredits}>Watch credits</button>
        </div>
      </div>
    </div>
  );
}

function CountdownRing({ seconds, total }: { seconds: number; total: number }) {
  const radius = 9;
  const circumference = 2 * Math.PI * radius;
  const fraction = total > 0 ? clamp(seconds / total, 0, 1) : 0;
  return (
    <svg className={styles.countdown} viewBox="0 0 24 24" width={22} height={22} aria-label={`${seconds} seconds`}>
      <circle cx="12" cy="12" r={radius} fill="none" stroke="currentColor" strokeOpacity={0.25} strokeWidth={2.5} />
      <circle
        cx="12" cy="12" r={radius} fill="none" stroke="currentColor" strokeWidth={2.5}
        strokeDasharray={circumference} strokeDashoffset={circumference * (1 - fraction)}
        strokeLinecap="round" transform="rotate(-90 12 12)"
      />
      <text x="12" y="15.5" textAnchor="middle" fontSize="9" fontWeight="700" fill="currentColor">{seconds}</text>
    </svg>
  );
}

// Preview while dragging (with a time bubble), commit the seek on release.
function ScrubBar({
  progress,
  buffered,
  current,
  duration,
  markers,
  onCommit,
  onScrubbing,
}: {
  progress: number;
  buffered: number;
  current: number;
  duration: number;
  markers: PlayerMarkers;
  onCommit: (ratio: number) => void;
  onScrubbing: (active: boolean) => void;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [dragRatio, setDragRatio] = useState<number | null>(null);
  const [hoverRatio, setHoverRatio] = useState<number | null>(null);
  const ratioFromEvent = (event: ReactPointerEvent<HTMLDivElement>) => {
    const rect = ref.current?.getBoundingClientRect();
    if (!rect || rect.width <= 0) return 0;
    return clamp((event.clientX - rect.left) / rect.width, 0, 1);
  };
  const shown = dragRatio ?? progress;
  const bubbleRatio = dragRatio ?? hoverRatio;
  const timeAt = (ratio: number) => (duration > 0 ? ratio * duration : current);
  const mark = (start?: number, end?: number) => {
    if (start == null || duration <= 0) return null;
    const left = clamp(start / duration, 0, 1) * 100;
    const width = end != null ? Math.max(0.4, clamp((end - start) / duration, 0, 1) * 100) : 0.4;
    return { left: `${left}%`, width: `${width}%` };
  };
  const intro = mark(markers.introStart, markers.introEnd);
  const credits = mark(markers.creditsStart, markers.creditsEnd);
  return (
    <div className={styles.scrubWrap}>
      <div
        ref={ref}
        className={`${styles.scrub} ${dragRatio !== null ? styles.scrubActive : ''}`}
        role="slider"
        tabIndex={0}
        aria-label="Playback progress"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(shown * 100)}
        aria-valuetext={`${formatTime(timeAt(shown))} of ${duration > 0 ? formatTime(duration) : 'unknown'}`}
        onPointerDown={(event) => {
          ref.current?.setPointerCapture?.(event.pointerId);
          setDragRatio(ratioFromEvent(event));
          onScrubbing(true);
        }}
        onPointerMove={(event) => {
          const ratio = ratioFromEvent(event);
          if (dragRatio !== null) setDragRatio(ratio);
          else setHoverRatio(ratio);
        }}
        onPointerLeave={() => setHoverRatio(null)}
        onPointerUp={(event) => {
          if (dragRatio === null) return;
          const ratio = ratioFromEvent(event);
          setDragRatio(null);
          onScrubbing(false);
          onCommit(ratio);
        }}
        onPointerCancel={() => { setDragRatio(null); onScrubbing(false); }}
      >
        <span className={styles.scrubTrack}>
          <span className={styles.scrubBuffer} style={{ width: `${buffered * 100}%` }} />
          {intro ? <span className={styles.scrubMark} style={intro} /> : null}
          {credits ? <span className={styles.scrubMark} style={credits} /> : null}
          <span className={styles.scrubFill} style={{ width: `${shown * 100}%` }} />
          <span className={styles.scrubThumb} style={{ left: `${shown * 100}%` }} />
          {bubbleRatio !== null && duration > 0 ? (
            <span className={styles.scrubTooltip} style={{ left: `${bubbleRatio * 100}%` }}>{formatTime(timeAt(bubbleRatio))}</span>
          ) : null}
        </span>
      </div>
    </div>
  );
}

/** How the current source is being played, when it is not played directly. */
function pathNote(path: PlanKind | null, local: boolean): string | null {
  if (path === 'server-hls') return local ? 'Prepared on this computer' : 'Prepared by the server';
  if (path === 'server-transcode') return local ? 'Converted on this computer' : 'Converted by the server (last resort)';
  return null;
}

function PlaybackSettings({
  heading,
  streams,
  sourceIndex,
  activePath,
  setSourceIndex,
  failureReasons,
  planKindOf,
  tab,
  setTab,
  onClose,
  audioTracks,
  preparedLocally,
  imageSubtitlesOnly,
  subtitleTracks,
  fit,
  setFit,
  speed,
  setSpeed,
  episodes,
  currentEpisode,
  onEpisode,
  onStartOver,
  otherQuality,
  sourceLabel,
  onAudioTrack,
  onSubtitleTrack,
}: {
  /** tvOS sheet header: "Show - S1:E2" (the title line), not a generic label. */
  heading?: string;
  streams: PlaybackSource[];
  sourceIndex: number;
  activePath: PlanKind | null;
  setSourceIndex: (index: number) => void;
  failureReasons: ReadonlyMap<number, string>;
  planKindOf: (index: number) => PlanKind;
  tab: SettingsTab;
  setTab: (tab: SettingsTab) => void;
  onClose: () => void;
  audioTracks: TrackOption[];
  /** The current source is prepared by the desktop app's engine (wording only). */
  preparedLocally: boolean;
  imageSubtitlesOnly: boolean;
  subtitleTracks: TrackOption[];
  fit: 'fit' | 'fill';
  setFit: (value: 'fit' | 'fill') => void;
  speed: number;
  setSpeed: (value: number) => void;
  episodes: EpisodeItem[];
  currentEpisode: number | null;
  onEpisode: (episode: EpisodeItem) => void;
  onStartOver: () => void;
  otherQuality?: { label: string; run: () => void };
  sourceLabel: string;
  onAudioTrack: (id: string) => void;
  onSubtitleTrack: (id: string) => void;
}) {
  // Only tabs with something to choose: an Audio tab with one track is noise.
  const tabs: Array<[SettingsTab, string, typeof Library]> = [['sources', 'Sources', Library]];
  if (episodes.length) tabs.unshift(['episodes', 'Episodes', ListVideo]);
  if (audioTracks.length > 1) tabs.push(['audio', 'Audio', Volume2]);
  tabs.push(['subtitles', 'Subtitles', Captions], ['speed', 'Speed', Gauge], ['display', 'Display', Monitor]);
  const shown = tabs.some(([key]) => key === tab) ? tab : (tab === 'audio' ? 'subtitles' : 'sources');
  const panelRef = useRef<HTMLElement | null>(null);
  const note = pathNote(activePath, preparedLocally);

  // Focus follows the panel: the active tab when it opens, so the keyboard works at once.
  useEffect(() => {
    panelRef.current?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')?.focus({ preventScroll: true });
  }, []);

  // Close on a click anywhere outside the panel (and don't let that click toggle play).
  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null;
      if (!panelRef.current || !target || panelRef.current.contains(target)) return;
      // The buttons that open the panel handle themselves.
      if (target instanceof Element && target.closest('button')) return;
      event.preventDefault();
      event.stopPropagation();
      const swallowClick = (click: MouseEvent) => { click.preventDefault(); click.stopPropagation(); };
      window.addEventListener('click', swallowClick, { capture: true, once: true });
      window.setTimeout(() => window.removeEventListener('click', swallowClick, { capture: true }), 400);
      onClose();
    };
    window.addEventListener('pointerdown', onPointerDown, true);
    return () => window.removeEventListener('pointerdown', onPointerDown, true);
  }, [onClose]);

  // Arrow keys: Left/Right between tabs, Up/Down through the list.
  const onKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    const panel = panelRef.current;
    if (!panel) return;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      const order = tabs.map(([key]) => key);
      const next = order[(order.indexOf(shown) + (event.key === 'ArrowRight' ? 1 : order.length - 1)) % order.length];
      setTab(next);
      window.requestAnimationFrame(() => panel.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')?.focus({ preventScroll: true }));
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      const items = Array.from(panel.querySelectorAll<HTMLElement>('[data-panel-body] button:not([disabled])'));
      if (!items.length) return;
      const at = items.indexOf(document.activeElement as HTMLElement);
      const next = event.key === 'ArrowDown' ? items[Math.min(at + 1, items.length - 1)] : at <= 0 ? panel.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]') : items[at - 1];
      next?.focus();
      next?.scrollIntoView({ block: 'nearest' });
    } else return;
    event.preventDefault();
    event.stopPropagation();
  };

  return (
    <aside className={styles.panel} aria-label="Playback settings" ref={panelRef} onKeyDown={onKeyDown}>
      <div className={styles.panelHeader}>
        <strong>{heading || 'Playback'}</strong>
        <button className={styles.roundButton} type="button" onClick={onClose} aria-label="Close playback settings"><X size={18} /></button>
      </div>
      <div className={styles.panelTabs} role="tablist">
        {tabs.map(([key, label, Icon]) => (
          <button key={key} role="tab" aria-selected={shown === key} tabIndex={shown === key ? 0 : -1} className={shown === key ? styles.tabActive : ''} type="button" title={label} onClick={() => setTab(key)}>
            <Icon size={16} />
            <span>{label}</span>
          </button>
        ))}
      </div>
      <div className={styles.panelBody} data-panel-body>
        {shown === 'sources' ? (
          <>
            {streams.map((stream, index) => (
              <SourceRow
                key={`${streamUrl(stream) || routeIdentity(stream)}:${index}`}
                stream={stream}
                selected={index === sourceIndex}
                failure={failureReasons.get(index)}
                // In the desktop app preparing a source locally is the normal path, not a fallback worth a label.
                compatibilityMode={!hasLocalEngine() && index === sourceIndex && (activePath === 'server-hls' || activePath === 'server-transcode')}
                compatibilityAvailable={!hasLocalEngine() && (planKindOf(index) === 'server-hls' || planKindOf(index) === 'server-transcode')}
                onSelect={() => setSourceIndex(index)}
              />
            ))}
            {note ? <p className={styles.panelNote}>{sourceLabel}: {note}</p> : null}
            <div className={styles.panelActions}>
              <button className={styles.ghostButton} type="button" onClick={onStartOver}><RotateCcw size={16} />Start over</button>
              {otherQuality ? <button className={styles.ghostButton} type="button" onClick={otherQuality.run}>{otherQuality.label}</button> : null}
            </div>
          </>
        ) : null}
        {shown === 'episodes' ? (
          <div className={styles.episodeList}>
            {episodes.map((episode) => (
              <button
                key={`${episode.season}:${episode.episode}`}
                className={styles.trackRow}
                type="button"
                aria-current={Number(episode.episode) === currentEpisode ? 'true' : undefined}
                onClick={() => onEpisode(episode)}
              >
                {Number(episode.episode) === currentEpisode ? <CheckCircle size={20} /> : <Circle size={20} className={styles.unselectedCircle} />}
                <span><strong>{`E${episode.episode} · ${displayTitle(episode)}`}</strong>{episode.overview ? <small>{String(episode.overview)}</small> : null}</span>
              </button>
            ))}
          </div>
        ) : null}
        {shown === 'audio' ? (
          audioTracks.length ? (
            <div className={styles.pillWrap}>
              {audioTracks.map((track) => (
                <button
                  key={track.id}
                  className={`${styles.pill} ${track.selected ? styles.pillActive : ''}`}
                  type="button"
                  aria-pressed={track.selected}
                  disabled={track.disabled}
                  onClick={() => onAudioTrack(track.id)}
                >
                  {pillLabel(track, audioTracks)}
                </button>
              ))}
            </div>
          ) : <p className={styles.panelNote}>No audio tracks available</p>
        ) : null}
        {shown === 'subtitles' ? (
          subtitleTracks.length ? (
            <div className={styles.pillWrap}>
              {subtitleTracks.map((track) => (
                <button
                  key={track.id}
                  className={`${styles.pill} ${track.selected ? styles.pillActive : ''}`}
                  type="button"
                  aria-pressed={track.selected}
                  onClick={() => onSubtitleTrack(track.id)}
                >
                  {pillLabel(track, subtitleTracks)}
                </button>
              ))}
            </div>
          ) : <p className={styles.panelNote}>No subtitle tracks available</p>
        ) : null}
        {shown === 'subtitles' && imageSubtitlesOnly ? (
          <p className={styles.panelNote}>This source only has image-based subtitles (PGS), which Curatio can't show yet.</p>
        ) : null}
        {shown === 'speed' ? (
          <div className={styles.pillWrap}>
            {SPEEDS.map((value) => (
              <button
                key={value}
                className={`${styles.pill} ${speed === value ? styles.pillActive : ''}`}
                type="button"
                aria-pressed={speed === value}
                onClick={() => setSpeed(value)}
              >
                {value === 1 ? 'Normal' : `${value}×`}
              </button>
            ))}
          </div>
        ) : null}
        {shown === 'display' ? (
          <>
            {([
              ['fit', 'Fit', 'Whole video visible'],
              ['fill', 'Fill', 'Fill screen'],
            ] as const).map(([value, label, detail]) => (
              <button key={value} className={styles.trackRow} type="button" aria-pressed={fit === value} onClick={() => setFit(value)}>
                {fit === value ? <CheckCircle size={20} /> : <Circle size={20} className={styles.unselectedCircle} />}
                <span><strong>{label}</strong><small>{detail}</small></span>
              </button>
            ))}
          </>
        ) : null}
      </div>
    </aside>
  );
}
