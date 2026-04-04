import asyncio
import logging
import re
from datetime import datetime

import aiohttp
import discord

from config import COLOR, NOW_PLAYING_RESEND_SECONDS, AUDIO_EFFECTS
from models.song import Song
from services.music_service import MusicService
from services.queue_service import QueueService
from utils.helpers import format_duration, build_progress_bar, create_embed

logger = logging.getLogger(__name__)


class PlaybackService:
    def __init__(self, bot):
        self.bot = bot
        self.queue_service = QueueService(bot)
        self.music_service = MusicService(bot)

    # FFmpeg options builder (speed + effects)

    def _build_ffmpeg_options(self, guild_data: dict) -> dict:
        """Build FFmpeg options with audio filters for speed and effects."""
        base_before = self.bot.ffmpeg_options["before_options"]
        base_options = self.bot.ffmpeg_options["options"]

        filters = []
        speed = guild_data.get("speed", 1.0)
        effect = guild_data.get("audio_effect", "none")

        # Speed via atempo (preserves pitch)
        if speed != 1.0:
            # atempo only supports 0.5–100.0; for <0.5 chain two filters
            if speed < 0.5:
                filters.append(f"atempo=0.5,atempo={speed / 0.5:.4f}")
            else:
                filters.append(f"atempo={speed:.4f}")

        # Audio effect
        if effect != "none" and effect in AUDIO_EFFECTS:
            effect_filter = AUDIO_EFFECTS[effect]["filter"]
            if effect_filter:
                filters.append(effect_filter)

        if filters:
            filter_str = ",".join(filters)
            options = f'{base_options} -af "{filter_str}"'
        else:
            options = base_options

        return {"before_options": base_before, "options": options}

    def get_effective_speed(self, guild_data: dict) -> float:
        """Calculate the combined speed multiplier (atempo * effect speed)."""
        speed = guild_data.get("speed", 1.0)
        effect = guild_data.get("audio_effect", "none")
        effect_mult = AUDIO_EFFECTS.get(effect, {}).get("speed_mult", 1.0)
        return speed * effect_mult

    # ── Pause / resume ─────────────────────────────────────────────────

    def handle_pause(self, guild_id: int):
        guild_data = self.bot.get_guild_data(guild_id)
        if guild_data["voice_client"] and guild_data["voice_client"].is_playing():
            current_pos = self.get_current_position(guild_id)
            guild_data["pause_position"] = current_pos
            guild_data["voice_client"].pause()
            return True
        return False

    def handle_resume(self, guild_id: int):
        guild_data = self.bot.get_guild_data(guild_id)
        if guild_data["voice_client"] and guild_data["voice_client"].is_paused():
            if guild_data.get("pause_position") is not None:
                guild_data["seek_offset"] = guild_data["pause_position"]
                guild_data["start_time"] = datetime.now()
                guild_data["pause_position"] = None
            guild_data["voice_client"].resume()
            return True
        return False

    def get_current_position(self, guild_id: int) -> int:
        guild_data = self.bot.get_guild_data(guild_id)

        if guild_data.get("seeking", False):
            return guild_data.get("seek_offset", 0)

        if not guild_data["start_time"]:
            return guild_data["seek_offset"]

        voice_client = guild_data["voice_client"]
        if not voice_client:
            return guild_data["seek_offset"]

        effective_speed = self.get_effective_speed(guild_data)

        if voice_client.is_paused():
            if guild_data.get("pause_position") is not None:
                return guild_data["pause_position"]
            elapsed = (datetime.now() - guild_data["start_time"]).total_seconds()
            return int(elapsed * effective_speed) + guild_data["seek_offset"]

        if voice_client.is_playing():
            elapsed = (datetime.now() - guild_data["start_time"]).total_seconds()
            return int(elapsed * effective_speed) + guild_data["seek_offset"]

        return guild_data["seek_offset"]

    def is_paused(self, guild_id: int) -> bool:
        guild_data = self.bot.get_guild_data(guild_id)
        return guild_data["voice_client"] and guild_data["voice_client"].is_paused()

    # ── Timestamp updates ──────────────────────────────────────────────

    async def update_timestamps_task(self):
        current_time = asyncio.get_running_loop().time()

        for guild_id, guild_data in list(self.bot.guilds_data.items()):
            try:
                if self.bot.is_closed():
                    return

                if guild_data.get("seeking_start_time"):
                    if current_time - guild_data["seeking_start_time"] > 15:
                        guild_data["seeking"] = False
                        del guild_data["seeking_start_time"]

                if not self._should_update_timestamp(guild_id, guild_data, current_time):
                    continue

                asyncio.create_task(self._update_single_timestamp(guild_id, current_time))

            except Exception as e:
                logger.error(f"Timer loop error for guild {guild_id}: {e}")
                continue

    def _should_update_timestamp(self, guild_id: int, guild_data: dict, current_time: float) -> bool:
        return (
                guild_data.get("current")
                and guild_data.get("now_playing_message")
                and guild_data.get("voice_client")
                and guild_data.get("message_ready_for_timestamps", False)
                and (
                        guild_data["voice_client"].is_playing()
                        or guild_data["voice_client"].is_paused()
                )
                and not self._is_update_locked(guild_id, current_time)
        )

    def _is_update_locked(self, guild_id: int, current_time: float) -> bool:
        if guild_id not in self.bot.message_update_locks:
            return False

        lock_time = self.bot.message_update_locks[guild_id]
        if current_time - lock_time > 2.0:
            del self.bot.message_update_locks[guild_id]
            return False

        return True

    def _should_resend_message(self, guild_id: int) -> bool:
        guild_data = self.bot.get_guild_data(guild_id)

        message_sent_time = guild_data.get("now_playing_message_sent_time")
        if not message_sent_time:
            return False

        elapsed_since_send = (datetime.now() - message_sent_time).total_seconds()
        return elapsed_since_send >= NOW_PLAYING_RESEND_SECONDS

    async def _update_single_timestamp(self, guild_id: int, current_time: float):
        try:
            self.bot.message_update_locks[guild_id] = current_time

            guild_data = self.bot.get_guild_data(guild_id)

            if not await self._validate_message_cached(guild_id, current_time):
                return

            current_position = self.get_current_position(guild_id)
            is_paused = self.is_paused(guild_id)

            if self._should_resend_message(guild_id):
                music_cog = self.bot.get_cog("MusicCommands")
                if music_cog:
                    embed = self._build_timestamp_embed(guild_data, current_position, is_paused)
                    await music_cog.create_now_playing_message(guild_id, embed)
                return

            embed = self._build_timestamp_embed(guild_data, current_position, is_paused)
            await self._safe_message_edit(guild_data["now_playing_message"], embed)

        except Exception as e:
            logger.warning(f"Timestamp update failed for guild {guild_id}: {e}")
        finally:
            if guild_id in self.bot.message_update_locks:
                del self.bot.message_update_locks[guild_id]

    async def _validate_message_cached(self, guild_id: int, current_time: float) -> bool:
        guild_data = self.bot.get_guild_data(guild_id)
        message = guild_data.get("now_playing_message")

        if not message:
            return False

        cache_key = f"{guild_id}_{message.id}"
        cached = self.bot.message_validation_cache.get(cache_key)

        if cached and current_time - cached["time"] < 10.0:
            return cached["valid"]

        try:
            await message.fetch()
            self.bot.message_validation_cache[cache_key] = {
                "valid": True,
                "time": current_time,
            }
            return True
        except discord.NotFound:
            guild_data["now_playing_message"] = None
            guild_data["message_ready_for_timestamps"] = False
            self.bot.message_validation_cache[cache_key] = {
                "valid": False,
                "time": current_time,
            }
            return False
        except discord.HTTPException:
            return False

    # ── Now-playing embed builders ─────────────────────────────────────

    def _build_now_playing_description(
            self, guild_data: dict, current_position: int, is_paused: bool
    ) -> tuple:
        """Shared description builder used by both the initial embed and 1-second updates."""
        current = guild_data["current"]
        is_live = getattr(current, "is_live", False) or not current.duration
        voice_client = guild_data.get("voice_client")

        if is_paused:
            status, status_emoji = "Paused", "\u23f8\ufe0f"
        elif not voice_client or not voice_client.is_playing():
            status, status_emoji = "Stopped", "\u23f9\ufe0f"
        else:
            status, status_emoji = "Playing", "\U0001f3b5"

        title = f"{status_emoji} Now {status}"

        # Progress line
        if is_live:
            progress_line = f"`\U0001f534 LIVE — {format_duration(current_position)}`"
        else:
            progress = build_progress_bar(current_position, current.duration)
            progress_line = f"`{format_duration(current_position)} {progress} {format_duration(current.duration)}`"

        # Speed/effect indicators
        speed = guild_data.get("speed", 1.0)
        effect = guild_data.get("audio_effect", "none")
        modifiers = []
        if speed != 1.0:
            modifiers.append(f"\u23e9 Speed: {speed:.1f}x")
        if effect != "none":
            effect_name = AUDIO_EFFECTS.get(effect, {}).get("name", effect)
            modifiers.append(f"\U0001f3a7 Effect: {effect_name}")
        modifier_line = "\n".join(modifiers)

        description = (
            f"**{current.title}**\n"
            f"*by {current.uploader}*\n\n"
            f"{progress_line}\n\n"
            f"\U0001f50a Volume: {guild_data['volume']}%\n"
            f"\U0001f501 Loop: {guild_data['loop_mode'].title()}\n"
            f"\U0001f500 Shuffle: {'On' if guild_data['shuffle'] else 'Off'}\n"
            f"Autoplay: {'Enabled' if guild_data['autoplay'] else 'Disabled'}\n"
        )
        if modifier_line:
            description += f"{modifier_line}\n"
        description += (
            f"\U0001f464 Requested by: {current.requested_by}\n"
            f"\U0001f4cb Queue length: {len(guild_data['queue'])}"
        )
        return title, description

    def _build_timestamp_embed(self, guild_data: dict, current_position: int, is_paused: bool) -> discord.Embed:
        current = guild_data["current"]
        title, description = self._build_now_playing_description(guild_data, current_position, is_paused)
        embed = discord.Embed(title=title, description=description, color=COLOR)
        embed.set_footer(
            text="Music Bot",
            icon_url=self.bot.user.avatar.url if self.bot.user.avatar else None,
        )
        if current.thumbnail:
            embed.set_thumbnail(url=current.thumbnail)
        return embed

    @staticmethod
    async def _safe_message_edit(message: discord.Message, embed: discord.Embed):
        try:
            await message.edit(embed=embed)
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            if "rate limited" not in str(e).lower():
                logger.warning(f"Message edit failed: {e}")
        except Exception as e:
            logger.warning(f"Unexpected message edit error: {e}")

    # ── Core playback loop ─────────────────────────────────────────────

    async def play_next(self, guild_id: int):
        queue_service = self.queue_service

        guild_data = self.bot.get_guild_data(guild_id)

        async with guild_data["play_lock"]:
            if guild_data.get("seeking", False):
                return

            if guild_data["current"] and guild_data["voice_client"]:
                if (
                        guild_data["voice_client"].is_playing()
                        or guild_data["voice_client"].is_paused()
                ):
                    return

            if (
                    not guild_data["voice_client"]
                    or not guild_data["voice_client"].is_connected()
            ):
                logger.info(
                    f"Voice client disconnected for guild {guild_id}, stopping playback"
                )
                guild_data["current"] = None
                guild_data["position"] = 0
                guild_data["start_time"] = None
                guild_data["seeking"] = False
                guild_data["last_activity"] = datetime.now()
                return

            max_skip_attempts = 10
            skip_count = 0

            while skip_count < max_skip_attempts:
                next_song = await queue_service.get_next_song(guild_id)

                if not next_song:
                    autoplay_added = await self._handle_empty_queue(guild_id)

                    if autoplay_added:
                        continue
                    else:
                        return

                stream_success = await self._extract_and_play_song(guild_id, next_song, skip_count)

                if stream_success:
                    break

                skip_count += 1

            if skip_count >= max_skip_attempts:
                await self._handle_max_retries_exceeded(guild_id)

    async def _extract_and_play_song(self, guild_id: int, song: Song, skip_count: int) -> bool:
        guild_data = self.bot.get_guild_data(guild_id)
        max_retries = 3

        for attempt in range(max_retries):
            try:
                logger.info(f"Extracting fresh stream URL for: {song.title} (attempt {attempt + 1})")

                fresh_data = await self.bot.get_song_info(song.webpage_url)

                if not fresh_data or not fresh_data.get("url"):
                    raise Exception(f"No stream URL available for {song.title}")

                try:
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                        async with session.head(fresh_data["url"]) as response:
                            if response.status not in [200, 206]:
                                raise Exception(f"Stream URL returned status {response.status}")
                except Exception as e:
                    logger.warning(f"URL validation failed: {e}")

                song.url = fresh_data["url"]
                if fresh_data.get("title"):
                    song.title = fresh_data["title"]
                if fresh_data.get("duration"):
                    song.duration = fresh_data["duration"]
                if fresh_data.get("thumbnail"):
                    song.thumbnail = fresh_data["thumbnail"]
                if fresh_data.get("uploader"):
                    song.uploader = fresh_data["uploader"]
                # Propagate livestream flag
                if fresh_data.get("is_live"):
                    song.is_live = True

                return await self._start_playback(guild_id, song)

            except Exception as e:
                logger.error(f"Error extracting stream URL (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                continue

        await self._handle_song_skip(guild_id, song)
        return False

    async def _start_playback(self, guild_id: int, song: Song) -> bool:
        queue_service = self.queue_service

        guild_data = self.bot.get_guild_data(guild_id)

        try:
            if guild_data["voice_client"].is_playing() or guild_data["voice_client"].is_paused():
                guild_data["voice_client"].stop()
                await asyncio.sleep(0.3)

            # Build FFmpeg options with speed/effect filters
            ffmpeg_opts = self._build_ffmpeg_options(guild_data)

            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(song.url, **ffmpeg_opts),
                volume=guild_data["volume"] / 100,
            )

            def after_playing(error):
                try:
                    if error:
                        logger.error(f"Player error: {error}")

                        if "Connection" in str(error) or "1006" in str(error):
                            logger.warning(f"Connection error detected in guild {guild_id}")
                    else:
                        if guild_data["current"] and not guild_data.get("seeking", False):
                            queue_service.add_to_history(guild_id, guild_data["current"])

                            # Record listening stats for the requester
                            self._record_stat_from_callback(guild_id, guild_data)

                    if not guild_data.get("seeking", False):
                        coro = self.play_next(guild_id)
                        fut = asyncio.run_coroutine_threadsafe(coro, self.bot.loop)

                        def handle_future_result(future):
                            try:
                                future.result()
                            except Exception as e:
                                logger.error(f"Error in play_next callback: {e}")

                        fut.add_done_callback(handle_future_result)

                except Exception as e:
                    logger.error(f"Error in after_playing callback: {e}")

            guild_data["current"] = song
            guild_data["seek_offset"] = 0
            guild_data["position"] = 0
            guild_data["start_time"] = datetime.now()
            guild_data["last_activity"] = datetime.now()

            guild_data["voice_client"].play(source, after=after_playing, bitrate=384)

            await asyncio.sleep(0.2)

            music_cog = self.bot.get_cog("MusicCommands")
            if music_cog:
                await music_cog.update_now_playing(guild_id)

            await self.bot.save_guild_queue(guild_id)

            guild = self.bot.get_guild(guild_id)
            guild_name = guild.name if guild else "unknown"
            logger.info(f"Now playing: {song.title} in guild: {guild_name} ({guild_id})")

            if guild_data.get("autoplay") and len(guild_data["queue"]) == 0:
                existing_prefetch = guild_data.get("autoplay_prefetch_task")
                if existing_prefetch and not existing_prefetch.done():
                    existing_prefetch.cancel()
                guild_data["autoplay_prefetch"] = None
                task = asyncio.create_task(self._prefetch_autoplay_song(guild_id, song))
                guild_data["autoplay_prefetch_task"] = task
                logger.debug(f"Autoplay pre-fetch triggered for guild {guild_id}")

            return True

        except Exception as e:
            logger.error(f"Error creating audio source for {song.title}: {e}")
            await self._handle_song_skip(guild_id, song)
            return False

    def _record_stat_from_callback(self, guild_id: int, guild_data: dict):
        """Fire-and-forget stat recording from the after_playing thread callback."""
        current = guild_data.get("current")
        if not current:
            return

        # Parse requester mention to user ID
        requester = current.requested_by or ""
        match = re.search(r'<@!?(\d+)>', requester)
        if not match:
            return

        user_id = int(match.group(1))

        # Calculate actual listening time instead of full song duration
        start_time = guild_data.get("start_time")
        if start_time:
            elapsed = (datetime.now() - start_time).total_seconds()
            effective_speed = self.get_effective_speed(guild_data)
            duration = int(elapsed * effective_speed)
        else:
            duration = current.duration if current.duration and current.duration > 0 else 0

        if duration > 0:
            # Cap at song duration to avoid over-counting
            if current.duration and current.duration > 0:
                duration = min(duration, current.duration)
            coro = self.bot.record_listening_stat(user_id, guild_id, current, duration)
            asyncio.run_coroutine_threadsafe(coro, self.bot.loop)

    # Empty queue / autoplay

    async def _handle_empty_queue(self, guild_id: int) -> bool:
        queue_service = self.queue_service
        guild_data = self.bot.get_guild_data(guild_id)

        if guild_data.get("autoplay", False) and guild_data.get("current"):
            logger.info(f"Autoplay enabled for guild {guild_id}, checking for pre-fetched song...")

            try:
                music_service = self.music_service
                history = guild_data.get("history", [])
                recent_titles = {
                    self._normalize_title(h.title) for h in history[-10:]
                } if history else set()
                if guild_data.get("current"):
                    recent_titles.add(self._normalize_title(guild_data["current"].title))

                next_song = None

                prefetch_task = guild_data.get("autoplay_prefetch_task")
                if prefetch_task and not prefetch_task.done():
                    logger.info(f"Pre-fetch still running for guild {guild_id}, waiting up to 20s...")
                    try:
                        done, _ = await asyncio.wait({prefetch_task}, timeout=20)
                        if not done:
                            logger.warning(f"Pre-fetch timed out for guild {guild_id}, cancelling")
                            prefetch_task.cancel()
                            guild_data["autoplay_prefetch_task"] = None
                    except Exception as wait_err:
                        logger.warning(f"Error waiting for pre-fetch: {wait_err}")

                existing_urls = {s.webpage_url for s in guild_data.get("queue", [])}
                if guild_data.get("current"):
                    existing_urls.add(guild_data["current"].webpage_url)

                prefetched = guild_data.get("autoplay_prefetch")
                if prefetched:
                    guild_data["autoplay_prefetch"] = None
                    if (prefetched.webpage_url not in existing_urls and
                            self._normalize_title(prefetched.title) not in recent_titles):
                        next_song = prefetched
                        logger.info(f"Using pre-fetched autoplay song: {prefetched.title}")
                    else:
                        logger.info(f"Pre-fetched song is now a duplicate, fetching inline")

                if not next_song:
                    logger.info(f"Fetching autoplay song inline for guild {guild_id}...")
                    for fetch_attempt in range(2):
                        related_songs = await music_service.get_related_songs(
                            guild_data["current"], limit=3
                        )
                        if not related_songs:
                            logger.warning(f"No related songs on inline attempt {fetch_attempt + 1}")
                            continue

                        for song_data in related_songs:
                            url = song_data.get("webpage_url")
                            title = song_data.get("title", "")
                            if (url not in existing_urls and
                                    self._normalize_title(title) not in recent_titles):
                                next_song = Song(song_data)
                                next_song.requested_by = "Autoplay"
                                logger.info(f"Inline autoplay found: {title}")
                                break

                        if next_song:
                            break

                if next_song:
                    queue_service.add_song_to_queue(guild_id, next_song)
                    logger.info(f"Successfully queued autoplay song: {next_song.title}")

                    try:
                        music_cog = self.bot.get_cog("MusicCommands")
                        if music_cog:
                            channel = await music_cog.get_music_channel(guild_id)
                            if channel:
                                embed = create_embed(
                                    "\U0001f3b5 Autoplay",
                                    f"Up next: **{next_song.title}**",
                                    COLOR,
                                    self.bot.user
                                )
                                await channel.send(embed=embed, delete_after=15)
                    except Exception as notify_error:
                        logger.warning(f"Failed to send autoplay notification: {notify_error}")

                    return True

                logger.warning(f"Could not find any autoplay song for guild {guild_id}")

            except Exception as e:
                logger.error(f"Autoplay error for guild {guild_id}: {e}", exc_info=True)

        guild_data["current"] = None
        guild_data["position"] = 0
        guild_data["start_time"] = None
        guild_data["last_activity"] = datetime.now()

        if guild_data.get("now_playing_message"):
            try:
                await guild_data["now_playing_message"].edit(
                    embed=create_embed(
                        "Queue Empty",
                        "Add songs with `/play` or enable `/autoplay`",
                        COLOR,
                        self.bot.user
                    ),
                    view=None
                )
            except Exception as e:
                logger.warning(f"Failed to edit now playing message on queue empty: {e}")
            guild_data["now_playing_message"] = None

        await self.bot.save_guild_queue(guild_id)
        return False

    def _normalize_title(self, title: str) -> str:
        normalized = title.lower().strip()

        patterns = [
            r'\(official.*?\)',
            r'\[official.*?\]',
            r'\(audio\)',
            r'\[audio\]',
            r'\(music video\)',
            r'\[music video\]',
            r'\(lyrics\)',
            r'\[lyrics\]',
            r'\(hd\)',
            r'\[hd\]',
        ]

        for pattern in patterns:
            normalized = re.sub(pattern, '', normalized, flags=re.IGNORECASE)

        normalized = re.sub(r'[^\w\s]', '', normalized)
        normalized = re.sub(r'\s+', ' ', normalized).strip()

        return normalized

    async def _handle_song_skip(self, guild_id: int, song: Song):
        queue_service = self.queue_service

        guild_data = self.bot.get_guild_data(guild_id)

        if guild_data["loop_mode"] != "song":
            queue_service.add_to_history(guild_id, song)

        try:
            music_cog = self.bot.get_cog("MusicCommands")
            if music_cog:
                channel = await music_cog.get_music_channel(guild_id)
                if channel:
                    skip_embed = create_embed(
                        "Song Skipped",
                        f"**{song.title}** was skipped (stream unavailable)",
                        COLOR,
                        self.bot.user
                    )
                    await channel.send(embed=skip_embed, delete_after=10)
        except Exception as e:
            logger.warning(f"Failed to send skip notification for '{song.title}': {e}")

        if guild_data["loop_mode"] == "song":
            guild_data["loop_mode"] = "off"
            logger.info(f"Disabled song loop mode due to stream failure for {song.title}")

    async def _handle_max_retries_exceeded(self, guild_id: int):
        guild_data = self.bot.get_guild_data(guild_id)
        logger.error(f"Exhausted retry attempts for guild {guild_id}, stopping playback")
        guild_data["current"] = None
        guild_data["start_time"] = None
        await self.bot.save_guild_queue(guild_id)

        try:
            music_cog = self.bot.get_cog("MusicCommands")
            if music_cog:
                channel = await music_cog.get_music_channel(guild_id)
                if channel:
                    error_embed = create_embed(
                        "Playback Stopped",
                        "Too many consecutive song failures. Please check your queue and try again.",
                        COLOR,
                        self.bot.user
                    )
                    await channel.send(embed=error_embed)
        except Exception as e:
            logger.warning(f"Failed to send max retries notification for guild {guild_id}: {e}")

    async def _prefetch_autoplay_song(self, guild_id: int, current_song: Song):
        """Pre-fetch the next autoplay song in background while the current song plays."""
        guild_data = self.bot.get_guild_data(guild_id)

        try:
            logger.info(f"Starting autoplay pre-fetch for guild {guild_id}: '{current_song.title}'")

            music_service = self.music_service

            existing_urls = {s.webpage_url for s in guild_data.get("queue", [])}
            history = guild_data.get("history", [])
            recent_titles = {
                self._normalize_title(h.title) for h in history[-10:]
            } if history else set()
            recent_titles.add(self._normalize_title(current_song.title))

            for attempt in range(2):
                if not guild_data.get("autoplay"):
                    logger.info(f"Autoplay disabled during pre-fetch for guild {guild_id}, stopping")
                    return

                related_songs = await music_service.get_related_songs(current_song, limit=3)

                for song_data in related_songs:
                    url = song_data.get("webpage_url")
                    title = song_data.get("title", "")
                    normalized_title = self._normalize_title(title)

                    if url in existing_urls or normalized_title in recent_titles:
                        continue

                    song = Song(song_data)
                    song.requested_by = "Autoplay"
                    guild_data["autoplay_prefetch"] = song
                    logger.info(f"Autoplay pre-fetch complete for guild {guild_id}: '{title}'")
                    return

                if attempt == 0:
                    logger.warning(f"Pre-fetch attempt 1 found no suitable songs for guild {guild_id}, retrying...")

            logger.warning(f"Autoplay pre-fetch could not find a suitable song for guild {guild_id}")

        except asyncio.CancelledError:
            logger.debug(f"Autoplay pre-fetch cancelled for guild {guild_id}")
            raise
        except Exception as e:
            logger.error(f"Autoplay pre-fetch error for guild {guild_id}: {e}")
        finally:
            if guild_data.get("autoplay_prefetch_task") is asyncio.current_task():
                guild_data["autoplay_prefetch_task"] = None
