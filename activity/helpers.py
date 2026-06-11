import asyncio
import logging
from datetime import datetime
from typing import Optional

from activity.state_serializer import serialize_guild_state
from activity.tasks import spawn
from models.song import Song
from utils.helpers import youtube_thumbnail

logger = logging.getLogger(__name__)


def fill_missing_thumbnails(songs: list[dict]) -> list[dict]:
    """Derive YouTube thumbnails from the video ID for songs stored without one
    (older flat-extracted entries persisted an empty thumbnail)."""
    for s in songs:
        if not s.get("thumbnail"):
            s["thumbnail"] = youtube_thumbnail(s.get("webpage_url") or "")
    return songs


def member_avatar_url(member, size: int = 128) -> Optional[str]:
    """Return a member's avatar URL, preserving animation for animated avatars."""
    if not member or not member.display_avatar:
        return None
    avatar = member.display_avatar
    fmt = "gif" if avatar.is_animated() else "png"
    return str(avatar.replace(size=size, format=fmt))


async def record_activity_listening(bot, ws_manager, guild_id: int, user_ids: set[int] | None = None):
    """Record listening stats for Activity users on song end.

    Skips when voice client is connected (PlaybackService handles those).
    Pass user_ids explicitly when WS connections are already cleared (on_last_disconnect).
    """
    guild_data = bot.get_guild_data(guild_id)

    vc = guild_data.get("voice_client")
    if vc and vc.is_connected():
        return

    current = guild_data.get("current")
    if not current:
        return

    start_time = guild_data.get("start_time")
    if not start_time:
        pause_pos = guild_data.get("pause_position")
        if not pause_pos or pause_pos <= 0:
            return
        duration = int(pause_pos)
    else:
        elapsed = (datetime.now() - start_time).total_seconds()
        duration = int(elapsed * bot._playback_service.get_effective_speed(guild_data))

    if duration <= 0:
        return
    if current.duration and current.duration > 0:
        duration = min(duration, current.duration)

    if user_ids is None:
        if not ws_manager:
            return
        user_ids = ws_manager.get_connected_user_ids(guild_id)

    if not user_ids:
        return

    await asyncio.gather(*(
        bot.record_listening_stat(uid, guild_id, current, duration)
        for uid in user_ids
    ))

    logger.debug(f"Activity stats: {len(user_ids)} users, {duration}s on '{current.title}'")


def is_activity_paused(guild_data: dict) -> bool:
    """Return whether Activity-only playback is currently paused."""
    vc = guild_data.get("voice_client")
    if vc:
        return vc.is_paused()
    return guild_data.get("pause_position") is not None


def clear_activity_playback(guild_data: dict, cancel_prefetch: bool = True) -> None:
    """Reset Activity-only playback state when nothing is playing."""
    guild_data["current"] = None
    guild_data["start_time"] = None
    guild_data["seek_offset"] = 0
    guild_data["pause_position"] = None

    if cancel_prefetch:
        guild_data["autoplay_prefetch"] = None
        prefetch_task = guild_data.get("autoplay_prefetch_task")
        if prefetch_task and not prefetch_task.done():
            prefetch_task.cancel()
        guild_data["autoplay_prefetch_task"] = None


async def broadcast_state(bot, ws_manager, guild_id: int):
    """Broadcast guild state to connected Activity clients (non-blocking).

    Schedules the 0.1s ordering yield + serialize + broadcast off the request
    path so mutating POSTs return immediately, while preserving send ordering.
    """
    if not ws_manager or not ws_manager.has_connections(guild_id):
        return

    async def _broadcast():
        await asyncio.sleep(0.1)
        data = serialize_guild_state(bot, guild_id)
        await ws_manager.broadcast(guild_id, "STATE_UPDATE", data)

    spawn(_broadcast())


def set_current_for_activity(guild_data: dict, song):
    """Set a song as current for Activity-only playback (no voice client)."""
    guild_data["current"] = song
    guild_data["seek_offset"] = 0
    guild_data["start_time"] = datetime.now()
    guild_data["pause_position"] = None


async def activity_advance(bot, ws_manager, guild_id: int, *, ended_url: str | None = None, force: bool = False) -> Optional[Song]:
    """Unified Activity-only advance-to-next-song.

    Acquires the per-guild play_lock internally. No-op when a voice client is
    connected (the voice path owns advancement). Records listening stats and
    history for the finished song, picks the next song (queue then autoplay),
    sets it as current, and schedules background caching + autoplay prefetch.
    Does NOT broadcast state; callers should call broadcast_state afterwards.
    """
    guild_data = bot.get_guild_data(guild_id)

    vc = guild_data.get("voice_client")
    if vc and vc.is_connected():
        return guild_data.get("current")

    async with guild_data["play_lock"]:
        current = guild_data.get("current")

        # Idempotency (skipped for a forced, user-initiated skip):
        if not force:
            if ended_url is not None:
                # "song ended" event — only advance if the song that ended is
                # still current (another client may have already advanced).
                if current is None or current.webpage_url != ended_url:
                    return current
            else:
                # idle-start (e.g. add-to-empty-queue) — only advance if nothing
                # is playing yet, so concurrent clients don't double-advance.
                if current is not None:
                    return current

        queue_service = bot._playback_service.queue_service

        if current:
            await record_activity_listening(bot, ws_manager, guild_id)
            queue_service.add_to_history(guild_id, current)

        next_song = await queue_service.get_next_song(guild_id)

        if not next_song and guild_data.get("autoplay") and current:
            next_song = await bot._playback_service.pick_autoplay_song(guild_id, current)

        if not next_song:
            clear_activity_playback(guild_data)
            await bot.save_guild_queue(guild_id)
            return None

        set_current_for_activity(guild_data, next_song)

        # Pre-extract + cache the new current song so /stream gets a cache hit.
        from activity.routes.stream_routes import _preextract_and_cache
        spawn(_preextract_and_cache(bot, next_song.webpage_url, next_song.title, guild_id))

        # Prefetch the next autoplay recommendation while this song plays.
        if guild_data.get("autoplay") and not guild_data.get("queue"):
            async def _prefetch(song=next_song):
                try:
                    related = await bot._music_service.get_related_songs(song, limit=3)
                    if related:
                        prefetched = Song(related[0])
                        prefetched.requested_by = "Autoplay"
                        guild_data["autoplay_prefetch"] = prefetched
                        await _preextract_and_cache(
                            bot, prefetched.webpage_url, prefetched.title, guild_id
                        )
                except Exception as e:
                    logger.debug(f"Activity autoplay prefetch failed: {e}")

            guild_data["autoplay_prefetch_task"] = spawn(_prefetch())

        await bot.save_guild_queue(guild_id)
        return next_song
