import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from activity.dependencies import get_bot, get_current_user, get_ws_manager, require_dj, require_guild_member
from activity.helpers import broadcast_state, record_activity_listening, set_current_for_activity
from activity.state_serializer import _get_activity_position
from config import AUDIO_EFFECTS
from models.song import Song
from utils.helpers import parse_time_to_seconds

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/guild/{guild_id}", tags=["playback"])


# ── Pause / Resume ────────────────────────────────────────────────────

@router.post("/pause")
async def pause(guild_id: int, user=Depends(get_current_user), bot=Depends(get_bot), ws=Depends(get_ws_manager)):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)
    require_dj(bot, guild_id, uid)

    guild_data = bot.get_guild_data(guild_id)
    vc = guild_data.get("voice_client")

    if vc and vc.is_connected():
        success = bot._playback_service.handle_pause(guild_id)
        if not success:
            raise HTTPException(status_code=400, detail="Nothing is playing or already paused")
    else:
        if not guild_data.get("current"):
            raise HTTPException(status_code=400, detail="Nothing is playing")

        guild_data["pause_position"] = _get_activity_position(bot, guild_id, guild_data)
        guild_data["start_time"] = None

    await broadcast_state(bot, ws, guild_id)
    return {"ok": True}


@router.post("/resume")
async def resume(guild_id: int, user=Depends(get_current_user), bot=Depends(get_bot), ws=Depends(get_ws_manager)):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)
    require_dj(bot, guild_id, uid)

    guild_data = bot.get_guild_data(guild_id)
    vc = guild_data.get("voice_client")

    if vc and vc.is_connected():
        success = bot._playback_service.handle_resume(guild_id)
        if not success:
            raise HTTPException(status_code=400, detail="Nothing is paused")
    else:
        if not guild_data.get("current"):
            raise HTTPException(status_code=400, detail="Nothing is playing")
        pause_pos = guild_data.get("pause_position", 0) or 0
        guild_data["seek_offset"] = pause_pos
        guild_data["start_time"] = datetime.now()
        guild_data["pause_position"] = None

    await broadcast_state(bot, ws, guild_id)
    return {"ok": True}


# ── Skip / Previous / Stop ────────────────────────────────────────────

@router.post("/skip")
async def skip(guild_id: int, user=Depends(get_current_user), bot=Depends(get_bot), ws=Depends(get_ws_manager)):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)
    require_dj(bot, guild_id, uid)

    guild_data = bot.get_guild_data(guild_id)
    if not guild_data.get("current"):
        raise HTTPException(status_code=400, detail="Nothing is playing")

    vc = guild_data.get("voice_client")
    if vc and vc.is_connected():
        if vc.is_playing() or vc.is_paused():
            vc.stop()  # Triggers after_playing -> play_next
    else:
        # Activity-only: advance via /play logic
        from activity.routes.stream_routes import _preextract_and_cache
        queue_service = bot._playback_service.queue_service
        await record_activity_listening(bot, ws, guild_id)
        queue_service.add_to_history(guild_id, guild_data["current"])
        next_song = await queue_service.get_next_song(guild_id)
        if next_song:
            from datetime import datetime
            guild_data["current"] = next_song
            guild_data["seek_offset"] = 0
            guild_data["start_time"] = datetime.now()
            guild_data["pause_position"] = None
            asyncio.create_task(_preextract_and_cache(bot, next_song.webpage_url, next_song.title, guild_id))
        else:
            guild_data["current"] = None
            guild_data["start_time"] = None
        await bot.save_guild_queue(guild_id)
        await broadcast_state(bot, ws, guild_id)

    return {"ok": True}


@router.post("/previous")
async def previous(guild_id: int, user=Depends(get_current_user), bot=Depends(get_bot), ws=Depends(get_ws_manager)):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)
    require_dj(bot, guild_id, uid)

    guild_data = bot.get_guild_data(guild_id)
    vc = guild_data.get("voice_client")

    if vc and vc.is_connected():
        music_cog = bot.get_cog("MusicCommands")
        if not music_cog:
            raise HTTPException(status_code=500, detail="Music commands not loaded")
        success = await music_cog.play_previous(guild_id)
        if not success:
            raise HTTPException(status_code=400, detail="No previous song in history")
    else:
        # Activity-only: mirror play_previous — walk history via history_position
        # (non-destructive), rather than popping, so repeated clicks keep working.
        history = guild_data.get("history", [])
        if not history:
            raise HTTPException(status_code=400, detail="No previous song in history")

        if "history_position" not in guild_data:
            guild_data["history_position"] = len(history)

        target = guild_data["history_position"] - 1
        if target < 0:
            raise HTTPException(status_code=400, detail="Already at the beginning of history")

        guild_data["history_position"] = target
        prev_song = history[target]

        if guild_data.get("current"):
            await record_activity_listening(bot, ws, guild_id)
            guild_data["queue"].insert(0, guild_data["current"])

        set_current_for_activity(guild_data, Song.from_dict(prev_song.to_dict()))

        from activity.routes.stream_routes import _preextract_and_cache
        asyncio.create_task(_preextract_and_cache(bot, prev_song.webpage_url, prev_song.title, guild_id))

        await bot.save_guild_queue(guild_id)

    await broadcast_state(bot, ws, guild_id)
    return {"ok": True}


@router.post("/stop")
async def stop(guild_id: int, user=Depends(get_current_user), bot=Depends(get_bot), ws=Depends(get_ws_manager)):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)
    require_dj(bot, guild_id, uid)

    guild_data = bot.get_guild_data(guild_id)
    queue_service = bot._playback_service.queue_service

    # Record Activity listening stats and save to history before stopping
    if guild_data.get("current"):
        await record_activity_listening(bot, ws, guild_id)
        queue_service.add_to_history(guild_id, guild_data["current"])

    queue_service.clear_queue(guild_id)

    guild_data["autoplay_prefetch"] = None
    prefetch_task = guild_data.get("autoplay_prefetch_task")
    if prefetch_task and not prefetch_task.done():
        prefetch_task.cancel()
    guild_data["autoplay_prefetch_task"] = None

    vc = guild_data.get("voice_client")
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()

    guild_data["current"] = None
    guild_data["start_time"] = None
    guild_data["seek_offset"] = 0

    await bot.save_guild_queue(guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True}


# ── Volume / Loop / Shuffle / Speed / Effects / Autoplay ──────────────

class VolumeBody(BaseModel):
    level: int


@router.post("/volume")
async def set_volume(guild_id: int, body: VolumeBody, user=Depends(get_current_user), bot=Depends(get_bot), ws=Depends(get_ws_manager)):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)
    require_dj(bot, guild_id, uid)

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
async def set_loop(guild_id: int, body: LoopBody, user=Depends(get_current_user), bot=Depends(get_bot), ws=Depends(get_ws_manager)):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)
    require_dj(bot, guild_id, uid)

    if body.mode not in ("off", "song", "queue"):
        raise HTTPException(status_code=400, detail="Invalid loop mode")

    bot._playback_service.queue_service.set_loop_mode(guild_id, body.mode)
    await bot.save_guild_queue(guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True, "loop_mode": body.mode}


@router.post("/shuffle")
async def toggle_shuffle(guild_id: int, user=Depends(get_current_user), bot=Depends(get_bot), ws=Depends(get_ws_manager)):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)
    require_dj(bot, guild_id, uid)

    new_state = bot._playback_service.queue_service.toggle_shuffle(guild_id)
    await bot.save_guild_queue(guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True, "shuffle": new_state}


class SpeedBody(BaseModel):
    rate: float


@router.post("/speed")
async def set_speed(guild_id: int, body: SpeedBody, user=Depends(get_current_user), bot=Depends(get_bot), ws=Depends(get_ws_manager)):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)
    require_dj(bot, guild_id, uid)

    rate = max(0.5, min(2.0, body.rate))
    guild_data = bot.get_guild_data(guild_id)
    guild_data["speed"] = rate

    await bot.save_guild_queue(guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True, "speed": rate}


class EffectBody(BaseModel):
    effect: str


@router.post("/effects")
async def set_effect(guild_id: int, body: EffectBody, user=Depends(get_current_user), bot=Depends(get_bot), ws=Depends(get_ws_manager)):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)
    require_dj(bot, guild_id, uid)

    if body.effect not in AUDIO_EFFECTS:
        raise HTTPException(status_code=400, detail="Invalid effect")

    guild_data = bot.get_guild_data(guild_id)
    guild_data["audio_effect"] = body.effect

    await bot.save_guild_queue(guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True, "audio_effect": body.effect}


@router.post("/autoplay")
async def toggle_autoplay(guild_id: int, user=Depends(get_current_user), bot=Depends(get_bot), ws=Depends(get_ws_manager)):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)

    guild_data = bot.get_guild_data(guild_id)
    guild_data["autoplay"] = not guild_data.get("autoplay", False)

    await bot.save_guild_queue(guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True, "autoplay": guild_data["autoplay"]}


# ── Seek ──────────────────────────────────────────────────────────────

class SeekBody(BaseModel):
    position: str


@router.post("/seek")
async def seek(guild_id: int, body: SeekBody, user=Depends(get_current_user), bot=Depends(get_bot), ws=Depends(get_ws_manager)):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)
    require_dj(bot, guild_id, uid)

    guild_data = bot.get_guild_data(guild_id)

    vc = guild_data.get("voice_client")
    has_voice = vc and vc.is_connected()

    if not guild_data.get("current"):
        raise HTTPException(status_code=400, detail="Nothing is playing")

    current_song = guild_data["current"]
    if not current_song.duration or current_song.duration == 0:
        raise HTTPException(status_code=400, detail="Cannot seek in a livestream")

    if guild_data.get("seeking", False):
        raise HTTPException(status_code=409, detail="Already seeking, please wait")

    try:
        seek_seconds = parse_time_to_seconds(body.position)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    seek_seconds = max(0, seek_seconds)
    if current_song.duration > 0 and seek_seconds >= current_song.duration - 5:
        seek_seconds = max(0, current_song.duration - 5)

    if not has_voice:
        guild_data["seek_offset"] = seek_seconds
        if guild_data.get("pause_position") is not None:
            # Stay paused at the new position so the server clock doesn't drift past it
            guild_data["pause_position"] = seek_seconds
            guild_data["start_time"] = None
        else:
            guild_data["start_time"] = datetime.now()
            guild_data["pause_position"] = None
        await broadcast_state(bot, ws, guild_id)
        return {"ok": True, "position": seek_seconds}

    voice_was_paused = guild_data["voice_client"].is_paused()

    try:
        guild_data["seeking"] = True
        guild_data["seeking_start_time"] = asyncio.get_running_loop().time()

        if guild_data["voice_client"].is_playing() or guild_data["voice_client"].is_paused():
            guild_data["voice_client"].stop()

        await asyncio.sleep(0.2)

        # Try cached file first
        cached_path = bot.audio_cache.get_cached_file(current_song.webpage_url) if current_song.webpage_url else None
        import discord as _discord

        strategy_used = 0
        source = None

        if cached_path:
            try:
                ffmpeg_opts = bot._playback_service._build_ffmpeg_options(guild_data, local_file=True)
                seek_opts = {
                    "before_options": f"-nostdin -ss {seek_seconds}",
                    "options": ffmpeg_opts["options"],
                }
                source = _discord.PCMVolumeTransformer(
                    _discord.FFmpegPCMAudio(cached_path, **seek_opts),
                    volume=guild_data["volume"] / 100,
                )
            except Exception:
                bot.audio_cache.remove_cached(current_song.webpage_url)
                cached_path = None

        if not cached_path:
            # Remote stream seek
            fresh_data = None
            for attempt in range(3):
                try:
                    fresh_data = await bot.get_song_info_cached(current_song.webpage_url)
                    if fresh_data and fresh_data.get("url"):
                        break
                    await asyncio.sleep(0.5)
                except Exception:
                    if attempt < 2:
                        await asyncio.sleep(1)

            if not fresh_data or not fresh_data.get("url"):
                raise HTTPException(status_code=500, detail="Failed to get fresh stream URL for seek")

            ffmpeg_opts = bot._playback_service._build_ffmpeg_options(guild_data)
            base_opts = bot.ffmpeg_options["options"]
            extra_af = ffmpeg_opts["options"].replace(base_opts, "").strip()

            seek_strategies = [
                {
                    "before_options": f"-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -ss {seek_seconds} -nostdin -user_agent 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0'",
                    "options": f"-vn -bufsize 1024k {extra_af}".strip() if extra_af else "-vn -bufsize 1024k",
                },
                {
                    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin -user_agent 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0'",
                    "options": f"-vn -ss {seek_seconds} -bufsize 1024k {extra_af}".strip() if extra_af else f"-vn -ss {seek_seconds} -bufsize 1024k",
                },
                {
                    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin",
                    "options": "-vn",
                },
            ]

            for i, opts in enumerate(seek_strategies):
                try:
                    source = _discord.PCMVolumeTransformer(
                        _discord.FFmpegPCMAudio(fresh_data["url"], **opts),
                        volume=guild_data["volume"] / 100,
                    )
                    strategy_used = i
                    break
                except Exception:
                    if i == len(seek_strategies) - 1:
                        raise HTTPException(status_code=500, detail="All seek strategies failed")

        guild_data["seek_offset"] = seek_seconds if strategy_used <= 1 else 0
        guild_data["start_time"] = datetime.now()

        playback = bot._playback_service
        queue_service = playback.queue_service

        def after_seeking(error):
            if error:
                logger.error(f"Seek player error: {error}")
            else:
                if guild_data["current"] and not guild_data.get("seeking", False):
                    queue_service.add_to_history(guild_id, guild_data["current"])
            if not guild_data.get("seeking", False):
                coro = playback.play_next(guild_id)
                fut = asyncio.run_coroutine_threadsafe(coro, bot.loop)
                fut.add_done_callback(lambda f: f.result() if not f.cancelled() else None)

        guild_data["voice_client"].play(source, after=after_seeking, bitrate=384)

        if voice_was_paused:
            await asyncio.sleep(0.2)
            guild_data["voice_client"].pause()
            guild_data["pause_position"] = seek_seconds

        guild_data["message_ready_for_timestamps"] = True

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Seek error: {e}")
        guild_data["seek_offset"] = 0
        guild_data["start_time"] = datetime.now()
        asyncio.create_task(bot._playback_service.play_next(guild_id))
        raise HTTPException(status_code=500, detail="Seek failed, restarted from beginning")
    finally:
        guild_data["seeking"] = False
        guild_data.pop("seeking_start_time", None)

    await broadcast_state(bot, ws, guild_id)
    return {"ok": True, "position": seek_seconds}


# ── Skip to position ─────────────────────────────────────────────────

class SkipToBody(BaseModel):
    position: int  # 0-based queue index


@router.post("/skipto")
async def skipto(guild_id: int, body: SkipToBody, user=Depends(get_current_user), bot=Depends(get_bot), ws=Depends(get_ws_manager)):
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)
    require_dj(bot, guild_id, uid)

    guild_data = bot.get_guild_data(guild_id)
    queue = guild_data.get("queue", [])
    queue_service = bot._playback_service.queue_service

    if body.position < 0 or body.position >= len(queue):
        raise HTTPException(status_code=400, detail="Invalid position")

    # Record stats and add current song to history
    if guild_data.get("current"):
        await record_activity_listening(bot, ws, guild_id)
        queue_service.add_to_history(guild_id, guild_data["current"])

    # Slice out the target and remove skipped songs in one operation (O(n))
    target = queue[body.position]
    # Bulk-add skipped songs to history (skip duplicate check per-song,
    # just extend and let history trimming handle the rest)
    skipped = queue[:body.position]
    history = guild_data.get("history", [])
    existing_urls = {s.webpage_url for s in history}
    for s in skipped:
        if s.webpage_url not in existing_urls:
            history.append(Song.from_dict(s.to_dict()))
            existing_urls.add(s.webpage_url)
    # Trim history
    from config import MAX_HISTORY_SIZE
    if len(history) > MAX_HISTORY_SIZE:
        guild_data["history"] = history[-MAX_HISTORY_SIZE:]
    guild_data["history_position"] = len(guild_data["history"])

    # Remove target + everything before it from queue in one slice (O(n))
    guild_data["queue"] = queue[body.position + 1:]

    vc = guild_data.get("voice_client")
    if vc and vc.is_connected():
        guild_data["current"] = target
        guild_data["seek_offset"] = 0
        guild_data["start_time"] = datetime.now()
        guild_data["pause_position"] = None
        if vc.is_playing() or vc.is_paused():
            vc.stop()  # triggers after_playing -> play_next, but current is already set
        else:
            asyncio.create_task(bot._playback_service.play_next(guild_id))
    else:
        set_current_for_activity(guild_data, target)

    # Pre-cache
    from activity.routes.stream_routes import _preextract_and_cache
    asyncio.create_task(_preextract_and_cache(bot, target.webpage_url, target.title, guild_id))

    await bot.save_guild_queue(guild_id)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True, "title": target.title}
