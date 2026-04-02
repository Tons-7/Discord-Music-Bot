import asyncio
import concurrent.futures
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Dict

import discord
import pylast
import spotipy
import yt_dlp
from discord.ext import commands, tasks
from spotipy.oauth2 import SpotifyClientCredentials

from config import (
    get_intents, COLOR, MAX_CACHE_SIZE, CACHE_TTL,
    INACTIVE_TIMEOUT_MINUTES, DB_VERSION,
)
from models.song import Song
from services.music_service import MusicService
from services.playback_service import PlaybackService
from utils import create_embed

logger = logging.getLogger(__name__)


class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=get_intents(), help_command=None)

        self.message_update_locks = {}
        self.message_validation_cache = {}
        self.loop = None

        self.init_database()
        self.guilds_data = {}
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)

        self.song_cache = {}
        self.max_cache_size = MAX_CACHE_SIZE
        self.cache_ttl = CACHE_TTL

        self.db_save_tasks = {}

        #  Last.fm rate-limiter state
        self._lastfm_call_times: list[float] = []
        self._lastfm_lock = asyncio.Lock()
        self.lastfm_rate_limit = 5  # max calls per second

        self.ytdl_format_options = {
            "format": "bestaudio/best",
            "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
            "restrictfilenames": True,
            "noplaylist": False,
            "extract_flat": True,
            "nocheckcertificate": True,
            "ignoreerrors": True,
            "logtostderr": False,
            "quiet": True,
            "no_warnings": True,
            "default_search": "auto",
            "source_address": "0.0.0.0",
            "age_limit": 18,
            "retries": 15,
            "fragment_retries": 15,
            "skip_unavailable_fragments": True,
            "keep_fragments": False,
            "concurrent_fragment_downloads": 1,
            "extractor_retries": 10,
            "file_access_retries": 10,
            "socket_timeout": 60,
            "http_chunk_size": 10485760,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            },
            "geo_bypass": True,
            "prefer_free_formats": False,
            "playliststart": 1,
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "web", "mweb"],
                    "player_skip": ["webpage"],
                }
            },
        }

        self.ffmpeg_options = {
            "before_options": (
                "-reconnect 1 "
                "-reconnect_streamed 1 "
                "-reconnect_delay_max 5 "
                "-reconnect_on_network_error 1 "
                "-reconnect_on_http_error 5xx "
                "-nostdin "
                "-user_agent 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0'"
            ),
            "options": (
                "-vn "
                "-threads 0 "
                "-probesize 1M "
                "-analyzeduration 1M "
                "-fflags +discardcorrupt+genpts "
                "-flags +low_delay"
            ),
        }

        self.voice_reconnect_enabled = True
        self.voice_reconnect_delay = 2

        self.ytdl = yt_dlp.YoutubeDL(self.ytdl_format_options)

        try:
            spotify_client_id = os.getenv("SPOTIFY_CLIENT_ID")
            spotify_client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")

            if spotify_client_id and spotify_client_secret:
                self.spotify = spotipy.Spotify(
                    client_credentials_manager=SpotifyClientCredentials(
                        client_id=spotify_client_id, client_secret=spotify_client_secret
                    )
                )
                logger.info("Spotify integration enabled")
            else:
                self.spotify = None
                logger.info("Spotify credentials not found. Spotify features disabled.")
        except Exception as e:
            self.spotify = None
            logger.warning(f"Spotify setup failed: {e}")

        try:
            lastfm_api_key = os.getenv("LASTFM_API_KEY")
            lastfm_api_secret = os.getenv("LASTFM_API_SECRET")

            if lastfm_api_key and lastfm_api_secret:
                self.lastfm = pylast.LastFMNetwork(
                    api_key=lastfm_api_key,
                    api_secret=lastfm_api_secret
                )
                logger.info("Last.fm integration enabled")
            else:
                self.lastfm = None
                logger.info("Last.fm credentials not found. Last.fm features disabled.")
        except Exception as e:
            self.lastfm = None
            logger.warning(f"Last.fm setup failed: {e}")

        # Shared service instances — avoids re-allocating on every call/task tick
        self._music_service = MusicService(self)
        self._playback_service = PlaybackService(self)

    # Database

    @staticmethod
    def init_database():
        conn = None
        try:
            conn = sqlite3.connect("music_bot.db")
            conn.execute("PRAGMA journal_mode=WAL")
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

            # Collaborative playlist members
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS playlist_collaborators
                (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    playlist_id INTEGER NOT NULL,
                    user_id     INTEGER NOT NULL,
                    is_global   INTEGER DEFAULT 0,
                    added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (playlist_id, user_id, is_global)
                )
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

            conn.commit()
            logger.info("Database initialized successfully (WAL mode, schema v%d)", DB_VERSION)

        except Exception as e:
            logger.error(f"Database initialization failed: {e}")
            raise
        finally:
            if conn:
                conn.close()

    @staticmethod
    def _run_migrations(cursor, from_version: int, to_version: int):
        """Run sequential schema migrations."""
        migrations = {
            # version 2: add dj_role_id column to guild_settings
            2: [
                "ALTER TABLE guild_settings ADD COLUMN dj_role_id INTEGER",
            ],
            # version 3: add song_url column to favorites for proper dedup
            3: [
                "ALTER TABLE favorites ADD COLUMN song_url TEXT NOT NULL DEFAULT ''",
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
    def get_db_connection():
        conn = sqlite3.connect("music_bot.db")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    async def execute_db_query(self, query: str, params: tuple = None):
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
        return await loop.run_in_executor(self.executor, _execute)

    async def fetch_db_query(self, query: str, params: tuple = None):
        def _fetch():
            with self.get_db_connection() as conn:
                cursor = conn.cursor()
                if params:
                    cursor.execute(query, params)
                else:
                    cursor.execute(query)
                return cursor.fetchall()

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, _fetch)

    # Guild state

    def get_guild_data(self, guild_id: int) -> Dict:
        if guild_id not in self.guilds_data:
            self.guilds_data[guild_id] = {
                "guild_id": guild_id,
                "queue": [],
                "loop_backup": [],
                "history": [],
                "history_position": 0,
                "current": None,
                "position": 0,
                "seek_offset": 0,
                "loop_mode": "off",
                "shuffle": False,
                "volume": 100,
                "autoplay": False,
                "autoplay_prefetch": None,
                "autoplay_prefetch_task": None,
                "voice_client": None,
                "intentional_disconnect": False,
                "last_activity": datetime.now(),
                "now_playing_message": None,
                "music_channel_id": None,
                "start_time": None,
                "message_ready_for_timestamps": False,
                "message_last_validated": 0,
                "seeking": False,
                "pause_position": None,
                "now_playing_message_sent_time": None,
                "play_lock": asyncio.Lock(),
                "speed": 1.0,
                "audio_effect": "none",
                "dj_role_id": None,
            }
        return self.guilds_data[guild_id]

    def cancel_autoplay(self, guild_data: Dict):
        guild_data["autoplay"] = False
        prefetch_task = guild_data.get("autoplay_prefetch_task")
        if prefetch_task and not prefetch_task.done():
            prefetch_task.cancel()
        guild_data["autoplay_prefetch"] = None
        guild_data["autoplay_prefetch_task"] = None

    # Guild settings persistence

    async def save_guild_music_channel(self, guild_id: int, channel_id: int):
        try:
            await self.execute_db_query(
                """
                INSERT INTO guild_settings (guild_id, music_channel_id)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET music_channel_id = excluded.music_channel_id
            """,
                (guild_id, channel_id),
            )
        except Exception as e:
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
        except Exception as e:
            logger.error(f"Failed to save DJ role: {e}")

    # User stats tracking

    async def record_listening_stat(
            self,
            user_id: int,
            guild_id: int,
            song: 'Song',
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
        except Exception as e:
            logger.debug(f"Failed to record listening stat: {e}")

    # Favorites

    async def add_favorite(self, user_id: int, song: 'Song') -> bool:
        """Add a song to the user's favorites. Returns False if already exists."""
        try:
            # Check if this song URL is already favorited
            existing = await self.fetch_db_query(
                "SELECT id FROM favorites WHERE user_id = ? AND song_url = ?",
                (user_id, song.webpage_url),
            )
            if existing:
                return False

            song_json = json.dumps(song.to_dict(), sort_keys=True)
            await self.execute_db_query(
                "INSERT INTO favorites (user_id, song_data, song_url) VALUES (?, ?, ?)",
                (user_id, song_json, song.webpage_url),
            )
            return True
        except Exception as e:
            logger.error(f"Failed to add favorite: {e}")
            return False

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
        except Exception as e:
            logger.error(f"Failed to remove favorite: {e}")
            return False

    async def get_favorites(self, user_id: int) -> list[dict]:
        try:
            rows = await self.fetch_db_query(
                "SELECT song_data FROM favorites WHERE user_id = ? ORDER BY added_at ASC",
                (user_id,),
            )
            return [json.loads(row[0]) for row in rows]
        except Exception as e:
            logger.error(f"Failed to get favorites: {e}")
            return []

    # Collaborative playlist helpers

    async def get_playlist_id(self, user_id: int, name: str, guild_id: int = None) -> int | None:
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

    async def add_collaborator(self, playlist_id: int, user_id: int, is_global: bool = False):
        await self.execute_db_query(
            "INSERT OR IGNORE INTO playlist_collaborators (playlist_id, user_id, is_global) VALUES (?, ?, ?)",
            (playlist_id, user_id, 1 if is_global else 0),
        )

    async def remove_collaborator(self, playlist_id: int, user_id: int, is_global: bool = False):
        await self.execute_db_query(
            "DELETE FROM playlist_collaborators WHERE playlist_id = ? AND user_id = ? AND is_global = ?",
            (playlist_id, user_id, 1 if is_global else 0),
        )

    async def get_collaborators(self, playlist_id: int, is_global: bool = False) -> list[int]:
        rows = await self.fetch_db_query(
            "SELECT user_id FROM playlist_collaborators WHERE playlist_id = ? AND is_global = ?",
            (playlist_id, 1 if is_global else 0),
        )
        return [row[0] for row in rows]

    async def is_collaborator(self, playlist_id: int, user_id: int, is_global: bool = False) -> bool:
        rows = await self.fetch_db_query(
            "SELECT 1 FROM playlist_collaborators WHERE playlist_id = ? AND user_id = ? AND is_global = ?",
            (playlist_id, user_id, 1 if is_global else 0),
        )
        return bool(rows)

    # Lifecycle hooks

    async def setup_hook(self):
        pass

    async def on_ready(self):
        logger.info(f"{self.user} has connected to Discord!")
        logger.info(f"Connected to {len(self.guilds)} guilds")

        self.loop = asyncio.get_running_loop()

        # Scale thread pool based on guild count
        desired_workers = max(3, min(len(self.guilds), 10))
        if self.executor._max_workers < desired_workers:
            self.executor._max_workers = desired_workers
            logger.info(f"ThreadPoolExecutor scaled to {desired_workers} workers")

        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} slash command(s)")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")

        for task in [
            self.cleanup_inactive, self.cleanup_cache,
            self.cleanup_inactive_guilds, self.update_now_playing_timestamps,
            self.cleanup_validation_cache, self.check_voice_health,
        ]:
            if not task.is_running():
                task.start()
        await self.load_persistent_queues()

    # Voice state handling

    async def on_voice_state_update(self, member, before, after):
        if member.id != self.user.id:
            return

        if not before.channel and not after.channel:
            return

        guild_id = before.channel.guild.id if before.channel else after.channel.guild.id
        guild_data = self.get_guild_data(guild_id)

        if not before.channel and after.channel:
            logger.info(f"Bot reconnected to voice in guild {guild_id} (auto-reconnect)")
            guild = self.get_guild(guild_id)
            if guild and guild.voice_client:
                guild_data["voice_client"] = guild.voice_client

                await asyncio.sleep(1)
                await self._resume_playback_after_reconnect(guild_id)
            return

        if before.channel and not after.channel:
            logger.warning(f"Bot disconnected from voice in guild {guild_id}")

            self.cancel_autoplay(guild_data)

            had_current_song = guild_data.get("current") is not None
            had_queue = len(guild_data.get("queue", [])) > 0
            voice_channel = before.channel

            if guild_data["voice_client"]:
                guild_data["voice_client"] = None

            if guild_data.get("intentional_disconnect", False):
                logger.info(f"Intentional disconnect for guild {guild_id}, skipping reconnect")
                guild_data["intentional_disconnect"] = False
                guild_data["current"] = None
                guild_data["start_time"] = None
                guild_data["history_position"] = len(guild_data["history"])
                guild_data["queue"].clear()
                guild_data["loop_backup"].clear()
                await self.clear_guild_queue_from_db(guild_id)
                return

            if self.voice_reconnect_enabled and (had_current_song or had_queue):
                logger.info(f"Attempting voice reconnection for guild {guild_id}...")
                task = asyncio.create_task(self._attempt_voice_reconnect(guild_id, voice_channel))
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() and t.exception() else None)
            else:
                guild_data["current"] = None
                guild_data["start_time"] = None
                guild_data["history_position"] = len(guild_data["history"])
                guild_data["queue"].clear()
                guild_data["loop_backup"].clear()
                await self.clear_guild_queue_from_db(guild_id)

    async def _attempt_voice_reconnect(self, guild_id: int, voice_channel):
        guild_data = self.get_guild_data(guild_id)

        try:
            await asyncio.sleep(self.voice_reconnect_delay)

            guild = self.get_guild(guild_id)
            if guild and guild.voice_client and guild.voice_client.is_connected():
                logger.info(f"Discord auto-reconnected to guild {guild_id}")
                guild_data["voice_client"] = guild.voice_client

                await self._resume_playback_after_reconnect(guild_id)
                return

            if guild_data["voice_client"] and guild_data["voice_client"].is_connected():
                logger.info(f"Already reconnected to guild {guild_id}")
                await self._resume_playback_after_reconnect(guild_id)
                return

            logger.info(f"Reconnecting to voice channel in guild {guild_id}...")
            voice_client = await voice_channel.connect(timeout=10.0, reconnect=True)
            guild_data["voice_client"] = voice_client

            logger.info(f"Successfully reconnected to voice in guild {guild_id}")

            await self._resume_playback_after_reconnect(guild_id)

        except discord.ClientException as e:
            if "already connected" in str(e).lower():
                logger.info(f"Voice already connected for guild {guild_id}, using existing connection")

                guild = self.get_guild(guild_id)
                if guild and guild.voice_client:
                    guild_data["voice_client"] = guild.voice_client
                    await self._resume_playback_after_reconnect(guild_id)
            else:
                logger.error(f"Client exception during reconnect for guild {guild_id}: {e}")
                await self._cleanup_after_failed_reconnect(guild_id)
        except asyncio.TimeoutError:
            logger.error(f"Voice reconnection timeout for guild {guild_id}")
            await self._cleanup_after_failed_reconnect(guild_id)
        except Exception as e:
            logger.error(f"Failed to reconnect voice for guild {guild_id}: {e}")
            await self._cleanup_after_failed_reconnect(guild_id)

    async def _resume_playback_after_reconnect(self, guild_id: int):
        guild_data = self.get_guild_data(guild_id)

        try:
            if guild_data.get("voice_client") and (
                    guild_data["voice_client"].is_playing()
                    or guild_data["voice_client"].is_paused()
            ):
                guild_data["voice_client"].stop()
                await asyncio.sleep(0.5)

            playback_service = self._playback_service

            if guild_data.get("current"):
                current_song = guild_data["current"]
                logger.info(f"Resuming playback of: {current_song.title}")

                guild_data["current"] = None

                guild_data["queue"].insert(0, current_song)

                await playback_service.play_next(guild_id)
            elif guild_data.get("queue"):
                logger.info(f"Starting queue playback after reconnect")
                await playback_service.play_next(guild_id)
        except Exception as e:
            logger.error(f"Error resuming playback after reconnect: {e}")

    async def _cleanup_after_failed_reconnect(self, guild_id: int):
        guild_data = self.get_guild_data(guild_id)

        logger.info(f"Cleaning up after failed reconnect for guild {guild_id}")

        guild_data["current"] = None
        guild_data["start_time"] = None
        guild_data["voice_client"] = None

        try:
            music_cog = self.get_cog("MusicCommands")
            if music_cog and guild_data.get("music_channel_id"):
                channel = self.get_channel(guild_data["music_channel_id"])
                if channel:
                    embed = create_embed(
                        "Voice Connection Lost",
                        "The bot was disconnected from voice and couldn't reconnect. Use `/play` to start again.",
                        COLOR,
                        self.user
                    )
                    await channel.send(embed=embed, delete_after=30)
        except Exception as e:
            logger.error(f"Error sending disconnect notification: {e}")

    # Background tasks

    @tasks.loop(minutes=5)
    async def cleanup_inactive(self):
        try:
            for guild_id, data in list(self.guilds_data.items()):
                if data["voice_client"] and data["voice_client"].is_connected():
                    inactive_time = datetime.now() - data["last_activity"]

                    is_truly_inactive = (
                            not data["voice_client"].is_playing()
                            and not data["voice_client"].is_paused()
                            and not data.get("current")
                    )

                    if inactive_time > timedelta(minutes=INACTIVE_TIMEOUT_MINUTES) and is_truly_inactive:
                        guild = self.get_guild(guild_id)
                        guild_name = guild.name if guild else "unknown"
                        await data["voice_client"].disconnect()
                        data["voice_client"] = None
                        logger.info(f"Disconnected from inactive guild: {guild_name} ({guild_id})")
        except Exception as e:
            logger.error(f"Cleanup task error: {e}")

    @tasks.loop(minutes=10)
    async def cleanup_cache(self):
        try:
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
        except Exception as e:
            logger.error(f"Cache cleanup error: {e}")

    @tasks.loop(hours=1)
    async def cleanup_inactive_guilds(self):
        try:
            current_guild_ids = {guild.id for guild in self.guilds}
            inactive_guilds = []

            for guild_id in list(self.guilds_data.keys()):
                if guild_id not in current_guild_ids:
                    inactive_guilds.append(guild_id)
                    del self.guilds_data[guild_id]

            if inactive_guilds:
                logger.info(
                    f"Cleaned up data for {len(inactive_guilds)} inactive guilds"
                )
        except Exception as e:
            logger.error(f"Guild cleanup error: {e}")

    @tasks.loop(seconds=1)
    async def update_now_playing_timestamps(self):
        await self._playback_service.update_timestamps_task()

    @tasks.loop(minutes=5)
    async def cleanup_validation_cache(self):
        try:
            current_time = asyncio.get_running_loop().time()
            expired_keys = [
                key
                for key, value in self.message_validation_cache.items()
                if current_time - value["time"] > 60.0
            ]

            for key in expired_keys:
                del self.message_validation_cache[key]

            if expired_keys:
                logger.debug(f"Cleaned {len(expired_keys)} validation cache entries")
        except Exception as e:
            logger.error(f"Validation cache cleanup error: {e}")

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

                    has_current = guild_data.get("current") is not None
                    is_playing = voice_client.is_playing()
                    is_paused = voice_client.is_paused()
                    is_seeking = guild_data.get("seeking", False)

                    if has_current and not is_playing and not is_paused and not is_seeking:
                        logger.warning(f"Detected stalled playback in guild: {guild_name} ({guild_id})")

                        guild_data["current"] = None
                        await self._playback_service.play_next(guild_id)
                else:
                    if guild_data.get("current") or guild_data.get("queue"):
                        logger.warning(
                            f"Voice client disconnected but has active state in guild: {guild_name} ({guild_id})")
                        guild_data["voice_client"] = None

        except Exception as e:
            logger.error(f"Voice health check error: {e}")

    # Queue persistence

    async def load_persistent_queues(self):
        try:
            results = await self.fetch_db_query(
                "SELECT guild_id, queue_data, music_channel_id, dj_role_id FROM guild_settings "
                "WHERE queue_data IS NOT NULL OR music_channel_id IS NOT NULL OR dj_role_id IS NOT NULL"
            )

            for row in results:
                guild_id = row[0]
                queue_data = row[1]
                music_channel_id = row[2]
                dj_role_id = row[3] if len(row) > 3 else None

                guild_data = self.get_guild_data(guild_id)

                if music_channel_id:
                    guild_data["music_channel_id"] = music_channel_id

                if dj_role_id:
                    guild_data["dj_role_id"] = dj_role_id

                if queue_data:
                    try:
                        data = json.loads(queue_data)
                        guild_data["queue"] = [
                            Song.from_dict(song_data)
                            for song_data in data.get("queue", [])
                        ]
                        guild_data["loop_backup"] = [
                            Song.from_dict(song_data)
                            for song_data in data.get("loop_backup", [])
                        ]

                        history_data = data.get("history", [])

                        guild_data["history"] = [
                            Song.from_dict(song_data) for song_data in history_data
                        ]
                        guild_data["history_position"] = data.get(
                            "history_position", len(guild_data["history"])
                        )

                        guild_data["loop_mode"] = data.get("loop_mode", "off")
                        guild_data["shuffle"] = data.get("shuffle", False)
                        guild_data["volume"] = data.get("volume", 100)
                        guild_data["speed"] = data.get("speed", 1.0)
                        guild_data["audio_effect"] = data.get("audio_effect", "none")
                    except json.JSONDecodeError:
                        continue

            total_songs = sum(len(self.guilds_data[gid]["queue"]) for gid in self.guilds_data)
            logger.info(f"Persistent data loaded: {len(results)} guild(s), {total_songs} queued song(s) restored")
        except Exception as e:
            logger.error(f"Failed to load persistent data: {e}")

    async def save_guild_queue(self, guild_id: int):
        if guild_id in self.db_save_tasks:
            old_task = self.db_save_tasks[guild_id]
            old_task.cancel()
            try:
                await old_task
            except (asyncio.CancelledError, Exception):
                pass

        self.db_save_tasks[guild_id] = asyncio.create_task(
            self._delayed_save_guild_queue(guild_id)
        )

    def _serialize_guild_queue(self, guild_data: Dict) -> str:
        queue_data = {
            "queue": [song.to_dict() for song in guild_data["queue"]],
            "loop_backup": [song.to_dict() for song in guild_data["loop_backup"]],
            "history": [song.to_dict() for song in guild_data["history"]],
            "history_position": guild_data.get(
                "history_position", len(guild_data["history"])
            ),
            "loop_mode": guild_data["loop_mode"],
            "shuffle": guild_data["shuffle"],
            "volume": guild_data["volume"],
            "speed": guild_data.get("speed", 1.0),
            "audio_effect": guild_data.get("audio_effect", "none"),
        }
        return json.dumps(queue_data)

    async def _delayed_save_guild_queue(self, guild_id: int):
        await asyncio.sleep(1)

        try:
            guild_data = self.get_guild_data(guild_id)

            await self.execute_db_query(
                """
                INSERT INTO guild_settings (guild_id, queue_data, music_channel_id, dj_role_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    queue_data = excluded.queue_data,
                    music_channel_id = excluded.music_channel_id,
                    dj_role_id = excluded.dj_role_id
            """,
                (
                    guild_id,
                    self._serialize_guild_queue(guild_data),
                    guild_data.get("music_channel_id"),
                    guild_data.get("dj_role_id"),
                ),
            )

        except Exception as e:
            logger.error(f"Failed to save guild queue: {e}")
        finally:
            if guild_id in self.db_save_tasks:
                del self.db_save_tasks[guild_id]

    async def clear_guild_queue_from_db(self, guild_id: int):
        try:
            guild_data = self.get_guild_data(guild_id)

            self.cancel_autoplay(guild_data)

            queue_data = {
                "queue": [],
                "loop_backup": [],
                "history": [song.to_dict() for song in guild_data.get("history", [])],
                "history_position": guild_data.get(
                    "history_position", len(guild_data.get("history", []))
                ),
                "loop_mode": "off",
                "shuffle": False,
                "volume": guild_data.get("volume", 100),
                "speed": 1.0,
                "audio_effect": "none",
            }

            await self.execute_db_query(
                """
                INSERT INTO guild_settings (guild_id, queue_data, music_channel_id, dj_role_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    queue_data = excluded.queue_data,
                    music_channel_id = excluded.music_channel_id,
                    dj_role_id = excluded.dj_role_id
            """,
                (
                    guild_id,
                    json.dumps(queue_data),
                    guild_data.get("music_channel_id"),
                    guild_data.get("dj_role_id"),
                ),
            )
            logger.info(f"Cleared queue data from database for guild {guild_id} (history preserved)")
        except Exception as e:
            logger.error(f"Failed to clear guild queue from database: {e}")

    async def get_song_info_cached(self, url_or_query: str) -> Optional[Dict]:
        return await self._music_service.get_song_info_cached(url_or_query)

    async def get_song_info(self, url_or_query: str) -> Optional[Dict]:
        return await self._music_service.get_song_info(url_or_query)

    # Graceful shutdown

    async def close(self):
        logger.info("Shutting down bot...")

        # Cancel all background tasks first
        for task_method in [
            self.cleanup_inactive,
            self.cleanup_cache,
            self.cleanup_inactive_guilds,
            self.update_now_playing_timestamps,
            self.cleanup_validation_cache,
            self.check_voice_health,
        ]:
            try:
                task_method.cancel()
            except Exception:
                pass

        logger.info("Saving all queues before shutdown...")
        for guild_id in list(self.guilds_data.keys()):
            try:
                guild_data = self.get_guild_data(guild_id)

                guild_data["autoplay"] = False

                if guild_data.get("queue") or guild_data.get("current") or guild_data.get("loop_backup"):
                    await self.execute_db_query(
                        """
                        INSERT INTO guild_settings (guild_id, queue_data, music_channel_id, dj_role_id)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(guild_id) DO UPDATE SET
                            queue_data = excluded.queue_data,
                            music_channel_id = excluded.music_channel_id,
                            dj_role_id = excluded.dj_role_id
                    """,
                        (
                            guild_id,
                            self._serialize_guild_queue(guild_data),
                            guild_data.get("music_channel_id"),
                            guild_data.get("dj_role_id"),
                        ),
                    )
            except Exception as e:
                logger.error(f"Failed to save queue for guild {guild_id} on shutdown: {e}")

        pending_tasks = list(self.db_save_tasks.values())
        for task in pending_tasks:
            task.cancel()

        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
            logger.info("All database save tasks completed")

        for guild_data in self.guilds_data.values():
            if guild_data["voice_client"]:
                try:
                    guild_data["intentional_disconnect"] = True
                    await guild_data["voice_client"].disconnect()
                except Exception as e:
                    logger.debug(f"Error disconnecting voice client: {e}")

        self.executor.shutdown(wait=True)

        await super().close()
        logger.info("Bot shutdown complete")
