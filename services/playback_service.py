import asyncio
import contextlib
import enum
import logging
import re
import shlex
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING

import aiohttp
import discord

from config import (
    AUDIO_EFFECTS,
    MAX_QUEUE_SIZE,
    MAX_SKIPPED_SONGS,
    MAX_TRANSIENT_FAILURES,
    NOW_PLAYING_RESEND_SECONDS,
    VOICE_CHANNEL_STATUS,
)
from models.song import Song
from services.audio_cache import cacheable
from services.music_service import ExtractionError, MusicService
from services.now_playing import NowPlayingPanel
from services.queue_service import QueueService
from utils.helpers import extract_youtube_id, get_existing_urls, md
from utils.ytdl import available_at, wait_until_available

if TYPE_CHECKING:
    from bot import GuildData

logger = logging.getLogger(__name__)


def _log_import_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        logger.error("Background import failed", exc_info=task.exception())

# How many recent history entries autoplay treats as "already played"
AUTOPLAY_REPEAT_WINDOW = 30
# A now-playing edit that takes longer is abandoned so the next tick can try again
NOW_PLAYING_EDIT_TIMEOUT = 10
# A stream FFmpeg dropped is reopened only when at least this much of the song is left
STREAM_RECOVERY_MIN_REMAINING = 10
# A Spotify import saves (and so broadcasts) after the first song, then every this many songs or second
IMPORT_SAVE_EVERY = 10


class PlaybackError(Exception):
    """A playback request that cannot be carried out; str(error) is a user-facing reason."""


class _VoiceLost(Exception):
    pass


class _Cancelled(Exception):
    pass


class _Outcome(enum.Enum):
    PLAYED = "played"
    SKIP = "skip"  # this song can't be played (gone, private, no match)
    FAIL = "fail"  # something went wrong that may work on a retry
    BLOCKED = "blocked"  # YouTube's bot check: every song will fail
    NO_VOICE = "no_voice"
    CANCELLED = "cancelled"


class PlaybackService:
    def __init__(self, bot):
        self.bot = bot
        self.queue_service = QueueService(bot)
        self.music_service = MusicService(bot)
        self.panel = NowPlayingPanel(bot)
        self._np_tasks: dict[int, asyncio.Task] = {}

    # ── FFmpeg options ─────────────────────────────────────────────────

    def build_ffmpeg_options(
            self, guild_data: GuildData, *, local_file: bool = False, seek: float | None = None,
            headers: dict | None = None,
    ) -> dict:
        """FFmpeg options for a voice source: reconnects, yt-dlp's request headers, a seek, speed and effect."""
        before = "-nostdin" if local_file else self.bot.ffmpeg_options["before_options"]
        if headers and not local_file:
            lines = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
            before += f" -headers {shlex.quote(lines)}"
        if seek:
            before += f" -ss {seek}"

        filters = []
        speed = guild_data.get("speed", 1.0)
        if speed != 1.0:
            # atempo keeps the pitch; speed is clamped to 0.5-2.0 where it is set
            filters.append(f"atempo={speed:.4f}")
        effect_filter = AUDIO_EFFECTS.get(guild_data.get("audio_effect", "none"), {}).get("filter")
        if effect_filter:
            filters.append(effect_filter)

        options = self.bot.ffmpeg_options["options"]
        if filters:
            options = f'{options} -af "{",".join(filters)}"'
        return {"before_options": before, "options": options}

    def _build_ffmpeg_options(self, guild_data: GuildData, local_file: bool = False) -> dict:
        """Compatibility alias of build_ffmpeg_options."""
        return self.build_ffmpeg_options(guild_data, local_file=local_file)

    def _make_source(self, guild_data: GuildData, url: str, *, local_file: bool, seek: float = 0,
                     headers: dict | None = None) -> discord.PCMVolumeTransformer:
        options = self.build_ffmpeg_options(guild_data, local_file=local_file, seek=seek, headers=headers)
        return discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(url, **options), volume=guild_data["volume"] / 100)

    def get_effective_speed(self, guild_data: GuildData) -> float:
        """Calculate the combined speed multiplier (atempo * effect speed)."""
        speed = guild_data.get("speed", 1.0)
        effect = guild_data.get("audio_effect", "none")
        effect_mult = AUDIO_EFFECTS.get(effect, {}).get("speed_mult", 1.0)
        return speed * effect_mult

    def source_speed(self, guild_data: GuildData) -> float:
        """Speed of the voice source now playing; a speed or effect change only reaches it once it is rebuilt."""
        return guild_data.get("source_speed") or self.get_effective_speed(guild_data)

    # ── Clock ──────────────────────────────────────────────────────────

    def handle_pause(self, guild_id: int):
        guild_data = self.bot.get_guild_data(guild_id)
        if guild_data["voice_client"] and guild_data["voice_client"].is_playing():
            current_pos = self.get_current_position(guild_id)
            self._close_listen_segment(guild_data)
            guild_data["pause_position"] = current_pos
            guild_data["voice_client"].pause()
            self.bot.notify_state_changed(guild_id)
            return True
        return False

    def handle_resume(self, guild_id: int):
        guild_data = self.bot.get_guild_data(guild_id)
        if guild_data["voice_client"] and guild_data["voice_client"].is_paused():
            if guild_data.get("pause_position") is not None:
                guild_data["seek_offset"] = guild_data["pause_position"]
                guild_data["start_time"] = time.monotonic()
                guild_data["pause_position"] = None
            guild_data["paused_by"] = None
            guild_data["listen_start"] = time.monotonic()
            guild_data["voice_client"].resume()
            self.bot.notify_state_changed(guild_id)
            return True
        return False

    def get_current_position(self, guild_id: int) -> int:
        guild_data = self.bot.get_guild_data(guild_id)

        # While a seek prepares the new source the old one keeps playing, so its clock stays right.
        if not guild_data["start_time"]:
            return guild_data["seek_offset"]

        voice_client = guild_data["voice_client"]
        if not voice_client:
            return guild_data["seek_offset"]

        speed = self.source_speed(guild_data)

        if voice_client.is_paused():
            if guild_data.get("pause_position") is not None:
                return guild_data["pause_position"]
            elapsed = time.monotonic() - guild_data["start_time"]
            return int(elapsed * speed) + guild_data["seek_offset"]

        if voice_client.is_playing():
            elapsed = time.monotonic() - guild_data["start_time"]
            return int(elapsed * speed) + guild_data["seek_offset"]

        return guild_data["seek_offset"]

    def clock_position(self, guild_data: GuildData, at: float | None = None) -> int:
        """Position from the clock alone, whatever the player is doing (voice and Activity-only playback)."""
        if guild_data.get("pause_position") is not None:
            return int(guild_data["pause_position"])
        start = guild_data.get("start_time")
        offset = int(guild_data.get("seek_offset") or 0)
        if not start:
            return offset
        now = time.monotonic() if at is None else min(at, time.monotonic())
        return max(0, int((now - start) * self.source_speed(guild_data)) + offset)

    def saved_position(self, guild_data: GuildData) -> int:
        """Where the current song should start again after a restart or a lost connection."""
        interrupted = guild_data.get("interrupted")
        return int(interrupted["position"]) if interrupted else self.clock_position(guild_data)

    def is_paused(self, guild_id: int) -> bool:
        guild_data = self.bot.get_guild_data(guild_id)
        return guild_data["voice_client"] and guild_data["voice_client"].is_paused()

    # ── Listening time ─────────────────────────────────────────────────

    def _close_listen_segment(self, guild_data: GuildData, at: float | None = None) -> None:
        start = guild_data.get("listen_start")
        if start is not None:
            end = time.monotonic() if at is None else max(start, min(at, time.monotonic()))
            guild_data["listened"] = guild_data.get("listened", 0.0) + (end - start) * self.source_speed(guild_data)
            guild_data["listen_start"] = None

    def get_listened_seconds(self, guild_data: GuildData) -> int:
        """Song time actually played for the current voice song, across pauses and seeks."""
        total = guild_data.get("listened", 0.0)
        start = guild_data.get("listen_start")
        if start is not None:
            total += (time.monotonic() - start) * self.source_speed(guild_data)
        return int(total)

    @staticmethod
    def _listeners(channel) -> list:
        """Members who can hear the channel: people, not bots, and not deafened."""
        return [
            m for m in getattr(channel, "members", [])
            if not m.bot and not (m.voice and (m.voice.deaf or m.voice.self_deaf))
        ]

    def take_listened(self, guild_data: GuildData) -> int:
        """The current song's listening time since the last credit (at most its length); counting starts over."""
        seconds = self.get_listened_seconds(guild_data)
        guild_data["listened"] = 0.0
        if guild_data.get("listen_start") is not None:
            guild_data["listen_start"] = time.monotonic()
        song = guild_data.get("current")
        if song and song.duration > 0:
            seconds = min(seconds, song.duration)
        return seconds

    def credit_listeners(self, guild_id: int, channel=None) -> None:
        """Record the current song's listening time so far for the people in the channel, then start over."""
        guild_data = self.bot.get_guild_data(guild_id)
        song = guild_data.get("current")
        if channel is None:
            channel = getattr(guild_data.get("voice_client"), "channel", None)
        seconds = self.take_listened(guild_data)
        if not song or channel is None or seconds <= 0:
            return
        for member in self._listeners(channel):
            self.bot.spawn(self.bot.record_listening_stat(member.id, guild_id, song, seconds))

    def _finish_song(self, guild_id: int, song: Song) -> None:
        self.queue_service.add_to_history(guild_id, song)
        self.credit_listeners(guild_id)

    # ── State primitives ───────────────────────────────────────────────

    @staticmethod
    def generation(guild_data: GuildData) -> int:
        """Bumped by stop/leave; a play_next or advance that started earlier must not start a song."""
        return guild_data.get("play_gen", 0)

    @staticmethod
    def bump_generation(guild_data: GuildData) -> None:
        guild_data["play_gen"] = guild_data.get("play_gen", 0) + 1

    @staticmethod
    def _new_token(guild_data: GuildData) -> int:
        """Identifies the player now running, so callbacks of replaced or stopped players are ignored."""
        guild_data["play_token"] = guild_data.get("play_token", 0) + 1
        return guild_data["play_token"]

    @staticmethod
    def reset_playback_state(guild_data: GuildData) -> None:
        """Nothing is playing: clear the current song, its clock and its listening time."""
        guild_data["current"] = None
        guild_data["start_time"] = None
        guild_data["seek_offset"] = 0
        guild_data["pause_position"] = None
        guild_data["seeking"] = False
        guild_data["last_activity"] = time.monotonic()
        for key in ("seeking_start_time", "source_speed", "listen_start", "paused_by", "interrupted",
                    "stream_retry", "force_advance"):
            guild_data.pop(key, None)
        guild_data["listened"] = 0.0

    def take_resume_position(self, guild_data: GuildData, song: Song) -> int:
        """Saved start position for this song (a restart or a parked song), consumed once."""
        resume = guild_data.get("resume_at")
        if not resume:
            return 0
        if resume.get("url") != song.webpage_url:
            # Another song plays first: keep the position for its own song, unless that left the queue.
            if all(s.webpage_url != resume.get("url") for s in guild_data["queue"]):
                guild_data.pop("resume_at", None)
            return 0
        guild_data.pop("resume_at", None)
        if song.is_live:
            return 0
        position = int(resume.get("position") or 0)
        return position if 0 < position < song.duration - 5 else 0

    def park_current(self, guild_id: int) -> Song | None:
        """Put the current song back at the front of the queue, remembering where it was, and stop tracking it."""
        guild_data = self.bot.get_guild_data(guild_id)
        song = guild_data.get("current")
        starting = guild_data.pop("starting", None)
        position = self.saved_position(guild_data)
        self.credit_listeners(guild_id)
        self.reset_playback_state(guild_data)
        if starting is not None:
            # A song was starting, so the one before it had ended or was left: the starting one is next.
            guild_data["queue"].insert(0, starting)
            return starting
        if song:
            # A song that had just ended is already listed as played; it is the next one now.
            played = guild_data["played"]
            if played and played[-1].webpage_url == song.webpage_url:
                played.pop()
            guild_data["queue"].insert(0, song)
            guild_data["resume_at"] = {"url": song.webpage_url, "position": position}
        return song

    def request_advance(self, guild_data: GuildData) -> None:
        """The next get_next_song moves on even in song-loop mode (a user skip)."""
        guild_data["force_advance"] = True

    # ── Notices ────────────────────────────────────────────────────────

    async def notify(self, guild_id: int, title: str, description: str, *, delete_after: float | None = None):
        await self.panel.notify(guild_id, title, description, delete_after=delete_after)

    # ── Now-playing panel ──────────────────────────────────────────────

    async def update_timestamps_task(self):
        current_time = asyncio.get_running_loop().time()

        for guild_id, guild_data in list(self.bot.guilds_data.items()):
            if self.bot.is_closed():
                return

            try:
                if guild_data.get("seeking_start_time") and current_time - guild_data["seeking_start_time"] > 15:
                    guild_data["seeking"] = False
                    del guild_data["seeking_start_time"]

                if not self._should_update_timestamp(guild_id, guild_data, current_time):
                    continue

                # One edit per guild at a time; a slow (rate-limited) edit makes the next tick wait for it.
                task = self._np_tasks.get(guild_id)
                if task and not task.done():
                    continue
                self._np_tasks[guild_id] = asyncio.create_task(self._update_single_timestamp(guild_id, current_time))
            except Exception as e:
                logger.error(f"Timer loop error for guild {guild_id}: {e}")

    def _should_update_timestamp(self, guild_id: int, guild_data: GuildData, current_time: float | None = None) -> bool:
        voice_client = guild_data.get("voice_client")
        return bool(
            guild_data.get("current")
            and guild_data.get("now_playing_message")
            and voice_client
            and guild_data.get("message_ready_for_timestamps", False)
            and (voice_client.is_playing() or voice_client.is_paused())
        )

    def _should_resend_message(self, guild_id: int) -> bool:
        guild_data = self.bot.get_guild_data(guild_id)

        message_sent_time = guild_data.get("now_playing_message_sent_time")
        if not message_sent_time:
            return False

        elapsed_since_send = time.monotonic() - message_sent_time
        return elapsed_since_send >= NOW_PLAYING_RESEND_SECONDS

    async def _update_single_timestamp(self, guild_id: int, current_time: float | None = None):
        guild_data = self.bot.get_guild_data(guild_id)
        try:
            message = guild_data.get("now_playing_message")
            if not message:
                return
            current_position = self.get_current_position(guild_id)
            is_paused = self.is_paused(guild_id)

            if self._should_resend_message(guild_id):
                # Not under the edit timeout: cancelling a send after the old panel was deleted could leave the new
                # one posted but untracked.
                await self.panel.send(
                    guild_id, current_position=current_position, is_paused=is_paused, replacing=message,
                )
                return

            async with asyncio.timeout(NOW_PLAYING_EDIT_TIMEOUT):
                view = self.panel.view(guild_id, current_position=current_position, is_paused=is_paused)
                if await self._safe_message_edit(message, view, guild_data):
                    guild_data["now_playing_view"] = view
        except TimeoutError:
            logger.debug(f"Now-playing update timed out for guild {guild_id}")
        except Exception as e:
            logger.warning(f"Timestamp update failed for guild {guild_id}: {e}")

    async def _safe_message_edit(self, message: discord.Message, view: discord.ui.LayoutView,
                                 guild_data: GuildData | None = None):
        """Edit the panel; a deleted or unreachable panel is dropped instead of being retried every second."""
        try:
            await message.edit(view=view)
            return True
        except (discord.NotFound, discord.Forbidden) as e:
            logger.info(f"Now-playing message is gone or not editable ({e.status}); no longer updating it")
            if guild_data is not None and guild_data.get("now_playing_message") is message:
                self.panel.forget(guild_data["guild_id"])
        except discord.HTTPException as e:
            logger.warning(f"Message edit failed: {e}")
        return False

    async def clear_now_playing(self, guild_id: int) -> None:
        """Delete the now-playing panel, so no frozen panel with dead buttons is left behind."""
        task = self._np_tasks.pop(guild_id, None)
        if task and not task.done():
            task.cancel()
        await self.panel.clear(guild_id)

    # ── Voice channel status ───────────────────────────────────────────

    def _set_voice_status(self, guild_id: int, song: Song) -> None:
        if not VOICE_CHANNEL_STATUS:
            return
        status = f"\U0001f3b5 {song.title}"
        if song.uploader and song.uploader != "Unknown":
            status += f" — {song.uploader}"
        self.bot.spawn(self._apply_voice_status(guild_id, status[:500]))

    def clear_voice_status(self, guild_id: int) -> None:
        """Remove the song status the bot set on its voice channel, if any."""
        if self.bot.get_guild_data(guild_id).get("voice_status_channel"):
            self.bot.spawn(self._apply_voice_status(guild_id, None))

    async def _apply_voice_status(self, guild_id: int, status: str | None) -> None:
        guild_data = self.bot.get_guild_data(guild_id)
        if status is None:
            channel = self.bot.get_channel(guild_data.get("voice_status_channel") or 0)
        else:
            channel = getattr(guild_data.get("voice_client"), "channel", None)
        if not isinstance(channel, discord.VoiceChannel):
            return
        if status is not None and status == guild_data.get("voice_status") and \
                channel.id == guild_data.get("voice_status_channel"):
            return
        permissions = channel.permissions_for(channel.guild.me)
        if not (permissions.set_voice_channel_status or permissions.manage_channels):
            return
        try:
            await channel.edit(status=status)
        except discord.HTTPException as e:
            logger.debug(f"Could not set the voice channel status in guild {guild_id}: {e}")
            return
        guild_data["voice_status"] = status
        guild_data["voice_status_channel"] = channel.id if status is not None else None

    # ── Background imports ─────────────────────────────────────────────

    def import_songs(
            self, guild_id: int, songs: AsyncIterator[dict], requested_by: str,
            on_first: Callable[[], Awaitable[object]],
    ) -> asyncio.Task:
        """Queue songs in the background as they are found; clear_queue cancels it. Result: (added, skipped)."""
        task = asyncio.create_task(self._queue_as_found(guild_id, songs, requested_by, on_first))
        tasks = self.bot.get_guild_data(guild_id).setdefault("import_tasks", set())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        task.add_done_callback(_log_import_failure)
        return task

    async def _queue_as_found(self, guild_id, songs, requested_by, on_first) -> tuple[int, int]:
        guild_data = self.bot.get_guild_data(guild_id)
        added = skipped = unsaved = 0
        existing = get_existing_urls(guild_data)
        last_save = time.monotonic()
        cancelled = False
        try:
            async for info in songs:
                if info["webpage_url"] in existing:
                    skipped += 1
                    continue
                # The room was worked out before the slow lookups; other adds may have filled it since.
                if len(guild_data["queue"]) >= MAX_QUEUE_SIZE:
                    break
                self.queue_service.add_song_to_queue(guild_id, Song({**info, "requested_by": requested_by}))
                existing.add(info["webpage_url"])
                added += 1
                unsaved += 1
                # Each save broadcasts the whole state to the Activity, so save in batches.
                if added == 1 or unsaved >= IMPORT_SAVE_EVERY or time.monotonic() - last_save >= 1.0:
                    await self.bot.save_guild_queue(guild_id)
                    unsaved, last_save = 0, time.monotonic()
                    existing = get_existing_urls(guild_data)
                if added == 1:
                    await on_first()
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if unsaved and not cancelled:
                await self.bot.save_guild_queue(guild_id)
        return added, skipped

    # ── Core playback loop ─────────────────────────────────────────────

    async def play_next(self, guild_id: int):
        guild_data = self.bot.get_guild_data(guild_id)

        async with guild_data["play_lock"]:
            gen = self.generation(guild_data)
            if guild_data.get("seeking", False) or self.bot.shutting_down:
                return

            voice_client = guild_data["voice_client"]
            if guild_data["current"] and voice_client and (voice_client.is_playing() or voice_client.is_paused()):
                return

            if not voice_client:
                guild_data["last_activity"] = time.monotonic()
                return

            if not voice_client.is_connected() and not await self._wait_for_voice(voice_client):
                # discord.py is still reconnecting; the voice watchdog restarts playback once it is back.
                logger.info(f"Voice not connected in guild {guild_id}, waiting for the reconnect")
                self.bot.watch_voice(guild_id)
                return

            skipped: list[Song] = []
            transient = 0
            loop_turned_off = False

            while True:
                if self.generation(guild_data) != gen:
                    return

                next_song = await self.queue_service.get_next_song(guild_id)
                if not next_song:
                    if await self._handle_empty_queue(guild_id, gen=gen):
                        continue
                    break

                outcome = await self._try_play(guild_id, next_song, gen)
                if outcome is _Outcome.PLAYED:
                    break
                if outcome is _Outcome.CANCELLED:
                    return
                if outcome in (_Outcome.NO_VOICE, _Outcome.BLOCKED):
                    if guild_data["loop_mode"] != "song":
                        guild_data["queue"].insert(0, next_song)
                    if outcome is _Outcome.NO_VOICE:
                        self.bot.watch_voice(guild_id)
                    else:
                        await self._stop_blocked(guild_id)
                    break

                loop_turned_off |= self._song_failed(guild_id, next_song)
                skipped.append(next_song)
                if outcome is _Outcome.FAIL:
                    transient += 1
                if len(skipped) >= MAX_SKIPPED_SONGS or transient >= MAX_TRANSIENT_FAILURES:
                    await self._handle_max_retries_exceeded(guild_id)
                    break

            if skipped:
                await self._notify_skipped(guild_id, skipped, loop_turned_off=loop_turned_off)

    async def _wait_for_voice(self, voice_client, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while not voice_client.is_connected():
            if time.monotonic() >= deadline or self.bot.shutting_down:
                return False
            await asyncio.sleep(0.25)
        return True

    async def _resolve_youtube_thumbnail(self, webpage_url: str) -> str:
        video_id = extract_youtube_id(webpage_url)
        if not video_id:
            return ""
        maxres = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
        try:
            async with self.bot.http_session.head(maxres, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status == 200:
                    return maxres
        except (aiohttp.ClientError, TimeoutError):
            pass
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

    @staticmethod
    def _refresh_metadata(song: Song, info: dict) -> None:
        fresh = Song(info)
        for field in ("title", "duration", "thumbnail", "uploader", "album", "track", "artist"):
            if info.get(field):
                setattr(song, field, getattr(fresh, field))
        if fresh.is_live:
            song.is_live = True

    @staticmethod
    async def _wait_until_available(info: dict) -> None:
        at = available_at(info)
        if at > time.time():
            logger.info(f"Waiting {at - time.time():.1f}s before streaming, as YouTube requires")
        await wait_until_available(at)

    async def _fresh_stream(self, song: Song) -> dict:
        """Extract a stream for the song; raises ExtractionError, or PlaybackError when there is none."""
        target = song.webpage_url or song.title
        info = await self.music_service.get_song_info(target, raise_errors=True)
        if isinstance(info, dict) and (page := info.get("webpage_url")) and page != target and info.get("url") == page:
            # A search or Spotify match only names the video; extract it for the stream.
            info = await self.music_service.get_song_info(page, raise_errors=True)
        if not isinstance(info, dict) or "entries" in info or not info.get("url") or \
                info["url"] == info.get("webpage_url"):
            raise PlaybackError(f"No stream available for {song.title}")
        return info

    async def _try_play(self, guild_id: int, song: Song, gen: int) -> _Outcome:
        guild_data = self.bot.get_guild_data(guild_id)
        seek = self.take_resume_position(guild_data, song)
        # In neither list while it starts: a leave that cancels the start parks it (park_current).
        guild_data["starting"] = song
        try:
            outcome = await self._play_once(guild_id, song, gen, seek)
        finally:
            if guild_data.get("starting") is song:
                guild_data.pop("starting", None)
        if seek and outcome in (_Outcome.NO_VOICE, _Outcome.BLOCKED):
            guild_data["resume_at"] = {"url": song.webpage_url, "position": seek}
        return outcome

    async def _play_once(self, guild_id: int, song: Song, gen: int, seek: int) -> _Outcome:
        guild_data = self.bot.get_guild_data(guild_id)
        cached_path = None
        if not song.is_live and song.webpage_url:
            cached_path = self.bot.audio_cache.get_cached_file(song.webpage_url)
        if cached_path:
            logger.info(f"Playing from cache: {song.title}")
            song.url = cached_path
            if not song.thumbnail and song.webpage_url:
                song.thumbnail = await self._resolve_youtube_thumbnail(song.webpage_url)
            return await self._start(guild_id, song, gen, local_file=True, seek=seek)

        try:
            logger.info(f"Extracting fresh stream URL for: {song.title}")
            info = await self._fresh_stream(song)
        except ExtractionError as e:
            logger.warning(f"Could not get a stream for {song.title}: {e.reason}")
            if e.bot_check:
                return _Outcome.BLOCKED
            return _Outcome.SKIP if e.permanent else _Outcome.FAIL
        except PlaybackError as e:
            logger.warning(str(e))
            return _Outcome.SKIP

        self._refresh_metadata(song, info)
        await self._wait_until_available(info)
        if self.generation(guild_data) != gen:
            return _Outcome.CANCELLED
        song.url = info["url"]
        return await self._start(guild_id, song, gen, seek=seek, headers=info.get("http_headers"))

    async def _start(self, guild_id: int, song: Song, gen: int, **kwargs) -> _Outcome:
        try:
            started = await self._start_playback(guild_id, song, gen=gen, **kwargs)
        except _VoiceLost:
            return _Outcome.NO_VOICE
        except _Cancelled:
            return _Outcome.CANCELLED
        return _Outcome.PLAYED if started else _Outcome.FAIL

    async def _extract_and_play_song(self, guild_id: int, song: Song, skip_count: int = 0) -> bool:
        """Play one song now (callers hold play_lock); a song that can't be played is reported and skipped."""
        guild_data = self.bot.get_guild_data(guild_id)
        outcome = await self._try_play(guild_id, song, self.generation(guild_data))
        if outcome is _Outcome.PLAYED:
            return True
        if outcome in (_Outcome.SKIP, _Outcome.FAIL):
            loop_turned_off = self._song_failed(guild_id, song)
            await self._notify_skipped(guild_id, [song], loop_turned_off=loop_turned_off)
        elif outcome is _Outcome.BLOCKED:
            guild_data["queue"].insert(0, song)
            await self._stop_blocked(guild_id)
        elif outcome is _Outcome.NO_VOICE:
            guild_data["queue"].insert(0, song)
            self.bot.watch_voice(guild_id)
        return False

    async def _start_playback(
            self, guild_id: int, song: Song, local_file: bool = False, *, seek: float = 0,
            headers: dict | None = None, gen: int | None = None,
    ) -> bool:
        """Start song.url on the voice client. Raises _VoiceLost when voice is not connected."""
        guild_data = self.bot.get_guild_data(guild_id)
        if gen is not None and gen != self.generation(guild_data):
            raise _Cancelled
        voice_client = guild_data["voice_client"]
        if not voice_client or not voice_client.is_connected():
            raise _VoiceLost

        try:
            source = self._make_source(guild_data, song.url, local_file=local_file, seek=seek, headers=headers)
        except (discord.ClientException, OSError) as e:
            logger.error(f"Error creating audio source for {song.title}: {e}")
            return False

        if voice_client.is_playing() or voice_client.is_paused():
            self._new_token(guild_data)
            voice_client.stop()
        token = self._new_token(guild_data)
        try:
            voice_client.play(source, after=self._make_after_playing(guild_id, token), bitrate=384)
        except discord.ClientException as e:
            source.cleanup()
            if not voice_client.is_connected():
                raise _VoiceLost from e
            logger.error(f"Could not start {song.title}: {e}")
            return False

        now = time.monotonic()
        guild_data["current"] = song
        # Numbers each play of a song, so the Activity can tell a repeat from a late message about the last one
        guild_data["song_seq"] = guild_data.get("song_seq", 0) + 1
        guild_data["seek_offset"] = seek
        guild_data["start_time"] = now
        guild_data["pause_position"] = None
        guild_data["source_speed"] = self.get_effective_speed(guild_data)
        guild_data["listened"] = 0.0
        guild_data["listen_start"] = now
        guild_data["last_activity"] = now
        # A new play (a song loop repeat too) gets its own stream recovery: recovery reopens via _replay_current.
        for key in ("paused_by", "interrupted", "force_advance", "stream_retry"):
            guild_data.pop(key, None)
        # Keep the 1 s updater off the old panel while the new one is sent
        guild_data["message_ready_for_timestamps"] = False

        guild = self.bot.get_guild(guild_id)
        logger.info(f"Now playing: {song.title} in guild: {guild.name if guild else 'unknown'} ({guild_id})")

        # Playback has started; a failure in the follow-up work below must not skip the song.
        try:
            self._cache_in_background(guild_data, song, local_file)
            await self.panel.refresh(guild_id)
            self._set_voice_status(guild_id, song)
            await self.bot.save_guild_queue(guild_id)
            self._start_autoplay_prefetch(guild_id, song)
        except Exception:
            logger.exception(f"Post-start work failed for {song.title}")
        return True

    def _cache_in_background(self, guild_data: GuildData, song: Song, local_file: bool) -> None:
        """Cache the song that started and the next one, so the next transition and seeks need no extraction."""
        cache = self.bot.audio_cache
        if not local_file and song.webpage_url and cacheable(song.duration, song.is_live):
            self.bot.spawn(cache.ensure_cached(song.webpage_url, song.title))
        if guild_data["loop_mode"] != "song" and guild_data["queue"]:
            upcoming = guild_data["queue"][0]
            if upcoming.webpage_url and cacheable(upcoming.duration, upcoming.is_live):
                self.bot.spawn(cache.ensure_cached(upcoming.webpage_url, upcoming.title))

    # ── After a song ends ──────────────────────────────────────────────

    def _make_after_playing(self, guild_id: int, token: int) -> Callable[[Exception | None], None]:
        loop = self.bot.loop

        def after_playing(error):
            # Audio thread: all guild state lives on the loop.
            # A closed loop means the bot is shutting down.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._on_playback_finished, guild_id, token, error)

        return after_playing

    def _on_playback_finished(self, guild_id: int, token: int, error: Exception | None) -> None:
        if self.bot.shutting_down:
            return
        guild_data = self.bot.guilds_data.get(guild_id)
        if guild_data is None or guild_data.get("play_token") != token:
            return
        if guild_data.get("seeking"):
            # Its replacement is being prepared; if that fails, the song ended for real (see _replay_current).
            guild_data["ended_while_seeking"] = token
            return
        if error is not None:
            logger.error(f"Player error in guild {guild_id}: {error}")

        song = guild_data.get("current")
        voice_client = guild_data.get("voice_client")
        if guild_data.get("intentional_disconnect"):
            if song:
                self._finish_song(guild_id, song)
            return
        if song and not guild_data.get("force_advance") and (voice_client is None or not voice_client.is_connected()):
            # The voice connection dropped, so the song did not end: keep it and resume once voice is back.
            self._mark_interrupted(guild_data)
            return
        if song and error is not None:
            if self._should_recover(guild_data, song):
                self.bot.spawn(self._recover_stream(guild_id, token))
                return
            if self.clock_position(guild_data) < STREAM_RECOVERY_MIN_REMAINING:
                # It never really played: report it like any other unplayable song.
                self.bot.spawn(self._cut_short_then_next(guild_id, song))
                return
        if song:
            self._finish_song(guild_id, song)
        self.bot.spawn(self.play_next(guild_id))

    async def _cut_short_then_next(self, guild_id: int, song: Song) -> None:
        self.credit_listeners(guild_id)
        await self._handle_song_skip(guild_id, song)
        await self.play_next(guild_id)

    def _mark_interrupted(self, guild_data: GuildData) -> None:
        if guild_data.get("interrupted") or not guild_data.get("current"):
            return
        lost_at = guild_data.get("voice_lost_at") or guild_data.get("voice_ok_at")
        position = self.clock_position(guild_data, at=lost_at)
        self._close_listen_segment(guild_data, at=lost_at)
        guild_data["interrupted"] = {"position": position}
        guild_data["seek_offset"] = position
        guild_data["start_time"] = None
        guild_data["pause_position"] = None
        logger.info(f"Playback of {guild_data['current'].title} interrupted at {position}s by a voice drop")

    def _should_recover(self, guild_data: GuildData, song: Song) -> bool:
        if guild_data.get("stream_retry") == song.webpage_url:
            return False
        if song.is_live or not song.duration:
            return True
        return self.clock_position(guild_data) < song.duration - STREAM_RECOVERY_MIN_REMAINING

    async def _recover_stream(self, guild_id: int, token: int) -> None:
        """FFmpeg gave up on the stream mid-song: reopen it once from a fresh URL at the same position."""
        guild_data = self.bot.get_guild_data(guild_id)
        async with guild_data["play_lock"]:
            song = guild_data.get("current")
            if not song or guild_data.get("play_token") != token:
                return
            guild_data["stream_retry"] = song.webpage_url
            position = 0 if song.is_live else self.clock_position(guild_data)
            if song.url and not song.url.startswith("http"):
                self.bot.audio_cache.remove_cached(song.webpage_url)
            logger.info(f"Reopening the stream of {song.title} at {position}s")
            try:
                await self._replay_current(guild_id, position, fresh=True)
                return
            except PlaybackError as e:
                logger.warning(f"Could not reopen {song.title}: {e}")
        await self._cut_short_then_next(guild_id, song)

    async def resume_after_reconnect(self, guild_id: int) -> None:
        """Voice is connected again: continue an interrupted song, fix the clock, or start the queue."""
        guild_data = self.bot.get_guild_data(guild_id)
        voice_client = guild_data.get("voice_client")
        if not voice_client or not voice_client.is_connected() or self.bot.shutting_down:
            return
        lost_at = guild_data.pop("voice_lost_at", None)

        if guild_data.get("interrupted") and guild_data.get("current"):
            position = guild_data.pop("interrupted")["position"]
            try:
                async with guild_data["play_lock"]:
                    await self._replay_current(guild_id, position)
                return
            except PlaybackError as e:
                logger.warning(f"Could not resume after reconnecting in guild {guild_id}: {e}")
                self.park_current(guild_id)
        elif voice_client.is_playing() or voice_client.is_paused():
            # discord.py's player waited out the outage; move the clock past the time nothing was sent.
            if lost_at and guild_data.get("start_time"):
                outage = time.monotonic() - lost_at
                guild_data["start_time"] += outage
                if guild_data.get("listen_start") is not None:
                    guild_data["listen_start"] += outage
            return

        if guild_data.get("queue") or guild_data.get("current"):
            await self.play_next(guild_id)

    # ── Shared voice actions (seek, restart, stop, skip, previous, leave) ──

    async def _replay_current(self, guild_id: int, position: int, *, keep_paused: bool = False,
                              fresh: bool = False) -> int:
        """Restart the current song at a position with the current speed and effect."""
        guild_data = self.bot.get_guild_data(guild_id)
        song = guild_data.get("current")
        voice_client = guild_data.get("voice_client")
        if not song:
            raise PlaybackError("No song is currently playing!")
        if not voice_client or not voice_client.is_connected():
            raise PlaybackError("Bot is not connected to voice!")

        guild_data["seeking"] = True
        guild_data["seeking_start_time"] = asyncio.get_running_loop().time()
        try:
            cached_path = None
            if not fresh and not song.is_live and song.webpage_url:
                cached_path = self.bot.audio_cache.get_cached_file(song.webpage_url)
            headers = None
            if cached_path:
                url = cached_path
            else:
                try:
                    info = await self._fresh_stream(song)
                except ExtractionError as e:
                    logger.warning(f"No fresh stream for {song.title}: {e.reason}")
                    raise PlaybackError("Couldn't get this song's stream right now. Try again in a moment.") from e
                await self._wait_until_available(info)
                url, headers = info["url"], info.get("http_headers")
            if not voice_client.is_connected():
                raise PlaybackError("Bot is not connected to voice!")

            try:
                source = self._make_source(guild_data, url, local_file=bool(cached_path), seek=position, headers=headers)
            except (discord.ClientException, OSError) as e:
                logger.error(f"Could not create the audio source for {song.title}: {e}")
                raise PlaybackError("Couldn't start the audio. Try again in a moment.") from e

            self._close_listen_segment(guild_data)
            token = self._new_token(guild_data)
            if voice_client.is_playing() or voice_client.is_paused():
                voice_client.stop()
            try:
                voice_client.play(source, after=self._make_after_playing(guild_id, token), bitrate=384)
            except discord.ClientException as e:
                # Voice dropped between the check and play(): resume here once it is back.
                source.cleanup()
                guild_data["interrupted"] = {"position": position}
                guild_data["seek_offset"] = position
                guild_data["start_time"] = None
                raise PlaybackError("Bot is not connected to voice!") from e

            now = time.monotonic()
            song.url = url
            guild_data["seek_offset"] = position
            guild_data["start_time"] = now
            guild_data["source_speed"] = self.get_effective_speed(guild_data)
            guild_data["pause_position"] = None
            guild_data.pop("interrupted", None)
            if keep_paused:
                voice_client.pause()
                guild_data["pause_position"] = position
            else:
                guild_data["listen_start"] = now
            if guild_data.get("now_playing_message"):
                guild_data["message_ready_for_timestamps"] = True
            return position
        finally:
            guild_data["seeking"] = False
            guild_data.pop("seeking_start_time", None)
            ended = guild_data.pop("ended_while_seeking", None)
            if ended is not None and ended == guild_data.get("play_token"):
                self._on_playback_finished(guild_id, ended, None)

    async def seek(self, guild_id: int, position: float) -> int:
        """Jump the voice song to a position; returns the position used. Raises PlaybackError."""
        guild_data = self.bot.get_guild_data(guild_id)
        song = guild_data.get("current")
        voice_client = guild_data.get("voice_client")
        if not song:
            raise PlaybackError("No song is currently playing!")
        if song.is_live or not song.duration:
            raise PlaybackError("Cannot seek in a livestream!")
        if not voice_client or not voice_client.is_connected():
            raise PlaybackError("Bot is not connected to voice!")
        if guild_data.get("seeking", False):
            raise PlaybackError("Already seeking, please wait...")
        position = max(0, min(int(position), song.duration - 5))
        position = await self._replay_current(guild_id, position, keep_paused=voice_client.is_paused())
        await self.bot.save_guild_queue(guild_id)
        return position

    async def reapply_audio_filters(self, guild_id: int) -> bool:
        """Rebuild the playing voice song so a speed or effect change applies now. False when nothing plays."""
        guild_data = self.bot.get_guild_data(guild_id)
        voice_client = guild_data.get("voice_client")
        song = guild_data.get("current")
        if not song or not voice_client or not (voice_client.is_playing() or voice_client.is_paused()):
            return False
        if guild_data.get("seeking"):
            return False
        position = 0 if song.is_live else self.get_current_position(guild_id)
        try:
            await self._replay_current(guild_id, position, keep_paused=voice_client.is_paused())
        except PlaybackError as e:
            logger.warning(f"Could not apply the new speed or effect in guild {guild_id}: {e}")
            return False
        await self.bot.save_guild_queue(guild_id)
        return True

    async def stop(self, guild_id: int) -> Song | None:
        """Stop playback and clear the queue; the song that was playing goes to history and stats."""
        guild_data = self.bot.get_guild_data(guild_id)
        self.bump_generation(guild_data)
        song = guild_data.get("current")
        if song:
            self._finish_song(guild_id, song)
        self.queue_service.clear_queue(guild_id)
        self.bot.cancel_autoplay(guild_data)
        self._new_token(guild_data)
        voice_client = guild_data.get("voice_client")
        if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
            voice_client.stop()
        self.reset_playback_state(guild_data)
        guild_data.pop("resume_at", None)
        await self.clear_now_playing(guild_id)
        self.clear_voice_status(guild_id)
        await self.bot.clear_guild_queue_from_db(guild_id)
        return song

    async def skip(self, guild_id: int) -> Song | None:
        """Move to the next song, in song-loop mode too. Returns the skipped song, or None if nothing plays."""
        guild_data = self.bot.get_guild_data(guild_id)
        song = guild_data.get("current")
        if not song:
            return None
        self.request_advance(guild_data)
        voice_client = guild_data.get("voice_client")
        if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
            voice_client.stop()  # after_playing records it and plays the next song
        else:
            self._finish_song(guild_id, song)
            self.bot.spawn(self.play_next(guild_id))
        return song

    async def skip_to(self, guild_id: int, index: int) -> Song:
        """Voice skip-to: play the index-th song of get_visible_queue; the songs before it count as played."""
        guild_data = self.bot.get_guild_data(guild_id)
        queue = guild_data["queue"]
        if index >= len(queue):
            # Queue loop: a song of the next round is one this round played already.
            previous = await self.play_previous(guild_id, index - len(queue))
            if previous is None:
                raise LookupError("That song is no longer in the queue")
            return previous
        target = self.queue_service.skip_ahead(guild_data, index)
        voice_client = guild_data.get("voice_client")
        if guild_data.get("current") and voice_client and (voice_client.is_playing() or voice_client.is_paused()):
            await self.skip(guild_id)
        else:
            self.bot.spawn(self.play_next(guild_id))
        return target

    async def play_previous(self, guild_id: int, index: int | None = None) -> Song | None:
        """Voice previous: go back to a played song (default: the one before the current song) and play it."""
        guild_data = self.bot.get_guild_data(guild_id)
        voice_client = guild_data.get("voice_client")
        if not voice_client or not voice_client.is_connected():
            return None
        previous = self.queue_service.step_back(guild_data, index)
        if not previous:
            return None

        self.bot.cancel_autoplay_prefetch(guild_data)
        self.credit_listeners(guild_id)
        self._new_token(guild_data)
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
        self.reset_playback_state(guild_data)

        async with guild_data["play_lock"]:
            played = await self._extract_and_play_song(guild_id, previous)
        if not played:
            await self.play_next(guild_id)
        await self.bot.save_guild_queue(guild_id)
        return previous

    async def leave(self, guild_id: int, *, clear_queue: bool = True, notice: str | None = None) -> bool:
        """Leave voice. clear_queue=False keeps the queue, with the current song back in front, for a later /join."""
        guild_data = self.bot.get_guild_data(guild_id)
        guild = self.bot.get_guild(guild_id)
        voice_client = guild_data.get("voice_client") or (guild.voice_client if guild else None)

        if clear_queue:
            await self.stop(guild_id)
        else:
            self.bump_generation(guild_data)
            self.bot.cancel_autoplay(guild_data)
            self._new_token(guild_data)
            self.park_current(guild_id)
            await self.clear_now_playing(guild_id)
            self.clear_voice_status(guild_id)
            await self.bot.save_guild_queue(guild_id)

        if voice_client:
            guild_data["intentional_disconnect"] = True
            try:
                await voice_client.disconnect(force=True)
            except (discord.HTTPException, discord.ClientException, OSError) as e:
                logger.debug(f"Error disconnecting voice client: {e}")
        guild_data["voice_client"] = None
        if notice:
            await self.notify(guild_id, "Left Voice", notice, delete_after=60)
        return voice_client is not None

    async def move_to(self, guild_id: int, channel) -> None:
        """Move the bot to another voice channel itself (no "Moved" notice is posted for it)."""
        guild_data = self.bot.get_guild_data(guild_id)
        voice_client = guild_data.get("voice_client")
        if not voice_client:
            raise PlaybackError("Bot is not connected to voice!")
        guild_data["self_move_at"] = time.monotonic()
        await voice_client.move_to(channel)

    def pause_for(self, guild_id: int, reason: str) -> bool:
        """Pause on the bot's own initiative (alone, server-muted); only those pauses are resumed automatically."""
        guild_data = self.bot.get_guild_data(guild_id)
        voice_client = guild_data.get("voice_client")
        if not voice_client or not voice_client.is_playing():
            return False
        self.handle_pause(guild_id)
        guild_data["paused_by"] = reason
        return True

    def resume_for(self, guild_id: int, reason: str) -> bool:
        guild_data = self.bot.get_guild_data(guild_id)
        voice_client = guild_data.get("voice_client")
        if guild_data.get("paused_by") != reason or not voice_client or not voice_client.is_paused():
            return False
        return self.handle_resume(guild_id)

    # ── Failures ───────────────────────────────────────────────────────

    def _song_failed(self, guild_id: int, song: Song) -> bool:
        """A song that couldn't play stays in the queue as played. Returns True when it turned song loop off."""
        guild_data = self.bot.get_guild_data(guild_id)
        self.queue_service.mark_played(guild_data, [song])
        if guild_data["loop_mode"] == "song":
            guild_data["loop_mode"] = "off"
            logger.info(f"Disabled song loop mode due to stream failure for {song.title}")
            return True
        return False

    async def _notify_skipped(self, guild_id: int, songs: list[Song], *, loop_turned_off: bool = False):
        if len(songs) == 1:
            text = f"**{md(songs[0].title)}** was skipped (stream unavailable)"
        else:
            names = ", ".join(f"**{md(s.title)}**" for s in songs[:5])
            more = f" and {len(songs) - 5} more" if len(songs) > 5 else ""
            text = f"Skipped {len(songs)} songs (stream unavailable): {names}{more}"
        if loop_turned_off:
            text += "\nSong loop was turned off."
        await self.notify(guild_id, "Song Skipped", text, delete_after=10 if len(songs) == 1 else 20)

    async def _handle_song_skip(self, guild_id: int, song: Song):
        loop_turned_off = self._song_failed(guild_id, song)
        await self._notify_skipped(guild_id, [song], loop_turned_off=loop_turned_off)

    async def _end_playback(self, guild_id: int) -> None:
        guild_data = self.bot.get_guild_data(guild_id)
        self.reset_playback_state(guild_data)
        await self.clear_now_playing(guild_id)
        self.clear_voice_status(guild_id)
        await self.bot.save_guild_queue(guild_id)

    async def _handle_max_retries_exceeded(self, guild_id: int):
        logger.error(f"Exhausted retry attempts for guild {guild_id}, stopping playback")
        await self._end_playback(guild_id)
        await self.notify(
            guild_id, "Playback Stopped",
            "Too many consecutive song failures. Please check your queue and try again.",
        )

    async def _stop_blocked(self, guild_id: int):
        logger.error(f"YouTube is refusing extractions (bot check) in guild {guild_id}, stopping playback")
        await self._end_playback(guild_id)
        await self.notify(
            guild_id, "Playback Stopped",
            "YouTube is refusing requests from the bot right now (\"confirm you're not a bot\"). "
            "The queue is kept; try again later.",
        )

    # ── Empty queue / autoplay ─────────────────────────────────────────

    async def _handle_empty_queue(self, guild_id: int, gen: int | None = None) -> bool:
        queue_service = self.queue_service
        guild_data = self.bot.get_guild_data(guild_id)
        # The song that just ended seeds autoplay; read it once, before any wait.
        seed = guild_data.get("current")

        if guild_data.get("autoplay", False) and seed:
            logger.info(f"Autoplay enabled for guild {guild_id}, checking for pre-fetched song...")

            try:
                # Wait for any in-flight pre-fetch to finish so pick_autoplay_song can
                # consume its result instead of refetching inline.
                prefetch_task = guild_data.get("autoplay_prefetch_task")
                if prefetch_task and not prefetch_task.done():
                    logger.info(f"Pre-fetch still running for guild {guild_id}, waiting up to 20s...")
                    done, _ = await asyncio.wait({prefetch_task}, timeout=20)
                    if not done:
                        logger.warning(f"Pre-fetch timed out for guild {guild_id}, cancelling")
                        prefetch_task.cancel()
                        guild_data["autoplay_prefetch_task"] = None

                next_song = await self.pick_autoplay_song(guild_id, seed)
                still_wanted = guild_data.get("autoplay") and (gen is None or gen == self.generation(guild_data))

                if next_song and still_wanted:
                    queue_service.add_song_to_queue(guild_id, next_song)
                    logger.info(f"Successfully queued autoplay song: {next_song.title}")
                    self.bot.spawn(self.notify(guild_id, "\U0001f3b5 Autoplay", f"Up next: **{md(next_song.title)}**",
                                               delete_after=15))
                    return True

                if not next_song:
                    logger.warning(f"Could not find any autoplay song for guild {guild_id}")

            except Exception:
                logger.exception(f"Autoplay error for guild {guild_id}")

        if gen is not None and gen != self.generation(guild_data):
            return False

        had_panel = guild_data.get("now_playing_message") is not None
        self.reset_playback_state(guild_data)
        await self.clear_now_playing(guild_id)
        self.clear_voice_status(guild_id)
        if had_panel:
            self.bot.spawn(self.notify(
                guild_id, "Queue Empty", "Add songs with `/play` or enable `/autoplay`", delete_after=30,
            ))

        await self.bot.save_guild_queue(guild_id)
        return False

    def _recent_played_titles(self, guild_data: GuildData) -> set:
        """Titles the queue played lately (unplayable songs included), windowed like history."""
        played = guild_data.get("played", [])
        return {self._normalize_title(s.title) for s in played[-AUTOPLAY_REPEAT_WINDOW:]}

    def _autoplay_exclusions(self, guild_data: GuildData, seed: Song | None) -> tuple[set[str], set[str]]:
        """URLs and normalized titles autoplay must not pick: queued, recently played, and the seed."""
        recent = guild_data.get("history", [])[-AUTOPLAY_REPEAT_WINDOW:]
        urls = {s.webpage_url for s in guild_data.get("queue", [])}
        urls.update(s.webpage_url for s in recent)
        titles = {self._normalize_title(s.title) for s in recent}
        titles.update(self._recent_played_titles(guild_data))
        if seed:
            urls.add(seed.webpage_url)
            titles.add(self._normalize_title(seed.title))
        urls.discard("")
        return urls, titles

    def _is_repeat(self, song_data: dict, urls: set[str], titles: set[str]) -> bool:
        return song_data.get("webpage_url") in urls or self._normalize_title(song_data.get("title") or "") in titles

    async def _related_picks(self, guild_data: GuildData, seed: Song, limit: int = 1) -> list[Song]:
        urls, titles = self._autoplay_exclusions(guild_data, seed)
        related = await self.music_service.get_related_songs(
            seed, limit=limit, exclude_urls=urls, exclude=lambda match: self._is_repeat(match, urls, titles),
        )
        picks = []
        for song_data in related:
            if self._is_repeat(song_data, urls, titles):
                continue
            song = Song(song_data)
            song.requested_by = "Autoplay"
            picks.append(song)
        return picks

    async def pick_autoplay_song(self, guild_id: int, current_song: Song) -> Song | None:
        """Pick the next autoplay song with no side effects.

        Single source of the autoplay dedup logic, shared by the voice and
        Activity-only paths. Consumes ``guild_data["autoplay_prefetch"]`` (always a
        ``Song``; tolerated as a dict for safety) when it is not a duplicate,
        otherwise fetches related songs inline. Does NOT add to the queue, send a
        Discord embed, or otherwise mutate playback state.
        """
        guild_data = self.bot.get_guild_data(guild_id)
        urls, titles = self._autoplay_exclusions(guild_data, current_song)

        prefetched = guild_data.get("autoplay_prefetch")
        if prefetched:
            guild_data["autoplay_prefetch"] = None
            # autoplay_prefetch should always be a Song; tolerate a raw dict.
            p = prefetched if not isinstance(prefetched, dict) else Song(prefetched)
            if not self._is_repeat(p.to_dict(), urls, titles):
                return p

        if not current_song:
            return None
        picks = await self._related_picks(guild_data, current_song)
        return picks[0] if picks else None

    def _normalize_title(self, title: str) -> str:
        normalized = title.lower().strip()

        patterns = [
            r'\(official.*?\)',
            r'\[official.*?\]',
            r'\(audio\)',
            r'\[audio\]',
            r'\(music video\)',
            r'\[music video\]',
            r'\(lyrics?(?: video)?\)',
            r'\[lyrics?(?: video)?\]',
            r'\(visuali[sz]er\)',
            r'\[visuali[sz]er\]',
            r'\(hd\)',
            r'\[hd\]',
        ]

        for pattern in patterns:
            normalized = re.sub(pattern, '', normalized, flags=re.IGNORECASE)

        normalized = re.sub(r'[^\w\s]', '', normalized)
        normalized = re.sub(r'\s+', ' ', normalized).strip()

        return normalized

    def _start_autoplay_prefetch(self, guild_id: int, song: Song) -> None:
        guild_data = self.bot.get_guild_data(guild_id)
        # In a loop mode the queue never runs dry, so a pick would never be used.
        if not guild_data.get("autoplay") or guild_data["queue"] or guild_data.get("loop_mode", "off") != "off":
            return
        self.bot.cancel_autoplay_prefetch(guild_data)
        guild_data["autoplay_prefetch_task"] = asyncio.create_task(self._prefetch_autoplay_song(guild_id, song))
        logger.debug(f"Autoplay pre-fetch triggered for guild {guild_id}")

    async def _prefetch_autoplay_song(self, guild_id: int, current_song: Song):
        """Pre-fetch the next autoplay song in background while the current song plays."""
        guild_data = self.bot.get_guild_data(guild_id)

        try:
            logger.info(f"Starting autoplay pre-fetch for guild {guild_id}: '{current_song.title}'")
            picks = await self._related_picks(guild_data, current_song)
            if not guild_data.get("autoplay"):
                logger.info(f"Autoplay disabled during pre-fetch for guild {guild_id}, stopping")
                return
            if not picks:
                logger.warning(f"Autoplay pre-fetch could not find a suitable song for guild {guild_id}")
                return
            song = picks[0]
            guild_data["autoplay_prefetch"] = song
            logger.info(f"Autoplay pre-fetch complete for guild {guild_id}: '{song.title}'")
            if song.webpage_url and cacheable(song.duration, song.is_live):
                await self.bot.audio_cache.ensure_cached(song.webpage_url, song.title)

        except asyncio.CancelledError:
            logger.debug(f"Autoplay pre-fetch cancelled for guild {guild_id}")
            raise
        except Exception as e:
            logger.error(f"Autoplay pre-fetch error for guild {guild_id}: {e}")
        finally:
            if guild_data.get("autoplay_prefetch_task") is asyncio.current_task():
                guild_data["autoplay_prefetch_task"] = None
