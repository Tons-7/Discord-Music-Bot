import logging

from fastapi import APIRouter, HTTPException, Query

from activity import known_songs
from activity.dependencies import Bot, Member
from activity.rate_limit import check_rate
from config import DEFAULT_SEARCH_RESULTS, MAX_SEARCH_RESULTS
from services.music_service import link_platform, track_collection, youtube_list_link
from utils.helpers import youtube_thumbnail

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/guild/{guild_id}", tags=["search"])


def _result(entry: dict) -> dict:
    webpage_url = entry.get("webpage_url") or entry.get("url", "")
    return {
        "title": entry.get("title", "Unknown"),
        "duration": entry.get("duration", 0) or 0,
        "album": entry.get("album", ""),
        "thumbnail": entry.get("thumbnail") or youtube_thumbnail(webpage_url),
        "uploader": entry.get("uploader", entry.get("channel", "Unknown")),
        "webpage_url": webpage_url,
    }


@router.get("/search")
async def search_songs(
        guild_id: int,
        user: Member,
        bot: Bot,
        q: str = Query(..., min_length=1, max_length=2048),
        limit: int = Query(DEFAULT_SEARCH_RESULTS, ge=1, le=MAX_SEARCH_RESULTS),
):
    check_rate("search", int(user["id"]))
    music_service = bot.music

    # Spotify and Apple Music lists are read without YouTube lookups, so they show at once;
    # each row is looked up when it is added.
    is_track_list = track_collection(q) is not None
    try:
        if is_track_list:
            results = await music_service.collection_tracks(q)
        elif link_platform(q):
            results = await music_service.get_song_info_cached(q)
        else:
            results = await music_service.search_youtube_many(q, limit)
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail="Search failed") from e

    if isinstance(results, dict):
        entries = results.get("entries")
        if entries is None:
            entries = [results] if results.get("title") else []
    else:
        entries = results or []

    normalized = [_result(entry) for entry in entries if entry and entry.get("title")]
    if not is_track_list:
        known_songs.remember(normalized)
    response: dict = {"results": normalized}
    # A video opened from a playlist or Mix resolves to that video; the list stays one tap away.
    if len(normalized) == 1 and (list_url := youtube_list_link(q)):
        response["list_url"] = list_url
    return response
