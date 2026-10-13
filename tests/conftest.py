import asyncio

import pytest

from activity import helpers, known_songs, tasks
from activity.cog_hooks import install_broadcast_hooks
from bot import MusicBot
from models.song import Song

GUILD_ID = 111
USER = {"id": "42", "username": "tester"}


class FakeWsManager:
    def __init__(self):
        self.sent = []

    def has_connections(self, guild_id):
        return True

    def get_connected_user_ids(self, guild_id):
        return set()

    async def broadcast(self, guild_id, event_type, data):
        self.sent.append((event_type, data))


def make_song(video_id: str, **overrides) -> Song:
    return Song({
        "title": f"Song {video_id}",
        "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
        "duration": 180,
        **overrides,
    })


async def settle():
    """Let the 0.1s coalesced broadcast fire."""
    await asyncio.sleep(0.25)


@pytest.fixture
def ws():
    return FakeWsManager()


@pytest.fixture
async def bot(tmp_path, monkeypatch, ws):
    # MusicBot creates music_bot.db and audio_cache/ in the working directory.
    monkeypatch.chdir(tmp_path)
    bot = MusicBot()
    bot.loop = asyncio.get_running_loop()

    async def skip_persistence(guild_id):
        pass

    bot.save_guild_queue = skip_persistence
    bot.clear_guild_queue_from_db = skip_persistence
    install_broadcast_hooks(bot, ws)

    yield bot

    await tasks.cancel_all()
    bot.executor.shutdown(wait=False)
    bot.download_executor.shutdown(wait=False)
    known_songs._songs.clear()
    helpers._pending_broadcasts.clear()


@pytest.fixture
def guild_data(bot):
    return bot.get_guild_data(GUILD_ID)
