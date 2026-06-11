import logging

from fastapi import APIRouter, Depends, HTTPException

from activity.dependencies import get_bot, guild_member
from utils.lyrics import fetch_lyrics

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/guild/{guild_id}", tags=["lyrics"])


@router.get("/lyrics")
async def get_lyrics(guild_id: int, user=Depends(guild_member), bot=Depends(get_bot)):
    guild_data = bot.get_guild_data(guild_id)
    current = guild_data.get("current")
    if not current:
        raise HTTPException(status_code=404, detail="Nothing is playing")

    result = await fetch_lyrics(current.title, current.uploader)
    if not result:
        raise HTTPException(status_code=404, detail="Lyrics not found")

    return {
        "lyrics": result.get("lyrics", ""),
        "synced": result.get("synced", ""),
        "title": result.get("title", current.title),
        "artist": result.get("artist", current.uploader),
        "webpage_url": current.webpage_url,
    }
