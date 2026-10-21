import discord
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from activity.dependencies import Bot, Member, require_guild_member
from activity.permissions import dj_role_id

router = APIRouter(prefix="/api/guild/{guild_id}/settings", tags=["settings"])

# Channels a music channel may be: where the bot posts now-playing panels
_MESSAGEABLE = (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.Thread)


def _require_admin(bot, guild_id: int, user_id: int):
    member = require_guild_member(bot, guild_id, user_id)
    if not member.guild_permissions.administrator:
        raise HTTPException(status_code=403, detail="Administrator permission required")
    return member


def _can_post(guild, channel) -> bool:
    permissions = channel.permissions_for(guild.me)
    return permissions.view_channel and permissions.send_messages


@router.get("")
async def get_settings(guild_id: int, user: Member, bot: Bot):
    """Guild settings, plus the pickable roles/channels when the caller is an admin."""
    guild_data = bot.get_guild_data(guild_id)
    guild = bot.get_guild(guild_id)
    member = guild.get_member(int(user["id"])) if guild else None
    is_admin = bool(member and member.guild_permissions.administrator)

    # A deleted role counts as no DJ role, as it does for every permission check.
    role_id = dj_role_id(bot, guild) if guild else None
    music_channel_id = guild_data.get("music_channel_id")
    dj_role = guild.get_role(role_id) if guild and role_id else None
    music_channel = guild.get_channel_or_thread(music_channel_id) if guild and music_channel_id else None

    payload = {
        "is_admin": is_admin,
        "dj_role_id": str(role_id) if role_id else None,
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
            if _can_post(guild, c)
        ]

    return payload


class DJRoleBody(BaseModel):
    role_id: str | None = None


@router.post("/dj-role")
async def set_dj_role(guild_id: int, body: DJRoleBody, user: Member, bot: Bot):
    _require_admin(bot, guild_id, int(user["id"]))

    role_id = None
    if body.role_id:
        try:
            role_id = int(body.role_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail="Invalid role") from e
        guild = bot.get_guild(guild_id)
        if not guild or not guild.get_role(role_id):
            raise HTTPException(status_code=404, detail="Role not found")

    bot.get_guild_data(guild_id)["dj_role_id"] = role_id
    await bot.save_guild_dj_role(guild_id, role_id)
    return {"ok": True, "dj_role_id": str(role_id) if role_id else None}


class MusicChannelBody(BaseModel):
    channel_id: str | None = None


@router.post("/music-channel")
async def set_music_channel(guild_id: int, body: MusicChannelBody, user: Member, bot: Bot):
    _require_admin(bot, guild_id, int(user["id"]))

    channel_id = None
    if body.channel_id:
        try:
            channel_id = int(body.channel_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail="Invalid channel") from e
        guild = bot.get_guild(guild_id)
        channel = guild.get_channel_or_thread(channel_id) if guild else None
        if not isinstance(channel, _MESSAGEABLE):
            raise HTTPException(status_code=404, detail="Text channel not found")
        if not _can_post(guild, channel):
            raise HTTPException(status_code=400, detail=f"I can't send messages in #{channel.name}")

    bot.get_guild_data(guild_id)["music_channel_id"] = channel_id
    await bot.save_guild_music_channel(guild_id, channel_id)
    return {"ok": True, "music_channel_id": str(channel_id) if channel_id else None}
