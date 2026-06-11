import asyncio
import logging

from activity.helpers import is_activity_paused
from activity.state_serializer import _get_activity_position

logger = logging.getLogger(__name__)


async def _broadcast_positions(bot, ws_manager):
    """Broadcast current playback position to all connected Activity clients every second."""
    while True:
        try:
            await asyncio.sleep(1)

            guild_ids = ws_manager.get_guild_ids_with_connections()
            if not guild_ids:
                continue

            for guild_id in guild_ids:
                try:
                    guild_data = bot.get_guild_data(guild_id)
                    if not guild_data.get("current"):
                        continue

                    is_paused = is_activity_paused(guild_data)

                    # Skip broadcasting if paused (position doesn't change)
                    if is_paused:
                        continue

                    position = _get_activity_position(bot, guild_id, guild_data)

                    await ws_manager.broadcast(guild_id, "POSITION_UPDATE", {
                        "position": position,
                        "is_paused": False,
                    })
                except Exception as e:
                    logger.debug(f"Position broadcast error for guild {guild_id}: {e}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Position broadcaster error: {e}")
            await asyncio.sleep(5)


def start_position_broadcaster(bot, ws_manager) -> asyncio.Task:
    return asyncio.create_task(_broadcast_positions(bot, ws_manager))
