import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from activity.dependencies import Bot, Controller, Listener, WsManager
from activity.helpers import (
    activity_advance,
    activity_previous,
    activity_skip_to,
    activity_stop,
    broadcast_state,
    pause_activity,
    queue_index,
    rebase_activity_clock,
    resume_activity,
)
from config import AUDIO_EFFECTS
from services.playback_service import PlaybackError
from utils.helpers import parse_time_to_seconds

router = APIRouter(prefix="/api/guild/{guild_id}", tags=["playback"])


def _voice(guild_data: dict):
    vc = guild_data.get("voice_client")
    return vc if vc and vc.is_connected() else None


# ── Pause / Resume ────────────────────────────────────────────────────

@router.post("/pause")
async def pause(guild_id: int, user: Controller, bot: Bot, ws: WsManager):
    guild_data = bot.get_guild_data(guild_id)

    if _voice(guild_data):
        if not bot.playback.handle_pause(guild_id):
            raise HTTPException(status_code=400, detail="Nothing is playing or already paused")
    else:
        if not guild_data.get("current"):
            raise HTTPException(status_code=400, detail="Nothing is playing")
        if guild_data.get("pause_position") is None:
            pause_activity(bot, guild_data)

    await broadcast_state(bot, ws, guild_id)
    return {"ok": True}


@router.post("/resume")
async def resume(guild_id: int, user: Controller, bot: Bot, ws: WsManager):
    guild_data = bot.get_guild_data(guild_id)

    if _voice(guild_data):
        if not bot.playback.handle_resume(guild_id):
            raise HTTPException(status_code=400, detail="Nothing is paused")
    else:
        if not guild_data.get("current"):
            raise HTTPException(status_code=400, detail="Nothing is playing")
        # Resuming a song that isn't paused would restart it at 0:00 for everyone.
        if guild_data.get("pause_position") is None:
            raise HTTPException(status_code=400, detail="Nothing is paused")
        resume_activity(guild_data)

    await broadcast_state(bot, ws, guild_id)
    return {"ok": True}


# ── Skip / Previous / Stop ────────────────────────────────────────────

@router.post("/skip")
async def skip(guild_id: int, user: Controller, bot: Bot, ws: WsManager):
    guild_data = bot.get_guild_data(guild_id)
    if not guild_data.get("current"):
        raise HTTPException(status_code=400, detail="Nothing is playing")

    if _voice(guild_data):
        await bot.playback.skip(guild_id)
    else:
        # A user skip always advances, in song loop too (the end-of-song guards don't apply).
        await activity_advance(bot, ws, guild_id, force=True)
        await broadcast_state(bot, ws, guild_id)

    return {"ok": True}


@router.post("/previous")
async def previous(guild_id: int, user: Controller, bot: Bot, ws: WsManager):
    guild_data = bot.get_guild_data(guild_id)

    if not bot.playback.queue_service.has_previous(guild_data):
        raise HTTPException(status_code=400, detail="There's no previous song")
    if _voice(guild_data):
        if not await bot.playback.play_previous(guild_id):
            raise HTTPException(status_code=400, detail="Couldn't go back to the previous song")
    elif await activity_previous(bot, ws, guild_id) is None:
        raise HTTPException(status_code=400, detail="There's no previous song")

    await broadcast_state(bot, ws, guild_id)
    return {"ok": True}


@router.post("/stop")
async def stop(guild_id: int, user: Controller, bot: Bot, ws: WsManager):
    guild_data = bot.get_guild_data(guild_id)
    if _voice(guild_data):
        await bot.playback.stop(guild_id)
    else:
        await activity_stop(bot, ws, guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True}


# ── Volume / Loop / Shuffle / Speed / Effects / Autoplay ──────────────

class VolumeBody(BaseModel):
    level: int


@router.post("/volume")
async def set_volume(guild_id: int, body: VolumeBody, user: Controller, bot: Bot, ws: WsManager):
    level = max(0, min(100, body.level))
    guild_data = bot.get_guild_data(guild_id)
    guild_data["volume"] = level

    vc = guild_data.get("voice_client")
    if vc and vc.source and hasattr(vc.source, "volume"):
        vc.source.volume = level / 100

    await bot.save_guild_queue(guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True, "volume": level}


class LoopBody(BaseModel):
    mode: str


@router.post("/loop")
async def set_loop(guild_id: int, body: LoopBody, user: Controller, bot: Bot, ws: WsManager):
    if body.mode not in ("off", "song", "queue"):
        raise HTTPException(status_code=400, detail="Invalid loop mode")

    bot.playback.queue_service.set_loop_mode(guild_id, body.mode)
    await bot.save_guild_queue(guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True, "loop_mode": body.mode}


@router.post("/shuffle")
async def toggle_shuffle(guild_id: int, user: Controller, bot: Bot, ws: WsManager):
    new_state = bot.playback.queue_service.toggle_shuffle(guild_id)
    await bot.save_guild_queue(guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True, "shuffle": new_state}


class SpeedBody(BaseModel):
    rate: float


async def _apply_audio_settings(bot, guild_id: int, guild_data: dict) -> bool:
    """A voice song is rebuilt so a new speed or effect applies now; Activity clients apply it themselves."""
    if _voice(guild_data):
        return await bot.playback.reapply_audio_filters(guild_id)
    return guild_data.get("current") is not None


@router.post("/speed")
async def set_speed(guild_id: int, body: SpeedBody, user: Controller, bot: Bot, ws: WsManager):
    rate = max(0.5, min(2.0, body.rate))
    guild_data = bot.get_guild_data(guild_id)
    rebase_activity_clock(bot, guild_data)
    guild_data["speed"] = rate
    applied = await _apply_audio_settings(bot, guild_id, guild_data)

    await bot.save_guild_queue(guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True, "speed": rate, "applied": applied}


class EffectBody(BaseModel):
    effect: str


@router.post("/effects")
async def set_effect(guild_id: int, body: EffectBody, user: Controller, bot: Bot, ws: WsManager):
    if body.effect not in AUDIO_EFFECTS:
        raise HTTPException(status_code=400, detail="Invalid effect")

    guild_data = bot.get_guild_data(guild_id)
    rebase_activity_clock(bot, guild_data)  # effects carry speed multipliers
    guild_data["audio_effect"] = body.effect
    applied = await _apply_audio_settings(bot, guild_id, guild_data)

    await bot.save_guild_queue(guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True, "audio_effect": body.effect, "applied": applied}


# Any listener, not only DJs: autoplay is a listening preference, not a transport control.
@router.post("/autoplay")
async def toggle_autoplay(guild_id: int, user: Listener, bot: Bot, ws: WsManager):
    guild_data = bot.get_guild_data(guild_id)
    guild_data["autoplay"] = not guild_data.get("autoplay", False)

    await bot.save_guild_queue(guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True, "autoplay": guild_data["autoplay"]}


# ── Seek ──────────────────────────────────────────────────────────────

class SeekBody(BaseModel):
    position: str


@router.post("/seek")
async def seek(guild_id: int, body: SeekBody, user: Controller, bot: Bot, ws: WsManager):
    guild_data = bot.get_guild_data(guild_id)
    current_song = guild_data.get("current")
    if not current_song:
        raise HTTPException(status_code=400, detail="Nothing is playing")
    if current_song.is_live or not current_song.duration:
        raise HTTPException(status_code=400, detail="Cannot seek in a livestream")
    if guild_data.get("seeking", False):
        raise HTTPException(status_code=409, detail="Already seeking, please wait")

    try:
        seek_seconds = parse_time_to_seconds(body.position)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    seek_seconds = max(0, min(seek_seconds, current_song.duration - 5))

    if _voice(guild_data):
        try:
            seek_seconds = await bot.playback.seek(guild_id, seek_seconds)
        except PlaybackError as e:
            status = 409 if "Already seeking" in str(e) else 400
            raise HTTPException(status_code=status, detail=str(e)) from e
    else:
        bot.playback._close_listen_segment(guild_data)
        guild_data["seek_offset"] = seek_seconds
        if guild_data.get("pause_position") is not None:
            # Stay paused at the new position so the server clock doesn't drift past it
            guild_data["pause_position"] = seek_seconds
            guild_data["start_time"] = None
        else:
            now = time.monotonic()
            guild_data["start_time"] = now
            guild_data["listen_start"] = now

    await broadcast_state(bot, ws, guild_id)
    return {"ok": True, "position": seek_seconds}


# ── Skip to position ─────────────────────────────────────────────────

class SkipToBody(BaseModel):
    position: int  # 0-based index in the upcoming songs, or in the played ones with `played`
    # The song the client meant; a stale position is matched by URL instead
    webpage_url: str | None = None
    # Go back to a played song of the queue
    played: bool = False


@router.post("/skipto")
async def skipto(guild_id: int, body: SkipToBody, user: Controller, bot: Bot, ws: WsManager):
    guild_data = bot.get_guild_data(guild_id)
    playback = bot.playback

    try:
        if _voice(guild_data):
            songs = playback.queue_service.played_before(guild_data) if body.played else guild_data["queue"]
            index = queue_index(songs, body.position, body.webpage_url)
            if body.played:
                target = await playback.play_previous(guild_id, index)
                if target is None:
                    raise LookupError("Couldn't go back to that song")
            else:
                target = await playback.skip_to(guild_id, index)
            await bot.save_guild_queue(guild_id)
        else:
            target = await activity_skip_to(bot, ws, guild_id, body.position, body.webpage_url, played=body.played)
    except LookupError as e:
        raise HTTPException(status_code=404 if body.webpage_url else 400, detail=str(e)) from e

    await broadcast_state(bot, ws, guild_id)
    return {"ok": True, "title": target.title}
