from fastapi import Depends, HTTPException, Request

from activity.auth import get_discord_user
from activity.permissions import check_banned, check_dj_permission


async def get_bot(request: Request):
    return request.app.state.bot


async def get_ws_manager(request: Request):
    return request.app.state.ws_manager


async def get_current_user(request: Request) -> dict:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = auth_header[7:]
    user = await get_discord_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid access token")

    user_id = int(user["id"])
    if check_banned(user_id):
        raise HTTPException(status_code=403, detail="You are banned from using this bot")

    return user


def require_guild_member(bot, guild_id: int, user_id: int):
    guild = bot.get_guild(guild_id)
    if not guild:
        raise HTTPException(status_code=404, detail="Bot is not in this guild")

    member = guild.get_member(user_id)
    if not member:
        raise HTTPException(status_code=403, detail="You are not a member of this guild")

    return member


def require_dj(bot, guild_id: int, user_id: int):
    if not check_dj_permission(bot, guild_id, user_id):
        raise HTTPException(status_code=403, detail="DJ role required for this action")
