"""Playlists shared by the slash commands and the Activity: lookup, access levels, locking and writes."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass

from config import MAX_PLAYLIST_SIZE, PLAYLIST_PERMISSION_RANK
from utils.helpers import index_by_url

MAX_NAME_LENGTH = 50


class PlaylistGone(Exception):
    """The playlist was deleted while a change to it was being prepared."""


def table(global_mode: bool) -> str:
    return "global_playlists" if global_mode else "playlists"


def label(global_mode: bool) -> str:
    return "Global playlist" if global_mode else "Playlist"


def name_error(name: str) -> str | None:
    """Why a playlist name can't be used, or None. Names travel in Activity URLs, so "/" is not allowed."""
    name = (name or "").strip()
    if not name:
        return "Give the playlist a name"
    if len(name) > MAX_NAME_LENGTH:
        return f"Use {MAX_NAME_LENGTH} characters or fewer"
    if "/" in name:
        return "Playlist names can't contain /"
    return None


@dataclass
class Playlist:
    id: int
    owner_id: int
    name: str
    global_mode: bool
    songs: list[dict]
    # "owner", or the collaborator's permission (config.PLAYLIST_PERMISSIONS)
    level: str

    def allows(self, needed: str) -> bool:
        return PLAYLIST_PERMISSION_RANK.get(self.level, 0) >= PLAYLIST_PERMISSION_RANK[needed]

    @property
    def room(self) -> int:
        return max(0, MAX_PLAYLIST_SIZE - len(self.songs))


async def find(
        bot, user_id: int, name: str, guild_id: int | None, global_mode: bool, owner_id: int | None = None,
        *, shared_only: bool = False,
) -> Playlist | None:
    """A playlist the user owns or collaborates on, or None without access.

    Names are only unique per owner, so a shared playlist is addressed by ``owner_id``; without it the user's
    own playlist wins (unless ``shared_only``), then the oldest shared one.
    """
    tbl = table(global_mode)
    if not shared_only and (owner_id is None or owner_id == user_id):
        if global_mode:
            rows = await bot.fetch_db_query(f"SELECT id, songs FROM {tbl} WHERE user_id = ? AND name = ?", (user_id, name))
        else:
            rows = await bot.fetch_db_query(
                f"SELECT id, songs FROM {tbl} WHERE user_id = ? AND guild_id = ? AND name = ? ORDER BY id",
                (user_id, guild_id, name),
            )
        if rows:
            return Playlist(rows[0][0], user_id, name, global_mode, json.loads(rows[0][1]), "owner")
        if owner_id == user_id:
            return None

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
        f"SELECT p.id, p.user_id, p.songs, pc.permission FROM {tbl} p "
        f"JOIN playlist_collaborators pc ON pc.playlist_id = p.id AND pc.is_global = {1 if global_mode else 0} "
        f"WHERE {where} AND pc.user_id = ? ORDER BY p.id",
        tuple(params),
    )
    if not rows:
        return None
    pid, owner, songs, permission = rows[0]
    return Playlist(pid, owner, name, global_mode, json.loads(songs), permission or "edit")


async def find_by_name(
        bot, user_id: int, name: str, guild_id: int | None, global_mode: bool, owner_id: int | None = None,
        *, shared_only: bool = False,
) -> Playlist | None:
    """Like find(), also matching the name in another letter case (commands are typed by hand)."""
    found = await find(bot, user_id, name, guild_id, global_mode, owner_id, shared_only=shared_only)
    if found is not None:
        return found
    wanted = name.strip().lower()
    candidates = [] if shared_only else await owned(bot, user_id, guild_id, global_mode)
    candidates += await shared_with(bot, user_id, guild_id, global_mode)
    for playlist in candidates:
        if playlist.name.lower() == wanted and (owner_id is None or playlist.owner_id == owner_id):
            return playlist
    return None


async def owned(bot, user_id: int, guild_id: int | None, global_mode: bool) -> list[Playlist]:
    tbl = table(global_mode)
    if global_mode:
        rows = await bot.fetch_db_query(
            f"SELECT id, name, songs FROM {tbl} WHERE user_id = ? ORDER BY created_at DESC", (user_id,),
        )
    else:
        rows = await bot.fetch_db_query(
            f"SELECT id, name, songs FROM {tbl} WHERE user_id = ? AND guild_id = ? ORDER BY created_at DESC",
            (user_id, guild_id),
        )
    return [Playlist(pid, user_id, name, global_mode, json.loads(songs), "owner") for pid, name, songs in rows]


async def shared_with(bot, user_id: int, guild_id: int | None, global_mode: bool) -> list[Playlist]:
    tbl = table(global_mode)
    scope, params = ("", (user_id,)) if global_mode else (" AND p.guild_id = ?", (user_id, guild_id))
    rows = await bot.fetch_db_query(
        f"SELECT p.id, p.user_id, p.name, p.songs, pc.permission FROM {tbl} p "
        f"JOIN playlist_collaborators pc ON pc.playlist_id = p.id AND pc.is_global = {1 if global_mode else 0} "
        f"WHERE pc.user_id = ?{scope} ORDER BY p.created_at DESC",
        params,
    )
    return [
        Playlist(pid, owner, name, global_mode, json.loads(songs), permission or "edit")
        for pid, owner, name, songs, permission in rows
    ]


async def create(bot, user_id: int, name: str, guild_id: int | None, global_mode: bool) -> str:
    """Create an empty playlist; returns the cleaned name. Raises ValueError with a user-facing reason."""
    name = (name or "").strip()
    if error := name_error(name):
        raise ValueError(error)
    if await find(bot, user_id, name, guild_id, global_mode, owner_id=user_id):
        raise ValueError(f"{label(global_mode)} '{name}' already exists")
    if global_mode:
        await bot.execute_db_query(f"INSERT INTO {table(True)} (user_id, name, songs) VALUES (?, ?, ?)",
                                   (user_id, name, "[]"))
    else:
        await bot.execute_db_query(
            f"INSERT INTO {table(False)} (user_id, guild_id, name, songs) VALUES (?, ?, ?, ?)",
            (user_id, guild_id, name, "[]"),
        )
    return name


async def delete(bot, playlist: Playlist) -> None:
    async with lock(playlist):
        await bot.execute_db_query(f"DELETE FROM {table(playlist.global_mode)} WHERE id = ?", (playlist.id,))
        await bot.execute_db_query(
            "DELETE FROM playlist_collaborators WHERE playlist_id = ? AND is_global = ?",
            (playlist.id, 1 if playlist.global_mode else 0),
        )


# Every change reads the whole songs blob, edits it in Python and writes it back, so two writers must not
# interleave. One lock per playlist row covers the read and the write, for the cog and the Activity alike.
# Each entry counts the tasks holding or waiting for it, and goes away when none are left.
_locks: dict[tuple[str, int], tuple[asyncio.Lock, int]] = {}


@asynccontextmanager
async def lock(playlist: Playlist):
    key = (table(playlist.global_mode), playlist.id)
    held, users = _locks.get(key, (None, 0))
    if held is None:
        held = asyncio.Lock()
    _locks[key] = (held, users + 1)
    try:
        async with held:
            yield
    finally:
        _, users = _locks[key]
        if users <= 1:
            del _locks[key]
        else:
            _locks[key] = (held, users - 1)


@asynccontextmanager
async def editing(bot, playlist: Playlist):
    """Hold the playlist's lock with its songs freshly read; they are written back when the block ends.

    Slow work (extractions) belongs before the block. Raises PlaylistGone if the playlist was deleted.
    """
    async with lock(playlist):
        rows = await bot.fetch_db_query(
            f"SELECT songs FROM {table(playlist.global_mode)} WHERE id = ?", (playlist.id,),
        )
        if not rows:
            raise PlaylistGone(playlist.name)
        playlist.songs = json.loads(rows[0][0])
        yield playlist
        await bot.execute_db_query(
            f"UPDATE {table(playlist.global_mode)} SET songs = ? WHERE id = ?",
            (json.dumps(playlist.songs), playlist.id),
        )


def song_index(songs: list[dict], position: int, url: str | None = None) -> int:
    """The index a caller meant. Raises LookupError when the song is not in the playlist."""
    return index_by_url(songs, position, url, lambda s: s.get("webpage_url"), "That song is no longer in the playlist")


def append_new(playlist: Playlist, songs: list[dict]) -> dict:
    """Append the songs not already in the playlist, as many as fit. Returns the counts for a reply."""
    existing = {s.get("webpage_url") for s in playlist.songs}
    fresh, seen = [], set()
    for song in songs:
        url = song.get("webpage_url")
        if url and url not in existing and url not in seen:
            fresh.append(song)
            seen.add(url)
    skipped = len(songs) - len(fresh)
    room = playlist.room
    playlist.songs.extend(fresh[:room])
    dropped = max(0, len(fresh) - room)
    return {
        "added": len(fresh) - dropped,
        "skipped": skipped,
        "dropped": dropped,
        "truncated": dropped > 0,
        "song_count": len(playlist.songs),
    }
