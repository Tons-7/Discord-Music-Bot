"""Free lyrics fetching via lrclib.net (no API key required)."""

import asyncio
import logging
import re
from typing import Optional, Dict

import aiohttp

from config import LYRICS_API_BASE

logger = logging.getLogger(__name__)

# Patterns stripped from YouTube titles before searching lyrics
_TITLE_NOISE = re.compile(
    r'\s*[\(\[](official(\s*(music\s*)?video|[\s\w]*audio)?|lyric\s*video|lyrics|hd|hq|4k|mv|visuali[sz]er|remaster(ed)?)[\)\]]',
    re.IGNORECASE,
)
_VEVO_SUFFIX = re.compile(r'VEVO$', re.IGNORECASE)
_TOPIC_SUFFIX = re.compile(r'\s*[-\u2013]\s*Topic$', re.IGNORECASE)


def _clean_title(title: str) -> str:
    cleaned = _TITLE_NOISE.sub("", title).strip()
    return cleaned or title


def _clean_artist(uploader: str) -> str:
    artist = _VEVO_SUFFIX.sub("", uploader).strip()
    artist = _TOPIC_SUFFIX.sub("", artist).strip()
    artist = re.sub(r'\s*Official$', '', artist, flags=re.IGNORECASE).strip()
    return artist or uploader


def _split_artist_title(raw_title: str) -> tuple[str, str]:
    """Try to split 'Artist - Song Title' patterns."""
    for sep in [' - ', ' \u2013 ', ' \u2014 ']:
        if sep in raw_title:
            parts = raw_title.split(sep, 1)
            return parts[0].strip(), _clean_title(parts[1].strip())
    return "", _clean_title(raw_title)


async def fetch_lyrics(title: str, uploader: str = "") -> Optional[Dict]:
    """Fetch lyrics from lrclib.net.

    Returns dict with keys: lyrics, synced, title, artist  — or None.
    """
    parsed_artist, parsed_title = _split_artist_title(title)
    clean_t = _clean_title(parsed_title)
    clean_a = parsed_artist or _clean_artist(uploader)

    timeout = aiohttp.ClientTimeout(total=10)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # 1) Exact match
            if clean_a:
                params = {"track_name": clean_t, "artist_name": clean_a}
                async with session.get(f"{LYRICS_API_BASE}/get", params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("plainLyrics") or data.get("syncedLyrics"):
                            return _format(data, clean_t, clean_a)

            # 2) Fallback: keyword search
            query = f"{clean_t} {clean_a}".strip()
            async with session.get(f"{LYRICS_API_BASE}/search", params={"q": query}) as resp:
                if resp.status == 200:
                    results = await resp.json()
                    if results:
                        best = results[0]
                        if best.get("plainLyrics") or best.get("syncedLyrics"):
                            return _format(best, clean_t, clean_a)

    except asyncio.TimeoutError:
        logger.debug("Lyrics request timed out")
    except Exception as e:
        logger.warning(f"Lyrics fetch failed: {e}")

    return None


def _format(data: dict, fallback_title: str, fallback_artist: str) -> Dict:
    return {
        "lyrics": data.get("plainLyrics", ""),
        "synced": data.get("syncedLyrics", ""),
        "title": data.get("trackName") or fallback_title,
        "artist": data.get("artistName") or fallback_artist,
    }
