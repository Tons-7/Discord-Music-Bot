import asyncio
import hashlib
import logging
import os
import threading
from pathlib import Path

import yt_dlp

from config import AUDIO_CACHE_DIR, AUDIO_CACHE_MAX_DURATION, AUDIO_CACHE_MAX_FILESIZE

logger = logging.getLogger(__name__)

# Downloads are written under <name>.dl.<ext> and renamed only once complete, so a partial file is never served.
TEMP_MARK = ".dl."


def cacheable(duration: int | None, is_live: bool = False) -> bool:
    """Whether a song may go into the audio cache: not live, and not longer than AUDIO_CACHE_MAX_DURATION."""
    if is_live:
        return False
    if AUDIO_CACHE_MAX_DURATION <= 0:
        return True
    return 0 < (duration or 0) <= AUDIO_CACHE_MAX_DURATION


class AudioCacheService:
    def __init__(self, bot):
        self.bot = bot
        self.cache_dir = Path(AUDIO_CACHE_DIR).resolve()
        self.cache_dir.mkdir(exist_ok=True)
        self._download_tasks: dict[str, asyncio.Task] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def get_cache_path(self, webpage_url: str) -> str:
        url_hash = hashlib.sha256(webpage_url.encode()).hexdigest()[:16]
        return str(self.cache_dir / f"{url_hash}.opus")

    def get_cached_file(self, webpage_url: str) -> str | None:
        if webpage_url in self._download_tasks:
            return None
        path = self.get_cache_path(webpage_url)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
        return None

    async def ensure_cached(self, webpage_url: str, song_title: str):
        if self.get_cached_file(webpage_url):
            return

        async with self._get_lock():
            if webpage_url in self._download_tasks:
                return
            cancel = threading.Event()
            self._cancel_events[webpage_url] = cancel
            task = asyncio.create_task(self._download_audio(webpage_url, song_title, cancel))
            self._download_tasks[webpage_url] = task

    def _temp_files(self, base: str) -> list[Path]:
        folder, name = os.path.split(base)
        return [p for p in Path(folder).glob(f"{name}{TEMP_MARK}*") if p.is_file()]

    async def _download_audio(self, webpage_url: str, song_title: str, cancel: threading.Event | None = None):
        cache_path = self.get_cache_path(webpage_url)
        base = cache_path.removesuffix(".opus")
        cancel = cancel or threading.Event()

        def stop_if_cancelled(_progress):
            if cancel.is_set() or self.bot.shutting_down:
                raise yt_dlp.utils.DownloadCancelled("cache download cancelled")

        ydl_opts = self.bot.ytdl_options(
            format="bestaudio/best",
            outtmpl=f"{base}{TEMP_MARK}%(ext)s",
            postprocessors=[{"key": "FFmpegExtractAudio", "preferredcodec": "opus", "preferredquality": "128"}],
            noplaylist=True,
            extract_flat=False,
            socket_timeout=60,
            retries=3,
            overwrites=True,
            max_filesize=AUDIO_CACHE_MAX_FILESIZE,
            progress_hooks=[stop_if_cancelled],
        )

        def do_download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(webpage_url, download=True)

        completed = False
        try:
            for stale in self._temp_files(base):
                self._unlink(stale)
            await asyncio.get_running_loop().run_in_executor(self.bot.download_executor, do_download)
            produced = [p for p in self._temp_files(base) if not p.name.endswith((".part", ".ytdl"))]
            if len(produced) != 1:
                # No file: over max_filesize or skipped by yt-dlp
                log = logger.warning if produced else logger.info
                log(f"Cache download of '{song_title}' left {len(produced)} files; not caching it")
                return
            os.replace(produced[0], cache_path)
            completed = True
            logger.info(f"Audio cached: {song_title} -> {cache_path}")
        except asyncio.CancelledError:
            cancel.set()
            raise
        except (yt_dlp.utils.YoutubeDLError, OSError) as e:
            logger.warning(f"Audio cache download failed for '{song_title}': {e}")
        finally:
            if not completed:
                for partial in self._temp_files(base):
                    self._unlink(partial)
            async with self._get_lock():
                if self._download_tasks.get(webpage_url) is asyncio.current_task():
                    self._download_tasks.pop(webpage_url, None)
                    self._cancel_events.pop(webpage_url, None)

    def remove_cached(self, webpage_url: str):
        path = self.get_cache_path(webpage_url)
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.debug(f"Removed cached file: {path}")
        except OSError as e:
            logger.warning(f"Failed to remove cached file {path}: {e}")

    async def startup_cleanup(self):
        junk_extensions = {".part", ".temp", ".ytdl", ".webm", ".m4a", ".mp3", ".mp4", ".ogg"}
        removed = 0
        try:
            for f in self.cache_dir.iterdir():
                if f.is_file() and (f.suffix in junk_extensions or TEMP_MARK in f.name):
                    removed += self._unlink(f)
            # The Activity cache keeps its files in a subfolder; only its unfinished downloads are removed.
            activity_dir = self.cache_dir / "activity"
            if activity_dir.is_dir():
                for f in activity_dir.iterdir():
                    if f.is_file() and (TEMP_MARK in f.name or f.suffix in {".part", ".ytdl", ".temp"}):
                        removed += self._unlink(f)
            if removed:
                logger.info(f"Startup cleanup: removed {removed} partial/intermediate files from audio cache")
        except OSError as e:
            logger.error(f"Audio cache startup cleanup error: {e}")

    @staticmethod
    def _unlink(path: Path) -> int:
        try:
            path.unlink()
            return 1
        except OSError:
            return 0

    async def cancel_all_downloads(self):
        async with self._get_lock():
            for event in self._cancel_events.values():
                event.set()
            for task in self._download_tasks.values():
                if not task.done():
                    task.cancel()
            self._download_tasks.clear()
            self._cancel_events.clear()
