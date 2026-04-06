import logging
from typing import TYPE_CHECKING

import discord
from discord import ui

from utils.ban_system import is_banned
from config import COLOR
from utils.helpers import create_embed, format_duration, build_progress_bar

if TYPE_CHECKING:
    from cogs.music_commands import MusicCommands

logger = logging.getLogger(__name__)

ACCENT_COLOUR = discord.Colour(COLOR)


# ── Button subclasses (LayoutView dispatches via callback) ─────────

class _NPButton(ui.Button):
    """Base button that stores the view reference for guild access."""

    async def _get_context(self):
        v: NowPlayingControls = self.view  # type: ignore
        return v.bot.get_guild_data(v.guild_id), v.music_commands_cog, v


class PlayPauseButton(_NPButton):
    def __init__(self):
        super().__init__(emoji="\u23ef\ufe0f", style=discord.ButtonStyle.secondary, custom_id="np_play_pause")

    async def callback(self, interaction: discord.Interaction):
        gd, cog, v = await self._get_context()
        if gd["voice_client"].is_playing():
            cog.playback_service.handle_pause(v.guild_id)
            await interaction.response.send_message("\u23f8\ufe0f Paused", ephemeral=True, delete_after=2)
        elif gd["voice_client"].is_paused():
            cog.playback_service.handle_resume(v.guild_id)
            await interaction.response.send_message("\u25b6\ufe0f Resumed", ephemeral=True, delete_after=2)
        else:
            await interaction.response.send_message("Nothing to play/pause", ephemeral=True, delete_after=2)


class PreviousButton(_NPButton):
    def __init__(self):
        super().__init__(emoji="\u23ee\ufe0f", style=discord.ButtonStyle.secondary, custom_id="np_previous")

    async def callback(self, interaction: discord.Interaction):
        _, cog, v = await self._get_context()
        await interaction.response.defer(ephemeral=True, thinking=False)
        success = await cog.play_previous(v.guild_id)
        msg = "\u23ee\ufe0f Playing previous" if success else "No previous song available"
        followup_msg = await interaction.followup.send(msg, ephemeral=True, wait=True)
        if followup_msg:
            await followup_msg.delete(delay=3)


class SkipButton(_NPButton):
    def __init__(self):
        super().__init__(emoji="\u23ed\ufe0f", style=discord.ButtonStyle.secondary, custom_id="np_skip")

    async def callback(self, interaction: discord.Interaction):
        gd, cog, v = await self._get_context()
        if gd["voice_client"] and (gd["voice_client"].is_playing() or gd["voice_client"].is_paused()):
            if gd["current"]:
                cog.queue_service.add_to_history(v.guild_id, gd["current"])
            gd["voice_client"].stop()
            await interaction.response.send_message("\u23ed\ufe0f Skipped", ephemeral=True, delete_after=2)
        else:
            await interaction.response.send_message("Nothing to skip", ephemeral=True, delete_after=2)


class StopButton(_NPButton):
    def __init__(self):
        super().__init__(emoji="\u23f9\ufe0f", style=discord.ButtonStyle.danger, custom_id="np_stop")

    async def callback(self, interaction: discord.Interaction):
        gd, cog, v = await self._get_context()
        cog.queue_service.clear_queue(v.guild_id)
        gd["current"] = None
        gd["start_time"] = None
        if gd["voice_client"] and (gd["voice_client"].is_playing() or gd["voice_client"].is_paused()):
            gd["voice_client"].stop()
        await v.bot.clear_guild_queue_from_db(v.guild_id)
        await interaction.response.send_message("\u23f9\ufe0f Stopped and queue cleared", ephemeral=True, delete_after=2)


class VolDownButton(_NPButton):
    def __init__(self):
        super().__init__(emoji="\U0001f509", style=discord.ButtonStyle.secondary, custom_id="np_vol_down")

    async def callback(self, interaction: discord.Interaction):
        gd, _, v = await self._get_context()
        new_vol = max(0, gd["volume"] - 10)
        gd["volume"] = new_vol
        if gd["voice_client"] and gd["voice_client"].source:
            try:
                gd["voice_client"].source.volume = new_vol / 100
            except AttributeError:
                pass
        await v.bot.save_guild_queue(v.guild_id)
        await interaction.response.send_message(f"\U0001f509 Volume: {new_vol}%", ephemeral=True, delete_after=2)


class VolUpButton(_NPButton):
    def __init__(self):
        super().__init__(emoji="\U0001f50a", style=discord.ButtonStyle.secondary, custom_id="np_vol_up")

    async def callback(self, interaction: discord.Interaction):
        gd, _, v = await self._get_context()
        new_vol = min(100, gd["volume"] + 10)
        gd["volume"] = new_vol
        if gd["voice_client"] and gd["voice_client"].source:
            try:
                gd["voice_client"].source.volume = new_vol / 100
            except AttributeError:
                pass
        await v.bot.save_guild_queue(v.guild_id)
        await interaction.response.send_message(f"\U0001f50a Volume: {new_vol}%", ephemeral=True, delete_after=2)


class ShuffleButton(_NPButton):
    def __init__(self):
        super().__init__(emoji="\U0001f500", style=discord.ButtonStyle.secondary, custom_id="np_shuffle")

    async def callback(self, interaction: discord.Interaction):
        _, cog, v = await self._get_context()
        state = cog.queue_service.toggle_shuffle(v.guild_id)
        await v.bot.save_guild_queue(v.guild_id)
        await interaction.response.send_message(
            f"\U0001f500 Shuffle {'enabled' if state else 'disabled'}", ephemeral=True, delete_after=2
        )


class LoopButton(_NPButton):
    def __init__(self):
        super().__init__(emoji="\U0001f501", style=discord.ButtonStyle.secondary, custom_id="np_loop")

    async def callback(self, interaction: discord.Interaction):
        gd, cog, v = await self._get_context()
        modes = ["off", "song", "queue"]
        cur = gd.get("loop_mode", "off")
        if cur not in modes:
            cur = "off"
        new_mode = modes[(modes.index(cur) + 1) % len(modes)]
        cog.queue_service.set_loop_mode(v.guild_id, new_mode)
        await v.bot.save_guild_queue(v.guild_id)
        icons = {"off": "\U0001f504", "song": "\U0001f502", "queue": "\U0001f501"}
        await interaction.response.send_message(
            f"{icons[new_mode]} Loop: {new_mode.title()}", ephemeral=True, delete_after=3
        )


# ── Main layout ────────────────────────────────────────────────────

class NowPlayingControls(ui.LayoutView):
    """Components V2 now-playing layout with integrated controls."""

    def __init__(
        self,
        music_commands_cog: 'MusicCommands',
        guild_id: int,
        *,
        current_position: int = 0,
        is_paused: bool = False,
    ):
        super().__init__(timeout=None)
        self.music_commands_cog = music_commands_cog
        self.guild_id = guild_id
        self.bot = music_commands_cog.bot

        self._build_layout(current_position, is_paused)

    def _build_layout(self, current_position: int, is_paused: bool):
        guild_data = self.bot.get_guild_data(self.guild_id)
        current = guild_data.get("current")
        if not current:
            return

        from config import AUDIO_EFFECTS

        is_live = getattr(current, "is_live", False) or not current.duration
        voice_client = guild_data.get("voice_client")

        # Status icon
        if is_paused:
            status_icon = "\u23f8\ufe0f"
        elif not voice_client or not voice_client.is_playing():
            status_icon = "\u23f9\ufe0f"
        else:
            status_icon = "\u25b6\ufe0f"

        # Progress
        if is_live:
            progress_text = f"\U0001f534 `LIVE \u2014 {format_duration(current_position)}`"
        else:
            bar = build_progress_bar(current_position, current.duration, length=16)
            progress_text = f"`{format_duration(current_position)}` {bar} `{format_duration(current.duration)}`"

        # Song title
        title_line = f"**[{current.title}]({current.webpage_url})**" if current.webpage_url else f"**{current.title}**"

        # Status line
        loop_mode = guild_data['loop_mode']
        loop_labels = {"off": "\U0001f501 Off", "song": "\U0001f502 Song", "queue": "\U0001f501 Queue"}

        status_parts = [
            f"\U0001f50a {guild_data['volume']}%",
            f"{loop_labels.get(loop_mode, '\U0001f501 Off')}",
            f"\U0001f500 {'On' if guild_data['shuffle'] else 'Off'}",
            f"\u267e\ufe0f {'On' if guild_data['autoplay'] else 'Off'}",
        ]

        speed = guild_data.get("speed", 1.0)
        effect = guild_data.get("audio_effect", "none")
        if speed != 1.0:
            status_parts.append(f"\u23e9 {speed:.1f}x")
        if effect != "none":
            effect_name = AUDIO_EFFECTS.get(effect, {}).get("name", effect)
            status_parts.append(f"\U0001f3a7 {effect_name}")

        status_line = " \u2502 ".join(status_parts)

        queue_len = len(guild_data['queue'])
        footer = f"{current.requested_by} \u2022 {queue_len} in queue"

        # ── Build container ────────────────────────────────────────
        container = ui.Container(accent_colour=ACCENT_COLOUR)

        # Song info + status + thumbnail
        song_text = f"{status_icon} {title_line}\n*{current.uploader}*\n{status_line}\n-# {footer}"
        if current.thumbnail:
            container.add_item(ui.Section(
                ui.TextDisplay(song_text),
                accessory=ui.Thumbnail(current.thumbnail),
            ))
        else:
            container.add_item(ui.TextDisplay(song_text))

        # Progress bar
        container.add_item(ui.TextDisplay(progress_text))

        container.add_item(ui.Separator())

        # Buttons inside container
        container.add_item(ui.ActionRow(
            PlayPauseButton(), PreviousButton(), SkipButton(), StopButton(),
        ))
        container.add_item(ui.ActionRow(
            VolDownButton(), VolUpButton(), ShuffleButton(), LoopButton(),
        ))

        self.add_item(container)

    # ── Interaction check ──────────────────────────────────────────

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_banned(interaction.user.id):
            await interaction.response.send_message(
                embed=create_embed("Access Denied", "You are banned from using this bot.", COLOR, self.bot.user),
                ephemeral=True,
            )
            return False

        guild_data = self.bot.get_guild_data(self.guild_id)

        if not interaction.user.voice:
            await interaction.response.send_message(
                embed=create_embed("Error", "You must be in a voice channel!", COLOR, self.bot.user),
                ephemeral=True,
            )
            return False

        if not guild_data.get("voice_client"):
            await interaction.response.send_message(
                embed=create_embed("Error", "Bot is not connected to voice!", COLOR, self.bot.user),
                ephemeral=True,
            )
            return False

        if guild_data["voice_client"].channel != interaction.user.voice.channel:
            await interaction.response.send_message(
                embed=create_embed("Error", "You must be in the same voice channel as the bot!", COLOR, self.bot.user),
                ephemeral=True,
            )
            return False

        return True
