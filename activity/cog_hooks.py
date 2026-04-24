import asyncio
import functools
import logging

from activity.state_serializer import serialize_guild_state

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
                loop = bot.loop or asyncio.get_event_loop()
                data = serialize_guild_state(bot, guild_id)
                asyncio.run_coroutine_threadsafe(
                    ws_manager.broadcast(guild_id, event_type, data),
                    loop,
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
            try:
                data = serialize_guild_state(bot, guild_id)
                await ws_manager.broadcast(guild_id, event_type, data)
            except Exception as e:
                logger.debug(f"Broadcast failed for {event_type}: {e}")
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

    # QueueService is intentionally not hooked — routes broadcast after
    # each operation; hooking caused double-broadcasts with race conditions.

    logger.info("Activity broadcast hooks installed")
