# IMDb-keyed sync — watch progress, scrobbles & library

This is the hand-off contract for syncing content that has **no TMDB id**, only an
IMDb id (`tt1234567`). Until now the sync API was TMDB-only: progress, scrobble,
history and library records were keyed solely by an integer `tmdb_id`, so the
native client dropped any item it could only identify by an `tt…` id and it never
reached the server. The change below makes `imdb_id` a first-class, **purely
additive** field on both requests and responses.

> TL;DR: `tmdb_id` is now optional; a new optional `imdb_id` string is accepted on
> every write and echoed on every read. A request must carry **at least one** of
> the two. The server resolves `imdb_id → tmdb_id` via TMDB at write time (cached)
> so the same title reported under either id system collapses to one row and the
> TMDB-driven recommendation/activity features keep working. Existing tmdb-only
> traffic is byte-compatible.

All endpoints are authenticated by the opaque `user_key` first path segment, exactly
as before.

---

## 1. Field names & types (the agreement)

Wire fields are snake_case; the client decodes snake_case → camelCase, so
`imdb_id` ⇄ `imdbID`.

| Field      | Type            | Notes                                                        |
| ---------- | --------------- | ------------------------------------------------------------ |
| `tmdb_id`  | `int \| null`   | **Now nullable.** `null` only for imdb-only rows.            |
| `imdb_id`  | `string \| null`| New. `"tt1234567"`. `null` when unknown. Validated `^tt\d+$`.|
| `media_type` | `"movie" \| "tv"` | Unchanged wire values.                                    |

**Validation:** a write with neither `tmdb_id` nor a well-formed `imdb_id` is
rejected with `400`. An `imdb_id` that doesn't match `^tt\d+$` is ignored (and, if
it was the only id, the request 400s).

---

## 2. Requests (writes)

### `POST /api/v2/progress`
```jsonc
{
  "tmdb_id": 603,            // optional if imdb_id present
  "imdb_id": "tt0133093",    // optional if tmdb_id present
  "media_type": "movie",      // "movie" | "tv"
  "position": 1234,           // seconds
  "duration": 8100,           // seconds
  "season": null,             // required for tv (unless resolved from an episode imdb id)
  "episode": null,
  "title": "The Matrix"
}
```

### `POST /api/v2/scrobble`
```jsonc
{
  "tmdb_id": 603,            // optional if imdb_id present
  "imdb_id": "tt0133093",    // optional if tmdb_id present
  "media_type": "movie",
  "action": "complete",
  "season": null,
  "episode": null,
  "title": "The Matrix"
}
```

### Library writes
- `POST /api/v2/library` (add) and each item in `POST /api/v2/library/sync`
  (`added[]` / `removed[]`) accept the same optional `tmdb_id` + `imdb_id` pair.
- `DELETE /api/v2/library/{media_type}/{item_id}` — `item_id` is **either** a
  numeric TMDB id **or** an IMDb id (`tt…`); the key is chosen by its shape, so
  tmdb-only clients are unaffected.

The client should send `imdb_id` whenever an item lacks a TMDB id. (Previously it
gated writes on `ScrobbleService.extractTMDBID` and sent `tmdb_id` as a required
int — that gate must be relaxed to also send `imdb_id`.)

---

## 3. Responses (reads)

Every history and library item now includes an optional `imdb_id` alongside the
(now-nullable) `tmdb_id`.

### `GET /api/v2/history?limit=N`
```jsonc
{
  "items": [
    {
      "tmdb_id": 603,            // null for imdb-only rows
      "imdb_id": "tt0133093",    // null when unknown
      "media_type": "movie",
      "title": "The Matrix",
      "position": 1234,
      "duration": 8100,
      "updated_at": "2026-06-29T12:00:00Z",
      "season": null,            // present only for tv
      "episode": null
    }
  ]
}
```

### `GET /api/v2/library`
Each item gains `"imdb_id": string | null` next to `"tmdb_id"`; all other fields
are unchanged.

**Why echo `imdb_id`:** device B stores its local catalog row under whatever id the
catalog gave it — often the IMDb id. To match a synced record to its local
`tt…`-keyed item, the server must echo the `imdb_id` it stored. `imdb_id` is
populated from the id on the row, falling back to a cheap `MediaMetadata` lookup by
`tmdb_id` (no extra TMDB call); otherwise `null`.

---

## 4. Server-side canonicalization (how a row gets its ids)

When a write arrives with `imdb_id` and **no** `tmdb_id`, the server resolves it via
TMDB `GET /3/find/{imdb_id}?external_source=imdb_id`:

- `movie_results[0].id` → `tmdb_id`, media_type `movie`.
- `tv_results[0].id` → `tmdb_id`, media_type `tv` (series).
- `tv_episode_results[0]` → `show_id` (the series `tmdb_id`) + `season_number` +
  `episode_number`, media_type `tv` (these override/fill `season`/`episode`).

- **On success:** the row is stored under the canonical `tmdb_id` (+ resolved
  media_type/season/episode) **and** retains the originating `imdb_id`.
- **On not-found / failure:** the row is stored **imdb-only** (`tmdb_id = null`). It
  still syncs cross-device; it just doesn't feed TMDB-only features. The write is
  never dropped.

Resolutions are cached persistently (`imdb_resolutions` table) so each `imdb_id` is
resolved at most once (negatives retried after a cooldown / a later write). The
TMDB call is bounded (≈4 s) and best-effort, so a slow TMDB never stalls a progress
write — the imdb-only fallback is stored and upgraded on the next write.

### Identity / dedup
Canonical row identity is `(tmdb_id, media_type, season, episode)`; imdb-only rows
use `(imdb_id, season, episode)`. When a previously imdb-only row later resolves to
a `tmdb_id` (or a tmdb row and an imdb row meet), they are **merged** into one
canonical row — newer `updated_at` wins, the known ids are unioned, the duplicate is
dropped. Progress/duration are last-write-wins. Duplicate/near-duplicate writes (the
client throttles progress to ~1 POST / 30 s per item) never create duplicate rows.

---

## 5. Backward compatibility

- tmdb-only requests behave **exactly** as before; responses are byte-compatible
  except for the additive `imdb_id` field (which is `null` when unknown).
- Auth is unchanged (per-`user_key` path segment).
- TMDB-keyed recommendation and activity logic continues to read `tmdb_id` and is unaffected.
