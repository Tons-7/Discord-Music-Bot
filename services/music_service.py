import asyncio
import logging
import random
import re
import time
from typing import Optional, Dict, List, Set

import aiohttp

from config import MAX_PLAYLIST_SIZE
from models.song import Song

logger = logging.getLogger(__name__)


class MusicService:
    def __init__(self, bot):
        self.bot = bot

    # Rate limiter (for Last.fm)

    async def _rate_limit_lastfm(self):
        """Wait if we're exceeding the Last.fm rate limit."""
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

    @staticmethod
    def _normalize_youtube_entry(entry: Dict) -> Optional[Dict]:
        if not entry:
            return None

        normalized = dict(entry)

        title = normalized.get("title") or normalized.get("alt_title")
        if not title:
            return None
        normalized["title"] = title

        webpage_url = normalized.get("webpage_url")
        url = normalized.get("url")
        video_id = normalized.get("id")

        if not webpage_url:
            if isinstance(url, str) and url.startswith("http"):
                webpage_url = url
            elif video_id:
                webpage_url = f"https://www.youtube.com/watch?v={video_id}"

        if not webpage_url:
            return None

        normalized["webpage_url"] = webpage_url

        if "url" not in normalized or not normalized["url"]:
            normalized["url"] = webpage_url

        # Livestream flag — propagate so callers can detect it
        if entry.get("is_live"):
            normalized["is_live"] = True

        return normalized

    # Cache

    async def get_song_info_cached(self, url_or_query: str) -> Optional[Dict]:
        cache_key = url_or_query.lower().strip()

        if cache_key in self.bot.song_cache:
            cached_data = self.bot.song_cache[cache_key]
            current_time = asyncio.get_running_loop().time()
            if current_time - cached_data["cached_at"] < self.bot.cache_ttl:
                logger.debug(f"Using cached data for: {url_or_query[:50]}")
                return cached_data["data"]

        data = await self.get_song_info(url_or_query)

        if data:
            current_time = asyncio.get_running_loop().time()
            self.bot.song_cache[cache_key] = {"data": data, "cached_at": current_time}
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

    async def get_song_info(self, url_or_query: str) -> Optional[Dict]:
        try:
            loop = asyncio.get_running_loop()
            lower = url_or_query.lower()

            # Platform-specific handling
            if "spotify.com" in lower and self.bot.spotify:
                return await self.handle_spotify_url(url_or_query)

            if "music.apple.com" in lower:
                return await self._handle_apple_music_url(url_or_query)

            if "tidal.com" in lower:
                return await self._handle_tidal_url(url_or_query)

            if any(p in lower for p in ["youtube.com", "youtu.be", "soundcloud.com"]):
                return await self._extract_with_retries(url_or_query, max_retries=3)

            # Fallback: text search
            return await self.search_youtube(url_or_query)

        except Exception as e:
            logger.error(f"Error getting song info: {e}")
        return None

    async def _extract_with_retries(
            self, url: str, max_retries: int = 3
    ) -> Optional[Dict]:
        """Extract info with exponential backoff."""
        loop = asyncio.get_running_loop()
        for attempt in range(max_retries):
            try:
                data = await loop.run_in_executor(
                    self.bot.executor,
                    lambda: self.bot.ytdl.extract_info(url, download=False),
                )
                if data:
                    return data
            except Exception as e:
                logger.warning(f"Extract attempt {attempt + 1}/{max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
        return None

    # YouTube search

    async def search_youtube(self, query: str, limit: int = 1) -> Optional[Dict]:
        try:
            loop = asyncio.get_running_loop()
            search_prefix = f"ytsearch{limit}:" if limit > 1 else "ytsearch:"
            data = await loop.run_in_executor(
                self.bot.executor,
                lambda: self.bot.ytdl.extract_info(f"{search_prefix}{query}", download=False),
            )

            if data and "entries" in data and data["entries"]:
                if limit == 1:
                    for raw_entry in data["entries"]:
                        normalized = self._normalize_youtube_entry(raw_entry)
                        if normalized:
                            return normalized
                else:
                    return data  # Return full results for multi-search
        except Exception as e:
            logger.error(f"YouTube search error: {e}")
        return None

    # Playlist handling

    async def handle_youtube_playlist(self, url: str) -> List[Dict]:
        try:
            loop = asyncio.get_running_loop()

            playlist_info = await loop.run_in_executor(
                self.bot.executor,
                lambda: self.bot.ytdl.extract_info(url, download=False, process=False),
            )

            if not playlist_info or "entries" not in playlist_info:
                logger.error("No playlist entries found")
                return []

            entries = list(playlist_info["entries"])[:]
            if not entries:
                logger.error("Playlist entries list is empty")
                return []

            songs = []

            for i, entry in enumerate(entries):
                if entry and entry.get("id"):
                    song_data = {
                        "url": None,
                        "title": entry.get("title", f"Song {i + 1}"),
                        "duration": entry.get("duration", 0),
                        "thumbnail": entry.get("thumbnail", ""),
                        "uploader": entry.get("uploader", "Unknown"),
                        "webpage_url": f"https://www.youtube.com/watch?v={entry['id']}",
                        "requested_by": "Unknown",
                    }
                    songs.append(song_data)

            logger.info(f"Playlist metadata extracted: {len(songs)} songs")
            return songs

        except Exception as e:
            logger.error(f"Playlist handling error: {e}")
            return []

    # Spotify

    @staticmethod
    def _spotify_search_query(track: Dict) -> Optional[str]:
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

    def _collect_spotify_tracks(self, first_page: Dict, is_playlist: bool) -> List[Dict]:
        """Walk all Spotify pages and return a flat list of track dicts."""
        tracks = []
        page = first_page

        while page and len(tracks) < MAX_PLAYLIST_SIZE:
            for item in page.get("items", []):
                if len(tracks) >= MAX_PLAYLIST_SIZE:
                    break
                track = item.get("track") if is_playlist else item
                if track and track.get("name"):
                    tracks.append(track)

            if not page.get("next"):
                break
            try:
                page = self.bot.spotify.next(page)
            except Exception as e:
                logger.warning(f"Spotify pagination error after {len(tracks)} tracks: {e}")
                break

        logger.info(f"Collected {len(tracks)} Spotify tracks across pages")
        return tracks

    async def handle_spotify_url(self, url: str) -> Optional[Dict]:
        if not self.bot.spotify:
            return None

        loop = asyncio.get_running_loop()

        try:
            if "track/" in url:
                track_id = url.split("track/")[-1].split("?")[0]
                track = await loop.run_in_executor(
                    self.bot.executor, lambda: self.bot.spotify.track(track_id)
                )
                search_query = self._spotify_search_query(track)
                if not search_query:
                    return None
                return await self.search_youtube(search_query)

            elif "playlist/" in url:
                playlist_id = url.split("playlist/")[-1].split("?")[0]
                first_page = await loop.run_in_executor(
                    self.bot.executor, lambda: self.bot.spotify.playlist_tracks(playlist_id)
                )
                all_tracks = await loop.run_in_executor(
                    self.bot.executor, lambda: self._collect_spotify_tracks(first_page, is_playlist=True)
                )
                songs = []
                for track in all_tracks:
                    search_query = self._spotify_search_query(track)
                    if not search_query:
                        continue
                    song_data = await self.search_youtube(search_query)
                    if song_data:
                        songs.append(song_data)
                    await asyncio.sleep(0.1)
                return songs if songs else None

            elif "album/" in url:
                album_id = url.split("album/")[-1].split("?")[0]
                first_page = await loop.run_in_executor(
                    self.bot.executor, lambda: self.bot.spotify.album_tracks(album_id)
                )
                all_tracks = await loop.run_in_executor(
                    self.bot.executor, lambda: self._collect_spotify_tracks(first_page, is_playlist=False)
                )
                songs = []
                for track in all_tracks:
                    search_query = self._spotify_search_query(track)
                    if not search_query:
                        continue
                    song_data = await self.search_youtube(search_query)
                    if song_data:
                        songs.append(song_data)
                    await asyncio.sleep(0.1)
                return songs if songs else None

        except Exception as e:
            logger.error(f"Spotify error: {e}")
        return None

    # Apple Music (free via iTunes Lookup API)

    async def _handle_apple_music_url(self, url: str) -> Optional[Dict]:
        """Extract song info from Apple Music URL via the free iTunes Lookup API."""
        try:
            # Extract track ID from URL
            # Formats: .../album/name/123?i=456  or  .../song/name/456
            track_id = None
            if "i=" in url:
                track_id = url.split("i=")[-1].split("&")[0]
            elif "/song/" in url:
                track_id = url.rstrip("/").split("/")[-1]

            if track_id and track_id.isdigit():
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    api_url = f"https://itunes.apple.com/lookup?id={track_id}&entity=song"
                    async with session.get(api_url) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            results = data.get("results", [])
                            for item in results:
                                if item.get("wrapperType") == "track":
                                    track = item.get("trackName", "")
                                    artist = item.get("artistName", "")
                                    if track:
                                        return await self.search_youtube(f"{track} {artist}".strip())

            # Fallback: extract from URL path
            return await self._search_from_url_path(url)

        except Exception as e:
            logger.warning(f"Apple Music URL handling failed: {e}")
            return await self._search_from_url_path(url)

    # Tidal

    async def _handle_tidal_url(self, url: str) -> Optional[Dict]:
        """Extract song info from Tidal URL by scraping page title."""
        try:
            return await self._search_from_url_path(url)
        except Exception as e:
            logger.warning(f"Tidal URL handling failed: {e}")
            return None

    # Generic URL → search fallback

    async def _search_from_url_path(self, url: str) -> Optional[Dict]:
        """Try to extract a useful search query from any music URL's HTML title."""
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, allow_redirects=True) as resp:
                    if resp.status == 200:
                        # Read only first 20KB to find <title>
                        html = await resp.text(encoding="utf-8", errors="replace")
                        html = html[:20000]
                        match = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
                        if match:
                            title = match.group(1).strip()
                            # Remove common suffixes
                            for suffix in [" - Apple Music", " on Apple Music", " - Tidal",
                                           " | Tidal", " - Listen on", " | Listen"]:
                                title = title.replace(suffix, "")
                            title = title.strip()
                            if title and len(title) > 2:
                                logger.info(f"Extracted search query from URL: {title}")
                                return await self.search_youtube(title)
        except Exception as e:
            logger.debug(f"URL title extraction failed: {e}")
        return None

    # Livestream detection

    @staticmethod
    def is_livestream(song_data: Dict) -> bool:
        """Check if extracted data represents a livestream/radio."""
        if song_data.get("is_live"):
            return True
        # Only flag as livestream if duration is 0 AND is_live is explicitly True
        duration = song_data.get("duration")
        if duration == 0 and song_data.get("is_live") is True:
            return True
        return False

    # Artist/title extraction for Last.fm

    @staticmethod
    def _clean_artist_name(name: str) -> str:
        """Strip genre/label markers from artist names."""
        name = re.sub(r'[「」【】『』〔〕][^「」【】『』〔〕]*[「」【】『』〔〕]', '', name)
        name = re.sub(
            r'[\(\[【「]?\s*(ost|o\.s\.t\.?|soundtrack|bgm|music|theme|score|original)s?\s*[\)\]】」]?',
            '', name, flags=re.IGNORECASE
        )
        return name.strip(' \u2013\u2014-|:')

    @staticmethod
    def _strip_brackets(text: str) -> str:
        """Strip CJK bracket content (「」【】『』〔〕) and trailing 'Official'."""
        text = re.sub(r'「[^」]*」|【[^】]*】|『[^』]*』|〔[^〕]*〕', '', text)
        text = re.sub(r'\s*Official\s*$', '', text, flags=re.IGNORECASE)
        return re.sub(r'\s+', ' ', text).strip()

    @staticmethod
    def _clean_uploader(uploader: str) -> str:
        """Strip VEVO / Topic / Official suffixes from an uploader name."""
        cleaned = uploader.strip()
        cleaned = re.sub(r'VEVO$', '', cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r'\s*[-\u2013]\s*Topic$', '', cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r'\s*Official$', '', cleaned, flags=re.IGNORECASE).strip()
        return cleaned if cleaned else uploader

    @staticmethod
    def _name_match(a: str, b: str) -> bool:
        """Case/space-insensitive name matching"""
        a, b = a.lower().replace(" ", ""), b.lower().replace(" ", "")
        return bool(a and b and (a == b or a in b or b in a))

    @staticmethod
    def _split_title(title: str, uploader: str = "") -> tuple[str, str]:
        """Split a YouTube title into (artist, song_title).

        Strips noise and CJK brackets, then uses the uploader name to
        determine which side of a dash separator is the artist.
        Returns (artist, song_title) where artist may be empty.
        """
        # Strip YouTube noise patterns and CJK brackets
        clean = re.sub(
            r'\s*[\(\[](official(\s*(music\s*)?video|[\s\w]*audio)?|lyric\s*video|lyrics|hd|hq|4k|mv)[\)\]]',
            '', title, flags=re.IGNORECASE
        ).strip() or title
        clean = MusicService._strip_brackets(clean) or clean

        cleaned_up = MusicService._clean_uploader(uploader) if uploader else ""

        for sep in [' - ', ' \u2013 ', ' \u2014 ']:
            if sep in clean:
                parts = [p.strip() for p in clean.split(sep)]
                first, last = parts[0], parts[-1]

                # Use uploader to disambiguate which side is the artist
                if cleaned_up:
                    if MusicService._name_match(cleaned_up, last):
                        return last, first
                    if MusicService._name_match(cleaned_up, first):
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
    def _extract_content_name(title: str) -> Optional[str]:
        """For OST-style titles extract the franchise/game name."""
        # Title starts with OST/soundtrack keyword
        match = re.match(
            r'^[「\[\(【]?\s*(?:soundtrack|ost|o\.s\.t\.?|bgm|music|score|theme)s?\s*[」\]\)】]?\s*'
            r'([A-Za-z0-9][A-Za-z0-9\s\'\-:!]+?)\s*[-\u2013\u2014|]',
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

    # Related songs (Last.fm autoplay)

    async def get_related_songs(self, song: 'Song', limit: int = 1) -> List[Dict]:
        try:
            if not self.bot.lastfm:
                logger.warning("Last.fm not configured, cannot get recommendations")
                return []

            original_artist = self._clean_artist_name(
                self._extract_artist_from_title(song.title, song.uploader)
            )
            clean_title = self._extract_song_title(song.title, song.uploader)
            logger.info(
                f"Finding songs similar to: '{clean_title}' by '{original_artist}' "
                f"(uploader: '{song.uploader}')"
            )

            seen_track_names: Set[str] = {self._normalize_track_name(clean_title)}
            seen_artists: Set[str] = {original_artist.lower()}
            candidate_tracks = []

            loop = asyncio.get_running_loop()

            # Similar tracks
            try:
                await self._rate_limit_lastfm()

                def _fetch_similar_tracks():
                    t = self.bot.lastfm.get_track(original_artist, clean_title)
                    return t.get_similar(limit=limit * 5)

                similar_tracks = await loop.run_in_executor(self.bot.executor, _fetch_similar_tracks)

                if similar_tracks:
                    for similar in similar_tracks:
                        try:
                            track_item = similar.item
                            track_name = track_item.get_name()
                            track_artist = track_item.get_artist().get_name()

                            normalized_name = self._normalize_track_name(track_name)
                            normalized_artist = track_artist.lower()

                            if normalized_name not in seen_track_names:
                                priority = 1 if normalized_artist not in seen_artists else 2
                                candidate_tracks.append({
                                    'name': track_name,
                                    'artist': track_artist,
                                    'priority': priority,
                                    'source': 'similar_tracks'
                                })
                                seen_track_names.add(normalized_name)
                                if normalized_artist not in seen_artists:
                                    seen_artists.add(normalized_artist)
                                logger.debug(f"Added similar track: {track_name} by {track_artist}")
                        except Exception as track_error:
                            logger.debug(f"Error processing similar track: {track_error}")
                            continue

                    logger.info(f"Found {len(candidate_tracks)} similar tracks from Last.fm")

            except Exception as e:
                logger.warning(f"Error getting similar tracks: {e}")

            # Similar artists
            if len(candidate_tracks) < limit * 5:
                try:
                    await self._rate_limit_lastfm()

                    def _fetch_similar_artists():
                        a = self.bot.lastfm.get_artist(original_artist)
                        return a.get_similar(limit=5)

                    similar_artists = await loop.run_in_executor(self.bot.executor, _fetch_similar_artists)

                    if similar_artists:
                        for similar_artist in similar_artists:
                            try:
                                artist_item = similar_artist.item
                                similar_artist_name = artist_item.get_name()
                                normalized_artist = similar_artist_name.lower()

                                await self._rate_limit_lastfm()

                                def _fetch_artist_top_tracks(name=similar_artist_name):
                                    obj = self.bot.lastfm.get_artist(name)
                                    return obj.get_top_tracks(limit=5)

                                similar_top_tracks = await loop.run_in_executor(
                                    self.bot.executor, _fetch_artist_top_tracks
                                )

                                for top_track in similar_top_tracks:
                                    track_item = top_track.item
                                    track_name = track_item.get_name()
                                    track_artist = track_item.get_artist().get_name()

                                    normalized_name = self._normalize_track_name(track_name)

                                    if normalized_name not in seen_track_names:
                                        priority = 1 if normalized_artist not in seen_artists else 3
                                        candidate_tracks.append({
                                            'name': track_name,
                                            'artist': track_artist,
                                            'priority': priority,
                                            'source': 'similar_artists'
                                        })
                                        seen_track_names.add(normalized_name)
                                        if normalized_artist not in seen_artists:
                                            seen_artists.add(normalized_artist)
                                        logger.debug(f"Added track from similar artist: {track_name}")

                                        if len(candidate_tracks) >= limit * 8:
                                            break

                                if len(candidate_tracks) >= limit * 8:
                                    break

                            except Exception as artist_error:
                                logger.debug(f"Error processing similar artist: {artist_error}")
                                continue

                        logger.info(f"Total candidates after similar artists: {len(candidate_tracks)}")

                except Exception as e:
                    logger.warning(f"Error getting similar artists: {e}")

            # Tag-based fallback
            if len(candidate_tracks) < limit * 5:
                try:
                    await self._rate_limit_lastfm()

                    def _fetch_top_tags():
                        t = self.bot.lastfm.get_track(original_artist, clean_title)
                        return t.get_top_tags(limit=2)

                    top_tags = await loop.run_in_executor(self.bot.executor, _fetch_top_tags)

                    if top_tags:
                        for tag_item in top_tags:
                            try:
                                tag_name = tag_item.item.get_name()
                                logger.info(f"Fetching tracks from tag: {tag_name}")

                                await self._rate_limit_lastfm()

                                def _fetch_tag_tracks(name=tag_name):
                                    g = self.bot.lastfm.get_tag(name)
                                    return g.get_top_tracks(limit=8)

                                tag_tracks = await loop.run_in_executor(self.bot.executor, _fetch_tag_tracks)

                                for tag_track in tag_tracks:
                                    track_item = tag_track.item
                                    track_name = track_item.get_name()
                                    track_artist = track_item.get_artist().get_name()

                                    normalized_name = self._normalize_track_name(track_name)
                                    normalized_artist = track_artist.lower()

                                    if normalized_name not in seen_track_names:
                                        priority = 2 if normalized_artist not in seen_artists else 4
                                        candidate_tracks.append({
                                            'name': track_name,
                                            'artist': track_artist,
                                            'priority': priority,
                                            'source': f'tag_{tag_name}'
                                        })
                                        seen_track_names.add(normalized_name)
                                        if normalized_artist not in seen_artists:
                                            seen_artists.add(normalized_artist)

                                    if len(candidate_tracks) >= limit * 8:
                                        break

                                if len(candidate_tracks) >= limit * 8:
                                    break

                            except Exception as tag_error:
                                logger.debug(f"Error processing tag: {tag_error}")
                                continue

                        logger.info(f"Total candidates after tags: {len(candidate_tracks)}")

                except Exception as e:
                    logger.warning(f"Error getting tag-based recommendations: {e}")

            # Content-name tag fallback (OST/soundtrack)
            if len(candidate_tracks) < limit * 5:
                content_name = self._extract_content_name(song.title)
                if content_name:
                    logger.info(f"Trying content tag lookup for: '{content_name}'")
                    try:
                        await self._rate_limit_lastfm()

                        def _fetch_content_tag_tracks(name=content_name.lower()):
                            g = self.bot.lastfm.get_tag(name)
                            return g.get_top_tracks(limit=10)

                        content_tag_tracks = await loop.run_in_executor(
                            self.bot.executor, _fetch_content_tag_tracks
                        )

                        for top_track in content_tag_tracks:
                            track_item = top_track.item
                            track_name = track_item.get_name()
                            track_artist = track_item.get_artist().get_name()
                            normalized_name = self._normalize_track_name(track_name)
                            normalized_artist = track_artist.lower()

                            if normalized_name not in seen_track_names:
                                priority = 2 if normalized_artist not in seen_artists else 4
                                candidate_tracks.append({
                                    'name': track_name,
                                    'artist': track_artist,
                                    'priority': priority,
                                    'source': f'content_tag_{content_name}'
                                })
                                seen_track_names.add(normalized_name)
                                if normalized_artist not in seen_artists:
                                    seen_artists.add(normalized_artist)

                            if len(candidate_tracks) >= limit * 8:
                                break

                        logger.info(
                            f"Total candidates after content tag '{content_name}': {len(candidate_tracks)}"
                        )

                    except Exception as e:
                        logger.debug(f"Content tag lookup failed for '{content_name}': {e}")

            # Last resort: search Last.fm by title (no artist/title split needed)
            if not candidate_tracks:
                try:
                    await self._rate_limit_lastfm()

                    def _search_track(q=clean_title):
                        results = self.bot.lastfm.search_for_track("", q)
                        page = results.get_next_page()
                        return page[0] if page else None

                    found = await loop.run_in_executor(self.bot.executor, _search_track)

                    if found:
                        found_artist = found.get_artist().get_name()
                        found_name = found.get_name()
                        logger.info(f"Title search found: '{found_name}' by '{found_artist}'")

                        await self._rate_limit_lastfm()

                        def _similar_from_found():
                            return found.get_similar(limit=limit * 5)

                        similar = await loop.run_in_executor(self.bot.executor, _similar_from_found)

                        for s in similar:
                            try:
                                track_item = s.item
                                name = track_item.get_name()
                                artist = track_item.get_artist().get_name()
                                norm = self._normalize_track_name(name)
                                if norm not in seen_track_names:
                                    candidate_tracks.append({
                                        'name': name, 'artist': artist,
                                        'priority': 2, 'source': 'title_search'
                                    })
                                    seen_track_names.add(norm)
                            except Exception:
                                continue

                        if candidate_tracks:
                            logger.info(f"Title search fallback found {len(candidate_tracks)} candidates")

                except Exception as e:
                    logger.debug(f"Title search fallback failed: {e}")

            if not candidate_tracks:
                logger.warning(f"No candidate tracks found for {song.title}")
                return []

            candidate_tracks.sort(key=lambda x: (x.get('priority', 99), random.random()))

            logger.info(f"Total candidates collected: {len(candidate_tracks)} (prioritizing different artists)")

            related_songs = []
            attempts = 0
            max_attempts = min(len(candidate_tracks), limit * 5)

            for candidate in candidate_tracks:
                if len(related_songs) >= limit:
                    break

                if attempts >= max_attempts:
                    logger.info(f"Reached max attempts ({max_attempts}), stopping search")
                    break

                attempts += 1

                try:
                    track_name = candidate.get('name', '')
                    track_artist = candidate.get('artist', '')
                    source = candidate.get('source', 'unknown')

                    if not track_name or not track_artist:
                        continue

                    youtube_query = f"{track_name} {track_artist}"

                    logger.debug(f"Searching YouTube for: {youtube_query} (from {source})")
                    song_data = await self.search_youtube(youtube_query)

                    if song_data:
                        related_songs.append(song_data)
                        logger.info(
                            f"Added song {len(related_songs)}/{limit}: "
                            f"{track_name} by {track_artist} (from {source})"
                        )

                    await asyncio.sleep(0.3)

                except Exception as track_error:
                    logger.warning(f"Error processing track: {track_error}")
                    continue

            logger.info(f"Returning {len(related_songs)} related songs")
            return related_songs

        except Exception as e:
            logger.error(f"Error in get_related_songs: {e}", exc_info=True)
            return []
