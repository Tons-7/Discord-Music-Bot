import logging
import random
from typing import List, Optional

from config import MAX_HISTORY_SIZE
from models.song import Song

logger = logging.getLogger(__name__)


class QueueService:
    def __init__(self, bot):
        self.bot = bot

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

        if any(
                s.webpage_url == song.webpage_url for s in guild_data["history"]
        ):
            return

        history_song = Song.from_dict(song.to_dict())
        guild_data["history"].append(history_song)

        guild_data["history_position"] = len(guild_data["history"])

        if len(guild_data["history"]) > MAX_HISTORY_SIZE:
            guild_data["history"] = guild_data["history"][-MAX_HISTORY_SIZE:]
            # Clamp position to new length (use len after trim)
            trimmed_len = len(guild_data["history"])
            guild_data["history_position"] = min(
                guild_data.get("history_position", trimmed_len),
                trimmed_len,
            )

        existing_urls = {s.webpage_url for s in guild_data["loop_backup"]}
        if song.webpage_url not in existing_urls:
            guild_data["loop_backup"].append(Song.from_dict(song.to_dict()))
            logger.info(f"Added finished song to loop backup: {song.title}")

    async def get_next_song(self, guild_id: int) -> Optional[Song]:
        guild_data = self.bot.get_guild_data(guild_id)

        if guild_data["loop_mode"] == "song" and guild_data["current"]:
            return Song.from_dict(guild_data["current"].to_dict())

        if guild_data["queue"]:
            return guild_data["queue"].pop(0)

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

            if guild_data["queue"]:
                return guild_data["queue"].pop(0)

        return None

    def clear_queue(self, guild_id: int):
        guild_data = self.bot.get_guild_data(guild_id)
        guild_data["queue"].clear()
        guild_data["loop_backup"].clear()

    def remove_song_from_queue(self, guild_id: int, position: int) -> Optional[Song]:
        guild_data = self.bot.get_guild_data(guild_id)

        if position < 0 or position >= len(guild_data["queue"]):
            return None

        return guild_data["queue"].pop(position)

    def move_song_in_queue(self, guild_id: int, from_pos: int, to_pos: int) -> bool:
        guild_data = self.bot.get_guild_data(guild_id)
        queue = guild_data["queue"]

        if from_pos < 0 or from_pos >= len(queue) or to_pos < 0 or to_pos >= len(queue):
            return False

        song = queue.pop(from_pos)
        # Clamp to_pos to valid range after pop (list is now 1 shorter)
        to_pos = min(to_pos, len(queue))
        queue.insert(to_pos, song)
        return True

    def shuffle_queue(self, guild_id: int):
        guild_data = self.bot.get_guild_data(guild_id)
        if guild_data["queue"]:
            random.shuffle(guild_data["queue"])

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

    # Queue duration & search

    def get_queue_duration(self, guild_id: int) -> int:
        """Total duration of all songs in queue (seconds). 0-duration songs are excluded."""
        guild_data = self.bot.get_guild_data(guild_id)
        return sum(s.duration for s in guild_data["queue"] if s.duration and s.duration > 0)

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
