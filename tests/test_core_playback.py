import asyncio
import shlex
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import services.playback_service as playback_module
from services.music_service import ExtractionError
from services.playback_service import PlaybackError
from tests.conftest import GUILD_ID, make_song


class FakeSource(discord.AudioSource):
    def __init__(self, url, before_options="", options=""):
        self.url = url
        self.before_options = before_options
        self.options = options
        self.cleaned = False

    def read(self):
        return b""

    def cleanup(self):
        self.cleaned = True


class FakeVoiceClient:
    """Stands in for discord.VoiceClient: play() keeps the after callback, stop() fires it like the player thread."""

    def __init__(self, channel=None):
        self.channel = channel or SimpleNamespace(members=[], id=5, mention="#music")
        self.connected = True
        self.playing = False
        self.paused = False
        self.after = None
        self.played = []

    def is_connected(self):
        return self.connected

    def is_playing(self):
        return self.playing and not self.paused

    def is_paused(self):
        return self.playing and self.paused

    def play(self, source, *, after=None, bitrate=None):
        if not self.connected:
            raise discord.ClientException("Not connected to voice.")
        self.playing, self.paused = True, False
        self.after = after
        self.played.append(source)

    def finish(self, error=None):
        """The song ends (or the player aborts) on the audio thread."""
        after, self.after = self.after, None
        self.playing = False
        thread = threading.Thread(target=after, args=(error,))
        thread.start()
        thread.join()

    def stop(self):
        if self.playing:
            self.finish()

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    async def disconnect(self, *, force=False):
        self.connected = False
        self.stop()


async def settle(rounds: int = 5):
    for _ in range(rounds):
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)


@pytest.fixture(autouse=True)
def no_cache_downloads(bot, monkeypatch):
    """Songs that start would otherwise launch real yt-dlp cache downloads."""
    monkeypatch.setattr(bot.audio_cache, "ensure_cached", AsyncMock())


@pytest.fixture
def voice(guild_data, monkeypatch):
    monkeypatch.setattr(discord, "FFmpegPCMAudio", FakeSource)
    vc = FakeVoiceClient()
    guild_data["voice_client"] = vc
    return vc


@pytest.fixture
def playback(bot):
    return bot.playback


@pytest.fixture
def streams(playback, monkeypatch):
    """get_song_info returns a stream for every YouTube URL; tests set .errors[url] to make one fail."""
    calls = []
    errors = {}

    async def fake_get_song_info(target, *, raise_errors=False):
        calls.append(target)
        if target in errors:
            raise errors[target]
        return {
            "url": f"https://stream.example/{target.rsplit('=', 1)[-1]}", "webpage_url": target,
            "title": f"Fresh {target.rsplit('=', 1)[-1]}", "duration": 200,
            "http_headers": {"User-Agent": "yt-dlp UA", "Accept": "*/*"},
        }

    monkeypatch.setattr(playback.music_service, "get_song_info", fake_get_song_info)
    return SimpleNamespace(calls=calls, errors=errors)


@pytest.fixture
def notices(bot, monkeypatch):
    sent = []

    class Channel:
        async def send(self, embed=None, delete_after=None, **_):
            sent.append(embed.title if embed else None)

    panel = bot.playback.panel
    monkeypatch.setattr(panel, "music_channel", lambda guild_id: Channel())
    monkeypatch.setattr(panel, "refresh", AsyncMock())
    return sent


@pytest.fixture
def stats(bot, monkeypatch):
    rows = []

    async def record(user_id, guild_id, song, seconds):
        rows.append((user_id, song.webpage_url.rsplit("=", 1)[1], seconds))

    monkeypatch.setattr(bot, "record_listening_stat", record)
    return rows


def member(user_id, *, bot=False, deaf=False):
    return SimpleNamespace(id=user_id, bot=bot, voice=SimpleNamespace(deaf=deaf, self_deaf=False))


# FFmpeg options


def test_seek_keeps_the_reconnect_options_and_passes_yt_dlp_headers(bot, guild_data):
    options = bot.playback.build_ffmpeg_options(
        guild_data, seek=90, headers={"User-Agent": "Mozilla/5.0 (X11)", "Accept": "*/*"},
    )

    args = shlex.split(options["before_options"])
    assert args[:len(shlex.split(bot.ffmpeg_options["before_options"]))] == shlex.split(
        bot.ffmpeg_options["before_options"])
    assert args[args.index("-headers") + 1] == "User-Agent: Mozilla/5.0 (X11)\r\nAccept: */*\r\n"
    assert args[-2:] == ["-ss", "90"]
    assert "-user_agent" not in args and "-rw_timeout" in args


def test_local_files_get_no_network_options(bot, guild_data):
    guild_data["speed"] = 1.5
    guild_data["audio_effect"] = "nightcore"

    options = bot.playback.build_ffmpeg_options(guild_data, local_file=True, seek=12)

    assert options["before_options"] == "-nostdin -ss 12"
    assert 'atempo=1.5000,aresample=48000,asetrate=48000*1.25' in options["options"]


def test_old_options_builder_still_works(bot, guild_data):
    assert bot.playback._build_ffmpeg_options(guild_data) == bot.playback.build_ffmpeg_options(
        guild_data)


# The real after_playing callback (#98)


async def test_audio_thread_records_history_on_the_loop_before_play_next(bot, playback, guild_data, voice, monkeypatch):
    events = []
    record = playback.queue_service.add_to_history

    def add_to_history(guild_id, song):
        events.append(("history", threading.current_thread() is threading.main_thread()))
        record(guild_id, song)

    async def play_next(guild_id):
        events.append(("play_next", True))

    monkeypatch.setattr(playback.queue_service, "add_to_history", add_to_history)
    monkeypatch.setattr(playback, "play_next", play_next)
    song = make_song("a")
    song.url = "https://stream.example/a"
    assert await playback._start_playback(GUILD_ID, song)

    voice.finish()
    await settle()

    assert events == [("history", True), ("play_next", True)]
    assert guild_data["history"][-1].webpage_url == song.webpage_url


async def test_a_seek_replacing_the_player_ignores_the_old_callback(bot, playback, guild_data, voice, streams):
    song = make_song("a")
    song.url = "https://stream.example/a"
    await playback._start_playback(GUILD_ID, song)
    old_after = voice.after

    await playback.seek(GUILD_ID, 60)
    threading.Thread(target=old_after, args=(None,)).start()
    await settle()

    assert guild_data["history"] == []
    assert voice.is_playing() and guild_data["seek_offset"] == 60
    assert shlex.split(voice.played[-1].original.before_options)[-2:] == ["-ss", "60"]


async def test_callback_while_seeking_does_nothing(bot, playback, guild_data, voice, monkeypatch):
    calls = []
    monkeypatch.setattr(playback, "play_next", AsyncMock(side_effect=lambda gid: calls.append(gid)))
    song = make_song("a")
    song.url = "https://stream.example/a"
    await playback._start_playback(GUILD_ID, song)
    guild_data["seeking"] = True

    voice.finish()
    await settle()

    assert guild_data["history"] == [] and calls == []


async def test_leaving_while_a_song_starts_keeps_that_song_next(bot, playback, guild_data, voice, monkeypatch):
    for video_id in "ab":
        playback.queue_service.add_song_to_queue(GUILD_ID, make_song(video_id))
    starting = asyncio.Event()

    async def slow_stream(song):
        starting.set()
        await asyncio.sleep(0.2)
        return {"url": "https://stream.example/" + song.webpage_url[-1]}

    monkeypatch.setattr(playback, "_fresh_stream", slow_stream)
    task = asyncio.create_task(playback.play_next(GUILD_ID))
    await starting.wait()

    await playback.leave(GUILD_ID, clear_queue=False)
    await task

    assert [s.webpage_url[-1] for s in guild_data["queue"]] == ["a", "b"]
    assert guild_data["current"] is None


async def test_a_song_that_ends_while_a_failed_seek_prepares_still_moves_on(bot, playback, guild_data, voice, monkeypatch):
    calls = []
    monkeypatch.setattr(playback, "play_next", AsyncMock(side_effect=lambda gid: calls.append(gid)))
    song = make_song("a")
    song.url = "https://stream.example/a"
    await playback._start_playback(GUILD_ID, song)

    async def ends_then_fails(_song):
        voice.finish()
        await settle()
        raise PlaybackError("no stream")

    monkeypatch.setattr(playback, "_fresh_stream", ends_then_fails)

    with pytest.raises(PlaybackError):
        await playback._replay_current(GUILD_ID, 30, fresh=True)
    await settle()

    assert calls == [GUILD_ID]
    assert [s.title for s in guild_data["history"]] == ["Song a"]


# Voice drops (#251, #113)


async def test_player_ending_while_voice_is_down_keeps_the_song_to_resume(bot, playback, guild_data, voice, stats):
    song = make_song("a")
    song.url = "https://stream.example/a"
    await playback._start_playback(GUILD_ID, song)
    guild_data["start_time"] -= 30

    voice.connected = False
    voice.finish()
    await settle()

    assert guild_data["current"] is song
    assert guild_data["history"] == [] and stats == []
    assert 29 <= guild_data["interrupted"]["position"] <= 31


async def test_reconnect_resumes_an_interrupted_song_where_it_stopped(bot, playback, guild_data, voice, streams):
    song = make_song("a")
    guild_data["current"] = song
    guild_data["interrupted"] = {"position": 42}

    await playback.resume_after_reconnect(GUILD_ID)

    assert voice.is_playing()
    assert shlex.split(voice.played[-1].original.before_options)[-2:] == ["-ss", "42"]
    assert "interrupted" not in guild_data and guild_data["seek_offset"] == 42


async def test_reconnect_with_a_living_player_moves_the_clock_past_the_outage(bot, playback, guild_data, voice):
    voice.playing = True
    guild_data["current"] = make_song("a")
    guild_data["start_time"] = time.monotonic() - 50
    guild_data["voice_lost_at"] = time.monotonic() - 20

    await playback.resume_after_reconnect(GUILD_ID)

    assert 29 <= playback.get_current_position(GUILD_ID) <= 31
    assert voice.played == []


async def test_voice_down_at_a_song_change_puts_the_song_back(bot, playback, guild_data, voice, streams, monkeypatch):
    watched = []
    monkeypatch.setattr(bot, "watch_voice", lambda guild_id, channel=None: watched.append(guild_id))
    monkeypatch.setattr(playback, "_wait_for_voice", AsyncMock(return_value=True))
    playback.queue_service.add_song_to_queue(GUILD_ID, make_song("a"))
    playback.queue_service.add_song_to_queue(GUILD_ID, make_song("b"))
    voice.connected = False

    await playback.play_next(GUILD_ID)

    assert [s.title for s in guild_data["queue"]] == ["Fresh a", "Song b"]
    assert guild_data["history"] == [] and watched == [GUILD_ID]


# Retries (#114, #258)


async def test_an_unavailable_video_is_skipped_after_one_extraction(bot, playback, guild_data, voice, streams, notices):
    playback.queue_service.add_song_to_queue(GUILD_ID, make_song("gone"))
    playback.queue_service.add_song_to_queue(GUILD_ID, make_song("ok"))
    streams.errors[make_song("gone").webpage_url] = ExtractionError("Video unavailable", permanent=True)

    await playback.play_next(GUILD_ID)

    assert streams.calls == [make_song("gone").webpage_url, make_song("ok").webpage_url]
    assert guild_data["current"].webpage_url.endswith("ok")
    assert notices == ["Song Skipped"]


async def test_the_bot_check_stops_playback_and_keeps_the_queue(bot, playback, guild_data, voice, streams, notices):
    for video_id in "abc":
        playback.queue_service.add_song_to_queue(GUILD_ID, make_song(video_id))
        streams.errors[make_song(video_id).webpage_url] = ExtractionError("Sign in to confirm", bot_check=True)

    await playback.play_next(GUILD_ID)

    assert len(streams.calls) == 1
    assert [s.webpage_url[-1] for s in guild_data["queue"]] == ["a", "b", "c"]
    assert guild_data["current"] is None and notices == ["Playback Stopped"]


async def test_transient_failures_stop_after_a_few_songs(bot, playback, guild_data, voice, streams, notices):
    for video_id in "abcdef":
        playback.queue_service.add_song_to_queue(GUILD_ID, make_song(video_id))
        streams.errors[make_song(video_id).webpage_url] = ExtractionError("HTTP Error 503")

    await playback.play_next(GUILD_ID)

    assert len(streams.calls) == playback_module.MAX_TRANSIENT_FAILURES
    assert notices == ["Playback Stopped", "Song Skipped"]


async def test_a_dropped_stream_is_reopened_once_at_the_same_spot(bot, playback, guild_data, voice, streams):
    song = make_song("a")
    song.url = "https://stream.example/a"
    await playback._start_playback(GUILD_ID, song)
    guild_data["start_time"] -= 40

    voice.finish(discord.FFmpegProcessError("FFmpeg exited with code 1"))
    await settle(20)

    assert len(voice.played) == 2
    assert 39 <= int(shlex.split(voice.played[-1].original.before_options)[-1]) <= 41
    assert guild_data["history"] == []


async def test_a_stream_that_fails_at_once_is_reported_as_skipped(bot, playback, guild_data, voice, streams, notices):
    song = make_song("a")
    song.url = "https://stream.example/a"
    await playback._start_playback(GUILD_ID, song)
    guild_data["stream_retry"] = song.webpage_url

    voice.finish(discord.FFmpegProcessError("FFmpeg exited with code 1"))
    await settle(20)

    assert notices[0] == "Song Skipped"


async def test_playback_waits_for_youtubes_preroll_delay(playback, monkeypatch):
    waits = []
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(side_effect=lambda s: waits.append(round(s))))

    await playback._wait_until_available({"available_at": time.time() + 7})
    await playback._wait_until_available({"available_at": time.time() - 7})

    assert waits == [7]


# Stop and leave (#208)


async def test_stop_during_an_extraction_keeps_the_song_from_starting(bot, playback, guild_data, voice, monkeypatch):
    release = asyncio.Event()

    async def slow_get_song_info(target, *, raise_errors=False):
        await release.wait()
        return {"url": "https://stream.example/b", "webpage_url": target, "title": "B"}

    monkeypatch.setattr(playback.music_service, "get_song_info", slow_get_song_info)
    playback.queue_service.add_song_to_queue(GUILD_ID, make_song("b"))
    starting = asyncio.create_task(playback.play_next(GUILD_ID))
    await settle()

    await playback.stop(GUILD_ID)
    release.set()
    await starting

    assert guild_data["current"] is None and voice.played == []


async def test_stop_records_the_playing_song(bot, playback, guild_data, voice, stats):
    voice.channel.members = [member(1), member(2, bot=True), member(3, deaf=True)]
    song = make_song("a")
    song.url = "https://stream.example/a"
    await playback._start_playback(GUILD_ID, song)
    guild_data["listen_start"] -= 50

    await playback.stop(GUILD_ID)
    await settle()

    assert [s.webpage_url for s in guild_data["history"]] == [song.webpage_url]
    assert [(uid, sec // 10) for uid, _, sec in stats] == [(1, 5)]
    assert not voice.is_playing() and guild_data["autoplay"] is False


async def test_leave_keeping_the_queue_puts_the_song_back_with_its_position(bot, playback, guild_data, voice):
    song = make_song("a")
    song.url = "https://stream.example/a"
    await playback._start_playback(GUILD_ID, song)
    guild_data["start_time"] -= 75
    playback.queue_service.add_song_to_queue(GUILD_ID, make_song("b"))

    await playback.leave(GUILD_ID, clear_queue=False)

    assert [s.webpage_url[-1] for s in guild_data["queue"]] == ["a", "b"]
    assert 74 <= guild_data["resume_at"]["position"] <= 76
    assert guild_data["voice_client"] is None and not voice.connected


async def test_a_parked_song_starts_at_its_saved_position(bot, playback, guild_data, voice, streams):
    guild_data["queue"] = [make_song("a")]
    guild_data["resume_at"] = {"url": make_song("a").webpage_url, "position": 75}

    await playback.play_next(GUILD_ID)

    assert shlex.split(voice.played[-1].original.before_options)[-2:] == ["-ss", "75"]
    assert guild_data["seek_offset"] == 75


async def test_skip_moves_on_in_song_loop(bot, playback, guild_data, voice, streams):
    song = make_song("a")
    song.url = "https://stream.example/a"
    await playback._start_playback(GUILD_ID, song)
    guild_data["loop_mode"] = "song"
    playback.queue_service.add_song_to_queue(GUILD_ID, make_song("b"))

    assert await playback.skip(GUILD_ID) is song
    await settle(20)

    assert guild_data["current"].webpage_url.endswith("b")
    assert guild_data["loop_mode"] == "song"


async def test_voice_skip_to_keeps_the_passed_songs_as_played(bot, playback, guild_data, voice, streams):
    song = make_song("x")
    song.url = "https://stream.example/x"
    await playback._start_playback(GUILD_ID, song)
    for video_id in "abc":
        playback.queue_service.add_song_to_queue(GUILD_ID, make_song(video_id))

    target = await playback.skip_to(GUILD_ID, 2)
    await settle(20)

    assert target.webpage_url.endswith("c")
    assert guild_data["current"].webpage_url.endswith("c")
    assert [s.webpage_url[-1] for s in guild_data["played"]] == ["x", "a", "b"]


async def test_voice_previous_goes_back_up_the_queue(bot, playback, guild_data, voice, streams):
    for video_id in "ab":
        playback.queue_service.add_song_to_queue(GUILD_ID, make_song(video_id))
    await playback.play_next(GUILD_ID)
    await playback.skip(GUILD_ID)
    await settle(20)
    assert guild_data["current"].webpage_url.endswith("b")

    previous = await playback.play_previous(GUILD_ID)
    await settle(20)

    assert previous.webpage_url.endswith("a")
    assert guild_data["current"].webpage_url.endswith("a")
    assert [s.webpage_url[-1] for s in guild_data["queue"]] == ["b"]
    assert guild_data["played"] == []


async def test_seek_on_a_livestream_is_refused(playback, guild_data, voice):
    guild_data["current"] = make_song("live", duration=0, is_live=True)

    with pytest.raises(PlaybackError, match="livestream"):
        await playback.seek(GUILD_ID, 30)


# Clock and stats (#116, #126, #125)


async def test_a_speed_change_does_not_rescale_the_playing_song(bot, playback, guild_data, voice):
    song = make_song("a")
    song.url = "https://stream.example/a"
    await playback._start_playback(GUILD_ID, song)
    guild_data["start_time"] -= 120

    guild_data["speed"] = 2.0

    assert 119 <= playback.get_current_position(GUILD_ID) <= 121


async def test_reapplying_filters_restarts_at_the_same_position(bot, playback, guild_data, voice, streams):
    song = make_song("a")
    song.url = "https://stream.example/a"
    await playback._start_playback(GUILD_ID, song)
    guild_data["start_time"] -= 60
    guild_data["audio_effect"] = "bass_boost"

    assert await playback.reapply_audio_filters(GUILD_ID)

    assert 59 <= guild_data["seek_offset"] <= 61
    assert "bass=g=10" in voice.played[-1].original.options


async def test_listening_time_spans_pauses_and_leaves_out_paused_time(bot, playback, guild_data, voice):
    song = make_song("a")
    song.url = "https://stream.example/a"
    await playback._start_playback(GUILD_ID, song)
    guild_data["listen_start"] -= 30
    playback.handle_pause(GUILD_ID)
    guild_data["listened"] += 0  # paused for a while: nothing accrues
    playback.handle_resume(GUILD_ID)
    guild_data["listen_start"] -= 20

    assert 49 <= playback.get_listened_seconds(guild_data) <= 51


async def test_a_moved_bot_credits_the_people_it_left_behind(bot, playback, guild_data, voice, stats):
    song = make_song("a")
    song.url = "https://stream.example/a"
    await playback._start_playback(GUILD_ID, song)
    guild_data["listen_start"] -= 40
    old_channel = SimpleNamespace(members=[member(7)])

    playback.credit_listeners(GUILD_ID, old_channel)
    await settle()

    assert [(uid, sec // 10) for uid, _, sec in stats] == [(7, 4)]
    assert playback.get_listened_seconds(guild_data) < 2


# Autoplay (#123, #128, #263, #365)


async def test_autoplay_passes_its_exclusions_to_the_search(bot, playback, guild_data, monkeypatch):
    seen = {}

    async def related(song, limit=1, exclude_urls=None, exclude=None):
        seen.update(urls=exclude_urls, exclude=exclude)
        return [{"webpage_url": "https://www.youtube.com/watch?v=new", "title": "New"}]

    monkeypatch.setattr(playback.music_service, "get_related_songs", related)
    playback.queue_service.add_to_history(GUILD_ID, make_song("old", title="Old Song (Official Video)"))

    pick = await playback.pick_autoplay_song(GUILD_ID, make_song("seed"))

    assert pick.title == "New" and pick.requested_by == "Autoplay"
    assert make_song("old").webpage_url in seen["urls"] and make_song("seed").webpage_url in seen["urls"]
    assert seen["exclude"]({"title": "old song", "webpage_url": "x"})


async def test_no_autoplay_prefetch_in_a_loop_mode(bot, playback, guild_data, voice):
    guild_data["autoplay"] = True
    guild_data["loop_mode"] = "song"
    song = make_song("a")
    song.url = "https://stream.example/a"

    await playback._start_playback(GUILD_ID, song)

    assert guild_data["autoplay_prefetch_task"] is None


async def test_autoplay_seeds_from_the_song_that_ended_even_if_current_is_cleared(bot, playback, guild_data, monkeypatch):
    seeds = []
    release = asyncio.Event()

    async def slow_prefetch():
        await release.wait()

    async def pick(guild_id, seed):
        seeds.append(seed)
        return make_song("next")

    monkeypatch.setattr(playback, "pick_autoplay_song", pick)
    guild_data["autoplay"] = True
    guild_data["current"] = make_song("ended")
    guild_data["autoplay_prefetch_task"] = asyncio.create_task(slow_prefetch())

    handling = asyncio.create_task(playback._handle_empty_queue(GUILD_ID))
    await settle()
    guild_data["current"] = None
    release.set()

    assert await handling is True
    assert seeds[0].webpage_url.endswith("ended")


# Now-playing panel (#129, #2026, #122, #133)


async def test_an_unreachable_panel_is_dropped_instead_of_retried(playback, guild_data):
    message = SimpleNamespace(edit=AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "Missing Access")))
    guild_data["now_playing_message"] = message
    guild_data["message_ready_for_timestamps"] = True

    assert not await playback._safe_message_edit(message, MagicMock(), guild_data)

    assert guild_data["now_playing_message"] is None and not guild_data["message_ready_for_timestamps"]


async def test_one_panel_edit_at_a_time_per_guild(bot, playback, guild_data, voice, monkeypatch):
    started = []
    release = asyncio.Event()

    async def slow_update(guild_id, current_time=None):
        started.append(guild_id)
        await release.wait()

    monkeypatch.setattr(playback, "_update_single_timestamp", slow_update)
    voice.playing = True
    guild_data.update(current=make_song("a"), now_playing_message=object(), message_ready_for_timestamps=True)

    await playback.update_timestamps_task()
    await playback.update_timestamps_task()
    await settle()
    release.set()
    await settle()

    assert started == [GUILD_ID]


async def test_giving_up_after_failures_removes_the_panel(bot, playback, guild_data, notices):
    message = SimpleNamespace(delete=AsyncMock())
    guild_data["now_playing_message"] = message
    guild_data["current"] = make_song("a")

    await playback._handle_max_retries_exceeded(GUILD_ID)

    message.delete.assert_awaited_once()
    assert guild_data["now_playing_message"] is None and guild_data["current"] is None


async def test_the_skip_notice_says_song_loop_was_turned_off(bot, playback, guild_data, notices, monkeypatch):
    texts = []
    monkeypatch.setattr(playback, "notify", AsyncMock(side_effect=lambda g, t, d, **k: texts.append(d)))
    guild_data["loop_mode"] = "song"

    await playback._handle_song_skip(GUILD_ID, make_song("a"))

    assert "Song loop was turned off" in texts[0] and guild_data["loop_mode"] == "off"


# Voice channel status


async def test_song_status_is_set_once_and_cleared(bot, playback, guild_data, voice, monkeypatch):
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.id = 77
    channel.edit = AsyncMock()
    voice.channel = channel
    monkeypatch.setattr(bot, "get_channel", lambda cid: channel if cid == 77 else None)
    song = make_song("a", uploader="Artist")

    playback._set_voice_status(GUILD_ID, song)
    await settle()
    playback._set_voice_status(GUILD_ID, song)
    await settle()
    playback.clear_voice_status(GUILD_ID)
    await settle()

    assert [c.kwargs["status"] for c in channel.edit.await_args_list] == ["\U0001f3b5 Song a — Artist", None]


# Imports (#366)


async def test_an_import_saves_in_batches(bot, playback, guild_data, monkeypatch):
    saves = []

    async def count_save(guild_id):
        saves.append(len(guild_data["queue"]))

    async def songs():
        for i in range(25):
            yield {"webpage_url": f"https://www.youtube.com/watch?v={i}", "title": str(i)}

    monkeypatch.setattr(bot, "save_guild_queue", count_save)

    added, skipped = await playback._queue_as_found(GUILD_ID, songs(), "<@1>", AsyncMock())

    assert (added, skipped) == (25, 0)
    assert saves[0] == 1 and saves[-1] == 25 and len(saves) <= 5
