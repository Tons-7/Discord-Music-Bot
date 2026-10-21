import asyncio

import pytest

from activity import helpers, known_songs, rate_limit, tasks
from activity.cog_hooks import install_broadcast_hooks
from bot import MusicBot
from models.song import Song
from services.queue_service import cancel_imports

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


@pytest.fixture(autouse=True)
def fresh_rate_limits():
    """Every test is a new user session as far as the per-user request limits go."""
    rate_limit._hits.clear()
    yield
    rate_limit._hits.clear()


@pytest.fixture
def ws():
    return FakeWsManager()


@pytest.fixture
async def bot(tmp_path, monkeypatch, ws):
    # MusicBot creates music_bot.db and audio_cache/ in the working directory, so each test gets its own
    # database: queue saves and restores run for real.
    monkeypatch.chdir(tmp_path)
    bot = MusicBot()
    bot.loop = asyncio.get_running_loop()
    install_broadcast_hooks(bot, ws)

    yield bot

    for guild_data in bot.guilds_data.values():
        cancel_imports(guild_data)
    pending = [*bot.db_save_tasks.values(), *bot._background_tasks]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    await tasks.cancel_all()
    for session in (bot._http_session, bot._stream_session):
        if session is not None:
            await session.close()
    bot.executor.shutdown(wait=False)
    bot.db_executor.shutdown(wait=True)
    bot.import_executor.shutdown(wait=False)
    bot.download_executor.shutdown(wait=False)
    known_songs.clear()
    helpers._pending_broadcasts.clear()
    helpers._sent_lists.clear()
    helpers._end_watchers.clear()


@pytest.fixture
def guild_data(bot):
    return bot.get_guild_data(GUILD_ID)
