import time
from types import SimpleNamespace

import pytest

from activity.helpers import set_current_for_activity
from activity.routes.playback_routes import pause, resume
from activity.state_serializer import _get_activity_position
from tests.conftest import GUILD_ID, USER, make_song


class Clock:
    """Stands in for time.monotonic so tests can move time forward instantly."""

    def __init__(self):
        self._real = time.monotonic
        self._offset = 0.0

    def __call__(self):
        return self._real() + self._offset

    def advance(self, seconds: float):
        self._offset += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock)
    return clock


def position(bot, guild_data):
    return _get_activity_position(bot, GUILD_ID, guild_data)


async def test_activity_position_follows_elapsed_time(bot, guild_data, clock):
    set_current_for_activity(guild_data, make_song("a"))

    clock.advance(30)

    assert position(bot, guild_data) == 30


async def test_speed_scales_elapsed_time(bot, guild_data, clock):
    guild_data["speed"] = 2.0
    set_current_for_activity(guild_data, make_song("a"))

    clock.advance(10)

    assert position(bot, guild_data) == 20


async def test_pause_freezes_position_and_resume_continues(bot, ws, guild_data, clock):
    set_current_for_activity(guild_data, make_song("a"))
    clock.advance(30)

    await pause(GUILD_ID, user=USER, bot=bot, ws=ws)
    clock.advance(100)
    assert position(bot, guild_data) == 30

    await resume(GUILD_ID, user=USER, bot=bot, ws=ws)
    clock.advance(5)
    assert position(bot, guild_data) == 35


async def test_voice_position_follows_elapsed_time(bot, guild_data, clock):
    guild_data["voice_client"] = SimpleNamespace(
        is_playing=lambda: True, is_paused=lambda: False, is_connected=lambda: True,
    )
    guild_data["seek_offset"] = 60
    guild_data["start_time"] = time.monotonic()

    clock.advance(12)

    assert bot._playback_service.get_current_position(GUILD_ID) == 72
