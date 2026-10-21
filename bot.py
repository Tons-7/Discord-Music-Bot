import asyncio
import concurrent.futures
import contextlib
import datetime
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, NotRequired, TypedDict, cast

import aiohttp
import discord
import pylast
import spotipy
import yt_dlp
from discord.app_commands import AppCommand, AppCommandContext, CommandSyncFailure
from discord.ext import commands, tasks
from spotipy.oauth2 import SpotifyClientCredentials

from config import (
    ALONE_TIMEOUT_SECONDS,
    CACHE_TTL,
    DB_BACKUP_DIR,
    DB_BACKUP_KEEP,
    DB_PATH,
    DB_VERSION,
    INACTIVE_TIMEOUT_MINUTES,
    LRCLIB_USER_AGENT,
    MAX_CACHE_SIZE,
    MAX_FAVORITES,
    STATS_RETENTION_DAYS,
    VOICE_RECONNECT_TIMEOUT,
    get_intents,
)
from models.song import Song
from services.audio_cache import AudioCacheService
from services.music_service import MusicService
from services.playback_service import PlaybackError, PlaybackService
from services.queue_service import cancel_imports
from utils.ytdl import ytdl_options

logger = logging.getLogger(__name__)

# One SQLite connection per executor thread; connections must not cross threads.
_db_local = threading.local()

# Queue writes are batched: at most one per guild per this many seconds.
QUEUE_SAVE_DELAY = 1.0
# A voice drop this soon after the connection was last seen healthy came from Discord's side
# (a moderator's disconnect or a deleted channel), not from discord.py giving up on a reconnect.
VOICE_OK_WINDOW = 3.0
# A move the bot made itself within this many seconds gets no "Moved to" notice.
SELF_MOVE_WINDOW = 10.0

_QUEUE_UPSERT = (
    "INSERT INTO guild_settings (guild_id, queue_data) VALUES (?, ?) "
    "ON CONFLICT(guild_id) DO UPDATE SET queue_data = excluded.queue_data"
)


class FavoritesFull(Exception):
    pass


class GuildData(TypedDict):
    guild_id: int
    queue: list[Song]
    # Songs of the queue already played, oldest first (the queue keeps them, like YouTube's)
    played: list[Song]
    history: list[Song]
    current: Song | None
    seek_offset: float
    loop_mode: str
    shuffle: bool
    volume: int
    autoplay: bool
    autoplay_prefetch: Song | None
    autoplay_prefetch_task: asyncio.Task | None
    voice_client: discord.VoiceClient | None
    intentional_disconnect: bool
    last_activity: float
    now_playing_message: discord.Message | None
    music_channel_id: int | None
    start_time: float | None
    message_ready_for_timestamps: bool
    seeking: bool
    pause_position: float | None
    now_playing_message_sent_time: float | None
    play_lock: asyncio.Lock
    speed: float
    audio_effect: str
    dj_role_id: int | None
    # Set on demand
    import_tasks: NotRequired[set[asyncio.Task]]
    seeking_start_time: NotRequired[float]
    play_gen: NotRequired[int]
    play_token: NotRequired[int]
    source_speed: NotRequired[float]
    listened: NotRequired[float]
    listen_start: NotRequired[float | None]
    paused_by: NotRequired[str | None]
    interrupted: NotRequired[dict | None]
    resume_at: NotRequired[dict | None]
    force_advance: NotRequired[bool]
    stream_retry: NotRequired[str | None]
    voice_ok_at: NotRequired[float]
    voice_lost_at: NotRequired[float]
    voice_left_external: NotRequired[bool]
    self_move_at: NotRequired[float]
    stall_seen: NotRequired[bool]
    voice_status: NotRequired[str | None]
    voice_status_channel: NotRequired[int | None]
    song_seq: NotRequired[int]


class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=get_intents(),
            help_command=None,
            # Every command reads interaction.guild, so keep them out of DMs.
            allowed_contexts=AppCommandContext(guild=True, dm_channel=False, private_channel=False),
            # Song titles, requesters and leaderboards contain mentions; none of them may ping anyone.
            allowed_mentions=discord.AllowedMentions.none(),
        )

        self.loop = None
        # Set first thing in close(): voice events and player callbacks then leave the saved state alone.
        self.shutting_down = False

        self.init_database()
        self.guilds_data = {}
        self.ws_manager = None
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)
        # Cache downloads hold a worker for the whole download + transcode (minutes),
        # so they get their own pool instead of starving extraction and DB queries.
        self.download_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        # Queries take milliseconds and must not queue behind multi-second extractions.
        self.db_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        # Bulk lookups (a Spotify playlist's tracks) run four at a time without taking playback's workers.
        self.import_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

        self.song_cache = {}
        # Spotify's Web API is skipped until this monotonic time after it refuses us (no Premium).
        self.spotify_api_paused_until = 0.0
        self.max_cache_size = MAX_CACHE_SIZE
        self.cache_ttl = CACHE_TTL

        # One queue writer per guild; _queue_dirty marks guilds with unsaved changes.
        self.db_save_tasks: dict[int, asyncio.Task] = {}
        self._queue_dirty: set[int] = set()
        self._queue_write_locks: dict[int, asyncio.Lock] = {}

        self._background_tasks: set[asyncio.Task] = set()
        self._voice_watchdogs: dict[int, asyncio.Task] = {}
        self._alone_timers: dict[int, asyncio.Task] = {}
        # (channel_id, message_id) of now-playing panels left by a crash, deleted once logged in
        self._leftover_panels: list[tuple[int, int]] = []
        # Called with a guild id whenever its playback state changes (the Activity broadcasts it)
        self._state_listeners: list[Callable[[int], object]] = []
        # Slash command name -> id, for clickable mentions (</play:id>); filled when commands sync
        self.command_ids: dict[str, int] = {}

        #  Last.fm rate-limiter state
        self._lastfm_call_times: list[float] = []
        self._lastfm_lock = None  # Created in setup_hook when the event loop is running
        self.lastfm_rate_limit = 5  # max calls per second

        # Base yt-dlp settings; build real option dicts with ytdl_options(), which adds logging and cookies.
        self.ytdl_format_options = {
            "format": "bestaudio/best",
            "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
            "restrictfilenames": True,
            "noplaylist": False,
            "extract_flat": True,
            "logtostderr": False,
            "quiet": True,
            "default_search": "auto",
            "source_address": "0.0.0.0",
            "age_limit": 18,
            "retries": 5,
            "fragment_retries": 5,
            "skip_unavailable_fragments": True,
            "keep_fragments": False,
            "concurrent_fragment_downloads": 5,
            "extractor_retries": 3,
            "file_access_retries": 3,
            "socket_timeout": 30,
            "http_chunk_size": 1048576,
            "geo_bypass": True,
            "prefer_free_formats": False,
            "playliststart": 1,
            # No player_client override — pinned client lists go stale and break
        }

        self.ffmpeg_options = {
            # -rw_timeout (µs) turns a stalled socket into a reconnect instead of a frozen player.
            "before_options": (
                "-reconnect 1 "
                "-reconnect_streamed 1 "
                "-reconnect_delay_max 30 "
                "-reconnect_on_network_error 1 "
                "-reconnect_on_http_error 5xx "
                "-rw_timeout 15000000 "
                "-probesize 1M "
                "-analyzeduration 1M "
                "-nostdin"
            ),
            "options": (
                "-vn "
                "-threads 1 "
                "-fflags +discardcorrupt+genpts "
                "-flags +low_delay"
            ),
        }

        self.voice_reconnect_enabled = True
        self.voice_reconnect_delay = 2

        self._http_session: aiohttp.ClientSession | None = None
        self._stream_session: aiohttp.ClientSession | None = None
        self.audio_cache = AudioCacheService(self)

        try:
            spotify_client_id = os.getenv("SPOTIFY_CLIENT_ID")
            spotify_client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")

            if spotify_client_id and spotify_client_secret:
                # No retries: a 429 would otherwise sleep for its Retry-After (up to hours) on an executor
                # worker, and the public-page fallback already covers failed calls.
                self.spotify = spotipy.Spotify(
                    client_credentials_manager=SpotifyClientCredentials(
                        client_id=spotify_client_id, client_secret=spotify_client_secret, requests_timeout=5,
                    ),
                    requests_timeout=5, retries=0, status_retries=0,
                )
                logger.info("Spotify integration enabled")
            else:
                self.spotify = None
                logger.info("Spotify credentials not found. Spotify features disabled.")
        except Exception as e:
            self.spotify = None
            logger.warning(f"Spotify setup failed: {e}")

        # Read-only Last.fm calls need only the key; Last.fm asks for an identifiable User-Agent.
        lastfm_api_key = os.getenv("LASTFM_API_KEY")
        if lastfm_api_key:
            pylast.HEADERS["User-Agent"] = LRCLIB_USER_AGENT
            self.lastfm = pylast.LastFMNetwork(
                api_key=lastfm_api_key,
                api_secret=os.getenv("LASTFM_API_SECRET") or "",
            )
            logger.info("Last.fm integration enabled")
        else:
            self.lastfm = None
            logger.info("Last.fm credentials not found. Last.fm features disabled.")

        # Shared service instances — avoids re-allocating on every call/task tick
        self.music = MusicService(self)
        self.playback = PlaybackService(self)

    # Shared clients

    def ytdl_options(self, **overrides) -> dict:
        """Fresh yt-dlp options: the bot's base settings, then overrides."""
        return ytdl_options(**{**self.ytdl_format_options, **overrides})

    @property
    def ytdl(self) -> yt_dlp.YoutubeDL:
        """A new YoutubeDL per access; instances are not thread-safe, so none is shared."""
        return yt_dlp.YoutubeDL(cast(Any, self.ytdl_options()))

    @property
    def http_session(self) -> aiohttp.ClientSession:
        """One shared session; pass timeout= and headers= on each request."""
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout())
        return self._http_session

    @property
    def stream_session(self) -> aiohttp.ClientSession:
        """The Activity's audio proxy: each stream holds a connection for a whole song, so it has its own pool
        (without a cap) and can't make API requests wait."""
        if self._stream_session is None or self._stream_session.closed:
            self._stream_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(), connector=aiohttp.TCPConnector(limit=0),
            )
        return self._stream_session

    def add_state_listener(self, listener: Callable[[int], object]) -> None:
        self._state_listeners.append(listener)

    def notify_state_changed(self, guild_id: int) -> None:
        """Tell listeners that a guild's queue or playback changed."""
        for listener in self._state_listeners:
            try:
                listener(guild_id)
            except Exception:
                logger.exception("State listener failed")

    def spawn(self, coro: Coroutine, *, name: str | None = None) -> asyncio.Task:
        """Run a coroutine in the background, keeping a reference to it and logging its failure."""
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_task_done)
        return task

    def _background_task_done(self, task: asyncio.Task) -> None:
        self._background_tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error(f"Background task {task.get_name()} failed", exc_info=error)

    # Database

    @staticmethod
    def init_database():
        conn = None
        try:
            Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            cursor = conn.cursor()

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS playlists
                (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER NOT NULL,
                    guild_id   INTEGER NOT NULL,
                    name       TEXT    NOT NULL,
                    songs      TEXT    NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS global_playlists
                (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER NOT NULL,
                    name       TEXT    NOT NULL,
                    songs      TEXT    NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (user_id, name)
                )
                """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_settings
                (
                    guild_id          INTEGER PRIMARY KEY,
                    auto_disconnect   INTEGER DEFAULT 300,
                    default_volume    INTEGER DEFAULT 100,
                    music_channel_id  INTEGER,
                    queue_data        TEXT,
                    dj_role_id        INTEGER
                )
                """
            )

            # Favorites table
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS favorites
                (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id   INTEGER NOT NULL,
                    song_data TEXT    NOT NULL,
                    song_url  TEXT    NOT NULL DEFAULT '',
                    added_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (user_id, song_url)
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_favorites_user ON favorites (user_id)"
            )

            # User stats table
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_stats
                (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id           INTEGER NOT NULL,
                    guild_id          INTEGER NOT NULL,
                    song_url          TEXT    NOT NULL,
                    song_title        TEXT    NOT NULL,
                    artist            TEXT    DEFAULT '',
                    duration_seconds  INTEGER DEFAULT 0,
                    played_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_stats_user ON user_stats (user_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_stats_guild ON user_stats (user_id, guild_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_stats_guild_id ON user_stats (guild_id)"
            )

            # Collaborative playlist members
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS playlist_collaborators
                (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    playlist_id INTEGER NOT NULL,
                    user_id     INTEGER NOT NULL,
                    is_global   INTEGER DEFAULT 0,
                    permission  TEXT    DEFAULT 'edit',
                    added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (playlist_id, user_id, is_global)
                )
                """
            )

            # Per-user Activity preferences (theme), shared across servers and devices
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_preferences
                (
                    user_id INTEGER PRIMARY KEY,
                    theme   TEXT
                )
                """
            )

            # Auto-cleanup collaborators when a playlist is deleted
            cursor.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_delete_playlist_collabs
                AFTER DELETE ON playlists
                BEGIN
                    DELETE FROM playlist_collaborators
                    WHERE playlist_id = OLD.id AND is_global = 0;
                END
                """
            )
            cursor.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_delete_global_playlist_collabs
                AFTER DELETE ON global_playlists
                BEGIN
                    DELETE FROM playlist_collaborators
                    WHERE playlist_id = OLD.id AND is_global = 1;
                END
                """
            )

            # Deduplicate before creating the index — safe migration for existing data.
            cursor.execute(
                "DELETE FROM playlists WHERE id NOT IN "
                "(SELECT MIN(id) FROM playlists GROUP BY user_id, guild_id, name)"
            )
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_playlists_unique "
                "ON playlists (user_id, guild_id, name)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_global_playlists_user_name "
                "ON global_playlists (user_id, name)"
            )

            # Schema version tracking
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version
                (
                    id      INTEGER PRIMARY KEY CHECK (id = 1),
                    version INTEGER NOT NULL
                )
                """
            )
            cursor.execute(
                "INSERT OR IGNORE INTO schema_version (id, version) VALUES (1, ?)",
                (DB_VERSION,),
            )

            # Run migrations if needed
            cursor.execute("SELECT version FROM schema_version WHERE id = 1")
            row = cursor.fetchone()
            current_version = row[0] if row else 1

            if current_version < DB_VERSION:
                MusicBot._run_migrations(cursor, current_version, DB_VERSION)
                cursor.execute(
                    "UPDATE schema_version SET version = ? WHERE id = 1",
                    (DB_VERSION,),
                )

            MusicBot._rename_slashed_playlists(cursor)
            conn.commit()
            logger.info("Database initialized successfully (WAL mode, schema v%d)", DB_VERSION)

        except Exception:
            logger.exception("Database initialization failed")
            raise
        finally:
            if conn:
                conn.close()

    @staticmethod
    def _run_migrations(cursor, from_version: int, to_version: int):
        """Run sequential schema migrations."""
        migrations = {
            # Existing collaborators keep full access; the column only lets an
            # owner narrow it from here on.
            5: [
                "ALTER TABLE playlist_collaborators ADD COLUMN permission TEXT DEFAULT 'edit'",
            ],
        }

        for version in range(from_version + 1, to_version + 1):
            stmts = migrations.get(version, [])
            for stmt in stmts:
                try:
                    cursor.execute(stmt)
                    logger.info("Migration v%d applied: %s", version, stmt[:80])
                except sqlite3.OperationalError as e:
                    if "duplicate column" in str(e).lower():
                        logger.debug("Column already exists, skipping: %s", stmt[:80])
                    else:
                        raise

    @staticmethod
    def _rename_slashed_playlists(cursor) -> None:
        """Playlist names travel in Activity URLs, where "/" can't be addressed: give such names a "-"."""
        for tbl, scoped in (("playlists", True), ("global_playlists", False)):
            scope = "guild_id" if scoped else "0"
            rows = cursor.execute(f"SELECT id, user_id, {scope}, name FROM {tbl} WHERE name LIKE '%/%'").fetchall()
            for pid, user_id, scope_id, name in rows:
                base = name.replace("/", "-")
                candidate, n = base, 2
                while cursor.execute(
                        f"SELECT 1 FROM {tbl} WHERE user_id = ? AND {scope} = ? AND name = ?",
                        (user_id, scope_id, candidate),
                ).fetchone():
                    candidate, n = f"{base} ({n})", n + 1
                cursor.execute(f"UPDATE {tbl} SET name = ? WHERE id = ?", (candidate, pid))
                logger.info(f"Renamed playlist {name!r} to {candidate!r} (names can't contain /)")

    @staticmethod
    def get_db_connection():
        conn = getattr(_db_local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            # Safe with WAL: a crash never corrupts the database, and commits skip a per-write fsync.
            conn.execute("PRAGMA synchronous=NORMAL")
            # Recommended for long-lived connections: analyze tables that need it, with a time limit.
            conn.execute("PRAGMA optimize=0x10002")
            _db_local.conn = conn
        return conn

    async def execute_db_query(self, query: str, params: tuple | None = None):
        def _execute():
            with self.get_db_connection() as conn:
                cursor = conn.cursor()
                if params:
                    cursor.execute(query, params)
                else:
                    cursor.execute(query)
                conn.commit()
                return cursor.fetchall()

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.db_executor, _execute)

    async def fetch_db_query(self, query: str, params: tuple | None = None):
        def _fetch():
            with self.get_db_connection() as conn:
                cursor = conn.cursor()
                if params:
                    cursor.execute(query, params)
                else:
                    cursor.execute(query)
                return cursor.fetchall()

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.db_executor, _fetch)

    async def get_user_stats(self, user_id: int, guild_id: int, top: int = 5) -> dict:
        """A user's listening totals in a guild, with their most played songs (by URL) and artists."""
        totals = await self.fetch_db_query(
            "SELECT COUNT(*), COALESCE(SUM(duration_seconds), 0) FROM user_stats WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        )
        top_songs = await self.fetch_db_query(
            "SELECT MAX(song_title), song_url, COUNT(*) AS cnt FROM user_stats "
            "WHERE user_id = ? AND guild_id = ? GROUP BY song_url ORDER BY cnt DESC LIMIT ?",
            (user_id, guild_id, top),
        )
        top_artists = await self.fetch_db_query(
            "SELECT artist, COUNT(*) AS cnt FROM user_stats "
            "WHERE user_id = ? AND guild_id = ? AND artist != '' GROUP BY artist ORDER BY cnt DESC LIMIT ?",
            (user_id, guild_id, top),
        )
        return {
            "play_count": totals[0][0] if totals else 0,
            "total_seconds": totals[0][1] if totals else 0,
            "top_songs": [{"title": title, "url": url, "plays": plays} for title, url, plays in top_songs],
            "top_artists": [{"name": name, "plays": plays} for name, plays in top_artists],
        }

    async def get_leaderboard(self, guild_id: int, limit: int = 10) -> list[tuple[int, int, int]]:
        """(user_id, plays, seconds listened) of a guild's top listeners."""
        rows = await self.fetch_db_query(
            "SELECT user_id, COUNT(*), COALESCE(SUM(duration_seconds), 0) AS total FROM user_stats "
            "WHERE guild_id = ? GROUP BY user_id ORDER BY total DESC LIMIT ?",
            (guild_id, limit),
        )
        return [tuple(row) for row in rows]

    async def delete_user_data(self, user_id: int) -> dict[str, int]:
        """Delete everything stored about a user (stats, favorites, playlists, shares, preferences).

        Returns the number of rows removed per table. Deleting a playlist also removes its collaborators.
        """
        statements = {
            "user_stats": "DELETE FROM user_stats WHERE user_id = ?",
            "favorites": "DELETE FROM favorites WHERE user_id = ?",
            "playlists": "DELETE FROM playlists WHERE user_id = ?",
            "global_playlists": "DELETE FROM global_playlists WHERE user_id = ?",
            "playlist_collaborators": "DELETE FROM playlist_collaborators WHERE user_id = ?",
            "user_preferences": "DELETE FROM user_preferences WHERE user_id = ?",
        }

        def _delete():
            with self.get_db_connection() as conn:
                return {table: conn.execute(sql, (user_id,)).rowcount for table, sql in statements.items()}

        return await asyncio.get_running_loop().run_in_executor(self.db_executor, _delete)

    # Guild state

    def get_guild_data(self, guild_id: int) -> GuildData:
        if guild_id not in self.guilds_data:
            self.guilds_data[guild_id] = {
                "guild_id": guild_id,
                "queue": [],
                "played": [],
                "history": [],
                "current": None,
                "seek_offset": 0,
                "loop_mode": "off",
                "shuffle": False,
                "volume": 100,
                "autoplay": False,
                "autoplay_prefetch": None,
                "autoplay_prefetch_task": None,
                "voice_client": None,
                "intentional_disconnect": False,
                "last_activity": time.monotonic(),
                "now_playing_message": None,
                "music_channel_id": None,
                "start_time": None,
                "message_ready_for_timestamps": False,
                "seeking": False,
                "pause_position": None,
                "now_playing_message_sent_time": None,
                "play_lock": asyncio.Lock(),
                "speed": 1.0,
                "audio_effect": "none",
                "dj_role_id": None,
            }
        return self.guilds_data[guild_id]

    @staticmethod
    def cancel_autoplay_prefetch(guild_data: GuildData) -> None:
        """Drop a pending autoplay pick (it was chosen for a song that is no longer current)."""
        prefetch_task = guild_data.get("autoplay_prefetch_task")
        if prefetch_task and not prefetch_task.done():
            prefetch_task.cancel()
        guild_data["autoplay_prefetch"] = None
        guild_data["autoplay_prefetch_task"] = None

    def cancel_autoplay(self, guild_data: GuildData):
        """Turn autoplay off (stop, leave, a voice connection lost for good)."""
        guild_data["autoplay"] = False
        self.cancel_autoplay_prefetch(guild_data)

    # Guild settings persistence

    async def save_guild_music_channel(self, guild_id: int, channel_id: int | None):
        try:
            await self.execute_db_query(
                """
                INSERT INTO guild_settings (guild_id, music_channel_id)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET music_channel_id = excluded.music_channel_id
            """,
                (guild_id, channel_id),
            )
        except sqlite3.Error as e:
            logger.error(f"Failed to save music channel: {e}")

    async def save_guild_dj_role(self, guild_id: int, role_id: int | None):
        try:
            await self.execute_db_query(
                """
                INSERT INTO guild_settings (guild_id, dj_role_id)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET dj_role_id = excluded.dj_role_id
            """,
                (guild_id, role_id),
            )
        except sqlite3.Error as e:
            logger.error(f"Failed to save DJ role: {e}")

    # User stats tracking

    async def record_listening_stat(
            self,
            user_id: int,
            guild_id: int,
            song: Song,
            duration_listened: int,
    ):
        """Record that a user listened to a song."""
        if duration_listened <= 0:
            return
        try:
            artist = song.uploader or ""
            await self.execute_db_query(
                """
                INSERT INTO user_stats (user_id, guild_id, song_url, song_title, artist, duration_seconds)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, guild_id, song.webpage_url, song.title, artist, duration_listened),
            )
        except sqlite3.Error as e:
            logger.debug(f"Failed to record listening stat: {e}")

    # Favorites

    async def add_favorite(self, user_id: int, song: Song) -> bool:
        """Add a song to the user's favorites. Returns False if it is already there.

        Raises FavoritesFull at MAX_FAVORITES, and sqlite3.Error when the database fails.
        """
        try:
            rows = await self.fetch_db_query(
                "SELECT song_url = ? FROM favorites WHERE user_id = ?", (song.webpage_url, user_id),
            )
            if any(row[0] for row in rows):
                return False
            if len(rows) >= MAX_FAVORITES:
                raise FavoritesFull(f"Favorites are full ({MAX_FAVORITES} songs)")

            song_json = json.dumps(song.to_dict(), sort_keys=True)
            await self.execute_db_query(
                "INSERT INTO favorites (user_id, song_data, song_url) VALUES (?, ?, ?)",
                (user_id, song_json, song.webpage_url),
            )
            return True
        except sqlite3.Error:
            logger.exception("Failed to add favorite")
            raise

    async def remove_favorite(self, user_id: int, position: int) -> bool:
        """Remove favorite by 1-based position. Returns True on success."""
        try:
            rows = await self.fetch_db_query(
                "SELECT id FROM favorites WHERE user_id = ? ORDER BY added_at ASC",
                (user_id,),
            )
            if not rows or position < 1 or position > len(rows):
                return False
            fav_id = rows[position - 1][0]
            await self.execute_db_query("DELETE FROM favorites WHERE id = ?", (fav_id,))
            return True
        except sqlite3.Error as e:
            logger.error(f"Failed to remove favorite: {e}")
            return False

    async def remove_favorite_by_url(self, user_id: int, url: str) -> tuple[bool, str | None]:
        """Remove the favorite matching a song URL. Returns (removed, title).

        Deletes by row id rather than list position: a position resolved from an
        earlier read can point at a different row by the time the delete runs.
        """
        try:
            rows = await self.fetch_db_query(
                "SELECT id, song_data FROM favorites WHERE user_id = ? ORDER BY added_at ASC",
                (user_id,),
            )
            for fav_id, song_json in rows:
                data = json.loads(song_json)
                if (data.get("webpage_url") or data.get("url")) == url:
                    await self.execute_db_query("DELETE FROM favorites WHERE id = ?", (fav_id,))
                    return True, data.get("title")
            return False, None
        except (sqlite3.Error, ValueError) as e:
            logger.error(f"Failed to remove favorite: {e}")
            return False, None

    async def get_user_theme(self, user_id: int) -> str | None:
        rows = await self.fetch_db_query("SELECT theme FROM user_preferences WHERE user_id = ?", (user_id,))
        return rows[0][0] if rows else None

    async def set_user_theme(self, user_id: int, theme: str) -> None:
        await self.execute_db_query(
            "INSERT INTO user_preferences (user_id, theme) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET theme = excluded.theme",
            (user_id, theme),
        )

    async def get_favorites(self, user_id: int) -> list[dict]:
        try:
            rows = await self.fetch_db_query(
                "SELECT song_data FROM favorites WHERE user_id = ? ORDER BY added_at ASC",
                (user_id,),
            )
            return [json.loads(row[0]) for row in rows]
        except (sqlite3.Error, ValueError) as e:
            logger.error(f"Failed to get favorites: {e}")
            return []

    # Collaborative playlist helpers

    async def get_playlist_id(self, user_id: int, name: str, guild_id: int | None = None) -> int | None:
        """Return the playlist row id, or None if not found."""
        if guild_id is not None:
            rows = await self.fetch_db_query(
                "SELECT id FROM playlists WHERE user_id = ? AND guild_id = ? AND name = ?",
                (user_id, guild_id, name),
            )
        else:
            rows = await self.fetch_db_query(
                "SELECT id FROM global_playlists WHERE user_id = ? AND name = ?",
                (user_id, name),
            )
        return rows[0][0] if rows else None

    async def add_collaborator(
        self, playlist_id: int, user_id: int, is_global: bool = False, permission: str = "edit"
    ):
        await self.execute_db_query(
            "INSERT OR IGNORE INTO playlist_collaborators "
            "(playlist_id, user_id, is_global, permission) VALUES (?, ?, ?, ?)",
            (playlist_id, user_id, 1 if is_global else 0, permission),
        )

    async def set_collaborator_permission(
        self, playlist_id: int, user_id: int, permission: str, is_global: bool = False
    ) -> None:
        await self.execute_db_query(
            "UPDATE playlist_collaborators SET permission = ? "
            "WHERE playlist_id = ? AND user_id = ? AND is_global = ?",
            (permission, playlist_id, user_id, 1 if is_global else 0),
        )

    async def get_collaborator_permission(
        self, playlist_id: int, user_id: int, is_global: bool = False
    ) -> str | None:
        """Permission level for a collaborator, or None when they are not one."""
        rows = await self.fetch_db_query(
            "SELECT permission FROM playlist_collaborators "
            "WHERE playlist_id = ? AND user_id = ? AND is_global = ?",
            (playlist_id, user_id, 1 if is_global else 0),
        )
        if not rows:
            return None
        return rows[0][0] or "edit"

    async def remove_collaborator(self, playlist_id: int, user_id: int, is_global: bool = False):
        await self.execute_db_query(
            "DELETE FROM playlist_collaborators WHERE playlist_id = ? AND user_id = ? AND is_global = ?",
            (playlist_id, user_id, 1 if is_global else 0),
        )

    async def get_collaborators(self, playlist_id: int, is_global: bool = False) -> list[tuple[int, str]]:
        """(user_id, permission) for each collaborator."""
        rows = await self.fetch_db_query(
            "SELECT user_id, permission FROM playlist_collaborators "
            "WHERE playlist_id = ? AND is_global = ?",
            (playlist_id, 1 if is_global else 0),
        )
        return [(row[0], row[1] or "edit") for row in rows]

    async def is_collaborator(self, playlist_id: int, user_id: int, is_global: bool = False) -> bool:
        rows = await self.fetch_db_query(
            "SELECT 1 FROM playlist_collaborators WHERE playlist_id = ? AND user_id = ? AND is_global = ?",
            (playlist_id, user_id, 1 if is_global else 0),
        )
        return bool(rows)

    # Lifecycle hooks

    @property
    def _task_loops(self) -> list[tasks.Loop]:
        return [
            self.cleanup_inactive, self.cleanup_cache, self.cleanup_inactive_guilds,
            self.update_now_playing_timestamps, self.check_voice_health, self.maintain_database,
        ]

    async def setup_hook(self):
        # Set event loop reference early so ConnectionState has it
        # before _delay_ready runs (needed when uvicorn owns the loop)
        self.loop = asyncio.get_running_loop()
        self._connection.loop = self.loop
        if self._lastfm_lock is None:
            self._lastfm_lock = asyncio.Lock()

        # One-time startup work lives here: on_ready fires again after every gateway re-identify, and
        # running it there would reload the saved queues over live ones and delete in-flight downloads.
        await self.audio_cache.startup_cleanup()
        await self.load_persistent_queues()
        try:
            synced = await self._sync_commands_with_entry_point()
            self.command_ids = {command.name: command.id for command in synced}
            logger.info(f"Synced {len(synced)} slash command(s)")
        except Exception:
            # A failed sync leaves the previous commands in place; it must not stop the bot from starting.
            logger.exception("Failed to sync commands")
        if self._leftover_panels:
            self.spawn(self._delete_leftover_panels())

    async def _sync_commands_with_entry_point(self):
        """Sync slash commands while preserving the Activity Entry Point command.

        Discord's bulk PUT rejects requests that omit the Entry Point (type 4)
        command created when Activities are enabled (error 50240). This fetches
        existing commands, finds the Entry Point, and includes it in the payload.
        """
        app_id = self.application_id
        if app_id is None:
            raise RuntimeError("application id is unknown before login")
        commands = self.tree._get_all_commands(guild=None)

        translator = self.tree.translator
        if translator:
            payload = [await cmd.get_translated_payload(self.tree, translator) for cmd in commands]
        else:
            payload = [cmd.to_dict(self.tree) for cmd in commands]

        existing = await self.http.get_global_commands(app_id)
        for cmd in existing:
            if cmd.get("type") == 4:
                payload.append(cmd)

        try:
            data = await self.http.bulk_upsert_global_commands(app_id, payload=payload)
        except discord.HTTPException as e:
            if e.status == 400 and e.code == 50035:
                raise CommandSyncFailure(e, commands) from None
            raise
        return [AppCommand(data=d, state=self._connection) for d in data]

    async def _delete_leftover_panels(self):
        """Delete now-playing panels a crash left behind: their buttons stopped working with the old process."""
        panels, self._leftover_panels = self._leftover_panels, []
        for channel_id, message_id in panels:
            with contextlib.suppress(discord.HTTPException):
                await self.http.delete_message(channel_id, message_id)

    async def on_ready(self):
        logger.info(f"{self.user} has connected to Discord!")
        logger.info(f"Connected to {len(self.guilds)} guilds")

        self.loop = asyncio.get_running_loop()

        # Create asyncio.Lock now that the event loop is running
        if self._lastfm_lock is None:
            self._lastfm_lock = asyncio.Lock()

        # Scale thread pool based on guild count
        desired_workers = max(3, min(len(self.guilds), 10))
        if self.executor._max_workers < desired_workers:
            self.executor._max_workers = desired_workers
            logger.info(f"ThreadPoolExecutor scaled to {desired_workers} workers")

        for task in self._task_loops:
            if not task.is_running():
                task.start()

    # Voice state handling

    @staticmethod
    def _voice_client_of(guild) -> discord.VoiceClient | None:
        return cast(discord.VoiceClient | None, guild.voice_client if guild else None)

    def voice_reconnecting(self, guild_id: int) -> bool:
        """True while the bot is waiting for (or making) a voice reconnect in this guild."""
        task = self._voice_watchdogs.get(guild_id)
        return task is not None and not task.done()

    async def on_voice_state_update(self, member, before, after):
        if self.shutting_down or self.user is None:
            return
        guild = self.get_guild(member.guild.id)
        if guild is None:
            return

        if member.id != self.user.id:
            if before.channel != after.channel:
                await self._check_alone(guild.id)
            return

        guild_data = self.get_guild_data(guild.id)
        if before.channel == after.channel:
            await self._on_self_voice_flags(guild, before, after)
        elif after.channel is not None:
            voice_client = self._voice_client_of(guild)
            if voice_client is not None:
                guild_data["voice_client"] = voice_client
            if before.channel is None:
                await self._on_voice_joined(guild, after.channel)
            else:
                await self._on_voice_moved(guild, before.channel, after.channel)
        else:
            await self._on_voice_left(guild, before.channel)

    async def _on_voice_joined(self, guild, channel):
        guild_data = self.get_guild_data(guild.id)
        if self.voice_reconnecting(guild.id):
            return  # discord.py's own reconnect: the watchdog picks playback up again
        guild_data["intentional_disconnect"] = False
        guild_data.pop("voice_lost_at", None)
        if isinstance(channel, discord.StageChannel):
            await self._try_unsuppress(guild)
        await self._check_alone(guild.id)
        # A join with songs waiting (a queue restored after a restart, or kept on leaving) starts them.
        if guild_data.get("queue") or guild_data.get("interrupted"):
            self.spawn(self._start_when_connected(guild.id))

    async def _start_when_connected(self, guild_id: int):
        voice_client = self.get_guild_data(guild_id).get("voice_client")
        if voice_client and await self.playback._wait_for_voice(voice_client, timeout=15):
            await self.playback.resume_after_reconnect(guild_id)

    async def _on_voice_moved(self, guild, before_channel, after_channel):
        guild_data = self.get_guild_data(guild.id)
        playback = self.playback
        # People left behind keep the listening time they heard.
        playback.credit_listeners(guild.id, before_channel)
        if isinstance(after_channel, discord.StageChannel):
            await self._try_unsuppress(guild)

        self_moved = time.monotonic() - guild_data.get("self_move_at", 0.0) < SELF_MOVE_WINDOW
        afk = guild.afk_channel
        if afk is not None and after_channel.id == afk.id:
            await playback.notify(
                guild.id, "Moved to AFK",
                "Nobody can hear me in the AFK channel, so playback is paused. Move me back to continue.",
                delete_after=60,
            )
        elif not self_moved and guild_data.get("current"):
            await playback.notify(guild.id, "Moved", f"Now playing in {after_channel.mention}.", delete_after=15)
        await self._check_alone(guild.id)

    async def _on_self_voice_flags(self, guild, before, after):
        """Server mute or Stage suppress: pause rather than play into silence, and resume when lifted."""
        silenced = bool(after.mute or after.suppress)
        if silenced == bool(before.mute or before.suppress):
            return
        playback = self.playback
        if silenced:
            if playback.pause_for(guild.id, "muted"):
                what = "moved to the audience" if after.suppress else "server-muted"
                await playback.notify(guild.id, "Paused", f"I was {what}, so playback is paused.", delete_after=30)
        elif playback.resume_for(guild.id, "muted"):
            await playback.notify(guild.id, "Resumed", "I can be heard again, so playback resumed.", delete_after=15)

    async def _on_voice_left(self, guild, channel):
        guild_data = self.get_guild_data(guild.id)
        self._cancel_alone_timer(guild.id)
        now = time.monotonic()
        guild_data["voice_lost_at"] = now

        if guild_data.get("intentional_disconnect", False):
            logger.info(f"Intentional disconnect for guild {guild.id}, skipping reconnect")
            guild_data["intentional_disconnect"] = False
            guild_data["voice_client"] = None
            await self.playback.clear_now_playing(guild.id)
            self.playback.clear_voice_status(guild.id)
            return

        logger.warning(f"Bot disconnected from voice in guild {guild.id}")
        # Right after a healthy connection, the drop came from Discord: a moderator or a deleted channel.
        guild_data["voice_left_external"] = (
            now - guild_data.get("voice_ok_at", 0.0) < VOICE_OK_WINDOW or guild.get_channel(channel.id) is None
        )
        self.watch_voice(guild.id, channel)

    def watch_voice(self, guild_id: int, channel=None) -> None:
        """Watch a dropped voice connection: let discord.py reconnect, rejoin only if it gives up."""
        if self.shutting_down or self.voice_reconnecting(guild_id):
            return
        guild_data = self.get_guild_data(guild_id)
        channel = channel or getattr(guild_data.get("voice_client"), "channel", None)
        if channel is None:
            return
        self._voice_watchdogs[guild_id] = self.spawn(self._voice_watchdog(guild_id, channel))

    async def _voice_watchdog(self, guild_id: int, channel):
        guild_data = self.get_guild_data(guild_id)
        try:
            await asyncio.sleep(self.voice_reconnect_delay)
            deadline = time.monotonic() + VOICE_RECONNECT_TIMEOUT
            while True:
                if self.shutting_down or guild_data.get("intentional_disconnect"):
                    return
                guild = self.get_guild(guild_id)
                if guild is None:
                    return
                voice_client = self._voice_client_of(guild)
                if voice_client is not None and voice_client.is_connected():
                    logger.info(f"Voice reconnected in guild {guild_id}")
                    guild_data["voice_client"] = voice_client
                    await self.playback.resume_after_reconnect(guild_id)
                    return
                if voice_client is None:
                    break
                if time.monotonic() >= deadline:
                    if _reconnect_running(voice_client):
                        # Still retrying with backoff: its eventual rejoin restarts playback like a /join.
                        logger.warning(f"discord.py is still reconnecting voice in guild {guild_id}")
                        return
                    logger.warning(f"Voice reconnect stalled in guild {guild_id}, reconnecting")
                    guild_data["voice_left_external"] = False
                    with contextlib.suppress(discord.HTTPException, discord.ClientException, OSError):
                        await voice_client.disconnect(force=True)
                    break
                await asyncio.sleep(1)

            target = guild.get_channel(channel.id)
            if guild_data.get("voice_left_external") or target is None or not self.voice_reconnect_enabled:
                logger.info(f"Disconnected from voice in guild {guild_id} by Discord; not rejoining")
                await self._cleanup_after_failed_reconnect(guild_id)
                return
            await self._rejoin_voice(guild_id, target)
        finally:
            if self._voice_watchdogs.get(guild_id) is asyncio.current_task():
                del self._voice_watchdogs[guild_id]

    async def _rejoin_voice(self, guild_id: int, channel):
        guild_data = self.get_guild_data(guild_id)
        guild = channel.guild
        if not channel.permissions_for(guild.me).connect:
            await self._cleanup_after_failed_reconnect(guild_id)
            return
        logger.info(f"Rejoining voice in guild {guild_id}")
        try:
            voice_client = await channel.connect(timeout=30.0, reconnect=True)
        except discord.ClientException:
            voice_client = self._voice_client_of(guild)  # someone (/join) connected meanwhile
        except (TimeoutError, discord.HTTPException, aiohttp.ClientError, OSError) as e:
            logger.error(f"Failed to reconnect voice for guild {guild_id}: {e}")
            voice_client = None
        if voice_client is None:
            await self._cleanup_after_failed_reconnect(guild_id)
            return
        guild_data["voice_client"] = voice_client
        if await self.playback._wait_for_voice(voice_client, timeout=15):
            await self.playback.resume_after_reconnect(guild_id)

    async def _cleanup_after_failed_reconnect(self, guild_id: int):
        """Voice is gone for good: keep the queue (the current song back in front) and say how to continue."""
        guild_data = self.get_guild_data(guild_id)
        playback = self.playback

        logger.info(f"Cleaning up after failed reconnect for guild {guild_id}")
        cancel_imports(guild_data)
        self.cancel_autoplay(guild_data)
        playback.bump_generation(guild_data)
        playback.park_current(guild_id)
        guild_data["voice_client"] = None
        await playback.clear_now_playing(guild_id)
        playback.clear_voice_status(guild_id)
        await self.save_guild_queue(guild_id)

        text = "The bot was disconnected from voice."
        if guild_data["queue"]:
            text += " The queue is kept: use `/join` to pick up where it stopped."
        else:
            text += " Use `/play` to start again."
        await playback.notify(guild_id, "Voice Connection Lost", text, delete_after=30)

    @staticmethod
    async def _try_unsuppress(guild):
        """On a Stage the bot joins as audience: become a speaker, or ask to speak."""
        me = guild.me
        if not me.voice or not me.voice.suppress:
            return
        try:
            await me.edit(suppress=False)
        except discord.Forbidden:
            with contextlib.suppress(discord.HTTPException):
                await me.request_to_speak()
        except discord.HTTPException as e:
            logger.debug(f"Could not become a Stage speaker in guild {guild.id}: {e}")

    # Alone in the channel

    def _has_audience(self, guild, channel) -> bool:
        afk = guild.afk_channel
        if afk is not None and channel.id == afk.id:
            return False
        if any(not m.bot for m in channel.members):
            return True
        return bool(self.ws_manager and self.ws_manager.has_connections(guild.id))

    async def _check_alone(self, guild_id: int):
        """Pause when nobody is left to listen, leave after ALONE_TIMEOUT_SECONDS, resume when someone is back."""
        if ALONE_TIMEOUT_SECONDS <= 0:
            return
        guild = self.get_guild(guild_id)
        voice_client = self._voice_client_of(guild)
        if guild is None or voice_client is None or not voice_client.is_connected():
            return
        if self._has_audience(guild, voice_client.channel):
            self._cancel_alone_timer(guild_id)
            self.playback.resume_for(guild_id, "alone")
            return
        self.playback.pause_for(guild_id, "alone")
        timer = self._alone_timers.get(guild_id)
        if timer is None or timer.done():
            self._alone_timers[guild_id] = self.spawn(self._leave_when_alone(guild_id))

    async def _leave_when_alone(self, guild_id: int):
        try:
            await asyncio.sleep(ALONE_TIMEOUT_SECONDS)
            guild = self.get_guild(guild_id)
            voice_client = self._voice_client_of(guild)
            if guild is None or voice_client is None or self._has_audience(guild, voice_client.channel):
                return
            logger.info(f"Leaving voice in guild {guild_id}: nobody is listening")
            await self.playback.leave(
                guild_id, clear_queue=False,
                notice=f"Left {voice_client.channel.mention} because nobody was listening. "
                       "The queue is kept: use `/join` to continue.",
            )
        finally:
            if self._alone_timers.get(guild_id) is asyncio.current_task():
                del self._alone_timers[guild_id]

    def _cancel_alone_timer(self, guild_id: int) -> None:
        timer = self._alone_timers.pop(guild_id, None)
        if timer and not timer.done():
            timer.cancel()

    async def on_guild_role_delete(self, role):
        """A deleted DJ role would otherwise lock every non-admin out of the controls."""
        guild_data = self.guilds_data.get(role.guild.id)
        if guild_data is not None and guild_data.get("dj_role_id") == role.id:
            guild_data["dj_role_id"] = None
            await self.save_guild_dj_role(role.guild.id, None)
            logger.info(f"DJ role deleted in guild {role.guild.id}; controls are open to everyone again")

    async def on_member_remove(self, member):
        """Kicked, banned or left: their open Activity stops receiving the server's state."""
        close_user = getattr(self.ws_manager, "close_user", None)
        if close_user is not None:
            await close_user(member.guild.id, member.id, code=4003, reason="You are not a member of this server")

    async def on_guild_remove(self, guild):
        """The bot was kicked or left: stop everything for that guild and drop its saved queue."""
        logger.info(f"Removed from guild {guild.name} ({guild.id})")
        self._cancel_alone_timer(guild.id)
        watchdog = self._voice_watchdogs.pop(guild.id, None)
        if watchdog and not watchdog.done():
            watchdog.cancel()
        save_task = self.db_save_tasks.pop(guild.id, None)
        if save_task and not save_task.done():
            save_task.cancel()
        self._queue_dirty.discard(guild.id)

        guild_data = self.guilds_data.pop(guild.id, None)
        if guild_data is not None:
            cancel_imports(guild_data)
            self.cancel_autoplay(guild_data)
            self.playback.bump_generation(guild_data)
            task = self.playback._np_tasks.pop(guild.id, None)
            if task and not task.done():
                task.cancel()
            voice_client = guild_data.get("voice_client")
            if voice_client is not None:
                guild_data["intentional_disconnect"] = True
                with contextlib.suppress(discord.HTTPException, discord.ClientException, OSError):
                    await asyncio.wait_for(voice_client.disconnect(force=True), timeout=10)

        close_guild = getattr(self.ws_manager, "close_guild", None)
        if close_guild is not None:
            await close_guild(guild.id)
        try:
            await self.execute_db_query("UPDATE guild_settings SET queue_data = NULL WHERE guild_id = ?", (guild.id,))
        except sqlite3.Error as e:
            logger.error(f"Failed to clear the saved queue of guild {guild.id}: {e}")

    # Background tasks

    @tasks.loop(minutes=5)
    async def cleanup_inactive(self):
        try:
            for guild_id, data in list(self.guilds_data.items()):
                if data["voice_client"] and data["voice_client"].is_connected():
                    inactive_seconds = time.monotonic() - data["last_activity"]

                    is_truly_inactive = (
                            not data["voice_client"].is_playing()
                            and not data["voice_client"].is_paused()
                            and not data.get("current")
                    )

                    if inactive_seconds > INACTIVE_TIMEOUT_MINUTES * 60 and is_truly_inactive:
                        guild = self.get_guild(guild_id)
                        guild_name = guild.name if guild else "unknown"
                        await self.playback.leave(guild_id, clear_queue=False)
                        logger.info(f"Disconnected from inactive guild: {guild_name} ({guild_id})")
        except Exception:
            logger.exception("Cleanup task error")

    @tasks.loop(minutes=10)
    async def cleanup_cache(self):
        current_time = asyncio.get_running_loop().time()
        expired_keys = [
            key
            for key, value in self.song_cache.items()
            if current_time - value["cached_at"] > self.cache_ttl
        ]

        for key in expired_keys:
            del self.song_cache[key]

        if expired_keys:
            logger.info(f"Cleaned {len(expired_keys)} expired cache entries")

    @tasks.loop(hours=1)
    async def cleanup_inactive_guilds(self):
        if not self.is_ready():
            return
        current_guild_ids = {guild.id for guild in self.guilds}
        inactive_guilds = [guild_id for guild_id in self.guilds_data if guild_id not in current_guild_ids]

        for guild_id in inactive_guilds:
            cancel_imports(self.guilds_data.pop(guild_id))

        if inactive_guilds:
            logger.info(f"Cleaned up data for {len(inactive_guilds)} inactive guilds")

    @tasks.loop(seconds=1)
    async def update_now_playing_timestamps(self):
        now = time.monotonic()
        for guild_data in list(self.guilds_data.values()):
            voice_client = guild_data.get("voice_client")
            if voice_client is not None and voice_client.is_connected():
                guild_data["voice_ok_at"] = now
        await self.playback.update_timestamps_task()

    @tasks.loop(seconds=30)
    async def check_voice_health(self):
        try:
            for guild_id, guild_data in list(self.guilds_data.items()):
                voice_client = guild_data.get("voice_client")

                if not voice_client:
                    continue

                guild = self.get_guild(guild_id)
                guild_name = guild.name if guild else "unknown"

                if voice_client.is_connected():
                    await self._check_alone(guild_id)
                    await self._check_stalled(guild_id, guild_name)
                else:
                    # Check discord.py's registry before nulling — could be mid-reconnect
                    actual_vc = self._voice_client_of(guild)
                    if actual_vc:
                        guild_data["voice_client"] = actual_vc
                        if guild_data.get("current") or guild_data.get("queue"):
                            self.watch_voice(guild_id)
                    elif not self.voice_reconnecting(guild_id) and (guild_data.get("current") or guild_data.get("queue")):
                        logger.warning(
                            f"Voice client disconnected but has active state in guild: {guild_name} ({guild_id})")
                        guild_data["voice_client"] = None
                        if guild_data.get("current"):
                            self.playback.park_current(guild_id)
                            await self.save_guild_queue(guild_id)

        except Exception:
            logger.exception("Voice health check error")

    async def _check_stalled(self, guild_id: int, guild_name: str):
        """A song that is current but not playing on two checks in a row is restarted where it stopped."""
        guild_data = self.get_guild_data(guild_id)
        voice_client = guild_data["voice_client"]
        song = guild_data.get("current")
        stalled = (
            song is not None
            and voice_client is not None
            and not voice_client.is_playing()
            and not voice_client.is_paused()
            and not guild_data.get("seeking", False)
            and not guild_data["play_lock"].locked()
            and not guild_data.get("interrupted")
        )
        if not stalled or not guild_data.get("stall_seen"):
            guild_data["stall_seen"] = stalled
            return
        guild_data["stall_seen"] = False
        logger.warning(f"Detected stalled playback in guild: {guild_name} ({guild_id})")

        playback = self.playback
        position = playback.clock_position(guild_data)
        if song.duration and not song.is_live and position < song.duration - 10:
            try:
                async with guild_data["play_lock"]:
                    await playback._replay_current(guild_id, position)
                return
            except PlaybackError as e:
                logger.warning(f"Could not restart stalled playback in guild {guild_id}: {e}")
        playback._finish_song(guild_id, song)
        await playback.play_next(guild_id)

    @tasks.loop(hours=24)
    async def maintain_database(self):
        """Daily: refresh query statistics, prune old listening stats, and back up the database."""
        try:
            await self.execute_db_query("PRAGMA optimize")
            if STATS_RETENTION_DAYS > 0:
                await self.execute_db_query(
                    "DELETE FROM user_stats WHERE played_at < datetime('now', ?)", (f"-{STATS_RETENTION_DAYS} days",),
                )
            if DB_BACKUP_DIR:
                await self._backup_database()
        except (sqlite3.Error, OSError):
            logger.exception("Database maintenance failed")

    async def _backup_database(self):
        backup_dir = Path(DB_BACKUP_DIR)
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d-%H%M%S")
        target = backup_dir / f"music_bot-{stamp}.db"
        # VACUUM INTO writes a consistent copy of a live WAL database.
        await self.execute_db_query("VACUUM INTO ?", (str(target),))
        backups = sorted(backup_dir.glob("music_bot-*.db"))
        for old in backups[:-max(1, DB_BACKUP_KEEP)]:
            old.unlink(missing_ok=True)
        logger.info(f"Database backed up to {target}")

    # Queue persistence

    async def load_persistent_queues(self):
        try:
            results = await self.fetch_db_query(
                "SELECT guild_id, queue_data, music_channel_id, dj_role_id FROM guild_settings "
                "WHERE queue_data IS NOT NULL OR music_channel_id IS NOT NULL OR dj_role_id IS NOT NULL"
            )
        except sqlite3.Error:
            logger.exception("Failed to load persistent data")
            return

        for guild_id, queue_data, music_channel_id, dj_role_id in results:
            # One damaged row must not keep the other guilds' settings from loading.
            try:
                guild_data = self.get_guild_data(guild_id)
                if music_channel_id:
                    guild_data["music_channel_id"] = music_channel_id
                if dj_role_id:
                    guild_data["dj_role_id"] = dj_role_id
                if queue_data:
                    self._restore_queue(guild_data, json.loads(queue_data))
            except (ValueError, TypeError, KeyError, AttributeError):
                logger.warning(f"Could not restore the saved queue of guild {guild_id}", exc_info=True)

        total_songs = sum(len(self.guilds_data[gid]["queue"]) for gid in self.guilds_data)
        logger.info(f"Persistent data loaded: {len(results)} guild(s), {total_songs} queued song(s) restored")

    def _restore_queue(self, guild_data: GuildData, data: dict) -> None:
        if not isinstance(data, dict):
            raise TypeError("queue_data is not an object")
        queue = [Song.from_dict(song_data) for song_data in data.get("queue", [])]
        resume = data.get("resume_at")

        # The song that was playing goes back in front, to start where it was.
        if data.get("current"):
            current = Song.from_dict(data["current"])
            if not queue or queue[0].webpage_url != current.webpage_url:
                queue.insert(0, current)
            resume = {"url": current.webpage_url, "position": int(data.get("current_position") or 0)}

        queued = {song.webpage_url for song in queue}
        if "played" in data:
            played = [Song.from_dict(song_data) for song_data in data["played"]]
        elif data.get("loop_mode") == "queue":
            # Saved before played songs were kept: the old loop list still holds this round's played songs.
            played = [Song.from_dict(song_data) for song_data in data.get("loop_backup", [])]
        else:
            played = []
        guild_data["queue"] = queue
        guild_data["played"] = [song for song in played if song.webpage_url not in queued]
        guild_data["history"] = [Song.from_dict(song_data) for song_data in data.get("history", [])]
        guild_data["loop_mode"] = data.get("loop_mode", "off")
        guild_data["shuffle"] = data.get("shuffle", False)
        guild_data["volume"] = data.get("volume", 100)
        guild_data["speed"] = data.get("speed", 1.0)
        guild_data["audio_effect"] = data.get("audio_effect", "none")
        if resume:
            guild_data["resume_at"] = resume

        panel = data.get("now_playing")
        if isinstance(panel, dict) and panel.get("channel_id") and panel.get("message_id"):
            self._leftover_panels.append((panel["channel_id"], panel["message_id"]))

    async def save_guild_queue(self, guild_id: int):
        """Persist the guild's queue soon; changes are batched into at most one write per second."""
        self.notify_state_changed(guild_id)
        self._queue_dirty.add(guild_id)
        if self.shutting_down:
            return  # close() writes every guild once more at the end
        task = self.db_save_tasks.get(guild_id)
        if task is None or task.done():
            self.db_save_tasks[guild_id] = asyncio.create_task(self._delayed_save_guild_queue(guild_id))

    async def _delayed_save_guild_queue(self, guild_id: int):
        try:
            while guild_id in self._queue_dirty and not self.shutting_down:
                await asyncio.sleep(QUEUE_SAVE_DELAY)
                await self.flush_guild_queue(guild_id)
        except sqlite3.Error:
            logger.exception(f"Failed to save guild queue for guild {guild_id}")
        finally:
            if self.db_save_tasks.get(guild_id) is asyncio.current_task():
                del self.db_save_tasks[guild_id]

    async def flush_guild_queue(self, guild_id: int):
        """Write the guild's queue now (in order with every other write for that guild)."""
        lock = self._queue_write_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            self._queue_dirty.discard(guild_id)
            payload = self._serialize_guild_queue(self.get_guild_data(guild_id))
            await self.execute_db_query(_QUEUE_UPSERT, (guild_id, payload))

    def _queue_payload(self, guild_data: GuildData) -> dict:
        current = guild_data.get("current")
        message = guild_data.get("now_playing_message")
        channel = getattr(message, "channel", None)
        return {
            "queue": [song.to_dict() for song in guild_data["queue"]],
            "played": [song.to_dict() for song in guild_data["played"]],
            "history": [song.to_dict() for song in guild_data["history"]],
            "loop_mode": guild_data["loop_mode"],
            "shuffle": guild_data["shuffle"],
            "volume": guild_data["volume"],
            "speed": guild_data.get("speed", 1.0),
            "audio_effect": guild_data.get("audio_effect", "none"),
            # Restored at the front of the queue, to start at the saved position
            "current": current.to_dict() if current else None,
            "current_position": self.playback.saved_position(guild_data) if current else 0,
            "resume_at": guild_data.get("resume_at"),
            "now_playing": {"channel_id": channel.id, "message_id": message.id} if message and channel else None,
        }

    def _serialize_guild_queue(self, guild_data: GuildData) -> str:
        return json.dumps(self._queue_payload(guild_data))

    async def clear_guild_queue_from_db(self, guild_id: int):
        """Save that nothing is queued or playing. History, volume, loop, shuffle, speed and effect are kept."""
        guild_data = self.get_guild_data(guild_id)
        self.cancel_autoplay(guild_data)
        self.notify_state_changed(guild_id)
        payload = self._queue_payload(guild_data)
        payload.update(queue=[], played=[], current=None, current_position=0, resume_at=None)
        lock = self._queue_write_locks.setdefault(guild_id, asyncio.Lock())
        try:
            async with lock:
                await self.execute_db_query(_QUEUE_UPSERT, (guild_id, json.dumps(payload)))
            logger.info(f"Cleared queue data from database for guild {guild_id} (history preserved)")
        except sqlite3.Error:
            logger.exception("Failed to clear guild queue from database")

    async def get_song_info_cached(self, url_or_query: str, **kwargs) -> dict | list[dict] | None:
        return await self.music.get_song_info_cached(url_or_query, **kwargs)

    async def get_song_info(self, url_or_query: str, **kwargs) -> dict | list[dict] | None:
        return await self.music.get_song_info(url_or_query, **kwargs)

    # Graceful shutdown

    async def close(self):
        if self.shutting_down:
            return await super().close()
        self.shutting_down = True
        logger.info("Shutting down bot...")
        playback = self.playback

        for task_loop in self._task_loops:
            task_loop.cancel()
        await self.audio_cache.cancel_all_downloads()
        for task in [*self._voice_watchdogs.values(), *self._alone_timers.values()]:
            task.cancel()

        guilds = list(self.guilds_data.items())
        for _, guild_data in guilds:
            cancel_imports(guild_data)
            self.cancel_autoplay_prefetch(guild_data)
            # Freeze each song's position before voice goes away, so the restart starts it there.
            if guild_data.get("current") and not guild_data.get("interrupted"):
                guild_data["interrupted"] = {"position": playback.clock_position(guild_data)}

        # Panels and voice statuses refer to this process: their buttons stop working after a restart.
        cleanup = [playback.clear_now_playing(guild_id) for guild_id, _ in guilds]
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.gather(*cleanup, return_exceptions=True), timeout=5)
        for _, guild_data in guilds:
            channel = self.get_channel(guild_data.get("voice_status_channel") or 0)
            if isinstance(channel, discord.VoiceChannel):
                with contextlib.suppress(discord.HTTPException, TimeoutError):
                    await asyncio.wait_for(channel.edit(status=None), timeout=3)

        # Leave voice first; with shutting_down set, neither the voice event nor the player callback
        # touches the queue, so the save below is the last word.
        for _, guild_data in guilds:
            voice_client = guild_data.get("voice_client")
            if voice_client:
                with contextlib.suppress(discord.HTTPException, discord.ClientException, OSError, TimeoutError):
                    await asyncio.wait_for(voice_client.disconnect(force=True), timeout=5)

        pending_saves = list(self.db_save_tasks.values())
        for task in pending_saves:
            task.cancel()
        await asyncio.gather(*pending_saves, return_exceptions=True)

        logger.info("Saving all queues before shutdown...")
        for guild_id, guild_data in guilds:
            if guild_id in self._queue_dirty or guild_data.get("current"):
                try:
                    await self.flush_guild_queue(guild_id)
                except sqlite3.Error:
                    logger.exception(f"Failed to save queue for guild {guild_id} on shutdown")

        background = list(self._background_tasks)
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)

        for session in (self._http_session, self._stream_session):
            if session is not None and not session.closed:
                await session.close()

        with contextlib.suppress(sqlite3.Error):
            await self.execute_db_query("PRAGMA wal_checkpoint(TRUNCATE)")

        # Extractions and downloads are disposable; queued DB writes must finish (off the event loop).
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.import_executor.shutdown(wait=False, cancel_futures=True)
        self.download_executor.shutdown(wait=False, cancel_futures=True)
        await asyncio.to_thread(self.db_executor.shutdown, wait=True)

        await super().close()
        logger.info("Bot shutdown complete")


def _reconnect_running(voice_client) -> bool:
    # discord.py exposes no flag for this; its reconnect loop is the connection state's runner task.
    runner = getattr(getattr(voice_client, "_connection", None), "_runner", None)
    return runner is not None and not runner.done()
