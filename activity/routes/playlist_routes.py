import asyncio
import functools
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from activity.dependencies import get_bot, get_ws_manager, guild_member
from activity.helpers import fill_missing_thumbnails, member_avatar_url
from activity.state_serializer import serialize_guild_state
from config import MAX_PLAYLIST_SIZE, PLAYLIST_PERMISSIONS, PLAYLIST_PERMISSION_RANK
from models.song import Song

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/guild/{guild_id}/playlists", tags=["playlists"])


def _table(global_mode: bool) -> str:
    return "global_playlists" if global_mode else "playlists"


def _label(global_mode: bool) -> str:
    return "Global playlist" if global_mode else "Playlist"


# Every mutation reads the whole songs blob, edits it in Python and writes it
# back, so two concurrent writes to one playlist lose an update. One lock per
# playlist covers the read and the write together.
_playlist_locks: dict[tuple, asyncio.Lock] = {}


def _playlist_lock(table: str, guild_id: int, name: str) -> asyncio.Lock:
    key = (table, guild_id, name)
    lock = _playlist_locks.get(key)
    if lock is None:
        if len(_playlist_locks) > 500:
            for k, held in list(_playlist_locks.items()):
                if not held.locked():
                    del _playlist_locks[k]
        lock = _playlist_locks.setdefault(key, asyncio.Lock())
    return lock


def _serialized(fn):
    """Run a playlist-mutating endpoint under that playlist's lock.

    FastAPI always invokes endpoints with keyword arguments, so the key can be
    read straight off kwargs.
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        body = kwargs.get("body")
        global_mode = (
            getattr(body, "global_mode", False) if body is not None
            else bool(kwargs.get("global_mode", False))
        )
        user = kwargs.get("user") or {}
        # Keyed by playlist identity, not caller: a collaborator mutates the
        # owner's row, and global playlists are not scoped to a guild.
        lock = _playlist_lock(
            _table(global_mode),
            0 if global_mode else int(kwargs.get("guild_id", 0) or 0),
            str(kwargs.get("name", "")),
        )
        async with lock:
            return await fn(*args, **kwargs)

    return wrapper


PERMISSIONS = PLAYLIST_PERMISSIONS
_RANK = PLAYLIST_PERMISSION_RANK


async def _resolve_playlist(
    bot, user_id: int, name: str, guild_id: int, global_mode: bool,
    owner_id: Optional[int] = None,
) -> tuple[Optional[int], Optional[list], Optional[str]]:
    """Find a playlist the user owns or collaborates on.

    Names are only unique per owner, so a shared playlist must be addressed with
    ``owner_id``; without it the oldest match wins deterministically.
    Returns (id, songs, level), or (None, None, None) with no access.
    """
    if owner_id is None or owner_id == user_id:
        pid, songs = await _get_playlist(bot, user_id, name, guild_id, global_mode)
        if pid is not None:
            return pid, songs, "owner"
        if owner_id == user_id:
            return None, None, None

    table = _table(global_mode)
    params: list = [name]
    where = "p.name = ?"
    if not global_mode:
        where += " AND p.guild_id = ?"
        params.append(guild_id)
    if owner_id is not None:
        where += " AND p.user_id = ?"
        params.append(owner_id)
    params.append(user_id)

    rows = await bot.fetch_db_query(
        f"SELECT p.id, p.songs, pc.permission FROM {table} p "
        f"JOIN playlist_collaborators pc ON pc.playlist_id = p.id AND pc.is_global = {1 if global_mode else 0} "
        f"WHERE {where} AND pc.user_id = ? ORDER BY p.id",
        tuple(params),
    )
    if not rows:
        return None, None, None
    return rows[0][0], json.loads(rows[0][1]), (rows[0][2] or "edit")


def _require(level: Optional[str], needed: str, name: str, label: str):
    """404 when there is no access at all, 403 when the grant is too weak."""
    if level is None:
        raise HTTPException(status_code=404, detail=f"{label} '{name}' not found")
    if _RANK.get(level, 0) < _RANK[needed]:
        raise HTTPException(
            status_code=403,
            detail=f"You only have {level} access to '{name}'",
        )


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
    user=Depends(guild_member),
    bot=Depends(get_bot),
):
    uid = int(user["id"])
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
        songs = fill_missing_thumbnails(json.loads(songs_json))
        playlists.append({
            "name": name,
            "song_count": len(songs),
            # First song's artwork doubles as the playlist cover
            "thumbnail": songs[0].get("thumbnail", "") if songs else "",
            "permission": "owner",
            "owner": None,
            "owner_id": str(uid),
        })

    # Playlists shared with this user. Listed after their own, tagged with the
    # granted level so the UI can hide what they cannot do.
    if global_mode:
        shared = await bot.fetch_db_query(
            f"SELECT p.name, p.songs, p.user_id, pc.permission FROM {table} p "
            "JOIN playlist_collaborators pc ON pc.playlist_id = p.id AND pc.is_global = 1 "
            "WHERE pc.user_id = ? ORDER BY p.created_at DESC",
            (uid,),
        )
    else:
        shared = await bot.fetch_db_query(
            f"SELECT p.name, p.songs, p.user_id, pc.permission FROM {table} p "
            "JOIN playlist_collaborators pc ON pc.playlist_id = p.id AND pc.is_global = 0 "
            "WHERE pc.user_id = ? AND p.guild_id = ? ORDER BY p.created_at DESC",
            (uid, guild_id),
        )

    guild = bot.get_guild(guild_id)
    for name, songs_json, owner_id, permission in shared:
        songs = fill_missing_thumbnails(json.loads(songs_json))
        owner = guild.get_member(owner_id) if guild else None
        playlists.append({
            "name": name,
            "song_count": len(songs),
            "thumbnail": songs[0].get("thumbnail", "") if songs else "",
            "permission": permission or "edit",
            "owner": owner.display_name if owner else f"User {owner_id}",
            "owner_id": str(owner_id),
        })

    return {"playlists": playlists}


# ── Guild members (for collab user picker) ────────────────────────────
# Must be before /{name} catch-all route

@router.get("/members")
async def search_members(
    guild_id: int,
    q: str = Query("", min_length=0),
    user=Depends(guild_member),
    bot=Depends(get_bot),
):
    uid = int(user["id"])

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
    owner_id: Optional[int] = Query(None),
    user=Depends(guild_member),
    bot=Depends(get_bot),
):
    uid = int(user["id"])

    pid, songs, level = await _resolve_playlist(bot, uid, name, guild_id, global_mode, owner_id)
    _require(level, "view", name, _label(global_mode))

    return {"name": name, "songs": fill_missing_thumbnails(songs)}


# ── Create playlist ──────────────────────────────────────────────────

class CreateBody(BaseModel):
    name: str
    global_mode: bool = False


@router.post("")
async def create_playlist(
    guild_id: int,
    body: CreateBody,
    user=Depends(guild_member),
    bot=Depends(get_bot),
):
    uid = int(user["id"])
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
    user=Depends(guild_member),
    bot=Depends(get_bot),
):
    uid = int(user["id"])
    table = _table(global_mode)
    label = _label(global_mode)

    pid = await bot.get_playlist_id(uid, name, None if global_mode else guild_id)
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
    owner_id: Optional[int] = None


@router.post("/{name}/load")
async def load_playlist(
    guild_id: int,
    name: str,
    body: LoadBody,
    user=Depends(guild_member),
    bot=Depends(get_bot),
    ws=Depends(get_ws_manager),
):
    uid = int(user["id"])
    label = _label(body.global_mode)

    pid, songs, level = await _resolve_playlist(bot, uid, name, guild_id, body.global_mode, body.owner_id)
    _require(level, "view", name, label)

    if not songs:
        raise HTTPException(status_code=400, detail=f"{label} '{name}' is empty")

    from utils.helpers import get_existing_urls

    guild_data = bot.get_guild_data(guild_id)
    existing = get_existing_urls(guild_data)
    added = 0

    fill_missing_thumbnails(songs)
    for s in songs:
        if s.get("webpage_url") not in existing:
            s["requested_by"] = f"<@{uid}>"
            bot._playback_service.queue_service.add_song_to_queue(guild_id, Song(s))
            existing.add(s.get("webpage_url"))
            added += 1

    should_start = False
    if not guild_data.get("current") and added > 0:
        from activity.routes.queue_routes import _auto_start_if_idle
        should_start = await _auto_start_if_idle(bot, ws, guild_id)

    await bot.save_guild_queue(guild_id)

    # Broadcast so the Activity UI updates (NowPlaying + queue)
    if ws.has_connections(guild_id):
        import asyncio
        await asyncio.sleep(0.1)
        data = serialize_guild_state(bot, guild_id)
        await ws.broadcast(guild_id, "STATE_UPDATE", data)

    return {"ok": True, "added": added, "total": len(songs), "auto_play": should_start}


# ── Add a song to a playlist ─────────────────────────────────────────

class AddSongBody(BaseModel):
    song_url: str
    global_mode: bool = False
    owner_id: Optional[int] = None
    # Metadata for songs that aren't in the session (search results, favorites,
    # playlist rows). Falls back to a lookup when absent.
    song: dict | None = None


@router.post("/{name}/add")
@_serialized
async def add_to_playlist(
    guild_id: int,
    name: str,
    body: AddSongBody,
    user=Depends(guild_member),
    bot=Depends(get_bot),
):
    uid = int(user["id"])
    table = _table(body.global_mode)
    label = _label(body.global_mode)

    pid, songs, level = await _resolve_playlist(bot, uid, name, guild_id, body.global_mode, body.owner_id)
    _require(level, "append", name, label)

    if len(songs) >= MAX_PLAYLIST_SIZE:
        raise HTTPException(status_code=400, detail=f"{label} is full ({MAX_PLAYLIST_SIZE} songs max)")

    if any(s.get("webpage_url") == body.song_url for s in songs):
        raise HTTPException(status_code=409, detail="Song already in playlist")

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

    if not song_dict and body.song:
        meta = {**body.song, "webpage_url": body.song_url}
        if meta.get("title"):
            song_dict = Song(meta).to_dict()

    if not song_dict:
        # Nothing local to go on (e.g. a pasted URL) — resolve it properly.
        try:
            info = await bot._music_service.get_song_info_cached(body.song_url)
        except Exception:
            info = None
        if isinstance(info, list):
            info = info[0] if info else None
        if isinstance(info, dict) and info.get("title"):
            info.setdefault("webpage_url", body.song_url)
            song_dict = Song(info).to_dict()

    if not song_dict:
        raise HTTPException(status_code=404, detail="Could not resolve that song")

    songs.append(song_dict)
    await bot.execute_db_query(
        f"UPDATE {table} SET songs = ? WHERE id = ?",
        (json.dumps(songs), pid),
    )

    return {"ok": True, "song_count": len(songs)}


# ── Remove song from playlist ────────────────────────────────────────

@router.delete("/{name}/{position}")
@_serialized
async def remove_from_playlist(
    guild_id: int,
    name: str,
    position: int,
    global_mode: bool = Query(False),
    owner_id: Optional[int] = Query(None),
    user=Depends(guild_member),
    bot=Depends(get_bot),
):
    uid = int(user["id"])
    table = _table(global_mode)
    label = _label(global_mode)

    pid, songs, level = await _resolve_playlist(bot, uid, name, guild_id, global_mode, owner_id)
    _require(level, "edit", name, label)

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
    owner_id: Optional[int] = None


@router.post("/{name}/move")
@_serialized
async def move_in_playlist(
    guild_id: int,
    name: str,
    body: MoveBody,
    user=Depends(guild_member),
    bot=Depends(get_bot),
):
    uid = int(user["id"])
    table = _table(body.global_mode)
    label = _label(body.global_mode)

    pid, songs, level = await _resolve_playlist(bot, uid, name, guild_id, body.global_mode, body.owner_id)
    _require(level, "edit", name, label)

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
    owner_id: Optional[int] = None


@router.post("/{name}/add-queue")
@_serialized
async def add_all_queue_to_playlist(
    guild_id: int,
    name: str,
    body: AddAllQueueBody,
    user=Depends(guild_member),
    bot=Depends(get_bot),
):
    uid = int(user["id"])
    table = _table(body.global_mode)
    label = _label(body.global_mode)

    pid, songs, level = await _resolve_playlist(bot, uid, name, guild_id, body.global_mode, body.owner_id)
    _require(level, "append", name, label)

    # Mirrors _handle_add_all_queue in cogs/playlist_commands.py: current song
    # first, then the queue, deduped within the session and against the playlist.
    guild_data = bot.get_guild_data(guild_id)
    session: list[dict] = []
    seen = set()

    current = guild_data.get("current")
    if current:
        session.append(current.to_dict())
        seen.add(current.webpage_url)

    for qs in guild_data.get("queue", []):
        if qs.webpage_url not in seen:
            session.append(qs.to_dict())
            seen.add(qs.webpage_url)

    existing_urls = {s.get("webpage_url") for s in songs}
    to_add = [s for s in session if s.get("webpage_url") not in existing_urls]
    skipped = len(session) - len(to_add)

    if not to_add:
        raise HTTPException(status_code=400, detail="No new songs to add (all duplicates or queue empty)")

    dropped = 0
    truncated = False
    if len(songs) + len(to_add) > MAX_PLAYLIST_SIZE:
        room = max(0, MAX_PLAYLIST_SIZE - len(songs))
        dropped = len(to_add) - room
        to_add = to_add[:room]
        truncated = True
        if not to_add:
            raise HTTPException(status_code=400, detail=f"{label} is full ({MAX_PLAYLIST_SIZE} songs max)")

    for song_dict in to_add:
        song_dict["requested_by"] = f"<@{uid}>"

    songs.extend(to_add)
    await bot.execute_db_query(
        f"UPDATE {table} SET songs = ? WHERE id = ?",
        (json.dumps(songs), pid),
    )

    return {
        "ok": True,
        "added": len(to_add),
        "skipped": skipped,
        "dropped": dropped,
        "truncated": truncated,
        "song_count": len(songs),
    }


# ── Copy one playlist's songs into another ───────────────────────────

class CopyBody(BaseModel):
    target: str
    global_mode: bool = False          # scope of the source playlist
    target_global_mode: bool = False   # scope of the destination
    owner_id: Optional[int] = None         # source owner, when it is a share
    target_owner_id: Optional[int] = None  # destination owner, when it is a share


# Not @_serialized: the source is only read, and taking both locks would
# deadlock two opposite copies (A->B and B->A). Only the target is locked.
@router.post("/{name}/copy")
async def copy_playlist(
    guild_id: int,
    name: str,
    body: CopyBody,
    user=Depends(guild_member),
    bot=Depends(get_bot),
):
    """Merge every song from `name` into `target`, skipping duplicates."""
    uid = int(user["id"])

    _, source_songs, source_level = await _resolve_playlist(bot, uid, name, guild_id, body.global_mode, body.owner_id)
    _require(source_level, "view", name, _label(body.global_mode))
    if not source_songs:
        raise HTTPException(status_code=400, detail=f"'{name}' is empty")

    if body.target == name and body.target_global_mode == body.global_mode:
        raise HTTPException(status_code=400, detail="Pick a different playlist to copy into")

    target_table = _table(body.target_global_mode)
    target_label = _label(body.target_global_mode)
    async with _playlist_lock(
        target_table, 0 if body.target_global_mode else guild_id, body.target
    ):
        pid, target_songs, target_level = await _resolve_playlist(
            bot, uid, body.target, guild_id, body.target_global_mode, body.target_owner_id
        )
        _require(target_level, "append", body.target, target_label)

        existing_urls = {song.get("webpage_url") for song in target_songs}
        to_add = []
        for song in source_songs:
            url = song.get("webpage_url")
            if url and url not in existing_urls:
                copied = dict(song)
                copied["requested_by"] = f"<@{uid}>"
                to_add.append(copied)
                existing_urls.add(url)

        skipped = len(source_songs) - len(to_add)
        if not to_add:
            return {"ok": True, "added": 0, "skipped": skipped, "dropped": 0,
                    "truncated": False, "song_count": len(target_songs)}

        dropped = 0
        truncated = False
        if len(target_songs) + len(to_add) > MAX_PLAYLIST_SIZE:
            room = max(0, MAX_PLAYLIST_SIZE - len(target_songs))
            dropped = len(to_add) - room
            to_add = to_add[:room]
            truncated = True
            if not to_add:
                raise HTTPException(
                    status_code=400,
                    detail=f"{target_label} is full ({MAX_PLAYLIST_SIZE} songs max)",
                )

        target_songs.extend(to_add)
        await bot.execute_db_query(
            f"UPDATE {target_table} SET songs = ? WHERE id = ?",
            (json.dumps(target_songs), pid),
        )

    return {
        "ok": True,
        "added": len(to_add),
        "skipped": skipped,
        "dropped": dropped,
        "truncated": truncated,
        "song_count": len(target_songs),
    }


# ── Collaborator management ───────────────────────────────────────────

@router.get("/{name}/collabs")
async def list_collaborators(
    guild_id: int,
    name: str,
    global_mode: bool = Query(False),
    user=Depends(guild_member),
    bot=Depends(get_bot),
):
    uid = int(user["id"])

    pid = await bot.get_playlist_id(uid, name, None if global_mode else guild_id)
    if pid is None:
        raise HTTPException(status_code=404, detail="Playlist not found")

    entries = await bot.get_collaborators(pid, global_mode)
    guild = bot.get_guild(guild_id)

    collabs = []
    for cid, permission in entries:
        member = guild.get_member(cid) if guild else None
        collabs.append({
            "id": str(cid),
            "display_name": member.display_name if member else f"User {cid}",
            "avatar": member_avatar_url(member),
            "permission": permission,
        })

    return {"collaborators": collabs, "levels": list(PERMISSIONS)}


class CollabBody(BaseModel):
    user_id: str
    global_mode: bool = False
    permission: str = "edit"


@router.post("/{name}/collabs")
async def add_collaborator(
    guild_id: int,
    name: str,
    body: CollabBody,
    user=Depends(guild_member),
    bot=Depends(get_bot),
):
    uid = int(user["id"])

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

    if body.permission not in PERMISSIONS:
        raise HTTPException(status_code=400, detail="Unknown permission level")

    await bot.add_collaborator(pid, target_id, body.global_mode, body.permission)
    return {"ok": True, "display_name": target.display_name, "permission": body.permission}


class CollabPermissionBody(BaseModel):
    permission: str
    global_mode: bool = False


@router.post("/{name}/collabs/{target_id}")
async def set_collaborator_permission(
    guild_id: int,
    name: str,
    target_id: int,
    body: CollabPermissionBody,
    user=Depends(guild_member),
    bot=Depends(get_bot),
):
    """Change what a collaborator may do. Owner only."""
    uid = int(user["id"])

    if body.permission not in PERMISSIONS:
        raise HTTPException(status_code=400, detail="Unknown permission level")

    pid = await bot.get_playlist_id(uid, name, None if body.global_mode else guild_id)
    if pid is None:
        raise HTTPException(status_code=404, detail="Playlist not found (you must own it)")

    if not await bot.is_collaborator(pid, target_id, body.global_mode):
        raise HTTPException(status_code=404, detail="User is not a collaborator")

    await bot.set_collaborator_permission(pid, target_id, body.permission, body.global_mode)
    return {"ok": True, "permission": body.permission}


@router.delete("/{name}/collabs/{target_id}")
async def remove_collaborator(
    guild_id: int,
    name: str,
    target_id: int,
    global_mode: bool = Query(False),
    user=Depends(guild_member),
    bot=Depends(get_bot),
):
    uid = int(user["id"])

    pid = await bot.get_playlist_id(uid, name, None if global_mode else guild_id)
    if pid is None:
        raise HTTPException(status_code=404, detail="Playlist not found (you must own it)")

    is_collab = await bot.is_collaborator(pid, target_id, global_mode)
    if not is_collab:
        raise HTTPException(status_code=404, detail="User is not a collaborator")

    await bot.remove_collaborator(pid, target_id, global_mode)
    return {"ok": True}
