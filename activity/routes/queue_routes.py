import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from activity import known_songs
from activity.dependencies import Bot, Controller, Listener, WsManager
from activity.helpers import activity_advance, broadcast_state, collection_entries, queue_index
from activity.rate_limit import check_rate
from config import MAX_QUEUE_SIZE
from models.song import Song
from services.music_service import SPOTIFY_TRACK_LIMIT, track_collection
from utils.helpers import get_existing_urls

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/guild/{guild_id}/queue", tags=["queue"])


async def _auto_start_if_idle(bot, ws_manager, guild_id: int) -> None:
    """Start playback if idle, server-side, so the state broadcast that follows already shows the song as
    current (leaving it to a follow-up POST /play made the queue briefly show the song about to play)."""
    guild_data = bot.get_guild_data(guild_id)

    if guild_data.get("current"):
        return

    vc = guild_data.get("voice_client")
    if vc and vc.is_connected():
        bot.spawn(bot.playback.play_next(guild_id))
        return

    await activity_advance(bot, ws_manager, guild_id)


def _known_song_info(guild_data: dict, url: str, user_id: int) -> dict | None:
    known = known_songs.lookup(url, owner=user_id)
    if known:
        return known

    heard = guild_data.get("played", []) + guild_data.get("history", [])
    song = next((s for s in heard if s.webpage_url == url), None)
    if song and song.duration and not song.is_live:
        return {**song.to_dict(), "url": url}
    return None


class AddBody(BaseModel):
    query: str = Field(min_length=1, max_length=2048)
    # The query is a playlist or Mix link (search's list_url): queue every song in it
    as_list: bool = False


@router.post("/add")
async def add_to_queue(guild_id: int, body: AddBody, user: Listener, bot: Bot, ws: WsManager):
    uid = int(user["id"])
    check_rate("queue_add", uid)
    requester = f"<@{uid}>"
    guild_data = bot.get_guild_data(guild_id)
    music = bot.music

    room = MAX_QUEUE_SIZE - len(guild_data["queue"])
    if room <= 0:
        raise HTTPException(status_code=409, detail=f"The queue is full ({MAX_QUEUE_SIZE} songs)")

    collection = track_collection(body.query)
    if collection:
        check_rate("playlist_import", uid)
        tracks = await music.collection_tracks(body.query)
        if not tracks:
            raise HTTPException(status_code=404, detail="Couldn't read that playlist or album")
        bot.playback.import_songs(
            guild_id, music.iter_youtube_matches(tracks[:room]), requester,
            on_first=lambda: _auto_start_if_idle(bot, ws, guild_id),
        )
        reason = "queue" if len(tracks) > room else (
            "spotify" if collection[0] == "spotify" and len(tracks) >= SPOTIFY_TRACK_LIMIT else None
        )
        return {
            "ok": True, "added": 0, "playlist": True, "in_background": True,
            "total": min(len(tracks), room), "capped": reason is not None, "capped_reason": reason,
        }

    info = None if body.as_list else _known_song_info(guild_data, body.query, uid)
    if body.as_list:
        check_rate("playlist_import", uid)
        info = {"entries": await music.handle_youtube_playlist(body.query, limit=room)}
    elif info is None:
        try:
            info = await music.get_song_info_cached(body.query)
        except Exception as e:
            logger.error(f"Song search error: {e}")
            raise HTTPException(status_code=500, detail="Failed to search for song") from e

    if not info:
        raise HTTPException(status_code=404, detail="No results found")

    entries = collection_entries(info)
    existing_urls = get_existing_urls(guild_data)

    if entries is not None:
        added = skipped = 0
        reason = "list" if isinstance(info, dict) and info.get("capped") else None
        for entry in entries:
            if not entry or not entry.get("title"):
                continue
            url = entry.get("webpage_url") or entry.get("url", "")
            if url in existing_urls:
                skipped += 1
                continue
            if added >= room:
                reason = "queue"
                break
            bot.playback.queue_service.add_song_to_queue(
                guild_id, Song({**entry, "webpage_url": url, "requested_by": requester}),
            )
            existing_urls.add(url)
            added += 1

        if added == 0 and skipped > 0:
            return {"ok": True, "added": 0, "skipped": skipped, "duplicate": True, "playlist": True}

        await _auto_start_if_idle(bot, ws, guild_id)
        await bot.save_guild_queue(guild_id)
        await broadcast_state(bot, ws, guild_id)
        return {
            "ok": True, "added": added, "skipped": skipped, "playlist": True,
            "capped": reason is not None, "capped_reason": reason,
        }

    # Single song — check for duplicate
    info = {**info, "requested_by": requester}
    if not info.get("webpage_url"):
        info["webpage_url"] = info.get("url", "")

    webpage_url = info.get("webpage_url", "")
    if webpage_url in existing_urls:
        title = info.get("title", "")
        for i, qs in enumerate(guild_data.get("queue", [])):
            if qs.webpage_url == webpage_url:
                return {"ok": True, "added": 0, "duplicate": True, "title": title, "position": i + 1}
        if guild_data.get("current") and guild_data["current"].webpage_url == webpage_url:
            return {"ok": True, "added": 0, "duplicate": True, "title": title, "playing": True}
        return {"ok": True, "added": 0, "duplicate": True, "title": title}

    song = Song(info)
    bot.playback.queue_service.add_song_to_queue(guild_id, song)

    await _auto_start_if_idle(bot, ws, guild_id)
    await bot.save_guild_queue(guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True, "title": song.title, "added": 1}


def _resolve(songs: list[Song], position: int, url: str | None) -> int:
    try:
        return queue_index(songs, position, url)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e) if url else "Invalid queue position") from e


@router.delete("/{position}")
async def remove_from_queue(
        guild_id: int, position: int, user: Controller, bot: Bot, ws: WsManager, url: str | None = None,
        played: bool = False,
):
    """Remove an upcoming song, or with ``played`` one the queue already played."""
    guild_data = bot.get_guild_data(guild_id)
    queue_service = bot.playback.queue_service
    if played:
        index = _resolve(queue_service.played_before(guild_data), position, url)
        removed = queue_service.remove_played(guild_id, index)
    else:
        index = _resolve(guild_data["queue"], position, url)
        removed = queue_service.remove_song_from_queue(guild_id, index)
    if not removed:
        raise HTTPException(status_code=404, detail="Invalid queue position")

    await bot.save_guild_queue(guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True, "removed": removed.title, "position": index}


class MoveBody(BaseModel):
    from_pos: int
    to_pos: int
    # The moved song; a stale from_pos is matched by URL instead
    webpage_url: str | None = None


@router.post("/move")
async def move_in_queue(guild_id: int, body: MoveBody, user: Controller, bot: Bot, ws: WsManager):
    guild_data = bot.get_guild_data(guild_id)
    from_pos = _resolve(guild_data["queue"], body.from_pos, body.webpage_url)
    if not bot.playback.queue_service.move_song_in_queue(guild_id, from_pos, body.to_pos):
        raise HTTPException(status_code=400, detail="Invalid positions")

    await bot.save_guild_queue(guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True}


@router.post("/clear")
async def clear_queue(guild_id: int, user: Controller, bot: Bot, ws: WsManager):
    bot.playback.queue_service.clear_queue(guild_id)
    await bot.save_guild_queue(guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True}
