<p align="center">
  <img src="../static/logo.png" alt="Curatio" width="240">
</p>

<h1 align="center">Curatio</h1>

<p align="center"><strong>Your library, curated: personal recommendations and a great player for the movies and shows you already have on Plex and Jellyfin.</strong></p>

<p align="center">
  <a href="https://github.com/curatio-app/curatio/actions/workflows/build.yml"><img src="https://github.com/curatio-app/curatio/actions/workflows/build.yml/badge.svg" alt="Build and Test"></a>
  <a href="../LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
</p>

<p align="center">
  <a href="#what-it-is">What it is</a> &middot;
  <a href="#features">Features</a> &middot;
  <a href="#architecture">Architecture</a> &middot;
  <a href="#requirements">Requirements</a> &middot;
  <a href="#setup">Setup</a> &middot;
  <a href="#configuration">Configuration</a> &middot;
  <a href="#first-run-behavior">First run</a>
</p>

---

## What it is

> **Installing Curatio?** Use the step-by-step guide at
> [github.com/curatio-app/curatio](https://github.com/curatio-app/curatio#readme). This document
> is the reference for the server itself: features, architecture, configuration and development.

Curatio helps people decide what to watch from the library they already own. It is a
self-hosted [FastAPI](https://fastapi.tiangolo.com/) server that sits next to Plex or Jellyfin,
learns each profile's taste from what it watched (in Curatio and on the media server), builds
Home rows picked from the owned library, and serves rich [TMDB](https://www.themoviedb.org/)
metadata to the Curatio apps through its native API (`/api/v2/...`, `/account/api/...`). It
manages invite-gated local accounts and Netflix-style viewing profiles, and stores per-profile
watch history, progress, and library data in PostgreSQL.

Curatio **hosts and stores no media**. It plays titles from media servers you already run,
[Plex](https://www.plex.tv/) and [Jellyfin](https://jellyfin.org/): it mirrors which titles each
server has, marks them as playable, and streams them from that server through short-lived signed
links. Everything else Curatio recommends is shown as a recommendation you can add to your
Watchlist. Optional, separately installed playback addons can add other sources the server owner
operates (see [Addons](#addons)). An AI provider is optional: it is used only to tag titles on the
connected servers (and on Watchlists) in the background, never during normal browsing. The project
is 100% self-hosted — the developer operates nothing.

## Features

Every item below is implemented in this repository (file references in parentheses).

### Discovery for your library

- **Watch history from Plex and Jellyfin** — library sync reads the signed-in account's watch
  state. Profiles that use it (automatic for the server owner's first profile; per profile under
  `server_history`) get `server_watched` events that shape taste and seed Because You Watched,
  without appearing in Continue Watching (`app/server_history.py`).
- **Rows picked from what you own** — Top Picks on Your Servers, Because You Watched (owned),
  Unwatched, Short Tonight, taste rows, Hidden Gems You Own, Haven't Watched in Years, ranked with
  the same taste and similarity scores but limited to owned titles. "My Servers" Home uses all of
  them; "Everything" adds Top Picks and Unwatched on Your Servers (`app/owned_rows.py`).
- **Reasons on every pick** — cards carry `reason` ("Because you watched Heat", "Slow-Burn ·
  Heist") through the Home snapshot.
- **Not for me** — hidden titles leave every row and the hero and count against similar tags
  (`/api/v2/hidden`, `/account/api/hidden`).
- **Library-only tagging** — only owned and Watchlist titles are tagged, after each sync. No AI
  key: rule-based tags, upgraded once a key is added. Optional monthly cap; catalog-wide tagging
  is opt-in (`workers/library_tagging.py`).
- **Tag pack** — import Curatio tags for common titles from a file or `TAG_PACK_URL` so an install
  pays to tag only what the pack doesn't cover; admins can export their own (`app/tag_pack.py`).
- **Local AI models** — any OpenAI-compatible endpoint (Ollama, LM Studio) as the tagging
  provider (`app/external_tagging_client.py`).

### Everything else

- **Synthesized discovery catalogs** — rows discovered from high-quality, current-version tag
  co-occurrence and materialized with no AI calls. The former 40 static rows remain inactive
  compatibility records only (`app/category_synth.py`, `app/catalog_generator.py`).
- **Personalized catalogs** — per-profile rows (Trending, New Releases, Because You Watched,
  Recommended, Top Picks, Hidden Gems) built from local watch state and tag similarity
  (`workers/personalized_sync.py`).
- **Background AI tagging** — Gemini, Claude, OpenAI or a local model tags titles across
  genre/mood/era/etc. in background jobs only; browsing never calls an AI API and instead reads
  PostgreSQL, TMDB, and precomputed rows (`app/tagging_provider.py`, `workers/library_tagging.py`).
- **TMDB metadata** — posters, IDs, details, and similar titles (`app/tmdb_client.py`;
  images from `image.tmdb.org`).
- **Search** — title, year and tag-aware search over Curatio's own catalog, ranked
  server-side (`app/main.py` `_discovery_search`).
- **Plex and Jellyfin playback** — sign in with Plex (plex.tv PIN flow) or connect Jellyfin
  (username/password or API key) under Admin → Integrations. A sync job mirrors each server's
  movies and shows by IMDb/TMDB/TVDB id (`app/sources/plex`, `app/sources/jellyfin`). Every title
  the apps receive carries `availability` (playable / in library), and playback offers the
  original file or the server's own optimized (transcoded HLS) version. Clients only ever see
  signed, expiring `/media/...` links; media-server tokens and addresses stay on the server
  (`app/media_proxy.py`). An optional same-origin byte pass-through exists for in-browser
  playback, and browser-safe H.264 can be copied into bounded temporary HLS with AAC audio;
  temporary files are deleted on stop or idle expiry (`app/remux.py`).
- **Recommendations for everything else** — titles not on your servers appear with a
  Watchlist action instead of Play.
- **Playback addons (optional)** — a small, versioned SDK (`app/sources/sdk.py`) lets a
  separately installed Python package register another source provider
  (`docs/playback-api.md`).
- **Private per-profile API keys** — clients reach the native API with a profile's private
  `user_key` sent as `Authorization: Bearer` to `/api/v2/...`, or with an account session
  (`/account/api/...`). Keys can be rotated from the account page.
- **Invite-gated accounts** — admins mint one-time invite codes; registration requires one
  (`app/auth.py`, `app/main.py`).
- **Accounts, profiles, 2FA, passkeys, pairing** — email/password login, TOTP 2FA (`pyotp`),
  WebAuthn passkeys (`app/passkeys.py`), and app / device / mobile-PKCE pairing (`app/auth.py`);
  Netflix-style multi-profile accounts where each profile has its own `user_key`
  (`docs/multi-profile-ios.md`; `app/main.py` `/api/v2/profiles`).
- **Watch state & library** — scrobbles, cross-device progress, history, saved library, and
  history import, keyed by TMDB **or** IMDb id (`app/main.py`; `docs/imdb-keyed-sync.md`).
- **Intro/credits segment markers (optional)** — audio-fingerprint jobs learn episode timing
  using ffmpeg + Chromaprint and expose segment markers plus user feedback endpoints
  (`workers/fingerprint_worker.py`).
- **Admin dashboard** — builds (start/stop/pause/resume), settings, invites, users, sessions,
  active streams, integrations (Plex, Jellyfin, addons), fingerprinting, diagnostics, and catalog regeneration
  (`app/admin.py`).
- **Daily update scheduler (optional)** — in-app scheduler that fetches new releases and tags
  owned and Watchlist titles; new releases nobody owns are tagged only with
  `TAG_CATALOG_BEYOND_LIBRARY=true` (`app/scheduler.py`; `daily_update_enabled=false` by default). Personal rows still refresh
  daily through a separate local, zero-AI scheduler when personalized sync is enabled.
- **Per-profile rate limiting** — `slowapi`, `rate_limit_per_minute` (default 60).

## Architecture

Curatio is a single FastAPI application backed by PostgreSQL. Media lives on your own Plex or
Jellyfin server.

```text
Curatio apps (web, Apple, Roku, desktop)
            │
            ▼
   Curatio (FastAPI app)  ──►  Plex / Jellyfin (your media servers, via signed /media/ links)
            │
            ├── PostgreSQL — tags, catalogs, accounts, profiles, sessions,
            │                watch state, library, admin settings, fingerprints
            ├── TMDB        — metadata, posters, IDs, similar titles
            └── AI provider — offline semantic tagging only (never during browsing)
```

- `app/main.py` — FastAPI app, native client API, auth/account/profile APIs, watch-state APIs.
- `app/admin.py` — admin dashboard + admin API.
- `app/config.py` — Pydantic settings loaded from environment / `.env`.
- `app/database.py` — PostgreSQL engine, schema creation, lightweight migrations, category seed.
- `app/sources/` — playback provider SDK, registry, resolver, availability index, and the
  built-in Plex and Jellyfin integrations. `app/media_proxy.py` serves signed media links.
- `app/gemini_client.py`, `app/external_tagging_client.py`, `app/tmdb_client.py` — upstream
  clients.
- `workers/` — `initial_build.py`, `daily_update.py`, `personalized_sync.py`,
  `fingerprint_worker.py`.

## Requirements

- Docker & Docker Compose (or Python 3.11 for local runs).
- A PostgreSQL database (provided automatically by the bundled Compose stack).
- A [TMDB API key](https://www.themoviedb.org/settings/api) (free).
- A Plex Media Server and/or Jellyfin server — connected in Admin → Integrations after
  startup, not via `.env`. Without one, Curatio is a recommendations catalog with nothing to play.
- **Optional:** an AI tagging key ([Google Gemini](https://aistudio.google.com/app/apikey),
  [Anthropic](https://console.anthropic.com/settings/keys),
  [OpenAI](https://platform.openai.com/api-keys)) or a local OpenAI-compatible model. Without one,
  titles get rule-based tags. Can be entered after startup in Admin Center.
- **Optional:** an [MDBList](https://mdblist.com/preferences/) key for ratings (Admin → Tagging).
- HTTPS in production: account sign-up uses passkeys, which browsers allow only over HTTPS.

### Required configuration (no defaults)

These settings have **no default value** in `app/config.py` and must be supplied. The app
instantiates `Settings()` at import time (`app/config.py:190`), so **it will not start** if any
are missing — startup fails with a Pydantic validation error.

| Variable | Where | Notes |
|----------|-------|-------|
| `TMDB_API_KEY` | `app/config.py:16` | TMDB metadata access. |
| `MASTER_PASSWORD` | `app/config.py` | Admin fallback login (break-glass). At least 16 characters. |
| `DATABASE_URL` | `app/config.py:27` | PostgreSQL DSN. Set automatically by Docker Compose (`docker-compose.yml:31`). |
| `BASE_URL` | `app/config.py:37` | Public origin, no trailing slash. |
| `SECRET_KEY` | `app/config.py:69` | Session/JWT signing secret; validated to be **≥ 32 characters** (`app/config.py:149`). |

When using Docker Compose, `POSTGRES_PASSWORD` is also mandatory: the Compose file uses the
`${POSTGRES_PASSWORD:?...}` sentinel, so `docker compose up` fails fast if it is unset
(`docker-compose.yml:9`).

## Setup

Self-hosters: follow the [install guide](https://github.com/curatio-app/curatio#readme), which
uses the prebuilt image and sets up HTTPS. The options below build from this repository.

### Option A — Docker Compose from source

```bash
git clone https://github.com/curatio-app/curatio.git
cd curatio
cp .env.example .env
# Edit .env and set at least:
#   TMDB_API_KEY, MASTER_PASSWORD, BASE_URL, SECRET_KEY, POSTGRES_PASSWORD
# Optionally choose Gemini, Claude, OpenAI or a local model on Admin → Tagging.
docker compose up -d
curl http://localhost:8000/health
```

A healthy instance returns JSON with `status`, `database`, and `version`
(`app/main.py:660`). The Compose stack starts PostgreSQL (kept on the internal Docker network)
and the app on port `8000`; `DATABASE_URL` is injected automatically
(`docker-compose.yml:31`). PostgreSQL is intentionally not published to the host in production —
use `docker-compose.dev.yml` for `127.0.0.1:5432` access during development.

### Option B — Prebuilt image

CI builds and pushes container images to the GitHub Container Registry on pushes to `main`
(`.github/workflows/build.yml`):

```
ghcr.io/curatio-app/curatio:latest      # PostgreSQL in its own container
ghcr.io/curatio-app/curatio:aio         # PostgreSQL built in (Unraid, single container)
```

The published image contains only application code (`app/`, `workers/`, `static/`, and the
built dashboard from the Node build stage — `Dockerfile`); it bakes in **no** secrets, API keys,
or stream sources. All configuration is supplied at runtime via environment variables.

### Tagging

Nothing to run by hand: after each Plex/Jellyfin sync, titles on the servers (and on Watchlists)
are tagged in the background. Progress, the monthly spend and the tag pack live in
**Admin → Tagging → Library & tag pack**. The same page retries titles whose AI call failed and
that were left with rule-based tags only.

A full catalog build (`workers/initial_build.py`, Admin → Jobs & build) is only needed to tag
titles nobody owns, for example to export a large tag pack. Claude/Claude Code and ChatGPT
subscriptions are separate from Anthropic and OpenAI API billing.

### Pair a device

An admin creates a one-time invite code in the dashboard; a user checks it with
`GET /auth/verify-invite?invite=CODE` and registers a local account. Devices then pair from
`/account/pair` (QR / short code) or sign in directly; each profile is reached at its private
server URL, `https://your-base-url/{user_key}`.

### Production notes

- Do not publish PostgreSQL to the public internet. The production `docker-compose.yml` keeps it
  on the internal Docker network; the `127.0.0.1:5432` mapping lives only in
  `docker-compose.dev.yml`.
- Serve Curatio behind HTTPS and set `BASE_URL` to the public `https://` origin. Auth cookies are
  marked `Secure` by default (`FORCE_SECURE_COOKIES=true`, `app/config.py:71`).
- Keep `MASTER_PASSWORD`, `SECRET_KEY` and database credentials private. Media-server tokens
  are stored encrypted with `SECRET_KEY`-derived keys; back up the key with the database.
- Treat `/{user_key}` server URLs as secrets — anyone holding one can use that profile's API
  until you rotate or revoke access.

## Configuration

All application settings are read from `.env` via Pydantic (`app/config.py`). Some runtime
settings can also be overridden from the admin dashboard and persisted in PostgreSQL. See
[`.env.example`](../.env.example) for a copy-ready template. Values below are the code defaults.

### Catalogs, personalization & filters

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_NAME` | `Curatio` | Product name shown to users (2FA issuer, passkey relying party). |
| `CATALOG_SIZE` | `200` | Items stored per catalog (validated 10–500). |
| `CATALOG_PAGE_SIZE` | `100` | Items returned per catalog page. |
| `CATALOG_SHUFFLE_HOURS` | `3` | Reshuffle catalog order every N hours (`0` disables). |
| `UNIVERSAL_CATALOG_REFRESH_HOURS` | `24` | Hours between universal catalog refreshes. |
| `PERSONALIZED_CATALOG_REFRESH_HOURS` | `24` | Hours between personalized refreshes. |
| `UNIVERSAL_CATALOG_COUNT` | `40` | Legacy curated-row count (compatibility documentation only). |
| `PERSONALIZED_CATALOG_COUNT` | `19` | Maximum stable per-profile catalog slots. |
| `SYNTH_ROTATION_DAYS` | `7` | Days between rotations through the synthesized row pool (`0` pins it). |
| `ENABLE_UNIVERSAL_CATALOGS` | `true` | Enable universal catalogs. |
| `ENABLE_PERSONALIZED_CATALOGS` | `true` | Enable personalized catalogs. |
| `ENABLE_PERSONALIZED_SYNC` | `true` | Enable the local daily personalized catalog sync; it makes no AI API calls. |
| `HIDE_FOREIGN` | `false` | Global default filter for non-English content. |
| `HIDE_ADULT` | `false` | Global default filter for explicit/adult content. |
| `HIDE_UNRELEASED` | `false` | Global default filter for not-yet-released movies. |
| `CATALOG_MIN_QUALITY` | `6.8` | Shared minimum quality for all recommendation rows. |
| `CATALOG_MIN_RATING_SOURCES` | `5` | A title qualifies on source coverage or the TMDB vote fallback. |
| `CATALOG_MIN_VOTE_COUNT` | `1000` | TMDB vote fallback used by the shared evidence gate. |
| `CATALOG_NICHE_MIN_QUALITY` | `7.4` | Higher quality floor for the older niche-title exception. |
| `CATALOG_NICHE_MIN_VOTE_COUNT` | `250` | Minimum votes for that niche exception. |
| `CATALOG_NICHE_MIN_AGE_DAYS` | `180` | Prevents recent thinly-rated releases from using the niche exception. |

### Tagging & scheduled updates

| Variable | Default | Description |
|----------|---------|-------------|
| `TAGGING_PROVIDER` | `gemini` | Tagging provider: `gemini`, `anthropic`, `openai`, or `local`. Admin Center can store and switch this without a restart. Provider/model changes create a new tagger version. Without a key, titles get rule-based tags only. |
| `LOCAL_BASE_URL` | empty | OpenAI-compatible base URL for `local`, e.g. `http://host.docker.internal:11434/v1` (Ollama). |
| `LOCAL_MODEL` | empty | Model name on the local server, e.g. `qwen2.5:14b`. |
| `LOCAL_API_KEY` | empty | Key for the local endpoint, if it needs one. |
| `TAG_CATALOG_BEYOND_LIBRARY` | `false` | Also AI-tag new TMDB releases nobody owns (nightly). Off: only owned and Watchlist titles are tagged. |
| `LIBRARY_TAGGING_MONTHLY_CAP_USD` | `0` | Stop AI-tagging owned titles for the month at this spend (`0` = no cap). |
| `TAG_PACK_URL` | Curatio's pack | Manifest URL (`{"version","url","sha256"}`) of a tag pack to download and keep current. Defaults to `https://github.com/curatio-app/curatio/releases/download/tag-pack/manifest.json`; set it empty to turn downloads off. |
| `GEMINI_API_KEY` | empty | Google AI Studio API key. Only required when Gemini is selected. |
| `GEMINI_MODEL` | `gemini-2.5-flash-lite` | Gemini model used for tagging. Google retires model IDs on a schedule and a retired ID returns `404` on every call, which silently stops all tagging — run `python workers/diagnose_tagging.py` to list what your key can reach and confirm the configured model still answers. **This setting is also admin-writable, and a value stored in `admin_settings` is replayed over `.env` on every restart** — the Tagging tab flags the mismatch and can clear it. |
| `ANTHROPIC_API_KEY` | empty | Anthropic API key. A Claude or Claude Code subscription does not include this usage. |
| `ANTHROPIC_MODEL` | `claude-haiku-4-5-20251001` | Claude model used when Anthropic is selected. |
| `OPENAI_API_KEY` | empty | OpenAI Platform API key. A ChatGPT subscription does not include this usage. |
| `OPENAI_MODEL` | `gpt-5.4-nano` | OpenAI model used when OpenAI is selected. The Admin picker also offers GPT-4.1 nano, GPT-4o mini, and GPT-5 nano with their per-million-token prices. |
| `TAG_MIN_CONFIDENCE` | `0.60` | Reject speculative model assignments below the production-audited confidence floor. Mood and pacing judgments have a stricter built-in `0.65` floor; derived facts are exempt. |
| `GEMINI_INPUT_COST_PER_MTOK` | `0.10` | Input price per million tokens for the default model. Known model IDs use the built-in price table; unknown IDs use a conservative maximum. |
| `GEMINI_OUTPUT_COST_PER_MTOK` | `0.40` | Output price per million tokens. Thinking tokens, if any, are billed as output; tagging explicitly disables thinking. |
| `RETAG_COST_CEILING_USD` | `20.0` | Hard per-run ceiling against measured usage plus conservative reservations for failed or timed-out attempts. Editable on the Tagging tab. |
| `GEMINI_REQUEST_TIMEOUT` | `120` | Per-request timeout (s) for any selected AI tagging provider. |
| `DAILY_UPDATE_ENABLED` | `false` | Enable the in-app daily update scheduler. |
| `DAILY_UPDATE_TIME` | `03:00` | Daily update time (UTC, `HH:MM`). |
| `DAILY_UPDATE_TIMEOUT` | `1800` | Deadline (s) before a single daily run is killed. |

### Runtime, security & performance

| Variable | Default | Description |
|----------|---------|-------------|
| `MOBILE_REDIRECT_SCHEMES` | `curatio` | Comma-separated native app URL schemes accepted by mobile-auth callbacks. |
| `TOKEN_EXPIRATION_DAYS` | `30` | Session/JWT expiration window. |
| `RATE_LIMIT_PER_MINUTE` | `60` | Per-user request rate limit. |
| `FORCE_SECURE_COOKIES` | `true` | Always mark auth cookies `Secure`. |
| `SECURITY_CSP` | *(built-in policy)* | Content-Security-Policy for HTML routes. |
| `SECURITY_CSP_REPORT_ONLY` | `false` | Serve the CSP in report-only mode. |
| `SECURITY_ENCRYPTION_KEYS` | *(empty)* | Comma-separated base secrets for at-rest encryption; first is active, the rest are accepted during rotation. |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. |
| `DEBUG` | `false` | Detailed debug behavior. |
| `WORKERS` | `4` | Tuning value; the bundled Dockerfile intentionally starts a single Uvicorn process (in-memory stream caches and the playback-proxy admission counter are per-process, so multiple workers would fragment them). |
| `CACHE_TTL` | `3600` | General cache TTL (s). |
| `DETAIL_SNAPSHOT_WARM_ENABLED` | `true` | Keep complete native metadata snapshots warm independently of the paid daily update scheduler. |
| `DETAIL_SNAPSHOT_WARM_INTERVAL_SECONDS` | `3600` | Interval between resumable active-inventory warm passes. |
| `DETAIL_SNAPSHOT_WARM_STARTUP_DELAY_SECONDS` | `10` | Delay before the first warm pass after app startup. |
| `DETAIL_SNAPSHOT_WARM_CONCURRENCY` | `2` | Concurrent background snapshot builds, capped at four and separately limited from foreground work. |
| `DETAIL_SNAPSHOT_WARM_LIMIT` | `2000` | Prioritized active shelf/library/history titles per pass; `0` means unlimited. |
| `DETAIL_DYNAMIC_OVERLAY_TTL_SECONDS` | `86400` | Freshness window for persisted tags, ratings, and similarity overlays. Fresh overlays survive app restarts without recomputation. |
| `DETAIL_DYNAMIC_REFRESH_CONCURRENCY` | `1` | Maximum concurrent background overlay refreshes; one prevents similarity self-joins from saturating PostgreSQL. |
| `PLAYBACK_PROXY_MAX_STREAMS` | `24` | Max concurrent playback byte-proxy streams before new ones are shed with a 503. Most playback reads bytes directly from the upstream host and never counts against this; raise it for many simultaneous Safari/proxied viewers. |
| `PLAYBACK_PROXY_CHUNK_BYTES` | `262144` | Chunk size (bytes) for relaying proxied media. |
| `PLAYBACK_REMUX_MAX_STREAMS` | `3` | Max concurrent H.264-to-HLS compatibility sessions used only after native and in-browser playback paths fail. |
| `PLAYBACK_REMUX_IDLE_TIMEOUT` | `90` | Seconds without an HLS asset request before a compatibility session is stopped and its temporary files are removed. |
| `ALLOW_SERVER_TRANSCODE` | `false` | Let the server fully transcode video (H.264 ≤1080p, `-preset superfast -threads 2`, HDR tone-mapped to SDR) for browsers that can't decode a source. Off: transcode-only sources fail over and the web player suggests the desktop app. Toggleable live in the admin panel. |
| `PLAYBACK_TRANSCODE_MAX_STREAMS` | `1` | Max concurrent full transcodes (they also count toward `PLAYBACK_REMUX_MAX_STREAMS`). |
| `DB_POOL_SIZE` | `8` | SQLAlchemy pool size. Eight connections cover the single bundled Uvicorn process without retaining a large idle backend set. |
| `DB_MAX_OVERFLOW` | `4` | Extra connections beyond the pool for short request bursts. |
| `DB_POOL_TIMEOUT` | `15` | Seconds to wait for a pooled connection before failing fast. |
| `FINGERPRINT_FFMPEG_THREADS` | `1` | Decoder threads per background fingerprint FFmpeg pass. Keep at one when fingerprinting shares CPU with the web app. |
| `SKIP_API_VALIDATION` | `false` | Testing-only: bypass API-key validation. Never enable in production. |

### Compose-level variables (not read by the app)

`POSTGRES_USER` (`curatio`), `POSTGRES_DB` (`curatio`), and `POSTGRES_PASSWORD` (required) are
consumed by `docker-compose.yml` to provision the database container and assemble
`DATABASE_URL`; the application itself only reads `DATABASE_URL`.

### Integrations

Plex and Jellyfin have **no** environment variables. Connect them in **Admin → Integrations**;
settings are stored encrypted in `admin_settings` (`integration_plex_v1`,
`integration_jellyfin_v1`). Libraries resync every 6 hours and on demand ("Sync now").

### Addons

Addons are Python packages that declare a `curatio.sources` entry point and implement
`app.sources.sdk.SourceProvider`. Install them into the server's environment (or a derived Docker
image) and restart; they appear under Admin → Integrations, and an addon may serve its own
settings page at `/admin/addons/{id}/`. Curatio refuses addons built for a different
`SDK_VERSION`. Addons are third-party software: their owners are responsible for what they
connect to.

## First-run behavior

- **The app will not start without the required configuration** listed above — missing values
  raise a validation error at import (`app/config.py:190`).
- **Connect Plex or Jellyfin first** (Admin → Integrations). The first sync stores the library
  and watch history, tags the owned titles in the background, and builds the "On Your Servers"
  and owned rows. Until a server is connected, nothing is playable and every title is a
  recommendation you can add to your Watchlist.
- Discovery rows for titles you don't own (Everything mode) come from tagged catalog titles. A
  tag pack or a catalog build fills these; without either, Home leans on your own library.
- On startup the former 40 universal definitions are retained as **inactive compatibility
  records**; synthesized rows appear once enough current tags exist.

## Documentation

- [`docs/playback-api.md`](playback-api.md) — client API v2: playback sources,
  availability, failure reports and the version handshake.
- [`docs/imdb-keyed-sync.md`](imdb-keyed-sync.md) — the IMDb-or-TMDB keyed sync contract for
  progress, scrobbles, history, and library.
- [`docs/multi-profile-ios.md`](multi-profile-ios.md) — the multi-profile account model and
  native (iOS/iPadOS) integration guide.
- [`docs/native-api-ios.md`](native-api-ios.md) — the complete native discovery API contract
  for iOS/iPadOS, including tags, reviews, ratings, coming-soon dates, and Swift decoding notes.

## Development

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Tests / lint / types
pytest tests/ -v --cov=app --cov-report=term
black app/ workers/ tests/
ruff check app/ workers/ tests/
mypy app/ --ignore-missing-imports

# Local DB access via Compose dev override (exposes 127.0.0.1:5432)
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

## Contributing

Issues and pull requests are welcome. Please run the tests, `black`, `ruff`, and `mypy` (the same
checks CI runs — `.github/workflows/build.yml`) before opening a PR.

## License

Released under the [MIT License](../LICENSE). Copyright (c) 2026 Curatio contributors.
