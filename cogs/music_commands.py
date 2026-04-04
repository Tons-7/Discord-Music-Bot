import asyncio
import logging
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import COLOR, SONGS_PER_PAGE, AUDIO_EFFECTS, COMMAND_COOLDOWN, PLAY_COOLDOWN
from models.song import Song
from services.music_service import MusicService
from utils.ban_system import ban_user_id, unban_user_id
from utils.helpers import (
    format_duration,
    get_existing_urls,
    parse_time_to_seconds,
    interaction_check,
    create_embed,
)
from views.now_playing_controls import NowPlayingControls
from views.pagination import PaginationView
from views.song_select import SongSelectView

logger = logging.getLogger(__name__)


class MusicCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.music_service = bot._music_service
        self.playback_service = bot._playback_service
        self.queue_service = bot._playback_service.queue_service
        super().__init__()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await interaction_check(self, interaction)

    # DJ role check

    def _has_dj_permission(self, interaction: discord.Interaction) -> bool:
        """Check if user has DJ permissions (DJ role, admin, or no DJ role set)."""
        guild_data = self.bot.get_guild_data(interaction.guild.id)
        dj_role_id = guild_data.get("dj_role_id")

        if not dj_role_id:
            return True  # No DJ role set — everyone can use commands

        if interaction.user.guild_permissions.administrator:
            return True

        return any(role.id == dj_role_id for role in interaction.user.roles)

    async def _check_dj(self, interaction: discord.Interaction) -> bool:
        """Check DJ permission and send error if denied. Returns True if allowed."""
        if self._has_dj_permission(interaction):
            return True

        guild_data = self.bot.get_guild_data(interaction.guild.id)
        role = interaction.guild.get_role(guild_data.get("dj_role_id", 0))
        role_name = role.name if role else "DJ"
        embed = create_embed(
            "DJ Only",
            f"This command requires the **{role_name}** role or Administrator.",
            COLOR,
            self.bot.user,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return False

    # Voice channel checks

    async def check_voice_channel(
            self, interaction: discord.Interaction, allow_auto_join: bool = False
    ) -> bool:
        if not interaction.user.voice:
            embed = create_embed(
                "Error",
                "You must be in a voice channel to use this command!",
                COLOR,
                self.bot.user,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False

        voice_client = interaction.guild.voice_client

        if not voice_client:
            if allow_auto_join:
                return True
            embed = create_embed(
                "Error",
                "The bot is not connected to any voice channel!",
                COLOR,
                self.bot.user,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False

        if interaction.user.voice.channel != voice_client.channel:
            embed = create_embed(
                "Error",
                f"You must be in the same voice channel as the bot! Bot is in: {voice_client.channel.name}",
                COLOR,
                self.bot.user,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False

        return True

    async def ensure_voice_connection(self, interaction: discord.Interaction) -> bool:
        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if (
                not guild_data["voice_client"]
                or not guild_data["voice_client"].is_connected()
        ):
            if not interaction.user.voice:
                return False

            try:
                guild_data["voice_client"] = (
                    await interaction.user.voice.channel.connect()
                )
            except Exception as e:
                logger.error(f"Failed to connect to voice: {e}")
                return False

        return True

    async def get_music_channel(self, guild_id: int) -> Optional[discord.TextChannel]:
        guild_data = self.bot.get_guild_data(guild_id)
        guild = self.bot.get_guild(guild_id)

        if not guild:
            return None

        if guild_data.get("music_channel_id"):
            channel = guild.get_channel(guild_data["music_channel_id"])
            if channel and channel.permissions_for(guild.me).send_messages:
                return channel
            else:
                guild_data["music_channel_id"] = None

        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).send_messages:
                return channel

        return None

    async def create_now_playing_message(
            self, guild_id: int, embed: discord.Embed
    ) -> Optional[discord.Message]:
        try:
            channel = await self.get_music_channel(guild_id)
            if not channel:
                return None

            guild_data = self.bot.get_guild_data(guild_id)
            guild_data["message_ready_for_timestamps"] = False

            if guild_data.get("now_playing_message"):
                try:
                    await guild_data["now_playing_message"].delete()
                    await asyncio.sleep(0.5)
                except (discord.NotFound, discord.HTTPException):
                    pass
                guild_data["now_playing_message"] = None

            msg = await channel.send(embed=embed)
            guild_data["now_playing_message"] = msg
            guild_data["now_playing_message_sent_time"] = datetime.now()

            view = NowPlayingControls(self, guild_id)
            await msg.edit(view=view)
            await asyncio.sleep(0.2)
            guild_data["message_ready_for_timestamps"] = True

            return msg

        except Exception as e:
            logger.error(f"Failed to create now playing message: {e}")
            guild_data["now_playing_message"] = None
            guild_data["message_ready_for_timestamps"] = False
            return None

    async def update_now_playing(self, guild_id: int):
        guild_data = self.bot.get_guild_data(guild_id)
        current = guild_data["current"]

        if not current:
            if guild_data.get("now_playing_message"):
                try:
                    await guild_data["now_playing_message"].delete()
                except (discord.NotFound, discord.HTTPException):
                    pass
                guild_data["now_playing_message"] = None
                guild_data["message_ready_for_timestamps"] = False
            return

        current_position = self.playback_service.get_current_position(guild_id)
        is_paused = self.playback_service.is_paused(guild_id)
        title, description = self.playback_service._build_now_playing_description(
            guild_data, current_position, is_paused
        )
        embed = create_embed(title, description, COLOR, self.bot.user)

        if current.thumbnail:
            embed.set_thumbnail(url=current.thumbnail)

        await self.create_now_playing_message(guild_id, embed)

    async def play_previous(self, guild_id: int):
        guild_data = self.bot.get_guild_data(guild_id)

        if not guild_data["history"]:
            return False

        guild_data["autoplay_prefetch"] = None
        prefetch_task = guild_data.get("autoplay_prefetch_task")
        if prefetch_task and not prefetch_task.done():
            prefetch_task.cancel()
        guild_data["autoplay_prefetch_task"] = None

        if "history_position" not in guild_data:
            guild_data["history_position"] = len(guild_data["history"])

        guild_data["history_position"] -= 1

        if guild_data["history_position"] < 0:
            guild_data["history_position"] = 0
            return False

        previous_song = guild_data["history"][guild_data["history_position"]]

        if guild_data["current"]:
            guild_data["queue"].insert(0, guild_data["current"])

        guild_data["current"] = Song.from_dict(previous_song.to_dict())
        guild_data["seek_offset"] = 0
        guild_data["position"] = 0
        guild_data["start_time"] = None

        if guild_data["voice_client"] and (
                guild_data["voice_client"].is_playing()
                or guild_data["voice_client"].is_paused()
        ):
            guild_data["voice_client"].stop()

        await self.play_previous_song_directly(guild_id)
        return True

    async def play_previous_song_directly(self, guild_id: int):
        guild_data = self.bot.get_guild_data(guild_id)

        current_song = guild_data["current"]
        if not current_song:
            return

        if (
                not guild_data["voice_client"]
                or not guild_data["voice_client"].is_connected()
        ):
            logger.info(f"Voice client disconnected for guild {guild_id}, stopping playback")
            guild_data["current"] = None
            guild_data["start_time"] = None
            return

        # Clear current so _extract_and_play_song can set it fresh
        guild_data["current"] = None

        async with guild_data["play_lock"]:
            success = await self.playback_service._extract_and_play_song(guild_id, current_song, 0)

        if not success:
            await self.playback_service.play_next(guild_id)

    # ═══════════════════════════════════════════════════════════════════
    # Slash Commands
    # ═══════════════════════════════════════════════════════════════════

    @app_commands.command(name="join", description="Join your voice channel")
    async def join_slash(self, interaction: discord.Interaction):
        if not interaction.user.voice:
            embed = create_embed(
                "Error", "You must be in a voice channel!", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        voice_channel = interaction.user.voice.channel
        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if guild_data["voice_client"] and guild_data["voice_client"].is_connected():
            if guild_data["voice_client"].channel == voice_channel:
                embed = create_embed(
                    "Already Connected",
                    f"I'm already in {voice_channel.name}!",
                    COLOR,
                    self.bot.user,
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            else:
                await interaction.response.defer()
                try:
                    await guild_data["voice_client"].move_to(voice_channel)
                    embed = create_embed(
                        "Moved", f"Moved to {voice_channel.name}!", COLOR, self.bot.user
                    )
                    await interaction.followup.send(embed=embed)
                    return
                except Exception as e:
                    logger.error(f"Failed to move to voice channel: {e}")
                    embed = create_embed(
                        "Error",
                        "Failed to move to your voice channel!",
                        COLOR,
                        self.bot.user,
                    )
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    return

        await interaction.response.defer()
        try:
            guild_data["voice_client"] = await voice_channel.connect()
            guild_data["last_activity"] = datetime.now()

            embed = create_embed(
                "Connected", f"Joined {voice_channel.name}!", COLOR, self.bot.user
            )
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"Failed to connect to voice channel: {e}")
            embed = create_embed(
                "Error",
                "Failed to connect to your voice channel! Check my permissions.",
                COLOR,
                self.bot.user,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="play", description="Play a song or add it to queue"
    )
    @app_commands.describe(query="Song name, URL, or search term")
    @app_commands.checks.cooldown(1, PLAY_COOLDOWN)
    async def play_slash(self, interaction: discord.Interaction, query: str):
        if not await self.check_voice_channel(interaction, allow_auto_join=True):
            return

        # Defer immediately — voice connect/move below can exceed 3s timeout
        await interaction.response.defer()

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data.get("music_channel_id"):
            guild_data["music_channel_id"] = interaction.channel.id
            await self.bot.save_guild_music_channel(
                interaction.guild.id, interaction.channel.id
            )

        if not await self.ensure_voice_connection(interaction):
            embed = create_embed(
                "Error", "Failed to connect to voice channel!", COLOR, self.bot.user
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        if guild_data["voice_client"].channel != interaction.user.voice.channel:
            try:
                await guild_data["voice_client"].move_to(interaction.user.voice.channel)
            except Exception as e:
                logger.error(f"Failed to move to voice channel: {e}")

        searching_embed = create_embed(
            "Searching...", f"Looking for: `{query}`", COLOR, self.bot.user
        )
        await interaction.edit_original_response(embed=searching_embed)

        try:
            is_playlist = "playlist" in query.lower() and "youtube.com" in query.lower()

            if is_playlist:
                playlist_songs = (
                    await self.music_service.handle_youtube_playlist(query)
                )

                if not playlist_songs:
                    embed = create_embed(
                        "Error", "Could not process playlist!", COLOR, self.bot.user
                    )
                    await interaction.edit_original_response(embed=embed)
                    return

                existing_urls = get_existing_urls(guild_data)
                added_count = 0
                skipped_count = 0

                for data in playlist_songs:
                    if data.get("webpage_url") not in existing_urls:
                        song = Song(data)
                        song.requested_by = interaction.user.mention
                        self.queue_service.add_song_to_queue(interaction.guild.id, song)
                        existing_urls.add(data.get("webpage_url"))
                        added_count += 1
                    else:
                        skipped_count += 1

                if added_count > 0:
                    total_duration = self.queue_service.get_queue_duration(interaction.guild.id)
                    embed = create_embed(
                        "Playlist Added",
                        f"Added {added_count} songs to queue!\n"
                        f"Total queue duration: {format_duration(total_duration)}"
                        + (
                            f"\nSkipped {skipped_count} duplicates."
                            if skipped_count > 0
                            else ""
                        ),
                        COLOR,
                        self.bot.user,
                    )
                else:
                    embed = create_embed(
                        "No Songs Added",
                        "All songs were duplicates!",
                        COLOR,
                        self.bot.user,
                    )

                await interaction.edit_original_response(embed=embed)

            else:
                song_data = await self.bot.get_song_info_cached(query)

                if not song_data:
                    embed = create_embed(
                        "Error", "Could not find the song!", COLOR, self.bot.user
                    )
                    await interaction.edit_original_response(embed=embed)
                    return

                if isinstance(song_data, list):
                    existing_urls = get_existing_urls(guild_data)
                    added_count = 0
                    skipped_count = 0

                    for data in song_data:
                        if data.get("webpage_url") and data.get("title"):
                            if data["webpage_url"] not in existing_urls:
                                song = Song(data)
                                song.requested_by = interaction.user.mention
                                self.queue_service.add_song_to_queue(
                                    interaction.guild.id, song
                                )
                                existing_urls.add(data["webpage_url"])
                                added_count += 1
                            else:
                                skipped_count += 1

                    if added_count > 0:
                        embed = create_embed(
                            "Playlist Added",
                            f"Added {added_count} songs to queue!"
                            + (
                                f"\nSkipped {skipped_count} duplicates."
                                if skipped_count > 0
                                else ""
                            ),
                            COLOR,
                            self.bot.user,
                        )
                    else:
                        embed = create_embed(
                            "No Songs Added",
                            "All songs were duplicates!",
                            COLOR,
                            self.bot.user,
                        )

                    await interaction.edit_original_response(embed=embed)
                else:
                    if not song_data.get("webpage_url") or not song_data.get("title"):
                        embed = create_embed(
                            "Error", "Invalid song data!", COLOR, self.bot.user
                        )
                        await interaction.edit_original_response(embed=embed)
                        return

                    song_url = song_data["webpage_url"]

                    if (
                            guild_data["current"]
                            and guild_data["current"].webpage_url == song_url
                    ):
                        embed = create_embed(
                            "Duplicate Song",
                            "This song is currently playing!",
                            COLOR,
                            self.bot.user,
                        )
                        await interaction.edit_original_response(embed=embed)
                        return

                    for i, existing_song in enumerate(guild_data["queue"], 1):
                        if existing_song.webpage_url == song_url:
                            embed = create_embed(
                                "Duplicate Song",
                                f"This song is already in queue at position {i}!",
                                COLOR,
                                self.bot.user,
                            )
                            await interaction.edit_original_response(embed=embed)
                            return

                    song = Song(song_data)
                    song.requested_by = interaction.user.mention
                    self.queue_service.add_song_to_queue(interaction.guild.id, song)

                    position = len(guild_data["queue"])
                    wait_time = self.queue_service.get_estimated_wait_time(
                        interaction.guild.id, position
                    )

                    desc = f"{song}\n\nPosition in queue: {position}"
                    if wait_time > 0:
                        desc += f"\nEstimated wait: {format_duration(wait_time)}"

                    embed = create_embed(
                        "Added to Queue", desc, COLOR, self.bot.user,
                    )

                    if hasattr(song, "thumbnail") and song.thumbnail:
                        embed.set_thumbnail(url=song.thumbnail)

                    await interaction.edit_original_response(embed=embed)

            if guild_data["queue"]:
                asyncio.create_task(
                    self.playback_service.play_next(interaction.guild.id)
                )

            guild_data["last_activity"] = datetime.now()

        except Exception as e:
            logger.error(f"Error in play command: {e}")
            embed = create_embed(
                "Error", f"An error occurred: {str(e)}", COLOR, self.bot.user
            )
            await interaction.edit_original_response(embed=embed)

    @app_commands.command(name="pause", description="Pause the current song")
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def pause_slash(self, interaction: discord.Interaction):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        if self.playback_service.handle_pause(interaction.guild.id):
            embed = create_embed(
                "⏸️ Paused", "Music has been paused.", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, silent=True)
        else:
            embed = create_embed(
                "❌ Error", "Nothing is playing!", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed)

    @app_commands.command(name="resume", description="Resume the paused song")
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def resume_slash(self, interaction: discord.Interaction):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        if self.playback_service.handle_resume(interaction.guild.id):
            embed = create_embed(
                "▶️ Resumed", "Music has been resumed.", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, silent=True)
        else:
            embed = create_embed(
                "❌ Error", "Music is not paused!", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed)

    @app_commands.command(name="skip", description="Skip the current song")
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def skip_slash(self, interaction: discord.Interaction):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if guild_data["voice_client"] and (
                guild_data["voice_client"].is_playing()
                or guild_data["voice_client"].is_paused()
        ):
            skipped_song = (
                guild_data["current"].title if guild_data["current"] else "Unknown"
            )

            if guild_data["current"]:
                self.queue_service.add_to_history(
                    interaction.guild.id, guild_data["current"]
                )

            guild_data["voice_client"].stop()
            embed = create_embed(
                "Skipped", f"Skipped: **{skipped_song}**", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, silent=True)
        else:
            embed = create_embed("Error", "Nothing is playing!", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed)

    @app_commands.command(name="previous", description="Play the previous song")
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def previous_slash(self, interaction: discord.Interaction):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data["history"]:
            embed = create_embed(
                "❌ No Previous Songs",
                "No previous songs in history",
                COLOR,
                self.bot.user,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if "history_position" not in guild_data:
            guild_data["history_position"] = len(guild_data["history"])

        target_position = guild_data["history_position"] - 1

        if target_position < 0:
            embed = create_embed(
                "❌ No Previous Songs",
                "Already at the beginning of history",
                COLOR,
                self.bot.user,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        previous_song_title = guild_data["history"][target_position].title

        await interaction.response.defer(thinking=False)

        success = await self.play_previous(interaction.guild.id)

        if success:
            embed = create_embed(
                "⏮️ Previous Song",
                f"Playing previous: **{previous_song_title}**",
                COLOR,
                self.bot.user,
            )
            await self.bot.save_guild_queue(interaction.guild.id)
            await interaction.followup.send(embed=embed, silent=True)
        else:
            embed = create_embed(
                "❌ Error", "Could not play previous song!", COLOR, self.bot.user
            )
            await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="skipto", description="Skip to a specific song in the queue"
    )
    @app_commands.describe(position="Position in queue to skip to")
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def skipto_slash(self, interaction: discord.Interaction, position: int):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)
        visible_queue = self.queue_service.get_visible_queue(interaction.guild.id)

        if not visible_queue:
            embed = create_embed("Error", "Queue is empty!", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if position < 1 or position > len(visible_queue):
            embed = create_embed(
                "Error",
                f"Invalid position! Queue has {len(visible_queue)} songs.",
                COLOR,
                self.bot.user,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if position == 1:
            if guild_data["voice_client"] and (
                    guild_data["voice_client"].is_playing()
                    or guild_data["voice_client"].is_paused()
            ):
                if guild_data["current"]:
                    self.queue_service.add_to_history(
                        interaction.guild.id, guild_data["current"]
                    )

                guild_data["voice_client"].stop()
                embed = create_embed(
                    "Skipped to Song",
                    f"Skipped to: **{visible_queue[0].title}**",
                    COLOR,
                    self.bot.user,
                )
                await interaction.response.send_message(embed=embed, silent=True)
            else:
                embed = create_embed(
                    "Error", "Nothing is playing!", COLOR, self.bot.user
                )
                await interaction.response.send_message(embed=embed)
            return

        target_song = visible_queue[position - 1]
        primary_queue_size = len(guild_data["queue"])

        if guild_data["current"]:
            self.queue_service.add_to_history(
                interaction.guild.id, guild_data["current"]
            )

        if position <= primary_queue_size:
            songs_to_skip = position - 1

            for _ in range(songs_to_skip):
                if guild_data["queue"]:
                    skipped_song = guild_data["queue"].pop(0)
                    self.queue_service.add_to_history(
                        interaction.guild.id, skipped_song
                    )

        else:
            for song in guild_data["queue"]:
                self.queue_service.add_to_history(interaction.guild.id, song)
            guild_data["queue"].clear()

            target_song_copy = Song.from_dict(target_song.to_dict())
            target_song_copy.requested_by = interaction.user.mention
            guild_data["queue"].insert(0, target_song_copy)

        if guild_data["voice_client"] and (
                guild_data["voice_client"].is_playing()
                or guild_data["voice_client"].is_paused()
        ):
            guild_data["voice_client"].stop()

        embed = create_embed(
            "Skipped to Song",
            f"Skipped to: **{target_song.title}**",
            COLOR,
            self.bot.user,
        )
        await interaction.response.send_message(embed=embed, silent=True)

        await self.bot.save_guild_queue(interaction.guild.id)

    @app_commands.command(name="queue", description="Show the current queue")
    @app_commands.describe(page="Page number to view (optional)")
    async def queue_slash(self, interaction: discord.Interaction, page: int = 1):
        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data["current"] and not guild_data["queue"]:
            embed = create_embed("📋 Queue", "Queue is empty!", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, silent=True)
            return

        all_visible_songs = self.queue_service.get_visible_queue(interaction.guild.id)
        total_duration = self.queue_service.get_queue_duration(interaction.guild.id)

        total_pages = max(
            1, (len(all_visible_songs) + SONGS_PER_PAGE - 1) // SONGS_PER_PAGE
        )

        page = max(1, min(page, total_pages))

        pages = []
        for page_num in range(total_pages):
            start_idx = page_num * SONGS_PER_PAGE
            end_idx = start_idx + SONGS_PER_PAGE

            description = ""

            if guild_data["current"]:
                current = guild_data["current"]
                dur = format_duration(current.duration) if current.duration else "LIVE"
                description += f"**🎵 Now Playing:**\n{current} `[{dur}]` — {current.requested_by}\n\n"

            if all_visible_songs:
                description += f"**📋 Up Next:**\n"
                for i, song in enumerate(
                        all_visible_songs[start_idx:end_idx], start_idx + 1
                ):
                    dur = format_duration(song.duration) if song.duration else "LIVE"
                    description += f"`{i}.` {song} `[{dur}]` — {song.requested_by}\n"

            if not description.strip():
                description = "Queue is empty!"

            embed = create_embed(
                f"📋 Queue - Page {page_num + 1}/{total_pages}",
                description[:4000],
                COLOR,
                self.bot.user,
            )

            embed.add_field(
                name="Songs", value=str(len(all_visible_songs)), inline=True
            )
            embed.add_field(
                name="Duration", value=format_duration(total_duration) if total_duration > 0 else "—", inline=True
            )
            embed.add_field(
                name="Loop", value=guild_data["loop_mode"].title(), inline=True
            )

            pages.append(embed)

        view = PaginationView(pages, interaction.user)
        view.current_page = page - 1

        view.previous_button.disabled = view.current_page == 0
        view.next_button.disabled = view.current_page == len(pages) - 1

        await interaction.response.send_message(embed=pages[page - 1], view=view, silent=True)
        view.message = await interaction.original_response()

    @app_commands.command(
        name="volume", description="Set or show the volume (0-100)"
    )
    @app_commands.describe(level="Volume level (0-100)")
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def volume_slash(self, interaction: discord.Interaction, level: int = None):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if level is None:
            embed = create_embed(
                "🔊 Volume",
                f"Current volume: {guild_data['volume']}%",
                COLOR,
                self.bot.user,
            )
        else:
            level = max(0, min(100, level))
            guild_data["volume"] = level

            if guild_data["voice_client"] and guild_data["voice_client"].source:
                guild_data["voice_client"].source.volume = level / 100

            embed = create_embed(
                "🔊 Volume", f"Volume set to {level}%", COLOR, self.bot.user
            )
            await self.bot.save_guild_queue(interaction.guild.id)

        await interaction.response.send_message(embed=embed, silent=True)

    @app_commands.command(
        name="loop", description="Set loop mode (off/song/queue)"
    )
    @app_commands.describe(mode="Loop mode: off, song, or queue")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Off", value="off"),
            app_commands.Choice(name="Current Song", value="song"),
            app_commands.Choice(name="Queue", value="queue"),
        ]
    )
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def loop_slash(self, interaction: discord.Interaction, mode: str):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        self.queue_service.set_loop_mode(interaction.guild.id, mode)

        mode_emojis = {"off": "🔄", "song": "🔂", "queue": "🔁"}
        embed = create_embed(
            f"{mode_emojis.get(mode, '🔄')} Loop Mode",
            f"Loop mode set to: **{mode.title()}**",
            COLOR,
            self.bot.user,
        )

        await self.bot.save_guild_queue(interaction.guild.id)
        await interaction.response.send_message(embed=embed, silent=True)

    @app_commands.command(name="shuffle", description="Toggle shuffle mode")
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def shuffle_slash(self, interaction: discord.Interaction):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        shuffle_state = self.queue_service.toggle_shuffle(interaction.guild.id)

        embed = create_embed(
            "🔀 Shuffle",
            f"Shuffle mode: **{'On' if shuffle_state else 'Off'}**",
            COLOR,
            self.bot.user,
        )

        await self.bot.save_guild_queue(interaction.guild.id)
        await interaction.response.send_message(embed=embed, silent=True)

    @app_commands.command(
        name="stop", description="Stop playback and clear queue"
    )
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def stop_slash(self, interaction: discord.Interaction):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        self.queue_service.clear_queue(interaction.guild.id)
        guild_data["current"] = None
        guild_data["start_time"] = None

        if guild_data["voice_client"] and (
                guild_data["voice_client"].is_playing()
                or guild_data["voice_client"].is_paused()
        ):
            guild_data["voice_client"].stop()

        await self.bot.clear_guild_queue_from_db(interaction.guild.id)

        embed = create_embed(
            "Stopped", "Playback stopped and queue cleared.", COLOR, self.bot.user
        )
        await interaction.response.send_message(embed=embed, silent=True)

    @app_commands.command(
        name="clear", description="Clear the queue without stopping current song"
    )
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def clear_slash(self, interaction: discord.Interaction):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data["queue"] and not guild_data["loop_backup"]:
            embed = create_embed(
                "Error", "Queue is already empty!", COLOR, self.bot.user
            )
            await self.bot.save_guild_queue(interaction.guild.id)
            await interaction.response.send_message(embed=embed)
        else:
            self.queue_service.clear_queue(interaction.guild.id)
            embed = create_embed(
                "Queue Cleared", f"Removed all songs from queue.", COLOR, self.bot.user
            )
            await self.bot.save_guild_queue(interaction.guild.id)
            await interaction.response.send_message(embed=embed, silent=True)

    @app_commands.command(
        name="leave", description="Disconnect from voice channel"
    )
    async def leave_slash(self, interaction: discord.Interaction):
        if not await self.check_voice_channel(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if guild_data["voice_client"]:
            guild_data["intentional_disconnect"] = True

            await guild_data["voice_client"].disconnect()
            guild_data["voice_client"] = None
            self.queue_service.clear_queue(interaction.guild.id)
            guild_data["current"] = None
            guild_data["start_time"] = None

            await self.bot.clear_guild_queue_from_db(interaction.guild.id)

            embed = create_embed(
                "Disconnected", "Left the voice channel.", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, silent=True)
        else:
            embed = create_embed(
                "Error", "Not connected to a voice channel!", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="nowplaying", description="Show the currently playing song"
    )
    async def nowplaying_slash(self, interaction: discord.Interaction):
        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data["current"]:
            embed = create_embed(
                "❌ Nothing Playing",
                "No song is currently playing!",
                COLOR,
                self.bot.user,
            )
            await interaction.response.send_message(embed=embed)
            return

        current = guild_data["current"]
        current_position = self.playback_service.get_current_position(
            interaction.guild.id
        )
        is_paused = self.playback_service.is_paused(interaction.guild.id)

        title, description = self.playback_service._build_now_playing_description(
            guild_data, current_position, is_paused
        )

        embed = create_embed(title, description, COLOR, self.bot.user)

        if current.thumbnail:
            embed.set_thumbnail(url=current.thumbnail)

        await interaction.response.send_message(embed=embed, silent=True)

    @app_commands.command(
        name="remove", description="Remove a song from the queue permanently"
    )
    @app_commands.describe(position="Position of the song to remove (1-based)")
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def remove_slash(self, interaction: discord.Interaction, position: int):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        visible_songs = self.queue_service.get_visible_queue(interaction.guild.id)

        if not visible_songs:
            embed = create_embed("Error", "Queue is empty!", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed)
            return

        if position < 1 or position > len(visible_songs):
            embed = create_embed(
                "Error",
                f"Invalid position! Visible queue has {len(visible_songs)} songs.",
                COLOR,
                self.bot.user,
            )
            await interaction.response.send_message(embed=embed)
            return

        song_to_remove = visible_songs[position - 1]
        actual_queue_size = len(guild_data["queue"])
        removed_song = None

        if position <= actual_queue_size:
            removed_song = self.queue_service.remove_song_from_queue(
                interaction.guild.id, position - 1
            )

        guild_data["loop_backup"] = [
            song
            for song in guild_data["loop_backup"]
            if song.webpage_url != song_to_remove.webpage_url
        ]

        if not removed_song:
            removed_song = song_to_remove

        embed = create_embed(
            "Song Removed",
            f"Permanently removed: **{removed_song.title}**\n",
            COLOR,
            self.bot.user,
        )

        await self.bot.save_guild_queue(interaction.guild.id)
        await interaction.response.send_message(embed=embed, silent=True)

    @app_commands.command(
        name="move", description="Move a song to a different position in queue"
    )
    @app_commands.describe(
        from_pos="Current position of the song", to_pos="New position for the song"
    )
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def move_slash(
            self, interaction: discord.Interaction, from_pos: int, to_pos: int
    ):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data["queue"]:
            embed = create_embed("❌ Error", "Queue is empty!", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed)
            return

        queue_length = len(guild_data["queue"])
        if (
                from_pos < 1
                or from_pos > queue_length
                or to_pos < 1
                or to_pos > queue_length
        ):
            embed = create_embed(
                "❌ Error",
                f"Invalid position! Queue has {queue_length} songs.",
                COLOR,
                self.bot.user,
            )
            await interaction.response.send_message(embed=embed)
            return

        from_pos -= 1
        to_pos -= 1

        song = guild_data["queue"][from_pos]
        self.queue_service.move_song_in_queue(interaction.guild.id, from_pos, to_pos)

        embed = create_embed(
            "🔄 Song Moved",
            f"Moved **{song.title}**\nFrom position {from_pos + 1} to position {to_pos + 1}",
            COLOR,
            self.bot.user,
        )

        await self.bot.save_guild_queue(interaction.guild.id)
        await interaction.response.send_message(embed=embed, silent=True)

    @app_commands.command(
        name="search", description="Search for songs and choose which to play"
    )
    @app_commands.describe(
        query="Search term",
        results="Number of results to show (1-25, default 5)",
    )
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def search_slash(self, interaction: discord.Interaction, query: str, results: int = 5):
        if not await self.check_voice_channel(interaction, allow_auto_join=True):
            return

        results = max(1, min(25, results))

        searching_embed = create_embed(
            "Searching...", f"Looking for: `{query}`", COLOR, self.bot.user
        )
        await interaction.response.send_message(embed=searching_embed, silent=True)

        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(
                self.bot.executor,
                lambda: self.bot.ytdl.extract_info(
                    f"ytsearch{results}:{query}", download=False
                ),
            )

            if not data or "entries" not in data or not data["entries"]:
                embed = create_embed(
                    "❌ Error", "No results found!", COLOR, self.bot.user
                )
                await interaction.edit_original_response(embed=embed)
                return

            valid_entries = []
            for entry in data["entries"]:
                normalized = MusicService._normalize_youtube_entry(entry)
                if normalized:
                    valid_entries.append(normalized)

            if not valid_entries:
                embed = create_embed(
                    "❌ Error", "No valid results found!", COLOR, self.bot.user
                )
                await interaction.edit_original_response(embed=embed)
                return

            description = ""
            for i, entry in enumerate(valid_entries[:results], 1):
                duration = entry.get("duration", 0)
                if duration:
                    minutes, seconds = divmod(int(duration), 60)
                    duration_str = f"{minutes}:{seconds:02d}"
                else:
                    duration_str = "LIVE" if entry.get("is_live") else "0:00"

                title = entry["title"]
                if len(title) > 50:
                    title = title[:47] + "..."

                description += f"`{i}.` **{title}**\n"
                description += (
                    f"    by {entry.get('uploader', 'Unknown')} • {duration_str}\n\n"
                )

            embed = create_embed("🔍 Search Results", description, COLOR, self.bot.user)

            view = SongSelectView(valid_entries[:results], interaction.user, self)
            message = await interaction.edit_original_response(embed=embed, view=view)
            view.message = message

        except Exception as e:
            logger.error(f"Search command error: {e}")
            embed = create_embed(
                "❌ Error", "An error occurred during search.", COLOR, self.bot.user
            )
            await interaction.edit_original_response(embed=embed)

    async def process_selected_song(
            self, interaction: discord.Interaction, selected_song: dict
    ):
        try:
            guild_data = self.bot.get_guild_data(interaction.guild.id)

            if not guild_data.get("music_channel_id"):
                guild_data["music_channel_id"] = interaction.channel.id
                await self.bot.save_guild_music_channel(
                    interaction.guild.id, interaction.channel.id
                )

            if (
                    not guild_data["voice_client"]
                    or not guild_data["voice_client"].is_connected()
            ):
                if not interaction.user.voice:
                    embed = create_embed(
                        "Error", "You must be in a voice channel!", COLOR, self.bot.user
                    )
                    await interaction.edit_original_response(embed=embed, view=None)
                    return

                try:
                    guild_data["voice_client"] = (
                        await interaction.user.voice.channel.connect()
                    )
                except Exception as e:
                    logger.error(f"Failed to connect to voice: {e}")
                    embed = create_embed(
                        "Error",
                        "Failed to connect to voice channel!",
                        COLOR,
                        self.bot.user,
                    )
                    await interaction.edit_original_response(embed=embed, view=None)
                    return
            elif interaction.user.voice and guild_data["voice_client"].channel != interaction.user.voice.channel:
                try:
                    await guild_data["voice_client"].move_to(
                        interaction.user.voice.channel
                    )
                except Exception as e:
                    logger.error(f"Failed to move to voice channel: {e}")

            song = Song(selected_song)
            song.requested_by = interaction.user.mention

            existing_urls = get_existing_urls(guild_data)
            if song.webpage_url in existing_urls:
                embed = create_embed(
                    "Duplicate Song",
                    "This song is already in queue or playing!",
                    COLOR,
                    self.bot.user,
                )
                await interaction.edit_original_response(embed=embed, view=None)
                return

            self.queue_service.add_song_to_queue(interaction.guild.id, song)

            if (
                    not guild_data["voice_client"].is_playing()
                    and not guild_data["current"]
            ):
                await self.playback_service.play_next(interaction.guild.id)
                embed = create_embed("🎵 Now Playing", str(song), COLOR, self.bot.user)
            else:
                position = len(guild_data["queue"])
                embed = create_embed(
                    "📋 Added to Queue",
                    f"{song}\n\nPosition in queue: {position}",
                    COLOR,
                    self.bot.user,
                )

            if song.thumbnail:
                embed.set_thumbnail(url=song.thumbnail)

            await interaction.edit_original_response(embed=embed, view=None)
            guild_data["last_activity"] = datetime.now()
            await self.bot.save_guild_queue(interaction.guild.id)

        except Exception as e:
            logger.error(f"Error processing selected song: {e}")
            embed = create_embed(
                "❌ Error", "Failed to add song to queue.", COLOR, self.bot.user
            )
            try:
                await interaction.edit_original_response(embed=embed, view=None)
            except discord.HTTPException:
                pass

    @app_commands.command(
        name="setmusicchannel", description="Set the channel for music messages"
    )
    async def set_music_channel_slash(self, interaction: discord.Interaction):
        guild_data = self.bot.get_guild_data(interaction.guild.id)

        old_channel_id = guild_data.get("music_channel_id")
        guild_data["music_channel_id"] = interaction.channel.id
        await self.bot.save_guild_music_channel(
            interaction.guild.id, interaction.channel.id
        )

        if old_channel_id and old_channel_id != interaction.channel.id:
            old_channel = interaction.guild.get_channel(old_channel_id)
            if old_channel:
                embed = create_embed(
                    "📺 Music Channel Updated",
                    f"Music messages moved from {old_channel.mention} to {interaction.channel.mention}",
                    COLOR,
                    self.bot.user,
                )
            else:
                embed = create_embed(
                    "📺 Music Channel Set",
                    f"Music messages will now be sent to {interaction.channel.mention}",
                    COLOR,
                    self.bot.user,
                )
        else:
            embed = create_embed(
                "📺 Music Channel Set",
                f"Music messages will now be sent to {interaction.channel.mention}",
                COLOR,
                self.bot.user,
            )

        await interaction.response.send_message(embed=embed, silent=True)

    @app_commands.command(
        name="seek", description="Seek to a specific position in the current song"
    )
    @app_commands.describe(
        position="Time position (e.g., '1:30', '90', '2:15')"
    )
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def seek_slash(self, interaction: discord.Interaction, position: str):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data["current"]:
            embed = create_embed(
                "Error", "No song is currently playing!", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Block seek on livestreams
        if not guild_data["current"].duration or guild_data["current"].duration == 0:
            embed = create_embed(
                "Error", "Cannot seek in a livestream!", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if (
                not guild_data["voice_client"]
                or not guild_data["voice_client"].is_connected()
        ):
            embed = create_embed(
                "Error", "Bot is not connected to voice!", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if not (
                guild_data["voice_client"].is_playing()
                or guild_data["voice_client"].is_paused()
        ):
            if guild_data["current"]:
                try:
                    await self.playback_service.play_next(interaction.guild.id)
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logger.warning(f"Failed to restart stalled playback before seek: {e}")

            if not (
                    guild_data["voice_client"].is_playing()
                    or guild_data["voice_client"].is_paused()
            ):
                embed = create_embed(
                    "Error", "No song is currently playing!", COLOR, self.bot.user
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

        try:
            seek_seconds = parse_time_to_seconds(position)
        except ValueError as e:
            embed = create_embed("Error", str(e), COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        current_song = guild_data["current"]
        if seek_seconds < 0:
            seek_seconds = 0
        elif current_song.duration > 0 and seek_seconds >= current_song.duration - 5:
            seek_seconds = max(0, current_song.duration - 5)

        if guild_data.get("seeking", False):
            embed = create_embed(
                "Error", "Already seeking, please wait...", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        was_paused = guild_data["voice_client"].is_paused()
        await interaction.response.defer()

        try:
            guild_data["seeking"] = True
            guild_data["seeking_start_time"] = asyncio.get_running_loop().time()

            seek_embed = create_embed(
                "Seeking...",
                f"Seeking to {format_duration(seek_seconds)} in **{current_song.title}**",
                COLOR,
                self.bot.user,
            )
            await interaction.followup.send(embed=seek_embed)

            if (
                    guild_data["voice_client"].is_playing()
                    or guild_data["voice_client"].is_paused()
            ):
                guild_data["voice_client"].stop()

            await asyncio.sleep(0.2)

            fresh_data = None
            for attempt in range(3):
                try:
                    fresh_data = await self.bot.get_song_info_cached(
                        current_song.webpage_url
                    )
                    if fresh_data and fresh_data.get("url"):
                        break
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logger.warning(
                        f"Stream extraction attempt {attempt + 1} failed: {e}"
                    )
                    if attempt < 2:
                        await asyncio.sleep(1)

            if not fresh_data or not fresh_data.get("url"):
                embed = create_embed(
                    "Error",
                    "Failed to seek - could not get fresh stream URL",
                    COLOR,
                    self.bot.user,
                )
                await interaction.edit_original_response(embed=embed)
                return

            # Build FFmpeg options with seek + speed/effects
            ffmpeg_opts = self.playback_service._build_ffmpeg_options(guild_data)

            seek_strategies = [
                {
                    "before_options": f"-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -ss {seek_seconds} -nostdin -user_agent 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0'",
                    "options": ffmpeg_opts["options"].replace(self.bot.ffmpeg_options["options"],
                                                              "").strip() or "-vn -bufsize 1024k",
                },
                {
                    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin -user_agent 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0'",
                    "options": f"-vn -ss {seek_seconds} -bufsize 1024k",
                },
                {
                    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin",
                    "options": "-vn",
                },
            ]

            # For strategy 0 & 1, prepend the audio filter if we have effects/speed
            base_opts = self.bot.ffmpeg_options["options"]
            extra_af = ffmpeg_opts["options"].replace(base_opts, "").strip()
            if extra_af:
                seek_strategies[0]["options"] = f"-vn -bufsize 1024k {extra_af}"
                seek_strategies[1]["options"] = f"-vn -ss {seek_seconds} -bufsize 1024k {extra_af}"

            source = None
            strategy_used = 0

            for i, ffmpeg_options in enumerate(seek_strategies):
                try:
                    source = discord.PCMVolumeTransformer(
                        discord.FFmpegPCMAudio(fresh_data["url"], **ffmpeg_options),
                        volume=guild_data["volume"] / 100,
                    )
                    strategy_used = i
                    break
                except Exception as e:
                    logger.warning(f"Seek strategy {i + 1} failed: {e}")
                    if i < len(seek_strategies) - 1:
                        continue
                    else:
                        raise e

            if not source:
                embed = create_embed(
                    "Error",
                    "Failed to seek - stream format not supported",
                    COLOR,
                    self.bot.user,
                )
                await interaction.edit_original_response(embed=embed)
                return

            guild_data["seek_offset"] = seek_seconds if strategy_used <= 1 else 0
            guild_data["start_time"] = datetime.now()

            def after_seeking(error):
                if error:
                    logger.error(f"Seek player error: {error}")
                else:
                    if guild_data["current"] and not guild_data.get("seeking", False):
                        self.queue_service.add_to_history(
                            interaction.guild.id, guild_data["current"]
                        )

                if not guild_data.get("seeking", False):
                    coro = self.playback_service.play_next(interaction.guild.id)
                    fut = asyncio.run_coroutine_threadsafe(coro, self.bot.loop)

                    def _handle_seek_callback(future):
                        try:
                            future.result()
                        except Exception as cb_err:
                            logger.error(f"Error in play_next after seek: {cb_err}")

                    fut.add_done_callback(_handle_seek_callback)

            guild_data["voice_client"].play(source, after=after_seeking, bitrate=384)

            if was_paused:
                await asyncio.sleep(0.2)
                guild_data["voice_client"].pause()
                guild_data["pause_position"] = seek_seconds

            if strategy_used <= 1:
                result_desc = f"Moved to {format_duration(seek_seconds)} in **{current_song.title}**"
                if strategy_used == 1:
                    result_desc += " (strategy 2)"
            else:
                result_desc = f"Could not seek — playing **{current_song.title}** from the beginning"
                logger.warning(f"Seek strategy 3 used for guild {interaction.guild.id}: playing from start")

            success_embed = create_embed("Seeked", result_desc, COLOR, self.bot.user)
            await interaction.edit_original_response(embed=success_embed)

            guild_data["message_ready_for_timestamps"] = True

        except Exception as e:
            logger.error(f"Seek error: {e}")
            guild_data["seek_offset"] = 0
            guild_data["start_time"] = datetime.now()
            embed = create_embed(
                "Seek Failed",
                "Could not seek, restarted song from beginning",
                COLOR,
                self.bot.user,
            )
            await interaction.edit_original_response(embed=embed)
            asyncio.create_task(self.playback_service.play_next(interaction.guild.id))
        finally:
            guild_data["seeking"] = False
            if "seeking_start_time" in guild_data:
                del guild_data["seeking_start_time"]

    @app_commands.command(
        name="autoplay",
        description="Toggle autoplay mode (automatically plays related songs when queue ends)"
    )
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def autoplay_slash(self, interaction: discord.Interaction):
        if not await self.check_voice_channel(interaction):
            return
        await interaction.response.defer()

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not self.bot.lastfm:
            embed = create_embed(
                "Autoplay Unavailable",
                "Autoplay requires Last.fm integration, which is not configured",
                COLOR,
                self.bot.user
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        guild_data["autoplay"] = not guild_data.get("autoplay", False)

        if not guild_data["autoplay"]:
            prefetch_task = guild_data.get("autoplay_prefetch_task")
            if prefetch_task and not prefetch_task.done():
                prefetch_task.cancel()
            guild_data["autoplay_prefetch"] = None
            guild_data["autoplay_prefetch_task"] = None

        status = "enabled" if guild_data["autoplay"] else "disabled"

        embed = create_embed(
            f"Autoplay {status.title()}",
            f"Autoplay has been **{status}**.\n" +
            ("The bot will automatically add related songs when the queue ends" if guild_data["autoplay"]
             else "The bot will stop when the queue ends"),
            COLOR,
            self.bot.user
        )

        await interaction.followup.send(embed=embed, silent=True)
        await self.bot.save_guild_queue(interaction.guild.id)

    # Speed, Effects, Lyrics, Favorites, Stats, DJ, Queue Search

    @app_commands.command(name="speed", description="Set playback speed (0.5x to 2.0x)")
    @app_commands.describe(rate="Playback speed multiplier (0.5 - 2.0)")
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def speed_slash(self, interaction: discord.Interaction, rate: float):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        rate = max(0.5, min(2.0, rate))
        guild_data = self.bot.get_guild_data(interaction.guild.id)
        guild_data["speed"] = rate

        embed = create_embed(
            "⏩ Playback Speed",
            f"Speed set to **{rate:.1f}x**\n"
            "The change takes effect on the **next song** (or use `/seek` to re-apply now).",
            COLOR,
            self.bot.user,
        )
        await self.bot.save_guild_queue(interaction.guild.id)
        await interaction.response.send_message(embed=embed, silent=True)

    @app_commands.command(name="effects", description="Apply an audio effect")
    @app_commands.describe(effect="Choose an audio effect")
    @app_commands.choices(
        effect=[
            app_commands.Choice(name="None (reset)", value="none"),
            app_commands.Choice(name="Bass Boost", value="bass_boost"),
            app_commands.Choice(name="Nightcore", value="nightcore"),
            app_commands.Choice(name="Vaporwave", value="vaporwave"),
            app_commands.Choice(name="Treble Boost", value="treble_boost"),
            app_commands.Choice(name="8D Audio", value="8d"),
        ]
    )
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def effects_slash(self, interaction: discord.Interaction, effect: str):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)
        guild_data["audio_effect"] = effect

        effect_name = AUDIO_EFFECTS.get(effect, {}).get("name", effect)
        embed = create_embed(
            "🎧 Audio Effect",
            f"Effect set to **{effect_name}**\n"
            "The change takes effect on the **next song** (or use `/seek` to re-apply now).",
            COLOR,
            self.bot.user,
        )
        await self.bot.save_guild_queue(interaction.guild.id)
        await interaction.response.send_message(embed=embed, silent=True)

    @app_commands.command(name="lyrics", description="Show lyrics for the current song")
    @app_commands.checks.cooldown(1, 5)
    async def lyrics_slash(self, interaction: discord.Interaction):
        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data.get("current"):
            embed = create_embed("Error", "No song is currently playing!", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await interaction.response.defer()

        current = guild_data["current"]

        from utils.lyrics import fetch_lyrics
        result = await fetch_lyrics(current.title, current.uploader)

        if not result or not result.get("lyrics"):
            embed = create_embed(
                "Lyrics Not Found",
                f"Could not find lyrics for **{current.title}**",
                COLOR,
                self.bot.user,
            )
            await interaction.followup.send(embed=embed)
            return

        lyrics_text = result["lyrics"]
        title = result.get("title", current.title)
        artist = result.get("artist", current.uploader)

        # Split lyrics into pages for readability
        max_len = 600
        if len(lyrics_text) <= max_len:
            embed = create_embed(
                f"🎤 {title} — {artist}",
                lyrics_text,
                COLOR,
                self.bot.user,
            )
            await interaction.followup.send(embed=embed)
        else:
            # Paginate by splitting at verse breaks, falling back to single lines
            pages = []
            chunks = []
            current_chunk = ""
            # Try verse breaks first, fall back to individual lines
            has_verses = "\n\n" in lyrics_text
            parts = lyrics_text.split("\n\n") if has_verses else lyrics_text.split("\n")
            separator = "\n\n" if has_verses else "\n"
            sep_len = len(separator)
            for part in parts:
                if len(current_chunk) + len(part) + sep_len > max_len and current_chunk:
                    chunks.append(current_chunk)
                    current_chunk = part
                else:
                    current_chunk = current_chunk + separator + part if current_chunk else part
            if current_chunk:
                chunks.append(current_chunk)
            for idx, chunk in enumerate(chunks):
                embed = create_embed(
                    f"🎤 {title} — {artist} ({idx + 1}/{len(chunks)})",
                    chunk,
                    COLOR,
                    self.bot.user,
                )
                pages.append(embed)

            view = PaginationView(pages, interaction.user)
            await interaction.followup.send(embed=pages[0], view=view)
            view.message = await interaction.original_response()

    @app_commands.command(name="favorite", description="Add the current song to your favorites")
    async def favorite_slash(self, interaction: discord.Interaction):
        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data.get("current"):
            embed = create_embed("Error", "No song is currently playing!", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        current = guild_data["current"]
        success = await self.bot.add_favorite(interaction.user.id, current)

        if success:
            embed = create_embed(
                "❤️ Favorited",
                f"Added {current.linked_title} to your favorites!",
                COLOR,
                self.bot.user,
            )
        else:
            embed = create_embed(
                "Already Favorited",
                f"{current.linked_title} is already in your favorites.",
                COLOR,
                self.bot.user,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="favorites", description="Show or play your favorite songs")
    @app_commands.describe(action="What to do", position="Song position (for play/remove)")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Show all", value="show"),
            app_commands.Choice(name="Play a song", value="play"),
            app_commands.Choice(name="Remove a song", value="remove"),
        ]
    )
    async def favorites_slash(
            self, interaction: discord.Interaction, action: str = "show", position: int = None
    ):
        favs = await self.bot.get_favorites(interaction.user.id)

        if not favs:
            embed = create_embed(
                "❤️ Favorites",
                "You have no favorites yet! Use `/favorite` while a song plays.",
                COLOR,
                self.bot.user,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if action == "show":
            description = ""
            for i, song_data in enumerate(favs, 1):
                title = song_data.get("title", "Unknown")
                uploader = song_data.get("uploader", "Unknown")
                description += f"`{i}.` **{title}** by {uploader}\n"

            embed = create_embed(
                f"❤️ Your Favorites ({len(favs)} songs)",
                description[:4000],
                COLOR,
                self.bot.user,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        elif action == "play":
            if position is None or position < 1 or position > len(favs):
                embed = create_embed(
                    "Error",
                    f"Please provide a valid position (1-{len(favs)}).",
                    COLOR,
                    self.bot.user,
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            if not await self.check_voice_channel(interaction, allow_auto_join=True):
                return

            await interaction.response.defer()

            if not await self.ensure_voice_connection(interaction):
                embed = create_embed("Error", "Failed to connect to voice!", COLOR, self.bot.user)
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            guild_data = self.bot.get_guild_data(interaction.guild.id)
            song_data = favs[position - 1]
            song = Song(song_data)
            song.requested_by = interaction.user.mention
            self.queue_service.add_song_to_queue(interaction.guild.id, song)

            if guild_data["voice_client"] and not guild_data["voice_client"].is_playing() and not guild_data.get(
                    "current"):
                asyncio.create_task(self.playback_service.play_next(interaction.guild.id))

            embed = create_embed(
                "❤️ Playing Favorite",
                f"Added {song.linked_title} to queue!",
                COLOR,
                self.bot.user,
            )
            await interaction.followup.send(embed=embed, silent=True)

        elif action == "remove":
            if position is None or position < 1 or position > len(favs):
                embed = create_embed(
                    "Error",
                    f"Please provide a valid position (1-{len(favs)}).",
                    COLOR,
                    self.bot.user,
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            removed_title = favs[position - 1].get("title", "Unknown")
            success = await self.bot.remove_favorite(interaction.user.id, position)
            if success:
                embed = create_embed(
                    "Removed",
                    f"Removed **{removed_title}** from your favorites.",
                    COLOR,
                    self.bot.user,
                )
            else:
                embed = create_embed("Error", "Failed to remove.", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="stats", description="Show your listening statistics")
    async def stats_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        user_id = interaction.user.id
        guild_id = interaction.guild.id

        # Total listening time & play count
        totals = await self.bot.fetch_db_query(
            "SELECT COUNT(*), COALESCE(SUM(duration_seconds), 0) "
            "FROM user_stats WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        )
        play_count = totals[0][0] if totals else 0
        total_seconds = totals[0][1] if totals else 0

        if play_count == 0:
            embed = create_embed(
                "📊 Your Stats",
                "No listening history yet! Play some songs first.",
                COLOR,
                self.bot.user,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Top songs
        top_songs = await self.bot.fetch_db_query(
            "SELECT song_title, COUNT(*) as cnt FROM user_stats "
            "WHERE user_id = ? AND guild_id = ? "
            "GROUP BY song_title ORDER BY cnt DESC LIMIT 5",
            (user_id, guild_id),
        )

        # Top artists
        top_artists = await self.bot.fetch_db_query(
            "SELECT artist, COUNT(*) as cnt FROM user_stats "
            "WHERE user_id = ? AND guild_id = ? AND artist != '' "
            "GROUP BY artist ORDER BY cnt DESC LIMIT 5",
            (user_id, guild_id),
        )

        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60

        desc = f"**Total plays:** {play_count}\n"
        desc += f"**Total listening time:** {hours}h {minutes}m\n\n"

        if top_songs:
            desc += "**Top Songs:**\n"
            for i, (title, cnt) in enumerate(top_songs, 1):
                desc += f"`{i}.` {title} ({cnt} plays)\n"
            desc += "\n"

        if top_artists:
            desc += "**Top Artists:**\n"
            for i, (artist, cnt) in enumerate(top_artists, 1):
                desc += f"`{i}.` {artist} ({cnt} plays)\n"

        embed = create_embed("📊 Your Stats", desc, COLOR, self.bot.user)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="setdj", description="Set or remove the DJ role for this server")
    @app_commands.describe(role="The DJ role (leave empty to remove)")
    @app_commands.checks.has_permissions(administrator=True)
    async def setdj_slash(self, interaction: discord.Interaction, role: discord.Role = None):
        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if role:
            guild_data["dj_role_id"] = role.id
            await self.bot.save_guild_dj_role(interaction.guild.id, role.id)
            embed = create_embed(
                "🎧 DJ Role Set",
                f"DJ role set to **{role.name}**.\n"
                f"Only members with this role (or admins) can use skip, stop, volume, loop, shuffle, speed, effects, clear, remove, and move.",
                COLOR,
                self.bot.user,
            )
        else:
            guild_data["dj_role_id"] = None
            await self.bot.save_guild_dj_role(interaction.guild.id, None)
            embed = create_embed(
                "🎧 DJ Role Removed",
                "DJ role restriction removed. All members can use all commands.",
                COLOR,
                self.bot.user,
            )

        await interaction.response.send_message(embed=embed, silent=True)

    @app_commands.command(name="queuesearch", description="Search for a song in the queue")
    @app_commands.describe(query="Search term to find in queue")
    async def queuesearch_slash(self, interaction: discord.Interaction, query: str):
        results = self.queue_service.search_queue(interaction.guild.id, query)

        if not results:
            embed = create_embed(
                "🔍 Queue Search",
                f"No songs matching **{query}** found in queue.",
                COLOR,
                self.bot.user,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        description = ""
        for pos, song in results[:15]:
            dur = format_duration(song.duration) if song.duration else "LIVE"
            description += f"`{pos}.` {song.linked_title} by {song.uploader} `[{dur}]`\n"

        if len(results) > 15:
            description += f"\n*...and {len(results) - 15} more*"

        embed = create_embed(
            f"🔍 Queue Search: \"{query}\" ({len(results)} found)",
            description[:4000],
            COLOR,
            self.bot.user,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # Help

    @app_commands.command(
        name="help", description="Shows all available commands and how to use them"
    )
    async def help_slash(self, interaction: discord.Interaction):
        description = """**Music Bot Commands Guide**

        **Basic Commands:**
        `/join` - Join your voice channel
        `/play <query>` - Play a song or add it to queue (YouTube, Spotify, Apple Music, Tidal, or search)
        `/pause` - Pause the current song
        `/resume` - Resume the paused song
        `/skip` - Skip the current song
        `/previous` - Play the previous song from history
        `/autoplay` - Auto play related songs after queue ends
        `/stop` - Stop playback and clear queue
        `/leave` - Disconnect from voice channel

        **Queue Management:**
        `/queue [page]` - Show the current queue (with durations & requesters)
        `/clear` - Clear the queue without stopping current song
        `/remove <position>` - Remove a song from queue by position
        `/move <from_pos> <to_pos>` - Move a song to different position
        `/skipto <position>` - Skip to a specific song in queue
        `/queuesearch <query>` - Search for a song in the queue

        **Playback Controls:**
        `/volume [level]` - Set or show volume (0-100)
        `/loop <mode>` - Set loop mode: off, song, or queue
        `/shuffle` - Toggle shuffle mode
        `/seek <position>` - Seek to specific time (e.g., '1:30', '90')
        `/nowplaying` - Show currently playing song info
        `/speed <rate>` - Set playback speed (0.5x to 2.0x)
        `/effects <effect>` - Apply audio effect (bass boost, nightcore, vaporwave, etc.)
        `/lyrics` - Show lyrics for the current song

        **Search & Discovery:**
        `/search <query> [results]` - Search for songs (configurable result count, 1-25)
        `/history show [page]` - Show recently played songs
        `/history play <number>` - Play a song from history
        `/history add_all` - Add all songs from history to queue
        `/history clear` - Clear all history songs

        **Favorites:**
        `/favorite` - Add the current song to your favorites
        `/favorites show` - Show your favorite songs
        `/favorites play <position>` - Play a song from your favorites
        `/favorites remove <position>` - Remove a song from your favorites

        **Stats:**
        `/stats` - Show your listening statistics (top songs, artists, total time)

        **Playlist Commands: (your playlist(s) are server specific)**
        `/playlist create/add/remove/move/load/show/list/delete`
        `/playlist add-from-queue` - Add a song from current queue
        `/playlist add-all-queue` - Add entire queue to playlist
        `/playlist collab-add <playlist> <user>` - Add a collaborator to your playlist
        `/playlist collab-remove <playlist> <user>` - Remove a collaborator
        `/playlist collab-list <playlist>` - List collaborators on a playlist
        `/playlist my-collabs` - Show playlists you collaborate on
        Use `collaborative: True` on add/remove/load/show to access collab playlists

        **Global Playlist Commands: (accessible from any server)**
        `/globalplaylist create/add/remove/move/load/show/list/delete`
        `/globalplaylist collab-add/collab-remove/collab-list/my-collabs`

        **Settings:**
        `/setmusicchannel` - Set the channel for music messages
        `/setdj [role]` - Set or remove DJ role (admin only)

        **Tips:**
        • Use button controls on the 'Now Playing' message
        • Supports YouTube, Spotify, Apple Music, Tidal, SoundCloud & search
        • Queue shows duration estimates and who requested each song
        • Use `/speed` and `/effects` for playback customization
        • Queue persists across bot restarts
        """

        embed = create_embed("Command Guide", description, COLOR, self.bot.user)
        await interaction.response.send_message(embed=embed, silent=True)

    # Error handler

    async def cog_app_command_error(
            self,
            interaction: discord.Interaction,
            error: app_commands.AppCommandError,
    ):
        if isinstance(error, app_commands.CommandOnCooldown):
            embed = create_embed(
                "⏰ Command on Cooldown",
                f"Try again in {error.retry_after:.1f} seconds.",
                COLOR,
                self.bot.user,
            )
        elif isinstance(error, app_commands.MissingPermissions):
            embed = create_embed(
                "❌ Missing Permissions",
                "You need Administrator permission to use this command.",
                COLOR,
                self.bot.user,
            )
        else:
            logger.error(f"Slash command error: {error}")
            embed = create_embed(
                "❌ Error",
                "An unexpected error occurred. Please try again.",
                COLOR,
                self.bot.user,
            )

        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.NotFound:
            logger.warning(f"Could not send error response — interaction expired: {error}")
        except Exception as e:
            logger.warning(f"Failed to send error response for command error: {e}")

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.NotOwner):
            return  # Silently ignore non-owners trying owner commands
        logger.error(f"Prefix command error: {error}")

    # Owner-only prefix commands (hidden)

    @commands.command(name="leaveguild")
    @commands.is_owner()
    async def leave_guild(self, ctx, guild_id: int = None):
        if guild_id:
            guild = self.bot.get_guild(guild_id)
        else:
            guild = ctx.guild

        if guild:
            await guild.leave()
            await ctx.send(f"✅ Left guild: {guild.name} ({guild.id})")
        else:
            await ctx.send("❌ Could not find that guild.")

    @commands.command(name="banuser")
    @commands.is_owner()
    async def ban_user(self, ctx, user: discord.User):
        if ban_user_id(user.id):
            await ctx.send(f"Banned {user.mention} ({user.id})")
        else:
            await ctx.send(f"{user.mention} ({user.id}) is already banned")

    @commands.command(name="unbanuser")
    @commands.is_owner()
    async def unban_user(self, ctx, user: discord.User):
        if unban_user_id(user.id):
            await ctx.send(f"Unbanned {user.mention} ({user.id})")
        else:
            await ctx.send(f"{user.mention} ({user.id}) was not banned")

    @commands.command(name="listbanned")
    @commands.is_owner()
    async def list_banned(self, ctx):
        import utils.ban_system as ban_sys
        if ban_sys._banned_cache is None:
            ban_sys._load_cache()

        banned_ids = sorted(str(uid) for uid in ban_sys._banned_cache)

        if not banned_ids:
            await ctx.send("No banned users.")
            return

        msg = "Banned users:\n" + "\n".join(banned_ids)
        await ctx.send(msg)
