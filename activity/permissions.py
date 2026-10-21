from utils.ban_system import is_banned


def dj_role_id(bot, guild) -> int | None:
    """The guild's DJ role id, or None when none is set or the set role no longer exists."""
    role_id = bot.get_guild_data(guild.id).get("dj_role_id")
    if not role_id or guild.get_role(role_id) is None:
        return None
    return role_id


def has_dj_permission(bot, guild, member) -> bool:
    """DJ role holders and administrators, or everyone when there is no DJ role."""
    role_id = dj_role_id(bot, guild)
    if role_id is None:
        return True
    if member is None:
        return False
    if member.guild_permissions.administrator:
        return True
    return any(role.id == role_id for role in member.roles)


def check_dj_permission(bot, guild_id: int, user_id: int) -> bool:
    guild = bot.get_guild(guild_id)
    if guild is None:
        return not bot.get_guild_data(guild_id).get("dj_role_id")
    return has_dj_permission(bot, guild, guild.get_member(user_id))


def voice_control_error(bot, guild_id: int, member) -> str | None:
    """Why the member may not control voice playback, or None when they may.

    While the bot is in a voice channel, only people in that channel control it (as with slash commands).
    Activity-only playback has no channel, so it is open to every member.
    """
    voice_client = bot.get_guild_data(guild_id).get("voice_client")
    if not voice_client or not voice_client.is_connected():
        return None
    channel = voice_client.channel
    user_channel = getattr(getattr(member, "voice", None), "channel", None)
    if user_channel is not None and user_channel.id == channel.id:
        return None
    return f"Join {channel.name} to control playback"


def check_banned(user_id: int) -> bool:
    return is_banned(user_id)
