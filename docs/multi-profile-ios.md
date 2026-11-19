# Multi-Profile Accounts — iOS / iPadOS Integration Guide

This document is the hand-off for adding **Netflix-style profiles** to the native
Curatio app. It describes the data model, the API surface (with exact request /
response shapes), the recommended in-app UX, and the backward-compatibility
guarantees so the app can adopt this at its own pace.

> TL;DR: One **account** (email / password / 2FA / passkeys) now owns one or more
> **profiles**. Each profile has its own watch history, continue-watching,
> library, recommendations, and — importantly — **its own `user_key` /
> server URL**. The app authenticates per profile with that `user_key`.
> Switching profiles in-app means switching which `user_key` you talk to.

---

## 1. Concepts

- **Account** — the login identity. Holds email, password, TOTP and passkeys.
  Not addressable by native clients directly.
- **Profile** — a viewing persona under an account. Each profile has a unique,
  opaque **`user_key`**, sent as `Authorization: Bearer <user_key>` on every
  content call (`/api/v2/...`). All watch data is scoped to the profile that owns the
  `user_key`.
- **`user_key` is the native credential.** Possessing a profile's `user_key`
  grants access to that profile's content endpoints *and* (see §4) account-level
  profile management. This is the same household trust boundary as a TV app where
  any profile can manage the others. There is no per-profile PIN yet (a future
  enhancement could add one).

### Identity is the `user_key`
The native API identifies a profile solely by the Bearer `user_key` and has
**no concept of switching profiles** mid-session. Therefore each profile is
effectively a distinct API identity. In the native app, **switching profiles =
swapping the active `user_key`** you use for all content calls (and re-fetching
bootstrap for the new key).

All values below assume a configured base URL (the app already knows it as
`baseURL`); paths are relative to it.

---

## 2. What's unchanged (backward compatibility)

The current app keeps working with **no changes**:

- Every existing user was migrated to *an account + one default profile* and
  **kept its original `user_key`**. No keys changed, no data moved.
- All `/api/v2/...` content endpoints (native API, scrobble, progress,
  history, library, segments) are unchanged.
- The existing auth flows (mobile PKCE, app pairing, device pairing) return the
  credential shape `userKey` / `baseURL` / `apiBase`.

A single-profile account behaves exactly as before. Profiles are **opt-in**:
until the user creates a second profile, nothing about the app's behavior
changes. Adopt the profile UI whenever you're ready.

---

## 3. Existing auth flows (unchanged shapes, profile-aware)

`GET /auth/capabilities` advertises supported flows:

```json
{
  "mobileAuth": {
    "supported": true,
    "startURL": "{baseURL}/auth/mobile/start",
    "exchangeURL": "{baseURL}/auth/mobile/exchange",
    "pkceMethods": ["S256"],
    "redirectSchemes": ["curatio", "..."]
  },
  "deviceCode": { "supported": true, "codeURL": "{baseURL}/auth/device/code" }
}
```

### Mobile PKCE (recommended sign-in)
1. `GET /auth/mobile/start?redirect_uri=…&code_challenge=…&code_challenge_method=S256&state=…`
   in `ASWebAuthenticationSession`. If not signed in, the user logs in on the
   web; **the profile that is active in that web session** is the one bound to
   the returned code (default = the account's first profile).
2. `POST /auth/mobile/exchange` with `{ "code": "...", "code_verifier": "..." }`
   returns the **active profile's** credentials:

```json
{
  "userKey": "<profile user_key>",
  "baseURL": "https://curatio.example.com",
  "apiBase": "https://curatio.example.com"
}
```

### App pairing (QR / short code) and device pairing (tvOS)
Unchanged. `POST /auth/pair/create`, `POST /auth/pair/{token}/claim`,
`POST /auth/pair/code/{short_code}/claim`, and the device flow
(`POST /auth/device/code`, `GET /auth/device/{device_token}/status`,
`POST /auth/device/claim`) all resolve to the **active profile** and return
`{ "user_key": "...", "api_base": "..." }` (or the `userKey`/`apiBase`
shape for mobile). Whatever profile is active on the web session at claim time is
the one paired.

> **Net effect:** the first sign-in/pair gives you **one** profile's `user_key`.
> Use the API in §4 to discover and obtain the account's other profiles.

---

## 4. New: native profile API (`user_key`-authed)

These let the app list/create/edit/delete profiles **without** a web session,
using any of the account's profile `user_key`s as the credential. There is
intentionally **no `/select`** — switching is purely local (see §5).

### List profiles
`GET /api/v2/profiles`

```json
{
  "active_profile_id": 456,
  "profiles": [
    {
      "id": 456,
      "display_name": "Alex",
      "avatar": "color:#4a8fd4",
      "bandwidth_tier": "high",
      "user_key": "abc123…",
      "api_base": "https://curatio.example.com",
      "created_at": "2026-01-15T10:30:00"
    },
    { "id": 457, "display_name": "Kids", "avatar": "color:#4ab46a", "bandwidth_tier": "high",
      "user_key": "def456…", "api_base": "https://…", "created_at": "…" }
  ]
}
```

`active_profile_id` is the profile whose `user_key` you called with (i.e. "you
are here"). Each entry includes the sibling's `user_key` + `api_base` — that
is everything you need to switch.

### Create
`POST /api/v2/profiles` with body (all fields optional):

```json
{ "display_name": "Guest", "avatar": "color:#d4834a", "bandwidth_tier": "low" }
```
→ `{ "status": "ok", "profile": { …full profile summary incl. new user_key… } }`

- `bandwidth_tier` ∈ `"low" | "high"` (defaults to `"high"`). It is the profile's saved playback quality: `low` = optimized (server transcode), `high` = original file.
- `avatar` is an opaque string; the web UI uses `color:#RRGGBB`. The native app
  may use the same convention to stay visually consistent, or its own scheme.
- Returns **400** if the account is at the cap (**10 profiles**).

### Update
`PATCH /api/v2/profiles/{profile_id}` — same body; omitted fields unchanged.
→ `{ "status": "ok", "profile": { … } }`. **404** if the profile isn't in this
account.

### Delete
`DELETE /api/v2/profiles/{profile_id}`
→ `{ "status": "ok" }`. Cascade-deletes that profile's watch data. Returns
**400** if it's the account's only profile, **404** if not in this account.

> Auth/ownership: all four endpoints resolve the account that owns the
> Bearer `user_key` and operate strictly within it. An unknown key → **401**;
> a `profile_id` outside the account → **404**.

---

## 5. Recommended native UX

Because the API binds identity to `user_key`, model a profile switch as a local
credential swap:

1. **On sign-in** (mobile PKCE / pairing) you receive one profile's `user_key`.
   Store it in the Keychain as the active profile credential.
2. **Discover siblings** with `GET /api/v2/profiles`. Cache the returned list
   (id, display_name, avatar, `user_key`, `api_base`). These keys are
   long-lived; persist them in the Keychain too.
3. **Profile picker** ("Who's watching?"): show the cached profiles. Selecting
   one = set it as the active `user_key`, then re-fetch its
   bootstrap and reset any in-memory continue-watching/library
   caches (each profile's data is isolated).
4. **Add / edit / delete** profiles via the §4 endpoints using the currently
   active `user_key`; after a create, persist the new profile's `user_key`.
5. **Re-sync the list** opportunistically (e.g. on app foreground or when the
   picker opens) so profiles added on other devices appear.

Notes:
- Treat each `user_key` as a secret (Keychain, not `UserDefaults`).
- A profile deleted elsewhere will start returning **404** on its content
  endpoints — drop it from the local list and fall back to another profile.
- There is no server-side "active profile" for native clients; the app owns that
  choice locally. (The web session's active profile only affects which key the
  *pairing/mobile-auth* flows hand out.)

---

## 6. Per-profile content endpoints (all `user_key`-scoped, unchanged)

Use the **active profile's** `user_key` for every content call. Switching the
key switches all of the following data automatically:

| Method & path | Purpose | Response (verified) |
| --- | --- | --- |
| `GET /api/v2/...` | Native discovery API (bootstrap, search, browse, details; see `docs/native-api-ios.md`) | JSON |
| `POST /api/v2/playback/sources` | Playable sources (see `docs/playback-api.md`) | JSON |
| `POST /api/v2/scrobble` | Record a completed watch | `{ "status": "ok" }` (or `{ "status":"ok","deduplicated":true }`) |
| `POST /api/v2/progress` | Upsert continue-watching position | `{ "status": "ok" }` (or `{ "status":"ok","completed":true }` at ≥90%) |
| `GET /api/v2/history?limit=` | Continue-watching list | `{ "items": [ { tmdb_id, media_type, title, position, duration, updated_at, season?, episode? } ] }` |
| `GET /api/v2/library` | Saved library | `{ "items": [ {...} ] }` |
| `POST /api/v2/library` | Add to library (idempotent) | `{ "status":"ok", "added": true|false, "item": {...} }` |
| `DELETE /api/v2/library/{media_type}/{tmdb_id}` | Remove | `{ "status":"ok", "removed": true|false }` |
| `POST /api/v2/library/sync` | Bulk reconcile `{added:[…],removed:[…]}` | `{ "status":"ok", "applied": {added,removed}, "items":[…] }` |
| `GET /api/v2/segments/{video_id}` | Intro/credits markers | `{ status, segments:[…] }` |

Request bodies for `scrobble` / `progress` / `library`:

```jsonc
// scrobble
{ "tmdb_id": 550, "media_type": "movie", "action": "complete", "title": "Fight Club", "season": null, "episode": null }
// progress
{ "tmdb_id": 550, "media_type": "movie", "position": 3600, "duration": 7200, "title": "Fight Club", "season": null, "episode": null }
// library add
{ "tmdb_id": 550, "media_type": "movie", "title": "Fight Club", "poster_path": "/x.jpg", "year": "1999" }
```

---

## 7. Suggested rollout for the app

1. **Phase 1 (no UI yet):** after sign-in, call `GET /api/v2/profiles`. If
   the account has >1 profile, show a picker; otherwise behave exactly as today.
2. **Phase 2:** add a profile picker ("Who's watching?") on launch + a switcher
   in settings/account; persist all profile `user_key`s.
3. **Phase 3:** add create / edit / delete via the §4 endpoints.

Every phase is independently shippable and backward-compatible.

---

## 8. Error reference

| Status | Meaning |
| --- | --- |
| `400` | Profile cap reached (create) or attempt to delete the only profile |
| `401` | Missing, unknown or rotated Bearer `user_key` (or expired web session on cookie endpoints) |
| `404` | `profile_id` not in this account |

---

## 9. Native rich-discovery API

The native app can use the same normalized home, tag browsing, search, and
detail payloads as the web player without a browser cookie. Authenticate by
sending the active profile's key as `Authorization: Bearer <user_key>`:

| Method & path | Purpose |
| --- | --- |
| `GET /api/v2/bootstrap?cursor=0&limit=24` | Account/profile context, activity collections, and a discovery-row page; follow `row_page.next_cursor` for at most 40 rows |
| `GET /api/v2/catalog-revision` | Private, no-store revision probe used to refresh a saved home after catalog publication or rotation |
| `GET /api/v2/facets?media_type=movie&min_titles=25` | Displayable tags grouped by taxonomy facet with title counts |
| `GET /api/v2/browse?tags=12,44&media_type=movie&mode=all&skip=0&limit=60` | Relevance-ranked arbitrary tag combinations; `mode` is `all` or `any` |
| `GET /api/v2/search?q=cyberpunk&tags=12,44` | Title, vibe, or optional tag-filtered search |
| `GET /api/v2/details?tmdb_id=550&media_type=movie` | Rich details, or pass `imdb_id=tt...` when no TMDB id is available |

All five calls validate that `user_key` belongs to an active profile. Unknown
keys return `404`; they do not fall back to the browser session. Browsing and
ranking use the local database and precomputed relevance weights and therefore
make no AI calls.

### Content ratings

Every media item returned by bootstrap, browse, and database-backed search has:

```json
{ "content_rating": "PG-13" }
```

The value can also be `R`, `G`, `TV-MA`, `TV-14`, etc., or `null` when neither
TMDB nor MDBList supplied a certification. It is read from the title's stored
enrichment, so a grid does not need to make one detail call per card.

Every catalog and native card uses the existing coming-soon contract too:
`comingSoonDate: "yyyy-MM-dd"` is included only for today-or-later dates and is
omitted when the title is already available or the date is unknown. Movies use
their digital release date; series use the next episode air date. The client
continues to own locale-aware display text such as “Coming Friday.”

The details response repeats the value on `item.content_rating` and at the top
level, and provides its available guidance:

```json
{
  "content_rating": "PG-13",
  "content_advisory": {
    "certification": "PG-13",
    "age_rating": 13,
    "common_sense": "Some intense sequences",
    "source": "database"
  }
}
```

### Reviews and extended details

`details` also returns canonical tags, multi-source ratings, tag-vector similar
titles, trailers, keywords, studios/networks, production countries, tagline,
status, and review information:

```json
{
  "reviews": [
    {
      "id": "review-id",
      "source": "tmdb",
      "source_name": "TMDB",
      "kind": "user_review",
      "author": "Alex",
      "username": "alex",
      "avatar": "https://image.tmdb.org/t/p/w185/avatar.jpg",
      "rating": 8.5,
      "content": "Review text…",
      "created_at": "2026-01-01T00:00:00Z",
      "url": "https://www.themoviedb.org/review/..."
    }
  ],
  "review_summary": {
    "returned": 1,
    "total_available": 42,
    "average_user_rating": 8.5,
    "rated_review_count": 1,
    "sources": ["tmdb"]
  }
}
```

TMDB reviews retain author attribution and their canonical URL. Existing short
MDBList review text is returned as `kind: "review_excerpt"` with no invented
author. The server caps the number and length of reviews so a detail request
cannot unexpectedly become a multi-megabyte response.

Questions or shape mismatches: the source of truth is `app/main.py`
(`/api/v2/profiles` handlers + the `/api/v2/...` content routes) and
`app/auth.py` (`_client_credentials_for_user`).
