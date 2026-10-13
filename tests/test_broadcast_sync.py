from types import SimpleNamespace

from activity.helpers import broadcast_state
from tests.conftest import GUILD_ID, make_song, settle


async def test_saving_the_queue_broadcasts_state(bot, ws, guild_data):
    guild_data["volume"] = 37
    await bot.save_guild_queue(GUILD_ID)
    await settle()

    assert len(ws.sent) == 1
    event_type, state = ws.sent[0]
    assert event_type == "STATE_UPDATE"
    assert state["volume"] == 37


async def test_rapid_triggers_coalesce_into_one_snapshot(bot, ws, guild_data):
    for volume in (10, 20, 30):
        guild_data["volume"] = volume
        await bot.save_guild_queue(GUILD_ID)
    await broadcast_state(bot, ws, GUILD_ID)
    await settle()

    assert [state["volume"] for _, state in ws.sent] == [30]


async def test_change_after_a_snapshot_is_broadcast_too(bot, ws, guild_data):
    guild_data["loop_mode"] = "song"
    await bot.save_guild_queue(GUILD_ID)
    await settle()
    guild_data["loop_mode"] = "queue"
    await bot.save_guild_queue(GUILD_ID)
    await settle()

    assert [state["loop_mode"] for _, state in ws.sent] == ["song", "queue"]


async def test_completed_slash_command_broadcasts_state(bot, ws, guild_data):
    guild_data["queue"].append(make_song("a"))
    bot.dispatch("app_command_completion", SimpleNamespace(guild_id=GUILD_ID), None)
    await settle()

    assert len(ws.sent) == 1
    assert len(ws.sent[0][1]["queue"]) == 1


async def test_voice_disconnect_broadcasts_state(bot, ws):
    bot._connection.user = SimpleNamespace(id=999)
    guild = SimpleNamespace(id=GUILD_ID)
    member = SimpleNamespace(id=999, guild=guild)
    before = SimpleNamespace(channel=SimpleNamespace(guild=guild))
    after = SimpleNamespace(channel=None)

    bot.dispatch("voice_state_update", member, before, after)
    await settle()

    assert len(ws.sent) == 1
    assert ws.sent[0][1]["is_connected"] is False
