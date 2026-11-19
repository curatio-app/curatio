import type { EpisodeItem, PlaybackSource, SeasonItem, WebMediaItem } from './types';
import { providerLabel } from './streamMeta';

// Capability-aware ranking lives in streamCompat so it can be unit-tested with
// an injected browser-capability matrix. Re-exported here to keep the existing
// `browserStreamRank` / `sortBrowserStreams` import sites unchanged.
export { browserStreamRank, sortBrowserStreams, playableStreams } from './streamCompat';

/**
 * Whether a title can be played here (docs/playback-api.md). Titles not in the
 * owner's Plex or Jellyfin (and that no addon can find) are recommendations
 * only; a payload without `availability` is an older cached one and counts as
 * not playable.
 */
export function isPlayable(item?: WebMediaItem | null): boolean {
  return item?.availability?.playable === true;
}

/** Shown wherever Play is withheld for a recommendation-only title. */
export const NOT_IN_LIBRARY_NOTE = 'Not in your Plex or Jellyfin';

/** The server's "On Your Servers" Home row; present only when a Plex/Jellyfin
 * library with titles is connected. */
export const SERVERS_ROW_ID = 'servers-recent';

let serverLibraryPresent = false;

/** Set from the dashboard: whether a Plex/Jellyfin library exists. Cards dim
 * titles that are not on the servers only then, so an addon-only setup never
 * shows a fully dimmed Home. */
export function setServerLibraryPresent(present: boolean) {
  serverLibraryPresent = present;
}

export function hasServerLibrary() {
  return serverLibraryPresent;
}

/**
 * On the owner's Plex/Jellyfin servers. Not the same as `isPlayable`: an addon
 * can make a title playable without anyone owning it.
 */
export function isOnServer(item?: WebMediaItem | null): boolean {
  return item?.availability?.in_library === true;
}

/** Dim a card: a library exists and this title is known not to be on it. */
export function dimsForOwnership(item?: WebMediaItem | null): boolean {
  return serverLibraryPresent && Boolean(item?.availability) && !isOnServer(item);
}

/** The addon that can play a title that is not on the servers, if any. */
export function addonSourceLabel(item?: WebMediaItem | null): string | null {
  if (!item || isOnServer(item) || !isPlayable(item)) return null;
  const addon = item.availability?.sources?.find((source) => source.kind === 'addon');
  return addon?.label?.trim() || null;
}

/**
 * The Details note, keyed on ownership: an unplayable title always says so; a
 * playable one only when a library exists, naming the addon that plays it.
 */
export function notOnServerNote(item?: WebMediaItem | null): string | null {
  if (!item || isOnServer(item)) return null;
  if (!isPlayable(item)) return NOT_IN_LIBRARY_NOTE;
  if (!serverLibraryPresent) return null;
  const addon = addonSourceLabel(item);
  return addon ? `${NOT_IN_LIBRARY_NOTE} · plays from ${addon}` : NOT_IN_LIBRARY_NOTE;
}

export function safeUrl(value: unknown) {
  const raw = String(value ?? '').trim();
  if (!raw) return '';
  if (raw.startsWith('/')) return raw;
  try {
    const url = new URL(raw, window.location.origin);
    return ['http:', 'https:', 'blob:'].includes(url.protocol) ? url.href : '';
  } catch {
    return '';
  }
}

export function tmdbImage(path: unknown, size = 'w780') {
  const raw = String(path ?? '').trim();
  if (!raw) return '';
  if (raw.startsWith('http://') || raw.startsWith('https://')) return safeUrl(raw);
  if (raw.startsWith('/')) return `https://image.tmdb.org/t/p/${size}${raw}`;
  return '';
}

export function displayTitle(item?: WebMediaItem | null) {
  return String(item?.title ?? item?.name ?? 'Untitled');
}

// Render a value only when it is actually a string. Guards JSX text bindings against
// a non-string (object/array) sneaking in through WebMediaItem's `[key: string]: unknown`
// index signature, which would otherwise coerce to the literal "[object Object]".
export function asText(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

// Stable, collision-proof React key for a result card. IMDb-keyed items have
// `tmdb_id: null` (many can share a title), so fall back to imdb_id and always append the
// map index — duplicate sibling keys break React reconciliation and leave stale cards mounted.
export function resultKey(item: WebMediaItem | null | undefined, index: number) {
  const id = item?.tmdb_id ?? item?.imdb_id ?? 'na';
  return `${item?.media_type ?? 'movie'}:${id}:${displayTitle(item)}:${index}`;
}

export function itemKind(item?: WebMediaItem | null) {
  return item?.media_type === 'tv' || item?.media_type === 'series' ? 'series' : 'movie';
}

export function mediaType(item?: WebMediaItem | null) {
  return String(item?.content_type ?? (itemKind(item) === 'series' ? 'series' : 'movie'));
}

export function playbackId(item?: WebMediaItem | null) {
  if (!item) return '';
  if (item.video_id) return String(item.video_id);
  if (itemKind(item) === 'series') {
    return `tmdb:${item.tmdb_id ?? ''}:${item.season ?? 1}:${item.episode ?? 1}`;
  }
  return String(item.imdb_id ?? `tmdb:${item.tmdb_id ?? ''}`);
}

export function itemImage(item?: WebMediaItem | null, poster = false, size?: string) {
  if (!item) return '';
  if (poster) return tmdbImage(item.poster_path ?? item.poster, size ?? 'w500');
  return tmdbImage(item.backdrop_path ?? item.backdrop ?? item.poster_path ?? item.poster, size ?? 'w780');
}

/** Logos render at most ~600px wide; w500 is plenty and a fraction of the bytes. */
export function logoImage(item?: WebMediaItem | null) {
  return tmdbImage(item?.logo_path ?? item?.logo, 'w500');
}

/**
 * TMDB backdrop size for full-screen art. `original` is often a multi-MB 4K
 * file; only screens wider than ~1600 physical pixels need more than w1280.
 */
export function fullBleedSize(): 'w1280' | 'original' {
  if (typeof window === 'undefined') return 'w1280';
  const physical = window.innerWidth * (window.devicePixelRatio || 1);
  return physical > 1600 ? 'original' : 'w1280';
}

export function runtimeLabel(item?: WebMediaItem | null) {
  const runtime = item?.runtime;
  if (!runtime) return '';
  return typeof runtime === 'number' ? `${runtime} min` : String(runtime);
}

/** Title runtime in seconds from the item's fields (minutes or "1h 52m"/"112 min"), or null. */
export function runtimeSecondsOf(item?: WebMediaItem | null): number | null {
  const explicit = Number(item?.runtime_seconds ?? item?.runtimeSeconds);
  if (Number.isFinite(explicit) && explicit > 0) return explicit;
  const runtime = item?.runtime;
  if (typeof runtime === 'number') return runtime > 0 ? runtime * 60 : null;
  const text = String(runtime ?? '').toLowerCase();
  const hours = /(\d+)\s*h/.exec(text);
  const minutes = /(\d+)\s*m/.exec(text);
  const total = (hours ? Number(hours[1]) * 60 : 0) + (minutes ? Number(minutes[1]) : 0);
  return total > 0 ? total * 60 : null;
}

const MONTHS_ABBR = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
];

// Parse a yyyy-MM-dd (or an ISO-8601 string with a time suffix) into a UTC midnight
// Date. Returns null when the leading date isn't a real calendar day, so garbage or a
// missing value renders nothing.
function parseReleaseDateUTC(value?: string | null): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value ?? '').trim());
  if (!match) return null;
  const [, y, m, d] = match;
  const year = Number(y);
  const month = Number(m) - 1;
  const day = Number(d);
  const date = new Date(Date.UTC(year, month, day));
  // Reject overflow (e.g. 2024-13-40 rolling into the next month/year).
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month || date.getUTCDate() !== day) {
    return null;
  }
  return date;
}

export interface DigitalReleaseDisplay {
  /** Self-labeled: "Digital in 9 days", "Digital · Jun 15, 2026". */
  chipText: string;
  /** Bare value for the labeled "Digital" info row: "In 9 days", "Jun 15, 2026". */
  rowValue: string;
  isUpcoming: boolean;
}

/**
 * Port of native `DigitalReleaseFormatter.display`: upcoming dates count down
 * within 30 days, a release in the last 30 days reads as a short date, and an
 * older one shows nothing.
 */
export function digitalReleaseDisplay(raw: unknown, now: Date = new Date()): DigitalReleaseDisplay | null {
  const release = parseReleaseDateUTC(typeof raw === 'string' ? raw : null);
  if (!release) return null;
  const todayUTC = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const days = Math.round((release.getTime() - todayUTC) / 86_400_000);
  if (days < -30) return null;
  const shortDate = `${MONTHS_ABBR[release.getUTCMonth()]} ${release.getUTCDate()}`;
  const withYear = `${shortDate}, ${release.getUTCFullYear()}`;
  if (days > 30) return { chipText: `Digital · ${withYear}`, rowValue: withYear, isUpcoming: true };
  if (days > 1) return { chipText: `Digital in ${days} days`, rowValue: `In ${days} days`, isUpcoming: true };
  if (days === 1) return { chipText: 'Digital tomorrow', rowValue: 'Tomorrow', isUpcoming: true };
  if (days === 0) return { chipText: 'Digital today', rowValue: 'Today', isUpcoming: false };
  return { chipText: `Digital · ${shortDate}`, rowValue: shortDate, isUpcoming: false };
}

const MONTHS_FULL = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const NEW_WINDOW_DAYS = 14;

export interface ReleaseBadge {
  /** Poster-corner text: "Friday", "July 31", "New". */
  badgeText: string;
  /** Self-labeled hero/detail chip: "Coming Friday", "Just released", "New". */
  chipText: string;
  upcoming: boolean;
  /** "title" ("Coming Friday") or, for a series that has already premiered, "episode" ("New episode Friday"). */
  kind?: 'title' | 'episode';
}

/** Days from the viewer's local today to a yyyy-MM-dd date (0 = today), or null. */
function daysUntil(value: unknown, now: Date): number | null {
  const release = parseReleaseDateUTC(typeof value === 'string' ? value : null);
  if (!release) return null;
  const todayUTC = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((release.getTime() - todayUTC) / 86_400_000);
}

function badgeForDate(value: unknown, kind: 'title' | 'episode', now: Date): ReleaseBadge | null {
  const diffDays = daysUntil(value, now);
  if (diffDays == null) return null;
  const release = parseReleaseDateUTC(String(value))!;
  const episode = kind === 'episode';
  if (diffDays > 0) {
    let when: string;
    if (diffDays === 1) when = 'Tomorrow';
    else if (diffDays < 7) when = WEEKDAYS[release.getUTCDay()];
    else if (release.getUTCFullYear() === now.getFullYear()) when = `${MONTHS_FULL[release.getUTCMonth()]} ${release.getUTCDate()}`;
    else when = `${MONTHS_ABBR[release.getUTCMonth()]} ${release.getUTCDate()}, ${release.getUTCFullYear()}`;
    const chipWhen = diffDays === 1 ? 'tomorrow' : when;
    return { badgeText: when, chipText: `${episode ? 'New episode' : 'Coming'} ${chipWhen}`, upcoming: true, kind };
  }
  if (diffDays > -NEW_WINDOW_DAYS) {
    const newText = episode ? 'New episode' : 'New';
    return {
      badgeText: newText,
      chipText: diffDays === 0 ? (episode ? 'New episode today' : 'Just released') : newText,
      upcoming: false,
      kind,
    };
  }
  return null;
}

/**
 * Port of the native `ComingSoonFormatter`: an upcoming release reads by
 * weekday within a week, otherwise by date; a release in the last 14 days
 * reads "New" ("Just released" on the day). `now` is injectable for tests.
 */
export function releaseBadge(item?: WebMediaItem | null, now: Date = new Date()): ReleaseBadge | null {
  if (!item) return null;
  return badgeForDate(item.comingSoonDate ?? item.digitalReleaseInfo, 'title', now);
}

/** Whether an episode has aired: no date, or a date on or before the viewer's today (fail-open, like native). */
export function episodeHasAired(episode?: { air_date?: unknown } | null, now: Date = new Date()): boolean {
  const days = daysUntil(episode?.air_date, now);
  return days == null || days <= 0;
}

/** The "Coming Friday" form of an unaired episode's date ("Friday", "July 31"), or null once it has aired. */
export function episodeComingText(episode?: { air_date?: unknown } | null, now: Date = new Date()): string | null {
  const badge = badgeForDate(episode?.air_date, 'title', now);
  return badge?.upcoming ? badge.badgeText : null;
}

/**
 * Port of native `ComingSoonFormatter.seriesBadge`: computed from the loaded
 * episode list so it advances as episodes air. The next unaired episode wins
 * ("New episode Friday", or "Coming Friday" before the premiere), then one that
 * just aired, then the item's cached date (never-premiered shows).
 */
export function seriesReleaseBadge(
  episodes: Array<{ air_date?: unknown }>,
  comingSoonDate?: string | null,
  now: Date = new Date(),
): ReleaseBadge | null {
  let premiered = false;
  let nextUnaired: { date: string; days: number } | null = null;
  let lastAired: { date: string; days: number } | null = null;
  for (const episode of episodes) {
    const days = daysUntil(episode.air_date, now);
    if (days == null) {
      premiered = true;
      continue;
    }
    const date = String(episode.air_date);
    if (days <= 0) {
      premiered = true;
      if (!lastAired || days > lastAired.days) lastAired = { date, days };
    } else if (!nextUnaired || days < nextUnaired.days) {
      nextUnaired = { date, days };
    }
  }
  const kind = premiered ? 'episode' : 'title';
  if (nextUnaired) return badgeForDate(nextUnaired.date, kind, now);
  const recent = lastAired ? badgeForDate(lastAired.date, 'episode', now) : null;
  return recent ?? badgeForDate(comingSoonDate, kind, now);
}

export function episodeLabel(item?: WebMediaItem | null) {
  if (!item) return '';
  if (item.subtitle) return String(item.subtitle);
  if (itemKind(item) === 'series' && item.season && item.episode) return `S${item.season}:E${item.episode}`;
  return item.year ? String(item.year) : '';
}

export function progressPercent(item?: WebMediaItem | null) {
  const explicit = Number(item?.progress_percent ?? item?.progressPercent);
  if (Number.isFinite(explicit) && explicit > 0) return clamp(explicit, 0, 100);
  const position = Number(item?.position ?? 0);
  const duration = Number(item?.duration ?? 0);
  if (duration > 0) return clamp(Math.round((position / duration) * 100), 0, 100);
  return 0;
}

export function progressFraction(item?: WebMediaItem | null) {
  return progressPercent(item) / 100;
}

export function playableEpisode(episode?: EpisodeItem | null) {
  return Boolean(episode?.season && episode?.episode);
}

export function episodePlayItem(series: WebMediaItem, episode: EpisodeItem): WebMediaItem {
  return {
    ...series,
    ...episode,
    title: displayTitle(series),
    subtitle: episode.season && episode.episode ? `S${episode.season}:E${episode.episode}` : episode.title ?? episode.name ?? 'Episode',
    media_type: 'tv',
  };
}

export function flattenEpisodes(seasons?: SeasonItem[]) {
  return (seasons ?? [])
    .flatMap((season) => season.episodes ?? [])
    .filter(playableEpisode)
    .sort((a, b) => Number(a.season ?? 0) - Number(b.season ?? 0) || Number(a.episode ?? 0) - Number(b.episode ?? 0));
}

export function findNextEpisode(current: WebMediaItem, seasons?: SeasonItem[]) {
  const episodes = flattenEpisodes(seasons);
  const index = episodes.findIndex(
    (episode) => String(episode.season) === String(current.season) && String(episode.episode) === String(current.episode),
  );
  // An unaired episode has no sources: never offer it as next (native PlaybackPromptRules).
  const next = index >= 0 ? episodes[index + 1] : null;
  return next && episodeHasAired(next) ? episodePlayItem(current, next) : null;
}

/** localStorage key for a title's (or episode's) saved playback position. */
let progressProfile: number | null = null;

/**
 * Positions saved on this device belong to the profile that watched: without
 * this, one profile resumed where another stopped.
 */
export function setProgressProfile(profileId?: number | null) {
  progressProfile = profileId ?? null;
}

/** Start of every saved-position key of the active profile. */
export function playbackProgressPrefix() {
  return progressProfile == null ? 'curatio-playback:' : `curatio-playback@${progressProfile}:`;
}

export function playbackProgressKey(item: WebMediaItem) {
  const identity = item.tmdb_id ?? item.imdb_id ?? item.video_id ?? displayTitle(item);
  return `${playbackProgressPrefix()}${item.media_type}:${identity}:${item.season ?? 0}:${item.episode ?? 0}`;
}

export function streamUrl(stream?: PlaybackSource | null) {
  return safeUrl(stream?.url);
}

/** A source's resolution as a badge ("4K", "1080p", "720p", "SD"), '' when unknown. */
export function resolutionBadge(source: PlaybackSource): string {
  const resolution = String(source.resolution ?? '').toLowerCase().trim();
  if (resolution === '2160p') return '4K';
  if (resolution === '1080p' || resolution === '720p') return resolution;
  if (resolution) return 'SD';
  return '';
}

/** Source row model for the picker and the in-player Sources menu. Display
 * strings come verbatim from the server (`label`, `detail`, `provider_label`,
 * `badges`); nothing here parses them. */
export type SourceRowModel = {
  /** Resolution badge, e.g. "4K", "1080p" ('' when unknown). */
  resolution: string;
  /** The server's `label` (first line). */
  name: string;
  /** The server's `provider_label`. */
  provider: string;
  /** The server's `detail` (often a file name), '' when absent. */
  detail: string;
  /** The server's `badges`, verbatim. */
  badges: string[];
  /** Human-readable file size, e.g. "12.4 GB" ('' when unknown). */
  size: string;
  /** In-player menu first line. */
  menuSummary: string;
  /** In-player menu second line: provider and detail. */
  menuDetails: string;
};

export function sourceRowModel(source: PlaybackSource): SourceRowModel {
  const provider = providerLabel(source);
  const name = String(source.label ?? '').trim() || provider || 'Source';
  const detail = String(source.detail ?? '').trim();
  const badges = Array.isArray(source.badges) ? source.badges.map((badge) => String(badge ?? '').trim()).filter(Boolean) : [];
  const size = sizeText(source.size_bytes);
  return {
    resolution: resolutionBadge(source),
    name,
    provider,
    detail: detail && detail !== name ? detail : '',
    badges,
    size,
    menuSummary: name,
    menuDetails: [provider !== name ? provider : '', size].filter(Boolean).join(' · '),
  };
}

function sizeText(value: unknown): string {
  const bytes = Number(value ?? 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return '';
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(bytes >= 1e11 ? 0 : 1)} GB`;
  if (bytes >= 1e6) return `${Math.round(bytes / 1e6)} MB`;
  return `${bytes} B`;
}

export function clamp(value: number, min: number, max: number) {
  return Math.max(min, Math.min(max, value));
}

export function formatTime(seconds: number) {
  if (!Number.isFinite(seconds) || seconds < 0) return '--:--';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
}

export function detailInitials(name: string) {
  return name
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join('') || 'C';
}

/**
 * Wide artwork for an expanded shelf card — a port of tvOS `LandscapeArtwork.url`.
 * The backdrop when it differs from the poster; otherwise a Metahub poster URL
 * rewritten to its parallel `/background/` path; otherwise ''. Unlike
 * `itemImage(item, false)` this never falls back to the poster.
 */
export function landscapeImage(item?: WebMediaItem | null, size = 'w780') {
  if (!item) return '';
  const poster = tmdbImage(item.poster_path ?? item.poster, size);
  const backdrop = tmdbImage(item.backdrop_path ?? item.backdrop, size);
  if (backdrop && backdrop !== poster) return backdrop;
  if (!poster) return '';
  try {
    const url = new URL(poster);
    if (url.hostname.includes('metahub.space') && url.pathname.includes('/poster/')) {
      url.pathname = url.pathname.replace('/poster/', '/background/');
      return url.href;
    }
  } catch {
    return '';
  }
  return '';
}

/** tvOS `formatRemainingTime`: "2h remaining", "1h 20m remaining", "45m remaining". */
export function remainingTimeLabel(seconds: number) {
  if (!Number.isFinite(seconds) || seconds <= 0) return '';
  const totalMinutes = Math.max(1, Math.round(seconds / 60));
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours && minutes) return `${hours}h ${minutes}m remaining`;
  if (hours) return `${hours}h remaining`;
  return `${minutes}m remaining`;
}

/** Progress caption for a Continue Watching card: time left, else "N% watched". */
export function progressCaption(item?: WebMediaItem | null) {
  const position = Number(item?.position ?? 0);
  const duration = Number(item?.duration ?? 0);
  if (duration > 0 && position > 0 && position < duration) return remainingTimeLabel(duration - position);
  const percent = progressPercent(item);
  return percent >= 3 && percent <= 95 ? `${Math.round(percent)}% watched` : '';
}

/** tvOS card metadata line: "Film • 2026 • 1h 32m" / "Series • 2024". */
export function cardMetaParts(item?: WebMediaItem | null) {
  return [itemKind(item) === 'series' ? 'Series' : 'Film', item?.year ? String(item.year) : '', runtimeLabel(item)].filter(Boolean);
}

/** tvOS `ShelfStyle.topTen`: catalog ids containing "top10" / "top-10". */
export function isTopTenRow(row?: { id?: unknown; name?: unknown } | null) {
  const key = `${String(row?.id ?? '')} ${String(row?.name ?? '')}`.toLowerCase();
  return key.includes('top10') || key.includes('top-10') || key.includes('top 10');
}
