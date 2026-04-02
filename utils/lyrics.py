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


_CJK_BRACKETS = re.compile(r'「[^」]*」|【[^】]*】|『[^』]*』|〔[^〕]*〕')


def _name_match(a: str, b: str) -> bool:
    """Case/space-insensitive name matching"""
    a, b = a.lower().replace(" ", ""), b.lower().replace(" ", "")
    return bool(a and b and (a == b or a in b or b in a))


def _split_artist_title(raw_title: str, uploader: str = "") -> tuple[str, str]:
    """Split 'Artist - Song Title' or 'Song Title - Artist' patterns.

    Uses the uploader name to disambiguate which side is the artist when the title contains a dash separator.
    """
    # Strip CJK bracket content so embedded dashes don't act as separators
    stripped = _CJK_BRACKETS.sub('', raw_title)
    stripped = re.sub(r'\s+', ' ', stripped).strip() or raw_title

    for sep in [' - ', ' \u2013 ', ' \u2014 ']:
        if sep in stripped:
            parts = stripped.split(sep, 1)
            left, right = parts[0].strip(), parts[1].strip()

            if uploader:
                clean_up = _clean_artist(uploader)
                if _name_match(clean_up, right):
                    return right, _clean_title(left)
                if _name_match(clean_up, left):
                    return left, _clean_title(right)

            # Default: assume "Artist - Title"
            return left, _clean_title(right)
    return "", _clean_title(stripped)


def _normalize(text: str) -> str:
    """Lowercase and strip non-alphanumeric chars for fuzzy comparison."""
    return re.sub(r'[^\w\s]', '', text.lower()).strip()


def _is_relevant(result: dict, search_title: str) -> bool:
    """Check if a search result's title is plausibly related to the query."""
    s_title = _normalize(search_title)
    r_title = _normalize(result.get("trackName", ""))

    if not s_title or not r_title:
        return True  # Can't validate, allow

    # Substring containment
    if s_title in r_title or r_title in s_title:
        return True

    # Word overlap - at least half the search words should appear
    s_words = set(s_title.split())
    r_words = set(r_title.split())
    overlap = len(s_words & r_words)
    return overlap >= max(1, len(s_words) // 2)


async def fetch_lyrics(title: str, uploader: str = "") -> Optional[Dict]:
    """Fetch lyrics from lrclib.net.

    Returns dict with keys: lyrics, synced, title, artist, or None.
    """
    parsed_artist, parsed_title = _split_artist_title(title, uploader)
    clean_t = _clean_title(parsed_title)
    clean_a = parsed_artist or _clean_artist(uploader)

    logger.debug(f"Lyrics search: title='{clean_t}', artist='{clean_a}' (raw='{title}', uploader='{uploader}')")

    timeout = aiohttp.ClientTimeout(total=10)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # 1) Exact match by track + artist
            if clean_a:
                params = {"track_name": clean_t, "artist_name": clean_a}
                async with session.get(f"{LYRICS_API_BASE}/get", params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("plainLyrics") or data.get("syncedLyrics"):
                            return _format(data, clean_t, clean_a)

            # 2) Keyword search — validate results instead of blindly taking first
            query = f"{clean_t} {clean_a}".strip()
            async with session.get(f"{LYRICS_API_BASE}/search", params={"q": query}) as resp:
                if resp.status == 200:
                    results = await resp.json()
                    for r in results:
                        if (r.get("plainLyrics") or r.get("syncedLyrics")) and _is_relevant(r, clean_t):
                            return _format(r, clean_t, clean_a)

            # 3) Title-only search (helps when artist is a cover channel or game OST)
            if clean_a and clean_t != query:
                async with session.get(f"{LYRICS_API_BASE}/search", params={"q": clean_t}) as resp:
                    if resp.status == 200:
                        results = await resp.json()
                        for r in results:
                            if (r.get("plainLyrics") or r.get("syncedLyrics")) and _is_relevant(r, clean_t):
                                return _format(r, clean_t, clean_a)

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
