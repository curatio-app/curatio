# Curatio Native API — iOS / iPadOS Handoff

**Contract revision:** 2026-08-10
**Server:** Curatio 1.0  
**Audience:** native iOS/iPadOS client developers

This is the implementation contract for Curatio's profile-aware native discovery
API. It covers the new home, search, tag browsing, details, reviews, content
ratings, and coming-soon fields, plus the profile calls needed to switch viewers.

The routes are currently not URL-versioned. Treat the shapes in this document as
the native v1 contract and decode optional fields defensively.

## 1. Base URL and authentication

All native content calls go to `/api/v2/...` and send the active profile's opaque
`user_key` as a Bearer credential:

```text
GET https://curatio.example.com/api/v2/home
Authorization: Bearer <user_key>
```

The key is never part of a URL. Possession of a profile's `user_key` grants
access to that profile and its sibling-profile management routes. Always use
HTTPS, and never put the key or the `Authorization` header in analytics or logs.
A missing, unknown or rotated key returns **401** (`WWW-Authenticate: Bearer`):
sign the profile out and pair again. Responses carry `Vary: Authorization`, so
clear any URL-keyed response cache when switching profiles.

Store `user_key` values in Keychain rather than `UserDefaults`. Apple describes
Keychain as storage for passwords, keys, and other small secret values:
[Keychain services](https://sosumi.ai/documentation/security/keychain-services).

Switching profiles is a local credential swap: replace the active `user_key`,
clear profile-scoped in-memory data, then fetch bootstrap for the new key. There
is intentionally no native `select profile` endpoint.

### How the app obtains a key

The existing mobile PKCE exchange returns:

```json
{
  "userKey": "profile-secret-key",
  "baseURL": "https://curatio.example.com",
  "apiBase": "https://curatio.example.com"
}
```

After sign-in, call `GET /api/v2/profiles` to discover the account's other
profiles and their keys.

## 2. Recommended app startup flow

1. Load the active profile `user_key` from Keychain.
2. Start `GET /api/v2/home` while the full tvOS intro plays. This returns
   all row descriptors, eight hero candidates, and the first six rows with twenty
   cards each. Restore a profile-scoped saved feed independently of the network.
3. After the intro, reveal available content. Fetch at most three nearby row
   previews or horizontal continuations with `POST /api/v2/home/rows`.
   Do not fetch the entire 40-row, 80-card-per-row inventory during startup.
4. Keep the snapshot ID and ETag with the saved feed. Revalidate with
   `If-None-Match` on `GET /api/home`; `304` keeps current data. A `202` means the
   durable server worker is preparing a first snapshot; retry after one second
   within a bounded client loading window. Preserve usable content on failures.
5. Load `GET /api/v2/facets` only when the user opens browse/filter UI.
6. Call `GET /api/v2/details?include=meta&view=hero` when an item is
   opened. Once `hero_complete` is true and required artwork is decoded, present
   the hero and request the default full view for the sections below it.
7. On profile switch, cancel outstanding requests, swap the key, clear the old
   profile's UI state, and repeat from step 2.

None of the native discovery endpoints invokes an LLM. Browse ranking and
recommendations use the database's precomputed tag relevance. Details makes a
normal TMDB metadata request and combines it with stored Curatio/MDBList facts.

### Progressive Home (schema version 1)

`GET /api/v2/home` returns:

```json
{
  "schema_version": 1,
  "snapshot_id": "immutable-profile-snapshot-uuid",
  "discovery_revision": "immutable-profile-snapshot-uuid",
  "row_descriptors": [{"id": "personal-picks-movie", "title": "Your Picks", "type": "movie", "total": 80}],
  "hero_candidates": [],
  "hero_source_row_id": "personal-picks-movie",
  "rows": [{"id": "personal-picks-movie", "title": "Your Picks", "type": "movie", "items": [], "cursor": 0, "next_cursor": 20, "total": 80}]
}
```

The example omits card bodies. Each card uses the existing `fields=card`
preview shape (`id`, `type`, `name`, `tmdbId`, `poster`, `background`, etc.). Row
types use `movie`/`tv`; card types retain `movie`/`series`. Descriptors cover all
ordered rows (at most 40), even when only the first six have cards loaded. The
24-card minimum density applies to full membership, never to a twenty-card
preview. Hero candidates come from one identified source row.

For vertical lookahead or horizontal paging:

```http
POST /api/v2/home/rows
Content-Type: application/json

{"snapshot_id":"immutable-profile-snapshot-uuid","rows":[{"id":"personal-picks-movie","cursor":20,"limit":20}]}
```

The response contains only `snapshot_id` and `rows` in the same page shape. A
request accepts one through three distinct rows and one through twenty cards per
row. Use `next_cursor` until null. Membership, row order, and cross-row title
deduplication remain frozen for the supplied snapshot; requests never assemble
the full Home or load metadata for unloaded members. Initial hydration is capped
at 120 visible cards plus at most eight distinct hero cards; each batch hydrates
at most 60 cards. Provider calls are never part of either route.

Unknown credentials return `401`, inactive profiles/accounts `403`, and a
missing, expired, or wrong-profile snapshot `409` with
`detail: "snapshot_expired"`. On `409`, request a fresh Home and retain the visible
feed until replacement is ready. Other malformed row requests return `422`.
Superseded snapshots remain available for 24 hours. A policy change that makes
old membership incompatible immediately prevents serving it. A continuation or
conditional Home request against incompatible published membership returns `412`
with `detail: "policy_invalidated"`; evict that cached feed before retrying an
unconditional Home request, which may return `202` while rebuilding.
This also applies when a compatible replacement is already published but the
client's native ETag names older incompatible membership. Unknown or pruned native
ETags conservatively return `412` because their cached policy cannot be verified.

`202` returns `{"snapshot_pending":true,"retry_after":1,"policy_fingerprint":"..."}`
and `Retry-After: 1`. It queues work but does not build membership in the request.
The last compatible complete snapshot stays usable while a refresh runs. The
ETag includes bounded card content as well as snapshot identity, so metadata
corrections cannot incorrectly receive `304`. All responses are private;
continuations and pending responses are `no-store`.

The database stores thin immutable membership and durable build leases. Startup
backfills active profiles, two off-loop builders drain the queue, source writes
invalidate in their existing transactions, and periodic reconciliation catches
rotation/shuffle boundaries. Expired leases recover interrupted builds; failed
refreshes preserve the last snapshot. No additional deployment service is needed.
Each request and build resolves the small set of persisted Home policy settings
directly, so toggles changed in one web worker apply consistently in the others.

The existing bootstrap, catalog-revision, and browser APIs retain their
contracts. Clients targeting older servers may use legacy bootstrap after an
explicit `404`/`405` endpoint-unavailable response. Timeouts, authorization errors,
and `202` must not trigger the legacy per-row request fan-out.

Local verification on 2026-10-04 used an in-memory SQLite fixture with 40 rows,
80 memberships per row, and 3,200 metadata records. The hero deliberately came
from an unloaded row, exercising the maximum 128 distinct initial-card reads.
Through FastAPI's in-process test client, the first request against a published
snapshot took 55.98 ms with seven SQL statements. Thirty subsequent `200` responses
measured 10.21 ms median / 10.90 ms p95; thirty conditional `304` responses measured
9.97 ms median / 11.16 ms p95. Initial JSON was 43,365 bytes (2,590 bytes when
locally gzipped at level nine). Titles/descriptions in this fixture are synthetic;
compression ratios and timings are not production or Apple TV launch measurements.
These numbers exclude first-snapshot construction, physical networking, artwork
decoding, and device rendering. The regression suite in
`tests/test_home_feed_snapshots.py` enforces hydration bounds, frozen membership,
lease recovery, policy/auth isolation, retention, and legacy ranking parity.

## 3. New discovery endpoints

| Method and path | Purpose |
| --- | --- |
| `GET /api/v2/bootstrap` | Account context, activity collections, and a page of the personalized home |
| `GET /api/v2/catalog-revision` | Small profile-scoped revision probe for foreground revalidation |
| `GET /api/v2/facets` | Browsable tags grouped by taxonomy facet |
| `GET /api/v2/browse` | Relevance-ranked results for arbitrary tag combinations |
| `GET /api/v2/search` | Title search, tag/vibe search, or title search filtered by tags |
| `GET /api/v2/details` | Full title details, ratings, reviews, cast, episodes, and similar titles |

### 3.1 Bootstrap

```http
GET /api/v2/bootstrap?cursor=0&limit=24
```

| Parameter | Type | Default | Rules |
| --- | --- | --- | --- |
| `cursor` | integer | `0` | 0 through 40; use the preceding page's `next_cursor` |
| `limit` | integer | `24` | 1 through 24 |
| `fields` | string | omitted | `card` reduces `dashboard.rows[].items` to the keys an Apple client decodes |

### Field projection

`fields=card` drops the row-item keys that exist only for the browser dashboard —
the raw `*_path` variants, the snake_case twins of fields already present in
camelCase, and the display fields the card preview model has no property for:

```text
title, media_type, overview, year, rating, subtitle, video_id, content_type,
rating_source, digital_release_date, next_episode_air_date, poster_path,
backdrop_path, logo_path, backdrop, content_rating, imdb_id, tmdb_id
```

Everything the Swift card model reads survives — including `description`,
`releaseInfo`, `contentRating`, `tmdbId`, `imdbId`, `background`, and
`comingSoonDate`. On a full page of deep catalog rows this substantially reduces
keyed decodes; the decode count is the reason to use it.

Only `dashboard.rows[].items` is projected. `continue_watching`, `recent_history`
and `library` keep the full shape, because their items carry the real
`season`/`episode`/`position`/`duration` values the projection removes.

Omitting the parameter returns the full shape, so an existing build is unaffected.

**`background`.** Row items carry the backdrop URL as both `backdrop` (browser
spelling) and `background` (card-model spelling). Apple clients read
`background`; it survives the projection, `backdrop` does not.

```json
{
  "account": {
    "id": 12,
    "email": "viewer@example.com",
    "display_name": "Alex",
    "totp_enabled": true,
    "active_profile_id": 456,
    "profiles": [
      {
        "id": 456,
        "display_name": "Alex",
        "avatar": "color:#4a8fd4",
        "bandwidth_tier": "high",
        "user_key": "profile-secret-key",
        "api_base": "https://curatio.example.com",
        "created_at": "2026-08-05T18:30:00"
      }
    ]
  },
  "dashboard": {
    "hero": { "tmdb_id": 550, "media_type": "movie", "title": "Fight Club" },
    "continue_watching": [],
    "recent_history": [],
    "library": [],
    "rows": [
      {
        "id": "personal-byw-movie-1",
        "name": "Because You Watched Transformers",
        "type": "movie",
        "media_type": "movie",
        "catalog_type": "movie",
        "source": "personal",
        "generation_method": "because_you_watched",
        "item_count": 80,
        "revision": "2026-08-06T12:00:00Z",
        "reason": {
          "kind": "because_you_watched",
          "seed": {
            "tmdb_id": 1858,
            "media_type": "movie",
            "title": "Transformers"
          },
          "basis_tags": ["Robots", "Alien Invasion", "Blockbuster"]
        },
        "items": []
      }
    ],
    "catalog_sync_pending": false,
    "catalog_revision": "758b76207aa99d83a60a38d9",
    "row_page": {
      "cursor": 0,
      "limit": 24,
      "total": 40,
      "next_cursor": 24
    }
  }
}
```

Notes:

- `hero` can be `null` when no suitable item exists.
- Every media collection can be empty.
- `rows` contains the profile's published synthesized and personal rows in
  server order. Do not re-sort them on the client. Row IDs identify the source
  (`personal-...` or `universal-...`); `catalog_type` is the content type
  `movie` or `series`.
- A complete native home contains at most 40 qualified discovery/personal
  shelves. Each response contains no more than 24 rows; follow
  `row_page.next_cursor` until it is `null`. The Apple client renders the first
  24 immediately and loads the remaining 16 as the full/deferred pass. Do not
  combine pages whose `catalog_revision` values differ; restart at cursor 0.
- A normal shelf has between 24 and 80 usable items after server-side quality
  filtering and page deduplication; the server does not pad a weak shelf. Continue Watching,
  Library, and recent history are separate activity collections and do not
  consume the 40-row budget.
- `source`, `generation_method`, `item_count`, `revision`, and `reason` are
  optional for backward-compatible decoding. New clients should use `reason`
  for explanation UI instead of parsing the display name.
- For `because_you_watched`, `reason.seed`, the row name, `revision`, and the
  returned items are from one database snapshot. Imported history never creates
  this claim: the seed represents completed Curatio playback. Every returned
  item passed the stored similarity/shared-tag floor.
- Refresh bootstrap after a profile switch or any progress/library mutation.
- Bootstrap responses are `Cache-Control: private, no-cache` and carry an ETag of
  `"{catalog_revision}:{cursor}:{limit}:{fields|full}"`. Keep the explicit native
  saved feed for offline/stale-if-error startup, but do not let a generic HTTP
  cache hide a changed server snapshot.
- **The server honors `If-None-Match`.** A matching tag returns `304` with an
  empty body, and skips assembling the page entirely rather than just skipping
  the transfer — so revalidating is far cheaper than refetching. The response
  shape is part of the tag, so switching `fields` never matches a stale entity.
- The revision behind that tag covers the profile's watch progress, completion
  events and library as well as the discovery rows, because this document carries
  all of them. A title started on another device therefore moves
  `catalog-revision`, which it previously did not.

### 3.2 Catalog revision

```http
GET /api/v2/catalog-revision
```

```json
{
  "catalog_revision": "758b76207aa99d83a60a38d9",
  "max_rows": 40,
  "page_size": 24
}
```

This endpoint is `Cache-Control: private, no-store`. It is intentionally small
enough for foreground revalidation and does not materialize or return catalog
items. A changed value means the synthesized/personal snapshot, rotation window,
or profile-scoped catalog state changed. Clear discovery request caches and
reload bootstrap from cursor 0. An unchanged value means the saved home can
remain mounted.

### 3.3 Facets

```http
GET /api/v2/facets?media_type=movie&min_titles=25
```

| Parameter | Type | Default | Rules |
| --- | --- | --- | --- |
| `media_type` | string | `movie` | `movie` or `tv` |
| `min_titles` | integer | `25` | 1 through 10,000 |

```json
{
  "media_type": "movie",
  "facets": [
    {
      "facet": "mood",
      "tags": [
        { "id": 44, "name": "Dark", "count": 1832 },
        { "id": 91, "name": "Feel-Good", "count": 946 }
      ]
    },
    {
      "facet": "theme",
      "tags": [
        { "id": 12, "name": "Time Travel", "count": 214 }
      ]
    }
  ],
  "total_tags": 3
}
```

`count` is the number of eligible titles carrying that tag for the requested
media type. Only active, displayable tags whose count reaches `min_titles` are
returned. Use the numeric tag `id` in browse/search requests; the displayed name
is not an identifier.

### 3.4 Browse by tags

```http
GET /api/v2/browse?tags=12,44&media_type=movie&mode=all&skip=0&limit=60
```

| Parameter | Type | Default | Rules |
| --- | --- | --- | --- |
| `tags` | comma-separated integers | empty | Maximum raw query length 400 characters |
| `media_type` | string | `movie` | `movie` or `tv` |
| `mode` | string | `all` | `all` = intersection, `any` = union |
| `skip` | integer | `0` | 0 through 10,000 |
| `limit` | integer | `60` | 1 through 120 |

```json
{
  "items": [
    {
      "tmdb_id": 550,
      "media_type": "movie",
      "title": "Fight Club",
      "year": "1999",
      "poster": "https://image.tmdb.org/t/p/w500/example.jpg",
      "rating": 8.4,
      "content_rating": "R",
      "relevance": 0.8731
    }
  ],
  "total_tags": 2,
  "has_more": true
}
```

Append the next page by increasing `skip` by the number of received items while
`has_more` is true. `relevance` is an opaque ranking score; display the
server-provided order and do not compare scores across unrelated tag queries.
With no valid tag IDs the endpoint returns an empty page.

### 3.5 Search

```http
GET /api/v2/search?q=cyberpunk
GET /api/v2/search?q=night&tags=12,44
```

| Parameter | Type | Required | Rules |
| --- | --- | --- | --- |
| `q` | string | yes | 1 through 80 characters |
| `tags` | comma-separated integers | no | Maximum raw query length 400 characters |

Normal title search:

```json
{
  "items": [
    { "tmdb_id": 603, "media_type": "movie", "title": "The Matrix" }
  ]
}
```

Exact tag/vibe search, used when the query names a tag but barely matches title
names:

```json
{
  "items": [],
  "result_type": "tag",
  "tag": { "id": 73, "name": "Cyberpunk", "facet": "theme" }
}
```

Title search intersected with supplied tags:

```json
{
  "items": [],
  "result_type": "filtered"
}
```

`result_type` is intentionally absent for ordinary title results. The returned
order is authoritative: exact/prefix relevance is resolved first, then the same
quality floor and consensus/Bayesian quality signal used by discovery catalogs
orders results within a relevance tier. A trailing four-digit year is treated as
a disambiguator rather than literal title text. Results come only from
Curatio's own catalog; treat all optional card metadata as nullable. The endpoint returns at most 40
items and does not currently paginate.

### 3.6 Details

Normal request:

```http
GET /api/v2/details?tmdb_id=550&media_type=movie
```

Fallback when the client only has an IMDb identifier:

```http
GET /api/v2/details?imdb_id=tt0137523
```

Rules:

- With `tmdb_id`, `media_type` is required and must be `movie` or `tv`.
- A positive `tmdb_id` or a valid `tt...` IMDb ID is required.
- With `imdb_id`, `media_type` is an optional hint. TMDB lists some works under
  both a movie and a TV record; the hint selects between records that exist and
  is otherwise ignored, so a stale type cannot redirect the lookup.
- IMDb-only requests are resolved to TMDB ID and media type by the server. When
  TMDB has no mapping the response is `404`.
- Complete successful responses include `Cache-Control: private, max-age=1800`;
  partial TV episode payloads use `private, no-store`.
- TV requests load regular seasons and episodes; specials (season 0) are omitted.
- Add `include=meta` when opening playback-capable details. The response then
  includes the compact `meta` block (playback identity and `videos`).
- `view=full` is the default and preserves the complete detail response.
  `view=hero` resolves hero facts without waiting for similar titles, vector
  ranking, reviews, or other lower-page enrichment. Both views share provider
  work and retain the same identity and playback keys.
- `hero_complete` states that hero sources have been resolved, including
  legitimately unavailable fields. Do not require a non-null logo, certification
  or rating. `details_complete` is false on hero-only responses; do not store
  those responses in the full-detail cache or render their abbreviated cast as
  a complete lower-page rail. Older servers omit both flags and return the full
  response.
- The hero view includes title/artwork URLs, synopsis, release/runtime facts,
  genres, certification, ratings, trailers, up to three cast names and two
  directors. Series retain the complete regular-season episode index (IDs,
  season/episode numbers, titles, summaries and air dates) so local resume and
  Up Next selection remains accurate. Season posters and episode thumbnails
  are deferred to the full view. A failed or truncated expected season keeps
  `hero_complete` and `episodes_complete` false and disables response caching.
- An explicitly failed appended hero source (credits, images, trailers, external
  IDs or certification/release dates) keeps `hero_complete` false and uses
  `private, no-store`, even if TMDB returned HTTP 200. A successful empty source
  is legitimate absence; below-hero review failures do not block the hero.
- A provider-confirmed zero-episode series can have a complete empty index. When
  its first-air date is still upcoming, `item.comingSoonDate` carries that date
  so the client can show a reminder hero. Accept the empty index only with
  explicit readiness, no expected episodes and an upcoming date in the viewer's
  timezone; an omitted provider count is not proof that no episodes exist.
- Hero-only results have a separate process cache and never replace a durable
  full snapshot. Complete durable snapshots can serve the hero directly without
  recalculating below-hero fields. `refresh=true` refreshes the hero's shared core
  and the following full request reuses that core instead of the old snapshot.

Abbreviated response:

```json
{
  "item": {
    "tmdb_id": 550,
    "media_type": "movie",
    "title": "Fight Club",
    "year": "1999",
    "overview": "...",
    "genres": ["Drama"],
    "poster": "https://image.tmdb.org/t/p/w500/example.jpg",
    "backdrop": "https://image.tmdb.org/t/p/w780/example.jpg",
    "logo": null,
    "rating": 8.4,
    "runtime": 139,
    "imdb_id": "tt0137523",
    "video_id": "tt0137523",
    "content_type": "movie",
    "addon_type": "movie",
    "content_rating": "R"
  },
  "cast": [
    { "name": "Edward Norton", "character": "The Narrator", "profile": "https://..." }
  ],
  "crew": {
    "creators": [],
    "directors": ["David Fincher"],
    "writers": ["Jim Uhls"]
  },
  "seasons": [],
  "similar": [],
  "similar_source": "tags",
  "tags": [
    { "id": 44, "name": "Dark", "facet": "mood", "confidence": 0.91 }
  ],
  "ratings": [
    { "source": "imdb", "label": "IMDb", "value": 8.8, "normalized": 8.8, "votes": 2400000 },
    { "source": "tomatoes", "label": "Rotten Tomatoes", "value": 80, "normalized": 8.0, "votes": null }
  ],
  "reviews": [],
  "review_summary": {
    "returned": 0,
    "total_available": 0,
    "average_user_rating": null,
    "rated_review_count": 0,
    "sources": []
  },
  "tagline": "Mischief. Mayhem. Soap.",
  "status": "Released",
  "original_language": "en",
  "homepage": null,
  "content_rating": "R",
  "content_advisory": {
    "certification": "R",
    "age_rating": 17,
    "common_sense": null,
    "source": "database"
  },
  "keywords": ["dual identity", "anti-consumerism"],
  "trailers": [
    {
      "id": "video-id",
      "name": "Official Trailer",
      "type": "trailer",
      "site": "youtube",
      "key": "abc123",
      "url": "https://www.youtube.com/watch?v=abc123",
      "official": true,
      "published_at": "2020-01-01T00:00:00Z"
    }
  ],
  "studios": ["20th Century Fox"],
  "networks": [],
  "countries": ["United States of America"]
}
```

#### Inline playback metadata

Request details and the playback identity in one trip:

```http
GET /api/v2/details?tmdb_id=550&media_type=movie&include=meta
```

The additive `meta` object always contains `id`, `type`, `name`, and a `videos`
array whose entries contain `id`. Movies also contain
`default_video_id`. Series deliberately omit it: the
client should retain the show-level dispatch identity from `meta.imdb_id` or
`meta.id`, while `videos[].id` supplies episode playback IDs. IMDb-only detail
requests retain the exact requested `tt...` identity for both movies and series.

For series, `meta.episodesComplete` says whether every advertised regular season
was loaded successfully. Do not treat an empty or short `videos` array as
authoritative when it is `false`.

```json
{
  "meta": {
    "id": "tmdb:123",
    "type": "series",
    "name": "Example Series",
    "imdb_id": "tt1234567",
    "episodesComplete": true,
    "videos": [
      {
        "id": "tmdb:123:1:1",
        "title": "Episode 1",
        "season": 1,
        "episode": 1
      }
    ]
  }
}
```

#### Batch prefetch

Up to 40 details can be fetched in one request. Prefer typed IDs so a batch can
mix movies and series:

```http
GET /api/v2/details?ids=movie:550,tv:1399&include=meta
```

Repeated `ids` parameters are also accepted. Bare numeric or `tmdb:` IDs must
be paired with one request-wide `media_type`; `tt...` IMDb IDs are resolved by
the server. `series:` is accepted as an alias for `tv:`.
Add `view=hero` to prefetch only hero data; each result carries its own readiness
flags and can later be enriched with the default full view.

Batch failures are per-ID and do not fail successful peers:

```json
{
  "results": [
    {
      "id": "movie:550",
      "details": {
        "item": { "tmdb_id": 550, "media_type": "movie" },
        "meta": { "id": "tmdb:550", "type": "movie", "name": "Fight Club" }
      }
    }
  ],
  "errors": [
    { "id": "tv:999999", "status": 502, "detail": "Could not load details" }
  ],
  "requested": 2,
  "returned": 1
}
```

The server returns `400` when more than 40 IDs are supplied. A `200` batch can
still contain per-ID `400`, `404`, or `502` entries in `errors`. Duplicate input
tokens remain duplicated in the ordered response but share one server-side
fetch. A batch containing a transient `5xx` result uses `private, no-store`, so
retrying the same URL can reach the server.

#### TV seasons

For TV, each `seasons` entry has this shape:

```json
{
  "season": 1,
  "title": "Season 1",
  "overview": "...",
  "poster": "https://...",
  "episode_count": 10,
  "episodes_complete": true,
  "episodes": [
    {
      "season": 1,
      "episode": 1,
      "title": "Episode 1",
      "overview": "...",
      "air_date": "2026-08-05",
      "still": "https://...",
      "tmdb_id": 123,
      "media_type": "tv",
      "video_id": "tmdb:123:1:1",
      "content_type": "series"
    }
  ]
}
```

Episode entries carry `content_type` only. `addon_type` is present on the detail
`item` and on `similar[]` entries, not on episodes.

TV detail responses also contain the rollup `episodes_complete`. Season calls
retry one TMDB 5xx once, then remain best-effort: a failed season is present
with `episodes_complete: false` and an empty `episodes` array. Any such partial
response uses `Cache-Control: private, no-store` and is not inserted into the
server's one-hour detail cache.

#### Similar titles

`similar_source` controls the rail label:

- `tags`: Curatio's tag-vector similarity; the client may label it **More Like
  This**.
- `tmdb`: TMDB fallback; use a neutral label such as **You May Also Like**.
- `catalog`: local catalog fallback when neither of the above produced a rail;
  label it as neutrally as `tmdb`.

Every entry carries `media_type` and `addon_type`, whatever the source.

#### Ratings

`value` is on the source's native scale. `normalized` is always the comparable
0–10 value. For example, Rotten Tomatoes can have `value: 83` and
`normalized: 8.3`. Render the source-native `value` with source-appropriate
formatting; use `normalized` only for comparisons or generic visuals.

#### Reviews

TMDB reviews preserve the author and canonical URL:

```json
{
  "id": "review-id",
  "source": "tmdb",
  "source_name": "TMDB",
  "kind": "user_review",
  "author": "Alex",
  "username": "alex92",
  "avatar": "https://image.tmdb.org/t/p/w185/avatar.jpg",
  "rating": 8.5,
  "content": "Review text",
  "created_at": "2026-01-01T00:00:00Z",
  "updated_at": "2026-01-02T00:00:00Z",
  "url": "https://www.themoviedb.org/review/review-id"
}
```

Stored MDBList prose is deliberately presented as an unattributed excerpt:

```json
{
  "id": "mdblist-excerpt-1",
  "source": "mdblist",
  "source_name": "MDBList",
  "kind": "review_excerpt",
  "author": null,
  "username": null,
  "avatar": null,
  "rating": null,
  "content": "Short excerpt",
  "created_at": null,
  "updated_at": null,
  "url": null
}
```

The server returns at most 10 reviews and truncates each review to 2,000
characters. `review_summary.total_available` can therefore be greater than
`review_summary.returned`. There is no paginated review endpoint in this
contract, so do not show a working “See all reviews” action yet. Render content
as user-supplied plain text, show attribution only when present, and open `url`
only when non-null.

## 4. Common media item contract

Collections return a normalized media item. Most fields are nullable because
catalog, progress, search, and live TMDB sources do not all know the same facts.

| Field | Type | Notes |
| --- | --- | --- |
| `tmdb_id` | integer or null | Primary content identifier when available |
| `imdb_id` | string or null | `tt...` identifier when available |
| `media_type` | string | Native value is `movie` or `tv` |
| `content_type` | string or null | `movie` or `series` |
| `addon_type` | string or null | Same `movie`/`series` value as `content_type`. Present on the details `item` and `similar[]` only, not on collection items |
| `title` | string or null | Display title |
| `subtitle` | string or null | Year, or `S1:E2` for progress items |
| `year` | string/integer/null | Treat as display text rather than arithmetic input |
| `overview` | string or null | Summary |
| `genres` | array of strings | Can be empty |
| `poster`, `backdrop`, `logo` | URL string or null | Fully-qualified image URLs |
| `poster_path`, `backdrop_path`, `logo_path` | string or null | Raw TMDB paths; normally prefer full URLs |
| `rating` | number/string/null | Primary title score |
| `content_rating` | string or null | Certification such as `PG-13`, `R`, `TV-14`, `TV-MA` |
| `comingSoonDate` | string, optional | Future/today watchable date, `yyyy-MM-dd` |
| `season`, `episode` | integer/null | Episode identity where relevant |
| `position`, `duration` | integer/null | Playback seconds |
| `progress_percent` | integer/null | 0–100 when duration is known |
| `updated_at`, `completed_at` | timestamp/null | ISO-8601 timestamp |
| `video_id` | string/null | Pass through to stream/playback routes |

Unknown JSON fields must be ignored. Missing, `null`, and empty collections are
normal states, not decoding errors.

### Content rating behavior

Card APIs use the certification already stored in the Curatio database, so a
grid does not need one details request per card. Details prefers the stored
certification and falls back to live TMDB certification. Ratings are advisory:
the server does not currently filter content by certification or enforce a kids
profile policy.

Native fields use `content_rating`. The inline `meta` block and card aliases use
camelCase `contentRating`; do not merge those two wire contracts.

### `comingSoonDate` behavior

`comingSoonDate` is intentionally camelCase everywhere and is included only
when the date is today or later:

- Movies: digital/home release date, not theatrical release date.
- TV: next episode air date.
- Past or unknown date: field omitted, not `null`.

Absence means “no future date the server can assert”; it does not by itself
prove that a stream is available. Preserve it as a day-only string and localize
the UI label on-device (for example, “Coming Friday”).

## 5. Profile endpoints

| Method and path | Body | Response |
| --- | --- | --- |
| `GET /api/v2/profiles` | none | `{ active_profile_id, profiles }` |
| `POST /api/v2/profiles` | profile fields | `{ status: "ok", profile }` |
| `PATCH /api/v2/profiles/{profile_id}` | changed profile fields | `{ status: "ok", profile }` |
| `DELETE /api/v2/profiles/{profile_id}` | none | `{ status: "ok" }` |

Profile create/update body:

```json
{
  "display_name": "Kids",
  "avatar": "color:#4ab46a",
  "bandwidth_tier": "low"
}
```

- `bandwidth_tier` is `low` (optimized playback quality) or `high` (original file).
- `avatar` is opaque to the server; the web app currently uses `color:#RRGGBB`.
- Accounts can have at most 10 profiles.
- The only profile cannot be deleted.
- Any sibling profile key can manage profiles in the same account.

## 6. Existing profile-scoped companion routes

These predate the new discovery API and remain supported:

| Method and path | Purpose |
| --- | --- |
| `POST /api/v2/progress` | Upsert continue-watching progress |
| `GET /api/v2/history?limit=50` | In-progress items |
| `DELETE /api/v2/progress/{media_type}/{item_id}` | Remove progress/completion state |
| `GET /api/v2/library` | Saved library |
| `POST /api/v2/library` | Add an item, idempotently |
| `DELETE /api/v2/library/{media_type}/{item_id}` | Remove an item |
| `POST /api/v2/library/sync` | Reconcile offline library changes |
| `POST /api/v2/scrobble` | Record completion |
| `GET /api/v2/segments/{video_id}` | Intro/credits markers |

For full progress/library identity rules, see
[`imdb-keyed-sync.md`](imdb-keyed-sync.md). For authentication and profile CRUD
details, see [`multi-profile-ios.md`](multi-profile-ios.md).

## 7. Errors

FastAPI errors use a JSON `detail` value:

```json
{ "detail": "Title not found" }
```

| Status | Meaning | Client behavior |
| --- | --- | --- |
| `400` | Missing/invalid identifier or illegal operation | Show a non-retryable error; fix the request |
| `403` | Profile/account exists but is inactive | Stop requests and require account recovery/sign-in |
| `404` | Unknown/deleted `user_key`, profile, or title | Re-fetch profiles or return to search as appropriate |
| `422` | Query/body failed schema validation | Treat as a client bug; log a redacted diagnostic |
| `429` | Rate limited | Honor `Retry-After` when present and retry with backoff |
| `502` | TMDB detail lookup failed | Keep the card screen, show retry UI for details |
| `5xx` | Temporary server failure | Exponential backoff with jitter; do not spin |

If the active `user_key` starts returning 404, it may have been deleted on
another device. Remove that credential locally, try a known sibling key if one
exists, and re-fetch the profile list. Otherwise require sign-in again.

## 8. Swift decoding guidance

The native API is mostly `snake_case`, with intentional camelCase exceptions
such as `comingSoonDate`. This decoder works for both:

```swift
let decoder = JSONDecoder()
decoder.keyDecodingStrategy = .convertFromSnakeCase
```

Apple notes that snake-case conversion does not infer acronym/initialism
capitalization. Prefer Swift properties such as `tmdbId` and `imdbId`, or use
explicit `CodingKeys` if the codebase requires `tmdbID`/`imdbID`:
[convertFromSnakeCase](https://sosumi.ai/documentation/foundation/jsondecoder/keydecodingstrategy-swift.enum/convertfromsnakecase).

Recommended core models:

```swift
import Foundation

enum MediaType: String, Decodable, Encodable {
    case movie
    case tv
}

struct MediaItem: Decodable {
    let tmdbId: Int?
    let imdbId: String?
    let mediaType: MediaType?
    let addonType: String?
    let title: String?
    let subtitle: String?
    let year: StringOrInt?
    let overview: String?
    let genres: [String]?
    let poster: URL?
    let backdrop: URL?
    let logo: URL?
    let rating: Double?
    let contentRating: String?
    let comingSoonDate: String?
    let videoId: String?
    let season: Int?
    let episode: Int?
    let position: Int?
    let duration: Int?
    let progressPercent: Int?

    var stableId: String? {
        if let tmdbId { return "\(mediaType?.rawValue ?? "unknown"):\(tmdbId)" }
        if let imdbId { return imdbId }
        return videoId
    }
}

enum StringOrInt: Decodable {
    case string(String)
    case int(Int)

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if let int = try? value.decode(Int.self) { self = .int(int); return }
        self = .string(try value.decode(String.self))
    }
}
```

`addonType` is populated on the details `item` and on `similar[]` entries. It is
absent on collection items, where the content type comes from `mediaType`
instead — map `tv` to `series` and everything else to `movie`. A build needs
nothing beyond these two.

For stable SwiftUI identity, production code should prefer
`(media_type, tmdb_id)`, then IMDb ID, then `video_id`, and drop items with no
stable identifier if necessary. Do not generate a new UUID each time a decoded
card is rendered.

Treat `yyyy-MM-dd` values as calendar days, not instants. Keeping
`comingSoonDate`/`air_date` as strings at the transport layer avoids timezone
shifts; convert them to a localized display value in a dedicated formatter.
Timestamps such as `created_at` and `updated_at` are ISO-8601 instants.

## 9. Request construction example

Use `URLComponents` for query values rather than interpolating user search text:

```swift
func detailsURL(
    baseURL: URL,
    userKey: String,
    tmdbId: Int,
    mediaType: MediaType
) -> URL? {
    var components = URLComponents(
        url: baseURL
            .appendingPathComponent(userKey)
            .appendingPathComponent("api")
            .appendingPathComponent("details"),
        resolvingAgainstBaseURL: false
    )
    components?.queryItems = [
        URLQueryItem(name: "tmdb_id", value: String(tmdbId)),
        URLQueryItem(name: "media_type", value: mediaType.rawValue),
        URLQueryItem(name: "include", value: "meta")
    ]
    return components?.url
}
```

Use an ephemeral or normal `URLSession` according to the app's image/network
cache policy, but ensure observability tooling redacts the first path component
after the host because it contains the credential.

## 10. Caching and refresh rules

- Respect response cache headers. Details currently permits private caching for
  30 minutes.
- Do not set `URLSessionConfiguration.urlCache` to `nil`, and do not use
  `.reloadIgnoringLocalCacheData` for JSON API requests. Those settings disable
  or bypass the server-directed cache. Use a memory-only cache if persisting
  profile-scoped responses to disk is undesirable:

  ```swift
  let configuration = URLSessionConfiguration.default
  configuration.urlCache = URLCache(
      memoryCapacity: 64 * 1024 * 1024,
      diskCapacity: 0
  )
  configuration.requestCachePolicy = .useProtocolCachePolicy
  ```

  Requests should likewise keep `.useProtocolCachePolicy`. Apple documents
  that `urlCache = nil` disables caching and that `.useProtocolCachePolicy`
  honors fresh HTTP responses:
  [URLSessionConfiguration.urlCache](https://sosumi.ai/documentation/foundation/urlsessionconfiguration/urlcache),
  [useProtocolCachePolicy](https://sosumi.ai/documentation/foundation/nsurlrequest/cachepolicy-swift.enum/useprotocolcachepolicy).
- Keep bootstrap cache profile-scoped; never show one profile's history or
  library after switching to another profile.
- On foreground, fetch the no-store catalog revision and refresh bootstrap only
  when it differs from the saved feed. Pull-to-refresh and a detected revision
  change must bypass any in-memory catalog memo. Refresh bootstrap
  after successful progress/library mutation and profile switch as well.
- Facet tag IDs are stable database IDs, but the display vocabulary/counts can
  change after a relevance rebuild. Replace the cached facet response rather
  than merging it indefinitely.
- Do not cache a 403 or 404 response as content data.
- Cancel stale detail/search tasks when the active profile changes.

## 11. Migration checklist for the iOS app

- [ ] Store all profile `user_key` values in Keychain.
- [ ] Add a shared native API client that injects the active key into every path.
- [ ] Decode unknown/missing optional fields without failing the entire payload.
- [ ] Request details with `include=meta` and seed the stream layer from the
      returned `meta`; do not issue the old serial `/meta` call.
- [ ] Use `ids` batches of at most 40 for details prefetch and retain successes
      when sibling IDs fail.
- [ ] Give `HTTPClient` a `URLCache` and use `.useProtocolCachePolicy` for JSON
      requests so server `Cache-Control` headers are reachable.
- [ ] Use paged `GET /api/v2/bootstrap` instead of assembling the home
      screen from many catalog calls: render 24 rows, then follow the cursor to
      the 40-row maximum.
- [ ] Persist `catalog_revision`, probe `/api/catalog-revision` on foreground,
      and restart pagination if two bootstrap pages have different revisions.
- [ ] Build filters from `/api/facets` and send tag IDs to `/api/browse`.
- [ ] Preserve server ordering for home rows and browse results.
- [ ] Support the three search response modes.
- [ ] Use `movie`/`tv` for `media_type` and `movie`/`series` for `content_type`/`addon_type`.
- [ ] Show `content_rating` when non-null.
- [ ] Show a coming-soon chip only when `comingSoonDate` is present.
- [ ] Use `similar_source` to choose an honest similar-rail label.
- [ ] Attribute TMDB reviews and do not invent attribution for MDBList excerpts.
- [ ] Do not add a reviews “See all” action until pagination exists.
- [ ] Redact `user_key` from logs, crash reports, and analytics.
- [ ] Clear profile-scoped state on profile switch or deleted-key 404.

## 12. Curl smoke tests

```bash
base_url="https://curatio.example.com"
profile_key="REDACTED"

curl -sS "$base_url/$profile_key/api/bootstrap"
curl -sS "$base_url/$profile_key/api/facets?media_type=movie&min_titles=25"
curl -sS "$base_url/$profile_key/api/browse?tags=12,44&media_type=movie&mode=all&skip=0&limit=20"
curl -sS --get "$base_url/$profile_key/api/search" --data-urlencode "q=cyberpunk"
curl -sS "$base_url/$profile_key/api/details?tmdb_id=550&media_type=movie&include=meta"
```

Do not paste real output containing the path key into tickets or chat without
redacting it.

## 13. Machine-readable reference

FastAPI also exposes the deployed schema at:

```text
GET /openapi.json
GET /docs
GET /redoc
```

The Markdown contract is the client handoff and behavioral reference; the
deployed OpenAPI schema is useful for confirming parameter validation against a
specific environment.

No database migration or retagging is required for an iOS client to adopt these
endpoints. Tag facets/browse quality depends on the existing tagging and
relevance data, while reviews and live detail extras require a working TMDB
configuration on the server.
