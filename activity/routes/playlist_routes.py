import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from activity import known_songs
from activity.dependencies import Bot, Listener, Member, WsManager
from activity.helpers import broadcast_state, collection_entries, fill_missing_thumbnails, member_avatar_url
from activity.rate_limit import check_rate
from config import MAX_PLAYLIST_SIZE, MAX_QUEUE_SIZE, PLAYLIST_PERMISSIONS
from models.song import Song
from services import playlist_service as playlists
from services.music_service import SPOTIFY_TRACK_LIMIT, link_platform, track_collection
from utils.helpers import get_existing_urls

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/guild/{guild_id}/playlists", tags=["playlists"])

PERMISSIONS = PLAYLIST_PERMISSIONS
Permission = Literal["view", "append", "edit"]


def _person(bot, guild, user_id: int):
    """A member of this server, else any user the bot knows (owners of global playlists elsewhere)."""
    return (guild.get_member(user_id) if guild else None) or bot.get_user(user_id)


async def _access(
        bot, uid: int, name: str, guild_id: int, global_mode: bool, owner_id: int | None, needed: str,
) -> playlists.Playlist:
    """The playlist, or 404 without any access and 403 when the grant is too weak."""
    playlist = await playlists.find(bot, uid, name, guild_id, global_mode, owner_id)
    if playlist is None:
        raise HTTPException(status_code=404, detail=f"{playlists.label(global_mode)} '{name}' not found")
    if not playlist.allows(needed):
        raise HTTPException(status_code=403, detail=f"You only have {playlist.level} access to '{name}'")
    return playlist


async def _owned(bot, uid: int, name: str, guild_id: int, global_mode: bool) -> playlists.Playlist:
    playlist = await playlists.find(bot, uid, name, guild_id, global_mode, owner_id=uid)
    if playlist is None:
        raise HTTPException(status_code=404, detail="Playlist not found (you must own it)")
    return playlist


def _full(playlist: playlists.Playlist) -> HTTPException:
    return HTTPException(
        status_code=400, detail=f"{playlists.label(playlist.global_mode)} is full ({MAX_PLAYLIST_SIZE} songs max)",
    )


# ── List playlists ────────────────────────────────────────────────────

def _summary(playlist: playlists.Playlist, owner_name: str | None) -> dict:
    songs = fill_missing_thumbnails(playlist.songs)
    return {
        "name": playlist.name,
        "song_count": len(songs),
        # First song's artwork doubles as the playlist cover
        "thumbnail": songs[0].get("thumbnail", "") if songs else "",
        "permission": playlist.level,
        "owner": owner_name,
        "owner_id": str(playlist.owner_id),
    }


@router.get("")
async def list_playlists(guild_id: int, user: Member, bot: Bot, global_mode: bool = Query(False)):
    uid = int(user["id"])
    guild = bot.get_guild(guild_id)
    result = [_summary(p, None) for p in await playlists.owned(bot, uid, guild_id, global_mode)]
    # Playlists shared with this user, listed after their own and tagged with the granted level.
    for playlist in await playlists.shared_with(bot, uid, guild_id, global_mode):
        owner = _person(bot, guild, playlist.owner_id)
        result.append(_summary(playlist, owner.display_name if owner else f"User {playlist.owner_id}"))
    return {"playlists": result}


# ── Guild members (for collab user picker) ────────────────────────────
# Must be before /{name} catch-all route

@router.get("/members")
async def search_members(guild_id: int, user: Member, bot: Bot, q: str = Query("", max_length=100)):
    uid = int(user["id"])

    guild = bot.get_guild(guild_id)
    if not guild:
        return {"members": []}

    query = q.lower().strip()
    results = []
    for member in guild.members:
        if member.bot or member.id == uid:
            continue
        if not query or query in member.display_name.lower() or query in member.name.lower():
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
        guild_id: int, name: str, user: Member, bot: Bot,
        global_mode: bool = Query(False), owner_id: int | None = Query(None),
):
    uid = int(user["id"])
    playlist = await _access(bot, uid, name, guild_id, global_mode, owner_id, "view")
    songs = fill_missing_thumbnails(playlist.songs)
    known_songs.remember(songs, owner=uid)
    return {"name": name, "songs": songs}


# ── Create / delete ──────────────────────────────────────────────────

class CreateBody(BaseModel):
    name: str = Field(max_length=200)
    global_mode: bool = False


@router.post("")
async def create_playlist(guild_id: int, body: CreateBody, user: Member, bot: Bot):
    try:
        name = await playlists.create(bot, int(user["id"]), body.name, guild_id, body.global_mode)
    except ValueError as e:
        status = 409 if "already exists" in str(e) else 400
        raise HTTPException(status_code=status, detail=str(e)) from e
    return {"ok": True, "name": name}


@router.delete("/{name}")
async def delete_playlist(guild_id: int, name: str, user: Member, bot: Bot, global_mode: bool = Query(False)):
    playlist = await playlists.find(bot, int(user["id"]), name, guild_id, global_mode, owner_id=int(user["id"]))
    if playlist is None:
        raise HTTPException(status_code=404, detail=f"{playlists.label(global_mode)} '{name}' not found")
    await playlists.delete(bot, playlist)
    return {"ok": True}


# ── Load playlist into queue ─────────────────────────────────────────

class LoadBody(BaseModel):
    global_mode: bool = False
    owner_id: int | None = None


@router.post("/{name}/load")
async def load_playlist(guild_id: int, name: str, body: LoadBody, user: Listener, bot: Bot, ws: WsManager):
    from activity.routes.queue_routes import _auto_start_if_idle

    uid = int(user["id"])
    playlist = await _access(bot, uid, name, guild_id, body.global_mode, body.owner_id, "view")
    if not playlist.songs:
        raise HTTPException(status_code=400, detail=f"{playlists.label(body.global_mode)} '{name}' is empty")

    guild_data = bot.get_guild_data(guild_id)
    existing = get_existing_urls(guild_data)
    room = MAX_QUEUE_SIZE - len(guild_data["queue"])
    added = 0
    capped = False  # a new song didn't fit
    for s in fill_missing_thumbnails(playlist.songs):
        if s.get("webpage_url") in existing:
            continue
        if added >= room:
            capped = True
            break
        bot.playback.queue_service.add_song_to_queue(guild_id, Song({**s, "requested_by": f"<@{uid}>"}))
        existing.add(s.get("webpage_url"))
        added += 1

    if added:
        await _auto_start_if_idle(bot, ws, guild_id)
    await bot.save_guild_queue(guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True, "added": added, "total": len(playlist.songs), "capped": capped}


# ── Add a song to a playlist ─────────────────────────────────────────

class AddSongBody(BaseModel):
    song_url: str = Field(max_length=2048)
    global_mode: bool = False
    owner_id: int | None = None
    # Sent by older clients; ignored, so a client can't decide what a URL's title is.
    song: dict | None = None


async def _song_for(bot, guild_data: dict, url: str, uid: int) -> dict | None:
    """Server-known metadata for a URL: the session, songs listed to this user, else a lookup."""
    current = guild_data.get("current")
    for song in [current, *guild_data.get("queue", []), *guild_data.get("history", [])]:
        if song is not None and song.webpage_url == url:
            return song.to_dict()

    # Spotify and Apple Music links can't be played directly, so they are always looked up below.
    platform = (link_platform(url) or ("", ""))[0]
    if platform not in ("spotify", "apple", "tidal") and (known := known_songs.lookup(url, owner=uid)):
        return Song(known).to_dict()

    try:
        info = await bot.music.get_song_info_cached(url)
    except Exception:
        info = None
    if isinstance(info, list):
        info = info[0] if info else None
    if isinstance(info, dict) and info.get("title") and "entries" not in info:
        return Song({**info, "webpage_url": info.get("webpage_url") or url}).to_dict()
    return None


@router.post("/{name}/add")
async def add_to_playlist(guild_id: int, name: str, body: AddSongBody, user: Member, bot: Bot):
    uid = int(user["id"])
    playlist = await _access(bot, uid, name, guild_id, body.global_mode, body.owner_id, "append")
    if not playlist.room:
        raise _full(playlist)
    if any(s.get("webpage_url") == body.song_url for s in playlist.songs):
        raise HTTPException(status_code=409, detail="Song already in playlist")

    # Resolved before taking the lock: a lookup can take seconds.
    song_dict = await _song_for(bot, bot.get_guild_data(guild_id), body.song_url, uid)
    if not song_dict:
        raise HTTPException(status_code=404, detail="Could not resolve that song")

    try:
        async with playlists.editing(bot, playlist):
            if not playlist.room:
                raise _full(playlist)
            if any(s.get("webpage_url") == song_dict["webpage_url"] for s in playlist.songs):
                raise HTTPException(status_code=409, detail="Song already in playlist")
            playlist.songs.append(song_dict)
    except playlists.PlaylistGone as e:
        raise HTTPException(status_code=404, detail=f"{playlists.label(body.global_mode)} '{name}' not found") from e
    return {"ok": True, "song_count": len(playlist.songs)}


# ── Remove / move ────────────────────────────────────────────────────

@router.delete("/{name}/{position}")
async def remove_from_playlist(
        guild_id: int, name: str, position: int, user: Member, bot: Bot,
        global_mode: bool = Query(False), owner_id: int | None = Query(None), url: str | None = Query(None),
):
    playlist = await _access(bot, int(user["id"]), name, guild_id, global_mode, owner_id, "edit")
    try:
        async with playlists.editing(bot, playlist):
            removed = playlist.songs.pop(playlists.song_index(playlist.songs, position, url))
    except LookupError as e:
        raise HTTPException(status_code=404 if url else 400, detail=str(e)) from e
    except playlists.PlaylistGone as e:
        raise HTTPException(status_code=404, detail=f"{playlists.label(global_mode)} '{name}' not found") from e
    return {"ok": True, "removed": removed.get("title", ""), "song_count": len(playlist.songs)}


class MoveBody(BaseModel):
    from_pos: int
    to_pos: int
    global_mode: bool = False
    owner_id: int | None = None
    # The moved song; a stale from_pos is matched by URL instead
    webpage_url: str | None = None


@router.post("/{name}/move")
async def move_in_playlist(guild_id: int, name: str, body: MoveBody, user: Member, bot: Bot):
    playlist = await _access(bot, int(user["id"]), name, guild_id, body.global_mode, body.owner_id, "edit")
    try:
        async with playlists.editing(bot, playlist):
            index = playlists.song_index(playlist.songs, body.from_pos, body.webpage_url)
            if not 0 <= body.to_pos < len(playlist.songs):
                raise LookupError("Invalid positions")
            playlist.songs.insert(body.to_pos, playlist.songs.pop(index))
    except LookupError as e:
        raise HTTPException(status_code=404 if body.webpage_url else 400, detail=str(e)) from e
    except playlists.PlaylistGone as e:
        raise HTTPException(status_code=404, detail=f"{playlists.label(body.global_mode)} '{name}' not found") from e
    return {"ok": True}


# ── Bulk adds ────────────────────────────────────────────────────────

async def _append(bot, playlist: playlists.Playlist, songs: list[dict], empty_detail: str) -> dict:
    try:
        async with playlists.editing(bot, playlist):
            if not playlist.room:
                raise _full(playlist)
            result = playlists.append_new(playlist, songs)
            if not result["added"]:
                raise HTTPException(status_code=400, detail=empty_detail if not result["skipped"] else
                                    f"Every song is already in {playlist.name}")
    except playlists.PlaylistGone as e:
        raise HTTPException(status_code=404, detail=f"'{playlist.name}' was deleted") from e
    return {"ok": True, **result}


class AddAllQueueBody(BaseModel):
    global_mode: bool = False
    owner_id: int | None = None


@router.post("/{name}/add-queue")
async def add_all_queue_to_playlist(guild_id: int, name: str, body: AddAllQueueBody, user: Member, bot: Bot):
    uid = int(user["id"])
    playlist = await _access(bot, uid, name, guild_id, body.global_mode, body.owner_id, "append")

    # The whole queue, as the cog's add-all-queue saves it: played songs, the current one, then the upcoming ones.
    guild_data = bot.get_guild_data(guild_id)
    session = [s.to_dict() for s in bot.playback.queue_service.whole_queue(guild_data)]
    for song_dict in session:
        song_dict["requested_by"] = f"<@{uid}>"
    return await _append(bot, playlist, session, "No new songs to add (all duplicates or queue empty)")


class AddUrlBody(BaseModel):
    url: str = Field(max_length=2048)
    global_mode: bool = False
    owner_id: int | None = None
    # The url is a playlist or Mix link (search's list_url) to expand
    as_list: bool = False


async def _link_entries(bot, url: str, as_list: bool = False) -> tuple[list[dict], bool]:
    """Songs of a playlist or album link, and whether the list was cut short."""
    music = bot.music
    if as_list:
        return await music.handle_youtube_playlist(url, limit=MAX_PLAYLIST_SIZE), False
    collection = track_collection(url)
    if collection:
        tracks = await music.collection_tracks(url)
        songs = [song async for song in music.iter_youtube_matches(tracks)]
        return songs, collection[0] == "spotify" and len(tracks) >= SPOTIFY_TRACK_LIMIT
    try:
        info = await music.get_song_info_cached(url)
    except Exception:
        info = None
    return collection_entries(info) or [], bool(isinstance(info, dict) and info.get("capped"))


@router.post("/{name}/add-url")
async def add_url_to_playlist(guild_id: int, name: str, body: AddUrlBody, user: Member, bot: Bot):
    uid = int(user["id"])
    check_rate("playlist_import", uid)
    playlist = await _access(bot, uid, name, guild_id, body.global_mode, body.owner_id, "append")
    if not playlist.room:
        raise _full(playlist)

    # Finding the songs can take a while, so only the final write holds the lock.
    entries, capped = await _link_entries(bot, body.url, body.as_list)
    if not entries:
        raise HTTPException(status_code=400, detail="That link is not a playlist or album")

    songs = []
    for entry in entries:
        url = entry.get("webpage_url") or entry.get("url")
        if entry.get("title") and url:
            songs.append(Song({**entry, "webpage_url": url, "requested_by": f"<@{uid}>"}).to_dict())
    result = await _append(bot, playlist, songs, "That link has no playable songs")
    return {**result, "capped": capped}


# ── Copy one playlist's songs into another ───────────────────────────

class CopyBody(BaseModel):
    target: str = Field(max_length=200)
    global_mode: bool = False          # scope of the source playlist
    target_global_mode: bool = False   # scope of the destination
    owner_id: int | None = None         # source owner, when it is a share
    target_owner_id: int | None = None  # destination owner, when it is a share


@router.post("/{name}/copy")
async def copy_playlist(guild_id: int, name: str, body: CopyBody, user: Member, bot: Bot):
    """Merge every song from `name` into `target`, skipping duplicates. Only the target is locked: the
    source is only read, and taking both locks would deadlock two opposite copies (A->B and B->A)."""
    uid = int(user["id"])
    source = await _access(bot, uid, name, guild_id, body.global_mode, body.owner_id, "view")
    if not source.songs:
        raise HTTPException(status_code=400, detail=f"'{name}' is empty")

    target = await _access(bot, uid, body.target, guild_id, body.target_global_mode, body.target_owner_id, "append")
    if target.global_mode == source.global_mode and target.id == source.id:
        raise HTTPException(status_code=400, detail="Pick a different playlist to copy into")

    copies = [{**song, "requested_by": f"<@{uid}>"} for song in source.songs if song.get("webpage_url")]
    try:
        async with playlists.editing(bot, target):
            if not target.room:
                raise _full(target)
            result = playlists.append_new(target, copies)
    except playlists.PlaylistGone as e:
        raise HTTPException(status_code=404, detail=f"'{body.target}' was deleted") from e
    return {"ok": True, **result}


# ── Collaborator management ───────────────────────────────────────────

@router.get("/{name}/collabs")
async def list_collaborators(guild_id: int, name: str, user: Member, bot: Bot, global_mode: bool = Query(False)):
    playlist = await _owned(bot, int(user["id"]), name, guild_id, global_mode)
    guild = bot.get_guild(guild_id)

    collabs = []
    for cid, permission in await bot.get_collaborators(playlist.id, global_mode):
        person = _person(bot, guild, cid)
        collabs.append({
            "id": str(cid),
            "display_name": person.display_name if person else f"User {cid}",
            "avatar": member_avatar_url(person),
            "permission": permission,
        })

    return {"collaborators": collabs, "levels": list(PERMISSIONS)}


class CollabBody(BaseModel):
    user_id: str = Field(pattern=r"^\d{1,20}$")
    global_mode: bool = False
    permission: Permission = "edit"


@router.post("/{name}/collabs")
async def add_collaborator(guild_id: int, name: str, body: CollabBody, user: Member, bot: Bot):
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

    playlist = await _owned(bot, uid, name, guild_id, body.global_mode)
    if await bot.is_collaborator(playlist.id, target_id, body.global_mode):
        raise HTTPException(status_code=409, detail=f"{target.display_name} is already a collaborator")

    await bot.add_collaborator(playlist.id, target_id, body.global_mode, body.permission)
    return {"ok": True, "display_name": target.display_name, "permission": body.permission}


class CollabPermissionBody(BaseModel):
    permission: Permission
    global_mode: bool = False


@router.post("/{name}/collabs/{target_id}")
async def set_collaborator_permission(
        guild_id: int, name: str, target_id: int, body: CollabPermissionBody, user: Member, bot: Bot,
):
    """Change what a collaborator may do. Owner only."""
    playlist = await _owned(bot, int(user["id"]), name, guild_id, body.global_mode)
    if not await bot.is_collaborator(playlist.id, target_id, body.global_mode):
        raise HTTPException(status_code=404, detail="User is not a collaborator")

    await bot.set_collaborator_permission(playlist.id, target_id, body.permission, body.global_mode)
    return {"ok": True, "permission": body.permission}


@router.delete("/{name}/collabs/{target_id}")
async def remove_collaborator(
        guild_id: int, name: str, target_id: int, user: Member, bot: Bot, global_mode: bool = Query(False),
):
    playlist = await _owned(bot, int(user["id"]), name, guild_id, global_mode)
    if not await bot.is_collaborator(playlist.id, target_id, global_mode):
        raise HTTPException(status_code=404, detail="User is not a collaborator")

    await bot.remove_collaborator(playlist.id, target_id, global_mode)
    return {"ok": True}
