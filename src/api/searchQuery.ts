import type { WebMediaItem } from './types';

/**
 * Search query cleanup, ported from the Apple clients' `SearchQueryNormalizer`
 * (Core/Services/SearchRanking.swift) so the web, tvOS and iOS read the same
 * typed query the same way:
 *
 * - a trailing release year ("The Matrix (1999)") is taken out of the text
 *   sent to the server and kept only as a ranking signal;
 * - a leading "the/a/an" is a fallback variant;
 * - conversational phrasing ("please show me The Matrix movie") is reduced to
 *   the title, but only as a fallback after the literal query;
 * - when every variant comes back empty, title prefixes are retried so a typo
 *   ("gladiater") still finds the title.
 *
 * Pure apart from `searchWithFallbacks`, which takes the fetcher as a parameter.
 */

export interface ParsedQuery {
  /** Trimmed original text. */
  raw: string;
  /** Year and the punctuation around it removed; original casing. */
  cleanedTitle: string;
  /** Release year parsed out of the query, used only to rank. */
  year: number | null;
  /** `cleanedTitle` without a leading article, when that differs. */
  articleStripped: string | null;
  /** Progressively relaxed readings, tried only after the literal query. */
  tolerantTitles: string[];
}

const YEAR_PATTERN = /\b(19|20)\d{2}\b/g;

/** Words wrapped around a title in a natural-language request. Never removed
 * from the primary query; they only produce extra fallback readings. */
const CONVERSATIONAL_NOISE = new Set([
  'a', 'an', 'the',
  'about', 'anything', 'called', 'can', 'could', 'episode', 'episodes',
  'film', 'films', 'find', 'for', 'get', 'give', 'i', 'id', 'im',
  'like', 'look', 'looking', 'me', 'movie', 'movies', 'named', 'play',
  'please', 'pls', 'search', 'series', 'show', 'shows', 'something',
  'stream', 'television', 'thanks', 'to', 'tv', 'want', 'watch',
  'would', 'you',
]);

/** Edge-only pass: keeps "please Show Me a Hero" as "Show Me a Hero" before
 * the broader pass reduces it further. */
const POLITE_EDGE_NOISE = new Set(['please', 'pls', 'thanks']);

/** Articles alone don't justify a conversational reading. */
const NON_ARTICLE_NOISE = new Set([...CONVERSATIONAL_NOISE].filter((word) => !['a', 'an', 'the'].includes(word)));

const EDGE_TRIM = '()[]{}-–—_ ';

function trimChars(value: string, chars: string) {
  let start = 0;
  let end = value.length;
  while (start < end && chars.includes(value[start])) start += 1;
  while (end > start && chars.includes(value[end - 1])) end -= 1;
  return value.slice(start, end);
}

function foldDiacritics(value: string) {
  return value.normalize('NFD').replace(/\p{M}/gu, '');
}

function wordKey(value: string) {
  return foldDiacritics(value).toLowerCase().replace(/[^\p{L}\p{N}]/gu, '');
}

function words(value: string) {
  return value.split(/\s+/).filter(Boolean);
}

function sameText(a: string, b: string) {
  return a.toLowerCase() === b.toLowerCase();
}

export function stripLeadingArticle(value: string) {
  const lower = value.toLowerCase();
  for (const article of ['the ', 'a ', 'an ']) {
    if (lower.startsWith(article)) return value.slice(article.length).trim();
  }
  return value;
}

/** A plausible *trailing* release year, optionally followed by request filler.
 * "2001: A Space Odyssey" keeps 2001; "Blade Runner 2049" isn't a year filter. */
function releaseYear(value: string, now: Date): { index: number; length: number; year: number } | null {
  const maxYear = now.getFullYear() + 1;
  const matches = [...value.matchAll(YEAR_PATTERN)].reverse();
  for (const match of matches) {
    const year = Number(match[0]);
    const index = match.index ?? 0;
    if (year > maxYear) continue;
    const suffixTerms = value
      .slice(index + match[0].length)
      .split(/[^\p{L}\p{N}]+/u)
      .filter(Boolean)
      .map((term) => term.toLowerCase());
    if (suffixTerms.every((term) => CONVERSATIONAL_NOISE.has(term))) {
      return { index, length: match[0].length, year };
    }
  }
  return null;
}

function makeTolerantTitles(value: string): string[] {
  const all = words(value);
  if (all.length < 2) return [];

  const variants: string[] = [];
  const add = (candidateWords: string[]) => {
    const candidate = candidateWords.join(' ').trim();
    if (!candidate || sameText(candidate, value) || variants.some((v) => sameText(v, candidate))) return;
    variants.push(candidate);
  };

  let start = 0;
  let end = all.length;
  while (start < end && POLITE_EDGE_NOISE.has(wordKey(all[start]))) start += 1;
  while (end > start && POLITE_EDGE_NOISE.has(wordKey(all[end - 1]))) end -= 1;
  if (start !== 0 || end !== all.length) add(all.slice(start, end));

  // Only build the broad intent reading when real request/media words are
  // present, so "The Office" doesn't gain a pointless second reading.
  const working = all.slice(start, end);
  if (!working.some((word) => NON_ARTICLE_NOISE.has(wordKey(word)))) return variants;
  const intentWords = working.filter((word) => !CONVERSATIONAL_NOISE.has(wordKey(word)));
  if (intentWords.length) add(intentWords);
  return variants;
}

export function parseSearchQuery(raw: string, now: Date = new Date()): ParsedQuery {
  const trimmed = raw.trim();
  if (!trimmed) return { raw: '', cleanedTitle: '', year: null, articleStripped: null, tolerantTitles: [] };

  let year: number | null = null;
  let cleaned = trimmed;
  const found = releaseYear(trimmed, now);
  if (found) {
    year = found.year;
    cleaned = `${trimmed.slice(0, found.index)} ${trimmed.slice(found.index + found.length)}`;
  }
  cleaned = trimChars(words(cleaned).join(' '), EDGE_TRIM);

  // A pure-year query ("2012") stays searchable as text.
  if (!cleaned) {
    cleaned = trimmed;
    year = null;
  }

  const articleStripped = stripLeadingArticle(cleaned);
  return {
    raw: trimmed,
    cleanedTitle: cleaned,
    year,
    articleStripped: articleStripped !== cleaned ? articleStripped : null,
    tolerantTitles: makeTolerantTitles(cleaned),
  };
}

/** Ordered, de-duplicated queries to send. The year-free title goes first; the
 * raw text (with its year) is deliberately not included. */
export function remoteQueryVariants(parsed: ParsedQuery): string[] {
  const variants: string[] = [];
  const add = (value: string | null | undefined) => {
    const trimmed = value?.trim();
    if (!trimmed || variants.some((v) => sameText(v, trimmed))) return;
    variants.push(trimmed);
  };
  add(parsed.cleanedTitle);
  add(parsed.articleStripped);
  for (const tolerant of parsed.tolerantTitles) {
    add(tolerant);
    const stripped = stripLeadingArticle(tolerant);
    add(stripped === tolerant ? null : stripped);
  }
  return variants;
}

/** Last-resort prefixes for typo tolerance: the server matches prefixes and
 * substrings but doesn't fuzzy-correct, so "gladiater" → "gladiat" finds
 * Gladiator. Only used when every primary variant came back empty. */
export function fuzzyFallbackVariants(parsed: ParsedQuery): string[] {
  const q = (parsed.tolerantTitles[parsed.tolerantTitles.length - 1] ?? parsed.cleanedTitle).trim();
  const chars = [...q];
  if (chars.length < 6) return [];

  const minLength = 5;
  const candidates: string[] = [];

  // Multi-word: drop the trailing, often half-typed, word.
  const parts = q.split(' ').filter(Boolean);
  if (parts.length >= 2) {
    const dropped = parts.slice(0, -1).join(' ');
    if ([...dropped].length >= minLength) candidates.push(dropped);
  }

  // Drop the last 2 characters (trailing typo), then keep ~60% (a typo or
  // transposition nearer the middle).
  const n = chars.length;
  for (const length of [n - 2, Math.floor(n * 0.6)]) {
    if (length >= minLength && length < n) candidates.push(chars.slice(0, length).join(''));
  }

  const seen = new Set(remoteQueryVariants(parsed).map((v) => v.toLowerCase()));
  const result: string[] = [];
  for (const candidate of candidates) {
    const key = candidate.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(candidate);
  }
  return result.slice(0, 3);
}

/** First plausible 4-digit year in a free-form year/release string. */
export function parseReleaseYear(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === '') return null;
  const match = String(value).match(/\b(19|20)\d{2}\b/);
  return match ? Number(match[0]) : null;
}

function normalizeTitle(value: string) {
  return foldDiacritics(value)
    .toLowerCase()
    .replace(/&/g, ' and ')
    .replace(/['’]/g, '')
    .split(/[^\p{L}\p{N}]+/u)
    .filter(Boolean)
    .join(' ');
}

function itemTitle(item: WebMediaItem) {
  return String(item.title ?? item.name ?? '');
}

/**
 * Year as a ranking signal only (as Apple's ranker): a result from the queried
 * year — or whose title carries it, "Wonder Woman 1984" — moves ahead; a
 * result known to be from another year moves behind ones with no year. The
 * server's order is kept within each group.
 */
export function rankByYear(items: WebMediaItem[], year: number | null): WebMediaItem[] {
  if (year === null || !items.length) return items;
  const bucket = (item: WebMediaItem) => {
    if (itemTitle(item).includes(String(year))) return 0;
    const itemYear = parseReleaseYear(item.year);
    if (itemYear === null) return 1;
    return itemYear === year ? 0 : 2;
  };
  return items
    .map((item, index) => ({ item, index, bucket: bucket(item) }))
    .sort((a, b) => a.bucket - b.bucket || a.index - b.index)
    .map((entry) => entry.item);
}

/** Bounded Damerau-Levenshtein (optimal string alignment) distance. */
function editDistance(a: string, b: string) {
  const s = [...a];
  const t = [...b];
  const d: number[][] = Array.from({ length: s.length + 1 }, (_, i) => {
    const row = new Array<number>(t.length + 1).fill(0);
    row[0] = i;
    return row;
  });
  for (let j = 0; j <= t.length; j += 1) d[0][j] = j;
  for (let i = 1; i <= s.length; i += 1) {
    for (let j = 1; j <= t.length; j += 1) {
      const cost = s[i - 1] === t[j - 1] ? 0 : 1;
      d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost);
      if (i > 1 && j > 1 && s[i - 1] === t[j - 2] && s[i - 2] === t[j - 1]) {
        d[i][j] = Math.min(d[i][j], d[i - 2][j - 2] + 1);
      }
    }
  }
  return d[s.length][t.length];
}

/** Closeness of a title to the typed query: the edit distance against the
 * whole title or its leading words, whichever is nearer. */
function fuzzyDistance(query: string, title: string) {
  const q = normalizeTitle(stripLeadingArticle(query));
  const t = normalizeTitle(stripLeadingArticle(title));
  if (!q || !t) return Number.POSITIVE_INFINITY;
  const titleWords = t.split(' ');
  const queryWordCount = q.split(' ').length;
  const prefix = titleWords.slice(0, queryWordCount).join(' ');
  return Math.min(editDistance(q, t), editDistance(q, prefix));
}

/** Re-rank a prefix-broadened candidate pool against what was typed. */
export function rankFuzzyCandidates(items: WebMediaItem[], query: string): WebMediaItem[] {
  return items
    .map((item, index) => ({ item, index, distance: fuzzyDistance(query, itemTitle(item)) }))
    .sort((a, b) => a.distance - b.distance || a.index - b.index)
    .map((entry) => entry.item);
}

function identity(item: WebMediaItem) {
  const id = item.tmdb_id ?? item.id ?? itemTitle(item);
  return `${item.media_type ?? ''}:${id}`;
}

export interface SearchPage {
  items?: WebMediaItem[];
  sections?: Array<{ items: WebMediaItem[] }>;
}

function hasResults(page: SearchPage) {
  return Boolean(page.items?.length || page.sections?.length);
}

/**
 * Runs a search the way the Apple clients do: the year-free title first, then
 * article-stripped and conversational readings until one returns something,
 * then typo prefixes (merged and re-ranked against the typed query). A year in
 * the query only reorders the results. One request for an ordinary query.
 */
export async function searchWithFallbacks<T extends SearchPage>(
  raw: string,
  fetchPage: (query: string) => Promise<T>,
  now: Date = new Date(),
): Promise<{ page: T; parsed: ParsedQuery; query: string }> {
  const parsed = parseSearchQuery(raw, now);
  const variants = remoteQueryVariants(parsed);
  let first: { page: T; query: string } | null = null;

  for (const query of variants) {
    const page = await fetchPage(query);
    if (!first) first = { page, query };
    if (hasResults(page)) {
      return { page: { ...page, items: rankByYear(page.items ?? [], parsed.year) }, parsed, query };
    }
  }

  const fallbacks = fuzzyFallbackVariants(parsed);
  const merged: WebMediaItem[] = [];
  const seen = new Set<string>();
  let fallbackPage: { page: T; query: string } | null = null;
  for (const query of fallbacks) {
    const page = await fetchPage(query);
    for (const item of page.items ?? []) {
      const key = identity(item);
      if (seen.has(key)) continue;
      seen.add(key);
      merged.push(item);
    }
    if (!fallbackPage && page.items?.length) fallbackPage = { page, query };
  }
  if (fallbackPage) {
    const typed = parsed.tolerantTitles[parsed.tolerantTitles.length - 1] ?? parsed.cleanedTitle;
    const ranked = rankByYear(rankFuzzyCandidates(merged, typed), parsed.year);
    return { page: { ...fallbackPage.page, items: ranked, sections: [] }, parsed, query: fallbackPage.query };
  }

  // Nothing anywhere: report the primary variant's (empty) answer.
  if (first) return { page: first.page, parsed, query: first.query };
  return { page: (await fetchPage(parsed.raw)), parsed, query: parsed.raw };
}
