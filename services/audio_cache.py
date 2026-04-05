import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Optional

import yt_dlp

from config import AUDIO_CACHE_DIR, AUDIO_CACHE_MAX_SIZE_MB, AUDIO_CACHE_MAX_AGE_HOURS

logger = logging.getLogger(__name__)


class AudioCacheService:
    def __init__(self, bot):
        self.bot = bot
        self.cache_dir = Path(AUDIO_CACHE_DIR).resolve()
        self.cache_dir.mkdir(exist_ok=True)
        self.max_size_bytes = AUDIO_CACHE_MAX_SIZE_MB * 1024 * 1024
        self.max_age_seconds = AUDIO_CACHE_MAX_AGE_HOURS * 3600
        self._download_tasks: dict[str, asyncio.Task] = {}
        self._lock: Optional[asyncio.Lock] = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def get_cache_path(self, webpage_url: str) -> str:
        url_hash = hashlib.sha256(webpage_url.encode()).hexdigest()[:16]
        return str(self.cache_dir / f"{url_hash}.opus")

    def get_cached_file(self, webpage_url: str) -> Optional[str]:
        path = self.get_cache_path(webpage_url)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            # Don't serve files past max age
            if time.time() - os.path.getmtime(path) > self.max_age_seconds:
                return None
            return path
        return None

    async def ensure_cached(self, webpage_url: str, song_title: str):
        if self.get_cached_file(webpage_url):
            return

        async with self._get_lock():
            if webpage_url in self._download_tasks:
                return
            task = asyncio.create_task(self._download_audio(webpage_url, song_title))
            self._download_tasks[webpage_url] = task

    async def _download_audio(self, webpage_url: str, song_title: str):
        try:
            cache_path = self.get_cache_path(webpage_url)
            # Remove extension — yt-dlp + postprocessor will add .opus
            out_template = cache_path.replace(".opus", "")

            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": out_template + ".%(ext)s",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "opus",
                        "preferredquality": "128",
                    }
                ],
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "extract_flat": False,
                "nocheckcertificate": True,
                "socket_timeout": 60,
                "retries": 3,
                "http_headers": {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
                },
            }

            loop = asyncio.get_running_loop()

            def do_download():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.extract_info(webpage_url, download=True)

            await loop.run_in_executor(self.bot.executor, do_download)

            if os.path.exists(cache_path):
                logger.info(f"Audio cached: {song_title} -> {cache_path}")
            else:
                logger.warning(f"Download completed but cache file not found at {cache_path}")

        except Exception as e:
            logger.warning(f"Audio cache download failed for '{song_title}': {e}")
            # Clean up any partial files
            cache_path = self.get_cache_path(webpage_url)
            for ext in [".opus", ".part", ".webm", ".m4a", ".mp3"]:
                partial = cache_path.replace(".opus", ext)
                if os.path.exists(partial) and ext != ".opus":
                    try:
                        os.remove(partial)
                    except OSError:
                        pass
        finally:
            async with self._get_lock():
                self._download_tasks.pop(webpage_url, None)

    def remove_cached(self, webpage_url: str):
        path = self.get_cache_path(webpage_url)
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.debug(f"Removed cached file: {path}")
        except OSError as e:
            logger.warning(f"Failed to remove cached file {path}: {e}")

    async def cleanup_cache(self):
        try:
            now = time.time()
            files = []
            expired_removed = 0

            for f in self.cache_dir.iterdir():
                if f.is_file() and f.suffix == ".opus":
                    stat = f.stat()
                    # Delete files older than max age
                    if now - stat.st_mtime > self.max_age_seconds:
                        try:
                            f.unlink()
                            expired_removed += 1
                        except OSError:
                            pass
                    else:
                        files.append((f, stat.st_atime, stat.st_size))

            if expired_removed:
                logger.info(f"Audio cache cleanup: removed {expired_removed} expired files (>{AUDIO_CACHE_MAX_AGE_HOURS}h)")

            # Size-based LRU eviction for remaining files
            total_size = sum(size for _, _, size in files)
            if total_size <= self.max_size_bytes:
                return

            files.sort(key=lambda x: x[1])

            size_removed = 0
            for file_path, _, size in files:
                if total_size <= self.max_size_bytes:
                    break
                try:
                    file_path.unlink()
                    total_size -= size
                    size_removed += 1
                except OSError:
                    pass

            if size_removed:
                logger.info(f"Audio cache size eviction: removed {size_removed} files, {total_size // (1024 * 1024)}MB remaining")

        except Exception as e:
            logger.error(f"Audio cache cleanup error: {e}")

    async def startup_cleanup(self):
        junk_extensions = {".part", ".temp", ".ytdl", ".webm", ".m4a", ".mp3", ".mp4", ".ogg"}
        try:
            removed = 0
            for f in self.cache_dir.iterdir():
                if f.is_file() and f.suffix in junk_extensions:
                    try:
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
            if removed:
                logger.info(f"Startup cleanup: removed {removed} partial/intermediate files from audio cache")
        except Exception as e:
            logger.error(f"Audio cache startup cleanup error: {e}")

    async def cancel_all_downloads(self):
        async with self._get_lock():
            for url, task in self._download_tasks.items():
                if not task.done():
                    task.cancel()
            self._download_tasks.clear()
