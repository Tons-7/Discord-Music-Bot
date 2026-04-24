from utils.ban_system import is_banned


def check_dj_permission(bot, guild_id: int, user_id: int) -> bool:
    guild_data = bot.get_guild_data(guild_id)
    dj_role_id = guild_data.get("dj_role_id")

    if not dj_role_id:
        return True

    guild = bot.get_guild(guild_id)
    if not guild:
        return False

    member = guild.get_member(user_id)
    if not member:
        return False

    if member.guild_permissions.administrator:
        return True

    return any(role.id == dj_role_id for role in member.roles)


def check_banned(user_id: int) -> bool:
    return is_banned(user_id)
