import asyncio
import functools
import logging

from activity.helpers import broadcast_state
from activity.state_serializer import serialize_guild_state
from activity.tasks import spawn

logger = logging.getLogger(__name__)


def _get_guild_id_from_args(args):
    """Extract guild_id from method args (always first positional arg after self)."""
    return args[0] if args else None


def _wrap_sync(original, bot, ws_manager, event_type):
    """Wrap a synchronous service method to broadcast after it returns."""
    @functools.wraps(original)
    def wrapper(*args, **kwargs):
        result = original(*args, **kwargs)
        guild_id = _get_guild_id_from_args(args)
        if guild_id and ws_manager.has_connections(guild_id):
            try:
                data = serialize_guild_state(bot, guild_id)
                asyncio.run_coroutine_threadsafe(
                    ws_manager.broadcast(guild_id, event_type, data),
                    bot.loop,
                )
            except Exception as e:
                logger.debug(f"Broadcast failed for {event_type}: {e}")
        return result
    return wrapper


def _wrap_async(original, bot, ws_manager, event_type):
    """Wrap an async service method to broadcast after it returns."""
    @functools.wraps(original)
    async def wrapper(*args, **kwargs):
        result = await original(*args, **kwargs)
        guild_id = _get_guild_id_from_args(args)
        if guild_id and ws_manager.has_connections(guild_id):
            # Broadcast off the critical path. _start_playback / _handle_empty_queue
            # run inside play_next while holding play_lock; awaiting a (possibly
            # backpressured) WebSocket send here would prolong the lock hold.
            async def _broadcast():
                try:
                    data = serialize_guild_state(bot, guild_id)
                    await ws_manager.broadcast(guild_id, event_type, data)
                except Exception as e:
                    logger.debug(f"Broadcast failed for {event_type}: {e}")
            spawn(_broadcast())
        return result
    return wrapper


def _wrap_async_coalesced(original, bot, ws_manager):
    """Wrap an async method to schedule a coalesced STATE_UPDATE after it returns."""
    @functools.wraps(original)
    async def wrapper(*args, **kwargs):
        result = await original(*args, **kwargs)
        guild_id = _get_guild_id_from_args(args)
        if guild_id:
            await broadcast_state(bot, ws_manager, guild_id)
        return result
    return wrapper


def install_broadcast_hooks(bot, ws_manager):
    """Wrap key service methods to broadcast state changes to Activity clients."""
    playback = bot._playback_service

    playback._start_playback = _wrap_async(
        playback._start_playback, bot, ws_manager, "STATE_UPDATE"
    )
    playback._handle_empty_queue = _wrap_async(
        playback._handle_empty_queue, bot, ws_manager, "STATE_UPDATE"
    )
    playback.handle_pause = _wrap_sync(
        playback.handle_pause, bot, ws_manager, "PLAYBACK_STATE"
    )
    playback.handle_resume = _wrap_sync(
        playback.handle_resume, bot, ws_manager, "PLAYBACK_STATE"
    )

    # Persistence calls end nearly every mutation, including commands and buttons.
    bot.save_guild_queue = _wrap_async_coalesced(bot.save_guild_queue, bot, ws_manager)
    bot.clear_guild_queue_from_db = _wrap_async_coalesced(bot.clear_guild_queue_from_db, bot, ws_manager)
    bot._cleanup_after_failed_reconnect = _wrap_async_coalesced(
        bot._cleanup_after_failed_reconnect, bot, ws_manager
    )

    async def _on_app_command_completion(interaction, _command):
        if interaction.guild_id:
            await broadcast_state(bot, ws_manager, interaction.guild_id)

    async def _on_voice_state_update(member, before, after):
        if bot.user and member.id == bot.user.id and before.channel != after.channel:
            await broadcast_state(bot, ws_manager, member.guild.id)

    bot.add_listener(_on_app_command_completion, "on_app_command_completion")
    bot.add_listener(_on_voice_state_update, "on_voice_state_update")

    logger.info("Activity broadcast hooks installed")
