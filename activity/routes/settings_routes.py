import logging
from typing import Optional

import discord
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from activity.dependencies import get_bot, guild_member, require_guild_member

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/guild/{guild_id}/settings", tags=["settings"])


def _require_admin(bot, guild_id: int, user_id: int):
    member = require_guild_member(bot, guild_id, user_id)
    if not member.guild_permissions.administrator:
        raise HTTPException(status_code=403, detail="Administrator permission required")
    return member


@router.get("")
async def get_settings(guild_id: int, user=Depends(guild_member), bot=Depends(get_bot)):
    """Guild settings, plus the pickable roles/channels when the caller is an admin."""
    guild_data = bot.get_guild_data(guild_id)
    guild = bot.get_guild(guild_id)
    member = guild.get_member(int(user["id"])) if guild else None
    is_admin = bool(member and member.guild_permissions.administrator)

    dj_role_id = guild_data.get("dj_role_id")
    music_channel_id = guild_data.get("music_channel_id")
    dj_role = guild.get_role(dj_role_id) if guild and dj_role_id else None
    music_channel = guild.get_channel(music_channel_id) if guild and music_channel_id else None

    payload = {
        "is_admin": is_admin,
        "dj_role_id": str(dj_role_id) if dj_role_id else None,
        "dj_role_name": dj_role.name if dj_role else None,
        "music_channel_id": str(music_channel_id) if music_channel_id else None,
        "music_channel_name": music_channel.name if music_channel else None,
    }

    if is_admin and guild:
        payload["roles"] = [
            {"id": str(r.id), "name": r.name}
            for r in sorted(guild.roles, key=lambda r: r.position, reverse=True)
            if not r.is_default()
        ]
        payload["channels"] = [
            {"id": str(c.id), "name": c.name}
            for c in guild.text_channels
        ]

    return payload


class DJRoleBody(BaseModel):
    role_id: Optional[str] = None


@router.post("/dj-role")
async def set_dj_role(guild_id: int, body: DJRoleBody, user=Depends(guild_member), bot=Depends(get_bot)):
    _require_admin(bot, guild_id, int(user["id"]))

    role_id = None
    if body.role_id:
        try:
            role_id = int(body.role_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid role")
        guild = bot.get_guild(guild_id)
        if not guild or not guild.get_role(role_id):
            raise HTTPException(status_code=404, detail="Role not found")

    bot.get_guild_data(guild_id)["dj_role_id"] = role_id
    await bot.save_guild_dj_role(guild_id, role_id)
    return {"ok": True, "dj_role_id": str(role_id) if role_id else None}


class MusicChannelBody(BaseModel):
    channel_id: Optional[str] = None


@router.post("/music-channel")
async def set_music_channel(guild_id: int, body: MusicChannelBody, user=Depends(guild_member), bot=Depends(get_bot)):
    _require_admin(bot, guild_id, int(user["id"]))

    channel_id = None
    if body.channel_id:
        try:
            channel_id = int(body.channel_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid channel")
        guild = bot.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild else None
        if not isinstance(channel, discord.TextChannel):
            raise HTTPException(status_code=404, detail="Text channel not found")

    bot.get_guild_data(guild_id)["music_channel_id"] = channel_id
    await bot.save_guild_music_channel(guild_id, channel_id)
    return {"ok": True, "music_channel_id": str(channel_id) if channel_id else None}
