import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from activity.dependencies import get_bot, get_current_user, require_guild_member
from activity.state_serializer import _ensure_thumbnail

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/guild/{guild_id}", tags=["search"])


@router.get("/search")
async def search_songs(
    guild_id: int,
    q: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=25),
    user=Depends(get_current_user),
    bot=Depends(get_bot),
):
    require_guild_member(bot, guild_id, int(user["id"]))

    music_service = bot._music_service

    try:
        # If input looks like a URL, resolve it directly (Spotify, YouTube, SoundCloud, etc.)
        is_url = q.startswith("http://") or q.startswith("https://")
        if is_url:
            results = await music_service.get_song_info(q)
        else:
            results = await music_service.search_youtube(q, limit=limit)
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail="Search failed")

    if not results:
        return {"results": []}

    # search_youtube returns a dict with 'entries' when limit > 1
    if isinstance(results, dict):
        entries = results.get("entries", [])
        if not entries and results.get("title"):
            # Single result returned as a flat dict
            entries = [results]
    elif isinstance(results, list):
        entries = results
    else:
        entries = [results] if results else []

    normalized = []
    for entry in entries:
        if not entry or not entry.get("title"):
            continue

        result = {
            "title": entry.get("title", "Unknown"),
            "duration": entry.get("duration", 0) or 0,
            "thumbnail": entry.get("thumbnail", ""),
            "uploader": entry.get("uploader", entry.get("channel", "Unknown")),
            "webpage_url": entry.get("webpage_url", entry.get("url", "")),
            "url": entry.get("url", ""),
        }
        normalized.append(_ensure_thumbnail(result))

    return {"results": normalized}
