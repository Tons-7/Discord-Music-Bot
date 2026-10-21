import asyncio
import threading

from cogs.music_commands import MusicCommands
from cogs.playlist_commands import PlaylistCommands

GUILD_ONLY = [0]


async def test_slash_commands_are_synced_as_guild_only(bot):
    await bot.add_cog(MusicCommands(bot))
    await bot.add_cog(PlaylistCommands(bot))

    payload = [command.to_dict(bot.tree) for command in bot.tree._get_all_commands(guild=None)]

    assert payload
    assert all(command["contexts"] == GUILD_ONLY for command in payload)


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
