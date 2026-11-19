<p align="center"><img src="static/logo.png" alt="Curatio" width="200"></p>

# Curatio Server

[![Build](https://github.com/curatio-app/curatio/actions/workflows/build.yml/badge.svg)](https://github.com/curatio-app/curatio/actions/workflows/build.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Your library, curated.** Curatio is a free app that helps you decide what to watch from the movies and shows you already have on **Plex** or **Jellyfin**. It learns what you like from what you've watched, builds rows like *Because You Watched*, *Unwatched on Your Servers* and *Short Tonight*, says why it picked each title, and plays the file in full quality.

Curatio Server is the part you run yourself. It sits next to your Plex or Jellyfin server and handles the recommendations, sign-in and profiles. The Curatio apps connect to it. **Your media never moves**: video still comes from your Plex or Jellyfin server.

```text
 Apple TV · iPhone · iPad · Mac · Windows · Web
              │
              ▼
       Curatio Server  ──────►  Plex / Jellyfin  (your movies and shows)
   (this repo, on your hardware)
```

- **Free.** No subscription, no ads, no account with us.
- **Private.** Your library, watch history and accounts live on your own server.
- **Works without AI.** An AI key (Gemini, Claude, OpenAI, or a free local model) makes picks sharper, and costs a few cents for a whole library.

Website: [curatio.tv](https://curatio.tv)

---

## Contents

1. [What you need](#1-what-you-need)
2. [Install](#2-install) ([Unraid](#unraid))
3. [First sign-in and connecting Plex or Jellyfin](#3-first-sign-in-and-connecting-plex-or-jellyfin)
4. [Create your account and invite people](#4-create-your-account-and-invite-people)
5. [Connect the apps](#5-connect-the-apps)
6. [Make recommendations better (optional)](#6-make-recommendations-better-optional)
7. [Updating, backups and moving servers](#7-updating-backups-and-moving-servers)
8. [Troubleshooting](#8-troubleshooting)
9. [Use your own reverse proxy](#9-use-your-own-reverse-proxy)
10. [For developers](#10-for-developers)

---

## 1. What you need

| | Why |
|---|---|
| **A Plex or Jellyfin server** with movies and/or shows | Curatio reads your library from it and plays from it. Both at once works too. |
| **A computer that can run Docker** (Linux PC, NAS, Mac, mini PC or a VPS), 2 GB RAM or more | Runs Curatio Server and its database. |
| **A domain name** pointing at that computer, e.g. `curatio.yourname.com` | Sign-in uses passkeys, which browsers only allow over HTTPS. Curatio sets up the certificate for you. |
| **A free TMDB API key** | Posters, descriptions and title matching. Get one at [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api). |
| *Optional:* a free **MDBList** key | Adds IMDb, Rotten Tomatoes and other ratings. [mdblist.com/preferences](https://mdblist.com/preferences/). |
| *Optional:* an **AI key** or a local model | Sharper tags, so sharper picks. See [step 6](#6-make-recommendations-better-optional). |

**Where to run it.** The best place is the same machine or network as Plex/Jellyfin. If you use a VPS instead, the VPS has to be able to reach your media server (Plex: allow remote access; Jellyfin: a public HTTPS address), and away from home, video passes through the VPS, so its bandwidth matters. At home, the apps play straight from your media server.

---

## 2. Install

These commands are for Linux or macOS. On a NAS or Windows, run the same steps in a terminal that has Docker.

**a. Point your domain at the server.** At your DNS provider, add an `A` record for your chosen name (e.g. `curatio.yourname.com`) with the server's public IP. If the server is at home, forward ports **80** and **443** on your router to it.

**b. Download the install kit:**

```bash
git clone https://github.com/curatio-app/curatio.git
cd curatio/deploy
cp .env.example .env
```

**c. Fill in `.env`.** Open it (`nano .env`) and set:

| Setting | What to put |
|---|---|
| `BASE_URL` | `https://` + your domain, e.g. `https://curatio.yourname.com` |
| `CURATIO_DOMAIN` | your domain without `https://` |
| `TMDB_API_KEY` | your TMDB key |
| `MASTER_PASSWORD` | a password of 16+ characters for the admin page |
| `SECRET_KEY` | the output of `openssl rand -hex 32` |
| `POSTGRES_PASSWORD` | the output of `openssl rand -hex 24` |

Keep `.env` private and back it up. **Never change `SECRET_KEY` later**: it encrypts your stored Plex/Jellyfin sign-in.

**d. Start it:**

```bash
docker compose --profile https up -d
```

This starts the database, Curatio Server and Caddy (which gets the HTTPS certificate). After a minute, check it:

```bash
curl https://curatio.yourname.com/health
```

You should see `"status":"healthy"`. If not, see [Troubleshooting](#8-troubleshooting).

### Unraid

Curatio is in **Community Applications**: search for **Curatio** on the Apps tab and click **Install**. This version has its database built in, so it's one container.

1. Fill in **Base URL** (the `https://` address people will use), **TMDB API key** and **Admin password** (16+ characters). Leave the rest as it is; you can add an AI key later in the admin page.
2. Click **Apply**. Your data, database and generated secrets live in `/mnt/user/appdata/curatio`; back up that folder.
3. Put it behind HTTPS with the reverse proxy you already use (SWAG, Nginx Proxy Manager, Cloudflare Tunnel), pointing at port `8000`. See [Use your own reverse proxy](#9-use-your-own-reverse-proxy).

Then continue with [step 3](#3-first-sign-in-and-connecting-plex-or-jellyfin). Not on Unraid but want one container? Run `ghcr.io/curatio-app/curatio:aio` with a volume on `/data` and the same three settings.

---

## 3. First sign-in and connecting Plex or Jellyfin

1. Open `https://curatio.yourname.com/admin` and sign in with your `MASTER_PASSWORD`.
2. Go to **Playback → Integrations**.
   - **Plex:** click **Sign in with Plex**, approve Curatio on plex.tv, and choose your server.
   - **Jellyfin:** enter your Jellyfin address and either a username and password or an API key (Jellyfin Dashboard → API Keys).
3. Curatio syncs your library in the background (a few minutes for a big library) and checks for new additions every couple of minutes after that.

---

## 4. Create your account and invite people

Curatio is invite-only, including for you.

1. In the admin page, open **Access → Invites** and create an invite.
2. Open the invite link (or go to `https://curatio.yourname.com/account/register` and paste the code). Create your account and a passkey. An authenticator app is the backup.
3. Back in **Access → Users**, select your account and click **Make admin**.

Your account's first profile now uses your Plex/Jellyfin **watch history** automatically, so your Home screen is personal from the start. Other profiles can switch this on under **Settings → Server Watch History**.

To add family or friends, create one invite per person. Each person can add profiles for their household.

---

## 5. Connect the apps

- **Apple TV, iPhone, iPad:** install [Curatio Media Player from the App Store](https://apps.apple.com/app/id6762537510) (tvOS 18.6 / iOS 17.5 or later), open it and enter your server address. On Apple TV, the app shows a 6-character code: enter it at `https://curatio.yourname.com/account/pair` while signed in.
- **Mac and Windows:** install the Curatio desktop app and sign in with your server address.
- **Web:** open `https://curatio.yourname.com` and sign in.
- **Roku:** coming soon.

**Home** has a switch at the top:
- **My Servers** shows only what's on your Plex/Jellyfin.
- **Everything** also shows titles you don't have yet, marked so you can tell them apart.

Long-press any title and choose **Not for Me** to hide it and see fewer like it.

---

## 6. Make recommendations better (optional)

Curatio tags every title on your servers with things like mood, themes, pace and era, and recommends by matching those tags to what you've watched.

- **Without an AI key**, titles get rule-based tags (genre, decade, country, runtime). It works, but picks are broader.
- **With an AI key**, titles also get mood, theme and style tags. In **Admin → Discovery → Tagging**, choose a provider, paste the key, and pick a model. Only titles on your servers and on Watchlists are tagged, at a typical cost of a few cents per thousand titles. Set a monthly limit with `LIBRARY_TAGGING_MONTHLY_CAP_USD`.
- **Free and private:** run a model locally with [Ollama](https://ollama.com) or LM Studio, and choose **Local model (OpenAI-compatible)** with the address `http://host.docker.internal:11434/v1`.
- **MDBList:** paste your key on the same Tagging page to add ratings.
- **Tag pack:** every install downloads Curatio's shared tag pack (AI tags for about 93,000 popular titles) within six hours of starting (or straight away: **Download from pack URL** on the Library & tag pack page), so many of your titles start with sharp tags at no cost. It holds only tags keyed by TMDB/IMDb id, and nothing about your library is sent anywhere. To turn it off, set `TAG_PACK_URL=` (empty) in `.env`.

Progress is shown under **Tagging → Library & tag pack**.

---

## 7. Updating, backups and moving servers

**Update to the latest version:**

```bash
cd curatio/deploy
git pull
docker compose --profile https pull
docker compose --profile https up -d
```

**On Unraid**, updates appear on the Docker tab ("update ready"); click the icon and **Apply update**. Back up `/mnt/user/appdata/curatio` (stop the container first, or use the Appdata Backup plugin).

**Back up** (database plus settings). Run this regularly and keep the files somewhere else:

```bash
docker exec curatio-postgres pg_dump -U curatio -Fc curatio > curatio-$(date +%F).dump
cp .env curatio-env-$(date +%F)
```

**Restore** on a new machine: copy `.env` across, run `docker compose up -d postgres`, then:

```bash
docker exec -i curatio-postgres pg_restore -U curatio -d curatio --clean --if-exists < curatio-YYYY-MM-DD.dump
docker compose --profile https up -d
```

---

## 8. Troubleshooting

| Problem | What to check |
|---|---|
| The server doesn't start | `docker compose logs app --tail 50`. A "Configuration errors" line names the setting to fix in `.env`; the usual cause is a value still set to its `change_me` placeholder. |
| No HTTPS / certificate errors | `docker compose logs caddy --tail 50`. The domain must point at this machine, and ports 80 and 443 must be reachable from the internet. |
| Can't create a passkey | You must use the `https://` address, not an IP address or `http://`. |
| Plex or Jellyfin titles don't appear | **Admin → Playback → Integrations** shows the last sync and any error. Click **Sync now**. The server running Curatio must be able to reach your media server's address. |
| Titles show but won't play | Open the title's sources in the app to see the error. Check that Plex/Jellyfin can play the file itself, and that your media server allows remote connections if Curatio runs elsewhere. |
| Recommendations feel generic | Make sure your profile uses server watch history (**Settings → Server Watch History**), watch or mark a few titles as watched, and consider adding an AI key ([step 6](#6-make-recommendations-better-optional)). |

Still stuck? Use the contact form on [curatio.tv](https://curatio.tv/#support).

---

## 9. Use your own reverse proxy

Already run Nginx Proxy Manager, Traefik, Cloudflare Tunnel or Tailscale? Skip Caddy:

1. Start without the `https` profile: `docker compose up -d`.
2. Point your proxy at `http://127.0.0.1:8000`. If the proxy runs on another machine or in another Docker network, set `CURATIO_LISTEN=0.0.0.0` in `.env` and point it at this machine's address, port 8000.
3. Keep `BASE_URL` set to the public `https://` address your proxy serves.
4. Allow large request bodies and long-lived responses (video streams can run for hours): in Nginx, `proxy_buffering off;` and `proxy_read_timeout 3600s;`.

---

## 10. For developers

The server is a FastAPI app with a React admin dashboard. Architecture, every setting and how to run the tests are in [docs/SERVER.md](docs/SERVER.md). Issues and pull requests are welcome.

---

*Curatio uses the TMDB API but is not endorsed or certified by TMDB. Plex and Jellyfin are trademarks of their respective owners; Curatio is not affiliated with them.*
