import asyncio
import random
from types import SimpleNamespace

import pytest

import services.queue_service as queue_module
from tests.conftest import GUILD_ID, make_song


@pytest.fixture
def queue(bot):
    return bot.playback.queue_service


def urls(songs):
    return [s.webpage_url.rsplit("=", 1)[1] for s in songs]


def queue_songs(queue, *ids):
    for video_id in ids:
        queue.add_song_to_queue(GUILD_ID, make_song(video_id))


async def advance(queue, guild_data):
    """Finish the current song and start the next one, as both playback paths do."""
    song = await queue.get_next_song(GUILD_ID)
    guild_data["current"] = song
    return song


def title(song):
    return song.webpage_url.rsplit("=", 1)[1]


# Loop modes and shuffle


async def test_song_loop_returns_a_copy_of_current_without_consuming_the_queue(queue, guild_data):
    queue_songs(queue, "a", "b")
    guild_data["current"] = make_song("x")
    guild_data["loop_mode"] = "song"

    song = await queue.get_next_song(GUILD_ID)

    assert song is not guild_data["current"]
    assert song.webpage_url == guild_data["current"].webpage_url
    assert urls(guild_data["queue"]) == ["a", "b"]
    assert guild_data["played"] == []


async def test_a_skip_leaves_song_loop_once(bot, queue, guild_data):
    queue_songs(queue, "a")
    guild_data["current"] = make_song("x")
    guild_data["loop_mode"] = "song"
    bot.playback.request_advance(guild_data)

    assert urls([await queue.get_next_song(GUILD_ID)]) == ["a"]
    guild_data["current"] = make_song("a")
    assert urls([await queue.get_next_song(GUILD_ID)]) == ["a"]
    assert guild_data["loop_mode"] == "song"


async def test_played_songs_stay_in_the_queue_above_the_current_one(queue, guild_data):
    queue_songs(queue, "a", "b", "c")

    for _ in range(3):
        await advance(queue, guild_data)

    assert urls(guild_data["played"]) == ["a", "b"]
    assert title(guild_data["current"]) == "c"
    assert guild_data["queue"] == []


async def test_queue_loop_starts_again_from_the_first_played_song(queue, guild_data):
    queue_songs(queue, "a", "b", "c")
    guild_data["loop_mode"] = "queue"
    played = [await advance(queue, guild_data) for _ in range(3)]

    restarted = await advance(queue, guild_data)

    assert urls(played) == ["a", "b", "c"]
    assert urls([restarted, *guild_data["queue"]]) == ["a", "b", "c"]
    assert guild_data["played"] == []


async def test_queue_loop_shuffles_the_next_round(queue, guild_data, monkeypatch):
    queue_songs(queue, *"abcdef")
    guild_data["loop_mode"] = "queue"
    guild_data["shuffle"] = True
    guild_data["played"] = guild_data["queue"]
    guild_data["queue"] = []
    monkeypatch.setattr(queue_module, "random", random.Random(4))

    first = await queue.get_next_song(GUILD_ID)

    assert sorted(urls([first, *guild_data["queue"]])) == list("abcdef")
    assert urls([first, *guild_data["queue"]]) != list("abcdef")


async def test_loop_off_returns_none_at_the_end_and_keeps_the_last_song_as_played(queue, guild_data):
    queue_songs(queue, "a")
    await advance(queue, guild_data)

    assert await queue.get_next_song(GUILD_ID) is None
    assert urls(guild_data["played"]) == ["a"]
    # Until playback is reset, the song that just ended is still the current one, not a played one.
    assert queue.played_before(guild_data) == []


async def test_visible_queue_in_queue_loop_continues_with_the_next_round(queue, guild_data):
    queue_songs(queue, "a", "b", "c")
    guild_data["loop_mode"] = "queue"
    await advance(queue, guild_data)
    await advance(queue, guild_data)

    assert urls(queue.get_visible_queue(GUILD_ID)) == ["c", "a"]


async def test_clear_queue_cancels_imports_and_clears_played_songs(queue, guild_data):
    queue_songs(queue, "a", "b")
    await advance(queue, guild_data)
    await advance(queue, guild_data)
    task = asyncio.create_task(asyncio.sleep(10))
    guild_data["import_tasks"] = {task}

    queue.clear_queue(GUILD_ID)
    await asyncio.sleep(0)

    assert guild_data["queue"] == [] and guild_data["played"] == []
    assert task.cancelled()


# Skipping ahead and going back


async def test_skip_ahead_counts_the_current_and_jumped_over_songs_as_played(queue, guild_data):
    guild_data["current"] = make_song("x")
    queue_songs(queue, "a", "b", "c")

    target = queue.skip_ahead(guild_data, 2)

    assert title(target) == "c"
    assert urls(guild_data["played"]) == ["x", "a", "b"]
    assert urls(guild_data["queue"]) == ["c"]
    # The current song is recorded once, though the advance that follows sees it again.
    await advance(queue, guild_data)
    assert urls(guild_data["played"]) == ["x", "a", "b"]


async def test_previous_returns_the_song_before_and_queues_the_current_one_next(queue, guild_data):
    guild_data["played"] = [make_song(v) for v in "abc"]
    guild_data["current"] = make_song("x")
    queue_songs(queue, "y")

    target = queue.step_back(guild_data)

    assert title(target) == "c"
    assert urls(guild_data["played"]) == ["a", "b"]
    assert urls(guild_data["queue"]) == ["x", "y"]


async def test_going_back_to_a_played_song_keeps_the_queue_order(queue, guild_data):
    guild_data["played"] = [make_song(v) for v in "abc"]
    guild_data["current"] = make_song("x")
    queue_songs(queue, "y")

    target = queue.step_back(guild_data, 0)

    assert title(target) == "a"
    assert guild_data["played"] == []
    assert urls(guild_data["queue"]) == ["b", "c", "x", "y"]


async def test_previous_skips_a_current_song_that_just_ended(queue, guild_data):
    queue_songs(queue, "a", "b")
    await advance(queue, guild_data)
    await advance(queue, guild_data)
    assert await queue.get_next_song(GUILD_ID) is None  # b ended and nothing follows

    target = queue.step_back(guild_data)

    assert title(target) == "a"
    assert urls(guild_data["queue"]) == ["b"]
    assert guild_data["played"] == []


async def test_previous_falls_back_to_history_when_nothing_was_played(queue, guild_data):
    for video_id in "ABC":
        queue.add_to_history(GUILD_ID, make_song(video_id))
    guild_data["current"] = make_song("X")

    guild_data["current"] = queue.step_back(guild_data)
    assert title(guild_data["current"]) == "C"
    guild_data["current"] = queue.step_back(guild_data)

    assert title(guild_data["current"]) == "B"
    assert urls(guild_data["queue"]) == ["C", "X"]


async def test_previous_from_history_right_after_the_only_song_ended_keeps_it_once(queue, guild_data):
    queue.add_to_history(GUILD_ID, make_song("A"))
    queue_songs(queue, "x")
    await advance(queue, guild_data)
    queue.add_to_history(GUILD_ID, guild_data["current"])
    assert await queue.get_next_song(GUILD_ID) is None  # x ended; autoplay may still be searching

    target = queue.step_back(guild_data)

    assert title(target) == "A"
    assert guild_data["played"] == []
    assert urls(guild_data["queue"]) == ["x"]


async def test_a_played_song_index_never_falls_back_to_history(queue, guild_data):
    queue.add_to_history(GUILD_ID, make_song("A"))
    guild_data["current"] = make_song("X")

    assert queue.step_back(guild_data, 0) is None
    assert queue.has_previous(guild_data)


async def test_nothing_before_the_first_song(queue, guild_data):
    guild_data["current"] = make_song("X")

    assert queue.step_back(guild_data) is None
    assert not queue.has_previous(guild_data)


# Played songs


async def test_re_adding_a_played_song_moves_it_to_the_upcoming_songs(queue, guild_data):
    queue_songs(queue, "a", "b")
    await advance(queue, guild_data)
    await advance(queue, guild_data)

    queue_songs(queue, "a")

    assert guild_data["played"] == []
    assert urls(guild_data["queue"]) == ["a"]


async def test_mark_played_keeps_the_order_and_each_song_once(queue, guild_data):
    queue.mark_played(guild_data, [make_song("a"), make_song("b")])
    queue.mark_played(guild_data, [make_song("a"), make_song("c")])

    assert urls(guild_data["played"]) == ["a", "b", "c"]


async def test_played_songs_are_trimmed_outside_queue_loop(queue, guild_data, monkeypatch):
    monkeypatch.setattr(queue_module, "PLAYED_LIMIT", 3)

    queue.mark_played(guild_data, [make_song(v) for v in "abcde"])

    assert urls(guild_data["played"]) == ["c", "d", "e"]


async def test_played_songs_are_kept_in_queue_loop(queue, guild_data, monkeypatch):
    monkeypatch.setattr(queue_module, "PLAYED_LIMIT", 2)
    guild_data["loop_mode"] = "queue"

    queue.mark_played(guild_data, [make_song(v) for v in "abcd"])

    assert urls(guild_data["played"]) == list("abcd")


async def test_removing_a_played_song(queue, guild_data):
    guild_data["played"] = [make_song(v) for v in "ab"]

    assert title(queue.remove_played(GUILD_ID, 0)) == "a"
    assert urls(guild_data["played"]) == ["b"]
    assert queue.remove_played(GUILD_ID, 5) is None


async def test_an_unplayable_song_stays_as_played_and_ends_song_loop(bot, guild_data):
    guild_data["loop_mode"] = "song"

    assert bot.playback._song_failed(GUILD_ID, make_song("a")) is True

    assert guild_data["loop_mode"] == "off"
    assert urls(guild_data["played"]) == ["a"]
    assert guild_data["history"] == []


# Saving and restoring


async def test_played_songs_are_saved_and_restored(bot, queue, guild_data):
    queue_songs(queue, "a", "b", "c")
    await advance(queue, guild_data)
    await advance(queue, guild_data)
    saved = bot._queue_payload(guild_data)

    restored = bot.get_guild_data(GUILD_ID + 1)
    bot._restore_queue(restored, saved)

    assert urls(restored["played"]) == ["a"]
    # The song that was playing goes back in front, to start where it was.
    assert urls(restored["queue"]) == ["b", "c"]


async def test_a_queue_loop_saved_by_the_old_version_keeps_its_played_songs(bot):
    old = {
        "queue": [make_song("c").to_dict()],
        "loop_backup": [make_song(v).to_dict() for v in "abc"],
        "current": make_song("b").to_dict(),
        "loop_mode": "queue",
    }
    restored = bot.get_guild_data(GUILD_ID)

    bot._restore_queue(restored, old)

    assert urls(restored["played"]) == ["a"]
    assert urls(restored["queue"]) == ["b", "c"]


# Listening history


async def test_replayed_song_moves_to_most_recent_end(queue, guild_data):
    for video_id in ["A", "B", "C", "A"]:
        queue.add_to_history(GUILD_ID, make_song(video_id))

    assert urls(guild_data["history"]) == ["B", "C", "A"]


async def test_history_is_capped(queue, guild_data, monkeypatch):
    monkeypatch.setattr(queue_module, "MAX_HISTORY_SIZE", 3)

    for video_id in "ABCDE":
        queue.add_to_history(GUILD_ID, make_song(video_id))

    assert urls(guild_data["history"]) == ["C", "D", "E"]


# Durations (#118, #116)


async def test_queue_duration_follows_direct_list_changes(queue, guild_data):
    queue_songs(queue, "a")
    assert queue.get_queue_duration(GUILD_ID) == 180

    guild_data["queue"].insert(0, make_song("b", duration=60))

    assert queue.get_queue_duration(GUILD_ID) == 240


async def test_wait_time_uses_the_playing_source_speed_for_the_current_song(bot, queue, guild_data):
    import time

    guild_data["voice_client"] = SimpleNamespace(is_playing=lambda: True, is_paused=lambda: False)
    guild_data["current"] = make_song("x", duration=100)
    guild_data["start_time"] = time.monotonic()
    guild_data["source_speed"] = 1.0
    queue_songs(queue, "a")
    guild_data["speed"] = 2.0  # applies to the next song only

    assert queue.get_estimated_wait_time(GUILD_ID, 2) == 100 + 90
