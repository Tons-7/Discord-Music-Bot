import asyncio
import logging

from activity.helpers import is_activity_paused
from activity.state_serializer import position_now

logger = logging.getLogger(__name__)


async def _broadcast_positions(bot, ws_manager):
    """Broadcast current playback position to all connected Activity clients every second."""
    while True:
        try:
            await asyncio.sleep(1)

            for guild_id in ws_manager.get_guild_ids_with_connections():
                try:
                    guild_data = bot.get_guild_data(guild_id)
                    current = guild_data.get("current")
                    # A paused position doesn't move; the state update already carries it.
                    if not current or is_activity_paused(guild_data):
                        continue

                    # Names the song (and its play) so a tick sent during a song change can't rewind the next one.
                    await ws_manager.broadcast(guild_id, "POSITION_UPDATE", {
                        "position": round(position_now(bot, guild_data), 1),
                        "is_paused": False,
                        "webpage_url": current.webpage_url,
                        "seq": guild_data.get("song_seq", 0),
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
