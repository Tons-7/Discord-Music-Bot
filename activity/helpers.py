import asyncio
import logging
import time

from activity import stream
from activity.state_serializer import SONG_LISTS, position_now, serialize_guild_state
from activity.tasks import spawn
from bot import MusicBot
from models.song import Song
from services.playback_service import PlaybackService
from services.queue_service import cancel_imports
from utils.helpers import index_by_url, youtube_thumbnail

logger = logging.getLogger(__name__)

# How long an Activity-only advance waits for an unfinished autoplay prefetch before searching itself
PREFETCH_WAIT = 10


def fill_missing_thumbnails(songs: list[dict]) -> list[dict]:
    """Derive YouTube thumbnails from the video ID for songs stored without one
    (older flat-extracted entries persisted an empty thumbnail)."""
    for s in songs:
        if not s.get("thumbnail"):
            s["thumbnail"] = youtube_thumbnail(s.get("webpage_url") or "")
    return songs


def collection_entries(info) -> list[dict] | None:
    """Entries of a playlist or album lookup result, or None for a single song."""
    if isinstance(info, list):
        return info
    if isinstance(info, dict) and "entries" in info:
        return info["entries"]
    return None


def member_avatar_url(member, size: int = 128) -> str | None:
    """Return a member's avatar URL, preserving animation for animated avatars."""
    if not member or not member.display_avatar:
        return None
    avatar = member.display_avatar
    fmt = "gif" if avatar.is_animated() else "png"
    return str(avatar.replace(size=size, format=fmt))


# Listening time

async def record_activity_listening(bot, ws_manager, guild_id: int, user_ids: set[int] | None = None):
    """Credit Activity listeners with the song time played since the last credit, across pauses and seeks.

    Skips when a voice client is connected (PlaybackService credits voice listeners).
    Pass user_ids explicitly when the sockets are already gone (the last viewer left).
    """
    guild_data = bot.get_guild_data(guild_id)

    vc = guild_data.get("voice_client")
    if vc and vc.is_connected():
        return

    current = guild_data.get("current")
    if not current:
        return

    seconds = bot.playback.take_listened(guild_data)
    if seconds <= 0:
        return

    if user_ids is None:
        if not ws_manager:
            return
        user_ids = ws_manager.get_connected_user_ids(guild_id)
    if not user_ids:
        return

    await asyncio.gather(*(bot.record_listening_stat(uid, guild_id, current, seconds) for uid in user_ids))
    logger.debug(f"Activity stats: {len(user_ids)} users, {seconds}s on '{current.title}'")


# Activity-only clock

def is_activity_paused(guild_data: dict) -> bool:
    """Return whether Activity-only playback is currently paused."""
    vc = guild_data.get("voice_client")
    if vc:
        return vc.is_paused()
    return guild_data.get("pause_position") is not None


def set_current_for_activity(guild_data: dict, song):
    """Set a song as current for Activity-only playback (no voice client)."""
    now = time.monotonic()
    guild_data["current"] = song
    guild_data["seek_offset"] = 0
    guild_data["start_time"] = now
    guild_data["pause_position"] = None
    guild_data.pop("source_speed", None)
    guild_data["song_seq"] = guild_data.get("song_seq", 0) + 1
    guild_data["listened"] = 0.0
    guild_data["listen_start"] = now


def pause_activity(bot, guild_data: dict) -> None:
    guild_data["pause_position"] = position_now(bot, guild_data)
    guild_data["start_time"] = None
    bot.playback._close_listen_segment(guild_data)


def resume_activity(guild_data: dict) -> None:
    now = time.monotonic()
    guild_data["seek_offset"] = guild_data.get("pause_position") or 0
    guild_data["start_time"] = now
    guild_data["pause_position"] = None
    guild_data["listen_start"] = now


def rebase_activity_clock(bot, guild_data: dict) -> None:
    """Anchor the position before the effective speed changes: position is elapsed * speed, so changing the
    speed without rebasing would rescale time already played (2:00 at 1x -> 1:00 at 0.5x)."""
    if guild_data.get("voice_client") or not guild_data.get("current"):
        return
    if guild_data.get("pause_position") is not None or not guild_data.get("start_time"):
        return
    bot.playback._close_listen_segment(guild_data)
    now = time.monotonic()
    guild_data["seek_offset"] = position_now(bot, guild_data)
    guild_data["start_time"] = now
    guild_data["listen_start"] = now


def clear_activity_playback(guild_data: dict, cancel_prefetch: bool = True) -> None:
    """Reset Activity-only playback state when nothing is playing."""
    PlaybackService.reset_playback_state(guild_data)
    if cancel_prefetch:
        MusicBot.cancel_autoplay_prefetch(guild_data)


# Broadcasts

_pending_broadcasts: set[int] = set()
# A listener's "song ended" this long before the song's end is early (see activity_advance).
ENDED_EARLY_TOLERANCE = 10
# The watcher per guild that moves the room on when an early-ended song is due: (url, seq, task).
_end_watchers: dict[int, tuple[str, int, asyncio.Task]] = {}

# Song lists as of the last broadcast per guild; unchanged ones are left out of the next one.
_sent_lists: dict[int, dict[str, tuple]] = {}


def _list_signature(songs) -> tuple:
    return tuple((s.webpage_url, s.title, s.duration, s.thumbnail, s.requested_by) for s in songs)


async def _broadcast(bot, ws_manager, guild_id: int):
    try:
        await asyncio.sleep(0.1)
    finally:
        _pending_broadcasts.discard(guild_id)
    guild_data = bot.get_guild_data(guild_id)
    lists = {
        "queue": guild_data.get("queue", []),
        "played": bot.playback.queue_service.played_before(guild_data),
        "history": guild_data.get("history", []),
    }
    signatures = {key: _list_signature(songs) for key, songs in lists.items()}
    previous = _sent_lists.get(guild_id, {})
    # Clients keep their previous list when a key is missing; every new socket gets a full snapshot first.
    changed = tuple(key for key in SONG_LISTS if previous.get(key) != signatures[key])
    _sent_lists[guild_id] = signatures
    data = serialize_guild_state(bot, guild_id, lists=changed)
    await ws_manager.broadcast(guild_id, "STATE_UPDATE", data)


def schedule_broadcast(bot, ws_manager, guild_id: int) -> None:
    """Broadcast the guild's state shortly; calls made while one is pending coalesce into it."""
    if not ws_manager or not ws_manager.has_connections(guild_id):
        return
    if guild_id in _pending_broadcasts:
        return
    _pending_broadcasts.add(guild_id)
    spawn(_broadcast(bot, ws_manager, guild_id))


async def broadcast_state(bot, ws_manager, guild_id: int):
    """Broadcast guild state to connected Activity clients (non-blocking; mutating POSTs return at once)."""
    schedule_broadcast(bot, ws_manager, guild_id)


# Activity-only transitions. Each runs under play_lock, so two clients (or a client and the end-of-song
# nudge) can't interleave; stop bumps the generation first so an advance waiting on autoplay drops its pick.

def queue_index(queue: list[Song], position: int, webpage_url: str | None) -> int:
    """The queue index a client meant. Raises LookupError when the song is gone (removed or played already)."""
    return index_by_url(queue, position, webpage_url, lambda s: s.webpage_url, "That song is no longer in the queue")


async def _wait_for_prefetch(guild_data: dict) -> None:
    task = guild_data.get("autoplay_prefetch_task")
    if task and not task.done():
        await asyncio.wait({task}, timeout=PREFETCH_WAIT)


def start_activity_prefetch(bot, guild_id: int, song: Song) -> None:
    """Find (and cache) the next autoplay song while this one plays; repeats are filtered before any download."""
    guild_data = bot.get_guild_data(guild_id)
    if not guild_data.get("autoplay") or guild_data.get("queue") or guild_data.get("loop_mode", "off") != "off":
        return
    bot.cancel_autoplay_prefetch(guild_data)

    async def prefetch():
        try:
            picks = await bot.playback._related_picks(guild_data, song)
            if picks and guild_data.get("autoplay"):
                guild_data["autoplay_prefetch"] = picks[0]
                await stream.precache(bot, picks[0])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"Activity autoplay prefetch failed: {e}")
        finally:
            if guild_data.get("autoplay_prefetch_task") is asyncio.current_task():
                guild_data["autoplay_prefetch_task"] = None

    guild_data["autoplay_prefetch_task"] = spawn(prefetch())


def _started(bot, guild_id: int, song: Song) -> None:
    spawn(stream.precache(bot, song, guild_id))
    start_activity_prefetch(bot, guild_id, song)


async def activity_advance(
        bot, ws_manager, guild_id: int, *, ended_url: str | None = None, ended_seq: int | None = None,
        force: bool = False,
) -> Song | None:
    """Unified Activity-only advance to the next song (queue, then autoplay).

    No-op when a voice client is connected (the voice path owns advancement). Records listening stats and
    history for the finished song and sets the next one current. Does NOT broadcast state; callers do.
    """
    guild_data = bot.get_guild_data(guild_id)

    vc = guild_data.get("voice_client")
    if vc and vc.is_connected():
        return guild_data.get("current")

    playback = bot.playback
    gen = playback.generation(guild_data)
    async with guild_data["play_lock"]:
        current = guild_data.get("current")
        if playback.generation(guild_data) != gen:
            return current

        # Idempotency (skipped for a forced, user-initiated skip):
        if not force:
            if ended_url is not None:
                # "song ended": only advance if that play of the song is still current (another listener may
                # have advanced already; in song loop the same URL plays again with a new play number).
                if current is None or current.webpage_url != ended_url:
                    return current
                if ended_seq is not None and ended_seq != guild_data.get("song_seq", 0):
                    return current
                if _ended_early(bot, guild_data, current):
                    # That listener's stream was cut short (or the report isn't true): the room moves on when
                    # the song's time is up instead.
                    _advance_when_due(bot, ws_manager, guild_id, current, guild_data.get("song_seq", 0))
                    return current
            elif current is not None:
                # idle start (a song added to an empty queue): only if nothing is playing yet
                return current

        queue_service = playback.queue_service
        if current:
            await record_activity_listening(bot, ws_manager, guild_id)
            queue_service.add_to_history(guild_id, current)
            if force:
                playback.request_advance(guild_data)

        next_song = await queue_service.get_next_song(guild_id)
        if not next_song and guild_data.get("autoplay") and current:
            await _wait_for_prefetch(guild_data)
            next_song = await playback.pick_autoplay_song(guild_id, current)
            if playback.generation(guild_data) != gen:
                return guild_data.get("current")

        if not next_song:
            clear_activity_playback(guild_data)
            await bot.save_guild_queue(guild_id)
            return None

        set_current_for_activity(guild_data, next_song)
        _started(bot, guild_id, next_song)
        await bot.save_guild_queue(guild_id)
        return next_song


def _ended_early(bot, guild_data: dict, song: Song) -> bool:
    if song.is_live or not song.duration:
        return False
    return position_now(bot, guild_data) < song.duration - ENDED_EARLY_TOLERANCE


def _advance_when_due(bot, ws_manager, guild_id: int, song: Song, seq: int) -> None:
    """Move the room on once the song's time is up, whatever its listeners' streams did."""
    watcher = _end_watchers.get(guild_id)
    if watcher and watcher[:2] == (song.webpage_url, seq) and not watcher[2].done():
        return

    async def watch():
        guild_data = bot.get_guild_data(guild_id)
        while True:
            current = guild_data.get("current")
            if current is None or current.webpage_url != song.webpage_url or guild_data.get("song_seq", 0) != seq:
                return
            remaining = current.duration - position_now(bot, guild_data)
            if remaining <= 0:
                break
            speed = bot.playback.get_effective_speed(guild_data) or 1.0
            # Checked again at least every 30s: a pause or a seek moves the end.
            await asyncio.sleep(min(remaining / speed + 0.5, 30.0))
        if await activity_advance(bot, ws_manager, guild_id, ended_url=song.webpage_url, ended_seq=seq) is not current:
            await broadcast_state(bot, ws_manager, guild_id)

    if watcher:
        watcher[2].cancel()
    _end_watchers[guild_id] = (song.webpage_url, seq, spawn(watch()))


async def activity_previous(bot, ws_manager, guild_id: int, index: int | None = None) -> Song | None:
    """Go back to a played song (default: the one before the current song); None when there is none."""
    guild_data = bot.get_guild_data(guild_id)
    async with guild_data["play_lock"]:
        if guild_data.get("current"):
            await record_activity_listening(bot, ws_manager, guild_id)
        previous = bot.playback.queue_service.step_back(guild_data, index)
        if previous is None:
            return None
        bot.cancel_autoplay_prefetch(guild_data)
        set_current_for_activity(guild_data, previous)
    _started(bot, guild_id, previous)
    await bot.save_guild_queue(guild_id)
    return previous


async def activity_skip_to(
        bot, ws_manager, guild_id: int, position: int, webpage_url: str | None = None, *, played: bool = False,
) -> Song:
    """Jump to a song of the queue: an upcoming one (those before it count as played) or, with ``played``, back
    to a played one. Raises LookupError when it is gone."""
    guild_data = bot.get_guild_data(guild_id)
    queue_service = bot.playback.queue_service
    async with guild_data["play_lock"]:
        if played:
            index = queue_index(queue_service.played_before(guild_data), position, webpage_url)
        else:
            index = queue_index(guild_data["queue"], position, webpage_url)
        current = guild_data.get("current")
        if current:
            await record_activity_listening(bot, ws_manager, guild_id)
            if not played:
                queue_service.add_to_history(guild_id, current)
        if played:
            target = queue_service.step_back(guild_data, index)
        else:
            queue_service.skip_ahead(guild_data, index)
            target = guild_data["queue"].pop(0)
        bot.cancel_autoplay_prefetch(guild_data)
        set_current_for_activity(guild_data, target)
    _started(bot, guild_id, target)
    await bot.save_guild_queue(guild_id)
    return target


async def activity_stop(bot, ws_manager, guild_id: int) -> None:
    """Stop Activity-only playback and clear the queue; the playing song goes to history and stats."""
    guild_data = bot.get_guild_data(guild_id)
    bot.playback.bump_generation(guild_data)
    async with guild_data["play_lock"]:
        if guild_data.get("current"):
            await record_activity_listening(bot, ws_manager, guild_id)
            bot.playback.queue_service.add_to_history(guild_id, guild_data["current"])
        bot.playback.queue_service.clear_queue(guild_id)
        clear_activity_playback(guild_data, cancel_prefetch=True)
        guild_data.pop("resume_at", None)
    await bot.save_guild_queue(guild_id)


async def end_activity_session(bot, ws_manager, guild_id: int, user_ids: set[int] | None = None) -> None:
    """The last viewer left (and the grace period passed): stop Activity-only playback, keeping the queue."""
    guild_data = bot.get_guild_data(guild_id)
    vc = guild_data.get("voice_client")
    if vc and vc.is_connected():
        return  # Bot is in voice: playback continues through it
    if not guild_data.get("current") or ws_manager.has_connections(guild_id):
        return

    async with guild_data["play_lock"]:
        # Checked again under the lock: a viewer who came back keeps the session (and their advances count).
        if ws_manager.has_connections(guild_id):
            return
        bot.playback.bump_generation(guild_data)
        await record_activity_listening(bot, ws_manager, guild_id, user_ids=user_ids)
        cancel_imports(guild_data)
        # The song that was playing stays in the queue, as played.
        bot.playback.queue_service.mark_played(guild_data, [guild_data["current"]])
        clear_activity_playback(guild_data, cancel_prefetch=True)
    await bot.save_guild_queue(guild_id)
    logger.info(f"Activity closed for guild {guild_id}, cleared playback state")
