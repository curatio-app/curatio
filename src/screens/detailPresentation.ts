/**
 * Pure presentation rules for the Details page, ported from the tvOS/iOS
 * client so the desktop page reads the same:
 *   - hero badge priority (DetailsView.heroBadge)
 *   - episode card markers (EpisodeShelfPresentationBuilder)
 *   - score order (MetadataComponents displayOrder)
 *   - the Details info card rows (movieInfoDetails / seriesInfoDetails)
 */
import { digitalReleaseDisplay, type ReleaseBadge } from '../api/mediaUtils';
import type { TitleRating } from '../api/types';

/** What the primary button will do, the web counterpart of native `PlaybackTarget`. */
/** `watchlist`: a recommendation-only title (not playable): the primary action saves it. */
export type PrimaryKind = 'play' | 'resume' | 'next' | 'start' | 'restart' | 'unavailable' | 'loading' | 'watchlist';

export interface HeroBadge {
  text: string;
  prominent: boolean;
}

/**
 * The badge above the title. A pending release outranks "Continue Watching" /
 * "Up Next" (those restate the Play button; "Coming Friday" is news nothing
 * else on the page says); "Just released" ranks below them.
 */
export function heroBadge(release: ReleaseBadge | null, kind: PrimaryKind): HeroBadge | null {
  if (release?.upcoming) return { text: release.chipText, prominent: true };
  if (kind === 'resume') return { text: 'Continue Watching', prominent: false };
  if (kind === 'next') return { text: 'Up Next', prominent: false };
  if (release) return { text: release.chipText, prominent: true };
  return null;
}

export type EpisodeMarker =
  | { kind: 'none' }
  | { kind: 'startHere'; label: 'Start Here' }
  | { kind: 'nextUp'; label: 'Next Up' }
  | { kind: 'watched' }
  | { kind: 'inProgress'; label: 'In Progress'; fraction: number }
  | { kind: 'comingSoon'; label: string };

export interface EpisodeMarkerInput {
  /** "Friday" / "July 31" when the episode hasn't aired, else null. */
  comingText: string | null;
  watched: boolean;
  /** Saved progress fraction, 0 when none. */
  fraction: number;
  /** Whether this is the episode the primary button targets. */
  isTarget: boolean;
  targetKind: PrimaryKind;
}

/**
 * Native marker precedence: unaired, then this episode's own progress, then
 * its role as the primary button's target.
 */
export function episodeMarker({ comingText, watched, fraction, isTarget, targetKind }: EpisodeMarkerInput): EpisodeMarker {
  if (comingText) return { kind: 'comingSoon', label: `Coming ${comingText}` };
  if (watched) return { kind: 'watched' };
  if (fraction > 0) return { kind: 'inProgress', label: 'In Progress', fraction };
  if (isTarget) {
    if (targetKind === 'next' || targetKind === 'resume') return { kind: 'nextUp', label: 'Next Up' };
    if (targetKind === 'start' || targetKind === 'restart') return { kind: 'startHere', label: 'Start Here' };
  }
  return { kind: 'none' };
}

const SCORE_ORDER: Record<string, number> = {
  tomatoes: 0, rottentomatoes: 0, rt: 0, tomatometer: 0,
  popcorn: 1, popcornmeter: 1, rtaudience: 1, tomatoesaudience: 1,
  metacritic: 10,
  metacriticuser: 11, metacriticusers: 11,
  rogerebert: 20,
  imdb: 30,
  tmdb: 40, themoviedb: 40,
  trakt: 50,
  letterboxd: 60,
  myanimelist: 70,
};

/** The fixed product order every detail page uses; unknown sources go last, by label. */
export function orderedScores(ratings: TitleRating[]): TitleRating[] {
  const rank = (rating: TitleRating) => SCORE_ORDER[String(rating.source ?? '').toLowerCase()] ?? 1000;
  return [...ratings].sort((a, b) => rank(a) - rank(b) || String(a.label ?? '').localeCompare(String(b.label ?? '')));
}

/** "R (17+)": the certification with the advisory age when the server sent one. */
export function ratedValue(certification?: string | null, age?: string | number | null): string {
  const rated = String(certification ?? '').trim();
  if (!rated) return '';
  const years = Number(age);
  return Number.isFinite(years) && years > 0 ? `${rated} (${years}+)` : rated;
}

export interface InfoRowsInput {
  isSeries: boolean;
  release?: string | number | null;
  certification?: string | null;
  ageRating?: string | number | null;
  status?: string | null;
  networks?: string[];
  /** The upcoming half of the release badge, if any. */
  coming?: ReleaseBadge | null;
  digitalReleaseInfo?: string | null;
  runtime?: string;
  genres?: string[];
  directors?: string[];
  creators?: string[];
  writers?: string[];
  language?: string;
  countries?: string[];
  studios?: string[];
  themes?: string[];
  now?: Date;
}

/** The Details info card, in the native field order. Rows with no value are dropped. */
export function detailInfoRows(input: InfoRowsInput): Array<[string, string]> {
  const list = (values: string[] | undefined, max: number, separator = ', ') => (values ?? []).filter(Boolean).slice(0, max).join(separator);
  const rows: Array<[string, string]> = [
    ['Release', input.release != null ? String(input.release) : ''],
    ['Rated', ratedValue(input.certification, input.ageRating)],
  ];
  const coming = input.coming?.upcoming ? input.coming : null;
  if (input.isSeries) {
    rows.push(['Status', input.status ?? '']);
    rows.push(['Network', list(input.networks, 2)]);
    if (coming) rows.push([coming.kind === 'episode' ? 'Next Episode' : 'Available', coming.badgeText]);
  } else if (coming) {
    rows.push(['Available', coming.badgeText]);
  } else {
    // Only one of the two: for an upcoming movie both name the same date.
    rows.push(['Digital', digitalReleaseDisplay(input.digitalReleaseInfo, input.now)?.rowValue ?? '']);
  }
  rows.push(['Runtime', input.runtime ?? '']);
  rows.push(['Genres', list(input.genres, 3, ' • ')]);
  if (input.isSeries) {
    rows.push(['Created By', list(input.creators?.length ? input.creators : input.directors, 2)]);
  } else {
    rows.push(['Director', list(input.directors, 2)]);
  }
  rows.push(['Language', input.language ?? '']);
  rows.push(['Country', list(input.countries, 2)]);
  rows.push(['Studio', list(input.studios, 2)]);
  rows.push(['Writer', list(input.writers, 2)]);
  rows.push(['Themes', list(input.themes, 4, ' • ')]);
  return rows.filter(([, value]) => value);
}
