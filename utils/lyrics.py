"""Free lyrics fetching via lrclib.net (no API key required)."""

import asyncio
import logging
import re
from typing import Optional, Dict

import aiohttp

from config import LYRICS_API_BASE

logger = logging.getLogger(__name__)

# --- Title / artist cleaning patterns ---

# Matches parenthesized/bracketed YouTube noise: (Official Video), [HD], (Audio Stream), etc.
# Uses [^)\]]* to stay within a single paren/bracket pair.
_TITLE_NOISE = re.compile(
    r'\s*[\(\[]'
    r'(?:'
    r'official[^)\]]*'  # (Official ..anything..)
    r'|[^)\]]*\b(?:music\s+)?video\b[^)\]]*'  # (..music video..), (..video..)
    r'|[^)\]]*\baudio\b[^)\]]*'  # (..audio..), (..audio stream..)
    r'|[^)\]]*\blyrics?\b[^)\]]*'  # (..lyrics..), (..lyric video..)
    r'|(?:hd|hq|4k|mv)'  # (HD), (HQ), (4K), (MV)
    r'|visuali[sz]er'  # (Visualizer)
    r'|remaster(?:ed)?(?:\s+\d{4})?'  # (Remastered), (Remastered 2023)
    r'|(?:short\s+)?film'  # (Short Film), (Film)
    r'|extended(?:\s+(?:mix|version|edit|remix))?'  # (Extended), (Extended Mix), etc.
    r')'
    r'[\)\]]',
    re.IGNORECASE,
)

# Last-resort: strip ALL parenthesized/bracketed content
_ALL_PARENS = re.compile(r'\s*[\(\[][^)\]]*[\)\]]')

_VEVO_SUFFIX = re.compile(r'VEVO$', re.IGNORECASE)
_TOPIC_SUFFIX = re.compile(r'\s*[-\u2013]\s*Topic$', re.IGNORECASE)
_CJK_BRACKETS = re.compile(r'[「『【〔][^」』】〕]*[」』】〕]')


_EXTENDED_BARE = re.compile(
    r'\s+extended(?:\s+(?:mix|version|edit|remix))?\s*$',
    re.IGNORECASE,
)


def _clean_title(title: str) -> str:
    """Strip known YouTube noise from a title."""
    cleaned = _TITLE_NOISE.sub("", title).strip()
    cleaned = _EXTENDED_BARE.sub("", cleaned).strip()
    return cleaned or title


def _clean_artist(uploader: str) -> str:
    """Normalize a YouTube uploader name to a bare artist name."""
    artist = _VEVO_SUFFIX.sub("", uploader).strip()
    artist = _TOPIC_SUFFIX.sub("", artist).strip()
    artist = re.sub(r'\s*Official$', '', artist, flags=re.IGNORECASE).strip()
    return artist or uploader


def _name_match(a: str, b: str) -> bool:
    """Case/space-insensitive substring name matching."""
    a, b = a.lower().replace(" ", ""), b.lower().replace(" ", "")
    return bool(a and b and (a == b or a in b or b in a))


def _split_artist_title(raw_title: str, uploader: str = "") -> tuple[str, str]:
    """Split 'Artist - Song Title' or 'Song Title - Artist' patterns.

    Uses the uploader name to disambiguate which side is the artist
    when the title contains a dash separator.
    """
    stripped = _CJK_BRACKETS.sub('', raw_title)
    stripped = re.sub(r'\s+', ' ', stripped).strip() or raw_title

    for sep in [' - ', ' \u2013 ', ' \u2014 ']:
        if sep in stripped:
            left, right = stripped.split(sep, 1)
            left, right = left.strip(), right.strip()

            if uploader:
                clean_up = _clean_artist(uploader)
                if _name_match(clean_up, right):
                    return right, _clean_title(left)
                if _name_match(clean_up, left):
                    return left, _clean_title(right)

            # Default: assume "Artist - Title"
            return left, _clean_title(right)

    return "", _clean_title(stripped)


# --- Relevance filtering ---

def _normalize(text: str) -> str:
    """Lowercase and strip non-alphanumeric chars for fuzzy comparison."""
    return re.sub(r'[^\w\s]', '', text.lower()).strip()


def _is_relevant(result: dict, search_title: str) -> bool:
    """Check if a search result's title is plausibly related to the query."""
    s_title = _normalize(search_title)
    r_title = _normalize(result.get("trackName", ""))

    if not s_title or not r_title:
        return True

    if s_title in r_title or r_title in s_title:
        return True

    # Word overlap - at least half the search words should appear
    s_words = set(s_title.split())
    r_words = set(r_title.split())
    overlap = len(s_words & r_words)
    return overlap >= max(1, len(s_words) // 2)


def _has_lyrics(result: dict) -> bool:
    return bool(result.get("plainLyrics") or result.get("syncedLyrics"))


def _first_match(results: list, search_title: str) -> Optional[dict]:
    """Return the first result that has lyrics and is relevant to the search title."""
    for r in results:
        if _has_lyrics(r) and _is_relevant(r, search_title):
            return r
    return None


# --- Main fetch logic ---

async def _search(session: aiohttp.ClientSession, query: str, title_hint: str) -> Optional[dict]:
    """Run a keyword search against lrclib and return the first relevant hit."""
    async with session.get(f"{LYRICS_API_BASE}/search", params={"q": query}) as resp:
        if resp.status == 200:
            return _first_match(await resp.json(), title_hint)
    return None


async def fetch_lyrics(title: str, uploader: str = "") -> Optional[Dict]:
    """Fetch lyrics from lrclib.net.

    Tries progressively looser searches:
      1. Exact /get by track and artist
      2. Keyword search: cleaned title and artist
      3. Keyword search: cleaned title only
      4. Swapped search: uploader as artist and left-of-dash as title
         (handles "SongTitle - description" where uploader is the real artist)
      5. Aggressive fallback: strip parenthesized content and retry

    Returns dict with keys: lyrics, synced, title, artist, or None.
    """
    parsed_artist, parsed_title = _split_artist_title(title, uploader)
    clean_t = _clean_title(parsed_title)
    clean_a = parsed_artist or _clean_artist(uploader)

    logger.debug(f"Lyrics search: title='{clean_t}', artist='{clean_a}' "
                 f"(raw='{title}', uploader='{uploader}')")

    timeout = aiohttp.ClientTimeout(total=10)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # 1) Exact match by track + artist
            if clean_a:
                params = {"track_name": clean_t, "artist_name": clean_a}
                async with session.get(f"{LYRICS_API_BASE}/get", params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if _has_lyrics(data):
                            return _format(data, clean_t, clean_a)

            # 2) Keyword search: title + artist
            query = f"{clean_t} {clean_a}".strip()
            hit = await _search(session, query, clean_t)
            if hit:
                return _format(hit, clean_t, clean_a)

            # 3) Title-only search (covers channels, game OSTs, etc.)
            if clean_a and clean_t != query:
                hit = await _search(session, clean_t, clean_t)
                if hit:
                    return _format(hit, clean_t, clean_a)

            # 4) Swapped search: when the uploader doesn't match the parsed artist,
            #    the "Artist - Title" split may be backwards (e.g. "Song - description").
            #    Try uploader as artist + left-of-dash (parsed_artist) as a title.
            uploader_artist = _clean_artist(uploader) if uploader else ""
            if uploader_artist and parsed_artist and not _name_match(uploader_artist, clean_a):
                alt_t = _clean_title(parsed_artist)
                hit = await _search(session, f"{alt_t} {uploader_artist}", alt_t)
                if hit:
                    return _format(hit, alt_t, uploader_artist)

            # 5) Aggressively strip ALL parenthesized/bracketed content and retry
            bare_t = _ALL_PARENS.sub('', clean_t).strip()
            if bare_t and bare_t != clean_t:
                hit = await _search(session, f"{bare_t} {clean_a}".strip(), bare_t)
                if hit:
                    return _format(hit, clean_t, clean_a)

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
