import logging
import random
from typing import List, Optional

from config import MAX_HISTORY_SIZE
from models.song import Song

logger = logging.getLogger(__name__)


class QueueService:
    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def _bump_queue_version(guild_data: dict):
        """Invalidate cached queue derivations (e.g. queue duration) after a mutation."""
        guild_data["queue_version"] = guild_data.get("queue_version", 0) + 1

    def sync_loop_backup(self, guild_id: int, force_rebuild: bool = False):
        guild_data = self.bot.get_guild_data(guild_id)

        if force_rebuild:
            seen_urls = set()
            deduplicated = []
            for song in guild_data["loop_backup"]:
                if song.webpage_url not in seen_urls:
                    deduplicated.append(song)
                    seen_urls.add(song.webpage_url)
            guild_data["loop_backup"] = deduplicated
            logger.info(
                f"Deduplicated loop backup to {len(guild_data['loop_backup'])} songs"
            )

    def get_visible_queue(self, guild_id: int) -> List[Song]:
        guild_data = self.bot.get_guild_data(guild_id)

        all_songs = guild_data["queue"][:]

        if guild_data["loop_mode"] == "queue" and guild_data["loop_backup"]:
            all_songs.extend(guild_data["loop_backup"])

        return list({song.webpage_url: song for song in all_songs}.values())

    def add_to_history(self, guild_id: int, song: Song):
        guild_data = self.bot.get_guild_data(guild_id)
        history = guild_data["history"]

        # Walking back with the previous button: the song that just finished is
        # the one history_position points at. Leave order and position alone so
        # the next previous click steps further back instead of repeating it.
        pos = guild_data.get("history_position", len(history))
        if 0 <= pos < len(history) and history[pos].webpage_url == song.webpage_url:
            return

        # A song played again moves to the most-recent end rather than being
        # skipped as a duplicate, so history order — and the previous button,
        # which reads history[history_position - 1] — reflect real play order.
        for i, existing in enumerate(history):
            if existing.webpage_url == song.webpage_url:
                del history[i]
                break

        history.append(Song.from_dict(song.to_dict()))

        if len(history) > MAX_HISTORY_SIZE:
            history = history[-MAX_HISTORY_SIZE:]
            guild_data["history"] = history

        guild_data["history_position"] = len(history)

        existing_urls = {s.webpage_url for s in guild_data["loop_backup"]}
        if song.webpage_url not in existing_urls:
            guild_data["loop_backup"].append(Song.from_dict(song.to_dict()))
            logger.info(f"Added finished song to loop backup: {song.title}")

    def add_songs_to_history(self, guild_id: int, songs: List[Song]):
        """Bulk add_to_history: same move-to-most-recent semantics, deduped once (O(n+m))."""
        if not songs:
            return

        guild_data = self.bot.get_guild_data(guild_id)
        backup_urls = {s.webpage_url for s in guild_data["loop_backup"]}
        added_backup = 0

        # Last occurrence wins, mirroring repeated per-song appends
        incoming = {}
        for song in songs:
            incoming.pop(song.webpage_url, None)
            incoming[song.webpage_url] = song

        history = [s for s in guild_data["history"] if s.webpage_url not in incoming]

        for song in incoming.values():
            history.append(Song.from_dict(song.to_dict()))
            if song.webpage_url not in backup_urls:
                guild_data["loop_backup"].append(Song.from_dict(song.to_dict()))
                backup_urls.add(song.webpage_url)
                added_backup += 1

        if len(history) > MAX_HISTORY_SIZE:
            history = history[-MAX_HISTORY_SIZE:]

        guild_data["history"] = history
        guild_data["history_position"] = len(history)

        if added_backup:
            logger.info(f"Added {added_backup} skipped song(s) to loop backup")

    async def get_next_song(self, guild_id: int) -> Optional[Song]:
        guild_data = self.bot.get_guild_data(guild_id)

        if guild_data["loop_mode"] == "song" and guild_data["current"]:
            return Song.from_dict(guild_data["current"].to_dict())

        if guild_data["queue"]:
            song = guild_data["queue"].pop(0)
            self._bump_queue_version(guild_data)
            return song

        if guild_data["loop_mode"] == "queue" and guild_data["loop_backup"]:
            logger.info(
                f"Queue empty, restoring from loop backup ({len(guild_data['loop_backup'])} songs)"
            )

            guild_data["queue"] = [
                Song.from_dict(song.to_dict())
                for song in guild_data["loop_backup"]
            ]

            if guild_data["shuffle"]:
                random.shuffle(guild_data["queue"])

            self._bump_queue_version(guild_data)

            if guild_data["queue"]:
                return guild_data["queue"].pop(0)

        return None

    def clear_queue(self, guild_id: int):
        guild_data = self.bot.get_guild_data(guild_id)
        guild_data["queue"].clear()
        guild_data["loop_backup"].clear()
        self._bump_queue_version(guild_data)

    def remove_song_from_queue(self, guild_id: int, position: int) -> Optional[Song]:
        guild_data = self.bot.get_guild_data(guild_id)

        if position < 0 or position >= len(guild_data["queue"]):
            return None

        song = guild_data["queue"].pop(position)
        self._bump_queue_version(guild_data)
        return song

    def move_song_in_queue(self, guild_id: int, from_pos: int, to_pos: int) -> bool:
        guild_data = self.bot.get_guild_data(guild_id)
        queue = guild_data["queue"]

        if from_pos < 0 or from_pos >= len(queue) or to_pos < 0 or to_pos >= len(queue):
            return False

        song = queue.pop(from_pos)
        # Clamp to_pos to valid range after pop (list is now 1 shorter)
        to_pos = min(to_pos, len(queue))
        queue.insert(to_pos, song)
        self._bump_queue_version(guild_data)
        return True

    def shuffle_queue(self, guild_id: int):
        guild_data = self.bot.get_guild_data(guild_id)
        if guild_data["queue"]:
            random.shuffle(guild_data["queue"])
            self._bump_queue_version(guild_data)

    def toggle_shuffle(self, guild_id: int) -> bool:
        guild_data = self.bot.get_guild_data(guild_id)
        guild_data["shuffle"] = not guild_data["shuffle"]

        if guild_data["shuffle"]:
            self.shuffle_queue(guild_id)

        return guild_data["shuffle"]

    def set_loop_mode(self, guild_id: int, mode: str):
        guild_data = self.bot.get_guild_data(guild_id)
        guild_data["loop_mode"] = mode

    def add_song_to_queue(self, guild_id: int, song: Song):
        guild_data = self.bot.get_guild_data(guild_id)
        guild_data["queue"].append(song)
        guild_data["loop_backup"].append(Song.from_dict(song.to_dict()))
        self._bump_queue_version(guild_data)

    # Queue duration & search

    def get_queue_duration(self, guild_id: int) -> int:
        """Total duration of all songs in queue (seconds). 0-duration songs are excluded.

        Cached against ``queue_version`` so the O(n) sum is only recomputed when the
        queue actually changes (read on every queue view and every state broadcast).
        """
        guild_data = self.bot.get_guild_data(guild_id)
        version = guild_data.get("queue_version", 0)

        cache = guild_data.get("_queue_duration_cache")
        if cache is not None and cache[0] == version:
            return cache[1]

        total = sum(s.duration for s in guild_data["queue"] if s.duration and s.duration > 0)
        guild_data["_queue_duration_cache"] = (version, total)
        return total

    def get_estimated_wait_time(self, guild_id: int, position: int) -> int:
        """Estimated wall-clock seconds until a given 1-based queue position starts playing.

        Accounts for the remaining time of the current song plus all songs before
        the target position, adjusted for current playback speed.
        """
        guild_data = self.bot.get_guild_data(guild_id)
        playback_service = self.bot._playback_service
        effective_speed = playback_service.get_effective_speed(guild_data)
        wait = 0

        # Add remaining time of current song
        current = guild_data.get("current")
        if current and current.duration:
            current_pos = playback_service.get_current_position(guild_id)
            remaining = max(0, current.duration - current_pos)
            wait += remaining

        # Add durations of songs before target position
        for i, song in enumerate(guild_data["queue"]):
            if i >= position - 1:
                break
            if song.duration and song.duration > 0:
                wait += song.duration

        # Adjust for playback speed (songs play faster/slower than their duration)
        if effective_speed > 0 and effective_speed != 1.0:
            wait = int(wait / effective_speed)

        return wait

    def search_queue(self, guild_id: int, query: str) -> List[tuple[int, Song]]:
        """Search the queue for songs matching a query. Returns (1-based position, Song) pairs."""
        guild_data = self.bot.get_guild_data(guild_id)
        query_lower = query.lower()
        results = []

        for i, song in enumerate(guild_data["queue"]):
            if (query_lower in song.title.lower()
                    or query_lower in song.uploader.lower()):
                results.append((i + 1, song))

        return results
