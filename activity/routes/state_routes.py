from fastapi import APIRouter

from activity.dependencies import DJ, Bot, Member, WsManager
from activity.helpers import broadcast_state
from activity.state_serializer import serialize_guild_state

router = APIRouter(prefix="/api/guild/{guild_id}", tags=["state"])


@router.get("/state")
async def get_state(guild_id: int, user: Member, bot: Bot):
    return serialize_guild_state(bot, guild_id)


@router.post("/history/clear")
async def clear_history(guild_id: int, user: DJ, bot: Bot, ws: WsManager):
    guild_data = bot.get_guild_data(guild_id)
    guild_data["history"].clear()
    await bot.save_guild_queue(guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True}
