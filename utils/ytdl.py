import asyncio
import io
import logging
import os
import time

logger = logging.getLogger("yt_dlp")

# Removed from every option set: they hide errors, disable TLS checks or pin a stale browser identity.
_DROPPED_OPTIONS = ("no_warnings", "nocheckcertificate", "http_headers")

# (path, mtime) -> file text, so the cookie file is read again only after it changes.
_cookie_cache: dict[tuple[str, float], str] = {}
_warned_paths: set[str] = set()


class YtdlLogger:
    """Routes yt-dlp output through logging instead of raw stderr."""

    def debug(self, msg: str) -> None:
        logger.debug(msg)

    def info(self, msg: str) -> None:
        logger.debug(msg)

    def warning(self, msg: str) -> None:
        logger.warning(msg)

    def error(self, msg: str) -> None:
        logger.error(msg)


YTDL_LOGGER = YtdlLogger()


def _cookie_file() -> io.StringIO | None:
    path = os.getenv("YTDLP_COOKIES")
    if not path:
        return None
    try:
        key = (path, os.path.getmtime(path))
        if key not in _cookie_cache:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            _cookie_cache.clear()
            _cookie_cache[key] = text
    except OSError as e:
        if path not in _warned_paths:
            _warned_paths.add(path)
            logger.warning("YTDLP_COOKIES is set but %s could not be read: %s", path, e)
        return None
    return io.StringIO(_cookie_cache[key])


def ytdl_options(**overrides) -> dict:
    """A fresh yt-dlp option dict for one YoutubeDL instance."""
    options = {"ignoreerrors": False, "logger": YTDL_LOGGER, "quiet": True, **overrides}
    for key in _DROPPED_OPTIONS:
        options.pop(key, None)
    # yt-dlp writes cookies back on close, so each instance gets its own in-memory copy.
    if "cookiefile" not in options and (cookies := _cookie_file()):
        options["cookiefile"] = cookies
    return options


# Longest wait for YouTube's mandatory preroll delay (a format's available_at) before the first request
MAX_AVAILABLE_AT_WAIT = 60


def available_at(info: dict) -> float:
    """When YouTube lets the extracted formats be fetched; 0 when they can be at once."""
    formats = info.get("requested_formats") or [info]
    return max((f.get("available_at") or 0) for f in formats)


async def wait_until_available(at: float) -> None:
    """YouTube refuses (403) a format fetched before its preroll wait ends."""
    wait = at - time.time()
    if wait > 0:
        await asyncio.sleep(min(wait, MAX_AVAILABLE_AT_WAIT))
