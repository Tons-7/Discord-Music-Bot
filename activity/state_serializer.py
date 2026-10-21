import re
import time
from typing import Any

from utils.helpers import youtube_thumbnail

_MENTION_RE = re.compile(r"<@!?(\d+)>")


def requester_name(requested_by: str, bot=None, guild=None) -> str:
    """A "<@id>" requester as a readable name (the member's server name when known)."""
    match = _MENTION_RE.fullmatch(requested_by or "")
    if not match or bot is None:
        return requested_by
    uid = int(match.group(1))
    member = guild.get_member(uid) if guild else None
    user = member or bot.get_user(uid)
    return user.display_name if user else f"User {uid}"


def serialize_song(song, bot=None, guild=None) -> dict[str, Any] | None:
    if song is None:
        return None
    d = song.to_dict()
    # The live url is an expiring stream URL (with the host's IP) or a cache path: it never leaves the server.
    d.pop("url", None)
    if not d.get("thumbnail") and (thumbnail := youtube_thumbnail(d.get("webpage_url", ""))):
        d["thumbnail"] = thumbnail
    d["requested_by"] = requester_name(d.get("requested_by", ""), bot, guild)
    if source := d.pop("autoplay_source", ""):
        d["autoplay_source"] = "youtube" if source.startswith("youtube") else source
    return d


def position_now(bot, guild_data: dict) -> float:
    """Song time of the current song, in seconds, for voice and Activity-only playback alike."""
    pause_position = guild_data.get("pause_position")
    if pause_position is not None:
        return float(pause_position)
    offset = float(guild_data.get("seek_offset") or 0)
    start = guild_data.get("start_time")
    if not start:
        return offset
    playback = bot.playback
    voice_client = guild_data.get("voice_client")
    if voice_client is not None:
        if not (voice_client.is_playing() or voice_client.is_paused()):
            return offset
        speed = playback.source_speed(guild_data)
    else:
        speed = playback.get_effective_speed(guild_data)
    return max(0.0, (time.monotonic() - start) * speed + offset)


def _get_activity_position(bot, guild_id: int, guild_data: dict) -> int:
    return int(position_now(bot, guild_data))


SONG_LISTS = ("queue", "played", "history")


def serialize_guild_state(bot, guild_id: int, lists: tuple[str, ...] = SONG_LISTS) -> dict[str, Any]:
    """The guild's state for Activity clients, with the song ``lists`` named (all of them by default)."""
    # Lazy import to avoid a cycle: activity.helpers imports serialize_guild_state
    # from this module at top level.
    from activity.helpers import is_activity_paused

    guild_data = bot.get_guild_data(guild_id)
    guild = bot.get_guild(guild_id)
    queue_service = bot.playback.queue_service

    current = guild_data.get("current")
    current_dict = None
    if current:
        current_dict = serialize_song(current, bot, guild)
        current_dict["position"] = round(position_now(bot, guild_data), 1)
        current_dict["is_paused"] = is_activity_paused(guild_data)
        current_dict["seq"] = guild_data.get("song_seq", 0)

    vc = guild_data.get("voice_client")
    is_connected = vc is not None and vc.is_connected()
    channel = vc.channel if is_connected else None

    songs = {
        "queue": guild_data.get("queue", []),
        "played": queue_service.played_before(guild_data),
        "history": guild_data.get("history", []),
    }
    return {
        "current": current_dict,
        **{key: [serialize_song(s, bot, guild) for s in songs[key]] for key in lists},
        "volume": guild_data.get("volume", 100),
        "loop_mode": guild_data.get("loop_mode", "off"),
        "shuffle": guild_data.get("shuffle", False),
        "autoplay": guild_data.get("autoplay", False),
        "speed": guild_data.get("speed", 1.0),
        "audio_effect": guild_data.get("audio_effect", "none"),
        "is_connected": is_connected,
        "voice_channel_id": str(channel.id) if channel is not None else None,
        "voice_channel_name": getattr(channel, "name", None),
        "queue_duration": queue_service.get_queue_duration(guild_id),
    }
