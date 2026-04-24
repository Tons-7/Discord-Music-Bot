import logging

from fastapi import APIRouter, Depends

from activity.dependencies import get_bot, get_current_user, require_guild_member
from activity.helpers import member_avatar_url

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/guild/{guild_id}/stats", tags=["stats"])


@router.get("/me")
async def my_stats(guild_id: int, user=Depends(get_current_user), bot=Depends(get_bot)):
    """Get the current user's listening stats for this guild."""
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)

    # Total plays & listening time
    totals = await bot.fetch_db_query(
        "SELECT COUNT(*), COALESCE(SUM(duration_seconds), 0) "
        "FROM user_stats WHERE user_id = ? AND guild_id = ?",
        (uid, guild_id),
    )
    play_count = totals[0][0] if totals else 0
    total_seconds = totals[0][1] if totals else 0

    # Top 5 songs
    top_songs = await bot.fetch_db_query(
        "SELECT song_title, COUNT(*) as cnt FROM user_stats "
        "WHERE user_id = ? AND guild_id = ? "
        "GROUP BY song_title ORDER BY cnt DESC LIMIT 5",
        (uid, guild_id),
    )

    # Top 5 artists
    top_artists = await bot.fetch_db_query(
        "SELECT artist, COUNT(*) as cnt FROM user_stats "
        "WHERE user_id = ? AND guild_id = ? AND artist != '' "
        "GROUP BY artist ORDER BY cnt DESC LIMIT 5",
        (uid, guild_id),
    )

    return {
        "play_count": play_count,
        "total_seconds": total_seconds,
        "top_songs": [{"title": r[0], "plays": r[1]} for r in top_songs],
        "top_artists": [{"name": r[0], "plays": r[1]} for r in top_artists],
    }


@router.get("/leaderboard")
async def leaderboard(guild_id: int, user=Depends(get_current_user), bot=Depends(get_bot)):
    """Get the server's top 10 listeners."""
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)

    rows = await bot.fetch_db_query(
        "SELECT user_id, COUNT(*) as play_count, COALESCE(SUM(duration_seconds), 0) as total_seconds "
        "FROM user_stats WHERE guild_id = ? "
        "GROUP BY user_id ORDER BY total_seconds DESC LIMIT 10",
        (guild_id,),
    )

    guild = bot.get_guild(guild_id)
    entries = []
    for row in rows:
        user_id, play_count, total_seconds = row
        # Resolve display name from guild member cache
        member = guild.get_member(user_id) if guild else None
        display_name = member.display_name if member else f"User {user_id}"
        avatar_url = member_avatar_url(member)
        entries.append({
            "user_id": str(user_id),
            "display_name": display_name,
            "avatar_url": avatar_url,
            "play_count": play_count,
            "total_seconds": total_seconds,
        })

    return {"entries": entries, "your_id": str(uid)}
