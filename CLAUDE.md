# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Discord music bot built with discord.py (2.7.1+), supporting YouTube, SoundCloud, Spotify, Apple Music and Tidal links.
Uses yt-dlp for stream extraction, spotipy for Spotify, pylast for Last.fm autoplay recommendations, FFmpeg for audio,
and lrclib.net for lyrics. A Discord Activity (FastAPI + Next.js) runs in the same process.

## Running the Bot

```bash
# Create/refresh the virtualenv from uv.lock
uv sync

# Run the bot (requires .env with BOT_TOKEN, optionally SPOTIFY_CLIENT_ID/SECRET, LASTFM_API_KEY)
uv run python main.py
```

Dependencies live in `pyproject.toml` and are pinned by `uv.lock`. Add with `uv add <pkg>`, remove with
`uv remove <pkg>`, upgrade with `uv lock --upgrade`. Docker builds with `uv sync --locked`, so commit the
refreshed lockfile with any dependency change or the image build fails.

`requirements.txt` is what the Wispbyte/Pterodactyl host installs (it has no uv, just `pip`), so it must list the
same packages and bounds as `[project.dependencies]` (`scripts/check_requirements.py` checks this in CI). Either
file may be edited first; after any change, update the other and run `uv lock`. Runtime deps only — dev tools (ruff,
pyrefly, pytest, httpx for the API tests) stay in the `dev` dependency group and never go in `requirements.txt`. Do
**not** replace it with a `uv export` of the full lock: the host's egg installs this file and game-tracker's together
through `all-requirements.txt`, and pinned transitive versions can deadlock that shared resolve (`google-genai` shares
pydantic/websockets/requests with this bot). `uv.lock` remains the real lock for local dev and Docker.

Python **3.14** everywhere (`requires-python`, `.python-version`, Docker, and the Wispbyte host). Ruff and pyrefly
infer 3.14 from `requires-python`, so lint fixes may assume 3.14 semantics (e.g. lazy PEP 649 annotations).

FFmpeg must be installed and on PATH. `davey` is required by discord.py for voice encryption. yt-dlp is installed with
its `[default]` extra, which brings `yt-dlp-ejs` (YouTube's challenge solver); it needs a JS runtime, Deno (Docker
copies one in, Wispbyte's bootstrap downloads one). Without Deno, yt-dlp drops the clients that need challenge solving.

Tests live in `tests/` and run with `uv run pytest`. CI (`.github/workflows/ci.yml`) runs them plus
`ruff check --select F,E9`, pyrefly (report-only until a baseline exists), the requirements mirror check, the frontend
typecheck/tests/build, a Docker build with a smoke test, and dependency audits.

## Architecture

**Entry flow:** `main.py` → uvicorn runs `activity.app:app` → its lifespan creates `MusicBot`, adds the 2 cogs
(`MusicCommands`, `PlaylistCommands`), logs in (retrying network and Discord outages) and runs `bot.connect()` as a
task that is restarted after failures discord.py doesn't retry itself. If Discord ends the connection for good, the
process exits so its supervisor restarts it.

**Core class (`bot.py`):** `MusicBot` owns all shared state:

- `guilds_data[guild_id]` — per-guild dict with queue, played, history, current song, voice client, playback state,
  locks. Lazy-initialized via `get_guild_data()`.
- `song_cache` — LRU-style cache for yt-dlp results (TTL from `config.CACHE_TTL`).
- Shared service singletons: `music`, `playback`, and `audio_cache` (`AudioCacheService`).
- `http_session` — one shared `aiohttp.ClientSession` (pass `timeout=`/`headers=` per request; closed in `close()`).
  `stream_session` is the Activity audio proxy's own pool, so long-lived streams never make API requests wait.
- `ytdl_options(**overrides)` — a fresh yt-dlp options dict per call (see Key Patterns).
- `add_state_listener()` / `notify_state_changed(guild_id)` — called by `save_guild_queue`,
  `clear_guild_queue_from_db` and pause/resume; the Activity registers one listener to broadcast state.
- Background tasks (cleanup, 1 s timestamp updates, voice health and stall checks, daily database maintenance).
- SQLite database at `config.DB_PATH` for playlists, guild settings, favorites, stats and queue persistence.

**Service layer (`services/`):**

- `MusicService` — link routing by host (`link_platform`), single-song vs collection lookups, search, playlists,
  Spotify/Apple Music/Tidal → YouTube matching, Last.fm related songs with a YouTube Mix fallback for autoplay.
  `get_song_info(..., raise_errors=True)` raises `ExtractionError` (`.permanent`, `.bot_check`) for failed links.
- `PlaybackService` — the `play_next` loop and every voice action the cog, buttons and Activity share: `seek`, `stop`,
  `skip` (moves on in song loop), `play_previous`, `leave(clear_queue=…)`, `move_to`, `reapply_audio_filters`,
  `park_current`, `pause_for`/`resume_for`; autoplay picks and prefetch; listening-stat credit; voice channel status.
  Creates its own `QueueService`, `MusicService` and `NowPlayingPanel` (`panel`).
- `NowPlayingPanel` (`services/now_playing.py`) — the music channel (falls back to where commands were last used), the
  now-playing panel message and channel notices. Services never call back into the cog.
- `QueueService` — the queue, shuffle, loop modes, listening history, wait estimates. The queue works like YouTube's:
  `played` holds the songs before the current one (oldest first, `PLAYED_LIMIT` outside queue loop) and `queue` the
  ones after it, each song in at most one of them. `get_next_song` moves the finished song to `played`, `skip_ahead`
  jumps forward (passed songs become played), `step_back` goes back up (previous; falls back to `history` when
  nothing was played), queue loop starts the next round from `played`, and re-adding a played song moves it down.
  `history` is only the listening log (/history, autoplay repeats).
- `AudioCacheService` (`services/audio_cache.py`) — voice disk cache (`.opus`), downloaded on `bot.download_executor`
  to a temp name and renamed when complete; live and over-long songs are skipped.
- `playlist_service` — playlist lookup, access levels, the per-playlist lock and writes, shared by the cog and the
  Activity. Edit songs inside `async with playlist_service.editing(bot, playlist):` (slow lookups go before it).

**Important:** Cogs reuse `bot.music` and `bot.playback` singletons. All service instances share the
same `bot` reference, so they operate on the same `guilds_data` state. There is no service-level state beyond
`self.bot` (and the panel's message references inside `guilds_data`).

**Cogs (`cogs/`):**

- `MusicCommands` — slash commands (play, pause, skip, seek, queue, favorites group, stats, forgetme, …) and owner-only
  prefix commands (leaveguild, banuser, unbanuser, listbanned, tasks). Voice joins check permissions, full and AFK
  channels first (`connect_voice`), and won't silence an Activity session in another channel.
- `PlaylistCommands` — server playlists, global playlists and history commands, on top of `playlist_service`. Server
  playlists are scoped to `(user_id, guild_id)`, global playlists to `(user_id)` only.

**Views (`views/`):** `NowPlayingControls` (panel layout + buttons; DJ-checked; `controls=False` for /nowplaying),
`PaginationView`, `SongSelectView` (up to 25 results), `ConfirmView`.

**Playback flow:** `play_next()` acquires `play_lock` → `get_next_song()` (respects loop mode and a one-shot
`force_advance`) → `_try_play()` (cache hit, or a fresh stream from `get_song_info(raise_errors=True)`, waiting out
YouTube's `available_at` preroll) → `_start_playback()` (FFmpeg + after callback) → the callback hops to the loop
(`call_soon_threadsafe`) and `_on_playback_finished` records history/stats and calls `play_next()`. A generation
counter (`play_gen`) lets stop/leave cancel an in-flight `play_next`; a per-player token ignores callbacks of replaced
players. A song FFmpeg drops mid-way is reopened once at the same position. A failing song is skipped (permanent
errors at once), YouTube's bot check stops playback and keeps the queue.

**Autoplay:** When the queue empties and autoplay is on, Last.fm similar tracks/artists/tags find related songs; when
Last.fm isn't configured or finds nothing, the seed's YouTube Mix is used. Repeats are excluded before any search.
The next pick is prefetched while the current song plays (not in loop modes).

**Database:** SQLite (`config.DB_PATH`, default `music_bot.db`, schema version 6) with tables:

- `playlists` — per-guild user playlists. Names can't contain "/" (they travel in Activity URLs; old ones are
  renamed at startup).
- `global_playlists` — cross-guild user playlists.
- `guild_settings` — autoplay, DJ role, music channel, saved queue, etc.
- `favorites` — user-favorited songs (at most `MAX_FAVORITES` per user).
- `user_stats` — per-user per-guild listening statistics (`STATS_RETENTION_DAYS` prunes them; `/forgetme` deletes a
  user's data).
- `playlist_collaborators` — shared playlist access, with a `permission` level per collaborator
  (`view` < `append` < `edit`; the owner always outranks them). Levels live in `config.PLAYLIST_PERMISSIONS`
  and are enforced by `services/playlist_service.py` for the cog and the Activity alike.
- `user_preferences` — per-user Activity preferences (theme), shared across servers and devices.
- `schema_version` — tracks DB migration version.

Queue state (including the playing song and its position) is persisted on changes and restored on startup; a
graceful shutdown never clears saved queues. `DB_BACKUP_DIR` enables daily consistent backups.

## Key Patterns

- Blocking I/O runs in executors via `run_in_executor`: `bot.executor` (yt-dlp extractions and searches; 3 workers,
  raised to min(guilds, 10) on ready), `bot.download_executor` (cache downloads, voice and Activity), `bot.db_executor`
  (SQLite; never queues behind extractions) and `bot.import_executor` (4 workers, bulk lookups such as Spotify imports).
- yt-dlp options always come from `bot.ytdl_options()` / `utils.ytdl.ytdl_options()`: `ignoreerrors: False` (failures
  raise `DownloadError`; every caller catches it), yt-dlp logs through `logging.getLogger("yt_dlp")`
  (`YTDLP_LOG_LEVEL`), TLS checks on, no pinned User-Agent, optional `YTDLP_COOKIES`. Use one `YoutubeDL` per call —
  instances are not thread-safe. Never pin `player_client` in extractor_args — pinned client lists go stale as
  YouTube gates clients (PO tokens/SABR); yt-dlp's per-release defaults track this.
- Guild state uses a `play_lock` (asyncio.Lock) to prevent concurrent `play_next` calls.
- `save_guild_queue()` batches DB writes (at most one per guild per second) and notifies state listeners.
- Now-playing panels update every 1 second (on purpose); a panel that is gone or not editable is dropped instead of
  retried, and its view is stopped only once its message is deleted.
- Elapsed time (`start_time`, `last_activity`, `now_playing_message_sent_time`) uses `time.monotonic()`, never
  wall-clock time, so clock adjustments can't move playback position.
- Voice: discord.py owns transient reconnects; a watchdog rejoins only when discord.py gives up, never after a
  moderator's disconnect or a deleted channel, and the queue is never lost. The bot pauses when nobody is left to
  listen (people in its channel, or Activity viewers) and leaves after `ALONE_TIMEOUT_SECONDS`; server mute or a Stage
  audience pauses it too.
- Slash commands are guild-only (`allowed_contexts` on the bot); they all read `interaction.guild`. The bot sends with
  `AllowedMentions.none()`, and user text in markdown goes through `utils.helpers.md()`. Error replies use
  `reply_error` / `report_command_error` (private, never raw exception text).
- Spotify: since March 2026 the Web API returns 403 unless the app owner has Premium, and it never lists other users'
  playlists. Track links try the API (skipped for 6h after a refusal), then the public embed page. Playlists, albums
  and artists never call the API: `collection_tracks` reads the embed player page (max 100 tracks, no pagination; Apple
  Music albums/playlists come from iTunes and the page's JSON-LD) and `iter_youtube_matches` yields each track's
  duration-checked YouTube match in order as soon as it is found. `/play` and the Activity's "Add all" queue them
  through `PlaybackService.import_songs`, so playback starts with the first; the import is cancelled
  (`cancel_imports`) whenever the queue is cleared or the last Activity viewer leaves. API keys are optional.
- A video link opened from a playlist or Mix (`watch?v=…&list=…`) is that one video; explicit playlist links
  (`/playlist?list=`, SoundCloud sets) are collections (`collection_entries`). `youtube_list_link` gives the list back.

## Config (`config.py`)

All tunable constants (most read from the environment with defaults that keep the old behaviour; `.env.example`
lists every variable):

- Appearance: `COLOR`
- Pagination & limits: `SONGS_PER_PAGE`, `MAX_PLAYLIST_SIZE`, `MAX_HISTORY_SIZE`, `MAX_QUEUE_SIZE`,
  `MAX_COLLECTION_ENTRIES`, `MAX_FAVORITES`
- Search: `DEFAULT_SEARCH_RESULTS`, `MAX_SEARCH_RESULTS`
- Cache: `MAX_CACHE_SIZE`, `CACHE_TTL`, `AUDIO_CACHE_DIR`, `AUDIO_CACHE_MAX_DURATION`, `AUDIO_CACHE_MAX_FILESIZE`
  (the disk caches are never evicted on purpose)
- Voice: `VOICE_RECONNECT_TIMEOUT`, `ALONE_TIMEOUT_SECONDS`, `VOICE_CHANNEL_STATUS`, `MAX_SKIPPED_SONGS`,
  `MAX_TRANSIENT_FAILURES`
- Timeouts: `INACTIVE_TIMEOUT_MINUTES`, `NOW_PLAYING_RESEND_SECONDS`, `GRACEFUL_SHUTDOWN_TIMEOUT`
- Cooldowns: `COMMAND_COOLDOWN`, `PLAY_COOLDOWN`
- Audio effects: `AUDIO_EFFECTS` — dict of FFmpeg filter chains (none, bass_boost, nightcore, vaporwave, treble_boost,
  8d) with speed multipliers for progress tracking. Speed and effect changes rebuild the playing voice song.
- Lyrics: `LYRICS_API_BASE` (lrclib.net), `LRCLIB_USER_AGENT`; optional `LRCLIB_APP_ID`/`LRCLIB_API_KEY` in .env
- Database: `DB_PATH`, `DB_VERSION`, `DB_BACKUP_DIR`, `DB_BACKUP_KEEP`, `STATS_RETENTION_DAYS`
- Logging: `YTDLP_LOG_LEVEL`
- Also exports `get_intents()` helper.
- Activity: `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `ACTIVITY_PORT`

## Discord Activity

**Architecture:** FastAPI backend in the same Python process as discord.py (shared `guilds_data`). Next.js static export
served by FastAPI. WebSocket for real-time state sync. Audio plays directly in the browser via `<audio>` element (not
through voice channel), with separate M4A disk cache at `audio_cache/activity/`.

**Backend (`activity/`):**

- `app.py` — FastAPI lifespan (login with retries, connection kept up, broadcast hooks, position broadcaster, last
  viewer cleanup), security headers (CSP report-only, nosniff), no OpenAPI docs, no CORS (same-origin only), and the
  static export with its cache policy.
- `auth.py` — session tokens (`activity/session.py`, HMAC) and Discord access-token lookups. Session-shaped tokens are
  never sent to Discord; rejected tokens are cached, concurrent lookups share one request, and a per-minute budget and
  Discord's 429s are honoured, so junk requests can't get the bot's IP banned. Ban and membership are checked on
  every path.
- `dependencies.py` — `Annotated` dependencies: `Bot`, `WsManager`, `User`, `Member`, `DJ`, and `Listener`/`Controller`,
  which also require being in the bot's voice channel while it is in one (Activity-only playback is open to members).
- `permissions.py` — DJ checks shared with the cog and buttons (a deleted DJ role counts as none) and the voice rule.
- `routes/` — REST API: auth, state, playback, queue, search, playlists (server + global), favorites, lyrics, stream,
  image proxy, stats, settings, presence, preferences, config, WebSocket.
- `ws_manager.py` — connections per guild (with each viewer's user, instance and voice channel), close codes
  (4001 session, 4003 banned/not a member, 4004 bot not in server; sent after `accept()` so the browser sees them),
  `close_user`/`close_guild`, the last-viewer grace timer (cancelled by a reconnect), messages encoded once.
- `cog_hooks.py` — registers a state listener on the bot and listens for slash command completion and the bot's voice
  moves; `broadcast_state` coalesces them and leaves an unchanged queue/history out of the message.
- `known_songs.py` — metadata of songs already listed to clients. Search results are shared; songs from someone's
  favorites or playlists only serve that user. `/queue/add` uses it (and history) to enqueue without extraction.
- `state_serializer.py` — guild state as JSON: no stream `url` ever, decimal positions, a per-play `seq`, the bot's
  voice channel, autoplay source; `position_now` is the one position function for voice and Activity-only playback.
- `stream.py` — the Activity's audio: stream URLs (one shared extraction per song, cached 30 min, with yt-dlp's
  headers and `available_at`), the M4A disk cache, and the YouTube proxy.
- `rate_limit.py` — per-user limits on search, queue adds and playlist imports.

**Frontend (`activity-frontend/`):**

- Next.js 16 static export (`output: 'export'`), React 19 with the React Compiler, Tailwind CSS v4, SWR, dnd-kit,
  Discord Embedded App SDK v2.5.0. Unit tests run with vitest (`npm test`). See `activity-frontend/AGENTS.md` for
  frontend-specific conventions.
- `useAudioPlayer` hook manages `new Audio()` — persists across tab switches, syncs to server position on song load,
  surfaces autoplay-policy blocks as a tap-to-play overlay, and tears the element down on remote stop. Play/pause is
  decided from the room's shared state, never from the local element.
- Playback position is never React state: WS ticks go into a ref + subscription (`ServerPosition`); progress bars and
  time labels are rAF-driven via `transform` (zero re-renders during playback).
- Bass/Treble/8D effects run through a lazily-created Web Audio chain mirroring the FFmpeg filters; nightcore/vaporwave
  use `playbackRate` + `preservesPitch`.
- Side panel layout: Now Playing always visible, Queue/Search/Playlists/Lyrics/History/Favorites/Stats slide in from
  right; mobile gets a 4-tab bottom nav + "More" sheet, tappable mini player, and touch drag-reorder (long-press).
- The Queue panel is YouTube-style: played songs (state `played`) above a "Now playing" row, upcoming songs below,
  opened scrolled to the current song. A played row's button goes back to it (`/skipto` with `played: true`), and
  played rows are removed with `DELETE /queue/{n}?played=true`.
- Panel data (favorites/stats/playlists/lyrics) is SWR-cached; lyrics are prefetched ~1.5s after a song starts.
- PiP/minimized view: `useLayoutMode` subscribes to `ACTIVITY_LAYOUT_MODE_UPDATE`, renders `PiPView` (fullscreen
  thumbnail, tap-to-play/pause, always-visible bottom progress strip; text only when the tile is ≥150px tall).
- Click/tap a row to add in Search/History/Favorites/Playlist detail, duplicate detection, toast notifications.
- Per-listener colour themes (Midnight, True black, Discord, Ocean, Violet) in Settings → Appearance: `[data-theme]`
  blocks in `globals.css`. Saved per Discord account in `user_preferences` (`/api/me/preferences`); a localStorage
  copy is applied before first paint (`lib/themes.ts`) and replaced by the server's value once it loads (`useTheme`).
- Searching a playlist/album link offers "Add all" (queue) and "Save all to playlist" (`/playlists/{name}/add-url`).
  A video link from a playlist or Mix returns that video plus `list_url`, offered as "Add the whole playlist"
  (`as_list`). URL searches go through `get_song_info_cached`, so both reuse the search's resolution. Spotify and
  Apple Music links are the exception: search lists their tracks straight from the service, and rows carry the
  service's track links, which are only looked up on YouTube when added (never stored in playlists or `known_songs`).

**Audio streaming (`activity/stream.py`, `routes/stream_routes.py`):**

- Cache downloads prefer YouTube's own AAC (`bestaudio[ext=m4a]`, kept as is) and convert anything else to M4A once;
  files are written to a temp name and renamed when complete. Live streams and songs longer than
  `AUDIO_CACHE_MAX_DURATION` are never cached.
- Cached: `FileResponse` with Range support — instant playback + full seeking.
- Uncached: both Range and full requests use `StreamingResponse` and forward chunks from the source as they arrive.
  **Never** read the upstream body before responding — Chromium issues `Range: bytes=0-` for `<audio>`
  elements, so buffering the body blocks playback until the entire song is downloaded. An expired URL (403/410) is
  re-extracted once. HLS/DASH sources (SoundCloud, YouTube live) are repackaged by FFmpeg as fragmented MP4. Background
  cache download starts on first play; the next song is pre-cached.
- `cached_file()` returns `None` while a URL is downloading, forcing the proxy path until the M4A is complete.
- Seeking also notifies backend (`POST /seek`) to keep server position in sync.

**Activity-only playback:** Works without voice channel. Pause/resume tracked via `pause_position`/`start_time`
(`/resume` refuses when nothing is paused). Advance, previous, skip-to and stop run under `play_lock`
(`activity.helpers.activity_*`); stop bumps the generation so an advance still picking an autoplay song drops it.
State is cleared `LAST_VIEWER_GRACE` seconds after the last Activity viewer disconnects (`end_activity_session`). When
adding songs with nothing playing, `_auto_start_if_idle` advances server-side via `activity_advance`, so the
broadcast that follows already shows the song as current. User-initiated skips must send `POST /play?force=true`
(plain `/play` is idempotent: it names the song and its `seq`, and no-ops unless that play is still current). A
listener's "ended" only counts within `ENDED_EARLY_TOLERANCE` of the song's end; an earlier one (a stream cut short,
or a skip without being a DJ) makes the room move on when the song's time is up instead (`_advance_when_due`).
Position is computed as `elapsed * speed + seek_offset`, so speed and effect changes rebase the clock first —
otherwise already-played time gets rescaled. Playlist songs stored without thumbnails get them derived from the
YouTube video ID at read time (`fill_missing_thumbnails`). `/play` or `/join` from the Activity's voice channel hands
the song over to voice at its position; from another channel it is refused.

**Activity listening stats:** `record_activity_listening()` in `activity/helpers.py` credits Activity viewers with the
song time actually played since the last credit (pauses excluded), at song transitions (skip, stop, previous, skipto,
play, the last viewer leaving). Skips when voice client is connected (PlaybackService credits voice listeners).
`ws_manager.py` tracks user IDs per WebSocket for attribution.

**Frontend caching/deploy:** Build chunk filenames are NOT content-hashed across builds, and Discord's activity proxy +
Cloudflare cache them — so `next.config.ts` sets a per-build `assetPrefix` (`/v-<stamp>`) and `FrontendStaticFiles` in
`app.py` strips the version segment, serving versioned paths as immutable (only when found) and `index.html` as
no-cache. The Connecting screen shows `build <stamp>` (`NEXT_PUBLIC_BUILD_ID`) — check it before debugging "change
didn't apply". Docker rebuilds `out/` during the image build. Wispbyte and a local `uv run python main.py` serve the
committed `activity-frontend/out` as-is, so after any UI change run `npm run build` and commit `out/`.

**Docker:** Multi-stage build — Node.js builds frontend, a uv builder stage installs the venv, the runtime runs as a
non-root user with a healthcheck on `/api/config`. Data (database, audio cache, bans) lives in the `bot-data` volume at
`/data` (`DB_PATH=/data/music_bot.db`); the first start imports an existing `./music_bot.db` from the read-only
`/legacy` mount. `docker-compose.yml` publishes `ACTIVITY_PORT` on 127.0.0.1 only and runs a pinned `cloudflared`
tunnel that reads its token from a compose secret.

**Dev setup:** Set `DISCORD_CLIENT_ID`/`DISCORD_CLIENT_SECRET` in `.env`. Enable Activities in Discord Developer Portal.
Set URL mapping. Build frontend: `cd activity-frontend && npm run build`. Run: `uv run python main.py`.
