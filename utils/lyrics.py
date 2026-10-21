"""Free lyrics fetching via lrclib.net (no API key required)."""

import asyncio
import logging
import os
import random
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

import aiohttp

from config import LRCLIB_USER_AGENT, LYRICS_API_BASE
from utils import titles

logger = logging.getLogger(__name__)


class LyricsServiceUnavailable(RuntimeError):
    """Raised when LRCLIB cannot serve a request."""


class _TransientStatusError(LyricsServiceUnavailable):
    """A 5xx on one request — other lookup steps may still succeed."""


class LyricsRateLimited(LyricsServiceUnavailable):
    """LRCLIB asked us to wait (429/503 with Retry-After); no request is sent until then."""

# --- Title / artist cleaning patterns ---

# Matches parenthesized/bracketed YouTube noise: (Official Video), [HD], (Audio Stream), etc.
# Uses [^)\]]* to stay within a single paren/bracket pair.
_TITLE_NOISE = re.compile(
    r'\s*[\(\[（]'
    r'(?:'
    r'official[^)\]）]*'  # (Official ..anything..)
    r'|[^)\]）]*\b(?:music\s+)?video\b[^)\]）]*'  # (..music video..), (..video..)
    r'|[^)\]）]*\baudio\b[^)\]）]*'  # (..audio..), (..audio stream..)
    r'|[^)\]）]*\blyrics?\b[^)\]）]*'  # (..lyrics..), (..lyric video..)
    r'|(?:hd|hq|4k|mv|m/v)'  # (HD), (HQ), (4K), (MV)
    r'|visuali[sz]er'  # (Visualizer)
    r'|remaster(?:ed)?(?:\s+\d{4})?'  # (Remastered), (Remastered 2023)
    r'|(?:short\s+)?film'  # (Short Film), (Film)
    r'|extended(?:\s+(?:mix|version|edit|remix))?'  # (Extended), (Extended Mix), etc.
    r')'
    r'[\)\]）]',
    re.IGNORECASE,
)

# Last-resort: strip ALL parenthesized/bracketed content
_ALL_PARENS = re.compile(r'\s*[\(\[（][^)\]）]*[\)\]）]')

_EXTENDED_BARE = re.compile(
    r'\s+extended(?:\s+(?:mix|version|edit|remix))?\s*$',
    re.IGNORECASE,
)

# Uploads whose tempo or arrangement differs from the studio master the LRC was timed against.
_TEMPO_TAGS = re.compile(
    r'\b(?:sped[\s-]?up|speed[\s-]?up|slowed|nightcore|daycore|reverb|\d(?:\.\d+)?x\s*speed|live)\b',
    re.IGNORECASE,
)
_LRC_TIME = re.compile(r'\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]')


def _clean_title(title: str) -> str:
    """Strip known YouTube noise from a title."""
    cleaned = _TITLE_NOISE.sub("", title).strip()
    cleaned = _EXTENDED_BARE.sub("", cleaned).strip()
    cleaned = titles.strip_trailing_noise(titles.unquote(cleaned))
    return cleaned or title


def _clean_artist(uploader: str) -> str:
    """Normalize a YouTube uploader name to a bare artist name."""
    return titles.clean_uploader(uploader)


def _name_match(a: str, b: str) -> bool:
    """Case/space-insensitive substring name matching."""
    return titles.name_match(a, b)


@dataclass
class _Parsed:
    artist: str
    title: str
    # 'Title - Artist' reading of a title that was split as 'Artist - Title' only by default.
    swapped: tuple[str, str] | None = None


def _parse_title(raw_title: str, uploader: str = "") -> _Parsed:
    stripped = titles.strip_tag_brackets(raw_title or "") or raw_title or ""
    clean_up = _clean_artist(uploader) if uploader else ""

    for separators in (titles.DASHES, titles.PIPES):
        sep = next((s for s in separators if s in stripped), None)
        if not sep:
            continue
        left, right = (side.strip() for side in stripped.split(sep, 1))
        if separators is titles.DASHES:
            # 'Artist - Song | Live Session': whatever follows a pipe is channel or series info
            right = re.split(r'\s+(?:\||//|｜)\s+', right)[0].strip()
        if not left or not right:
            continue

        # The uploader is compared with each side's bare name, so a remixer, cover artist or
        # featured artist named in brackets or a feat credit does not flip the split.
        if clean_up and _name_match(clean_up, titles.core(right)):
            return _Parsed(titles.clean_artist_side(right), _clean_title(left))
        if clean_up and _name_match(clean_up, titles.core(left)):
            return _Parsed(titles.clean_artist_side(left), _clean_title(right))
        return _Parsed(
            titles.clean_artist_side(left), _clean_title(right),
            swapped=(titles.clean_artist_side(right), _clean_title(left)),
        )

    if quoted := titles.split_quoted(stripped):
        artist, song = quoted
        return _Parsed(artist, _clean_title(song))

    return _Parsed("", _clean_title(stripped))


def _split_artist_title(raw_title: str, uploader: str = "") -> tuple[str, str]:
    """Split 'Artist - Song', 'Song - Artist', 'Song | Artist', 'ARTIST "Song" M/V' or 'Artist「Song」'.

    Uses the uploader name to disambiguate which side is the artist
    when the title contains a separator.
    """
    parsed = _parse_title(raw_title, uploader)
    return parsed.artist, parsed.title


def _bare_title(title: str) -> str:
    """Last resort: no brackets, feat credit or trailing labels at all."""
    return titles.strip_trailing_noise(titles.strip_feat(_ALL_PARENS.sub('', title).strip())).strip()


# --- Relevance filtering ---

def _normalize(text: str) -> str:
    """Lowercase and strip non-alphanumeric chars for fuzzy comparison."""
    return re.sub(r'[^\w\s]', '', text.lower()).strip()


def _is_relevant(result: dict, search_title: str) -> bool:
    """Check if a search result's title is plausibly related to the query."""
    s_title = _normalize(search_title)
    r_title = _normalize(result.get("trackName") or "")

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
    na, nb = _normalize(a or ""), _normalize(b or "")
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
# An instrumental record is only believed when its artist matches this well.
_INSTRUMENTAL_ARTIST = 0.5


def _seconds(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _duration_score(result_duration, want_duration: float) -> float:
    """Score the runtime delta; 0 when either side is unknown."""
    rd = _seconds(result_duration)
    if rd <= 0 or want_duration <= 0:
        return 0.0

    delta = abs(rd - want_duration)
    if delta <= _DURATION_TOLERANCE:
        return _DURATION_EXACT
    if delta <= _DURATION_LIMIT:
        span = _DURATION_LIMIT - _DURATION_TOLERANCE
        return _DURATION_EXACT * (1 - (delta - _DURATION_TOLERANCE) / span)
    return _DURATION_PENALTY


def _disagrees(result: dict, want_artist: str, want_duration: float, artist_confident: bool = True) -> bool:
    """True when both the artist and the runtime contradict the title match.

    A guessed artist (an aggregator or OST channel's own name) may well differ from the record's,
    so the artist alone never rejects; with the runtime also off by more than _DURATION_LIMIT it
    is another song that only shares the title.
    """
    if not (want_artist and want_duration > 0):
        return False
    if _similarity(result.get("artistName") or "", want_artist) >= 0.3:
        return False
    rd = _seconds(result.get("duration"))
    return rd > 0 and abs(rd - want_duration) > _DURATION_LIMIT


def _last_lrc_second(synced: str) -> float:
    last = 0.0
    for minutes, seconds, fraction in _LRC_TIME.findall(synced or ""):
        last = max(last, int(minutes) * 60 + int(seconds) + float(f"0.{fraction or 0}"))
    return last


def _synced_out_of_time(result: dict, want_duration: float, raw_title: str = "") -> bool:
    """True when the record's timestamps cannot follow this upload.

    Either its lines run past the end of the upload, or the upload is tagged as a different
    tempo or a live cut while the record runs for a different length. A constant offset (a
    music video's intro) still lines up, so a longer upload alone keeps its synced lyrics.
    """
    synced = result.get("syncedLyrics") or ""
    if not synced or want_duration <= 0:
        return False
    if _last_lrc_second(synced) > want_duration + 5:
        return True
    tag = _TEMPO_TAGS.search(raw_title or "")
    if not tag or re.search(re.escape(tag.group(0)), result.get("trackName") or "", re.IGNORECASE):
        return False
    rd = _seconds(result.get("duration"))
    return rd > 0 and abs(rd - want_duration) > _DURATION_LIMIT


def _score_candidate(
    result: dict,
    want_title: str,
    want_artist: str,
    want_duration: float,
    artist_confident: bool = True,
    raw_title: str = "",
) -> float:
    """Rank one LRCLIB result against the song we actually want."""
    score = _similarity(result.get("trackName") or "", want_title) * _W_TITLE
    if want_artist and artist_confident:
        score += _similarity(result.get("artistName") or "", want_artist) * _W_ARTIST
    score += _duration_score(result.get("duration"), want_duration)
    if result.get("syncedLyrics") and not _synced_out_of_time(result, want_duration, raw_title):
        score += _W_SYNCED
    return score


def _best_match(
    results: list,
    want_title: str,
    want_artist: str = "",
    want_duration: float = 0,
    artist_confident: bool = True,
    raw_title: str = "",
) -> dict | None:
    """Return the highest-scoring usable result, or None if none clears the floor."""
    best, best_score = None, float("-inf")
    for r in results:
        if not isinstance(r, dict) or not _has_lyrics(r) or not _is_relevant(r, want_title):
            continue
        if _disagrees(r, want_artist, want_duration, artist_confident):
            continue
        score = _score_candidate(r, want_title, want_artist, want_duration, artist_confident, raw_title)
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


def _best_instrumental(results: list, want_title: str, want_artist: str, want_duration: float) -> dict | None:
    """An instrumental record that clearly is this song: same title and artist, matching runtime."""
    for r in results:
        if not isinstance(r, dict) or not r.get("instrumental") or _has_lyrics(r):
            continue
        if _similarity(r.get("trackName") or "", want_title) < 0.85:
            continue
        if _similarity(r.get("artistName") or "", want_artist) < _INSTRUMENTAL_ARTIST:
            continue
        rd = _seconds(r.get("duration"))
        if want_duration > 0 and rd > 0 and abs(rd - want_duration) > _DURATION_LIMIT:
            continue
        return r
    return None


# --- Rate limits ---

# Monotonic time before which no request is sent, set from a 429/503 Retry-After.
_blocked_until = 0.0
_DEFAULT_WAIT = {429: 30.0, 503: 1.0}
_MAX_WAIT = 900.0
# After a timeout or connection failure, callers queued for the same lookups fail fast for this long.
_OUTAGE_PAUSE = 15.0
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


def _retry_after(value: str | None, default: float) -> float:
    if not value:
        return default
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, IndexError, OverflowError):
            return default
    return min(max(seconds, 0.0), _MAX_WAIT)


def _block(seconds: float) -> None:
    global _blocked_until
    _blocked_until = max(_blocked_until, time.monotonic() + seconds * (1 + random.uniform(0, 0.3)))


def _check_blocked() -> None:
    remaining = _blocked_until - time.monotonic()
    if remaining > 0:
        raise LyricsRateLimited(f"LRCLIB asked us to wait {remaining:.0f}s more")


def _headers() -> dict:
    headers = {"Accept": "application/json", "User-Agent": LRCLIB_USER_AGENT}
    # Optional registration (lrclib.net/developers) for higher limits.
    if app_id := os.getenv("LRCLIB_APP_ID", "").strip():
        headers["Lrclib-App-Id"] = app_id
    if api_key := os.getenv("LRCLIB_API_KEY", "").strip():
        headers["Lrclib-Api-Key"] = api_key
    return headers


# --- Main fetch logic ---

async def _get_json(
    session: aiohttp.ClientSession, endpoint: str, params: dict
) -> dict | list | None:
    """Request LRCLIB JSON while preserving unavailable-service failures."""
    for attempt in range(2):
        _check_blocked()
        wait = 0.0
        try:
            async with session.get(
                f"{LYRICS_API_BASE}{endpoint}", params=params, headers=_headers(), timeout=_REQUEST_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    try:
                        return await resp.json(content_type=None)
                    except ValueError:
                        logger.debug("LRCLIB %s returned a body that is not JSON", endpoint)
                        return None
                if resp.status in _DEFAULT_WAIT:
                    wait = _retry_after(resp.headers.get("Retry-After"), _DEFAULT_WAIT[resp.status])
                    # A short 503 ("usually 1 second") is waited out once; anything else stops the lookup.
                    if resp.status == 429 or attempt or wait > 3:
                        _block(wait)
                        raise LyricsRateLimited(f"LRCLIB returned HTTP {resp.status}, retry after {wait:.0f}s")
                elif resp.status in {500, 502, 504}:
                    raise _TransientStatusError(f"LRCLIB returned HTTP {resp.status}")
                else:
                    logger.debug("LRCLIB %s returned HTTP %s", endpoint, resp.status)
                    return None
        except TimeoutError as exc:
            _block(_OUTAGE_PAUSE)
            raise LyricsServiceUnavailable("LRCLIB request timed out") from exc
        except aiohttp.ClientError as exc:
            _block(_OUTAGE_PAUSE)
            raise LyricsServiceUnavailable("LRCLIB connection failed") from exc
        await asyncio.sleep(wait * (1 + random.uniform(0, 0.3)))
    return None


@dataclass
class _Lookup:
    """One song's lookup: the steps share the session, skip repeated queries and remember an instrumental."""
    session: aiohttp.ClientSession
    duration: float
    raw_title: str
    transient: list[LyricsServiceUnavailable] = field(default_factory=list)
    instrumental: dict | None = None
    _sent: set = field(default_factory=set)

    async def request(self, endpoint: str, params: dict):
        key = (endpoint, tuple(sorted(params.items())))
        if key in self._sent:
            return None
        self._sent.add(key)
        try:
            return await _get_json(self.session, endpoint, params)
        except _TransientStatusError as exc:
            # A 5xx on one step: the next step may still succeed.
            self.transient.append(exc)
            return None

    async def get(self, title: str, artist: str, album: str = "") -> dict | None:
        params: dict[str, str | int] = {"track_name": title, "artist_name": artist}
        if album:
            params["album_name"] = album
        if self.duration:
            params["duration"] = int(self.duration)
        data = await self.request("/get", params)
        return data if isinstance(data, dict) else None

    async def search(
        self,
        title: str,
        artist: str = "",
        album: str = "",
        want_artist: str | None = None,
        artist_confident: bool = True,
    ) -> dict | None:
        """Search LRCLIB with its structured title, artist, and album fields.

        ``want_artist`` scores results against a known artist even when the query
        itself omits it (the title-only step deliberately widens the search).
        """
        params = {"track_name": title}
        if artist:
            params["artist_name"] = artist
        if album:
            params["album_name"] = album

        data = await self.request("/search", params)
        if not isinstance(data, list):
            return None
        scored_artist = artist if want_artist is None else want_artist
        best = _best_match(data, title, scored_artist, self.duration, artist_confident, self.raw_title)
        if best is None and artist_confident and self.instrumental is None:
            self.instrumental = _best_instrumental(data, title, scored_artist, self.duration)
        return best


async def _search(
    session: aiohttp.ClientSession,
    title: str,
    artist: str = "",
    album: str = "",
    duration: float = 0,
    want_artist: str | None = None,
    artist_confident: bool = True,
) -> dict | None:
    """One structured LRCLIB search, scored against the wanted song."""
    return await _Lookup(session, duration, "").search(title, artist, album, want_artist, artist_confident)


def _instrumental(data: dict, fallback_title: str, fallback_artist: str) -> dict:
    return {
        "lyrics": "",
        "synced": "",
        "title": data.get("trackName") or fallback_title,
        "artist": data.get("artistName") or fallback_artist,
        "instrumental": True,
    }


async def fetch_lyrics(
    title: str,
    uploader: str = "",
    duration: float = 0,
    album: str = "",
    *,
    track: str = "",
    artist: str = "",
    session: aiohttp.ClientSession | None = None,
    instrumental_ok: bool = False,
) -> dict | None:
    """Fetch lyrics from lrclib.net.

    ``track``/``artist`` are yt-dlp's music metadata; when given they are tried first. Then the
    video title is parsed and progressively looser lookups are tried:
      1. Exact /get with title and artist (plus album and duration when known)
      2. Structured search: cleaned title, artist and album, then without the album
      3. Structured title-only search
      4. 'Title - Artist' read the other way round
      5. Swapped search: uploader as artist and left-of-dash as title
         (handles "SongTitle - description" where uploader is the real artist)
      6. Aggressive fallback: strip brackets, feat credits and labels, and retry

    Returns dict with keys: lyrics, synced, title, artist, instrumental, or None. An instrumental
    record is returned (with empty lyrics) only when ``instrumental_ok``; otherwise it means None.
    Raises LyricsServiceUnavailable when LRCLIB is down or rate limits us.
    """
    _check_blocked()
    parsed = _parse_title(title, uploader)
    uploader_artist = _clean_artist(uploader) if uploader else ""
    # An artist parsed from "Artist - Song" or a Topic/VEVO channel is reliable; one guessed
    # from another uploader is not (aggregator/OST channels upload under their own name).
    artist_confident = bool(parsed.artist) or titles.is_artist_channel(uploader)
    clean_t = parsed.title
    clean_a = parsed.artist or uploader_artist
    clean_album = (album or "").strip()
    numeric_duration = _seconds(duration)
    valid_duration = numeric_duration if 1 <= numeric_duration <= 3600 else 0

    logger.debug(f"Lyrics search: title='{clean_t}', artist='{clean_a}' "
                 f"(raw='{title}', uploader='{uploader}')")

    async def run(lookup: _Lookup) -> dict | None:
        def found(data: dict, fallback_title: str, fallback_artist: str) -> dict:
            return _format(data, fallback_title, fallback_artist, valid_duration, title)

        async def exact(t: str, a: str) -> dict | None:
            data = await lookup.get(t, a, clean_album)
            if data and _has_lyrics(data):
                return found(data, t, a)
            if data and data.get("instrumental"):
                lookup.instrumental = data
            return None

        hint_t = _clean_title(track) if track else ""
        hint_a = titles.clean_artist_side(artist) if artist else ""
        if hint_t and hint_a:
            if hit := await exact(hint_t, hint_a):
                return hit
            if lookup.instrumental:
                return None
            if hit := await lookup.search(hint_t, hint_a):
                return found(hit, hint_t, hint_a)

        # 1) Exact metadata lookup; LRCLIB matches the duration within 2s.
        if clean_a and (artist_confident or clean_album):
            if hit := await exact(clean_t, clean_a):
                return hit
            if lookup.instrumental:
                return None

        # 2) Structured search: title + artist (+ album, then without it)
        for search_album in dict.fromkeys((clean_album, "")):
            hit = await lookup.search(clean_t, clean_a, search_album, artist_confident=artist_confident)
            if hit:
                return found(hit, clean_t, clean_a)

        # 3) Title-only search (covers channels, game OSTs, etc.)
        if clean_a:
            hit = await lookup.search(clean_t, want_artist=clean_a, artist_confident=artist_confident)
            if hit:
                return found(hit, clean_t, clean_a)

        # 4) "Title - Artist" re-uploads: the left side is the song.
        if parsed.swapped:
            swap_a, swap_t = parsed.swapped
            hit = await lookup.search(swap_t, swap_a)
            if hit:
                return found(hit, swap_t, swap_a)

        # 5) Swapped search: when the uploader doesn't match the parsed artist,
        #    the "Artist - Title" split may be backwards (e.g. "Song - description").
        #    Try uploader as artist + left-of-dash (parsed_artist) as a title.
        if uploader_artist and parsed.artist and not _name_match(uploader_artist, clean_a):
            alt_t = _clean_title(parsed.artist)
            hit = await lookup.search(alt_t, uploader_artist, clean_album)
            if hit:
                return found(hit, alt_t, uploader_artist)

        # 6) Aggressively strip brackets, feat credits and labels, and retry
        bare_t = _bare_title(clean_t)
        if bare_t and bare_t != clean_t:
            hit = await lookup.search(bare_t, clean_a, artist_confident=artist_confident)
            if hit:
                return found(hit, clean_t, clean_a)
        return None

    try:
        if session is not None:
            lookup = _Lookup(session, valid_duration, title)
            result = await run(lookup)
        else:
            async with aiohttp.ClientSession() as own_session:
                lookup = _Lookup(own_session, valid_duration, title)
                result = await run(lookup)
    except LyricsServiceUnavailable:
        raise
    except Exception as exc:
        logger.warning("Lyrics fetch failed: %s", exc)
        return None

    if result:
        return result
    if lookup.instrumental:
        return _instrumental(lookup.instrumental, clean_t, clean_a) if instrumental_ok else None

    # Nothing found and at least one step failed on a transient LRCLIB status:
    # surface the outage instead of a misleading "not found".
    if lookup.transient:
        raise lookup.transient[0]

    return None


_LRC_TIMESTAMP = re.compile(r'\[\d{1,2}:\d{2}(?:[.:]\d{1,3})?\]\s*')


def strip_lrc_timestamps(synced: str) -> str:
    """Convert synced LRC text to plain lyrics by removing timestamp tags."""
    lines = [_LRC_TIMESTAMP.sub('', line).rstrip() for line in synced.splitlines()]
    return "\n".join(lines).strip()


def _format(
    data: dict, fallback_title: str, fallback_artist: str, want_duration: float = 0, raw_title: str = "",
) -> dict:
    synced = data.get("syncedLyrics") or ""
    plain = data.get("plainLyrics") or (strip_lrc_timestamps(synced) if synced else "")
    if _synced_out_of_time(data, want_duration, raw_title):
        # Highlighting would drift; the words are still right.
        synced = ""
    return {
        "lyrics": plain,
        "synced": synced,
        "title": data.get("trackName") or fallback_title,
        "artist": data.get("artistName") or fallback_artist,
        "instrumental": False,
    }


# --- Shared cache (Discord /lyrics and the Activity panel) ---

# webpage_url -> (payload or None, monotonic time). Lyrics never change for a song,
# so a hit is cached long; a miss is retried sooner in case LRCLIB gains it.
_CACHE_MAX = 200
_HIT_TTL = 6 * 3600
_MISS_TTL = 600
_cache: OrderedDict[str, tuple[dict | None, float]] = OrderedDict()


@dataclass
class _Inflight:
    """Concurrent requests for one song wait on this lock and read what the first one cached."""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    waiters: int = 0


_inflight: dict[str, _Inflight] = {}


def cached_lyrics(key: str) -> tuple[bool, dict | None]:
    """(found, payload) from the shared cache; payload None is a cached "not found"."""
    entry = _cache.get(key) if key else None
    if not entry:
        return False, None
    payload, stored = entry
    if time.monotonic() - stored >= (_HIT_TTL if payload else _MISS_TTL):
        del _cache[key]
        return False, None
    return True, payload


def _cache_store(key: str, payload: dict | None) -> None:
    _cache[key] = (payload, time.monotonic())
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


async def lyrics_for_song(song, *, session: aiohttp.ClientSession | None = None) -> dict | None:
    """Lyrics payload for a Song, shared by every caller and cached per webpage_url.

    Returns {"lyrics", "synced", "title", "artist", "instrumental", "webpage_url"}, or None when
    LRCLIB has nothing. An instrumental comes back with empty lyrics and instrumental=True.
    Raises LyricsServiceUnavailable on an outage or while rate limited; that is never cached.
    """
    key = getattr(song, "webpage_url", "") or ""
    found, payload = cached_lyrics(key)
    if found:
        return payload

    async def resolve() -> dict | None:
        result = await fetch_lyrics(
            song.title, song.uploader or "", song.duration or 0, song.album or "",
            track=getattr(song, "track", "") or "", artist=getattr(song, "artist", "") or "",
            session=session, instrumental_ok=True,
        )
        value = None
        if result:
            value = {
                "lyrics": result.get("lyrics", ""),
                "synced": result.get("synced", ""),
                "title": result.get("title") or song.title,
                "artist": result.get("artist") or song.uploader,
                "instrumental": bool(result.get("instrumental")),
                "webpage_url": key,
            }
        if key:
            _cache_store(key, value)
        return value

    if not key:
        return await resolve()

    slot = _inflight.setdefault(key, _Inflight())
    slot.waiters += 1
    try:
        async with slot.lock:
            found, payload = cached_lyrics(key)
            return payload if found else await resolve()
    finally:
        slot.waiters -= 1
        if slot.waiters <= 0:
            _inflight.pop(key, None)
