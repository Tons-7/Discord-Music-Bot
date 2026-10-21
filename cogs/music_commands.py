import asyncio
import contextlib
import io
import logging
import sqlite3
import time

import discord
from discord import app_commands
from discord.ext import commands

from activity.permissions import dj_role_id, has_dj_permission
from config import (
    AUDIO_EFFECTS,
    COLOR,
    COMMAND_COOLDOWN,
    DEFAULT_SEARCH_RESULTS,
    MAX_QUEUE_SIZE,
    MAX_SEARCH_RESULTS,
    PLAY_COOLDOWN,
    SONGS_PER_PAGE,
)
from models.song import Song
from services.music_service import SPOTIFY_TRACK_LIMIT, track_collection
from services.playback_service import PlaybackError
from bot import FavoritesFull
from utils.ban_system import ban_user_id, banned_ids, unban_user_id
from utils.helpers import (
    create_embed,
    create_v2_embed,
    V2_TEXT_LIMIT,
    format_duration,
    get_existing_urls,
    clip,
    interaction_check,
    md,
    parse_time_to_seconds,
    reply_error,
    report_command_error,
)
from views.confirm import ConfirmView
from views.now_playing_controls import NowPlayingControls
from views.pagination import PaginationView
from views.song_select import SongSelectView

logger = logging.getLogger(__name__)

# Played songs listed above Now Playing on the first /queue page
RECENT_PLAYED_SHOWN = 3


class MusicCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.music_service = bot.music
        self.playback_service = bot.playback
        self.queue_service = bot.playback.queue_service
        super().__init__()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Notices and the now-playing panel fall back to where music commands are used.
        if interaction.guild_id and interaction.channel_id:
            self.bot.get_guild_data(interaction.guild_id)["command_channel_id"] = interaction.channel_id
        return await interaction_check(self, interaction)

    # Replies

    async def _fail(self, interaction: discord.Interaction, text: str, title: str = "Error"):
        await reply_error(self.bot, interaction, text, title)

    # DJ role check

    def _has_dj_permission(self, interaction: discord.Interaction) -> bool:
        """DJ role holders and administrators, or everyone when there is no DJ role (or it was deleted)."""
        return has_dj_permission(self.bot, interaction.guild, interaction.user)

    async def _check_dj(self, interaction: discord.Interaction) -> bool:
        """Check DJ permission and send error if denied. Returns True if allowed."""
        if self._has_dj_permission(interaction):
            return True
        role = interaction.guild.get_role(dj_role_id(self.bot, interaction.guild) or 0)
        role_name = md(role.name) if role else "DJ"
        await self._fail(interaction, f"This command requires the **{role_name}** role or Administrator.", "DJ Only")
        return False

    # Voice channel checks

    async def check_voice_channel(
            self, interaction: discord.Interaction, allow_auto_join: bool = False
    ) -> bool:
        if not interaction.user.voice:
            await self._fail(interaction, "You must be in a voice channel to use this command!")
            return False

        voice_client = interaction.guild.voice_client

        # Sync with discord.py's authoritative voice client
        if voice_client:
            guild_data = self.bot.get_guild_data(interaction.guild.id)
            guild_data["voice_client"] = voice_client

        if not voice_client:
            if allow_auto_join:
                return True
            await self._fail(interaction, "The bot is not connected to any voice channel!")
            return False

        if interaction.user.voice.channel != voice_client.channel:
            await self._fail(
                interaction,
                f"You must be in the same voice channel as the bot! Bot is in: {voice_client.channel.mention}",
            )
            return False

        return True

    @staticmethod
    def _join_problem(channel) -> str | None:
        """Why joining this voice channel would fail or play to nobody, or None."""
        guild = channel.guild
        if guild.afk_channel is not None and channel.id == guild.afk_channel.id:
            return "I can't play music in the AFK channel."
        permissions = channel.permissions_for(guild.me)
        if not permissions.connect:
            return f"I don't have permission to join {channel.mention}."
        # On a Stage the bot joins as audience and asks to speak (see MusicBot._try_unsuppress).
        if not isinstance(channel, discord.StageChannel) and not permissions.speak:
            return f"I don't have permission to speak in {channel.mention}."
        if channel.user_limit and len(channel.members) >= channel.user_limit and not permissions.move_members:
            return f"{channel.mention} is full."
        return None

    def _move_problem(self, interaction: discord.Interaction, voice_client) -> str | None:
        """Moving the bot away from people listening needs Move Members, Administrator or the DJ role."""
        if voice_client.channel == interaction.user.voice.channel:
            return None
        listeners = [m for m in voice_client.channel.members if not m.bot and m.id != interaction.user.id]
        if not listeners or not self.bot.get_guild_data(interaction.guild.id).get("current"):
            return None
        member = interaction.user
        if member.guild_permissions.move_members or member.guild_permissions.administrator:
            return None
        role_id = dj_role_id(self.bot, interaction.guild)
        if role_id and any(role.id == role_id for role in member.roles):
            return None
        count = len(listeners)
        return (
            f"I'm playing in {voice_client.channel.mention} for {count} listener{'s' if count != 1 else ''}. "
            "Join them, or ask a DJ to move me."
        )

    def _activity_conflict(self, guild_id: int, channel) -> str | None:
        """Joining voice ends Activity-only playback for viewers in other voice channels, so it is refused."""
        guild_data = self.bot.get_guild_data(guild_id)
        ws = self.bot.ws_manager
        if not guild_data.get("current") or guild_data.get("voice_client") or not ws or not ws.has_connections(guild_id):
            return None
        channels = {viewer.channel_id for viewer in ws.get_viewers(guild_id) if viewer.channel_id}
        if not channels or channel.id in channels:
            return None
        where = ", ".join(f"<#{channel_id}>" for channel_id in sorted(channels))
        return f"Music is playing in the Activity in {where}. Join that channel, or stop the Activity's music first."

    async def _take_over_activity(self, guild_id: int) -> None:
        """Voice takes over Activity-only playback: the song carries on in voice from where it was."""
        guild_data = self.bot.get_guild_data(guild_id)
        if not guild_data.get("current") or guild_data.get("voice_client"):
            return
        from activity.helpers import record_activity_listening
        await record_activity_listening(self.bot, self.bot.ws_manager, guild_id)
        self.playback_service.park_current(guild_id)

    async def connect_voice(self, interaction: discord.Interaction) -> str | None:
        """Join (or keep) the caller's voice channel. Returns why it failed, or None."""
        guild = interaction.guild
        guild_data = self.bot.get_guild_data(guild.id)

        existing_vc = guild.voice_client
        if existing_vc:
            guild_data["voice_client"] = existing_vc
            if existing_vc.is_connected() or await self.playback_service._wait_for_voice(existing_vc, timeout=5):
                return None
            # Reconnect timed out — force disconnect and connect fresh
            guild_data["intentional_disconnect"] = True
            with contextlib.suppress(discord.HTTPException, discord.ClientException, OSError):
                await existing_vc.disconnect(force=True)
            guild_data["voice_client"] = None

        voice = interaction.user.voice
        if not voice or not voice.channel:
            return "You must be in a voice channel!"
        channel = voice.channel
        if problem := self._join_problem(channel) or self._activity_conflict(guild.id, channel):
            return problem
        await self._take_over_activity(guild.id)

        try:
            guild_data["voice_client"] = await channel.connect(timeout=20, reconnect=True)
        except discord.ClientException:
            # Race condition: a reconnect completed between our check and connect()
            if guild.voice_client:
                guild_data["voice_client"] = guild.voice_client
                return None
            return "Couldn't join your voice channel."
        except (TimeoutError, discord.HTTPException, OSError) as e:
            logger.error(f"Failed to connect to voice: {type(e).__name__}: {e}")
            return "Couldn't join your voice channel in time. Try again."
        return None

    async def ensure_voice_connection(self, interaction: discord.Interaction) -> bool:
        return await self.connect_voice(interaction) is None

    async def get_music_channel(self, guild_id: int):
        return self.playback_service.panel.music_channel(guild_id)

    async def create_now_playing_message(
            self, guild_id: int,
            *, current_position: int = 0, is_paused: bool = False,
    ) -> discord.Message | None:
        return await self.playback_service.panel.send(
            guild_id, current_position=current_position, is_paused=is_paused,
        )

    async def update_now_playing(self, guild_id: int):
        await self.playback_service.panel.refresh(guild_id)

    async def play_previous(self, guild_id: int) -> bool:
        return await self.playback_service.play_previous(guild_id)

    # ═══════════════════════════════════════════════════════════════════
    # Slash Commands
    # ═══════════════════════════════════════════════════════════════════

    @app_commands.command(name="join", description="Join your voice channel")
    async def join_slash(self, interaction: discord.Interaction):
        voice = interaction.user.voice
        if not voice or not voice.channel:
            await self._fail(interaction, "You must be in a voice channel!")
            return

        voice_channel = voice.channel
        guild_data = self.bot.get_guild_data(interaction.guild.id)
        voice_client = interaction.guild.voice_client

        if voice_client and voice_client.is_connected():
            if voice_client.channel == voice_channel:
                embed = create_embed(
                    "Already Connected", f"I'm already in {voice_channel.mention}!", COLOR, self.bot.user,
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            if problem := self._move_problem(interaction, voice_client) or self._join_problem(voice_channel):
                await self._fail(interaction, problem)
                return
            await interaction.response.defer()
            try:
                await self.playback_service.move_to(interaction.guild.id, voice_channel)
            except (discord.HTTPException, discord.ClientException, TimeoutError, PlaybackError) as e:
                logger.error(f"Failed to move to voice channel: {e}")
                await self._fail(interaction, "Failed to move to your voice channel!")
                return
            embed = create_embed("Moved", f"Moved to {voice_channel.mention}!", COLOR, self.bot.user)
            await interaction.followup.send(embed=embed)
            return

        await interaction.response.defer()
        if error := await self.connect_voice(interaction):
            await self._fail(interaction, error)
            return
        guild_data["last_activity"] = time.monotonic()
        embed = create_embed("Connected", f"Joined {voice_channel.mention}!", COLOR, self.bot.user)
        await interaction.followup.send(embed=embed)

    async def _queue_track_collection(self, interaction: discord.Interaction, url: str):
        """Queue a Spotify or Apple Music playlist/album in the background, playing as soon as the first is found."""
        service, _kind, _ident = track_collection(url) or ("", "", "")
        tracks = await self.music_service.collection_tracks(url)
        if not tracks:
            await self._fail(interaction, "Could not read that playlist or album!")
            return

        guild_id = interaction.guild.id
        room = MAX_QUEUE_SIZE - len(self.bot.get_guild_data(guild_id)["queue"])
        if room <= 0:
            await self._fail(interaction, f"The queue is full ({MAX_QUEUE_SIZE} songs).")
            return
        notes = []
        if service == "spotify" and len(tracks) >= SPOTIFY_TRACK_LIMIT:
            notes.append(f"Spotify only shares the first {SPOTIFY_TRACK_LIMIT} songs.")
        if len(tracks) > room:
            notes.append(f"Only {room} fit in the queue.")
            tracks = tracks[:room]
        capped_note = "".join(f"\n{note}" for note in notes)

        async def on_first():
            self.bot.spawn(self.playback_service.play_next(guild_id))
            embed = create_embed(
                "Adding Playlist...", f"Adding {len(tracks)} songs, playing as soon as the first is found.", COLOR,
                self.bot.user,
            )
            with contextlib.suppress(discord.HTTPException):
                await interaction.edit_original_response(embed=embed)

        task = self.playback_service.import_songs(
            guild_id, self.music_service.iter_youtube_matches(tracks), interaction.user.mention, on_first,
        )
        await asyncio.wait([task])

        if task.cancelled():
            embed = create_embed("Playlist Stopped", "Stopped adding songs because the queue was cleared.", COLOR, self.bot.user)
        elif task.exception():
            embed = create_embed("Error", "Could not process playlist!", COLOR, self.bot.user)
        else:
            added, skipped = task.result()
            if added:
                desc = f"Added {added} songs to queue!" + (f"\nSkipped {skipped} duplicates." if skipped else "")
                embed = create_embed("Playlist Added", desc + capped_note, COLOR, self.bot.user)
            elif skipped:
                embed = create_embed("No Songs Added", "All songs were duplicates!", COLOR, self.bot.user)
            else:
                embed = create_embed("Error", "Could not find any of these songs!", COLOR, self.bot.user)
        with contextlib.suppress(discord.HTTPException):
            await interaction.edit_original_response(embed=embed)

    async def _queue_entries(self, interaction: discord.Interaction, info, entries: list[dict]):
        """Queue every song of a playlist, set or album result (deduplicated, up to the queue's size cap)."""
        guild_id = interaction.guild.id
        guild_data = self.bot.get_guild_data(guild_id)
        existing_urls = get_existing_urls(guild_data)
        room = MAX_QUEUE_SIZE - len(guild_data["queue"])
        added = skipped = 0
        full = False
        for data in entries:
            url = data.get("webpage_url")
            if not url or not data.get("title"):
                continue
            if url in existing_urls:
                skipped += 1
                continue
            if added >= room:
                full = True
                break
            self.queue_service.add_song_to_queue(guild_id, Song({**data, "requested_by": interaction.user.mention}))
            existing_urls.add(url)
            added += 1

        if not added:
            text = "All songs were duplicates!" if skipped else (
                "The queue is full!" if full else "Could not find any songs in that link!"
            )
            embed = create_embed("No Songs Added", text, COLOR, self.bot.user)
        else:
            lines = [f"Added {added} songs to queue!",
                     f"Total queue duration: {format_duration(self.queue_service.get_queue_duration(guild_id))}"]
            if skipped:
                lines.append(f"Skipped {skipped} duplicates.")
            if full:
                lines.append(f"The queue is full ({MAX_QUEUE_SIZE} songs).")
            elif isinstance(info, dict) and info.get("capped"):
                lines.append("Only the first part of that list was added.")
            embed = create_embed("Playlist Added", "\n".join(lines), COLOR, self.bot.user)
            await self.bot.save_guild_queue(guild_id)
        await interaction.edit_original_response(embed=embed)

    async def _queue_one(self, interaction: discord.Interaction, song_data: dict):
        guild_id = interaction.guild.id
        guild_data = self.bot.get_guild_data(guild_id)
        if not song_data.get("webpage_url") or not song_data.get("title"):
            await self._fail(interaction, "Could not find the song!")
            return

        song_url = song_data["webpage_url"]
        if guild_data["current"] and guild_data["current"].webpage_url == song_url:
            embed = create_embed("Duplicate Song", "This song is currently playing!", COLOR, self.bot.user)
            await interaction.edit_original_response(embed=embed)
            return
        for i, existing_song in enumerate(guild_data["queue"], 1):
            if existing_song.webpage_url == song_url:
                embed = create_embed(
                    "Duplicate Song", f"This song is already in queue at position {i}!", COLOR, self.bot.user,
                )
                await interaction.edit_original_response(embed=embed)
                return
        if len(guild_data["queue"]) >= MAX_QUEUE_SIZE:
            await self._fail(interaction, f"The queue is full ({MAX_QUEUE_SIZE} songs).")
            return

        song = Song({**song_data, "requested_by": interaction.user.mention})
        self.queue_service.add_song_to_queue(guild_id, song)
        await self.bot.save_guild_queue(guild_id)

        position = len(guild_data["queue"])
        wait_time = self.queue_service.get_estimated_wait_time(guild_id, position)
        desc = f"{song}\n\nPosition in queue: {position}"
        if wait_time > 0:
            desc += f"\nEstimated wait: {format_duration(wait_time)}"
        embed = create_embed("Added to Queue", desc, COLOR, self.bot.user)
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
        await interaction.edit_original_response(embed=embed)

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

        guild_id = interaction.guild.id
        guild_data = self.bot.get_guild_data(guild_id)

        if not guild_data.get("music_channel_id"):
            guild_data["music_channel_id"] = interaction.channel.id
            await self.bot.save_guild_music_channel(guild_id, interaction.channel.id)

        if error := await self.connect_voice(interaction):
            await self._fail(interaction, error)
            return

        voice_client = guild_data["voice_client"]
        if voice_client and voice_client.channel != interaction.user.voice.channel:
            try:
                await self.playback_service.move_to(guild_id, interaction.user.voice.channel)
            except (discord.HTTPException, discord.ClientException, TimeoutError, PlaybackError) as e:
                logger.error(f"Failed to move to voice channel: {e}")

        searching_embed = create_embed("Searching...", f"Looking for: `{query[:200]}`", COLOR, self.bot.user)
        try:
            await interaction.edit_original_response(embed=searching_embed)
        except discord.HTTPException:
            return

        from activity.helpers import collection_entries
        try:
            if track_collection(query):
                await self._queue_track_collection(interaction, query)
            else:
                info = await self.bot.get_song_info_cached(query)
                entries = collection_entries(info)
                if not info:
                    await self._fail(interaction, "Could not find the song!")
                elif entries is not None:
                    await self._queue_entries(interaction, info, entries)
                else:
                    await self._queue_one(interaction, info)

            if guild_data["queue"]:
                self.bot.spawn(self.playback_service.play_next(guild_id))
            guild_data["last_activity"] = time.monotonic()

        except Exception:
            logger.exception("Error in play command")
            await self._fail(interaction, "Something went wrong while adding that. Try again in a moment.")

    @app_commands.command(name="pause", description="Pause the current song")
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def pause_slash(self, interaction: discord.Interaction):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        if self.playback_service.handle_pause(interaction.guild.id):
            embed = create_embed("⏸️ Paused", "Music has been paused.", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, silent=True)
        else:
            await self._fail(interaction, "Nothing is playing!")

    @app_commands.command(name="resume", description="Resume the paused song")
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def resume_slash(self, interaction: discord.Interaction):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        if self.playback_service.handle_resume(interaction.guild.id):
            embed = create_embed("▶️ Resumed", "Music has been resumed.", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, silent=True)
        else:
            await self._fail(interaction, "Music is not paused!")

    @app_commands.command(name="skip", description="Skip the current song")
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def skip_slash(self, interaction: discord.Interaction):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        # Moves on in song-loop mode too
        skipped = await self.playback_service.skip(interaction.guild.id)
        if skipped:
            embed = create_embed("Skipped", f"Skipped: **{md(skipped.title)}**", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, silent=True)
        else:
            await self._fail(interaction, "Nothing is playing!")

    @app_commands.command(name="previous", description="Play the previous song")
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def previous_slash(self, interaction: discord.Interaction):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)
        if not self.queue_service.has_previous(guild_data):
            await self._fail(interaction, "There's no previous song in the queue or history.", "❌ No Previous Songs")
            return

        await interaction.response.defer(thinking=False)
        previous = await self.playback_service.play_previous(interaction.guild.id)
        if previous:
            embed = create_embed(
                "⏮️ Previous Song", f"Playing previous: **{md(previous.title)}**", COLOR, self.bot.user,
            )
            await interaction.followup.send(embed=embed, silent=True)
        else:
            await self._fail(interaction, "Could not play previous song!")

    @app_commands.command(
        name="skipto", description="Skip to a specific song in the queue"
    )
    @app_commands.describe(position="Position in queue to skip to")
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def skipto_slash(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, None]):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        guild_id = interaction.guild.id
        visible_queue = self.queue_service.get_visible_queue(guild_id)

        if not visible_queue:
            await self._fail(interaction, "Queue is empty!")
            return
        if position > len(visible_queue):
            await self._fail(interaction, f"Invalid position! Queue has {len(visible_queue)} songs.")
            return

        # A song of the next queue-loop round starts playing before the answer, so answer later.
        await interaction.response.defer(thinking=False)
        try:
            target = await self.playback_service.skip_to(guild_id, position - 1)
        except LookupError as e:
            await self._fail(interaction, str(e))
            return
        embed = create_embed("Skipped to Song", f"Skipped to: **{md(target.title)}**", COLOR, self.bot.user)
        await interaction.followup.send(embed=embed, silent=True)
        await self.bot.save_guild_queue(guild_id)

    @app_commands.command(name="queue", description="Show the current queue")
    @app_commands.describe(page="Page number to view (optional)")
    async def queue_slash(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, None] = 1):
        guild_data = self.bot.get_guild_data(interaction.guild.id)
        played = self.queue_service.played_before(guild_data)

        if not guild_data["current"] and not guild_data["queue"] and not played:
            await interaction.response.send_message(
                view=create_v2_embed("\U0001f4cb Queue", "Queue is empty!"), silent=True,
            )
            return

        all_visible_songs = self.queue_service.get_visible_queue(interaction.guild.id)
        total_duration = self.queue_service.get_queue_duration(interaction.guild.id)

        total_pages = max(
            1, (len(all_visible_songs) + SONGS_PER_PAGE - 1) // SONGS_PER_PAGE
        )

        page = max(1, min(page, total_pages))

        dur_str = format_duration(total_duration) if total_duration > 0 else "—"
        stats_line = f"-# {len(all_visible_songs)} songs • {dur_str} • Loop: {guild_data['loop_mode'].title()}"

        pages = []
        for page_num in range(total_pages):
            start_idx = page_num * SONGS_PER_PAGE
            end_idx = start_idx + SONGS_PER_PAGE

            description = ""

            # The queue keeps what it played; in queue loop those songs show up again as the next round.
            if page_num == 0 and played and guild_data["loop_mode"] != "queue":
                shown = f", last {RECENT_PLAYED_SHOWN}" if len(played) > RECENT_PLAYED_SHOWN else ""
                description += f"**\u2705 Played** ({len(played)}{shown}; `/previous` goes back):\n"
                description += "".join(f"-# {song}\n" for song in played[-RECENT_PLAYED_SHOWN:]) + "\n"

            if guild_data["current"]:
                current = guild_data["current"]
                dur = format_duration(current.duration) if current.duration else "LIVE"
                description += f"**\U0001f3b5 Now Playing:**\n{current} `[{dur}]` — {current.requested_by}\n\n"

            if all_visible_songs:
                description += "**\U0001f4cb Up Next:**\n"
                for i, song in enumerate(
                        all_visible_songs[start_idx:end_idx], start_idx + 1
                ):
                    dur = format_duration(song.duration) if song.duration else "LIVE"
                    description += f"`{i}.` {song} `[{dur}]` — {song.requested_by}\n"

            if not description.strip():
                description = "Queue is empty!"

            text = f"### \U0001f4cb Queue — Page {page_num + 1}/{total_pages}\n{clip(description, 3800)}\n{stats_line}"
            pages.append(text)

        view = PaginationView(pages, interaction.user)
        view.current_page = page - 1
        view._rebuild()

        await interaction.response.send_message(view=view, silent=True)
        view.message = await interaction.original_response()

    @app_commands.command(
        name="volume", description="Set or show the volume (0-100)"
    )
    @app_commands.describe(level="Volume level (0-100)")
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def volume_slash(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 100] | None = None):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if level is None:
            embed = create_embed("🔊 Volume", f"Current volume: {guild_data['volume']}%", COLOR, self.bot.user)
        else:
            guild_data["volume"] = level
            voice_client = guild_data["voice_client"]
            if voice_client and isinstance(voice_client.source, discord.PCMVolumeTransformer):
                voice_client.source.volume = level / 100
            embed = create_embed("🔊 Volume", f"Volume set to {level}%", COLOR, self.bot.user)
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

        # Answered first: stopping deletes the panel and writes the database, which can outlast Discord's 3s.
        await interaction.response.defer()
        # The song that was playing still goes to history and listening stats.
        await self.playback_service.stop(interaction.guild.id)
        embed = create_embed("Stopped", "Playback stopped and queue cleared.", COLOR, self.bot.user)
        await interaction.followup.send(embed=embed, silent=True)

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

        if not guild_data["queue"] and not guild_data["played"]:
            await self._fail(interaction, "Queue is already empty!")
            return
        self.queue_service.clear_queue(interaction.guild.id)
        embed = create_embed("Queue Cleared", "Removed all songs from queue.", COLOR, self.bot.user)
        await self.bot.save_guild_queue(interaction.guild.id)
        await interaction.response.send_message(embed=embed, silent=True)

    @app_commands.command(
        name="leave", description="Disconnect from voice channel"
    )
    async def leave_slash(self, interaction: discord.Interaction):
        if not await self.check_voice_channel(interaction):
            return

        await interaction.response.defer()
        if await self.playback_service.leave(interaction.guild.id, clear_queue=True):
            embed = create_embed("Disconnected", "Left the voice channel.", COLOR, self.bot.user)
            await interaction.followup.send(embed=embed, silent=True)
        else:
            await self._fail(interaction, "Not connected to a voice channel!")

    @app_commands.command(
        name="nowplaying", description="Show the currently playing song"
    )
    async def nowplaying_slash(self, interaction: discord.Interaction):
        from activity.helpers import is_activity_paused
        from activity.state_serializer import position_now

        guild_data = self.bot.get_guild_data(interaction.guild.id)
        if not guild_data["current"]:
            await self._fail(interaction, "No song is currently playing!", "❌ Nothing Playing")
            return

        # The panel's layout without its buttons; works for Activity-only playback too.
        view = NowPlayingControls(
            self.bot, interaction.guild.id, current_position=int(position_now(self.bot, guild_data)),
            is_paused=is_activity_paused(guild_data), controls=False,
        )
        await interaction.response.send_message(view=view, silent=True)

    @app_commands.command(
        name="remove", description="Remove a song from the queue permanently"
    )
    @app_commands.describe(position="Position of the song to remove (1-based)")
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def remove_slash(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, None]):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        guild_id = interaction.guild.id
        guild_data = self.bot.get_guild_data(guild_id)
        visible_songs = self.queue_service.get_visible_queue(guild_id)

        if not visible_songs:
            await self._fail(interaction, "Queue is empty!")
            return
        if position > len(visible_songs):
            await self._fail(interaction, f"Invalid position! Visible queue has {len(visible_songs)} songs.")
            return

        song_to_remove = visible_songs[position - 1]
        queued = len(guild_data["queue"])
        if position <= queued:
            self.queue_service.remove_song_from_queue(guild_id, position - 1)
        else:
            # Queue loop: a song of the next round, which this round played already
            self.queue_service.remove_played(guild_id, position - 1 - queued)

        embed = create_embed(
            "Song Removed", f"Permanently removed: **{md(song_to_remove.title)}**", COLOR, self.bot.user,
        )
        await self.bot.save_guild_queue(guild_id)
        await interaction.response.send_message(embed=embed, silent=True)

    @app_commands.command(
        name="move", description="Move a song to a different position in queue"
    )
    @app_commands.describe(
        from_pos="Current position of the song", to_pos="New position for the song"
    )
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def move_slash(
            self, interaction: discord.Interaction,
            from_pos: app_commands.Range[int, 1, None], to_pos: app_commands.Range[int, 1, None],
    ):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)
        queue_length = len(guild_data["queue"])
        if not queue_length:
            await self._fail(interaction, "Queue is empty!")
            return
        if from_pos > queue_length or to_pos > queue_length:
            await self._fail(interaction, f"Invalid position! Queue has {queue_length} songs.")
            return

        song = guild_data["queue"][from_pos - 1]
        self.queue_service.move_song_in_queue(interaction.guild.id, from_pos - 1, to_pos - 1)

        embed = create_embed(
            "🔄 Song Moved",
            f"Moved **{md(song.title)}**\nFrom position {from_pos} to position {to_pos}",
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
        results=f"Number of results to show (1-{MAX_SEARCH_RESULTS}, default {DEFAULT_SEARCH_RESULTS})",
    )
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def search_slash(
            self, interaction: discord.Interaction, query: str,
            results: app_commands.Range[int, 1, MAX_SEARCH_RESULTS] = DEFAULT_SEARCH_RESULTS,
    ):
        if not await self.check_voice_channel(interaction, allow_auto_join=True):
            return

        await interaction.response.send_message(
            view=create_v2_embed("Searching...", f"Looking for: `{query[:200]}`", COLOR),
            silent=True,
        )

        try:
            # Its own YoutubeDL per call: an instance must not be shared between threads.
            entries = await self.music_service.search_youtube_many(query, results)
            if not entries:
                await interaction.edit_original_response(
                    view=create_v2_embed("❌ Error", "No results found!", COLOR))
                return

            view = SongSelectView(entries[:results], interaction.user, self)
            view.message = await interaction.edit_original_response(view=view)

        except Exception:
            logger.exception("Search command error")
            with contextlib.suppress(discord.HTTPException):
                await interaction.edit_original_response(
                    view=create_v2_embed("❌ Error", "An error occurred during search.", COLOR))

    async def process_selected_song(
            self, interaction: discord.Interaction, selected_song: dict
    ):
        async def show(title: str, text: str):
            with contextlib.suppress(discord.HTTPException):
                await interaction.edit_original_response(view=create_v2_embed(title, text, COLOR))

        try:
            guild_id = interaction.guild.id
            guild_data = self.bot.get_guild_data(guild_id)

            if not guild_data.get("music_channel_id"):
                guild_data["music_channel_id"] = interaction.channel.id
                await self.bot.save_guild_music_channel(guild_id, interaction.channel.id)

            # Checked again now: the pick can come up to 30 s after /search, when things may have changed.
            if not interaction.user.voice:
                await show("Error", "You must be in a voice channel!")
                return
            voice_client = interaction.guild.voice_client
            if voice_client and voice_client.is_connected():
                guild_data["voice_client"] = voice_client
                if voice_client.channel != interaction.user.voice.channel:
                    if problem := self._move_problem(interaction, voice_client) or \
                            self._join_problem(interaction.user.voice.channel):
                        await show("Error", problem)
                        return
                    await self.playback_service.move_to(guild_id, interaction.user.voice.channel)
            elif error := await self.connect_voice(interaction):
                await show("Error", error)
                return

            song = Song({**selected_song, "requested_by": interaction.user.mention})
            if song.webpage_url in get_existing_urls(guild_data):
                await show("Duplicate Song", "This song is already in queue or playing!")
                return
            if len(guild_data["queue"]) >= MAX_QUEUE_SIZE:
                await show("Queue Full", f"The queue is full ({MAX_QUEUE_SIZE} songs).")
                return

            self.queue_service.add_song_to_queue(guild_id, song)
            idle = not guild_data["current"] and not guild_data["voice_client"].is_playing()
            if idle:
                await self.playback_service.play_next(guild_id)
                await show("🎵 Now Playing", str(song))
            else:
                await show("📋 Added to Queue", f"{song}\n\nPosition in queue: {len(guild_data['queue'])}")

            guild_data["last_activity"] = time.monotonic()
            await self.bot.save_guild_queue(guild_id)

        except Exception:
            logger.exception("Error processing selected song")
            await show("❌ Error", "Failed to add song to queue.")

    @app_commands.command(
        name="setmusicchannel", description="Set the channel for music messages"
    )
    @app_commands.default_permissions(manage_guild=True)
    async def set_music_channel_slash(self, interaction: discord.Interaction):
        permissions = interaction.user.guild_permissions
        if not (permissions.manage_guild or permissions.administrator):
            await self._fail(interaction, "Changing the music channel needs the Manage Server permission.")
            return
        channel = interaction.channel
        mine = channel.permissions_for(interaction.guild.me)
        if not (mine.view_channel and mine.send_messages):
            await self._fail(interaction, f"I can't send messages in {channel.mention}.")
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)
        old_channel_id = guild_data.get("music_channel_id")
        guild_data["music_channel_id"] = channel.id
        await self.bot.save_guild_music_channel(interaction.guild.id, channel.id)

        old_channel = (
            interaction.guild.get_channel_or_thread(old_channel_id)
            if old_channel_id and old_channel_id != channel.id else None
        )
        if old_channel:
            embed = create_embed(
                "📺 Music Channel Updated",
                f"Music messages moved from {old_channel.mention} to {channel.mention}",
                COLOR,
                self.bot.user,
            )
        else:
            embed = create_embed(
                "📺 Music Channel Set",
                f"Music messages will now be sent to {channel.mention}",
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

        guild_id = interaction.guild.id
        current = self.bot.get_guild_data(guild_id)["current"]
        if not current:
            await self._fail(interaction, "No song is currently playing!")
            return
        if current.is_live or not current.duration:
            await self._fail(interaction, "Cannot seek in a livestream!")
            return
        try:
            seek_seconds = parse_time_to_seconds(position)
        except ValueError as e:
            await self._fail(interaction, str(e))
            return

        # A seek may need a fresh stream, which can take longer than the 3 s an answer is allowed.
        await interaction.response.defer()
        try:
            used = await self.playback_service.seek(guild_id, seek_seconds)
        except PlaybackError as e:
            await self._fail(interaction, str(e))
            return

        embed = create_embed(
            "⏩ Seeked", f"Moved to {format_duration(used)} in **{md(current.title)}**", COLOR, self.bot.user,
        )
        await interaction.followup.send(embed=embed, silent=True)

    @app_commands.command(
        name="autoplay",
        description="Toggle autoplay mode (automatically plays related songs when queue ends)"
    )
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def autoplay_slash(self, interaction: discord.Interaction):
        if not await self.check_voice_channel(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)
        guild_data["autoplay"] = not guild_data.get("autoplay", False)
        if not guild_data["autoplay"]:
            self.bot.cancel_autoplay_prefetch(guild_data)

        if guild_data["autoplay"]:
            text = "The bot will automatically add related songs when the queue ends"
            if not self.bot.lastfm:
                # Without Last.fm, YouTube songs get picks from their YouTube Mix.
                text += ".\nWithout Last.fm keys, picks come from YouTube's Mix, so SoundCloud songs get none."
        else:
            text = "The bot will stop when the queue ends"
        status = "enabled" if guild_data["autoplay"] else "disabled"
        embed = create_embed(f"Autoplay {status.title()}", f"Autoplay has been **{status}**.\n{text}", COLOR, self.bot.user)
        await interaction.response.send_message(embed=embed, silent=True)
        await self.bot.save_guild_queue(interaction.guild.id)

    # Speed, Effects, Lyrics, Favorites, Stats, DJ, Queue Search

    async def _applied_note(self, guild_id: int) -> str:
        """Rebuild the playing song so a speed or effect change is heard now."""
        if await self.playback_service.reapply_audio_filters(guild_id):
            return "Applied to the current song."
        return "It applies from the next song."

    @app_commands.command(name="speed", description="Set playback speed (0.5x to 2.0x)")
    @app_commands.describe(rate="Playback speed multiplier (0.5 - 2.0)")
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def speed_slash(self, interaction: discord.Interaction, rate: app_commands.Range[float, 0.5, 2.0]):
        if not await self.check_voice_channel(interaction):
            return
        if not await self._check_dj(interaction):
            return

        await interaction.response.defer()
        guild_data = self.bot.get_guild_data(interaction.guild.id)
        guild_data["speed"] = rate
        note = await self._applied_note(interaction.guild.id)
        await self.bot.save_guild_queue(interaction.guild.id)
        embed = create_embed("⏩ Playback Speed", f"Speed set to **{rate:.1f}x**\n{note}", COLOR, self.bot.user)
        await interaction.followup.send(embed=embed, silent=True)

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

        await interaction.response.defer()
        guild_data = self.bot.get_guild_data(interaction.guild.id)
        guild_data["audio_effect"] = effect
        note = await self._applied_note(interaction.guild.id)
        await self.bot.save_guild_queue(interaction.guild.id)
        effect_name = AUDIO_EFFECTS.get(effect, {}).get("name", effect)
        embed = create_embed("🎧 Audio Effect", f"Effect set to **{effect_name}**\n{note}", COLOR, self.bot.user)
        await interaction.followup.send(embed=embed, silent=True)

    @staticmethod
    def _lyrics_pages(title: str, artist: str, lyrics_text: str, max_len: int = 600) -> list[str]:
        """Lyrics in pages split at verse breaks (else lines); none goes past Discord's text limit."""
        has_verses = "\n\n" in lyrics_text
        separator = "\n\n" if has_verses else "\n"
        parts = []
        for part in lyrics_text.split(separator):
            # A single verse longer than a page is cut at line breaks, then hard.
            while len(part) > V2_TEXT_LIMIT - 300:
                cut = part.rfind("\n", 0, V2_TEXT_LIMIT - 300)
                cut = cut if cut > 0 else V2_TEXT_LIMIT - 300
                parts.append(part[:cut])
                part = part[cut:].lstrip("\n")
            parts.append(part)

        chunks, current_chunk = [], ""
        for part in parts:
            if current_chunk and len(current_chunk) + len(part) + len(separator) > max_len:
                chunks.append(current_chunk)
                current_chunk = part
            else:
                current_chunk = current_chunk + separator + part if current_chunk else part
        if current_chunk:
            chunks.append(current_chunk)
        heading = f"🎤 {md(title)} — {md(artist)}"
        if len(chunks) == 1:
            return [f"### {heading}\n{chunks[0]}"]
        return [f"### {heading} ({i}/{len(chunks)})\n{chunk}" for i, chunk in enumerate(chunks, 1)]

    @app_commands.command(name="lyrics", description="Show lyrics for the current song")
    @app_commands.checks.cooldown(1, 5)
    async def lyrics_slash(self, interaction: discord.Interaction):
        from utils.lyrics import LyricsServiceUnavailable, lyrics_for_song, strip_lrc_timestamps

        current = self.bot.get_guild_data(interaction.guild.id).get("current")
        if not current:
            await self._fail(interaction, "No song is currently playing!")
            return

        await interaction.response.defer()
        try:
            # Shared with the Activity's lyrics panel: one lookup per song, cached
            result = await lyrics_for_song(current, session=self.bot.http_session)
        except LyricsServiceUnavailable:
            embed = create_embed(
                "Lyrics Service Unavailable",
                "LRCLIB is temporarily unavailable. Please try again shortly.",
                COLOR,
                self.bot.user,
            )
            await interaction.followup.send(embed=embed)
            return

        if result and result.get("instrumental"):
            embed = create_embed("🎼 Instrumental", f"**{md(current.title)}** has no lyrics.", COLOR, self.bot.user)
            await interaction.followup.send(embed=embed)
            return
        lyrics_text = (result.get("lyrics") or strip_lrc_timestamps(result.get("synced", ""))) if result else ""
        if not lyrics_text:
            embed = create_embed(
                "Lyrics Not Found", f"Could not find lyrics for **{md(current.title)}**", COLOR, self.bot.user,
            )
            await interaction.followup.send(embed=embed)
            return

        pages = self._lyrics_pages(result.get("title") or current.title, result.get("artist") or current.uploader,
                                   lyrics_text)
        if len(pages) == 1:
            await interaction.followup.send(view=create_v2_embed("", pages[0], COLOR))
            return
        view = PaginationView(pages, interaction.user)
        await interaction.followup.send(view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="favorite", description="Add the current song to your favorites")
    async def favorite_slash(self, interaction: discord.Interaction):
        current = self.bot.get_guild_data(interaction.guild.id).get("current")
        if not current:
            await self._fail(interaction, "No song is currently playing!")
            return

        try:
            added = await self.bot.add_favorite(interaction.user.id, current)
        except FavoritesFull as e:
            await self._fail(interaction, str(e))
            return
        except sqlite3.Error:
            await self._fail(interaction, "Couldn't save the favorite. Try again.")
            return

        if added:
            embed = create_embed("❤️ Favorited", f"Added {current.linked_title} to your favorites!", COLOR, self.bot.user)
        else:
            embed = create_embed(
                "Already Favorited", f"{current.linked_title} is already in your favorites.", COLOR, self.bot.user,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    favorites_group = app_commands.Group(name="favorites", description="Show, play or remove your favorite songs")

    async def _no_favorites(self, interaction: discord.Interaction):
        embed = create_embed(
            "❤️ Favorites", "You have no favorites yet! Use `/favorite` while a song plays.", COLOR, self.bot.user,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def favorite_autocomplete(self, interaction: discord.Interaction, current: str):
        """The user's favorites by title; the value is the song's URL, so removal never hits the wrong one."""
        needle = current.lower()
        choices = []
        for song in await self.bot.get_favorites(interaction.user.id):
            title = song.get("title") or "Unknown"
            url = song.get("webpage_url") or song.get("url") or ""
            if url and len(url) <= 100 and needle in title.lower():
                choices.append(app_commands.Choice(name=clip(title, 100), value=url))
            if len(choices) >= 25:
                break
        return choices

    @favorites_group.command(name="show", description="Show your favorite songs")
    async def favorites_show(self, interaction: discord.Interaction):
        favs = await self.bot.get_favorites(interaction.user.id)
        if not favs:
            await self._no_favorites(interaction)
            return
        lines = [
            f"`{i}.` **{md(song.get('title') or 'Unknown')}** by {md(song.get('uploader') or 'Unknown')}"
            for i, song in enumerate(favs, 1)
        ]
        pages = [
            f"### ❤️ Your Favorites ({len(favs)} songs)\n" + "\n".join(lines[start:start + SONGS_PER_PAGE])
            for start in range(0, len(lines), SONGS_PER_PAGE)
        ]
        view = PaginationView(pages, interaction.user)
        await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @favorites_group.command(name="play", description="Add one of your favorites to the queue")
    @app_commands.describe(position="Song number from /favorites show")
    async def favorites_play(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, None]):
        favs = await self.bot.get_favorites(interaction.user.id)
        if not favs:
            await self._no_favorites(interaction)
            return
        if position > len(favs):
            await self._fail(interaction, f"Please provide a valid position (1-{len(favs)}).")
            return
        if not await self.check_voice_channel(interaction, allow_auto_join=True):
            return

        await interaction.response.defer()
        if error := await self.connect_voice(interaction):
            await self._fail(interaction, error)
            return

        guild_id = interaction.guild.id
        guild_data = self.bot.get_guild_data(guild_id)
        song = Song({**favs[position - 1], "requested_by": interaction.user.mention})
        if song.webpage_url in get_existing_urls(guild_data):
            await self._fail(interaction, f"{song.linked_title} is already in the queue or playing.", "Duplicate Song")
            return
        if len(guild_data["queue"]) >= MAX_QUEUE_SIZE:
            await self._fail(interaction, f"The queue is full ({MAX_QUEUE_SIZE} songs).")
            return
        self.queue_service.add_song_to_queue(guild_id, song)
        await self.bot.save_guild_queue(guild_id)

        voice_client = guild_data["voice_client"]
        if voice_client and not voice_client.is_playing() and not guild_data.get("current"):
            self.bot.spawn(self.playback_service.play_next(guild_id))

        embed = create_embed("❤️ Playing Favorite", f"Added {song.linked_title} to queue!", COLOR, self.bot.user)
        await interaction.followup.send(embed=embed, silent=True)

    @favorites_group.command(name="remove", description="Remove a song from your favorites")
    @app_commands.describe(song="The favorite to remove (start typing its title), or its number")
    @app_commands.autocomplete(song=favorite_autocomplete)
    async def favorites_remove(self, interaction: discord.Interaction, song: str):
        user_id = interaction.user.id
        if song.isdigit():
            favs = await self.bot.get_favorites(user_id)
            position = int(song)
            if not 1 <= position <= len(favs):
                await self._fail(interaction, f"Please provide a valid position (1-{len(favs)}).")
                return
            song = favs[position - 1].get("webpage_url") or favs[position - 1].get("url") or ""
        removed, title = await self.bot.remove_favorite_by_url(user_id, song)
        if not removed:
            await self._fail(interaction, "That song isn't in your favorites.")
            return
        embed = create_embed("Removed", f"Removed **{md(title or 'Unknown')}** from your favorites.", COLOR, self.bot.user)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="stats", description="Show your listening statistics")
    async def stats_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        stats = await self.bot.get_user_stats(interaction.user.id, interaction.guild.id)

        if not stats["play_count"]:
            embed = create_embed("📊 Your Stats", "No listening history yet! Play some songs first.", COLOR, self.bot.user)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        hours, minutes = stats["total_seconds"] // 3600, (stats["total_seconds"] % 3600) // 60
        desc = f"**Total plays:** {stats['play_count']}\n**Total listening time:** {hours}h {minutes}m\n\n"
        if stats["top_songs"]:
            desc += "**Top Songs:**\n" + "".join(
                f"`{i}.` {md(s['title'])} ({s['plays']} plays)\n" for i, s in enumerate(stats["top_songs"], 1)
            ) + "\n"
        if stats["top_artists"]:
            desc += "**Top Artists:**\n" + "".join(
                f"`{i}.` {md(a['name'])} ({a['plays']} plays)\n" for i, a in enumerate(stats["top_artists"], 1)
            )
        desc += "\n-# `/forgetme` deletes everything the bot stores about you."

        await interaction.followup.send(view=create_v2_embed("📊 Your Stats", desc, COLOR), ephemeral=True)

    @app_commands.command(name="forgetme", description="Delete everything the bot stores about you")
    async def forgetme_slash(self, interaction: discord.Interaction):
        view = ConfirmView(interaction.user)
        embed = create_embed(
            "Delete Your Data?",
            "This deletes your listening stats, favorites, playlists (server and global), playlist shares and "
            "Activity settings, in every server. It can't be undone.",
            COLOR,
            self.bot.user,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        if not await view.wait_for_answer():
            await interaction.edit_original_response(
                embed=create_embed("Nothing Deleted", "Your data was kept.", COLOR, self.bot.user), view=None,
            )
            return
        removed = await self.bot.delete_user_data(interaction.user.id)
        summary = ", ".join(f"{count} {table.replace('_', ' ')}" for table, count in removed.items() if count)
        await interaction.edit_original_response(
            embed=create_embed("Data Deleted", f"Deleted: {summary or 'nothing was stored'}.", COLOR, self.bot.user),
            view=None,
        )

    @app_commands.command(name="leaderboard", description="Show the server's top listeners")
    async def leaderboard_slash(self, interaction: discord.Interaction):
        await interaction.response.defer()
        rows = await self.bot.get_leaderboard(interaction.guild.id)

        if not rows or all(seconds == 0 for _, _, seconds in rows):
            embed = create_embed("🏆 Leaderboard", "No listening data yet! Play some songs first.", COLOR, self.bot.user)
            await interaction.followup.send(embed=embed)
            return

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        desc = ""
        for rank, (user_id, play_count, total_seconds) in enumerate(rows, 1):
            hours, minutes = total_seconds // 3600, (total_seconds % 3600) // 60
            # Mentions show names without pinging anyone (the bot sends with mentions disabled)
            desc += f"{medals.get(rank, f'`{rank}.`')} <@{user_id}> — **{hours}h {minutes}m** ({play_count} plays)\n"

        desc += f"\n-# {md(interaction.guild.name)} • Use /stats for your personal stats"
        await interaction.followup.send(view=create_v2_embed("🏆 Leaderboard", desc, COLOR))

    @app_commands.command(name="setdj", description="Set or remove the DJ role for this server")
    @app_commands.describe(role="The DJ role (leave empty to remove)")
    @app_commands.checks.has_permissions(administrator=True)
    async def setdj_slash(self, interaction: discord.Interaction, role: discord.Role | None = None):
        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if role:
            guild_data["dj_role_id"] = role.id
            await self.bot.save_guild_dj_role(interaction.guild.id, role.id)
            embed = create_embed(
                "🎧 DJ Role Set",
                f"DJ role set to **{md(role.name)}**.\n"
                f"Only members with this role (or admins) can use skip, stop, volume, loop, shuffle, speed, effects, "
                f"clear, remove, and move.",
                COLOR,
                self.bot.user,
            )
        else:
            guild_data["dj_role_id"] = None
            await self.bot.save_guild_dj_role(interaction.guild.id, None)
            embed = create_embed(
                "🎧 DJ Role Removed", "DJ role restriction removed. All members can use all commands.", COLOR,
                self.bot.user,
            )

        await interaction.response.send_message(embed=embed, silent=True)

    @app_commands.command(name="queuesearch", description="Search for a song in the queue")
    @app_commands.describe(query="Search term to find in queue")
    async def queuesearch_slash(self, interaction: discord.Interaction, query: str):
        results = self.queue_service.search_queue(interaction.guild.id, query)

        if not results:
            embed = create_embed(
                "🔍 Queue Search", f"No songs matching **{md(query[:100])}** found in queue.", COLOR, self.bot.user,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        description = ""
        for pos, song in results[:15]:
            dur = format_duration(song.duration) if song.duration else "LIVE"
            description += f"`{pos}.` {song} `[{dur}]`\n"
        if len(results) > 15:
            description += f"\n*...and {len(results) - 15} more*"

        view = create_v2_embed(f"🔍 Queue Search: \"{md(query[:100])}\" ({len(results)} found)", description, COLOR)
        await interaction.response.send_message(view=view, ephemeral=True)

    # Activity

    @app_commands.command(
        name="activity", description="Open the music dashboard Activity"
    )
    async def activity_slash(self, interaction: discord.Interaction):
        await interaction.response.launch_activity()

    # Help

    def _command(self, name: str) -> str:
        """A clickable mention of a slash command (or subcommand) once its id is known, else /name."""
        command_id = self.bot.command_ids.get(name.split(" ", 1)[0])
        return f"</{name}:{command_id}>" if command_id else f"`/{name}`"

    @app_commands.command(
        name="help", description="Shows all available commands and how to use them"
    )
    async def help_slash(self, interaction: discord.Interaction):
        c = self._command
        sections = {
            "Basic Commands": [
                (c("join"), "Join your voice channel"),
                (f"{c('play')} `<query>`", "Play a song or add it to queue"),
                (c("pause"), "Pause the current song"),
                (c("resume"), "Resume the paused song"),
                (c("skip"), "Skip the current song"),
                (c("previous"), "Play the previous song from history"),
                (c("autoplay"), "Auto play related songs after queue ends"),
                (c("stop"), "Stop playback and clear queue"),
                (c("leave"), "Disconnect from voice channel"),
                (c("activity"), "Open the music dashboard in your voice channel"),
            ],
            "Queue Management": [
                (f"{c('queue')} `[page]`", "Show the current queue"),
                (c("clear"), "Clear the queue without stopping current song"),
                (f"{c('remove')} `<position>`", "Remove a song by position"),
                (f"{c('move')} `<from> <to>`", "Move a song to different position"),
                (f"{c('skipto')} `<position>`", "Skip to a specific song"),
                (f"{c('queuesearch')} `<query>`", "Search for a song in the queue"),
            ],
            "Playback Controls": [
                (f"{c('volume')} `[level]`", "Set or show volume (0-100)"),
                (f"{c('loop')} `<mode>`", "Set loop mode: off, song, or queue"),
                (c("shuffle"), "Toggle shuffle mode"),
                (f"{c('seek')} `<position>`", "Seek to specific time (e.g., '1:30')"),
                (c("nowplaying"), "Show currently playing song info"),
                (f"{c('speed')} `<rate>`", "Set playback speed (0.5x to 2.0x)"),
                (f"{c('effects')} `<effect>`", "Apply audio effect"),
                (c("lyrics"), "Show lyrics for the current song"),
            ],
            "Search & Discovery": [
                (f"{c('search')} `<query> [results]`", "Search for songs"),
                (f"{c('history show')} `[page]`", "Show recently played songs"),
                (f"{c('history play')} `<number>`", "Play a song from history"),
                (c("history add_all"), "Add all history to queue"),
                (c("history clear"), "Clear all history"),
            ],
            "Favorites": [
                (c("favorite"), "Add the current song to favorites"),
                (c("favorites show"), "Show your favorite songs"),
                (f"{c('favorites play')} `<pos>`", "Play a favorite"),
                (f"{c('favorites remove')} `<song>`", "Remove a favorite"),
            ],
            "Stats": [
                (c("stats"), "Your listening statistics"),
                (c("leaderboard"), "Server's top listeners"),
                (c("forgetme"), "Delete everything the bot stores about you"),
            ],
            "Playlists (this server)": [
                (c("playlist create"), "create, add, remove, move, load, show, list, delete"),
                (c("playlist add-from-queue"), "Add from current queue"),
                (c("playlist add-all-queue"), "Add entire queue"),
                (c("playlist collab-add"), "collab-add, collab-remove, collab-list: share a playlist"),
                (c("playlist my-collabs"), "Your collaborations"),
            ],
            "Global Playlists (any server)": [
                (c("globalplaylist create"), "The same commands as playlists"),
            ],
            "Settings": [
                (c("setmusicchannel"), "Set music messages channel"),
                (f"{c('setdj')} `[role]`", "Set or remove DJ role"),
            ],
        }
        description = "\n".join(
            f"**{title}:**\n" + "".join(f"  {usage} - {text}\n" for usage, text in rows)
            for title, rows in sections.items()
        )
        description += (
            "\n-# Supports YouTube, Spotify, Apple Music, Tidal & SoundCloud • "
            "Autoplay recommendations powered by [Last.fm](<https://www.last.fm/>)"
        )

        view = create_v2_embed("Command Guide", description, COLOR)
        await interaction.response.send_message(view=view, silent=True)

    # Error handler

    async def cog_app_command_error(
            self,
            interaction: discord.Interaction,
            error: app_commands.AppCommandError,
    ):
        await report_command_error(self.bot, interaction, error)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.NotOwner):
            return  # Silently ignore non-owners trying owner commands
        if isinstance(error, commands.UserInputError):
            await ctx.send(f"❌ {error}\nUsage: `{ctx.prefix}{ctx.command.qualified_name} {ctx.command.signature}`")
            return
        logger.error(f"Prefix command error: {error}", exc_info=error)
        await ctx.send(f"❌ That failed: {error}")

    # Owner-only prefix commands (hidden)

    @commands.command(name="leaveguild")
    @commands.is_owner()
    async def leave_guild(self, ctx, guild_id: int | None = None):
        guild = self.bot.get_guild(guild_id) if guild_id else ctx.guild
        if guild is None:
            await ctx.send("❌ Give a server ID (in DMs there is no current server)." if not guild_id
                           else "❌ Could not find that guild.")
            return
        # Said first: after leaving, the bot can no longer post in that server. on_guild_remove cleans up.
        await ctx.send(f"✅ Leaving guild: {guild.name} ({guild.id})")
        await guild.leave()

    @commands.command(name="banuser")
    @commands.is_owner()
    async def ban_user(self, ctx, user: discord.User):
        if not ban_user_id(user.id):
            await ctx.send(f"{user.mention} ({user.id}) is already banned")
            return
        # An open Activity stops receiving state at once (the next request is refused anyway).
        close_user = getattr(self.bot.ws_manager, "close_user", None)
        if close_user is not None:
            for guild in self.bot.guilds:
                await close_user(guild.id, user.id, code=4003, reason="You are banned from using this bot")
        await ctx.send(f"Banned {user.mention} ({user.id})")

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
        ids = banned_ids()
        if not ids:
            await ctx.send("No banned users.")
            return
        text = "Banned users:\n" + "\n".join(str(uid) for uid in ids)
        if len(text) <= 1900:
            await ctx.send(text)
        else:
            await ctx.send(file=discord.File(io.BytesIO(text.encode()), filename="banned_users.txt"))

    @commands.command(name="tasks")
    @commands.is_owner()
    async def list_tasks(self, ctx):
        """Where every running task is waiting (asyncio call graphs), to debug a stuck play_next or import."""
        graphs = [asyncio.format_call_graph(task) for task in asyncio.all_tasks()]
        text = "\n".join(graphs)
        logger.info(f"Task call graphs ({len(graphs)} tasks):\n{text}")
        await ctx.send(
            f"{len(graphs)} running tasks.",
            file=discord.File(io.BytesIO(text.encode()), filename="tasks.txt"),
        )
