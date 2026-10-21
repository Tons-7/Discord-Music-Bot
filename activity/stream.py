"""Audio for the Activity's <audio> element: stream URLs, the M4A disk cache, and the YouTube proxy."""

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import yt_dlp
from fastapi import HTTPException
from starlette.responses import StreamingResponse

from activity.tasks import spawn
from config import AUDIO_CACHE_DIR, AUDIO_CACHE_MAX_DURATION, AUDIO_CACHE_MAX_FILESIZE
from services.audio_cache import TEMP_MARK
from utils.ytdl import available_at, wait_until_available

logger = logging.getLogger(__name__)

CACHE_DIR = Path(AUDIO_CACHE_DIR) / "activity"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

STREAM_TTL = 1800
CHUNK = 64 * 1024

# No total timeout: the browser may hold the connection open for the whole
# song while paused. sock_read guards against a truly dead upstream.
_PROXY_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)


@dataclass(frozen=True)
class StreamInfo:
    url: str
    headers: dict[str, str]
    protocol: str
    available_at: float
    fetched_at: float

    @property
    def segmented(self) -> bool:
        """HLS or DASH: a manifest the browser can't play through our proxy (its segments are cross-origin)."""
        return self.protocol.startswith(("m3u8", "http_dash"))


_streams: dict[str, StreamInfo] = {}
_extracting: dict[str, asyncio.Task] = {}
_downloading: set[str] = set()


def cache_path(webpage_url: str) -> Path:
    return CACHE_DIR / f"{hashlib.md5(webpage_url.encode()).hexdigest()}.m4a"


def cached_file(webpage_url: str) -> str | None:
    """The complete cached M4A of a song, or None (also while it is still being written)."""
    if not webpage_url or webpage_url in _downloading:
        return None
    path = cache_path(webpage_url)
    try:
        return str(path) if path.stat().st_size > 0 else None
    except OSError:
        return None


def _stream_options(bot) -> dict:
    return bot.ytdl_options(
        # /best only fires when no audio-only stream exists
        format="bestaudio/best", extract_flat=False, noplaylist=True, retries=3, socket_timeout=30,
    )


async def _extract_stream(bot, webpage_url: str) -> StreamInfo:
    def run():
        with yt_dlp.YoutubeDL(_stream_options(bot)) as ydl:
            return ydl.extract_info(webpage_url, download=False)

    info = await asyncio.get_running_loop().run_in_executor(bot.executor, run)
    if not info or not info.get("url"):
        raise ValueError("No stream URL extracted")
    stream = StreamInfo(
        url=info["url"],
        headers={k: v for k, v in (info.get("http_headers") or {}).items() if isinstance(v, str)},
        protocol=str(info.get("protocol") or "https"),
        available_at=available_at(info),
        fetched_at=time.time(),
    )
    _streams[webpage_url] = stream
    if len(_streams) > 100:
        cutoff = time.time() - STREAM_TTL
        for key in [k for k, v in _streams.items() if v.fetched_at < cutoff]:
            del _streams[key]
    return stream


async def stream_info(bot, webpage_url: str, *, force: bool = False) -> StreamInfo:
    """A playable stream of the song, cached for 30 minutes; listeners asking at once share one extraction.

    ``force`` refreshes it (the proxy saw the cached URL expire with a 403/410).
    """
    cached = _streams.get(webpage_url)
    if not force and cached and time.time() - cached.fetched_at < STREAM_TTL:
        return cached
    task = _extracting.get(webpage_url)
    if task is None:
        task = asyncio.ensure_future(_extract_stream(bot, webpage_url))
        _extracting[webpage_url] = task
        task.add_done_callback(lambda t: _extracting.pop(webpage_url, None))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
    return await asyncio.shield(task)


def forget_stream(webpage_url: str) -> None:
    _streams.pop(webpage_url, None)


async def _wait_until_available(stream: StreamInfo) -> None:
    await wait_until_available(stream.available_at)


# Disk cache

async def download_to_cache(bot, webpage_url: str, title: str = "", *, is_live: bool = False, duration: int = 0):
    """Save the song as M4A for instant start and seeking. Live streams and over-long songs are skipped."""
    if not webpage_url or is_live or (AUDIO_CACHE_MAX_DURATION > 0 and duration > AUDIO_CACHE_MAX_DURATION):
        return
    target = cache_path(webpage_url)
    if cached_file(webpage_url) or webpage_url in _downloading:
        return
    _downloading.add(webpage_url)
    base = str(target.with_suffix(""))

    # Second guard: songs listed without a duration or live flag are checked again once yt-dlp has them.
    rule = "!is_live" + (f" & duration <? {AUDIO_CACHE_MAX_DURATION}" if AUDIO_CACHE_MAX_DURATION > 0 else "")

    def stop_on_shutdown(_progress):
        if bot.shutting_down:
            raise yt_dlp.utils.DownloadCancelled("shutting down")

    opts = bot.ytdl_options(
        # YouTube's own AAC is kept as is; anything else is converted once
        format="bestaudio[ext=m4a]/bestaudio/best",
        extract_flat=False,
        noplaylist=True,
        outtmpl=f"{base}{TEMP_MARK}%(ext)s",
        postprocessors=[{"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "0"}],
        socket_timeout=60,
        retries=3,
        overwrites=True,
        max_filesize=AUDIO_CACHE_MAX_FILESIZE,
        match_filter=yt_dlp.utils.match_filter_func(rule),
        progress_hooks=[stop_on_shutdown],
    )

    def run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(webpage_url, download=True)

    def temp_files() -> list[Path]:
        return [p for p in CACHE_DIR.glob(f"{target.stem}{TEMP_MARK}*") if p.is_file()]

    try:
        await asyncio.get_running_loop().run_in_executor(bot.download_executor, run)
        finished = [p for p in temp_files() if p.suffix == ".m4a"]
        if finished:
            os.replace(finished[0], target)
            logger.info(f"Activity cached: {title or webpage_url}")
        else:
            logger.info(f"Activity cache skipped {title or webpage_url} (live, too long or too large)")
    except (yt_dlp.utils.YoutubeDLError, OSError) as e:
        logger.warning(f"Activity cache download failed: {e}")
    finally:
        for leftover in temp_files():
            leftover.unlink(missing_ok=True)
        _downloading.discard(webpage_url)


def cache_song(bot, song) -> None:
    spawn(download_to_cache(bot, song.webpage_url, song.title, is_live=song.is_live, duration=song.duration))


async def precache(bot, song, guild_id: int | None = None):
    """Resolve the song's stream now and cache it, then the next queued song, so /stream starts at once."""
    if not song.is_live:
        try:
            await stream_info(bot, song.webpage_url)
        except Exception as e:
            logger.debug(f"Pre-extraction failed for {song.title}: {e}")
    await download_to_cache(bot, song.webpage_url, song.title, is_live=song.is_live, duration=song.duration)

    if guild_id is not None:
        queue = bot.get_guild_data(guild_id).get("queue", [])
        if queue and not cached_file(queue[0].webpage_url):
            upcoming = queue[0]
            await download_to_cache(
                bot, upcoming.webpage_url, upcoming.title, is_live=upcoming.is_live, duration=upcoming.duration,
            )


# Proxy

async def _open_upstream(bot, stream: StreamInfo, range_header: str | None) -> aiohttp.ClientResponse:
    headers = dict(stream.headers)
    if range_header:
        headers["Range"] = range_header
    await _wait_until_available(stream)
    try:
        return await bot.stream_session.get(stream.url, headers=headers, timeout=_PROXY_TIMEOUT)
    except (aiohttp.ClientError, TimeoutError):
        raise HTTPException(status_code=502, detail="Upstream connection failed") from None


async def _remuxed(stream: StreamInfo) -> StreamingResponse:
    """Repackage an HLS/DASH stream (SoundCloud, YouTube live) as fragmented MP4 the <audio> element plays."""
    args = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    if stream.headers:
        args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in stream.headers.items())]
    args += [
        "-i", stream.url, "-vn", "-c:a", "copy",
        "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof", "pipe:1",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, NotImplementedError) as e:
        logger.warning(f"Could not start FFmpeg to repackage a stream: {e}")
        raise HTTPException(status_code=502, detail="This stream can't be played in the Activity yet") from e

    async def body():
        try:
            while chunk := await proc.stdout.read(CHUNK):
                yield chunk
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()

    return StreamingResponse(
        body(), status_code=200, media_type="audio/mp4",
        headers={"Cache-Control": "no-cache", "Accept-Ranges": "none"},
    )


async def proxy_stream(bot, webpage_url: str, range_header: str | None) -> StreamingResponse:
    """Stream the song from its source, forwarding Range.

    Never read the whole body before responding: Chromium asks `<audio>` for `Range: bytes=0-`, and
    buffering would hold playback until the song had downloaded. An expired URL (403/410) is
    re-extracted once.
    """
    try:
        stream = await stream_info(bot, webpage_url)
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to extract audio stream") from e
    if stream.segmented:
        return await _remuxed(stream)

    upstream = await _open_upstream(bot, stream, range_header)
    if upstream.status in (403, 410):
        upstream.release()
        forget_stream(webpage_url)
        try:
            stream = await stream_info(bot, webpage_url, force=True)
        except Exception as e:
            raise HTTPException(status_code=502, detail="Failed to refresh expired stream") from e
        if stream.segmented:
            return await _remuxed(stream)
        upstream = await _open_upstream(bot, stream, range_header)

    resp_headers: dict[str, str] = {"Accept-Ranges": "bytes"}
    if not range_header:
        resp_headers["Cache-Control"] = "no-cache"
    for key in ("Content-Length", "Content-Range"):
        if value := upstream.headers.get(key):
            resp_headers[key] = value

    async def body():
        try:
            async for chunk in upstream.content.iter_chunked(CHUNK):
                yield chunk
        finally:
            upstream.release()

    return StreamingResponse(
        body(),
        status_code=upstream.status,
        media_type=upstream.headers.get("Content-Type", "audio/mp4"),
        headers=resp_headers,
    )
