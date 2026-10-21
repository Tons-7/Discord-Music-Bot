"""The Discord now-playing panel and channel notices, owned by PlaybackService."""

import asyncio
import contextlib
import logging
import time

import discord

from config import COLOR
from utils.helpers import create_embed

logger = logging.getLogger(__name__)


def _can_post(guild, channel) -> bool:
    permissions = channel.permissions_for(guild.me)
    return permissions.view_channel and permissions.send_messages


class NowPlayingPanel:
    def __init__(self, bot):
        self.bot = bot
        # One panel change per guild at a time: two sends racing would each post a panel and orphan one.
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    def music_channel(self, guild_id: int):
        """Where panels and notices go: the music channel, else the channel music commands were last used in.

        A music channel the bot can't reach is skipped, not forgotten: it may come back (permissions, threads).
        """
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return None
        guild_data = self.bot.get_guild_data(guild_id)
        for channel_id in (guild_data.get("music_channel_id"), guild_data.get("command_channel_id")):
            channel = guild.get_channel_or_thread(channel_id) if channel_id else None
            if channel is not None and _can_post(guild, channel):
                return channel
        # A session restored after a restart, before anyone used a command
        return next((c for c in guild.text_channels if _can_post(guild, c)), None)

    def view(self, guild_id: int, *, current_position: int = 0, is_paused: bool = False):
        from views.now_playing_controls import NowPlayingControls
        return NowPlayingControls(self.bot, guild_id, current_position=current_position, is_paused=is_paused)

    async def send(self, guild_id: int, *, current_position: int = 0, is_paused: bool = False, replacing=None):
        """Replace the panel with a new message at the bottom of the channel.

        With ``replacing``, only if that message is still the panel (a song change may have replaced it already).
        """
        guild_data = self.bot.get_guild_data(guild_id)
        channel = self.music_channel(guild_id)
        if not channel:
            return None

        async with self._lock(guild_id):
            if replacing is not None and guild_data.get("now_playing_message") is not replacing:
                return None
            await self._clear(guild_id)
            view = self.view(guild_id, current_position=current_position, is_paused=is_paused)
            try:
                message = await channel.send(view=view, silent=True)
            except discord.HTTPException as e:
                logger.error(f"Failed to create now playing message: {e}")
                return None
            guild_data["now_playing_message"] = message
            guild_data["now_playing_view"] = view
            guild_data["now_playing_message_sent_time"] = time.monotonic()

        await asyncio.sleep(0.2)
        guild_data["message_ready_for_timestamps"] = guild_data.get("now_playing_message") is message
        return message

    async def refresh(self, guild_id: int) -> None:
        """A fresh panel for the current song, or no panel when nothing plays."""
        guild_data = self.bot.get_guild_data(guild_id)
        if not guild_data.get("current"):
            await self.clear(guild_id)
            return
        playback = self.bot.playback
        await self.send(
            guild_id, current_position=playback.get_current_position(guild_id), is_paused=playback.is_paused(guild_id),
        )

    def forget(self, guild_id: int) -> None:
        """Stop tracking the panel (it was deleted, or can't be edited) and release its buttons."""
        guild_data = self.bot.get_guild_data(guild_id)
        guild_data["now_playing_message"] = None
        guild_data["message_ready_for_timestamps"] = False
        # Only a deleted message's view is stopped: stopping unregisters its buttons by id, which a newer view
        # on the same message would share.
        view = guild_data.pop("now_playing_view", None)
        if view is not None:
            view.stop()

    async def clear(self, guild_id: int) -> None:
        """Delete the panel, so no frozen panel with dead buttons is left behind."""
        async with self._lock(guild_id):
            await self._clear(guild_id)

    async def _clear(self, guild_id: int) -> None:
        message = self.bot.get_guild_data(guild_id).get("now_playing_message")
        if message:
            with contextlib.suppress(discord.HTTPException):
                await message.delete()
        self.forget(guild_id)

    async def notify(self, guild_id: int, title: str, description: str, *, delete_after: float | None = None):
        channel = self.music_channel(guild_id)
        if not channel:
            return
        try:
            await channel.send(embed=create_embed(title, description, COLOR, self.bot.user), delete_after=delete_after)
        except discord.HTTPException as e:
            logger.warning(f"Failed to send '{title}' notice in guild {guild_id}: {e}")
