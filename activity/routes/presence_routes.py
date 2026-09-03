import logging

from fastapi import APIRouter, Depends

from activity.dependencies import get_bot, get_ws_manager, guild_member
from activity.helpers import member_avatar_url

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/guild/{guild_id}", tags=["presence"])


@router.get("/listeners")
async def list_listeners(
    guild_id: int,
    user=Depends(guild_member),
    bot=Depends(get_bot),
    ws=Depends(get_ws_manager),
):
    """Who currently has the Activity open in this guild."""
    guild = bot.get_guild(guild_id)
    listeners = []

    for uid in ws.get_connected_user_ids(guild_id):
        if not uid:
            continue
        member = guild.get_member(uid) if guild else None
        listeners.append({
            "id": str(uid),
            "name": member.display_name if member else "Unknown",
            "avatar": member_avatar_url(member, 64) if member else None,
        })

    listeners.sort(key=lambda listener: listener["name"].lower())
    return {"listeners": listeners, "count": len(listeners)}
