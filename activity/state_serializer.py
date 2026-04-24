from datetime import datetime
from typing import Any


def _ensure_thumbnail(d: dict) -> dict:
    """Fill in missing thumbnail from webpage_url (YouTube video ID)."""
    if d.get("thumbnail"):
        return d
    wp = d.get("webpage_url", "")
    vid = ""
    if "watch?v=" in wp:
        vid = wp.split("watch?v=")[1].split("&")[0]
    elif "youtu.be/" in wp:
        vid = wp.split("youtu.be/")[1].split("?")[0]
    if vid:
        d["thumbnail"] = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    return d


import re

_MENTION_RE = re.compile(r"<@!?(\d+)>")


def serialize_song(song, bot=None) -> dict[str, Any]:
    if song is None:
        return None
    d = song.to_dict()
    d.setdefault("is_live", getattr(song, "is_live", False))
    _ensure_thumbnail(d)

    # Resolve Discord mention to display name
    rb = d.get("requested_by", "")
    m = _MENTION_RE.match(rb)
    if m and bot:
        uid = int(m.group(1))
        user = bot.get_user(uid)
        d["requested_by"] = user.display_name if user else f"User {uid}"

    return d


def _get_activity_position(bot, guild_id: int, guild_data: dict) -> int:
    """Get current playback position, handling Activity-only mode.

    The bot's PlaybackService.get_current_position returns seek_offset (0)
    when there's no voice_client. For Activity-only playback, we compute
    elapsed time from start_time instead.
    """
    playback = bot._playback_service

    vc = guild_data.get("voice_client")
    if vc:
        return playback.get_current_position(guild_id)

    start_time = guild_data.get("start_time")
    if not start_time:
        return guild_data.get("seek_offset", 0)

    pause_pos = guild_data.get("pause_position")
    if pause_pos is not None:
        return pause_pos

    elapsed = (datetime.now() - start_time).total_seconds()
    speed = playback.get_effective_speed(guild_data)
    return int(elapsed * speed) + guild_data.get("seek_offset", 0)


def serialize_guild_state(bot, guild_id: int) -> dict[str, Any]:
    guild_data = bot.get_guild_data(guild_id)
    playback = bot._playback_service
    queue_service = playback.queue_service

    current = guild_data.get("current")
    current_dict = None
    if current:
        current_dict = serialize_song(current, bot)
        current_dict["position"] = _get_activity_position(bot, guild_id, guild_data)
        vc = guild_data.get("voice_client")
        if vc:
            current_dict["is_paused"] = vc.is_paused()
        else:
            current_dict["is_paused"] = guild_data.get("pause_position") is not None

    vc = guild_data.get("voice_client")
    is_connected = vc is not None and vc.is_connected()

    queue = guild_data.get("queue", [])
    history = guild_data.get("history", [])

    return {
        "current": current_dict,
        "queue": [serialize_song(s, bot) for s in queue],
        "history": [serialize_song(s, bot) for s in history],
        "volume": guild_data.get("volume", 100),
        "loop_mode": guild_data.get("loop_mode", "off"),
        "shuffle": guild_data.get("shuffle", False),
        "autoplay": guild_data.get("autoplay", False),
        "speed": guild_data.get("speed", 1.0),
        "audio_effect": guild_data.get("audio_effect", "none"),
        "is_connected": is_connected,
        "queue_duration": queue_service.get_queue_duration(guild_id),
    }
