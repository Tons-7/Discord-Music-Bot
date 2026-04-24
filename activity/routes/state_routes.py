from fastapi import APIRouter, Depends, Query, Request

from activity.dependencies import get_bot, get_current_user, require_guild_member
from activity.state_serializer import serialize_guild_state, serialize_song
from config import SONGS_PER_PAGE

router = APIRouter(prefix="/api/guild/{guild_id}", tags=["state"])


@router.get("/state")
async def get_state(guild_id: int, user=Depends(get_current_user), bot=Depends(get_bot)):
    require_guild_member(bot, guild_id, int(user["id"]))
    return serialize_guild_state(bot, guild_id)


@router.get("/queue")
async def get_queue(
    guild_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(SONGS_PER_PAGE, ge=1, le=50),
    user=Depends(get_current_user),
    bot=Depends(get_bot),
):
    require_guild_member(bot, guild_id, int(user["id"]))
    queue_service = bot._playback_service.queue_service
    queue = queue_service.get_visible_queue(guild_id)

    total = len(queue)
    start = (page - 1) * per_page
    end = start + per_page
    page_songs = queue[start:end]

    return {
        "songs": [serialize_song(s) for s in page_songs],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
        "queue_duration": queue_service.get_queue_duration(guild_id),
    }


@router.get("/history")
async def get_history(guild_id: int, user=Depends(get_current_user), bot=Depends(get_bot)):
    require_guild_member(bot, guild_id, int(user["id"]))
    guild_data = bot.get_guild_data(guild_id)
    history = guild_data.get("history", [])
    return {"songs": [serialize_song(s) for s in reversed(history)]}


@router.post("/history/clear")
async def clear_history(guild_id: int, request: Request, user=Depends(get_current_user), bot=Depends(get_bot)):
    require_guild_member(bot, guild_id, int(user["id"]))
    guild_data = bot.get_guild_data(guild_id)
    guild_data["history"].clear()
    guild_data["history_position"] = 0
    await bot.save_guild_queue(guild_id)

    ws = request.app.state.ws_manager
    if ws.has_connections(guild_id):
        data = serialize_guild_state(bot, guild_id)
        await ws.broadcast(guild_id, "STATE_UPDATE", data)

    return {"ok": True}
