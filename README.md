# Discord Music Bot

A **modular Discord music bot** written in Python using `discord.py`.
Supports YouTube, SoundCloud, Spotify, Apple Music and Tidal links with queue management, playlists, audio effects, lyrics,
autoplay, and an optional **Discord Activity** (in-Discord web UI) for browser-based playback.

## Features

- Play music from YouTube, SoundCloud, Spotify, Apple Music or Tidal links, or by search query
- Queue, history, and playlist management (server and global playlists)
- Playback controls: skip, pause, resume, seek, loop, shuffle
- Audio effects: bass boost, nightcore, vaporwave, treble boost, 8D audio
- Speed control with pitch preservation
- Autoplay mode using Last.fm recommendations (or YouTube's Mix when Last.fm isn't set up)
- Lyrics via lrclib.net (including synced LRC)
- Audio file caching for near-instant seeking
- Leaderboard and listening statistics
- DJ role support for permission control
- Auto-reconnect on voice disconnection; pauses (and later leaves) when nobody is listening
- Queue persistence across bot restarts
- **Discord Activity**: browser-based player that runs inside Discord with search,
  queue, playlists, favorites, lyrics, history, and a picture-in-picture mode

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) (manages Python 3.14 and dependencies)
- [FFmpeg](https://www.ffmpeg.org/download.html) installed and on PATH
- [Deno](https://deno.com/) on PATH, recommended: yt-dlp uses it to solve YouTube's challenges (without it some videos
  won't play)
- Node.js 26+ (only if you change the Discord Activity frontend)

## Setup

1. Clone the repository
2. Install Python dependencies (creates `.venv` from `uv.lock`):
   ```
   uv sync
   ```
3. Copy `.env.example` to `.env` and fill in your tokens:
   ```
   cp .env.example .env
   ```
4. (Only after changing the frontend) Rebuild the Discord Activity. The built `activity-frontend/out` is committed,
   so this step is optional otherwise; commit the rebuilt `out/` with your change:
   ```
   cd activity-frontend
   npm install
   npm run build
   cd ..
   ```
5. Run the bot:
   ```
   uv run python main.py
   ```

### Managing dependencies

Dependencies are declared in `pyproject.toml` and pinned in `uv.lock`; both are committed.

```
uv add <package>      # add a dependency (updates pyproject.toml + uv.lock)
uv remove <package>   # remove one
uv lock --upgrade     # refresh pinned versions
uv sync               # match .venv to the lockfile
```

`requirements.txt` is kept only for hosts that have `pip` but not uv. It mirrors `[project.dependencies]` with
the same loose bounds — update it alongside any `uv add`/`uv remove`, and don't replace it with a pinned
`uv export` (see the note in `CLAUDE.md`).

## Docker

```
docker compose up --build
```

The Docker image builds the Activity frontend during the image build, so Node.js is not required on the host.

- Data (database, audio cache, bans) lives in the `bot-data` volume. On its first start the container imports an
  existing `./music_bot.db` and audio cache from the project folder. `docker compose down -v` deletes the volume.
- The API port is published on `127.0.0.1` only (`ACTIVITY_PUBLISH_IP` changes that); the `cloudflared` service
  reaches the bot over the compose network and needs `CLOUDFLARE_TUNNEL_TOKEN`. Without a tunnel, run
  `docker compose up -d bot`.

## Configuration

All configuration is done through environment variables in `.env`:

| Variable                | Required                 | Description                                    |
|-------------------------|--------------------------|------------------------------------------------|
| `BOT_TOKEN`             | Yes                      | Discord bot token                              |
| `SPOTIFY_CLIENT_ID`     | No                       | Spotify API client ID                          |
| `SPOTIFY_CLIENT_SECRET` | No                       | Spotify API client secret                      |
| `LASTFM_API_KEY`        | No                       | Last.fm API key (better autoplay)              |
| `LASTFM_API_SECRET`     | No                       | Last.fm API secret (not needed: calls are read-only) |
| `LRCLIB_USER_AGENT`     | No                       | Identifiable client string for LRCLIB requests |
| `DISCORD_CLIENT_ID`     | Activity only            | Discord application client ID                  |
| `DISCORD_CLIENT_SECRET` | Activity only            | Discord application client secret              |
| `ACTIVITY_PORT`         | No (default `8080`)      | Port the FastAPI/Activity server listens on    |
| `ACTIVITY_SECRET`       | No                       | Signs Activity session tokens (falls back to `DISCORD_CLIENT_SECRET`) |
| `CLOUDFLARE_TUNNEL_TOKEN` | Docker tunnel only     | Token of the Cloudflare tunnel that exposes the Activity |

`.env.example` lists every other setting (database path and backups, cache limits, voice timeouts, cookies for
yt-dlp, LRCLIB registration, …), each commented out with its default.

Spotify credentials can be obtained from the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
Last.fm credentials can be obtained from [Last.fm API](https://www.last.fm/api/account/create).

## Discord Activity

The bot ships with an optional Activity that lets users play music in a browser pane inside Discord
(no voice-channel connection required).

To enable it:

1. In the [Discord Developer Portal](https://discord.com/developers/applications), open your application and
   enable **Activities**.
2. Configure **URL Mappings** to route the Activity to the host running this bot on `ACTIVITY_PORT`.
3. Set `DISCORD_CLIENT_ID` and `DISCORD_CLIENT_SECRET` in `.env`.
4. Build the frontend (step 4 of Setup above) and run `uv run python main.py`. The Activity is served by the same process.

Architecture notes live in `activity-frontend/README.md` and in `CLAUDE.md`.

Make sure `.env` remains in `.gitignore` so it is **never committed**.

## Disclaimer

This bot is only for personal use.
