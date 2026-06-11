import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path

import aiohttp
import yt_dlp
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.responses import StreamingResponse

import config
from activity.auth import authenticate_query_or_header
from activity.dependencies import get_bot, get_ws_manager, guild_member
from activity.helpers import activity_advance, broadcast_state
from activity.state_serializer import serialize_song
from activity.tasks import spawn
from config import AUDIO_CACHE_DIR

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/guild/{guild_id}", tags=["stream"])

_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"

# yt-dlp options for Activity audio extraction
_activity_ytdl_opts = {
    # /best only fires when no audio-only stream exists
    "format": "bestaudio/best",
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
    # No player_client override — pinned client lists go stale and break
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


def _evict_activity_cache() -> None:
    """Evict stale/oversized entries from the Activity M4A cache.

    Deletes files older than AUDIO_CACHE_MAX_AGE_HOURS, then trims the
    oldest-accessed files until total size is under AUDIO_CACHE_MAX_SIZE_MB.
    Runs synchronously (fast os.scandir); call it from the executor.
    """
    now = time.time()
    max_age = config.AUDIO_CACHE_MAX_AGE_HOURS * 3600
    max_size = config.AUDIO_CACHE_MAX_SIZE_MB * 1024 * 1024
    try:
        entries = []
        for entry in os.scandir(_ACTIVITY_CACHE_DIR):
            if not entry.name.endswith(".m4a") or not entry.is_file():
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            # Drop expired files outright.
            if now - st.st_mtime > max_age:
                try:
                    os.remove(entry.path)
                except OSError:
                    pass
                continue
            entries.append((entry.path, st.st_atime, st.st_size))

        total = sum(size for _, _, size in entries)
        if total <= max_size:
            return

        # Evict oldest-by-atime until under the cap.
        entries.sort(key=lambda e: e[1])
        for path, _, size in entries:
            if total <= max_size:
                break
            try:
                os.remove(path)
                total -= size
            except OSError:
                pass
    except Exception as e:
        logger.debug(f"Activity cache eviction skipped: {e}")


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
        st = cache_path.stat()
        # Reuse only a complete, non-stale file; otherwise re-download.
        if st.st_size > 0 and time.time() - st.st_mtime < config.AUDIO_CACHE_MAX_AGE_HOURS * 3600:
            return
        if st.st_size > 0:
            cache_path.unlink(missing_ok=True)
    except OSError:
        pass
    if webpage_url in _downloading:
        return
    _downloading.add(webpage_url)

    # Keep the Activity cache bounded before pulling another file.
    try:
        await asyncio.get_running_loop().run_in_executor(bot.executor, _evict_activity_cache)
    except Exception:
        pass

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


async def _get_stream_url(bot, webpage_url: str, force: bool = False) -> str:
    """Extract a direct stream URL, cached for 30 minutes.

    Pass ``force=True`` to bypass and refresh the cache (e.g. after the proxy
    sees the cached googlevideo URL expire with a 403/410).
    """
    now = time.time()
    if not force:
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
    token_header = request.headers.get("Authorization")
    token_query = request.query_params.get("token")
    await authenticate_query_or_header(token_header, token_query, guild_id, bot)

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
    spawn(_download_to_cache(bot, current.webpage_url, current.title))
    return await _proxy_youtube_stream(stream_url, range_header, bot, current.webpage_url)


async def _proxy_youtube_stream(
    stream_url: str, range_header: str | None, bot=None, webpage_url: str | None = None
) -> StreamingResponse:
    """Stream chunks from YouTube to the client, forwarding Range if present.

    Both the Range and full-request paths must stream chunks rather than buffer
    the whole body — Chromium issues `Range: bytes=0-` for `<audio>` elements,
    and buffering would block playback until the full song downloads.

    If the cached googlevideo URL has expired (403/410) we evict it, re-extract
    once with ``force=True`` and proxy the fresh URL.
    """
    req_headers = {"User-Agent": _BROWSER_UA}
    if range_header:
        req_headers["Range"] = range_header

    session = _get_proxy_session()
    try:
        upstream = await session.get(stream_url, headers=req_headers)
    except Exception:
        raise HTTPException(status_code=502, detail="Upstream connection failed")

    # Expired stream URL: refresh once and retry with a fresh googlevideo URL.
    if upstream.status in (403, 410) and bot is not None and webpage_url is not None:
        upstream.release()
        _stream_cache.pop(webpage_url, None)
        try:
            stream_url = await _get_stream_url(bot, webpage_url, force=True)
        except Exception:
            raise HTTPException(status_code=502, detail="Failed to refresh expired stream")
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
async def get_stream_url_endpoint(guild_id: int, user=Depends(guild_member), bot=Depends(get_bot)):
    """Return the direct stream URL for the current song."""
    current = bot.get_guild_data(guild_id).get("current")
    if not current:
        raise HTTPException(status_code=404, detail="Nothing is playing")

    try:
        return {"url": await _get_stream_url(bot, current.webpage_url)}
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to extract stream")


@router.post("/play")
async def play_song(
    guild_id: int,
    ended_url: str | None = None,
    force: bool = False,
    user=Depends(guild_member),
    bot=Depends(get_bot),
    ws=Depends(get_ws_manager),
):
    """Advance to the next song (Activity-only playback) via the unified helper.

    force=true is a user-initiated skip; without it the advance is idempotent
    (no-op while something is already playing).
    """
    new_current = await activity_advance(bot, ws, guild_id, ended_url=ended_url, force=force)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True, "current": serialize_song(new_current) if new_current else None}


async def close_proxy_session() -> None:
    """Close the shared proxy aiohttp session (called on app shutdown)."""
    global _proxy_session
    if _proxy_session is not None and not _proxy_session.closed:
        await _proxy_session.close()
    _proxy_session = None
