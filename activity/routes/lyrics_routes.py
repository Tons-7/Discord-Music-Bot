import asyncio
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from activity.dependencies import get_bot, guild_member
from utils.lyrics import LyricsServiceUnavailable, fetch_lyrics

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/guild/{guild_id}", tags=["lyrics"])

# webpage_url -> (payload or None, timestamp). Lyrics never change for a song,
# so a hit is cached long; a miss is retried sooner in case LRCLIB gains it.
_lyrics_cache: dict[str, tuple[Optional[dict], float]] = {}
_CACHE_MAX = 200
_HIT_TTL = 6 * 3600
_MISS_TTL = 600

# webpage_url -> [lock, waiter count]. One LRCLIB request per song at a time:
# whoever waits reads the cache the first caller filled instead of refetching.
_inflight: dict[str, list] = {}


def _acquire_slot(url: str) -> list:
    slot = _inflight.get(url)
    if slot is None:
        slot = _inflight[url] = [asyncio.Lock(), 0]
    slot[1] += 1
    return slot


def _release_slot(url: str, slot: list) -> None:
    slot[1] -= 1
    if slot[1] <= 0:
        _inflight.pop(url, None)


def _cache_get(url: str):
    entry = _lyrics_cache.get(url)
    if not entry:
        return None
    payload, ts = entry
    ttl = _HIT_TTL if payload else _MISS_TTL
    if time.time() - ts >= ttl:
        del _lyrics_cache[url]
        return None
    return entry


def _cache_put(url: str, payload: Optional[dict]) -> None:
    _lyrics_cache[url] = (payload, time.time())

    if len(_lyrics_cache) > _CACHE_MAX:
        now = time.time()
        for k, (p, t) in list(_lyrics_cache.items()):
            if now - t >= (_HIT_TTL if p else _MISS_TTL):
                del _lyrics_cache[k]
        # Still over budget: drop the oldest insertions (dicts keep order).
        for k in list(_lyrics_cache)[: len(_lyrics_cache) - _CACHE_MAX]:
            del _lyrics_cache[k]


async def _resolve_lyrics(song, cache_key: Optional[str]) -> dict:
    cached = _cache_get(cache_key) if cache_key else None
    if cached:
        payload, _ = cached
        if not payload:
            raise HTTPException(status_code=404, detail="Lyrics not found")
        return payload

    try:
        result = await fetch_lyrics(song.title, song.uploader, song.duration, song.album)
    except LyricsServiceUnavailable:
        # An outage is not a property of the song — never cached.
        raise HTTPException(status_code=503, detail="Lyrics service is temporarily unavailable")

    if not result:
        if cache_key:
            _cache_put(cache_key, None)
        raise HTTPException(status_code=404, detail="Lyrics not found")

    payload = {
        "lyrics": result.get("lyrics", ""),
        "synced": result.get("synced", ""),
        "title": result.get("title", song.title),
        "artist": result.get("artist", song.uploader),
        "webpage_url": song.webpage_url,
    }
    if cache_key:
        _cache_put(cache_key, payload)
    return payload


def _find_song(guild_data: dict, url: str):
    current = guild_data.get("current")
    if current and current.webpage_url == url:
        return current
    for bucket in ("queue", "history"):
        for song in guild_data.get(bucket) or []:
            if song.webpage_url == url:
                return song
    return None


@router.get("/lyrics")
async def get_lyrics(
    guild_id: int,
    url: Optional[str] = None,
    user=Depends(guild_member),
    bot=Depends(get_bot),
):
    guild_data = bot.get_guild_data(guild_id)

    if url:
        song = _find_song(guild_data, url)
        if not song:
            raise HTTPException(status_code=404, detail="Song not found")
    else:
        song = guild_data.get("current")
        if not song:
            raise HTTPException(status_code=404, detail="Nothing is playing")

    cache_key = song.webpage_url
    cached = _cache_get(cache_key) if cache_key else None
    if cached:
        payload, _ = cached
        if not payload:
            raise HTTPException(status_code=404, detail="Lyrics not found")
        return payload

    if not cache_key:
        return await _resolve_lyrics(song, None)

    slot = _acquire_slot(cache_key)
    try:
        async with slot[0]:
            return await _resolve_lyrics(song, cache_key)
    finally:
        _release_slot(cache_key, slot)
