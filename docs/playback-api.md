# Curatio client API v2: playback sources and availability

This is the contract between the Curatio server and its clients (the web app, the Curatio desktop app, Roku, tvOS/iOS).
It replaces the v1 discovery and stream routes listed under "Removed routes" below. Those routes
are **removed**; v2 clients must not call them.

Curatio plays titles from the user's own media servers (Plex, Jellyfin) and from optional
source addons the server owner installs. Titles that no source can play are
recommendations only: clients show **Watchlist / Saved** instead of **Play**.

## Server identity, pairing and authentication

- A paired client stores the bare server base URL (`api_base`) and the profile's
  `user_key`. Keep the key in the Keychain / Registry, never in plain settings.
- Every profile endpoint lives under **`/api/v2/`** and takes the key as
  **`Authorization: Bearer <user_key>`**. The key never appears in a URL, so it
  stays out of proxy, CDN and server logs.
- A missing, malformed, unknown or rotated key answers **401** with
  `WWW-Authenticate: Bearer`. More than 30 failed attempts a minute from one IP
  answer **429**.
- `/api/v2` responses carry `Vary: Authorization` and are never `public`. A client
  cache keyed only by URL must be cleared when switching profiles.
- Pairing responses (QR/device-code claim, profile summaries) return `user_key` and
  `api_base`; mobile PKCE exchange returns `userKey`, `baseURL` and `apiBase`. The old
  `server_url`/`serverURL` (`{base}/{user_key}`) and `manifest_url` fields are gone.
- `POST /api/v2/profiles/{id}/rotate-key` issues a new key for a profile in the
  caller's account and returns `{ "user_key": ... }`; the old key stops working at
  once. The web account page offers the same as **Sign Out Apps on This Profile**.
- Clients that stored `{base}/{user_key}` migrate once: keep the base, move the key
  to secure storage.

## `GET /api/version` (no auth)

```json
{
  "api_version": 2,
  "server_version": "1.0.0",
  "min_clients": { "roku": "2.0.0", "tvos": "2.0.0", "web": "2.0.0" }
}
```

Clients call this at launch. If it returns 404, or `api_version` differs from the version
the client was built for, the client shows "This app and your Curatio server need to be
updated to matching versions" and stops there. It does not keep making other requests.

## Availability

Every media item object (bootstrap items, home rows, search results, browse, details,
details-batch, watchlist entries) gains:

```json
"availability": {
  "playable": true,
  "in_library": true,
  "sources": [ { "provider_id": "plex", "label": "Plex", "kind": "library" } ]
}
```

- `in_library` is true when a Plex or Jellyfin server has the title. A show counts as
  soon as any of its episodes is present.
- `playable` is `in_library` OR an installed addon can search for it. When `playable` is
  false, clients **hide Play / Resume / Choose Source / card play shortcuts** and make
  Watchlist/Saved the primary action. They also show "Not in your Plex or Jellyfin" (web,
  tvOS) or the equivalent short text (Roku).
- A missing `availability` key means the item came from an older cached payload. Treat it as
  `playable: false`.

Bootstrap (`/api/v2/bootstrap`), `/account/api/bootstrap` and `/auth/capabilities`
also gain:

```json
"playback_providers": [
  { "id": "plex", "label": "Plex", "kind": "library", "status": "ok" },
  { "id": "jellyfin", "label": "Jellyfin", "kind": "library", "status": "ok" }
]
```

`status` is one of `ok`, `degraded` or `offline`.

## `POST /api/v2/playback/sources`

The web client calls `POST /account/api/playback/sources` (cookie session, same body).

Request:

```json
{
  "type": "movie",
  "imdb_id": "tt0111161",
  "tmdb_id": null,
  "season": null,
  "episode": null,
  "quality": "original",
  "refresh": false,
  "viewing_session_id": "uuid"
}
```

- `type` is `movie` or `series`. For `series`, `season` and `episode` are required.
- You must send at least one of `imdb_id` or `tmdb_id`.
- `quality` is `original` (direct file), `optimized` (server transcode HLS), or null.
  Null means the profile's saved quality preference.
- Set `refresh: true` to bypass the server cache. Clients do this after every source has
  failed once.

Response `200`:

```json
{
  "quality": "original",
  "availability": { "playable": true, "in_library": true, "sources": [] },
  "sources": [ PlaybackSource, ... ]
}
```

- `sources` is already ordered best-first: library sources, then addon sources, then by
  quality. Clients keep this order and only **filter out** sources they cannot decode.
- If nothing is found, the response is `200` with `sources: []`. Clients show "Not
  available to play" and offer Watchlist.
- `400 {"detail": {"code": "invalid_media", "message": "…"}}` means the request was malformed.
- `502 {"detail": {"code": "providers_unavailable", "message": "…"}}` means every enabled
  provider errored. This is a retryable error.

### PlaybackSource

```json
{
  "id": "src_9f2c…",
  "provider_id": "plex",
  "provider_label": "Plex",
  "kind": "library",
  "variant": "original",
  "delivery": "file",
  "url": "https://curatio.example/media/eyJ…",
  "headers": {},
  "label": "4K · HEVC · Dolby Vision · 5.1",
  "detail": "The.Movie.2019.2160p.mkv",
  "badges": ["Original"],
  "container": "mkv",
  "video_codec": "hevc",
  "audio_codec": "eac3",
  "hdr": "dv",
  "resolution": "2160p",
  "width": 3840,
  "height": 2160,
  "audio_channels": 6,
  "languages": ["en"],
  "size_bytes": 21474836480,
  "bitrate": 24000000,
  "duration_seconds": 8520,
  "subtitles": [
    { "id": "s1", "url": "https://curatio.example/media/eyJ…", "lang": "en", "label": "English", "format": "srt", "forced": false }
  ],
  "route_id": "hmac…",
  "file_id": "hmac…",
  "startup_timeout_seconds": 15
}
```

| field | notes |
|---|---|
| `id` | Opaque string. Unique within one response. Send it back in failure reports. |
| `kind` | `library` (Plex/Jellyfin) or `addon`. |
| `variant` | `original` or `optimized`. |
| `delivery` | `file` (progressive/range requests) or `hls` (`.m3u8`). |
| `url` | Absolute URL. Play it as-is with `headers`. Library URLs are short-lived signed `/media/…` URLs (about 6 h), so re-request sources rather than caching them for long. |
| `headers` | Request headers to send (often `{}`). |
| `label` / `detail` | Display strings for the source picker. **Clients do not parse them.** |
| `badges` | Short display-only pills. Render them verbatim. |
| codec fields | Lower-case. `video_codec`: `h264`/`hevc`/`av1`/`vp9`/`mpeg2`/… `audio_codec`: `aac`/`ac3`/`eac3`/`truehd`/`dts`/`dtshd`/`flac`/`opus`/… `hdr`: `dv`/`hdr10`/`hdr10plus`/`hlg`/null. Any of them may be null when unknown. |
| `resolution` | `2160p`/`1080p`/`720p`/`480p`/null. |
| `audio_channels` | Integer channel count (2, 6, 8) or null. |
| `route_id`, `file_id` | Opaque failover identities. Two sources with the same `file_id` are the same file, so skip the duplicate after a decode failure. `route_id` identifies the delivery path. |
| `startup_timeout_seconds` | How long to wait for first frame before failing over. Null means the client default. |

## `POST /api/v2/playback/failure`

The web client calls `POST /account/api/playback/failure`.

```json
{
  "source_id": "src_9f2c…",
  "provider_id": "plex",
  "route_id": "hmac…",
  "file_id": "hmac…",
  "failure_category": "network",
  "reason": "HTTP 503 after 3 retries"
}
```

- `failure_category` is one of `network`, `decode`, `timeout`, `auth`, `not_found`, `other`.
- Response: `{"status": "ok"}`.

## Unchanged routes

The watchlist routes (`/api/v2/library…`, `/account/api/library…`), presence, segments,
bootstrap, home, search, browse, details, facets, catalog-revision, the web proxy, remux
and probe routes, and auth all keep their paths. They only gain the fields above.

## Removed routes (return 404)

The v1 setup document routes, `/api/v2/catalog/…`, `/api/v2/meta/…`,
`/api/v2/stream/…`, `/api/v2/streams/failure`, `/account/api/playback/streams`.
