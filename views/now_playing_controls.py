import logging

import discord
from discord import ui

from activity.permissions import dj_role_id, has_dj_permission
from config import AUDIO_EFFECTS, COLOR
from utils.ban_system import is_banned
from utils.helpers import build_progress_bar, create_embed, format_duration, md

logger = logging.getLogger(__name__)

ACCENT_COLOUR = discord.Colour(COLOR)
LOOP_EMOJI = {"off": "\U0001f501", "song": "\U0001f502", "queue": "\U0001f501"}
LOOP_LABEL = {"off": "\U0001f501 Off", "song": "\U0001f502 Song", "queue": "\U0001f501 Queue"}


# ── Buttons (LayoutView dispatches via callback) ───────────────────

class _NPButton(ui.Button):
    @property
    def panel(self) -> NowPlayingControls:
        return self.view  # type: ignore[return-value]

    async def refresh(self, interaction: discord.Interaction):
        """Answer by redrawing the panel, so the new state shows where the button was pressed."""
        p = self.panel
        playback = p.bot.playback
        view = playback.panel.view(
            p.guild_id, current_position=playback.get_current_position(p.guild_id),
            is_paused=playback.is_paused(p.guild_id),
        )
        try:
            await interaction.response.edit_message(view=view)
            p.bot.get_guild_data(p.guild_id)["now_playing_view"] = view
        except discord.HTTPException:
            if not interaction.response.is_done():
                await interaction.response.defer()

    @staticmethod
    async def say(interaction: discord.Interaction, text: str):
        await interaction.response.send_message(text, ephemeral=True, delete_after=3)


class PlayPauseButton(_NPButton):
    def __init__(self, paused: bool):
        super().__init__(
            emoji="▶️" if paused else "⏸️", style=discord.ButtonStyle.secondary,
            custom_id="np_play_pause",
        )

    async def callback(self, interaction: discord.Interaction):
        p = self.panel
        playback = p.bot.playback
        if playback.handle_pause(p.guild_id) or playback.handle_resume(p.guild_id):
            await self.refresh(interaction)
        else:
            await self.say(interaction, "Nothing to play or pause")


class PreviousButton(_NPButton):
    def __init__(self):
        super().__init__(emoji="⏮️", style=discord.ButtonStyle.secondary, custom_id="np_previous")

    async def callback(self, interaction: discord.Interaction):
        p = self.panel
        # The previous song posts its own panel, so this one just acknowledges.
        await interaction.response.defer()
        if not await p.bot.playback.play_previous(p.guild_id):
            await interaction.followup.send("No previous song available", ephemeral=True)


class SkipButton(_NPButton):
    def __init__(self):
        super().__init__(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="np_skip")

    async def callback(self, interaction: discord.Interaction):
        p = self.panel
        if await p.bot.playback.skip(p.guild_id):
            await interaction.response.defer()
        else:
            await self.say(interaction, "Nothing to skip")


class StopButton(_NPButton):
    def __init__(self):
        super().__init__(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="np_stop")

    async def callback(self, interaction: discord.Interaction):
        p = self.panel
        # Stop deletes this panel.
        await interaction.response.defer()
        await p.bot.playback.stop(p.guild_id)
        await interaction.followup.send("⏹️ Stopped and queue cleared", ephemeral=True)


class VolumeButton(_NPButton):
    def __init__(self, step: int):
        super().__init__(
            emoji="\U0001f50a" if step > 0 else "\U0001f509", style=discord.ButtonStyle.secondary,
            custom_id="np_vol_up" if step > 0 else "np_vol_down",
        )
        self.step = step

    async def callback(self, interaction: discord.Interaction):
        p = self.panel
        guild_data = p.bot.get_guild_data(p.guild_id)
        guild_data["volume"] = max(0, min(100, guild_data["volume"] + self.step))
        voice_client = guild_data.get("voice_client")
        if voice_client and isinstance(voice_client.source, discord.PCMVolumeTransformer):
            voice_client.source.volume = guild_data["volume"] / 100
        await p.bot.save_guild_queue(p.guild_id)
        await self.refresh(interaction)


class ShuffleButton(_NPButton):
    def __init__(self, on: bool):
        super().__init__(
            emoji="\U0001f500", custom_id="np_shuffle",
            style=discord.ButtonStyle.success if on else discord.ButtonStyle.secondary,
        )

    async def callback(self, interaction: discord.Interaction):
        p = self.panel
        p.bot.playback.queue_service.toggle_shuffle(p.guild_id)
        await p.bot.save_guild_queue(p.guild_id)
        await self.refresh(interaction)


class LoopButton(_NPButton):
    def __init__(self, mode: str):
        super().__init__(
            emoji=LOOP_EMOJI.get(mode, LOOP_EMOJI["off"]), custom_id="np_loop",
            style=discord.ButtonStyle.secondary if mode == "off" else discord.ButtonStyle.success,
        )

    async def callback(self, interaction: discord.Interaction):
        p = self.panel
        guild_data = p.bot.get_guild_data(p.guild_id)
        modes = ["off", "song", "queue"]
        current = guild_data.get("loop_mode", "off")
        new_mode = modes[(modes.index(current) + 1) % len(modes)] if current in modes else "song"
        p.bot.playback.queue_service.set_loop_mode(p.guild_id, new_mode)
        await p.bot.save_guild_queue(p.guild_id)
        await self.refresh(interaction)


# ── Main layout ────────────────────────────────────────────────────

class NowPlayingControls(ui.LayoutView):
    """Components V2 now-playing layout with integrated controls."""

    def __init__(self, bot, guild_id: int, *, current_position: int = 0, is_paused: bool = False,
                 controls: bool = True):
        super().__init__(timeout=None)
        # Callers used to pass the music cog; anything with a .bot works the same.
        self.bot = getattr(bot, "bot", bot)
        self.guild_id = guild_id
        self._build_layout(current_position, is_paused, controls)

    def _build_layout(self, current_position: int, is_paused: bool, controls: bool = True):
        guild_data = self.bot.get_guild_data(self.guild_id)
        current = guild_data.get("current")
        if not current:
            return

        is_live = current.is_live or not current.duration
        voice_client = guild_data.get("voice_client")

        # Activity-only playback has no voice client; its clock runs while it plays.
        playing = voice_client.is_playing() if voice_client else bool(guild_data.get("start_time"))
        if is_paused:
            status_icon = "⏸️"
        elif not playing:
            status_icon = "⏹️"
        else:
            status_icon = "▶️"

        if is_live:
            progress_text = f"\U0001f534 `LIVE — {format_duration(current_position)}`"
        else:
            bar = build_progress_bar(current_position, current.duration, length=16)
            progress_text = f"`{format_duration(current_position)}` {bar} `{format_duration(current.duration)}`"

        loop_mode = guild_data.get("loop_mode", "off")
        status_parts = [
            f"\U0001f50a {guild_data['volume']}%",
            LOOP_LABEL.get(loop_mode, LOOP_LABEL["off"]),
            f"\U0001f500 {'On' if guild_data['shuffle'] else 'Off'}",
            f"♾️ {'On' if guild_data['autoplay'] else 'Off'}",
        ]
        speed = guild_data.get("speed", 1.0)
        effect = guild_data.get("audio_effect", "none")
        if speed != 1.0:
            status_parts.append(f"⏩ {speed:.1f}x")
        if effect != "none":
            status_parts.append(f"\U0001f3a7 {AUDIO_EFFECTS.get(effect, {}).get('name', effect)}")

        footer = f"{current.requested_by} • {len(guild_data['queue'])} in queue"
        if current.autoplay_source == "lastfm":
            # Last.fm's terms ask for attribution of what its data picked
            footer += " • via [Last.fm](<https://www.last.fm/>)"

        container = ui.Container(accent_colour=ACCENT_COLOUR)
        song_text = (
            f"{status_icon} {current.linked_title}\n*{md(current.uploader)}*\n"
            f"{' │ '.join(status_parts)}\n-# {footer}"
        )
        if current.thumbnail:
            container.add_item(ui.Section(ui.TextDisplay(song_text), accessory=ui.Thumbnail(current.thumbnail)))
        else:
            container.add_item(ui.TextDisplay(song_text))
        container.add_item(ui.TextDisplay(progress_text))
        if not controls:
            self.add_item(container)
            return
        container.add_item(ui.Separator())
        container.add_item(ui.ActionRow(
            PlayPauseButton(is_paused), PreviousButton(), SkipButton(), StopButton(),
        ))
        container.add_item(ui.ActionRow(
            VolumeButton(-10), VolumeButton(10), ShuffleButton(guild_data["shuffle"]), LoopButton(loop_mode),
        ))
        self.add_item(container)

    # ── Interaction check ──────────────────────────────────────────

    async def _refuse(self, interaction: discord.Interaction, text: str) -> bool:
        await interaction.response.send_message(
            embed=create_embed("Error", text, COLOR, self.bot.user), ephemeral=True,
        )
        return False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_banned(interaction.user.id):
            await interaction.response.send_message(
                embed=create_embed("Access Denied", "You are banned from using this bot.", COLOR, self.bot.user),
                ephemeral=True,
            )
            return False

        guild_data = self.bot.get_guild_data(self.guild_id)
        voice_client = guild_data.get("voice_client")
        user_voice = getattr(interaction.user, "voice", None)
        if not user_voice:
            return await self._refuse(interaction, "You must be in a voice channel!")
        if not voice_client:
            return await self._refuse(interaction, "Bot is not connected to voice!")
        if voice_client.channel != user_voice.channel:
            return await self._refuse(interaction, "You must be in the same voice channel as the bot!")

        # Every button does what a DJ-only slash command does.
        guild = interaction.guild
        if guild is not None and not has_dj_permission(self.bot, guild, interaction.user):
            role = guild.get_role(dj_role_id(self.bot, guild) or 0)
            return await self._refuse(
                interaction, f"These controls need the **{md(role.name) if role else 'DJ'}** role or Administrator.",
            )
        return True
