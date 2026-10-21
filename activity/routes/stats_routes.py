from fastapi import APIRouter

from activity.dependencies import Bot, Member
from activity.helpers import member_avatar_url

router = APIRouter(prefix="/api/guild/{guild_id}/stats", tags=["stats"])


@router.get("/me")
async def my_stats(guild_id: int, user: Member, bot: Bot):
    """Get the current user's listening stats for this guild."""
    return await bot.get_user_stats(int(user["id"]), guild_id)


@router.get("/leaderboard")
async def leaderboard(guild_id: int, user: Member, bot: Bot):
    """Get the server's top 10 listeners."""
    guild = bot.get_guild(guild_id)
    entries = []
    for user_id, play_count, total_seconds in await bot.get_leaderboard(guild_id):
        member = guild.get_member(user_id) if guild else None
        entries.append({
            "user_id": str(user_id),
            "display_name": member.display_name if member else f"User {user_id}",
            "avatar_url": member_avatar_url(member),
            "play_count": play_count,
            "total_seconds": total_seconds,
        })
    return {"entries": entries, "your_id": str(user["id"])}
