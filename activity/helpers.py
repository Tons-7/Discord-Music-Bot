import asyncio
import logging
from datetime import datetime
from typing import Optional

from activity.state_serializer import serialize_guild_state

logger = logging.getLogger(__name__)


def member_avatar_url(member, size: int = 128) -> Optional[str]:
    """Return a member's avatar URL, preserving animation for animated avatars."""
    if not member or not member.display_avatar:
        return None
    avatar = member.display_avatar
    fmt = "gif" if avatar.is_animated() else "png"
    return str(avatar.replace(size=size, format=fmt))


async def record_activity_listening(bot, ws_manager, guild_id: int, user_ids: set[int] | None = None):
    """Record listening stats for Activity users on song end.

    Skips when voice client is connected (PlaybackService handles those).
    Pass user_ids explicitly when WS connections are already cleared (on_last_disconnect).
    """
    guild_data = bot.get_guild_data(guild_id)

    vc = guild_data.get("voice_client")
    if vc and vc.is_connected():
        return

    current = guild_data.get("current")
    if not current:
        return

    start_time = guild_data.get("start_time")
    if not start_time:
        pause_pos = guild_data.get("pause_position")
        if not pause_pos or pause_pos <= 0:
            return
        duration = int(pause_pos)
    else:
        elapsed = (datetime.now() - start_time).total_seconds()
        duration = int(elapsed * bot._playback_service.get_effective_speed(guild_data))

    if duration <= 0:
        return
    if current.duration and current.duration > 0:
        duration = min(duration, current.duration)

    if user_ids is None:
        if not ws_manager:
            return
        user_ids = ws_manager.get_connected_user_ids(guild_id)

    if not user_ids:
        return

    await asyncio.gather(*(
        bot.record_listening_stat(uid, guild_id, current, duration)
        for uid in user_ids
    ))

    logger.debug(f"Activity stats: {len(user_ids)} users, {duration}s on '{current.title}'")


async def broadcast_state(bot, ws_manager, guild_id: int):
    """Broadcast guild state to connected Activity clients (0.1s yield for ordering)."""
    if not ws_manager or not ws_manager.has_connections(guild_id):
        return
    await asyncio.sleep(0.1)
    data = serialize_guild_state(bot, guild_id)
    await ws_manager.broadcast(guild_id, "STATE_UPDATE", data)


def set_current_for_activity(guild_data: dict, song):
    """Set a song as current for Activity-only playback (no voice client)."""
    guild_data["current"] = song
    guild_data["seek_offset"] = 0
    guild_data["start_time"] = datetime.now()
    guild_data["pause_position"] = None
