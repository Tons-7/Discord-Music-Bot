import logging
import random
from typing import TYPE_CHECKING

from config import MAX_HISTORY_SIZE, MAX_PLAYLIST_SIZE
from models.song import Song

if TYPE_CHECKING:
    from bot import GuildData

logger = logging.getLogger(__name__)


def cancel_imports(guild_data: GuildData) -> None:
    """Stop background playlist imports, so a cleared queue stays cleared."""
    for task in guild_data.pop("import_tasks", set()):
        task.cancel()


# Played songs kept in the queue outside queue-loop mode (in queue loop they are the rest of the loop)
PLAYED_LIMIT = MAX_PLAYLIST_SIZE


class QueueService:
    """The queue works like YouTube's: songs stay in it once played.

    ``played`` holds the songs before the current one (oldest first) and ``queue`` the ones after it. A song is in
    at most one of them, so skipping, going back and looping only move songs between the two.
    """

    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def mark_played(guild_data: GuildData, songs: list[Song]) -> None:
        """Songs passed in queue order (finished, jumped over or unplayable) stay above the current one."""
        played = guild_data["played"]
        urls = {s.webpage_url for s in played}
        for song in songs:
            if song.webpage_url not in urls:
                played.append(song)
                urls.add(song.webpage_url)
        if guild_data.get("loop_mode") != "queue" and len(played) > PLAYED_LIMIT:
            del played[:len(played) - PLAYED_LIMIT]

    @staticmethod
    def played_before(guild_data: GuildData) -> list[Song]:
        """The played songs as listeners see them: a current song that just ended is still the current one."""
        played = guild_data["played"]
        current = guild_data.get("current")
        if current and played and played[-1].webpage_url == current.webpage_url:
            return played[:-1]
        return played[:]

    def whole_queue(self, guild_data: GuildData) -> list[Song]:
        """Every song of the queue in order: the played ones, the current one, then the upcoming ones."""
        current = guild_data.get("current")
        return self.played_before(guild_data) + ([current] if current else []) + guild_data["queue"]

    def get_visible_queue(self, guild_id: int) -> list[Song]:
        """The songs still to come, in play order; in queue-loop mode the next round's played songs follow."""
        guild_data = self.bot.get_guild_data(guild_id)
        if guild_data["loop_mode"] == "queue":
            return guild_data["queue"] + self.played_before(guild_data)
        return guild_data["queue"][:]

    def add_to_history(self, guild_id: int, song: Song):
        """Record a finished song in the listening history; a song played again moves to the most recent end."""
        guild_data = self.bot.get_guild_data(guild_id)
        history = [s for s in guild_data["history"] if s.webpage_url != song.webpage_url]
        history.append(Song.from_dict(song.to_dict()))
        guild_data["history"] = history[-MAX_HISTORY_SIZE:]

    async def get_next_song(self, guild_id: int) -> Song | None:
        """Move on from the current song, which stays in the queue as played, and take the next one."""
        guild_data = self.bot.get_guild_data(guild_id)
        # A user skip moves on once even in song-loop mode.
        advance = guild_data.pop("force_advance", False)
        current = guild_data["current"]

        if guild_data["loop_mode"] == "song" and current and not advance:
            return Song.from_dict(current.to_dict())

        if current:
            self.mark_played(guild_data, [current])
        if guild_data["queue"]:
            return guild_data["queue"].pop(0)

        if guild_data["loop_mode"] == "queue" and guild_data["played"]:
            logger.info(f"Queue finished, starting it again ({len(guild_data['played'])} songs)")
            guild_data["queue"], guild_data["played"] = guild_data["played"], []
            if guild_data["shuffle"]:
                random.shuffle(guild_data["queue"])
            return guild_data["queue"].pop(0)

        return None

    def skip_ahead(self, guild_data: GuildData, index: int) -> Song:
        """Jump to queue[index], left at the front of the queue; the current song and those before it are played."""
        queue = guild_data["queue"]
        passed = queue[:index]
        if guild_data.get("current"):
            passed.insert(0, guild_data["current"])
        self.mark_played(guild_data, passed)
        guild_data["queue"] = queue[index:]
        return guild_data["queue"][0]

    def step_back(self, guild_data: GuildData, index: int | None = None) -> Song | None:
        """Go back to a played song (default: the one before the current song) and return it to be played.

        The songs after it, the current one included, return to the front of the queue. With nothing played yet,
        the previous button falls back to the listening history. None when there is nothing to go back to.
        """
        played = self.played_before(guild_data)
        current = guild_data.get("current")
        at = len(played) - 1 if index is None else index
        if 0 <= at < len(played):
            target = played[at]
            back = played[at + 1:]
            guild_data["played"] = played[:at]
        elif index is None and not played and (target := self._previous_in_history(guild_data)):
            back = []
            guild_data["played"] = played  # a current song that just ended goes back in the queue, below
            guild_data["queue"] = [s for s in guild_data["queue"] if s.webpage_url != target.webpage_url]
        else:
            return None
        if current:
            back.append(current)
        guild_data["queue"][:0] = back
        return target

    def has_previous(self, guild_data: GuildData) -> bool:
        return bool(self.played_before(guild_data)) or self._previous_in_history(guild_data) is not None

    @staticmethod
    def _previous_in_history(guild_data: GuildData) -> Song | None:
        """A copy of the history song heard before the current one (the latest one when nothing plays)."""
        history = guild_data["history"]
        current = guild_data.get("current")
        end = len(history)
        if current:
            end = next((i for i, s in enumerate(history) if s.webpage_url == current.webpage_url), end)
        return Song.from_dict(history[end - 1].to_dict()) if end > 0 else None

    def clear_queue(self, guild_id: int):
        """Empty the queue, played songs included; the current song keeps playing."""
        guild_data = self.bot.get_guild_data(guild_id)
        cancel_imports(guild_data)
        guild_data["queue"].clear()
        guild_data["played"].clear()

    def remove_song_from_queue(self, guild_id: int, position: int) -> Song | None:
        guild_data = self.bot.get_guild_data(guild_id)

        if position < 0 or position >= len(guild_data["queue"]):
            return None

        return guild_data["queue"].pop(position)

    def remove_played(self, guild_id: int, position: int) -> Song | None:
        guild_data = self.bot.get_guild_data(guild_id)
        if 0 <= position < len(self.played_before(guild_data)):
            return guild_data["played"].pop(position)
        return None

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
        """Append a song; one that was played already moves down from the played part."""
        guild_data = self.bot.get_guild_data(guild_id)
        played = guild_data["played"]
        for i, s in enumerate(played):
            if s.webpage_url == song.webpage_url:
                del played[i]
                break
        guild_data["queue"].append(song)

    # Queue duration & search

    def get_queue_duration(self, guild_id: int) -> int:
        """Total duration of all songs in queue (seconds). 0-duration songs are excluded."""
        guild_data = self.bot.get_guild_data(guild_id)
        return sum(s.duration for s in guild_data["queue"] if s.duration and s.duration > 0)

    def get_estimated_wait_time(self, guild_id: int, position: int) -> int:
        """Estimated wall-clock seconds until a given 1-based queue position starts playing.

        Accounts for the remaining time of the current song plus all songs before
        the target position, adjusted for playback speed.
        """
        guild_data = self.bot.get_guild_data(guild_id)
        playback_service = self.bot.playback
        wait = 0.0

        # The current song keeps the speed its source was built with
        current = guild_data.get("current")
        if current and current.duration:
            current_pos = playback_service.get_current_position(guild_id)
            remaining = max(0, current.duration - current_pos)
            wait += remaining / (playback_service.source_speed(guild_data) or 1.0)

        queued = sum(
            song.duration for song in guild_data["queue"][:max(0, position - 1)]
            if song.duration and song.duration > 0
        )
        effective_speed = playback_service.get_effective_speed(guild_data)
        wait += queued / effective_speed if effective_speed > 0 else queued

        return int(wait)

    def search_queue(self, guild_id: int, query: str) -> list[tuple[int, Song]]:
        """Search the queue for songs matching a query. Returns (1-based position, Song) pairs."""
        guild_data = self.bot.get_guild_data(guild_id)
        query_lower = query.lower()
        results = []

        for i, song in enumerate(guild_data["queue"]):
            if (query_lower in song.title.lower()
                    or query_lower in song.uploader.lower()):
                results.append((i + 1, song))

        return results
