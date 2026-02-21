import logging
from typing import TYPE_CHECKING

import discord

from utils.ban_system import is_banned

if TYPE_CHECKING:
    from cogs.music_commands import MusicCommands

logger = logging.getLogger(__name__)


class NowPlayingControls(discord.ui.View):

    def __init__(self, music_commands_cog: 'MusicCommands', guild_id: int, timeout: int = None):
        super().__init__(timeout=timeout)
        self.music_commands_cog = music_commands_cog
        self.guild_id = guild_id
        self.bot = music_commands_cog.bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_banned(interaction.user.id):
            await interaction.response.send_message(
                "You are banned from using this bot.",
                ephemeral=True
            )
            return False

        guild_data = self.bot.get_guild_data(self.guild_id)

        if not interaction.user.voice:
            await interaction.response.send_message(
                "You must be in a voice channel to use these controls!",
                ephemeral=True
            )
            return False

        if not guild_data.get("voice_client"):
            await interaction.response.send_message(
                "Bot is not connected to voice!",
                ephemeral=True
            )
            return False

        if guild_data["voice_client"].channel != interaction.user.voice.channel:
            await interaction.response.send_message(
                f"You must be in the same voice channel as the bot!",
                ephemeral=True
            )
            return False

        return True

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary, custom_id="play_pause")
    async def play_pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            guild_data = self.bot.get_guild_data(self.guild_id)

            if guild_data["voice_client"].is_playing():
                self.music_commands_cog.playback_service.handle_pause(self.guild_id)
                await interaction.response.send_message("⏸️ Paused", ephemeral=True, delete_after=2)
            elif guild_data["voice_client"].is_paused():
                self.music_commands_cog.playback_service.handle_resume(self.guild_id)
                await interaction.response.send_message("▶️ Resumed", ephemeral=True, delete_after=2)
            else:
                await interaction.response.send_message("Nothing to play/pause", ephemeral=True, delete_after=2)
        except Exception as e:
            logger.error(f"Play/Pause button error: {e}")
            await interaction.response.send_message("An error occurred", ephemeral=True, delete_after=2)

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.primary, custom_id="previous")
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            success = await self.music_commands_cog.play_previous(self.guild_id)
            if success:
                await interaction.response.send_message("⏮️ Playing previous", ephemeral=True, delete_after=2)
            else:
                await interaction.response.send_message("No previous song available", ephemeral=True, delete_after=2)
        except Exception as e:
            logger.error(f"Previous button error: {e}")
            await interaction.response.send_message("An error occurred", ephemeral=True, delete_after=2)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.primary, custom_id="skip")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            guild_data = self.bot.get_guild_data(self.guild_id)

            if guild_data["voice_client"] and (
                    guild_data["voice_client"].is_playing() or guild_data["voice_client"].is_paused()
            ):
                if guild_data["current"]:
                    self.music_commands_cog.queue_service.add_to_history(self.guild_id, guild_data["current"])

                guild_data["voice_client"].stop()
                await interaction.response.send_message("⏭️ Skipped", ephemeral=True, delete_after=2)
            else:
                await interaction.response.send_message("Nothing to skip", ephemeral=True, delete_after=2)
        except Exception as e:
            logger.error(f"Skip button error: {e}")
            await interaction.response.send_message("An error occurred", ephemeral=True, delete_after=2)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.primary, custom_id="shuffle")
    async def shuffle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            shuffle_state = self.music_commands_cog.queue_service.toggle_shuffle(self.guild_id)
            await self.bot.save_guild_queue(self.guild_id)

            status = "enabled" if shuffle_state else "disabled"
            await interaction.response.send_message(f"🔀 Shuffle {status}", ephemeral=True, delete_after=2)
        except Exception as e:
            logger.error(f"Shuffle button error: {e}")
            await interaction.response.send_message("An error occurred", ephemeral=True, delete_after=2)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.primary, custom_id="loop")
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            guild_data = self.bot.get_guild_data(self.guild_id)

            modes = ["off", "song", "queue"]
            current_mode = guild_data.get("loop_mode", "off")
            if current_mode not in modes:
                current_mode = "off"
            current_index = modes.index(current_mode)
            new_mode = modes[(current_index + 1) % len(modes)]

            self.music_commands_cog.queue_service.set_loop_mode(self.guild_id, new_mode)
            await self.bot.save_guild_queue(self.guild_id)

            mode_emojis = {"off": "🔄", "song": "🔂", "queue": "🔁"}
            await interaction.response.send_message(
                f"{mode_emojis[new_mode]} Loop: {new_mode.title()}",
                ephemeral=True,
                delete_after=3
            )
        except Exception as e:
            logger.error(f"Loop button error: {e}")
            await interaction.response.send_message("An error occurred", ephemeral=True, delete_after=2)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="stop")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            guild_data = self.bot.get_guild_data(self.guild_id)

            self.music_commands_cog.queue_service.clear_queue(self.guild_id)
            guild_data["current"] = None
            guild_data["start_time"] = None

            if guild_data["voice_client"] and (
                    guild_data["voice_client"].is_playing() or guild_data["voice_client"].is_paused()
            ):
                guild_data["voice_client"].stop()

            await self.bot.clear_guild_queue_from_db(self.guild_id)
            await interaction.response.send_message("⏹️ Stopped and queue cleared", ephemeral=True, delete_after=2)
        except Exception as e:
            logger.error(f"Stop button error: {e}")
            await interaction.response.send_message("An error occurred", ephemeral=True, delete_after=2)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.success, custom_id="volume_up")
    async def volume_up_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            guild_data = self.bot.get_guild_data(self.guild_id)

            new_volume = min(100, guild_data["volume"] + 10)
            guild_data["volume"] = new_volume

            if guild_data["voice_client"] and guild_data["voice_client"].source:
                try:
                    guild_data["voice_client"].source.volume = new_volume / 100
                except AttributeError:
                    pass

            await self.bot.save_guild_queue(self.guild_id)
            await interaction.response.send_message(f"🔊 Volume: {new_volume}%", ephemeral=True, delete_after=2)
        except Exception as e:
            logger.error(f"Volume up button error: {e}")
            await interaction.response.send_message("An error occurred", ephemeral=True, delete_after=2)

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.success, custom_id="volume_down")
    async def volume_down_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            guild_data = self.bot.get_guild_data(self.guild_id)

            new_volume = max(0, guild_data["volume"] - 10)
            guild_data["volume"] = new_volume

            if guild_data["voice_client"] and guild_data["voice_client"].source:
                try:
                    guild_data["voice_client"].source.volume = new_volume / 100
                except AttributeError:
                    pass

            await self.bot.save_guild_queue(self.guild_id)
            await interaction.response.send_message(f"🔉 Volume: {new_volume}%", ephemeral=True, delete_after=2)
        except Exception as e:
            logger.error(f"Volume down button error: {e}")
            await interaction.response.send_message("An error occurred", ephemeral=True, delete_after=2)
