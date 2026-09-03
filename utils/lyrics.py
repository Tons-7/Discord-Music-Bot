"""Free lyrics fetching via lrclib.net (no API key required)."""

import asyncio
import logging
import re
from typing import Optional, Dict

import aiohttp

from config import LRCLIB_USER_AGENT, LYRICS_API_BASE

logger = logging.getLogger(__name__)


class LyricsServiceUnavailable(RuntimeError):
    """Raised when LRCLIB cannot serve a request."""


class _TransientStatusError(LyricsServiceUnavailable):
    """A 429/5xx on one request — other lookup steps may still succeed."""

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


def _similarity(a: str, b: str) -> float:
    """Fuzzy 0..1 name similarity."""
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if _name_match(na, nb):
        return 0.85
    wa, wb = set(na.split()), set(nb.split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


# Scoring weights. Duration is the strongest signal: LRCLIB happily returns
# covers, live cuts and karaoke masters under the same title/artist, and only
# the runtime separates them.
_W_TITLE = 40.0
_W_ARTIST = 40.0
_DURATION_EXACT = 50.0          # within _DURATION_TOLERANCE seconds
_DURATION_TOLERANCE = 2.0
_DURATION_LIMIT = 15.0          # beyond this it is almost certainly another master
_DURATION_PENALTY = -30.0
_W_SYNCED = 25.0

# A title-only match with nothing else known must still clear this, so the floor
# sits at the bare title score. A wrong track is rejected by _disagrees instead.
_MIN_SCORE = 35.0


def _duration_score(result_duration, want_duration: float) -> float:
    """Score the runtime delta; 0 when either side is unknown."""
    try:
        rd = float(result_duration or 0)
    except (TypeError, ValueError):
        return 0.0
    if rd <= 0 or want_duration <= 0:
        return 0.0

    delta = abs(rd - want_duration)
    if delta <= _DURATION_TOLERANCE:
        return _DURATION_EXACT
    if delta <= _DURATION_LIMIT:
        span = _DURATION_LIMIT - _DURATION_TOLERANCE
        return _DURATION_EXACT * (1 - (delta - _DURATION_TOLERANCE) / span)
    return _DURATION_PENALTY


def _disagrees(result: dict, want_artist: str, want_duration: float, artist_confident: bool) -> bool:
    """True when everything checkable contradicts the title match.

    Only applied to an artist parsed out of "Artist - Song"; one guessed from
    the uploader proves nothing, since aggregator and OST channels upload under
    their own name.
    """
    if not (artist_confident and want_artist and want_duration > 0):
        return False
    if _similarity(result.get("artistName", ""), want_artist) >= 0.3:
        return False
    try:
        rd = float(result.get("duration") or 0)
    except (TypeError, ValueError):
        return False
    return rd > 0 and abs(rd - want_duration) > _DURATION_LIMIT


def _score_candidate(
    result: dict,
    want_title: str,
    want_artist: str,
    want_duration: float,
    artist_confident: bool = True,
) -> float:
    """Rank one LRCLIB result against the song we actually want."""
    score = _similarity(result.get("trackName", ""), want_title) * _W_TITLE
    if want_artist and artist_confident:
        score += _similarity(result.get("artistName", ""), want_artist) * _W_ARTIST
    score += _duration_score(result.get("duration"), want_duration)
    if result.get("syncedLyrics"):
        score += _W_SYNCED
    return score


def _best_match(
    results: list,
    want_title: str,
    want_artist: str = "",
    want_duration: float = 0,
    artist_confident: bool = True,
) -> Optional[dict]:
    """Return the highest-scoring usable result, or None if none clears the floor."""
    best, best_score = None, float("-inf")
    for r in results:
        if not _has_lyrics(r) or not _is_relevant(r, want_title):
            continue
        if _disagrees(r, want_artist, want_duration, artist_confident):
            continue
        score = _score_candidate(r, want_title, want_artist, want_duration, artist_confident)
        if score > best_score:
            best, best_score = r, score

    if best is None or best_score < _MIN_SCORE:
        logger.debug("Lyrics: no candidate cleared the score floor for '%s'", want_title)
        return None

    logger.debug(
        "Lyrics match: '%s' by '%s' (score %.1f)",
        best.get("trackName"), best.get("artistName"), best_score,
    )
    return best


# --- Main fetch logic ---

async def _get_json(
    session: aiohttp.ClientSession, endpoint: str, params: dict
) -> Optional[dict | list]:
    """Request LRCLIB JSON while preserving unavailable-service failures."""
    try:
        async with session.get(f"{LYRICS_API_BASE}{endpoint}", params=params) as resp:
            if resp.status == 200:
                return await resp.json()
            if resp.status in {429, 500, 502, 503, 504}:
                raise _TransientStatusError(f"LRCLIB returned HTTP {resp.status}")
            logger.debug("LRCLIB %s returned HTTP %s", endpoint, resp.status)
            return None
    except asyncio.TimeoutError as exc:
        raise LyricsServiceUnavailable("LRCLIB request timed out") from exc
    except aiohttp.ClientError as exc:
        raise LyricsServiceUnavailable("LRCLIB connection failed") from exc


async def _search(
    session: aiohttp.ClientSession,
    title: str,
    artist: str = "",
    album: str = "",
    duration: float = 0,
    want_artist: Optional[str] = None,
    artist_confident: bool = True,
) -> Optional[dict]:
    """Search LRCLIB with its structured title, artist, and album fields.

    ``want_artist`` scores results against a known artist even when the query
    itself omits it (the title-only step deliberately widens the search).
    """
    params = {"track_name": title}
    if artist:
        params["artist_name"] = artist
    if album:
        params["album_name"] = album

    data = await _get_json(session, "/search", params)
    if not isinstance(data, list):
        return None
    return _best_match(
        data, title, artist if want_artist is None else want_artist,
        duration, artist_confident,
    )


async def fetch_lyrics(
    title: str,
    uploader: str = "",
    duration: float = 0,
    album: str = "",
) -> Optional[Dict]:
    """Fetch lyrics from lrclib.net.

    Uses LRCLIB's required client identifier and tries progressively looser searches:
      1. Exact /get only when title, artist, album, and duration are known
      2. Structured search: cleaned title and artist
      3. Structured title-only search
      4. Swapped search: uploader as artist and left-of-dash as title
         (handles "SongTitle - description" where uploader is the real artist)
      5. Aggressive fallback: strip parenthesized content and retry

    Returns dict with keys: lyrics, synced, title, artist, or None.
    """
    parsed_artist, parsed_title = _split_artist_title(title, uploader)
    # An artist parsed from "Artist - Song" is reliable; one guessed from the
    # uploader is not (aggregator/OST channels upload under their own name).
    artist_confident = bool(parsed_artist)
    clean_t = _clean_title(parsed_title)
    clean_a = parsed_artist or _clean_artist(uploader)
    clean_album = (album or "").strip()
    try:
        numeric_duration = float(duration)
    except (TypeError, ValueError):
        numeric_duration = 0
    valid_duration = numeric_duration if 1 <= numeric_duration <= 3600 else 0

    logger.debug(f"Lyrics search: title='{clean_t}', artist='{clean_a}' "
                 f"(raw='{title}', uploader='{uploader}')")

    timeout = aiohttp.ClientTimeout(total=10)
    headers = {"Accept": "application/json", "User-Agent": LRCLIB_USER_AGENT}

    transient_errors: list[LyricsServiceUnavailable] = []

    async def attempt(coro):
        """Run one lookup step; a transient LRCLIB status skips to the next step."""
        try:
            return await coro
        except _TransientStatusError as exc:
            transient_errors.append(exc)
            return None

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            # LRCLIB requires all four fields for an exact metadata lookup.
            if clean_a and clean_album and valid_duration:
                params = {
                    "track_name": clean_t,
                    "artist_name": clean_a,
                    "album_name": clean_album,
                    "duration": int(valid_duration),
                }
                data = await attempt(_get_json(session, "/get", params))
                if isinstance(data, dict) and _has_lyrics(data):
                    return _format(data, clean_t, clean_a)

            # 2) Structured search: title + artist
            hit = await attempt(_search(
                session, clean_t, clean_a, clean_album, valid_duration,
                artist_confident=artist_confident,
            ))
            if hit:
                return _format(hit, clean_t, clean_a)

            # 3) Title-only search (covers channels, game OSTs, etc.)
            if clean_a:
                hit = await attempt(
                    _search(
                        session, clean_t, duration=valid_duration, want_artist=clean_a,
                        artist_confident=artist_confident,
                    )
                )
                if hit:
                    return _format(hit, clean_t, clean_a)

            # 4) Swapped search: when the uploader doesn't match the parsed artist,
            #    the "Artist - Title" split may be backwards (e.g. "Song - description").
            #    Try uploader as artist + left-of-dash (parsed_artist) as a title.
            uploader_artist = _clean_artist(uploader) if uploader else ""
            if uploader_artist and parsed_artist and not _name_match(uploader_artist, clean_a):
                alt_t = _clean_title(parsed_artist)
                hit = await attempt(
                    _search(session, alt_t, uploader_artist, clean_album, valid_duration)
                )
                if hit:
                    return _format(hit, alt_t, uploader_artist)

            # 5) Aggressively strip ALL parenthesized/bracketed content and retry
            bare_t = _ALL_PARENS.sub('', clean_t).strip()
            if bare_t and bare_t != clean_t:
                hit = await attempt(
                    _search(
                        session, bare_t, clean_a, clean_album, valid_duration,
                        artist_confident=artist_confident,
                    )
                )
                if hit:
                    return _format(hit, clean_t, clean_a)

    except LyricsServiceUnavailable:
        raise
    except Exception as exc:
        logger.warning("Lyrics fetch failed: %s", exc)

    # Nothing found and at least one step failed on a transient LRCLIB status:
    # surface the outage instead of a misleading "not found".
    if transient_errors:
        raise transient_errors[0]

    return None


_LRC_TIMESTAMP = re.compile(r'\[\d{1,2}:\d{2}(?:[.:]\d{1,3})?\]\s*')


def strip_lrc_timestamps(synced: str) -> str:
    """Convert synced LRC text to plain lyrics by removing timestamp tags."""
    lines = [_LRC_TIMESTAMP.sub('', line).rstrip() for line in synced.splitlines()]
    return "\n".join(lines).strip()


def _format(data: dict, fallback_title: str, fallback_artist: str) -> Dict:
    return {
        "lyrics": data.get("plainLyrics", ""),
        "synced": data.get("syncedLyrics", ""),
        "title": data.get("trackName") or fallback_title,
        "artist": data.get("artistName") or fallback_artist,
    }
