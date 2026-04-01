import asyncio
import logging
import random
import re
from typing import Optional, Dict, List, Set

from config import MAX_PLAYLIST_SIZE
from models.song import Song

logger = logging.getLogger(__name__)


class MusicService:
    def __init__(self, bot):
        self.bot = bot

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

        return normalized

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

    async def get_song_info(self, url_or_query: str) -> Optional[Dict]:
        try:
            loop = asyncio.get_running_loop()

            if any(
                    platform in url_or_query.lower()
                    for platform in [
                        "youtube.com",
                        "youtu.be",
                        "soundcloud.com",
                        "spotify.com",
                    ]
            ):
                if "spotify.com" in url_or_query and self.bot.spotify:
                    return await self.handle_spotify_url(url_or_query)
                else:
                    for attempt in range(2):
                        try:
                            data = await loop.run_in_executor(
                                self.bot.executor,
                                lambda: self.bot.ytdl.extract_info(
                                    url_or_query, download=False
                                ),
                            )
                            if data:
                                return data
                        except Exception as e:
                            logger.warning(f"Attempt {attempt + 1} failed: {e}")
                            if attempt < 1:
                                await asyncio.sleep(1)
                    return None
            else:
                return await self.search_youtube(url_or_query)
        except Exception as e:
            logger.error(f"Error getting song info: {e}")
        return None

    async def search_youtube(self, query: str) -> Optional[Dict]:
        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(
                self.bot.executor,
                lambda: self.bot.ytdl.extract_info(f"ytsearch:{query}", download=False),
            )

            if data and "entries" in data and data["entries"]:
                for raw_entry in data["entries"]:
                    normalized = self._normalize_youtube_entry(raw_entry)
                    if normalized:
                        return normalized
        except Exception as e:
            logger.error(f"YouTube search error: {e}")
        return None

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

    @staticmethod
    def _spotify_search_query(track: Dict) -> Optional[str]:
        """Build a YouTube search query from a Spotify track dict.
        Returns None if the track has no usable name."""
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
        """
        Walk all Spotify pages and return a flat list of track dicts,  capped at MAX_PLAYLIST_SIZE.

        - is_playlist=True  → each page item is {"track": {...}, "added_at": ...}
        - is_playlist=False → each page item is the track dict directly (album tracks)
        """
        tracks = []
        page = first_page

        while page and len(tracks) < MAX_PLAYLIST_SIZE:
            for item in page.get("items", []):
                if len(tracks) >= MAX_PLAYLIST_SIZE:
                    break
                track = item.get("track") if is_playlist else item
                # track can be None for removed/unavailable playlist entries
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

    @staticmethod
    def _clean_artist_name(name: str) -> str:
        """
        Strip genre/label markers that YouTube uploaders often prepend to OST titles.
        """
        # Remove content inside CJK bracket pairs like 「Soundtrack」
        name = re.sub(r'[「」【】『』〔〕][^「」【】『』〔〕]*[「」【】『』〔〕]', '', name)
        name = re.sub(
            r'[\(\[【「]?\s*(ost|o\.s\.t\.?|soundtrack|bgm|music|theme|score|original)s?\s*[\)\]】」]?',
            '', name, flags=re.IGNORECASE
        )
        return name.strip(' \u2013\u2014-|:')

    @staticmethod
    def _extract_artist_from_title(title: str, uploader: str) -> str:
        """
        Extract a clean artist name from a YouTube title and uploader.
        """
        for sep in [' - ', ' \u2013 ', ' \u2014 ']:
            if sep in title:
                parts = [p.strip() for p in title.split(sep)]
                first, last = parts[0], parts[-1]

                first_is_track_id = (
                        bool(re.search(r'\d', first))
                        or '(' in first
                        or '[' in first
                )

                if not first_is_track_id and len(first) <= 60:
                    return first

                if (last and len(last) <= 60
                        and not re.search(r'\d', last)
                        and '(' not in last
                        and '[' not in last):
                    return last

                if first and len(first) <= 60:
                    return first
                break

        cleaned = uploader.strip()
        cleaned = re.sub(r'VEVO$', '', cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r'\s*[-\u2013]\s*Topic$', '', cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r'\s*Official$', '', cleaned, flags=re.IGNORECASE).strip()
        return cleaned if cleaned else uploader

    @staticmethod
    def _extract_song_title(title: str) -> str:
        """
        Return a lightly cleaned version of the YouTube title for Last.fm lookup.
        """
        clean = re.sub(
            r'\s*[\(\[](official(\s*(music\s*)?video|[\s\w]*audio)?|lyric\s*video|lyrics|hd|hq|4k|mv)[\)\]]',
            '', title, flags=re.IGNORECASE
        ).strip()
        return clean if clean else title

    @staticmethod
    def _extract_content_name(title: str) -> Optional[str]:
        """
        For OST-style titles extract the franchise/game name.
        Returns None if no clear content name is found.
        """
        # Pattern: optional_bracket_label  CONTENT_NAME  separator  track_name
        match = re.match(
            r'^[「\[\(【]?\s*(?:soundtrack|ost|o\.s\.t\.?|bgm|music|score|theme)s?\s*[」\]\)】]?\s*'
            r'([A-Za-z0-9][A-Za-z0-9\s\'\-:!]+?)\s*[-\u2013\u2014|]',
            title, re.IGNORECASE
        )
        if match:
            return match.group(1).strip()
        return None

    async def get_related_songs(self, song: 'Song', limit: int = 1) -> List[Dict]:
        try:
            if not self.bot.lastfm:
                logger.warning("Last.fm not configured, cannot get recommendations")
                return []

            original_artist = self._clean_artist_name(
                self._extract_artist_from_title(song.title, song.uploader)
            )
            clean_title = self._extract_song_title(song.title)
            logger.info(
                f"Finding songs similar to: '{clean_title}' by '{original_artist}' "
                f"(uploader: '{song.uploader}')"
            )

            seen_track_names: Set[str] = {self._normalize_track_name(clean_title)}
            seen_artists: Set[str] = {original_artist.lower()}
            candidate_tracks = []

            loop = asyncio.get_running_loop()

            try:
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

            if len(candidate_tracks) < limit * 5:
                try:
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

                                def _fetch_artist_top_tracks(name=similar_artist_name):
                                    obj = self.bot.lastfm.get_artist(name)
                                    return obj.get_top_tracks(limit=5)

                                similar_top_tracks = await loop.run_in_executor(self.bot.executor,
                                                                                _fetch_artist_top_tracks)

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

            if len(candidate_tracks) < limit * 5:
                try:
                    def _fetch_top_tags():
                        t = self.bot.lastfm.get_track(original_artist, clean_title)
                        return t.get_top_tags(limit=2)

                    top_tags = await loop.run_in_executor(self.bot.executor, _fetch_top_tags)

                    if top_tags:
                        for tag_item in top_tags:
                            try:
                                tag_name = tag_item.item.get_name()
                                logger.info(f"Fetching tracks from tag: {tag_name}")

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

            if len(candidate_tracks) < limit * 5:
                content_name = self._extract_content_name(song.title)
                if content_name:
                    logger.info(f"Trying content tag lookup for: '{content_name}'")
                    try:
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

    def _normalize_track_name(self, name: str) -> str:
        if not name:
            return ""

        normalized = name.lower().strip()
        normalized = re.sub(r'[^\w\s]', '', normalized)
        normalized = re.sub(r'\s+', ' ', normalized)

        return normalized
