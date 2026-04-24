import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from activity.dependencies import get_bot, get_current_user, get_ws_manager, require_dj, require_guild_member
from activity.helpers import broadcast_state
from models.song import Song
from utils.helpers import get_existing_urls

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/guild/{guild_id}/queue", tags=["queue"])


async def _auto_start_if_idle(bot, guild_id: int) -> bool:
    """Start playback if idle. Returns True if the frontend should call POST /play."""
    guild_data = bot.get_guild_data(guild_id)

    if guild_data.get("current"):
        return False

    vc = guild_data.get("voice_client")
    if vc and vc.is_connected():
        asyncio.create_task(bot._playback_service.play_next(guild_id))
        return True

    # Activity-only: let the frontend handle via POST /play
    return True


class AddBody(BaseModel):
    query: str


@router.post("/add")
async def add_to_queue(guild_id: int, body: AddBody, user=Depends(get_current_user), bot=Depends(get_bot), ws=Depends(get_ws_manager)):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)

    guild_data = bot.get_guild_data(guild_id)

    try:
        info = await bot._music_service.get_song_info_cached(body.query)
    except Exception as e:
        logger.error(f"Song search error: {e}")
        raise HTTPException(status_code=500, detail="Failed to search for song")

    if not info:
        raise HTTPException(status_code=404, detail="No results found")

    # Handle playlists (list from Spotify, or dict with "entries" from yt-dlp)
    entries = None
    if isinstance(info, list):
        entries = info
    elif isinstance(info, dict) and "entries" in info:
        entries = info["entries"]

    existing_urls = get_existing_urls(guild_data)

    if entries:
        added = 0
        skipped = 0
        for entry in entries:
            if entry and entry.get("title"):
                entry["requested_by"] = user.get("username", f"<@{uid}>")
                if not entry.get("webpage_url"):
                    entry["webpage_url"] = entry.get("url", "")
                if entry.get("webpage_url") in existing_urls:
                    skipped += 1
                    continue
                song = Song(entry)
                bot._playback_service.queue_service.add_song_to_queue(guild_id, song)
                existing_urls.add(entry.get("webpage_url"))
                added += 1

        if added == 0 and skipped > 0:
            return {"ok": True, "added": 0, "skipped": skipped, "duplicate": True, "playlist": True}

        should_start = await _auto_start_if_idle(bot, guild_id)

        await bot.save_guild_queue(guild_id)
        await broadcast_state(bot, ws, guild_id)
        return {"ok": True, "added": added, "skipped": skipped, "playlist": True, "auto_play": should_start}

    # Single song — check for duplicate
    info["requested_by"] = user.get("username", f"<@{uid}>")
    if not info.get("webpage_url"):
        info["webpage_url"] = info.get("url", "")

    webpage_url = info.get("webpage_url", "")
    if webpage_url in existing_urls:
        # Find position in queue for the message
        for i, qs in enumerate(guild_data.get("queue", [])):
            if qs.webpage_url == webpage_url:
                return {"ok": True, "added": 0, "duplicate": True, "title": info.get("title", ""), "position": i + 1}
        if guild_data.get("current") and guild_data["current"].webpage_url == webpage_url:
            return {"ok": True, "added": 0, "duplicate": True, "title": info.get("title", ""), "playing": True}
        return {"ok": True, "added": 0, "duplicate": True, "title": info.get("title", "")}

    song = Song(info)
    bot._playback_service.queue_service.add_song_to_queue(guild_id, song)

    should_start = await _auto_start_if_idle(bot, guild_id)

    await bot.save_guild_queue(guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True, "title": song.title, "added": 1, "auto_play": should_start}


@router.delete("/{position}")
async def remove_from_queue(guild_id: int, position: int, user=Depends(get_current_user), bot=Depends(get_bot), ws=Depends(get_ws_manager)):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)
    require_dj(bot, guild_id, uid)

    removed = bot._playback_service.queue_service.remove_song_from_queue(guild_id, position)
    if not removed:
        raise HTTPException(status_code=404, detail="Invalid queue position")

    await bot.save_guild_queue(guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True, "removed": removed.title}


class MoveBody(BaseModel):
    from_pos: int
    to_pos: int


@router.post("/move")
async def move_in_queue(guild_id: int, body: MoveBody, user=Depends(get_current_user), bot=Depends(get_bot), ws=Depends(get_ws_manager)):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)
    require_dj(bot, guild_id, uid)

    success = bot._playback_service.queue_service.move_song_in_queue(guild_id, body.from_pos, body.to_pos)
    if not success:
        raise HTTPException(status_code=400, detail="Invalid positions")

    await bot.save_guild_queue(guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True}


@router.post("/clear")
async def clear_queue(guild_id: int, user=Depends(get_current_user), bot=Depends(get_bot), ws=Depends(get_ws_manager)):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)
    require_dj(bot, guild_id, uid)

    bot._playback_service.queue_service.clear_queue(guild_id)
    await bot.save_guild_queue(guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True}
