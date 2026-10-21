import asyncio
import contextlib
import itertools
import json
import logging
import random
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from html import unescape as html_unescape
from typing import Any, cast
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

import aiohttp
import pylast
import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError, GeoRestrictedError, UnsupportedError

from config import LRCLIB_USER_AGENT, MAX_COLLECTION_ENTRIES, MAX_PLAYLIST_SIZE
from models.song import Song
from utils import titles
from utils.helpers import extract_youtube_id, youtube_thumbnail

logger = logging.getLogger(__name__)

# Last.fm asks for an identifiable User-Agent.
pylast.HEADERS["User-Agent"] = LRCLIB_USER_AGENT

# Autoplay YouTube match filtering
MIN_AUTOPLAY_DURATION = 60
MAX_AUTOPLAY_DURATION = 720
YOUTUBE_MATCH_CANDIDATES = 5

# Set entries without metadata (SoundCloud) are extracted one by one, so fewer are read.
MAX_RESOLVED_ENTRIES = 100
# An imported track's YouTube match must run within this many seconds (or 5%) of the track.
IMPORT_DURATION_TOLERANCE = 10

_SPOTIFY_PAGE_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_SPOTIFY_EMBED_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', re.DOTALL)
# The embed player page lists at most this many tracks and has no pagination.
SPOTIFY_TRACK_LIMIT = 100
_SPOTIFY_API_PAUSE = 6 * 3600

_PAGE_CAP = 4 * 1024 * 1024
_TITLE_CAP = 20_000
_MAX_REDIRECTS = 3
_JSON_LD = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL)
_ISO_DURATION = re.compile(r'P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$')
_ITUNES_BATCH = 50

_PLATFORM_HOSTS = {
    "youtube": ("youtube.com", "youtu.be", "youtube-nocookie.com"),
    "soundcloud": ("soundcloud.com",),
    "spotify": ("open.spotify.com", "play.spotify.com"),
    "apple": ("music.apple.com", "itunes.apple.com"),
    "tidal": ("tidal.com",),
}
_ITUNES_HOSTS = ("itunes.apple.com",)
_MEDIA_EXTRACTORS = ["youtube.*", "soundcloud.*"]
_UNAVAILABLE_TITLES = {"[private video]", "[deleted video]", "[unavailable video]"}
_HIDDEN_AVAILABILITY = {"private", "premium_only", "subscriber_only", "needs_auth"}

_SONG_FIELDS = (
    "id", "url", "title", "duration", "thumbnail", "uploader", "channel", "webpage_url",
    "is_live", "live_status", "availability", "album", "track", "artist",
    "available_at", "http_headers", "extractor_key",
)
_COLLECTION_FIELDS = ("id", "title", "uploader", "thumbnail", "playlist_count")

_BOT_CHECK = re.compile(
    r"not a bot|sign in to confirm(?! your age)|try again later|rate.?limit|too many requests|HTTP Error 429",
    re.IGNORECASE,
)
_TRANSIENT = re.compile(r"requested format is not available|timed? ?out|temporar|connection|HTTP Error 5\d\d", re.IGNORECASE)
_PERMANENT = re.compile(
    r"unavailable|private video|video is private|been removed|terminated|copyright|"
    r"not available in your country|geo.?restrict|confirm your age|age.?restrict|inappropriate|"
    r"members.?only|join this channel|premieres? in|live event will begin|unsupported url|"
    r"no suitable extractor|HTTP Error 404|does not exist|not found|incomplete youtube id|not a valid url",
    re.IGNORECASE,
)

# Last.fm candidate lists and their verified YouTube matches, per seed song (ToS 4.3.4 asks for caching).
_RELATED_TTL = 24 * 3600
_MIX_TTL = 3600
_CACHE_SEEDS = 128
_related_cache: OrderedDict[tuple[str, str], tuple[float, list[dict]]] = OrderedDict()
_mix_cache: OrderedDict[str, tuple[float, list[dict]]] = OrderedDict()

JUNK_TITLE_PATTERNS = {
    "mix": r"\bmix(es)?\b",
    "megamix": r"\bmega\s?mix\b",
    "full album": r"\bfull\s+album\b",
    "live": r"\blive\b",
    "karaoke": r"\bkaraoke\b",
    "instrumental": r"\binstrumentals?\b",
    "reaction": r"\breactions?\b",
    "cover": r"\bcovers?\b",
    "sped up": r"\bsped[\s-]?up\b",
    "slowed": r"\bslowed\b",
    "nightcore": r"\bnightcore\b",
    "1 hour": r"\b1\s*hour\b",
    "10 hours": r"\b\d{1,3}\s*hours\b",
    "loop": r"\bloop(ed|ing)?\b",
    "compilation": r"\bcompilations?\b",
    "playlist": r"\bplaylists?\b",
    "best of": r"\bbest\s+of\b",
    "type beat": r"\btype\s+beat\b",
}

_ARTIST_MARKERS = re.compile(
    r'\s*[(\[【「『〔（]\s*(?:ost|o\.s\.t\.?|soundtracks?|bgm|music|themes?|scores?|original(?:\s+soundtrack)?)\s*'
    r'[)\]】」』〕）]'
    r'|\s+(?:ost|o\.s\.t\.?|soundtrack|bgm)\s*$',
    re.IGNORECASE,
)
_TRAILING_MUSIC = re.compile(r'^(\S+(?:\s+\S+)+?)\s+music$', re.IGNORECASE)
_NOISE_SEGMENT = re.compile(
    r'^(?:official(?:\s+(?:music\s+)?(?:video|audio))?|(?:official\s+)?lyric\s+video|lyrics|audio|video|mv|m/v|'
    r'hd|hq|4k|visuali[sz]er)$',
    re.IGNORECASE,
)


class ExtractionError(Exception):
    """A direct-URL extraction failed.

    ``permanent``: the item is gone or restricted, so retrying is pointless.
    ``bot_check``: YouTube refused us ("confirm you're not a bot", rate limit).
    """

    def __init__(self, reason: str, *, permanent: bool = False, bot_check: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.permanent = permanent
        self.bot_check = bot_check

    @classmethod
    def from_download_error(cls, error: Exception) -> ExtractionError:
        reason = re.sub(r'^ERROR:\s*', '', str(error)).strip() or type(error).__name__
        cause = (getattr(error, "exc_info", None) or (None, None))[1]
        if _BOT_CHECK.search(reason):
            return cls(reason, bot_check=True)
        if isinstance(cause, GeoRestrictedError | UnsupportedError):
            return cls(reason, permanent=True)
        if _TRANSIENT.search(reason):
            return cls(reason)
        expected = isinstance(cause, ExtractorError) and cause.expected
        return cls(reason, permanent=bool(expected or _PERMANENT.search(reason)))


# URL parsing

def parse_link(text: str):
    """SplitResult of an http(s) link, or None for anything else (plain text, other schemes)."""
    text = (text or "").strip().strip("<>")
    if not text or any(c.isspace() for c in text):
        return None
    if "://" not in text:
        if not re.match(r'^[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/|$)', text, re.IGNORECASE):
            return None
        text = f"https://{text}"
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    return parts


def _host_in(host: str | None, domains) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == d or host.endswith(f".{d}") for d in domains)


def link_platform(text: str) -> tuple[str, str] | None:
    """(platform, url) for a link to a supported service, or None, in which case it is searched as text."""
    spotify_uri = re.fullmatch(r'\s*spotify:(track|album|playlist|artist):([A-Za-z0-9]+)\s*', text or "")
    if spotify_uri:
        return "spotify", f"https://open.spotify.com/{spotify_uri[1]}/{spotify_uri[2]}"
    parts = parse_link(text)
    if not parts:
        return None
    for platform, hosts in _PLATFORM_HOSTS.items():
        if _host_in(parts.hostname, hosts):
            return platform, parts.geturl()
    return None


def youtube_list_link(url: str) -> str | None:
    """The playlist or Mix a YouTube video link was opened from (its list=), for handle_youtube_playlist()."""
    link = link_platform(url)
    if not link or link[0] != "youtube":
        return None
    list_id = (parse_qs(urlsplit(link[1]).query).get("list") or [""])[0]
    if not re.fullmatch(r'[\w-]+', list_id):
        return None
    video_id = extract_youtube_id(link[1])
    if list_id.startswith(("RD", "UL")) and video_id:
        # Mixes are generated around their video, so they need it in the link.
        return f"https://www.youtube.com/watch?v={video_id}&list={list_id}"
    return f"https://www.youtube.com/playlist?list={list_id}"


def _spotify_parts(url: str) -> tuple[str, str] | None:
    link = link_platform(url)
    if not link or link[0] != "spotify":
        return None
    segments = [s for s in urlsplit(link[1]).path.split("/") if s]
    if segments and segments[0].startswith("intl-"):
        segments = segments[1:]
    if segments and segments[0] == "embed":
        segments = segments[1:]
    if len(segments) >= 2 and re.fullmatch(r'[A-Za-z0-9]+', segments[1]):
        return segments[0], segments[1]
    return None


def spotify_collection(url: str) -> tuple[str, str] | None:
    """(kind, id) for a Spotify playlist, album or artist link, otherwise None."""
    parts = _spotify_parts(url)
    if parts and parts[0] in ("playlist", "album", "artist"):
        return parts
    return None


def _apple_parts(url: str) -> dict | None:
    link = link_platform(url)
    if not link or link[0] != "apple":
        return None
    parts = urlsplit(link[1])
    segments = [s for s in parts.path.split("/") if s]
    country = "us"
    if segments and re.fullmatch(r'[a-z]{2}', segments[0], re.IGNORECASE):
        country = segments.pop(0).lower()
    if not segments:
        return None
    kind, ident = segments[0], segments[-1] if len(segments) > 1 else ""
    song_id = (parse_qs(parts.query).get("i") or [""])[0]
    return {"country": country, "kind": kind, "id": ident, "song_id": song_id, "segments": segments}


def track_collection(url: str) -> tuple[str, str, str] | None:
    """(service, kind, id) of a Spotify playlist/album/artist or Apple Music album/playlist link.

    Their tracks come from collection_tracks() without any YouTube lookup, and each one is matched on
    YouTube by iter_youtube_matches(), so callers can import them in the background like Spotify.
    """
    if spotify := spotify_collection(url):
        return "spotify", *spotify
    apple = _apple_parts(url)
    if apple and not apple["song_id"]:
        if apple["kind"] == "album" and apple["id"].isdigit():
            return "apple", "album", apple["id"]
        if apple["kind"] == "playlist" and re.fullmatch(r'pl\.[\w-]+', apple["id"]):
            return "apple", "playlist", apple["id"]
    return None


def _tidal_parts(url: str) -> tuple[str, str] | None:
    link = link_platform(url)
    if not link or link[0] != "tidal":
        return None
    segments = [s for s in urlsplit(link[1]).path.split("/") if s and s != "browse"]
    found = None
    for kind, ident in itertools.pairwise(segments):
        if kind in ("track", "album", "playlist", "video", "mix", "artist") and re.fullmatch(r'[\w-]+', ident):
            found = (kind, ident)
    return found


def _iso_seconds(value) -> int:
    match = _ISO_DURATION.match(str(value or ""))
    if not match or not any(match.groups()):
        return 0
    days, hours, minutes, seconds = (float(g or 0) for g in match.groups())
    return int(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def _json_ld(html: str) -> list[dict]:
    blocks = []
    for raw in _JSON_LD.findall(html or ""):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for item in data.get("@graph", [data]) if isinstance(data, dict) else data:
            if isinstance(item, dict):
                blocks.append(item)
    return blocks


def _meta(html: str, prop: str) -> str:
    match = re.search(rf'<meta[^>]+(?:property|name)="{re.escape(prop)}"[^>]+content="([^"]*)"', html or "")
    return html_unescape(match.group(1)).strip() if match else ""


def _first_artist(names: str) -> str:
    return (names or "").split(",")[0].strip()


async def _read_capped(resp: aiohttp.ClientResponse, cap: int) -> bytes:
    """At most ``cap`` bytes of the body; read() would buffer (and decompress) all of it."""
    chunks, size = [], 0
    async for chunk in resp.content.iter_chunked(64 * 1024):
        chunks.append(chunk)
        size += len(chunk)
        if size >= cap:
            break
    return b"".join(chunks)[:cap]


def _decode(raw: bytes, charset: str | None) -> str:
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _cache_get(cache: OrderedDict, key, ttl: float):
    entry = cache.get(key)
    if entry and time.monotonic() - entry[0] < ttl:
        cache.move_to_end(key)
        return entry[1]
    cache.pop(key, None)
    return None


def _cache_put(cache: OrderedDict, key, value) -> None:
    cache[key] = (time.monotonic(), value)
    cache.move_to_end(key)
    while len(cache) > _CACHE_SEEDS:
        cache.popitem(last=False)


def _copy_result(data):
    """Callers set requested_by and webpage_url on results, so cached ones are handed out as copies."""
    if isinstance(data, list):
        return [dict(entry) for entry in data]
    if isinstance(data, dict) and isinstance(data.get("entries"), list):
        return {**data, "entries": [dict(entry) for entry in data["entries"]]}
    return dict(data) if isinstance(data, dict) else data


@dataclass
class _CandidatePool:
    """Last.fm candidates being collected for one seed song."""
    candidates: list[dict] = field(default_factory=list)
    names: set[str] = field(default_factory=set)
    artists: set[str] = field(default_factory=set)


class MusicService:
    def __init__(self, bot):
        self.bot = bot

    # Rate limiter (for Last.fm)

    async def _rate_limit_lastfm(self):
        """Wait if we're exceeding the Last.fm rate limit."""
        if self.bot._lastfm_lock is None:
            self.bot._lastfm_lock = asyncio.Lock()
        async with self.bot._lastfm_lock:
            now = time.monotonic()
            limit = self.bot.lastfm_rate_limit
            calls = self.bot._lastfm_call_times

            # Discard entries older than 1 second
            while calls and now - calls[0] > 1.0:
                calls.pop(0)

            if len(calls) >= limit:
                sleep_time = 1.0 - (now - calls[0])
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)

            self.bot._lastfm_call_times.append(time.monotonic())

    # YouTube helpers

    def _ytdl_params(self, opts: dict | None = None) -> dict:
        builder = getattr(self.bot, "ytdl_options", None)
        if callable(builder):
            return builder(**(opts or {}))
        # Before bot.ytdl_options existed: same base, but raise on failure like it does.
        return {**self.bot.ytdl_format_options, "ignoreerrors": False, **(opts or {})}

    def _extract(self, target: str, opts: dict | None = None, **kwargs):
        """yt-dlp extraction on a per-call instance; raises DownloadError on failure.

        A YoutubeDL object is not thread-safe and these calls run concurrently
        in bot.executor, so the shared bot.ytdl must not be reused here.
        """
        with yt_dlp.YoutubeDL(cast(Any, self._ytdl_params(opts))) as ydl:
            return cast(Any, ydl.extract_info(target, **kwargs))

    @staticmethod
    def _single_opts(url: str) -> dict:
        """One song per link: a video's playlist (list=) is ignored, redirect stubs are resolved,
        and an explicit playlist or set is listed flat, without private or deleted entries."""
        extractors = list(_MEDIA_EXTRACTORS)
        if _host_in(urlsplit(url).hostname, ("on.soundcloud.com",)):
            # SoundCloud's short links are plain redirects, which only the generic extractor follows.
            extractors.append("generic")
        return {
            "noplaylist": True,
            "extract_flat": "in_playlist",
            "playlistend": MAX_COLLECTION_ENTRIES,
            "compat_opts": ["no-youtube-unavailable-videos"],
            "allowed_extractors": extractors,
        }

    @staticmethod
    def _best_thumbnail(entry: Mapping[str, Any], webpage_url: str) -> str:
        thumbnail = entry.get("thumbnail") or ""
        thumbs = [t for t in entry.get("thumbnails") or [] if isinstance(t, dict) and t.get("url")]
        if not thumbnail and thumbs:
            thumbnail = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))["url"]
            if not thumbnail.startswith("https://i.ytimg.com/") and extract_youtube_id(webpage_url):
                thumbnail = ""
        if thumbnail and "maxresdefault" in thumbnail:
            # Only HD uploads have a maxres frame; elsewhere it 404s.
            heights = [f.get("height") or 0 for f in entry.get("formats") or [] if isinstance(f, dict)]
            if heights and max(heights) < 720:
                thumbnail = ""
        return thumbnail or youtube_thumbnail(webpage_url)

    @staticmethod
    def _normalize_youtube_entry(entry: Mapping[str, Any] | None) -> dict | None:
        """A search result, playlist entry or extracted video reduced to the fields callers use."""
        if not entry:
            return None

        title = entry.get("title") or entry.get("alt_title")
        if not title:
            return None

        webpage_url = entry.get("webpage_url")
        url = entry.get("url")
        video_id = entry.get("id")

        if not webpage_url:
            if isinstance(url, str) and url.startswith("http"):
                webpage_url = url
            elif video_id:
                webpage_url = f"https://www.youtube.com/watch?v={video_id}"

        if not webpage_url:
            return None

        normalized = {key: entry[key] for key in _SONG_FIELDS if entry.get(key) is not None}
        normalized["title"] = title
        normalized["webpage_url"] = webpage_url
        normalized["url"] = url or webpage_url
        normalized["thumbnail"] = MusicService._best_thumbnail(entry, webpage_url)
        normalized["uploader"] = entry.get("uploader") or entry.get("channel") or "Unknown"

        duration = entry.get("duration")
        if isinstance(duration, int | float):
            normalized["duration"] = round(duration)

        artists = entry.get("artists")
        if isinstance(artists, list) and artists and artists[0]:
            normalized["artist"] = artists[0]

        if MusicService.is_livestream(entry):
            normalized["is_live"] = True

        return normalized

    @staticmethod
    def _is_hidden(entry: Mapping[str, Any]) -> bool:
        title = (entry.get("title") or "").strip().lower()
        return title in _UNAVAILABLE_TITLES or entry.get("availability") in _HIDDEN_AVAILABILITY

    # Cache

    async def get_song_info_cached(self, url_or_query: str, *, raise_errors: bool = False) -> dict | list[dict] | None:
        cache_key = url_or_query.lower().strip()

        if cache_key in self.bot.song_cache:
            cached_data = self.bot.song_cache[cache_key]
            current_time = asyncio.get_running_loop().time()
            if current_time - cached_data["cached_at"] < self.bot.cache_ttl:
                logger.debug(f"Using cached data for: {url_or_query[:50]}")
                return _copy_result(cached_data["data"])

        if raise_errors:
            data = await self.get_song_info(url_or_query, raise_errors=True)
        else:
            data = await self.get_song_info(url_or_query)

        if data:
            current_time = asyncio.get_running_loop().time()
            self.bot.song_cache[cache_key] = {"data": _copy_result(data), "cached_at": current_time}
            await self._cleanup_cache_if_needed()

        return data

    async def _cleanup_cache_if_needed(self):
        if len(self.bot.song_cache) > self.bot.max_cache_size:
            sorted_items = sorted(
                self.bot.song_cache.items(), key=lambda x: x[1]["cached_at"]
            )

            for key, _ in sorted_items[:100]:
                del self.bot.song_cache[key]

            logger.debug(f"Cleaned cache, now has {len(self.bot.song_cache)} entries")

    # Main info router

    async def get_song_info(self, url_or_query: str, *, raise_errors: bool = False) -> dict | list[dict] | None:
        """Resolve a link or search text.

        Returns one song dict; a collection dict with "entries" for YouTube playlists and SoundCloud sets;
        a list of song dicts for Spotify, Apple Music and Tidal collections; or None. With raise_errors,
        a failed YouTube/SoundCloud link raises ExtractionError instead of returning None.
        """
        link = link_platform(url_or_query)
        try:
            if link is None:
                query = (url_or_query or "").strip()
                return await self.search_youtube(query) if query else None

            platform, url = link
            if platform == "spotify":
                return await self.handle_spotify_url(url)
            if platform == "apple":
                return await self._handle_apple_music_url(url)
            if platform == "tidal":
                return await self._handle_tidal_url(url)
            return await self._lookup_media_url(url)

        except ExtractionError as e:
            if raise_errors:
                raise
            logger.warning(f"Could not extract {url_or_query[:80]}: {e.reason}")
        except Exception as e:
            if raise_errors and link and link[0] in ("youtube", "soundcloud"):
                raise ExtractionError(str(e)) from e
            logger.error(f"Error getting song info: {e}")
        return None

    async def _extract_with_retries(
            self, url: str, max_retries: int = 3, opts: dict | None = None
    ) -> dict:
        """Extract with exponential backoff; gone, restricted and bot-checked items are not retried."""
        loop = asyncio.get_running_loop()
        error = ExtractionError(f"No result for {url}")
        for attempt in range(max_retries):
            try:
                data = await loop.run_in_executor(
                    self.bot.executor,
                    lambda: self._extract(url, opts=opts, download=False),
                )
            except DownloadError as e:
                error = ExtractionError.from_download_error(e)
                if error.permanent or error.bot_check:
                    raise error from e
                logger.warning(f"Extract attempt {attempt + 1}/{max_retries} failed: {error.reason}")
            else:
                if data:
                    return data
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
        raise error

    async def _lookup_media_url(self, url: str) -> dict | None:
        info = await self._extract_with_retries(url, max_retries=3, opts=self._single_opts(url))
        if info.get("_type") in ("playlist", "multi_video") or "entries" in info:
            return await self._collection_result(info, url)
        return self._extracted_song(info, url)

    def _extracted_song(self, info: Mapping[str, Any], url: str) -> dict | None:
        song = self._normalize_youtube_entry({**info, "webpage_url": info.get("webpage_url") or url})
        if song is not None:
            # A full extraction's url is the stream; never stand the page in for a missing one.
            song["url"] = info.get("url") or ""
        return song

    async def _collection_result(self, info: dict, url: str) -> dict:
        """A playlist or set as {"_type": "playlist", "entries": [song dicts], "capped": bool, ...}."""
        raw_entries = [e for e in info.get("entries") or [] if e and not self._is_hidden(e)]
        capped = len(info.get("entries") or []) >= MAX_COLLECTION_ENTRIES

        stubs = [e for e in raw_entries if not e.get("title")]
        if len(stubs) > MAX_RESOLVED_ENTRIES:
            capped = True
            dropped = {id(e) for e in stubs[MAX_RESOLVED_ENTRIES:]}
            raw_entries = [e for e in raw_entries if id(e) not in dropped]

        async def resolve(entry: dict) -> dict | None:
            if entry.get("title"):
                return self._normalize_youtube_entry(entry)
            return await self._resolve_stub(entry)

        entries = await asyncio.gather(*(resolve(e) for e in raw_entries))

        result = {key: info[key] for key in _COLLECTION_FIELDS if info.get(key) is not None}
        result.update({
            "_type": "playlist",
            "title": info.get("title") or "Playlist",
            "webpage_url": url,
            "entries": [e for e in entries if e],
            "capped": capped,
        })
        return result

    async def _resolve_stub(self, entry: dict) -> dict | None:
        """Extract one set entry that came without metadata (SoundCloud lists only track IDs)."""
        target = entry.get("url") or entry.get("webpage_url") or ""
        if not link_platform(target):
            return None
        loop = asyncio.get_running_loop()
        try:
            info = await loop.run_in_executor(
                self.bot.import_executor,
                lambda: self._extract(target, opts=self._single_opts(target), download=False),
            )
        except DownloadError as e:
            logger.debug(f"Skipping set entry {target}: {e}")
            return None
        return self._extracted_song(info, target) if info and "entries" not in info else None

    # YouTube search

    async def search_youtube_many(self, query: str, limit: int, executor=None) -> list[dict]:
        """Up to ``limit`` normalized results of a YouTube search (one results page, flat)."""
        prefix = f"ytsearch{limit}:" if limit > 1 else "ytsearch:"
        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(
                executor or self.bot.executor,
                lambda: self._extract(f"{prefix}{query}", download=False),
            )
        except DownloadError as e:
            logger.error(f"YouTube search error: {e}")
            return []
        entries = (data or {}).get("entries") or []
        return [e for e in (self._normalize_youtube_entry(raw) for raw in entries if raw) if e]

    async def search_youtube(self, query: str, limit: int = 1, executor=None) -> dict | None:
        """The top result when ``limit`` is 1, otherwise {"entries": [...]} (kept for older callers)."""
        entries = await self.search_youtube_many(query, limit, executor=executor)
        if not entries:
            return None
        if limit == 1:
            return entries[0]
        return {"_type": "playlist", "id": query, "title": query, "entries": entries}

    # Playlist handling

    async def handle_youtube_playlist(self, url: str, limit: int | None = None) -> list[dict]:
        """Songs of a YouTube playlist link, at most ``limit`` (default MAX_COLLECTION_ENTRIES)."""
        link = link_platform(url)
        if not link or link[0] != "youtube":
            logger.error("Not a YouTube playlist link")
            return []

        opts = {
            "noplaylist": False,
            "extract_flat": "in_playlist",
            "playlistend": limit or MAX_COLLECTION_ENTRIES,
            "compat_opts": ["no-youtube-unavailable-videos"],
            "allowed_extractors": ["youtube.*"],
        }
        loop = asyncio.get_running_loop()
        try:
            # Processing (and so paging through the playlist) stays on the worker thread.
            playlist_info = await loop.run_in_executor(
                self.bot.executor,
                lambda: self._extract(link[1], opts=opts, download=False),
            )
        except DownloadError as e:
            logger.error(f"Playlist handling error: {e}")
            return []

        entries = (playlist_info or {}).get("entries") or []
        if not entries:
            logger.error("No playlist entries found")
            return []

        songs = []
        for i, entry in enumerate(entries):
            if not entry or not entry.get("id") or self._is_hidden(entry):
                continue
            webpage_url = f"https://www.youtube.com/watch?v={entry['id']}"
            songs.append({
                "url": None,
                "title": entry.get("title") or f"Song {i + 1}",
                "duration": entry.get("duration") or 0,
                "album": entry.get("album") or "",
                "thumbnail": self._best_thumbnail(entry, webpage_url),
                "uploader": entry.get("uploader") or entry.get("channel") or "Unknown",
                "webpage_url": webpage_url,
                "requested_by": "Unknown",
                **({"is_live": True} if self.is_livestream(entry) else {}),
            })

        logger.info(f"Playlist metadata extracted: {len(songs)} songs")
        return songs

    # Web pages

    @contextlib.asynccontextmanager
    async def _http(self):
        shared = getattr(self.bot, "http_session", None)
        if isinstance(shared, aiohttp.ClientSession) and not shared.closed:
            yield shared
            return
        async with aiohttp.ClientSession() as session:
            yield session

    async def _fetch_text(
            self, url: str, hosts, *, cap: int = _PAGE_CAP, timeout: float = 10, headers: dict | None = None,
            html_only: bool = False,
    ) -> str | None:
        """GET a page on one of ``hosts``, following redirects only within them, reading at most ``cap`` bytes."""
        try:
            async with self._http() as session:
                for _ in range(_MAX_REDIRECTS + 1):
                    parts = parse_link(url)
                    if not parts or not _host_in(parts.hostname, hosts):
                        logger.debug(f"Refusing to fetch {url[:80]}")
                        return None
                    async with session.get(
                            url, headers=headers, allow_redirects=False,
                            timeout=aiohttp.ClientTimeout(total=timeout),
                    ) as resp:
                        if resp.status in (301, 302, 303, 307, 308):
                            url = urljoin(url, resp.headers.get("Location", ""))
                            continue
                        if resp.status != 200:
                            return None
                        if html_only and "html" not in (resp.content_type or ""):
                            return None
                        return _decode(await _read_capped(resp, cap), resp.charset)
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            logger.debug(f"Page fetch failed for {url[:80]}: {e}")
        return None

    async def _fetch_json(self, url: str, hosts, timeout: float = 10):
        text = await self._fetch_text(url, hosts, timeout=timeout, headers={"Accept": "application/json"})
        try:
            return json.loads(text) if text else None
        except ValueError:
            return None

    # Matching tracks from other services on YouTube

    @staticmethod
    def _track_query(track: dict) -> str:
        return f"{(track.get('title') or '').strip()} {_first_artist(track.get('uploader') or '')}".strip()

    def _pick_track_match(self, entries: list[dict], track: dict) -> dict | None:
        """The result whose runtime matches the track (no intro-padded video, loop or sped-up cut),
        otherwise the top result, as before."""
        if not entries:
            return None
        want = track.get("duration") or 0
        if want <= 0:
            return entries[0]

        title = track.get("title") or ""
        artist = _first_artist(track.get("uploader") or "")
        active = self._active_junk_patterns(title, artist)
        name = self._normalize_track_name(re.sub(r'\s*[(\[][^)\]]*[)\]]|\s+-\s+.*$', '', title)) \
            or self._normalize_track_name(title)
        words = [w for w in name.split() if len(w) > 2] or name.split()
        tolerance = max(IMPORT_DURATION_TOLERANCE, want * 0.05)

        best_key, best = None, None
        for index, entry in enumerate(entries):
            duration = entry.get("duration")
            if not duration or self.is_livestream(entry):
                continue
            delta = abs(duration - want)
            entry_title = entry.get("title") or ""
            if delta > tolerance or any(re.search(p, entry_title, re.IGNORECASE) for p in active):
                continue
            norm_title = self._normalize_track_name(entry_title)
            if words and sum(w in norm_title for w in words) < (len(words) + 1) // 2:
                continue
            key = (delta > 3, index)
            if best_key is None or key < best_key:
                best_key, best = key, entry
        return best or entries[0]

    async def _match_track(self, track: dict, executor=None) -> dict | None:
        query = self._track_query(track)
        if not query:
            return None
        entries = await self.search_youtube_many(query, YOUTUBE_MATCH_CANDIDATES, executor=executor)
        return self._pick_track_match(entries, track)

    async def iter_youtube_matches(self, tracks: list[dict]):
        """Yield the YouTube match of each collection_tracks() entry in order, each as soon as it is found."""
        lookups = [
            asyncio.ensure_future(self._match_track(track, executor=self.bot.import_executor))
            for track in tracks
        ]
        try:
            for lookup in lookups:
                if song := await lookup:
                    yield song
        finally:
            for lookup in lookups:
                lookup.cancel()

    async def collection_tracks(self, url: str) -> list[dict]:
        """Tracks of a track_collection() link: title, uploader (artists), duration and the service's own link."""
        collection = track_collection(url)
        if not collection:
            return []
        if collection[0] == "spotify":
            return await self.spotify_tracks(url)
        _, kind, ident = collection
        apple = _apple_parts(url) or {}
        if kind == "album":
            return await self._apple_album_tracks(ident, apple.get("country", "us"))
        return await self._apple_playlist_tracks(url, apple)

    async def _matched_list(self, tracks: list[dict]) -> list[dict] | None:
        return [song async for song in self.iter_youtube_matches(tracks)] or None

    # Spotify

    @staticmethod
    def _spotify_search_query(track: dict) -> str | None:
        """Build a YouTube search query from a Spotify track dict."""
        track_name = track.get("name", "").strip()
        if not track_name:
            return None
        artists = track.get("artists", [])
        if artists:
            artist_name = artists[0].get("name", "").strip()
            if artist_name:
                return f"{track_name} {artist_name}"
        logger.warning(f"Spotify track has no artists: {track_name}")
        return track_name

    async def _fetch_spotify_page(self, url: str) -> str | None:
        return await self._fetch_text(
            url.split("?")[0], _PLATFORM_HOSTS["spotify"], timeout=8, headers={"User-Agent": _SPOTIFY_PAGE_UA},
        )

    async def _spotify_api_track(self, track_id: str) -> dict | None:
        """Title, artists and duration from the Web API, which is skipped for 6h after a 403 (no Premium)."""
        if not self.bot.spotify or time.monotonic() < self.bot.spotify_api_paused_until:
            return None

        loop = asyncio.get_running_loop()
        try:
            track = await loop.run_in_executor(self.bot.executor, lambda: self.bot.spotify.track(track_id))
        except Exception as e:
            if getattr(e, "http_status", None) == 403:
                self.bot.spotify_api_paused_until = time.monotonic() + _SPOTIFY_API_PAUSE
                logger.warning(f"Spotify API refused, using public pages for {_SPOTIFY_API_PAUSE // 3600}h: {e}")
            else:
                logger.warning(f"Spotify API lookup failed, using the public page: {e}")
            return None
        if not self._spotify_search_query(track):
            return None
        return {
            "title": track["name"].strip(),
            "uploader": ", ".join(a.get("name", "") for a in track.get("artists") or [] if a.get("name")),
            "duration": (track.get("duration_ms") or 0) // 1000,
        }

    async def _spotify_api_query(self, url: str) -> str | None:
        """Track name + artist from the Web API (kept for callers that only want the query)."""
        parts = _spotify_parts(url)
        track = await self._spotify_api_track(parts[1]) if parts and parts[0] == "track" else None
        return self._track_query(track) if track else None

    async def _spotify_embed_track(self, track_id: str) -> dict | None:
        """Title, artists and duration from the 11 KB embed player page."""
        html = await self._fetch_spotify_page(f"https://open.spotify.com/embed/track/{track_id}")
        match = _SPOTIFY_EMBED_DATA.search(html or "")
        if not match:
            return None
        try:
            entity = json.loads(match.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
            title = (entity.get("title") or entity.get("name") or "").strip()
        except (ValueError, KeyError, TypeError, AttributeError):
            return None
        if not title:
            return None
        artists = [a.get("name", "") for a in entity.get("artists") or [] if isinstance(a, dict) and a.get("name")]
        return {"title": title, "uploader": ", ".join(artists), "duration": (entity.get("duration") or 0) // 1000}

    async def _spotify_public_track(self, track_id: str) -> dict | None:
        """Title and artists scraped from the public track page."""
        html = await self._fetch_spotify_page(f"https://open.spotify.com/track/{track_id}")
        if not html:
            return None
        title = _meta(html, "og:title")
        if not title:
            return None
        artists = _meta(html, "music:musician_description") or _meta(html, "og:description").split("·")[0].strip()
        return {"title": title, "uploader": artists, "duration": 0}

    async def _spotify_public_query(self, url: str) -> str | None:
        """Track name + artist scraped from the public Spotify page."""
        parts = _spotify_parts(url)
        track = await self._spotify_public_track(parts[1]) if parts and parts[0] == "track" else None
        return self._track_query(track) if track else None

    async def spotify_tracks(self, url: str) -> list[dict]:
        """Title, artists, duration and link of each track, read from the public embed player page."""
        collection = spotify_collection(url)
        if not collection:
            return []
        kind, spotify_id = collection

        html = await self._fetch_spotify_page(f"https://open.spotify.com/embed/{kind}/{spotify_id}")
        match = _SPOTIFY_EMBED_DATA.search(html or "")
        if not match:
            return []
        try:
            tracks = json.loads(match.group(1))["props"]["pageProps"]["state"]["data"]["entity"]["trackList"]
        except (ValueError, KeyError, TypeError):
            logger.warning(f"Unexpected Spotify embed page for {kind} {spotify_id}")
            return []
        if len(tracks) >= SPOTIFY_TRACK_LIMIT:
            logger.info(f"Spotify {kind} {spotify_id}: only the first {SPOTIFY_TRACK_LIMIT} tracks are available")

        result = []
        for track in tracks[:MAX_PLAYLIST_SIZE]:
            title = (track.get("title") or "").strip()
            # Podcast episodes in a playlist have no song to look up.
            if not title or track.get("entityType", "track") != "track":
                continue
            track_id = (track.get("uri") or "").rsplit(":", 1)[-1]
            result.append({
                "title": title,
                "uploader": (track.get("subtitle") or "").strip(),
                "duration": (track.get("duration") or 0) // 1000,
                "webpage_url": f"https://open.spotify.com/track/{track_id}" if track_id else "",
            })
        return result

    async def handle_spotify_url(self, url: str) -> dict | list[dict] | None:
        parts = _spotify_parts(url)
        if not parts:
            return None
        kind, spotify_id = parts
        try:
            if kind == "track":
                track = (
                        await self._spotify_api_track(spotify_id)
                        or await self._spotify_embed_track(spotify_id)
                        or await self._spotify_public_track(spotify_id)
                )
                return await self._match_track(track) if track else None

            if spotify_collection(url):
                return await self._matched_list(await self.spotify_tracks(url))

        except Exception as e:
            logger.error(f"Spotify error: {e}")
        return None

    # Apple Music (free via the iTunes Lookup API)

    @staticmethod
    def _itunes_track(item: dict, country: str) -> dict | None:
        if item.get("wrapperType") != "track" or item.get("kind", "song") != "song" or not item.get("trackName"):
            return None
        track_id = item.get("trackId")
        return {
            "title": item["trackName"],
            "uploader": item.get("artistName") or "",
            "duration": (item.get("trackTimeMillis") or 0) // 1000,
            "webpage_url": f"https://music.apple.com/{country}/song/{track_id}" if track_id else "",
        }

    async def _itunes_lookup(self, ids: str, country: str, **params) -> list[dict]:
        query = urlencode({"id": ids, "country": country, **params})
        data = await self._fetch_json(f"https://itunes.apple.com/lookup?{query}", _ITUNES_HOSTS)
        results = (data or {}).get("results") if isinstance(data, dict) else None
        return [r for r in results or [] if isinstance(r, dict)]

    async def _apple_album_tracks(self, album_id: str, country: str) -> list[dict]:
        items = await self._itunes_lookup(album_id, country, entity="song", limit=200)
        items.sort(key=lambda i: (i.get("discNumber") or 0, i.get("trackNumber") or 0))
        tracks = [t for t in (self._itunes_track(i, country) for i in items) if t]
        return tracks[:MAX_PLAYLIST_SIZE]

    async def _apple_playlist_tracks(self, url: str, apple: dict) -> list[dict]:
        """Songs listed in the playlist page's JSON-LD, with artists from one batched iTunes lookup."""
        country = apple.get("country", "us")
        segments = [quote(s, safe="-._~%") for s in apple.get("segments") or []]
        html = await self._fetch_text(f"https://music.apple.com/{country}/{'/'.join(segments)}", _PLATFORM_HOSTS["apple"])
        listed = []
        for block in _json_ld(html or ""):
            if block.get("@type") in ("MusicPlaylist", "MusicAlbum"):
                listed = [t for t in block.get("track") or [] if isinstance(t, dict) and t.get("name")]
                break
        listed = listed[:MAX_PLAYLIST_SIZE]

        ids = [(t.get("url") or "").rstrip("/").rsplit("/", 1)[-1] for t in listed]
        details: dict[str, dict] = {}
        numeric = [i for i in ids if i.isdigit()]
        for start in range(0, len(numeric), _ITUNES_BATCH):
            for item in await self._itunes_lookup(",".join(numeric[start:start + _ITUNES_BATCH]), country):
                details[str(item.get("trackId"))] = item

        tracks = []
        for entry, track_id in zip(listed, ids, strict=True):
            item = details.get(track_id) or {}
            tracks.append({
                "title": html_unescape(entry["name"]).strip(),
                "uploader": item.get("artistName") or "",
                "duration": (item.get("trackTimeMillis") or 0) // 1000 or _iso_seconds(entry.get("duration")),
                "webpage_url": entry.get("url") or "",
            })
        return tracks

    async def _handle_apple_music_url(self, url: str) -> dict | list[dict] | None:
        """Song, album or playlist from an Apple Music link."""
        apple = _apple_parts(url)
        if not apple:
            return None
        country = apple["country"]
        song_id = apple["song_id"] or (apple["id"] if apple["kind"] == "song" else "")

        if song_id.isdigit():
            for item in await self._itunes_lookup(song_id, country, entity="song"):
                if track := self._itunes_track(item, country):
                    return await self._match_track(track)
        elif collection := track_collection(url):
            if tracks := await self.collection_tracks(url):
                return await self._matched_list(tracks)
            logger.info(f"Apple Music {collection[1]} {collection[2]} has no readable track list")

        # Fallback: search for the page title
        return await self._search_from_url_path(url)

    # Tidal

    async def _tidal_page(self, kind: str, ident: str) -> tuple[str, dict]:
        html = await self._fetch_text(f"https://tidal.com/{kind}/{ident}", _PLATFORM_HOSTS["tidal"]) or ""
        wanted = {"track": "MusicRecording", "album": "MusicAlbum"}.get(kind)
        block = next((b for b in _json_ld(html) if b.get("@type") == wanted), {})
        return html, block

    async def _itunes_album_by_name(self, artist: str, album: str) -> list[dict]:
        """An album's tracks found by name on iTunes, for services whose pages don't list them."""
        query = urlencode({"term": f"{artist} {album}", "entity": "album", "limit": 5})
        data = await self._fetch_json(f"https://itunes.apple.com/search?{query}", _ITUNES_HOSTS)
        want = self._normalize_track_name(album)
        for item in (data or {}).get("results") or [] if isinstance(data, dict) else []:
            name = self._normalize_track_name(item.get("collectionName") or "")
            if (titles.name_match(item.get("artistName") or "", artist)
                    and name and want and (name.startswith(want) or want.startswith(name))):
                return await self._apple_album_tracks(str(item.get("collectionId")), "us")
        return []

    async def _handle_tidal_url(self, url: str) -> dict | list[dict] | None:
        """Track or album from a Tidal link; other pages are searched by their title."""
        parts = _tidal_parts(url)
        if not parts:
            return None
        kind, ident = parts

        if kind not in ("track", "album"):
            return await self._search_from_url_path(f"https://tidal.com/{kind}/{ident}")

        html, block = await self._tidal_page(kind, ident)
        name = html_unescape(block.get("name") or "").strip()
        artists = ", ".join(
            html_unescape(a.get("name") or "") for a in block.get("byArtist") or [] if isinstance(a, dict)
        )
        if kind == "track" and name:
            return await self._match_track(
                {"title": name, "uploader": artists, "duration": _iso_seconds(block.get("duration"))}
            )
        if kind == "album" and name and artists:
            if tracks := await self._itunes_album_by_name(_first_artist(artists), name):
                return await self._matched_list(tracks)
            return await self.search_youtube(f"{_first_artist(artists)} {name}")
        query = self._page_title_query(html)
        return await self.search_youtube(query) if query else None

    # Generic URL → search fallback

    @staticmethod
    def _page_title_query(html: str) -> str:
        """A search query from a music page's og:title or <title>, without the site's own suffixes."""
        title = _meta(html, "og:title")
        if not title:
            match = re.search(r'<title[^>]*>([^<]+)</title>', html or "", re.IGNORECASE)
            title = html_unescape(match.group(1)) if match else ""
        title = re.sub(
            r'\s*(?:[-|]\s*(?:apple\s+music|tidal|listen on .*)|on\s+(?:apple\s+music|tidal))\s*$',
            '', title.replace("\u200e", ""), flags=re.IGNORECASE,
        )
        title = re.sub(r'\s+-\s+(?:playlist|album|single|ep)$', '', title, flags=re.IGNORECASE).strip()
        return title if len(title) > 2 else ""

    async def _search_from_url_path(self, url: str) -> dict | None:
        """Search YouTube for the title of a supported music page."""
        link = link_platform(url)
        if not link or link[0] not in ("apple", "tidal"):
            return None
        html = await self._fetch_text(link[1], _PLATFORM_HOSTS[link[0]], cap=_TITLE_CAP, html_only=True)
        query = self._page_title_query(html or "")
        if query:
            logger.info(f"Extracted search query from URL: {query}")
            return await self.search_youtube(query)
        return None

    # Livestream detection

    @staticmethod
    def is_livestream(song_data: Mapping[str, Any]) -> bool:
        """Check if extracted data represents a livestream/radio (flat entries only carry live_status)."""
        return bool(song_data.get("is_live") or song_data.get("live_status") in ("is_live", "is_upcoming"))

    # Artist/title extraction for Last.fm

    @staticmethod
    def _clean_artist_name(name: str) -> str:
        """Strip OST/soundtrack markers, but only bracketed or as a trailing word ('Post Malone' stays)."""
        name = re.sub(r'[「」【】『』〔〕][^「」【】『』〔〕]*[「」【】『』〔〕]', ' ', name or "")
        name = _ARTIST_MARKERS.sub('', name)
        if match := _TRAILING_MUSIC.match(name.strip()):
            name = match.group(1)
        return titles.collapse(name).strip(' –—-|:')

    @staticmethod
    def _strip_brackets(text: str) -> str:
        """Strip tag brackets (【MV】) and trailing 'Official'; a 「quoted」 song name is kept."""
        text = titles.strip_tag_brackets(text)
        text = re.sub(r'\s*Official\s*$', '', text, flags=re.IGNORECASE)
        return re.sub(r'\s+', ' ', text).strip()

    @staticmethod
    def _clean_uploader(uploader: str) -> str:
        """Strip VEVO / Topic / Official suffixes from an uploader name."""
        return titles.clean_uploader(uploader)

    @staticmethod
    def _name_match(a: str, b: str) -> bool:
        """Case/space-insensitive name matching"""
        return titles.name_match(a, b)

    @staticmethod
    def _split_title(title: str, uploader: str = "") -> tuple[str, str]:
        """Split a YouTube title into (artist, song_title).

        Strips noise and tag brackets, then uses the uploader name to
        determine which side of a dash separator is the artist.
        Returns (artist, song_title) where artist may be empty.
        """
        # Strip YouTube noise patterns and tag brackets
        clean = re.sub(
            r'\s*[\(\[](official(\s*(music\s*)?video|[\s\w]*audio)?|lyric\s*video|lyrics|hd|hq|4k|mv|extended(\s+(mix|version|edit|remix))?)[\)\]]',
            '', title or "", flags=re.IGNORECASE
        ).strip() or title or ""
        clean = re.sub(
            r'\s+extended(?:\s+(?:mix|version|edit|remix))?\s*$',
            '', clean, flags=re.IGNORECASE
        ).strip() or clean
        clean = MusicService._strip_brackets(clean) or clean

        cleaned_up = MusicService._clean_uploader(uploader) if uploader else ""

        for sep in titles.DASHES:
            if sep in clean:
                parts = [titles.unquote(p.strip()) for p in clean.split(sep)]
                # 'Artist - Song - Official Video': a bare label is never the song
                parts = [p for p in parts if p and not _NOISE_SEGMENT.match(p)] or parts
                if len(parts) == 1:
                    return "", parts[0]
                first, last = parts[0], parts[1] if len(parts) > 2 else parts[-1]

                # Use uploader to disambiguate which side is the artist
                if cleaned_up:
                    if len(parts) == 2 and MusicService._name_match(cleaned_up, titles.core(last)):
                        return last, first
                    if MusicService._name_match(cleaned_up, titles.core(first)):
                        return first, last

                first_is_track_id = (
                        bool(re.search(r'\d', first))
                        or '(' in first
                        or '[' in first
                )

                if not first_is_track_id and len(first) <= 60:
                    return first, last

                if (last and len(last) <= 60
                        and not re.search(r'\d', last)
                        and '(' not in last
                        and '[' not in last):
                    return last, first

                if first and len(first) <= 60:
                    return first, last
                break

        if quoted := titles.split_quoted(clean):
            artist, song = quoted
            return artist, song

        return "", clean

    @staticmethod
    def _extract_artist_from_title(title: str, uploader: str) -> str:
        """Extract a clean artist name from a YouTube title and uploader."""
        artist, _ = MusicService._split_title(title, uploader)
        return artist if artist else MusicService._clean_uploader(uploader)

    @staticmethod
    def _extract_song_title(title: str, uploader: str = "") -> str:
        """Return a clean song title for Last.fm lookup."""
        _, song = MusicService._split_title(title, uploader)
        return song

    @staticmethod
    def _extract_content_name(title: str) -> str | None:
        """For OST-style titles extract the franchise/game name."""
        # Title starts with an OST/soundtrack keyword (theme/music/score only in brackets)
        match = re.match(
            r'^(?:[「\[\(【]\s*(?:soundtrack|ost|o\.s\.t\.?|bgm|music|score|theme)s?\s*[」\]\)】]'
            r'|(?:soundtrack|ost|o\.s\.t\.?|bgm)s?\b)\s*'
            r'([A-Za-z0-9][A-Za-z0-9\s\'\-:!]+?)\s*[-–—|]',
            title, re.IGNORECASE
        )
        if match:
            return match.group(1).strip()

        # "Game Name OST" pattern anywhere
        match = re.search(
            r'([A-Za-z][A-Za-z0-9\s\'\-:!,]{2,}?)\s+'
            r'(?:OST|O\.S\.T\.?|Soundtrack|Original\s*Soundtrack)\b',
            title, re.IGNORECASE
        )
        if match:
            name = match.group(1).strip()
            if len(name) > 3:
                return name

        return None

    def _normalize_track_name(self, name: str) -> str:
        if not name:
            return ""
        normalized = name.lower().strip()
        normalized = re.sub(r'[^\w\s]', '', normalized)
        normalized = re.sub(r'\s+', ' ', normalized)
        return normalized

    # Autoplay YouTube match verification

    @staticmethod
    def _active_junk_patterns(track_name: str, track_artist: str) -> list[str]:
        """Junk patterns, minus any keyword that is part of the wanted song's own name or artist."""
        norm_name = re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', '', (track_name or "").lower())).strip()
        return [
            pattern for keyword, pattern in JUNK_TITLE_PATTERNS.items()
            if not (re.search(pattern, track_name or "", re.IGNORECASE)
                    or re.search(pattern, track_artist or "", re.IGNORECASE)
                    or keyword in norm_name)
        ]

    def _pick_best_youtube_match(
            self, entries: list[dict], track_name: str, track_artist: str,
            candidate_duration: int | None = None, relaxed: bool = False
    ) -> dict | None:
        """Pick the YouTube result that actually is the Last.fm candidate.

        Returns None when every entry looks like a mix/karaoke/reaction/etc.
        ``relaxed`` drops the name/artist requirement and accepts any entry that
        is not junk and has a sane runtime — used only to keep autoplay alive
        when no candidate produced a confident match.
        """
        if not entries:
            return None

        norm_name = self._normalize_track_name(track_name)
        norm_artist = self._normalize_track_name(self._clean_artist_name(track_artist))
        name_words = [w for w in norm_name.split() if len(w) > 2]
        active_patterns = self._active_junk_patterns(track_name, track_artist)

        allow_long = bool(candidate_duration and candidate_duration > MAX_AUTOPLAY_DURATION)

        best_entry = None
        best_score = -1.0 if relaxed else 0.0

        for index, raw_entry in enumerate(entries):
            if not raw_entry:
                continue

            entry = self._normalize_youtube_entry(raw_entry)
            if not entry:
                continue

            title = entry.get("title") or ""
            uploader = entry.get("uploader") or entry.get("channel") or ""

            if self.is_livestream(entry):
                logger.debug(f"Rejected '{title}' (livestream)")
                continue

            junk = next((p for p in active_patterns if re.search(p, title, re.IGNORECASE)), None)
            if junk:
                logger.debug(f"Rejected '{title}' (matched junk pattern {junk})")
                continue

            duration = entry.get("duration")
            if duration is not None:
                if duration < MIN_AUTOPLAY_DURATION:
                    logger.debug(f"Rejected '{title}' (too short: {duration}s)")
                    continue
                if duration > MAX_AUTOPLAY_DURATION and not allow_long:
                    logger.debug(f"Rejected '{title}' (too long: {duration}s)")
                    continue

            norm_title = self._normalize_track_name(title)
            norm_uploader = self._normalize_track_name(self._clean_uploader(uploader))

            score = 0.0
            if norm_name and norm_name in norm_title:
                score += 5.0
            elif name_words and sum(w in norm_title for w in name_words) >= (len(name_words) + 1) // 2:
                score += 2.0

            if norm_artist and norm_artist in norm_title:
                score += 2.0
            if norm_artist and norm_artist in norm_uploader:
                score += 2.0
            if norm_artist and re.search(r'\s*[-–]\s*Topic$', uploader, re.IGNORECASE) \
                    and norm_artist in norm_uploader:
                score += 3.0

            score -= index * 0.01

            if score > best_score:
                best_score = score
                best_entry = entry

        if best_entry:
            logger.debug(f"Best YouTube match for '{track_name}': '{best_entry.get('title')}' (score {best_score:.2f})")
        return best_entry

    # Related songs (Last.fm autoplay, YouTube Mix fallback)

    async def _resolve_candidate_duration(self, candidate: dict) -> int | None:
        """Last.fm runtime (seconds) for a candidate, resolved only when it is actually searched.

        pylast drops the <duration> node from getSimilar/getTopTracks responses, so the
        only source is track.getInfo — done here, once per attempted candidate, instead
        of once per collected candidate.
        """
        if 'duration' in candidate:
            return candidate['duration']

        candidate['duration'] = None
        track = candidate.get('track')

        if track is not None:
            try:
                milliseconds = await self._lastfm(track.get_duration)
                if milliseconds:
                    candidate['duration'] = int(milliseconds) // 1000
            except (pylast.PyLastError, OSError, ValueError, TypeError) as e:
                logger.debug(f"Last.fm duration lookup failed for '{candidate.get('name')}': {e}")

        return candidate['duration']

    async def _lastfm(self, call: Callable):
        await self._rate_limit_lastfm()
        return await asyncio.get_running_loop().run_in_executor(self.bot.executor, call)

    def _seed(self, song: Song) -> tuple[str, str]:
        """(artist, title) to look the song up by: yt-dlp's music metadata when present, else the parsed title."""
        uploader = song.uploader or ""
        artist = getattr(song, "artist", "") or self._extract_artist_from_title(song.title or "", uploader)
        title = getattr(song, "track", "") or self._extract_song_title(song.title or "", uploader)
        return self._clean_artist_name(artist), title

    def _add_candidates(
            self, pool: _CandidatePool, items, source: str, priority_new: int, priority_seen: int, cap: int,
            artist_of: str | None = None,
    ) -> None:
        """Append unseen Last.fm tracks; a track by an artist not seen yet gets the better priority."""
        for item in items or []:
            if len(pool.candidates) >= cap:
                return
            track = getattr(item, "item", item)
            try:
                name = track.get_name()
                artist = track.get_artist().get_name()
            except (AttributeError, pylast.PyLastError, TypeError) as e:
                logger.debug(f"Skipping Last.fm item from {source}: {e}")
                continue
            key = self._normalize_track_name(name or "")
            if not key or not artist or key in pool.names:
                continue
            artist_key = (artist_of or artist).lower()
            pool.candidates.append({
                'name': name,
                'artist': artist,
                'priority': priority_new if artist_key not in pool.artists else priority_seen,
                'source': source,
                'track': track,
            })
            pool.names.add(key)
            pool.artists.add(artist_key)

    async def _lastfm_candidates(self, song: Song, artist: str, title: str, size: int) -> list[dict]:
        """Last.fm tracks related to the seed: similar tracks, then similar artists, tags and title search."""
        lastfm = self.bot.lastfm
        pool = _CandidatePool(names={self._normalize_track_name(title)}, artists={artist.lower()})
        wanted, cap = size * 5, size * 8
        lastfm_errors = (pylast.PyLastError, OSError, ValueError, AttributeError, TypeError)

        try:
            similar = await self._lastfm(lambda: lastfm.get_track(artist, title).get_similar(limit=wanted))
            self._add_candidates(pool, similar, "similar_tracks", 1, 2, cap)
            logger.info(f"Found {len(pool.candidates)} similar tracks from Last.fm")
        except lastfm_errors as e:
            logger.warning(f"Error getting similar tracks: {e}")

        if len(pool.candidates) < wanted:
            try:
                artists = await self._lastfm(lambda: lastfm.get_artist(artist).get_similar(limit=5))
                for similar_artist in artists or []:
                    if len(pool.candidates) >= cap:
                        break
                    try:
                        name = similar_artist.item.get_name()
                        top = await self._lastfm(lambda n=name: lastfm.get_artist(n).get_top_tracks(limit=5))
                    except lastfm_errors as e:
                        logger.debug(f"Error processing similar artist: {e}")
                        continue
                    self._add_candidates(pool, top, "similar_artists", 1, 3, cap, artist_of=name)
                logger.info(f"Total candidates after similar artists: {len(pool.candidates)}")
            except lastfm_errors as e:
                logger.warning(f"Error getting similar artists: {e}")

        if len(pool.candidates) < wanted:
            try:
                tags = await self._lastfm(lambda: lastfm.get_track(artist, title).get_top_tags(limit=2))
                for tag_item in tags or []:
                    if len(pool.candidates) >= cap:
                        break
                    try:
                        tag_name = tag_item.item.get_name()
                        logger.info(f"Fetching tracks from tag: {tag_name}")
                        tracks = await self._lastfm(lambda n=tag_name: lastfm.get_tag(n).get_top_tracks(limit=8))
                    except lastfm_errors as e:
                        logger.debug(f"Error processing tag: {e}")
                        continue
                    self._add_candidates(pool, tracks, f"tag_{tag_name}", 2, 4, cap)
                logger.info(f"Total candidates after tags: {len(pool.candidates)}")
            except lastfm_errors as e:
                logger.warning(f"Error getting tag-based recommendations: {e}")

        if len(pool.candidates) < wanted and (content_name := self._extract_content_name(song.title or "")):
            logger.info(f"Trying content tag lookup for: '{content_name}'")
            try:
                tracks = await self._lastfm(lambda: lastfm.get_tag(content_name.lower()).get_top_tracks(limit=10))
                self._add_candidates(pool, tracks, f"content_tag_{content_name}", 2, 4, cap)
                logger.info(f"Total candidates after content tag '{content_name}': {len(pool.candidates)}")
            except lastfm_errors as e:
                logger.debug(f"Content tag lookup failed for '{content_name}': {e}")

        if not pool.candidates:
            # Last resort: search Last.fm by title (no artist/title split needed)
            try:
                def _search_track():
                    page = lastfm.search_for_track("", title).get_next_page()
                    return page[0] if page else None

                found = await self._lastfm(_search_track)
                if found:
                    logger.info(f"Title search found: '{found.get_name()}' by '{found.get_artist().get_name()}'")
                    similar = await self._lastfm(lambda: found.get_similar(limit=wanted))
                    self._add_candidates(pool, similar, "title_search", 2, 2, cap)
                    if pool.candidates:
                        logger.info(f"Title search fallback found {len(pool.candidates)} candidates")
            except lastfm_errors as e:
                logger.debug(f"Title search fallback failed: {e}")

        return pool.candidates

    async def _search_candidate(self, candidate: dict) -> tuple[dict | None, dict | None] | None:
        """(verified match, plausible fallback) on YouTube for one Last.fm candidate; None if the search failed."""
        track_name, track_artist = candidate['name'], candidate['artist']
        logger.debug(f"Searching YouTube for: {track_name} {track_artist} (from {candidate['source']})")
        entries = await self.search_youtube_many(f"{track_name} {track_artist}", YOUTUBE_MATCH_CANDIDATES)
        if not entries:
            return None

        # The Last.fm runtime is only consulted to relax the 12-minute
        # cap, so skip the extra API call unless something is long.
        candidate_duration = (
            await self._resolve_candidate_duration(candidate)
            if any((e.get("duration") or 0) > MAX_AUTOPLAY_DURATION for e in entries)
            else None
        )
        match = self._pick_best_youtube_match(entries, track_name, track_artist, candidate_duration)
        relaxed = None if match else self._pick_best_youtube_match(
            entries, track_name, track_artist, candidate_duration, relaxed=True,
        )
        if match:
            match = {**match, "autoplay_source": "lastfm"}
        else:
            logger.info(f"No verified YouTube match for '{track_name}' by '{track_artist}' ({len(entries)} results)")
        return match, relaxed and {**relaxed, "autoplay_source": "lastfm"}

    async def _lastfm_related(
            self, song: Song, limit: int, usable: Callable[[dict], bool],
    ) -> tuple[list[dict], dict | None]:
        """Verified Last.fm picks, and one unverified pick to fall back on."""
        artist, title = self._seed(song)
        logger.info(f"Finding songs similar to: '{title}' by '{artist}' (uploader: '{song.uploader}')")
        size = max(limit, 3)
        seed = (self._normalize_track_name(artist), self._normalize_track_name(title))

        candidates = _cache_get(_related_cache, seed, _RELATED_TTL)
        if candidates is None:
            candidates = await self._lastfm_candidates(song, artist, title, size)
            if candidates:
                _cache_put(_related_cache, seed, candidates)
        if not candidates:
            logger.warning(f"No candidate tracks found for {song.title}")
            return [], None

        logger.info(f"Total candidates collected: {len(candidates)} (prioritizing different artists)")
        related: list[dict] = []
        fallback = None
        searches, max_searches = 0, min(len(candidates), size * 5)
        for candidate in sorted(candidates, key=lambda c: (c.get('priority', 99), random.random())):
            if len(related) >= limit:
                break
            if "match" not in candidate:
                if searches >= max_searches:
                    logger.info(f"Reached max attempts ({max_searches}), stopping search")
                    break
                searches += 1
                found = await self._search_candidate(candidate)
                await asyncio.sleep(0.3)
                if found is None:
                    continue
                # Kept with the cached candidate, so the next round for this seed needs no search.
                candidate["match"], candidate["relaxed"] = found

            match = candidate["match"]
            if match and usable(match) and all(r["webpage_url"] != match["webpage_url"] for r in related):
                related.append(dict(match))
                logger.info(
                    f"Added song {len(related)}/{limit}: {candidate['name']} by {candidate['artist']} "
                    f"(from {candidate['source']})"
                )
            elif fallback is None and candidate.get("relaxed") and usable(candidate["relaxed"]):
                # Remember one plausible-but-unconfirmed pick so a run where
                # nothing verifies still plays something instead of stopping.
                fallback = dict(candidate["relaxed"])
        return related, fallback

    async def _mix_entries(self, video_id: str) -> list[dict]:
        cached = _cache_get(_mix_cache, video_id, _MIX_TTL)
        if cached is not None:
            return cached
        url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
        opts = {
            "noplaylist": False, "extract_flat": "in_playlist", "playlistend": 25,
            "allowed_extractors": ["youtube.*"],
        }
        try:
            info = await asyncio.get_running_loop().run_in_executor(
                self.bot.executor, lambda: self._extract(url, opts=opts, download=False),
            )
        except DownloadError as e:
            logger.warning(f"YouTube Mix lookup failed for {video_id}: {e}")
            return []
        entries = [e for e in (self._normalize_youtube_entry(r) for r in (info or {}).get("entries") or [] if r) if e]
        _cache_put(_mix_cache, video_id, entries)
        return entries

    async def _mix_related(self, song: Song, limit: int, usable: Callable[[dict], bool]) -> list[dict]:
        """Songs from the seed video's YouTube Mix, other artists first, filtered like Last.fm matches."""
        video_id = extract_youtube_id(song.webpage_url or "")
        if not video_id:
            return []
        artist, title = self._seed(song)
        seed_title = self._normalize_track_name(title)
        active = self._active_junk_patterns(song.title or "", artist)

        other, same = [], []
        for entry in await self._mix_entries(video_id):
            entry_title = entry.get("title") or ""
            duration = entry.get("duration")
            if (extract_youtube_id(entry["webpage_url"]) == video_id or not usable(entry)
                    or self.is_livestream(entry)
                    or any(re.search(p, entry_title, re.IGNORECASE) for p in active)
                    or (duration is not None and not MIN_AUTOPLAY_DURATION <= duration <= MAX_AUTOPLAY_DURATION)):
                continue
            entry_artist, entry_song = self._split_title(entry_title, entry.get("uploader") or "")
            if seed_title and self._normalize_track_name(entry_song) == seed_title:
                continue  # another upload of the seed song
            uploader = self._clean_uploader(entry.get("uploader") or "")
            by_seed_artist = bool(artist) and (
                    self._name_match(uploader, artist) or self._name_match(entry_artist or "", artist)
            )
            (same if by_seed_artist else other).append({**entry, "autoplay_source": "youtube_mix"})

        picks = (other + same)[:limit]
        if picks:
            logger.info(f"YouTube Mix picks for '{song.title}': {[p['title'] for p in picks]}")
        return picks

    async def get_related_songs(
            self, song: Song, limit: int = 1, exclude_urls: set[str] | None = None,
            exclude: Callable[[dict], bool] | None = None,
    ) -> list[dict]:
        """Up to ``limit`` songs to autoplay after ``song``.

        Last.fm recommendations come first. When Last.fm is not configured, fails or verifies nothing,
        the seed's YouTube Mix is used, and an unverified Last.fm pick only after that. Songs whose
        webpage_url is in ``exclude_urls``, or for which ``exclude(song_dict)`` is true, are skipped
        while searching, so the search moves on to the next candidate.
        """
        if song is None:
            return []
        excluded = set(exclude_urls or ()) | {song.webpage_url}

        def usable(match: dict) -> bool:
            return match.get("webpage_url") not in excluded and not (exclude and exclude(match))

        try:
            related, fallback = [], None
            if self.bot.lastfm:
                related, fallback = await self._lastfm_related(song, limit, usable)
            else:
                logger.info("Last.fm not configured, using the YouTube Mix for recommendations")

            if not related:
                related = await self._mix_related(song, limit, usable)
            if not related and fallback:
                logger.info(f"No verified match for any candidate; falling back to '{fallback.get('title')}'")
                related = [fallback]

            logger.info(f"Returning {len(related)} related songs")
            return related

        except Exception:
            logger.exception("Error in get_related_songs")
            return []
