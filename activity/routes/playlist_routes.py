import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from activity.dependencies import get_bot, get_current_user, get_ws_manager, require_guild_member
from activity.helpers import member_avatar_url
from activity.state_serializer import serialize_guild_state
from config import MAX_PLAYLIST_SIZE

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/guild/{guild_id}/playlists", tags=["playlists"])


def _table(global_mode: bool) -> str:
    return "global_playlists" if global_mode else "playlists"


def _label(global_mode: bool) -> str:
    return "Global playlist" if global_mode else "Playlist"


async def _get_playlist(bot, user_id: int, name: str, guild_id: int, global_mode: bool) -> tuple[Optional[int], Optional[list]]:
    """Get playlist id and songs. Returns (id, songs_list) or (None, None)."""
    table = _table(global_mode)
    if global_mode:
        rows = await bot.fetch_db_query(
            f"SELECT id, songs FROM {table} WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
    else:
        rows = await bot.fetch_db_query(
            f"SELECT id, songs FROM {table} WHERE user_id = ? AND guild_id = ? AND name = ?",
            (user_id, guild_id, name),
        )
    if not rows:
        return None, None
    return rows[0][0], json.loads(rows[0][1])


# ── List playlists ────────────────────────────────────────────────────

@router.get("")
async def list_playlists(
    guild_id: int,
    global_mode: bool = Query(False),
    user=Depends(get_current_user),
    bot=Depends(get_bot),
):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)
    table = _table(global_mode)

    if global_mode:
        rows = await bot.fetch_db_query(
            f"SELECT name, songs FROM {table} WHERE user_id = ? ORDER BY created_at DESC",
            (uid,),
        )
    else:
        rows = await bot.fetch_db_query(
            f"SELECT name, songs FROM {table} WHERE user_id = ? AND guild_id = ? ORDER BY created_at DESC",
            (uid, guild_id),
        )

    playlists = []
    for name, songs_json in rows:
        songs = json.loads(songs_json)
        playlists.append({
            "name": name,
            "song_count": len(songs),
        })

    return {"playlists": playlists}


# ── Guild members (for collab user picker) ────────────────────────────
# Must be before /{name} catch-all route

@router.get("/members")
async def search_members(
    guild_id: int,
    q: str = Query("", min_length=0),
    user=Depends(get_current_user),
    bot=Depends(get_bot),
):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)

    guild = bot.get_guild(guild_id)
    if not guild:
        return {"members": []}

    query = q.lower().strip()
    results = []
    for member in guild.members:
        if member.bot or member.id == uid:
            continue
        name = member.display_name.lower()
        username = member.name.lower()
        if not query or query in name or query in username:
            results.append({
                "id": str(member.id),
                "display_name": member.display_name,
                "username": member.name,
                "avatar": member_avatar_url(member),
            })
            if len(results) >= 20:
                break

    return {"members": results}


# ── Show playlist songs ──────────────────────────────────────────────

@router.get("/{name}")
async def show_playlist(
    guild_id: int,
    name: str,
    global_mode: bool = Query(False),
    user=Depends(get_current_user),
    bot=Depends(get_bot),
):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)

    pid, songs = await _get_playlist(bot, uid, name, guild_id, global_mode)
    if pid is None:
        raise HTTPException(status_code=404, detail=f"{_label(global_mode)} '{name}' not found")

    return {"name": name, "songs": songs}


# ── Create playlist ──────────────────────────────────────────────────

class CreateBody(BaseModel):
    name: str
    global_mode: bool = False


@router.post("")
async def create_playlist(
    guild_id: int,
    body: CreateBody,
    user=Depends(get_current_user),
    bot=Depends(get_bot),
):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)
    table = _table(body.global_mode)
    label = _label(body.global_mode)

    # Check existing
    pid, _ = await _get_playlist(bot, uid, body.name, guild_id, body.global_mode)
    if pid is not None:
        raise HTTPException(status_code=409, detail=f"{label} '{body.name}' already exists")

    if body.global_mode:
        await bot.execute_db_query(
            f"INSERT INTO {table} (user_id, name, songs) VALUES (?, ?, ?)",
            (uid, body.name, "[]"),
        )
    else:
        await bot.execute_db_query(
            f"INSERT INTO {table} (user_id, guild_id, name, songs) VALUES (?, ?, ?, ?)",
            (uid, guild_id, body.name, "[]"),
        )

    return {"ok": True, "name": body.name}


# ── Delete playlist ──────────────────────────────────────────────────


@router.delete("/{name}")
async def delete_playlist(
    guild_id: int,
    name: str,
    global_mode: bool = Query(False),
    user=Depends(get_current_user),
    bot=Depends(get_bot),
):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)
    table = _table(global_mode)
    label = _label(global_mode)

    pid, _ = await _get_playlist(bot, uid, name, guild_id, global_mode)
    if pid is None:
        raise HTTPException(status_code=404, detail=f"{label} '{name}' not found")

    await bot.execute_db_query(f"DELETE FROM {table} WHERE id = ?", (pid,))
    await bot.execute_db_query(
        "DELETE FROM playlist_collaborators WHERE playlist_id = ? AND is_global = ?",
        (pid, 1 if global_mode else 0),
    )

    return {"ok": True}


# ── Load playlist into queue ─────────────────────────────────────────

class LoadBody(BaseModel):
    global_mode: bool = False


@router.post("/{name}/load")
async def load_playlist(
    guild_id: int,
    name: str,
    body: LoadBody,
    user=Depends(get_current_user),
    bot=Depends(get_bot),
    ws=Depends(get_ws_manager),
):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)
    label = _label(body.global_mode)

    pid, songs = await _get_playlist(bot, uid, name, guild_id, body.global_mode)
    if pid is None:
        raise HTTPException(status_code=404, detail=f"{label} '{name}' not found")

    if not songs:
        raise HTTPException(status_code=400, detail=f"{label} '{name}' is empty")

    from models.song import Song
    from utils.helpers import get_existing_urls

    guild_data = bot.get_guild_data(guild_id)
    existing = get_existing_urls(guild_data)
    added = 0

    for s in songs:
        if s.get("webpage_url") not in existing:
            s["requested_by"] = f"<@{uid}>"
            bot._playback_service.queue_service.add_song_to_queue(guild_id, Song(s))
            existing.add(s.get("webpage_url"))
            added += 1

    should_start = False
    if not guild_data.get("current") and added > 0:
        from activity.routes.queue_routes import _auto_start_if_idle
        should_start = await _auto_start_if_idle(bot, guild_id)

    await bot.save_guild_queue(guild_id)

    # Broadcast so the Activity UI updates (NowPlaying + queue)
    if ws.has_connections(guild_id):
        import asyncio
        await asyncio.sleep(0.1)
        data = serialize_guild_state(bot, guild_id)
        await ws.broadcast(guild_id, "STATE_UPDATE", data)

    return {"ok": True, "added": added, "total": len(songs), "auto_play": should_start}


# ── Add current song to playlist ─────────────────────────────────────

class AddSongBody(BaseModel):
    song_url: str
    global_mode: bool = False


@router.post("/{name}/add")
async def add_to_playlist(
    guild_id: int,
    name: str,
    body: AddSongBody,
    user=Depends(get_current_user),
    bot=Depends(get_bot),
):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)
    table = _table(body.global_mode)
    label = _label(body.global_mode)

    pid, songs = await _get_playlist(bot, uid, name, guild_id, body.global_mode)
    if pid is None:
        raise HTTPException(status_code=404, detail=f"{label} '{name}' not found")

    if len(songs) >= MAX_PLAYLIST_SIZE:
        raise HTTPException(status_code=400, detail=f"{label} is full ({MAX_PLAYLIST_SIZE} songs max)")

    # Find the song from current/queue/history
    guild_data = bot.get_guild_data(guild_id)
    song_dict = None

    current = guild_data.get("current")
    if current and current.webpage_url == body.song_url:
        song_dict = current.to_dict()

    if not song_dict:
        for s in guild_data.get("queue", []):
            if s.webpage_url == body.song_url:
                song_dict = s.to_dict()
                break

    if not song_dict:
        for s in guild_data.get("history", []):
            if s.webpage_url == body.song_url:
                song_dict = s.to_dict()
                break

    if not song_dict:
        raise HTTPException(status_code=404, detail="Song not found in current session")

    # Check duplicate
    if any(s.get("webpage_url") == body.song_url for s in songs):
        raise HTTPException(status_code=409, detail="Song already in playlist")

    songs.append(song_dict)
    await bot.execute_db_query(
        f"UPDATE {table} SET songs = ? WHERE id = ?",
        (json.dumps(songs), pid),
    )

    return {"ok": True, "song_count": len(songs)}


# ── Remove song from playlist ────────────────────────────────────────

@router.delete("/{name}/{position}")
async def remove_from_playlist(
    guild_id: int,
    name: str,
    position: int,
    global_mode: bool = Query(False),
    user=Depends(get_current_user),
    bot=Depends(get_bot),
):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)
    table = _table(global_mode)
    label = _label(global_mode)

    pid, songs = await _get_playlist(bot, uid, name, guild_id, global_mode)
    if pid is None:
        raise HTTPException(status_code=404, detail=f"{label} '{name}' not found")

    if position < 0 or position >= len(songs):
        raise HTTPException(status_code=400, detail="Invalid position")

    removed = songs.pop(position)
    await bot.execute_db_query(
        f"UPDATE {table} SET songs = ? WHERE id = ?",
        (json.dumps(songs), pid),
    )

    return {"ok": True, "removed": removed.get("title", ""), "song_count": len(songs)}


# ── Move song within playlist ────────────────────────────────────────

class MoveBody(BaseModel):
    from_pos: int
    to_pos: int
    global_mode: bool = False


@router.post("/{name}/move")
async def move_in_playlist(
    guild_id: int,
    name: str,
    body: MoveBody,
    user=Depends(get_current_user),
    bot=Depends(get_bot),
):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)
    table = _table(body.global_mode)
    label = _label(body.global_mode)

    pid, songs = await _get_playlist(bot, uid, name, guild_id, body.global_mode)
    if pid is None:
        raise HTTPException(status_code=404, detail=f"{label} '{name}' not found")

    if body.from_pos < 0 or body.from_pos >= len(songs) or body.to_pos < 0 or body.to_pos >= len(songs):
        raise HTTPException(status_code=400, detail="Invalid positions")

    song = songs.pop(body.from_pos)
    songs.insert(body.to_pos, song)
    await bot.execute_db_query(
        f"UPDATE {table} SET songs = ? WHERE id = ?",
        (json.dumps(songs), pid),
    )

    return {"ok": True}


# ── Add all queue songs to playlist ──────────────────────────────────

class AddAllQueueBody(BaseModel):
    global_mode: bool = False


@router.post("/{name}/add-queue")
async def add_all_queue_to_playlist(
    guild_id: int,
    name: str,
    body: AddAllQueueBody,
    user=Depends(get_current_user),
    bot=Depends(get_bot),
):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)
    table = _table(body.global_mode)
    label = _label(body.global_mode)

    pid, songs = await _get_playlist(bot, uid, name, guild_id, body.global_mode)
    if pid is None:
        raise HTTPException(status_code=404, detail=f"{label} '{name}' not found")

    guild_data = bot.get_guild_data(guild_id)
    queue = guild_data.get("queue", [])
    current = guild_data.get("current")

    existing_urls = {s.get("webpage_url") for s in songs}
    added = 0

    # Add current song first if playing
    if current and current.webpage_url not in existing_urls and len(songs) < MAX_PLAYLIST_SIZE:
        songs.append(current.to_dict())
        existing_urls.add(current.webpage_url)
        added += 1

    for s in queue:
        if len(songs) >= MAX_PLAYLIST_SIZE:
            break
        if s.webpage_url not in existing_urls:
            songs.append(s.to_dict())
            existing_urls.add(s.webpage_url)
            added += 1

    if added == 0:
        raise HTTPException(status_code=400, detail="No new songs to add (all duplicates or queue empty)")

    await bot.execute_db_query(
        f"UPDATE {table} SET songs = ? WHERE id = ?",
        (json.dumps(songs), pid),
    )

    return {"ok": True, "added": added, "song_count": len(songs)}


# ── Collaborator management ───────────────────────────────────────────

@router.get("/{name}/collabs")
async def list_collaborators(
    guild_id: int,
    name: str,
    global_mode: bool = Query(False),
    user=Depends(get_current_user),
    bot=Depends(get_bot),
):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)

    pid, _ = await _get_playlist(bot, uid, name, guild_id, global_mode)
    if pid is None:
        raise HTTPException(status_code=404, detail="Playlist not found")

    collab_ids = await bot.get_collaborators(pid, global_mode)
    guild = bot.get_guild(guild_id)

    collabs = []
    for cid in collab_ids:
        member = guild.get_member(cid) if guild else None
        collabs.append({
            "id": str(cid),
            "display_name": member.display_name if member else f"User {cid}",
            "avatar": member_avatar_url(member),
        })

    return {"collaborators": collabs}


class CollabBody(BaseModel):
    user_id: str
    global_mode: bool = False


@router.post("/{name}/collabs")
async def add_collaborator(
    guild_id: int,
    name: str,
    body: CollabBody,
    user=Depends(get_current_user),
    bot=Depends(get_bot),
):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)

    target_id = int(body.user_id)

    if target_id == uid:
        raise HTTPException(status_code=400, detail="You can't add yourself")

    guild = bot.get_guild(guild_id)
    target = guild.get_member(target_id) if guild else None
    if not target:
        raise HTTPException(status_code=404, detail="User not found in server")
    if target.bot:
        raise HTTPException(status_code=400, detail="Can't add a bot")

    # Must own the playlist
    pid = await bot.get_playlist_id(uid, name, None if body.global_mode else guild_id)
    if pid is None:
        raise HTTPException(status_code=404, detail="Playlist not found (you must own it)")

    already = await bot.is_collaborator(pid, target_id, body.global_mode)
    if already:
        raise HTTPException(status_code=409, detail=f"{target.display_name} is already a collaborator")

    await bot.add_collaborator(pid, target_id, body.global_mode)
    return {"ok": True, "display_name": target.display_name}


@router.delete("/{name}/collabs/{target_id}")
async def remove_collaborator(
    guild_id: int,
    name: str,
    target_id: int,
    global_mode: bool = Query(False),
    user=Depends(get_current_user),
    bot=Depends(get_bot),
):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)

    pid = await bot.get_playlist_id(uid, name, None if global_mode else guild_id)
    if pid is None:
        raise HTTPException(status_code=404, detail="Playlist not found (you must own it)")

    is_collab = await bot.is_collaborator(pid, target_id, global_mode)
    if not is_collab:
        raise HTTPException(status_code=404, detail="User is not a collaborator")

    await bot.remove_collaborator(pid, target_id, global_mode)
    return {"ok": True}
