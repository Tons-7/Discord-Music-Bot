# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Discord music bot built with discord.py (2.7.1+), supporting YouTube, Spotify, and SoundCloud playback. Uses yt-dlp for
stream extraction, spotipy for Spotify, pylast for Last.fm autoplay recommendations, FFmpeg for audio, and lrclib.net
for lyrics.

## Running the Bot

```bash
# Install dependencies
pip install -r requirements.txt

# Run the bot (requires .env with BOT_TOKEN, optionally SPOTIFY_CLIENT_ID/SECRET, LASTFM_API_KEY/SECRET)
python main.py
```

FFmpeg must be installed and on PATH. `davey` is required by discord.py for voice encryption. No test suite exists.
Docker installs `deno` as yt-dlp's JS runtime (signature solving); optional locally but avoids missing formats.

## Architecture

**Entry flow:** `main.py` → creates `MusicBot` (subclass of `commands.Bot`) → loads 3 cogs → starts bot.

**Core class (`bot.py`):** `MusicBot` owns all shared state:

- `guilds_data[guild_id]` — per-guild dict with queue, history, current song, voice client, playback state, locks.
  Lazy-initialized via `get_guild_data()`.
- `song_cache` — LRU-style cache for yt-dlp results (TTL from `config.CACHE_TTL`).
- Shared service singletons: `_music_service`, `_playback_service`.
- Background tasks (cleanup, timestamp updates, voice health checks).
- SQLite database for playlists, guild settings, and queue persistence.

**Service layer (`services/`):**

- `MusicService` — song search, metadata extraction, Spotify→YouTube conversion, Last.fm related songs for autoplay.
- `PlaybackService` — controls FFmpeg playback, manages `play_next` loop, autoplay prefetching, timestamp embed updates.
  Creates its own `QueueService` and `MusicService` internally.
- `QueueService` — queue/history/loop_backup manipulation, shuffle, loop modes.

**Important:** Cogs reuse `bot._music_service` and `bot._playback_service` singletons. All service instances share the
same `bot` reference, so they operate on the same `guilds_data` state. There is no service-level state beyond
`self.bot`.

**Cogs (`cogs/`):**

- `MusicCommands` — all slash commands (play, pause, skip, seek, queue, etc.) and owner-only prefix commands (
  ban/unban/leaveguild).
- `PlaylistCommands` — server playlist, global playlist, and history commands. Server playlists are scoped to
  `(user_id, guild_id)`, global playlists to `(user_id)` only. Both share the same handler methods via `global_mode`
  flag, with DB helpers abstracting the table difference.

**Views (`views/`):** Discord UI components — `NowPlayingControls` (playback buttons), `PaginationView`,
`SongSelectView`.

**Playback flow:** `play_next()` acquires `play_lock` → `get_next_song()` (respects loop mode) →
`_extract_and_play_song()` (fresh stream URL with retries) → `_start_playback()` (FFmpeg + after_playing callback) →
callback calls `play_next()` again via `run_coroutine_threadsafe`.

**Autoplay:** When queue empties and autoplay is enabled, uses Last.fm similar tracks/artists/tags to find related
songs. Pre-fetches next song in background while current plays.

**Database:** SQLite (`music_bot.db`, schema version 3) with tables:

- `playlists` — per-guild user playlists.
- `global_playlists` — cross-guild user playlists.
- `guild_settings` — autoplay, DJ role, default volume, effects, etc.
- `favorites` — user-favorited songs with play counts.
- `user_stats` — per-user per-guild listening statistics.
- `playlist_collaborators` — shared playlist access control.
- `schema_version` — tracks DB migration version.

Queue state persisted on changes and restored on startup.

## Key Patterns

- All blocking I/O (yt-dlp, Spotify, Last.fm) runs in `bot.executor` (ThreadPoolExecutor, 3 workers) via
  `run_in_executor`.
- Guild state uses a `play_lock` (asyncio.Lock) to prevent concurrent `play_next` calls.
- `save_guild_queue()` debounces DB writes with a 1-second delay via `_delayed_save_guild_queue`.
- Now-playing embeds update every 1 second; message validation is cached for 10 seconds to reduce API calls.
- Voice reconnection: bot auto-reconnects and resumes playback when disconnected non-intentionally.
- yt-dlp: never pin `player_client` in extractor_args — pinned client lists go stale as YouTube gates clients
  (PO tokens/SABR) and break extraction; yt-dlp's per-release defaults track this.

## Config (`config.py`)

All tunable constants:

- Appearance: `COLOR`
- Pagination & limits: `SONGS_PER_PAGE`, `MAX_PLAYLIST_SIZE`, `MAX_HISTORY_SIZE`
- Search: `DEFAULT_SEARCH_RESULTS`, `MAX_SEARCH_RESULTS`
- Cache: `MAX_CACHE_SIZE`, `CACHE_TTL`
- Timeouts: `INACTIVE_TIMEOUT_MINUTES`, `NOW_PLAYING_RESEND_SECONDS`
- Cooldowns: `COMMAND_COOLDOWN`, `PLAY_COOLDOWN`
- Audio effects: `AUDIO_EFFECTS` — dict of FFmpeg filter chains (none, bass_boost, nightcore, vaporwave, treble_boost,
  8d) with speed multipliers for progress tracking.
- Lyrics: `LYRICS_API_BASE` (lrclib.net)
- Database: `DB_VERSION`
- Also exports `get_intents()` helper.
- Activity: `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `ACTIVITY_PORT`

## Discord Activity

**Architecture:** FastAPI backend in the same Python process as discord.py (shared `guilds_data`). Next.js static export
served by FastAPI. WebSocket for real-time state sync. Audio plays directly in the browser via `<audio>` element (not
through voice channel), with separate M4A disk cache at `audio_cache/activity/`.

**Entry point:** `main.py` → uvicorn runs FastAPI app (`activity/app.py`) → bot runs as
`asyncio.create_task(bot.connect())` on the same event loop.

**Backend (`activity/`):**

- `app.py` — FastAPI lifespan: bot login/connect, WebSocket manager, broadcast hooks, position broadcaster, cleanup on
  last disconnect.
- `routes/` — REST API: auth (token exchange), state, playback controls, queue management, search, playlists (server +
  global), favorites, lyrics (with synced LRC), stream proxy + disk cache, image proxy, config.
- `ws_manager.py` — WebSocket connection manager with `on_last_disconnect` callback.
- `cog_hooks.py` — Non-invasive wrappers on service methods to broadcast state changes via WebSocket.
- `state_serializer.py` — Converts `guilds_data` to JSON, handles Activity-only position calculation.

**Frontend (`activity-frontend/`):**

- Next.js static export (`output: 'export'`), React Compiler enabled, Tailwind CSS v4, SWR, dnd-kit,
  Discord Embedded App SDK v2.4.1. See `activity-frontend/AGENTS.md` for frontend-specific conventions.
- `useAudioPlayer` hook manages `new Audio()` — persists across tab switches, syncs to server position on song load,
  surfaces autoplay-policy blocks as a tap-to-play overlay, and tears the element down on remote stop.
- Playback position is never React state: WS ticks go into a ref + subscription (`ServerPosition`); progress bars and
  time labels are rAF-driven via `transform` (zero re-renders during playback).
- Bass/Treble/8D effects run through a lazily-created Web Audio chain mirroring the FFmpeg filters; nightcore/vaporwave
  use `playbackRate` + `preservesPitch`.
- Side panel layout: Now Playing always visible, Queue/Search/Playlists/Lyrics/History/Favorites/Stats slide in from
  right; mobile gets a 4-tab bottom nav + "More" sheet, tappable mini player, and touch drag-reorder (long-press).
- Panel data (favorites/stats/playlists/lyrics) is SWR-cached; lyrics are prefetched ~1.5s after a song starts.
- PiP/minimized view: `useLayoutMode` subscribes to `ACTIVITY_LAYOUT_MODE_UPDATE`, renders `PiPView` (fullscreen
  thumbnail, tap-to-play/pause, always-visible bottom progress strip; text only when the tile is ≥150px tall).
- Click/tap a row to add in Search/History/Favorites/Playlist detail, duplicate detection, toast notifications.

**Audio streaming (`stream_routes.py`):**

- Format: `bestaudio/best` → yt-dlp gets highest quality (Opus ~160kbps); `/best` only fires when no audio-only stream
  exists. `FFmpegExtractAudio` postprocessor converts to M4A for disk cache.
- Cached: `FileResponse` with Range support — instant playback + full seeking.
- Uncached: both Range and full requests use `StreamingResponse` and forward chunks from YouTube's CDN as they arrive.
  **Never** `await upstream.content.read()` before responding — Chromium issues `Range: bytes=0-` for `<audio>`
  elements, so buffering the body blocks playback until the entire song is downloaded. Background cache download starts
  on first play; next song is pre-cached.
- `_get_cached_file()` returns `None` while a URL is in `_downloading`, forcing the proxy path until the M4A is fully
  written to disk.
- Seeking also notifies backend (`POST /seek`) to keep server position in sync.

**Activity-only playback:** Works without voice channel. Pause/resume tracked via `pause_position`/`start_time`.
Autoplay uses Last.fm recommendations. State cleared when last Activity client disconnects. When adding songs with
nothing playing, `_auto_start_if_idle` advances server-side via `activity_advance`, so the broadcast that follows
already shows the song as current instead of leaving it in the queue for a frontend round-trip; it always returns
`auto_play: false` and the frontend's `maybeAutoPlay` is a no-op guard. User-initiated skips must send `POST /play?force=true`
(plain `/play` is idempotent and no-ops while something is playing). Position is computed as `elapsed * speed +
seek_offset`, so `/speed` and `/effects` call `_rebase_activity_clock()` before changing the effective speed — otherwise
already-played time gets rescaled. Playlist songs stored without thumbnails get them derived from the YouTube video ID
at read time (`fill_missing_thumbnails`).

**Activity listening stats:** `record_activity_listening()` in `activity/helpers.py` records stats at song transitions (
skip, stop, previous, skipto, play, on_last_disconnect). Skips when voice client is connected (PlaybackService handles
those). `ws_manager.py` tracks user IDs per WebSocket for attribution.

**Frontend caching/deploy:** Build chunk filenames are NOT content-hashed across builds, and Discord's activity proxy +
Cloudflare cache them — so `next.config.ts` sets a per-build `assetPrefix` (`/v-<stamp>`) and `FrontendStaticFiles` in
`app.py` strips the version segment, serving versioned paths as immutable and `index.html` as no-cache. The Connecting
screen shows `build <stamp>` (`NEXT_PUBLIC_BUILD_ID`) — check it before debugging "change didn't apply". Deploying UI
changes requires `docker compose build bot && docker compose up -d` (a local `npm run build` alone is not served).

**Docker:** Multi-stage build — Node.js builds frontend, Python runs the bot + API. `docker-compose.yml` exposes
`ACTIVITY_PORT` and runs a `cloudflared` tunnel.

**Dev setup:** Set `DISCORD_CLIENT_ID`/`DISCORD_CLIENT_SECRET` in `.env`. Enable Activities in Discord Developer Portal.
Set URL mapping. Build frontend: `cd activity-frontend && npm run build`. Run: `python main.py`.
