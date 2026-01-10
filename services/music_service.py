import asyncio
import logging
import random
import re
from typing import Optional, Dict, List, Set

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
            current_time = asyncio.get_event_loop().time()
            if current_time - cached_data["cached_at"] < self.bot.cache_ttl:
                logger.debug(f"Using cached data for: {url_or_query[:50]}")
                return cached_data["data"]

        data = await self.get_song_info(url_or_query)

        if data:
            current_time = asyncio.get_event_loop().time()
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
            loop = asyncio.get_event_loop()

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
            else:
                data = await self.search_youtube(url_or_query)

            return data
        except Exception as e:
            logger.error(f"Error getting song info: {e}")
        return None

    async def search_youtube(self, query: str) -> Optional[Dict]:
        try:
            loop = asyncio.get_event_loop()
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
            loop = asyncio.get_event_loop()

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

    async def handle_spotify_url(self, url: str) -> Optional[Dict]:
        if not self.bot.spotify:
            return None

        try:
            if "track/" in url:
                track_id = url.split("track/")[-1].split("?")[0]
                track = self.bot.spotify.track(track_id)

                track_name = track.get("name", "")
                artists = track.get("artists", [])
                if not artists or len(artists) == 0:
                    logger.warning(f"Spotify track has no artists: {track_name}")
                    search_query = track_name
                else:
                    artist_name = artists[0].get("name", "")
                    search_query = f"{track_name} {artist_name}" if artist_name else track_name

                return await self.search_youtube(search_query)
            elif "playlist/" in url:
                playlist_id = url.split("playlist/")[-1].split("?")[0]
                results = self.bot.spotify.playlist_tracks(playlist_id)
                songs = []

                items = results.get("items", [])
                for item in items[:25]:
                    track = item.get("track") if item else None
                    if not track or not track.get("name"):
                        continue

                    track_name = track.get("name", "")
                    artists = track.get("artists", [])
                    if not artists or len(artists) == 0:
                        logger.warning(f"Spotify track has no artists: {track_name}")
                        search_query = track_name
                    else:
                        artist_name = artists[0].get("name", "")
                        search_query = f"{track_name} {artist_name}" if artist_name else track_name

                    song_data = await self.search_youtube(search_query)
                    if song_data:
                        songs.append(song_data)
                    await asyncio.sleep(0.1)
                return songs if songs else None
            elif "album/" in url:
                album_id = url.split("album/")[-1].split("?")[0]
                results = self.bot.spotify.album_tracks(album_id)
                songs = []

                items = results.get("items", [])
                for track in items[:25]:
                    if not track or not track.get("name"):
                        continue

                    track_name = track.get("name", "")
                    artists = track.get("artists", [])
                    if not artists or len(artists) == 0:
                        logger.warning(f"Spotify track has no artists: {track_name}")
                        search_query = track_name
                    else:
                        artist_name = artists[0].get("name", "")
                        search_query = f"{track_name} {artist_name}" if artist_name else track_name

                    song_data = await self.search_youtube(search_query)
                    if song_data:
                        songs.append(song_data)
                    await asyncio.sleep(0.1)
                return songs if songs else None
        except Exception as e:
            logger.error(f"Spotify error: {e}")
        return None

    async def get_related_songs(self, song: 'Song', limit: int = 1) -> List[Dict]:
        try:
            if not self.bot.spotify:
                logger.warning("Spotify not configured, cannot get recommendations")
                return []

            search_query = f"{song.title} {song.uploader}"
            results = self.bot.spotify.search(q=search_query, type='track', limit=1)

            if not results or not results['tracks']['items']:
                logger.warning(f"Could not find {song.title} on Spotify")
                return []

            track_info = results['tracks']['items'][0]
            track_id = track_info.get('id')
            track_name = track_info.get('name', song.title)

            if not track_id or not track_info.get('artists'):
                logger.warning(f"Incomplete track info for {song.title}")
                return []

            artist_id = track_info['artists'][0]['id']
            artist_name = track_info['artists'][0]['name']

            logger.info(f"Finding songs similar to: {track_name} by {artist_name}")

            seen_track_ids: Set[str] = {track_id}
            seen_track_names: Set[str] = {self._normalize_track_name(track_name)}
            candidate_tracks = []

            try:
                top_tracks = self.bot.spotify.artist_top_tracks(artist_id, country='US')
                if top_tracks and top_tracks.get('tracks'):
                    for track in top_tracks['tracks']:
                        if self._add_unique_track(track, candidate_tracks, seen_track_ids, seen_track_names):
                            logger.debug(f"Added top track: {track.get('name')}")
                    logger.info(f"Found {len(candidate_tracks)} from top tracks")
            except Exception as e:
                logger.warning(f"Error getting top tracks: {e}")

            if len(candidate_tracks) < limit * 8:
                try:
                    albums = self.bot.spotify.artist_albums(
                        artist_id,
                        album_type='album,single',
                        limit=10
                    )

                    if albums and albums.get('items'):
                        album_list = albums['items']
                        random.shuffle(album_list)

                        for album in album_list[:5]:
                            if len(candidate_tracks) >= limit * 15:
                                break

                            try:
                                album_tracks = self.bot.spotify.album_tracks(album['id'], limit=20)
                                if album_tracks and album_tracks.get('items'):
                                    for track in album_tracks['items']:
                                        track['artists'] = [{'name': artist_name, 'id': artist_id}]
                                        if self._add_unique_track(track, candidate_tracks, seen_track_ids,
                                                                  seen_track_names):
                                            logger.debug(f"Added album track: {track.get('name')}")

                                        if len(candidate_tracks) >= limit * 15:
                                            break
                            except Exception as album_error:
                                logger.debug(f"Error processing album: {album_error}")
                                continue

                    logger.info(f"Total candidates after albums: {len(candidate_tracks)}")
                except Exception as e:
                    logger.warning(f"Error getting albums: {e}")

            if len(candidate_tracks) < limit * 5:
                try:
                    artist_details = self.bot.spotify.artist(artist_id)
                    genres = artist_details.get('genres', [])

                    if genres:
                        genre_query = f'genre:"{genres[0]}" artist:"{artist_name}"'
                        logger.info(f"Using genre search as fallback: {genres[0]}")

                        genre_results = self.bot.spotify.search(
                            q=genre_query,
                            type='track',
                            limit=20
                        )

                        if genre_results and genre_results.get('tracks', {}).get('items'):
                            for track in genre_results['tracks']['items']:
                                if self._add_unique_track(track, candidate_tracks, seen_track_ids, seen_track_names):
                                    logger.debug(f"Added genre track: {track.get('name')}")

                                if len(candidate_tracks) >= limit * 15:
                                    break

                    logger.info(f"Total candidates after genre search: {len(candidate_tracks)}")
                except Exception as e:
                    logger.warning(f"Error with genre search: {e}")

            if not candidate_tracks:
                logger.warning(f"No candidate tracks found for {song.title}")
                return []

            random.shuffle(candidate_tracks)

            related_songs = []
            attempts = 0
            max_attempts = min(len(candidate_tracks), limit * 5)

            for track in candidate_tracks:
                if len(related_songs) >= limit:
                    break

                if attempts >= max_attempts:
                    logger.info(f"Reached max attempts ({max_attempts}), stopping search")
                    break

                attempts += 1

                try:
                    track_name = track.get('name', '')
                    artists = track.get('artists', [])

                    if not track_name or not artists:
                        continue

                    artist_str = artists[0].get('name', '')
                    youtube_query = f"{track_name} {artist_str}"

                    logger.debug(f"Searching YouTube for: {youtube_query}")
                    song_data = await self.search_youtube(youtube_query)

                    if song_data:
                        related_songs.append(song_data)
                        logger.info(f"Added song {len(related_songs)}/{limit}: {track_name} by {artist_str}")

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

    def _add_unique_track(
            self,
            track: Dict,
            candidates: List[Dict],
            seen_ids: Set[str],
            seen_names: Set[str]
    ) -> bool:
        track_id = track.get('id')
        track_name = track.get('name', '')

        if not track_id or not track_name:
            return False

        normalized_name = self._normalize_track_name(track_name)

        if track_id in seen_ids:
            return False

        if normalized_name in seen_names:
            return False

        seen_ids.add(track_id)
        seen_names.add(normalized_name)
        candidates.append(track)

        return True
