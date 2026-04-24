import asyncio
import hashlib
import logging
import time
from pathlib import Path

import aiohttp
import yt_dlp
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.responses import StreamingResponse

from activity.dependencies import get_bot, get_current_user, require_guild_member
from config import AUDIO_CACHE_DIR

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/guild/{guild_id}", tags=["stream"])

_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"

# yt-dlp options for Activity audio extraction
_activity_ytdl_opts = {
    "format": "bestaudio",
    "extract_flat": False,
    "noplaylist": True,
    "nocheckcertificate": True,
    "quiet": True,
    "no_warnings": True,
    "source_address": "0.0.0.0",
    "retries": 3,
    "socket_timeout": 30,
    "geo_bypass": True,
    "http_headers": {"User-Agent": _BROWSER_UA},
}

_activity_ytdl = yt_dlp.YoutubeDL(_activity_ytdl_opts)

_ACTIVITY_CACHE_DIR = Path(AUDIO_CACHE_DIR) / "activity"
_ACTIVITY_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_stream_cache: dict[str, tuple[str, float]] = {}  # webpage_url -> (stream_url, timestamp)
_STREAM_CACHE_TTL = 1800
_downloading: set[str] = set()

# No total timeout — the browser may hold the connection open for the whole
# song while paused. sock_read guards against a truly dead upstream.
_PROXY_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)

_proxy_session: aiohttp.ClientSession | None = None


def _get_proxy_session() -> aiohttp.ClientSession:
    global _proxy_session
    if _proxy_session is None or _proxy_session.closed:
        _proxy_session = aiohttp.ClientSession(
            timeout=_PROXY_TIMEOUT,
            connector=aiohttp.TCPConnector(limit=20, keepalive_timeout=60),
        )
    return _proxy_session


def _get_cache_path(webpage_url: str) -> Path:
    url_hash = hashlib.md5(webpage_url.encode()).hexdigest()
    return _ACTIVITY_CACHE_DIR / f"{url_hash}.m4a"


def _get_cached_file(webpage_url: str) -> str | None:
    """Return cached M4A path if complete and under 28 days old."""
    if webpage_url in _downloading:
        return None  # still being written
    path = _get_cache_path(webpage_url)
    try:
        stat = path.stat()
    except OSError:
        return None
    if stat.st_size > 0 and time.time() - stat.st_mtime < 28 * 24 * 3600:
        return str(path)
    return None


async def _download_to_cache(bot, webpage_url: str, title: str = ""):
    """Download best audio and convert to M4A for the Activity cache."""
    cache_path = _get_cache_path(webpage_url)
    try:
        if cache_path.stat().st_size > 0:
            return
    except OSError:
        pass
    if webpage_url in _downloading:
        return
    _downloading.add(webpage_url)

    opts = {
        **_activity_ytdl_opts,
        "outtmpl": f"{cache_path.with_suffix('')}.%(ext)s",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "m4a",
            "preferredquality": "0",
        }],
        "socket_timeout": 60,
    }

    def do_download():
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(webpage_url, download=True)

    try:
        await asyncio.get_running_loop().run_in_executor(bot.executor, do_download)
        if cache_path.exists():
            logger.info(f"Activity cached: {title or webpage_url}")
        else:
            logger.warning(f"Activity cache file not found after download: {cache_path}")
    except Exception as e:
        logger.warning(f"Activity cache download failed: {e}")
    finally:
        _downloading.discard(webpage_url)


async def _get_stream_url(bot, webpage_url: str) -> str:
    """Extract a direct stream URL, cached for 30 minutes."""
    now = time.time()
    cached = _stream_cache.get(webpage_url)
    if cached and now - cached[1] < _STREAM_CACHE_TTL:
        return cached[0]

    info = await asyncio.get_running_loop().run_in_executor(
        bot.executor,
        lambda: _activity_ytdl.extract_info(webpage_url, download=False),
    )
    if not info or not info.get("url"):
        raise ValueError("No stream URL extracted")

    _stream_cache[webpage_url] = (info["url"], now)

    # Evict stale entries
    if len(_stream_cache) > 100:
        cutoff = now - _STREAM_CACHE_TTL
        for k in [k for k, (_, t) in _stream_cache.items() if t < cutoff]:
            del _stream_cache[k]

    return info["url"]


async def _preextract_and_cache(bot, webpage_url: str, title: str = "", guild_id: int = None):
    """Pre-extract stream URL and cache current + next song."""
    try:
        await _get_stream_url(bot, webpage_url)
    except Exception:
        pass

    await _download_to_cache(bot, webpage_url, title)

    if guild_id is not None:
        try:
            queue = bot.get_guild_data(guild_id).get("queue", [])
            if queue and not _get_cached_file(queue[0].webpage_url):
                await _download_to_cache(bot, queue[0].webpage_url, queue[0].title)
        except Exception:
            pass


@router.get("/stream")
async def stream_current(guild_id: int, request: Request, bot=Depends(get_bot)):
    """Serve the current song's audio. Cached M4A first, YouTube proxy fallback."""
    from activity.auth import get_discord_user
    from activity.permissions import check_banned

    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else request.query_params.get("token", "")
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")

    user = await get_discord_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")

    uid = int(user["id"])
    if check_banned(uid):
        raise HTTPException(status_code=403, detail="Banned")

    require_guild_member(bot, guild_id, uid)

    current = bot.get_guild_data(guild_id).get("current")
    if not current:
        raise HTTPException(status_code=404, detail="Nothing is playing")

    cached_path = _get_cached_file(current.webpage_url)
    if cached_path:
        return FileResponse(
            cached_path,
            media_type="audio/mp4",
            headers={"Cache-Control": "no-cache", "Accept-Ranges": "bytes"},
        )

    try:
        stream_url = await _get_stream_url(bot, current.webpage_url)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to extract audio stream")

    range_header = request.headers.get("range")
    asyncio.create_task(_download_to_cache(bot, current.webpage_url, current.title))
    return await _proxy_youtube_stream(stream_url, range_header)


async def _proxy_youtube_stream(stream_url: str, range_header: str | None) -> StreamingResponse:
    """Stream chunks from YouTube to the client, forwarding Range if present.

    Both the Range and full-request paths must stream chunks rather than buffer
    the whole body — Chromium issues `Range: bytes=0-` for `<audio>` elements,
    and buffering would block playback until the full song downloads.
    """
    req_headers = {"User-Agent": _BROWSER_UA}
    if range_header:
        req_headers["Range"] = range_header

    session = _get_proxy_session()
    try:
        upstream = await session.get(stream_url, headers=req_headers)
    except Exception:
        raise HTTPException(status_code=502, detail="Upstream connection failed")

    resp_headers: dict[str, str] = {"Accept-Ranges": "bytes"}
    if not range_header:
        resp_headers["Cache-Control"] = "no-cache"
    for key in ("Content-Length", "Content-Range"):
        val = upstream.headers.get(key)
        if val:
            resp_headers[key] = val

    async def body():
        try:
            async for chunk in upstream.content.iter_chunked(65536):
                yield chunk
        finally:
            upstream.release()

    return StreamingResponse(
        body(),
        status_code=upstream.status,
        media_type=upstream.headers.get("Content-Type", "audio/mp4"),
        headers=resp_headers,
    )


@router.get("/stream/url")
async def get_stream_url_endpoint(guild_id: int, user=Depends(get_current_user), bot=Depends(get_bot)):
    """Return the direct stream URL for the current song."""
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)

    current = bot.get_guild_data(guild_id).get("current")
    if not current:
        raise HTTPException(status_code=404, detail="Nothing is playing")

    try:
        return {"url": await _get_stream_url(bot, current.webpage_url)}
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to extract stream")


@router.post("/play")
async def play_song(guild_id: int, request: Request, user=Depends(get_current_user), bot=Depends(get_bot)):
    """Advance to the next song in queue (Activity-driven playback)."""
    uid = int(user["id"])
    require_guild_member(bot, guild_id, uid)

    guild_data = bot.get_guild_data(guild_id)
    queue_service = bot._playback_service.queue_service

    if guild_data.get("current"):
        from activity.helpers import record_activity_listening
        await record_activity_listening(bot, request.app.state.ws_manager, guild_id)
        queue_service.add_to_history(guild_id, guild_data["current"])

    next_song = await queue_service.get_next_song(guild_id)

    # Try autoplay if queue is empty
    if not next_song and guild_data.get("autoplay") and guild_data.get("current"):
        try:
            from models.song import Song
            prefetched = guild_data.get("autoplay_prefetch")
            if prefetched:
                next_song = Song(prefetched) if isinstance(prefetched, dict) else prefetched
                next_song.requested_by = "Autoplay"
                guild_data["autoplay_prefetch"] = None
            else:
                related = await bot._music_service.get_related_songs(guild_data["current"], limit=1)
                if related:
                    next_song = Song(related[0])
                    next_song.requested_by = "Autoplay"
        except Exception as e:
            logger.debug(f"Activity autoplay failed: {e}")

    if not next_song:
        guild_data["current"] = None
        guild_data["start_time"] = None
        await bot.save_guild_queue(guild_id)
        await _broadcast(request.app.state.ws_manager, bot, guild_id)
        return {"ok": True, "current": None}

    from datetime import datetime
    from activity.state_serializer import serialize_song

    guild_data["current"] = next_song
    guild_data["seek_offset"] = 0
    guild_data["start_time"] = datetime.now()
    guild_data["pause_position"] = None

    # Extract stream URL now so /stream gets an instant cache hit (no executor contention)
    try:
        await _get_stream_url(bot, next_song.webpage_url)
    except Exception:
        pass

    async def _bg_cache():
        await _download_to_cache(bot, next_song.webpage_url, next_song.title)
        # Pre-cache next song in queue
        queue = guild_data.get("queue", [])
        if queue and not _get_cached_file(queue[0].webpage_url):
            await _download_to_cache(bot, queue[0].webpage_url, queue[0].title)
    asyncio.create_task(_bg_cache())

    # Prefetch next autoplay recommendation
    if guild_data.get("autoplay") and not guild_data.get("queue"):
        async def _prefetch():
            try:
                related = await bot._music_service.get_related_songs(next_song, limit=1)
                if related:
                    guild_data["autoplay_prefetch"] = related[0]
                    await _download_to_cache(bot, related[0].get("webpage_url", ""), related[0].get("title", ""))
            except Exception:
                pass
        asyncio.create_task(_prefetch())

    await bot.save_guild_queue(guild_id)
    await _broadcast(request.app.state.ws_manager, bot, guild_id)
    return {"ok": True, "current": serialize_song(next_song)}


async def _broadcast(ws_manager, bot, guild_id: int):
    """Broadcast state update to connected Activity clients."""
    if ws_manager and ws_manager.has_connections(guild_id):
        await asyncio.sleep(0.1)
        from activity.state_serializer import serialize_guild_state
        await ws_manager.broadcast(guild_id, "STATE_UPDATE", serialize_guild_state(bot, guild_id))
