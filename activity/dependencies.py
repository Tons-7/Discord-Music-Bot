from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request

from activity.auth import get_discord_user, require_member
from activity.permissions import check_banned, check_dj_permission, voice_control_error


async def get_bot(request: Request):
    return request.app.state.bot


async def get_ws_manager(request: Request):
    return request.app.state.ws_manager


async def get_current_user(request: Request) -> dict:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    user = await get_discord_user(request.app.state.bot.http_session, auth_header[7:])
    if not user:
        raise HTTPException(status_code=401, detail="Invalid access token")

    if check_banned(int(user["id"])):
        raise HTTPException(status_code=403, detail="You are banned from using this bot")

    return user


def require_guild_member(bot, guild_id: int, user_id: int):
    return require_member(bot, guild_id, user_id)


def require_dj(bot, guild_id: int, user_id: int):
    if not check_dj_permission(bot, guild_id, user_id):
        raise HTTPException(status_code=403, detail="DJ role required for this action")


def require_voice_control(bot, guild_id: int, user_id: int):
    """While the bot is in voice, only people in its channel may change what it plays."""
    guild = bot.get_guild(guild_id)
    member = guild.get_member(user_id) if guild else None
    if error := voice_control_error(bot, guild_id, member):
        raise HTTPException(status_code=403, detail=error)


async def guild_member(guild_id: int, user=Depends(get_current_user), bot=Depends(get_bot)) -> dict:
    require_guild_member(bot, guild_id, int(user["id"]))
    return user


async def dj_member(guild_id: int, user=Depends(guild_member), bot=Depends(get_bot)) -> dict:
    require_dj(bot, guild_id, int(user["id"]))
    return user


async def voice_member(guild_id: int, user=Depends(guild_member), bot=Depends(get_bot)) -> dict:
    require_voice_control(bot, guild_id, int(user["id"]))
    return user


async def voice_dj(guild_id: int, user=Depends(dj_member), bot=Depends(get_bot)) -> dict:
    require_voice_control(bot, guild_id, int(user["id"]))
    return user


Bot = Annotated[Any, Depends(get_bot)]
WsManager = Annotated[Any, Depends(get_ws_manager)]
User = Annotated[dict, Depends(get_current_user)]
Member = Annotated[dict, Depends(guild_member)]
DJ = Annotated[dict, Depends(dj_member)]
# Changes what the bot plays: in voice, the caller must be in the bot's channel too.
Listener = Annotated[dict, Depends(voice_member)]
Controller = Annotated[dict, Depends(voice_dj)]
