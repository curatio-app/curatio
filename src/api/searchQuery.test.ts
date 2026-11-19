import { describe, expect, it, vi } from 'vitest';
import {
  fuzzyFallbackVariants,
  parseReleaseYear,
  parseSearchQuery,
  rankByYear,
  remoteQueryVariants,
  searchWithFallbacks,
} from './searchQuery';
import type { WebMediaItem } from './types';

// Mirrors SearchQueryNormalizerTests in the Apple clients' Tests/LibraryServiceTests.swift.
const NOW = new Date('2026-11-10T12:00:00Z');
const parse = (raw: string) => parseSearchQuery(raw, NOW);
const lower = (values: string[]) => values.map((v) => v.toLowerCase());

describe('parseSearchQuery', () => {
  it('strips a trailing year', () => {
    const parsed = parse('girl next door 2004');
    expect(parsed.cleanedTitle.toLowerCase()).toBe('girl next door');
    expect(parsed.year).toBe(2004);
  });

  it('strips a parenthesized year', () => {
    const parsed = parse('The Matrix (1999)');
    expect(parsed.cleanedTitle.toLowerCase()).toBe('the matrix');
    expect(parsed.year).toBe(1999);
  });

  it('offers a leading-article variant', () => {
    const parsed = parse('The Matrix');
    expect(parsed.year).toBeNull();
    expect(parsed.articleStripped?.toLowerCase()).toBe('matrix');
  });

  it('keeps a pure-year query as text', () => {
    const parsed = parse('2012');
    expect(parsed.cleanedTitle).toBe('2012');
    expect(parsed.year).toBeNull();
  });

  it('does not mistake a title year for a release filter', () => {
    const odyssey = parse('2001: A Space Odyssey');
    expect(odyssey.cleanedTitle).toBe('2001: A Space Odyssey');
    expect(odyssey.year).toBeNull();

    const future = parse('Blade Runner 2049');
    expect(future.cleanedTitle).toBe('Blade Runner 2049');
    expect(future.year).toBeNull();
  });

  it('still parses the year when request filler follows it', () => {
    const parsed = parse('The Office 2005 please');
    expect(parsed.year).toBe(2005);
    expect(parsed.tolerantTitles[0]?.toLowerCase()).toBe('the office');
  });

  it('reduces conversational phrasing to conservative, then intent, fallbacks', () => {
    const parsed = parse('please show me The Matrix movie');
    expect(lower(parsed.tolerantTitles)).toEqual(['show me the matrix movie', 'matrix']);
    expect(lower(remoteQueryVariants(parsed))).toContain('matrix');
    // The literal query stays first.
    expect(remoteQueryVariants(parsed)[0]).toBe('please show me The Matrix movie');

    const realTitle = parse('please Show Me a Hero');
    expect(realTitle.tolerantTitles[0]?.toLowerCase()).toBe('show me a hero');
  });

  it('gives a plain title no conversational reading', () => {
    expect(parse('The Office').tolerantTitles).toEqual([]);
  });
});

describe('remoteQueryVariants', () => {
  it('orders and de-duplicates', () => {
    expect(lower(remoteQueryVariants(parse('The Office 2005')))).toEqual(['the office', 'office']);
    expect(lower(remoteQueryVariants(parse('office')))).toEqual(['office']);
  });

  it('never sends the year to the server', () => {
    const parsed = parse('Wonder Woman 1984');
    expect(parsed.year).toBe(1984);
    expect(remoteQueryVariants(parsed).some((v) => v.includes('1984'))).toBe(false);
  });
});

describe('parseReleaseYear', () => {
  it('reads the first plausible year', () => {
    expect(parseReleaseYear('2004')).toBe(2004);
    expect(parseReleaseYear('2011–2019')).toBe(2011);
    expect(parseReleaseYear(1999)).toBe(1999);
    expect(parseReleaseYear(null)).toBeNull();
    expect(parseReleaseYear('Limited Series')).toBeNull();
  });
});

describe('fuzzyFallbackVariants', () => {
  it('covers trailing typos', () => {
    expect(lower(fuzzyFallbackVariants(parse('gladiater')))).toContain('gladiat');
    expect(lower(fuzzyFallbackVariants(parse('inceptoin')))).toContain('incep');
    expect(fuzzyFallbackVariants(parse('the girl next dor'))[0]?.toLowerCase()).toBe('the girl next');
  });

  it('skips short queries and never repeats primary variants', () => {
    expect(fuzzyFallbackVariants(parse('up'))).toEqual([]);
    expect(fuzzyFallbackVariants(parse('dune'))).toEqual([]);

    const parsed = parse('the matrix');
    const primary = new Set(lower(remoteQueryVariants(parsed)));
    const fallbacks = fuzzyFallbackVariants(parsed);
    expect(fallbacks.every((v) => !primary.has(v.toLowerCase()))).toBe(true);
    expect(fallbacks.every((v) => v.length >= 5)).toBe(true);
  });

  it('works from the intent reading of a conversational query', () => {
    expect(lower(fuzzyFallbackVariants(parse('please show me gladiater')))).toContain('gladiat');
  });
});

describe('rankByYear', () => {
  const item = (id: number, title: string, year?: number | string): WebMediaItem => ({ id, tmdb_id: id, title, year });

  it('moves the queried year ahead and a wrong year behind unknown', () => {
    const ranked = rankByYear([item(1, 'Dune', 1984), item(2, 'Dune'), item(3, 'Dune', 2021)], 2021);
    expect(ranked.map((i) => i.id)).toEqual([3, 2, 1]);
  });

  it('treats a title carrying the year as a match', () => {
    const ranked = rankByYear([item(1, 'Wonder Woman', 2017), item(2, 'Wonder Woman 1984', 2020)], 1984);
    expect(ranked[0].id).toBe(2);
  });

  it('leaves the order alone without a year', () => {
    const items = [item(1, 'B', 2000), item(2, 'A', 2001)];
    expect(rankByYear(items, null)).toBe(items);
  });
});

describe('searchWithFallbacks', () => {
  const page = (titles: string[]) => ({ items: titles.map((title, i) => ({ id: title, tmdb_id: i + 1, title })) });

  it('sends one year-free request for an ordinary query and ranks by year', async () => {
    const fetchPage = vi.fn(async (_q: string) => ({
      items: [
        { id: 1, tmdb_id: 1, title: 'The Matrix Resurrections', year: 2021 },
        { id: 2, tmdb_id: 2, title: 'The Matrix', year: 1999 },
      ],
    }));
    const { page: result, query } = await searchWithFallbacks('The Matrix (1999)', fetchPage, NOW);
    expect(fetchPage.mock.calls.map((c) => c[0])).toEqual(['The Matrix']);
    expect(query).toBe('The Matrix');
    expect(result.items?.[0].title).toBe('The Matrix');
  });

  it('falls through conversational readings until one answers', async () => {
    const fetchPage = vi.fn(async (q: string) => (q === 'Matrix' ? page(['The Matrix']) : page([])));
    const { page: result } = await searchWithFallbacks('please show me The Matrix movie', fetchPage, NOW);
    expect(result.items?.map((i) => i.title)).toEqual(['The Matrix']);
    expect(fetchPage.mock.calls.at(-1)?.[0]).toBe('Matrix');
  });

  it('retries a title prefix when nothing matches ("gladiater" finds Gladiator)', async () => {
    const fetchPage = vi.fn(async (q: string) =>
      'gladiator'.startsWith(q.toLowerCase()) ? page(['Gladiators of Rome', 'Gladiator']) : page([]),
    );
    const { page: result } = await searchWithFallbacks('gladiater', fetchPage, NOW);
    expect(fetchPage.mock.calls.map((c) => c[0])).toEqual(['gladiater', 'gladiat', 'gladi']);
    expect(result.items?.map((i) => i.title)).toEqual(['Gladiator', 'Gladiators of Rome']);
  });

  it('returns the empty primary answer when every retry is empty', async () => {
    const fetchPage = vi.fn(async (_q: string) => page([]));
    const { page: result, query } = await searchWithFallbacks('zzzzzzzz', fetchPage, NOW);
    expect(result.items).toEqual([]);
    expect(query).toBe('zzzzzzzz');
  });

  it('keeps genre sections from the server as an answer', async () => {
    const fetchPage = vi.fn(async (_q: string) => ({ items: [], sections: [{ items: [] }] }));
    await searchWithFallbacks('horror movies', fetchPage, NOW);
    expect(fetchPage).toHaveBeenCalledTimes(1);
  });

  it('propagates a cancellation instead of retrying', async () => {
    const abort = new DOMException('Aborted', 'AbortError');
    const fetchPage = vi.fn(async (_q: string) => { throw abort; });
    await expect(searchWithFallbacks('gladiater', fetchPage, NOW)).rejects.toBe(abort);
    expect(fetchPage).toHaveBeenCalledTimes(1);
  });
});
