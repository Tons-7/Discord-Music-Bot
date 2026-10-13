import asyncio
import threading

from cogs.music_commands import MusicCommands
from cogs.playlist_commands import PlaylistCommands
from tests.conftest import GUILD_ID, make_song

GUILD_ONLY = [0]


async def test_slash_commands_are_synced_as_guild_only(bot):
    await bot.add_cog(MusicCommands(bot))
    await bot.add_cog(PlaylistCommands(bot))

    payload = [command.to_dict(bot.tree) for command in bot.tree._get_all_commands(guild=None)]

    assert payload
    assert all(command["contexts"] == GUILD_ONLY for command in payload)


async def test_audio_thread_records_history_on_the_loop_before_play_next(bot, guild_data):
    queue_service = bot._playback_service.queue_service
    record_history = queue_service.add_to_history
    events = []

    def add_to_history(guild_id, song):
        events.append(("history", threading.current_thread() is threading.main_thread()))
        record_history(guild_id, song)

    async def play_next():
        events.append(("play_next", True))

    def audio_thread_callback():
        bot.loop.call_soon_threadsafe(add_to_history, GUILD_ID, make_song("finished"))
        asyncio.run_coroutine_threadsafe(play_next(), bot.loop)

    thread = threading.Thread(target=audio_thread_callback)
    thread.start()
    thread.join()
    await asyncio.sleep(0.05)

    assert events == [("history", True), ("play_next", True)]
    assert guild_data["history"][-1].webpage_url.endswith("finished")


async def test_database_queries_do_not_wait_for_extractions(bot):
    release = threading.Event()
    extractions = [
        bot.loop.run_in_executor(bot.executor, release.wait)
        for _ in range(bot.executor._max_workers)
    ]

    try:
        rows = await asyncio.wait_for(bot.fetch_db_query("SELECT 1"), timeout=1)
    finally:
        release.set()
        await asyncio.gather(*extractions)

    assert rows == [(1,)]
