import asyncio
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from fastapi import HTTPException

from activity import helpers, known_songs, stream
from activity.routes import image_proxy, playback_routes, queue_routes
from activity.routes import playlist_routes as routes
from activity.state_serializer import serialize_guild_state, serialize_song
from activity.ws_manager import ConnectionManager
from tests.conftest import GUILD_ID, USER, make_song, settle

OTHER = {"id": "77", "username": "other"}


# Stream proxy (#437, #221, #311)

@pytest.fixture
async def upstream():
    """A stand-in for googlevideo: /old has expired (403), /fresh serves Range requests."""
    seen = []

    async def old(request):
        seen.append(("old", request.headers.get("Range"), request.headers.get("User-Agent")))
        return web.Response(status=403)

    async def fresh(request):
        seen.append(("fresh", request.headers.get("Range"), request.headers.get("User-Agent")))
        return web.Response(
            status=206, body=b"0123456789", headers={"Content-Range": "bytes 0-9/100", "Content-Type": "audio/webm"},
        )

    app = web.Application()
    app.router.add_get("/old", old)
    app.router.add_get("/fresh", fresh)
    server = TestServer(app)
    await server.start_server()
    yield server, seen
    await server.close()


def stream_of(server, path):
    return stream.StreamInfo(
        url=str(server.make_url(path)), headers={"User-Agent": "yt-dlp UA"}, protocol="https",
        available_at=0, fetched_at=time.time(),
    )


async def body_of(response) -> bytes:
    return b"".join([chunk async for chunk in response.body_iterator])


async def test_an_expired_stream_is_refreshed_once_and_range_is_forwarded(bot, upstream, monkeypatch):
    server, seen = upstream
    url = "https://www.youtube.com/watch?v=abc"
    monkeypatch.setitem(stream._streams, url, stream_of(server, "/old"))
    extractions = []

    async def extract(_bot, webpage_url):
        extractions.append(webpage_url)
        fresh = stream_of(server, "/fresh")
        stream._streams[webpage_url] = fresh
        return fresh

    monkeypatch.setattr(stream, "_extract_stream", extract)

    response = await stream.proxy_stream(bot, url, "bytes=0-")

    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 0-9/100"
    assert await body_of(response) == b"0123456789"
    assert seen == [("old", "bytes=0-", "yt-dlp UA"), ("fresh", "bytes=0-", "yt-dlp UA")]
    assert extractions == [url]


async def test_listeners_asking_at_once_share_one_extraction(bot, monkeypatch):
    calls = []

    async def extract(_bot, webpage_url):
        calls.append(webpage_url)
        await asyncio.sleep(0.05)
        return stream.StreamInfo("https://cdn/x", {}, "https", 0, time.time())

    monkeypatch.setattr(stream, "_extract_stream", extract)
    monkeypatch.setattr(stream, "_streams", {})
    results = await asyncio.gather(*(stream.stream_info(bot, "https://www.youtube.com/watch?v=x") for _ in range(4)))
    assert len({r.url for r in results}) == 1 and calls == ["https://www.youtube.com/watch?v=x"]


@pytest.mark.parametrize("song", [
    {"is_live": True, "duration": 0},
    {"is_live": False, "duration": 10 * 3600},
])
async def test_live_and_very_long_songs_are_never_cached(bot, monkeypatch, song):
    def no_download(*args, **kwargs):
        raise AssertionError("downloaded")

    monkeypatch.setattr(bot.download_executor, "submit", no_download)
    await stream.download_to_cache(bot, "https://www.youtube.com/watch?v=live", "Live", **song)


# Activity-only playback (#214, #228, #236, #447, #224)

async def test_resuming_a_song_that_is_not_paused_is_refused(bot, ws, guild_data):
    helpers.set_current_for_activity(guild_data, make_song("a"))
    with pytest.raises(HTTPException) as refused:
        await playback_routes.resume(GUILD_ID, user=USER, bot=bot, ws=ws)
    assert refused.value.status_code == 400


async def test_a_late_song_ended_for_an_earlier_play_does_not_advance(bot, ws, guild_data):
    guild_data["loop_mode"] = "song"
    helpers.set_current_for_activity(guild_data, make_song("a"))
    guild_data["seek_offset"] = 180  # the song's time is up
    first_play = guild_data["song_seq"]

    await helpers.activity_advance(bot, ws, GUILD_ID, ended_url=make_song("a").webpage_url, ended_seq=first_play)
    second_play = guild_data["song_seq"]
    # Another listener's "ended" for the first play arrives after the repeat started.
    await helpers.activity_advance(bot, ws, GUILD_ID, ended_url=make_song("a").webpage_url, ended_seq=first_play)

    assert second_play == first_play + 1
    assert guild_data["song_seq"] == second_play


async def test_an_early_song_ended_moves_the_room_on_only_when_the_song_is_due(bot, ws, guild_data, no_precache):
    helpers.set_current_for_activity(guild_data, make_song("a"))
    guild_data["queue"].append(make_song("b"))
    seq = guild_data["song_seq"]

    # A listener's stream stopped a minute in (or someone sent "ended" to skip without being a DJ).
    await helpers.activity_advance(bot, ws, GUILD_ID, ended_url=make_song("a").webpage_url, ended_seq=seq)
    assert guild_data["current"].title == "Song a"

    guild_data["seek_offset"] = 180  # the song's time is up
    for _ in range(100):
        if guild_data["current"].title == "Song b":
            break
        await asyncio.sleep(0.01)
    assert guild_data["current"].title == "Song b"


async def test_stop_wins_over_an_advance_still_picking_an_autoplay_song(bot, ws, guild_data, monkeypatch):
    helpers.set_current_for_activity(guild_data, make_song("a"))
    guild_data["autoplay"] = True
    picking = asyncio.Event()

    async def slow_pick(guild_id, current):
        picking.set()
        await asyncio.sleep(0.2)
        return make_song("autoplay")

    monkeypatch.setattr(bot.playback, "pick_autoplay_song", slow_pick)
    advance = asyncio.create_task(helpers.activity_advance(bot, ws, GUILD_ID, force=True))
    await picking.wait()
    await helpers.activity_stop(bot, ws, GUILD_ID)
    await advance

    assert guild_data["current"] is None
    assert guild_data["queue"] == []


async def test_the_last_viewer_leaving_clears_playback_after_the_grace(bot, guild_data, monkeypatch):
    manager = ConnectionManager()
    recorded = []

    async def record(uid, gid, song, seconds):
        recorded.append((uid, song.title))

    monkeypatch.setattr(bot, "record_listening_stat", record)
    helpers.set_current_for_activity(guild_data, make_song("a"))
    guild_data["listen_start"] -= 30

    await helpers.end_activity_session(bot, manager, GUILD_ID, {42})

    assert guild_data["current"] is None
    assert recorded == [(42, "Song a")]


async def test_a_quick_reconnect_cancels_the_last_viewer_cleanup(monkeypatch):
    monkeypatch.setattr("activity.ws_manager.LAST_VIEWER_GRACE", 0.1)
    manager = ConnectionManager()
    calls = []

    async def on_last(guild_id, user_ids):
        calls.append(guild_id)

    manager.set_on_last_disconnect(on_last)

    class Socket:
        async def accept(self):
            pass

    first, second = Socket(), Socket()
    await manager.connect(first, GUILD_ID, 1)
    manager.disconnect(first, GUILD_ID)
    await asyncio.sleep(0.05)
    await manager.connect(second, GUILD_ID, 1)
    await asyncio.sleep(0.15)

    assert calls == []


# State sent to clients (#232, #143, #358, #147)

def test_clients_never_receive_the_stream_url(bot):
    song = make_song("a", url="https://rr3---sn.googlevideo.com/videoplayback?ip=1.2.3.4")
    assert "url" not in serialize_song(song, bot)


def test_state_names_the_bots_voice_channel(bot, guild_data):
    from types import SimpleNamespace
    guild_data["voice_client"] = SimpleNamespace(
        is_connected=lambda: True, channel=SimpleNamespace(id=5, name="Lounge"),
        is_playing=lambda: False, is_paused=lambda: False,
    )
    state = serialize_guild_state(bot, GUILD_ID)
    assert (state["voice_channel_id"], state["voice_channel_name"]) == ("5", "Lounge")


async def test_unchanged_queue_and_history_are_left_out_of_later_broadcasts(bot, ws, guild_data):
    guild_data["queue"].append(make_song("a"))
    await helpers.broadcast_state(bot, ws, GUILD_ID)
    await settle()
    guild_data["volume"] = 50
    await helpers.broadcast_state(bot, ws, GUILD_ID)
    await settle()
    guild_data["queue"].append(make_song("b"))
    await helpers.broadcast_state(bot, ws, GUILD_ID)
    await settle()

    first, second, third = (data for _type, data in ws.sent[-3:])
    assert "queue" in first and "queue" not in second and "history" not in second
    assert [s["webpage_url"] for s in third["queue"]] == [make_song("a").webpage_url, make_song("b").webpage_url]


# Queue rows by identity (#2)

async def test_a_stale_position_removes_the_song_the_client_meant(bot, ws, guild_data):
    guild_data["queue"].extend([make_song("a"), make_song("b"), make_song("c")])
    await queue_routes.remove_from_queue(GUILD_ID, 0, user=USER, bot=bot, ws=ws, url=make_song("a").webpage_url)
    # The client still shows "c" at index 2, which is now index 1.
    result = await queue_routes.remove_from_queue(GUILD_ID, 2, user=USER, bot=bot, ws=ws, url=make_song("c").webpage_url)

    assert result["removed"] == "Song c" and result["position"] == 1
    assert [s.title for s in guild_data["queue"]] == ["Song b"]
    with pytest.raises(HTTPException) as gone:
        await queue_routes.remove_from_queue(GUILD_ID, 0, user=USER, bot=bot, ws=ws, url=make_song("c").webpage_url)
    assert gone.value.status_code == 404


# Played songs stay in the queue, like YouTube's

@pytest.fixture
def no_precache(monkeypatch):
    async def skip(*_args, **_kwargs):
        return None
    monkeypatch.setattr(stream, "precache", skip)


async def test_clicking_a_played_song_goes_back_to_it(bot, ws, guild_data, no_precache):
    guild_data["played"] = [make_song("a"), make_song("b")]
    helpers.set_current_for_activity(guild_data, make_song("x"))
    guild_data["queue"].append(make_song("y"))

    body = playback_routes.SkipToBody(position=0, webpage_url=make_song("a").webpage_url, played=True)
    result = await playback_routes.skipto(GUILD_ID, body, user=USER, bot=bot, ws=ws)

    assert result["title"] == "Song a"
    assert guild_data["current"].title == "Song a"
    assert guild_data["played"] == []
    assert [s.title for s in guild_data["queue"]] == ["Song b", "Song x", "Song y"]


async def test_skipping_ahead_keeps_the_passed_songs_as_played(bot, ws, guild_data, no_precache):
    helpers.set_current_for_activity(guild_data, make_song("x"))
    guild_data["queue"].extend([make_song("a"), make_song("b")])

    body = playback_routes.SkipToBody(position=1, webpage_url=make_song("b").webpage_url)
    await playback_routes.skipto(GUILD_ID, body, user=USER, bot=bot, ws=ws)

    assert guild_data["current"].title == "Song b"
    assert [s.title for s in guild_data["played"]] == ["Song x", "Song a"]
    assert guild_data["queue"] == []
    assert [s.title for s in guild_data["history"]] == ["Song x"]


async def test_a_played_song_can_be_removed(bot, ws, guild_data):
    guild_data["played"] = [make_song("a"), make_song("b")]

    result = await queue_routes.remove_from_queue(
        GUILD_ID, 0, user=USER, bot=bot, ws=ws, url=make_song("a").webpage_url, played=True,
    )

    assert result["removed"] == "Song a"
    assert [s.title for s in guild_data["played"]] == ["Song b"]


def test_state_lists_played_songs_but_not_a_current_song_that_just_ended(bot, guild_data):
    guild_data["played"] = [make_song("a"), make_song("b")]
    guild_data["current"] = guild_data["played"][-1]

    state = serialize_guild_state(bot, GUILD_ID)

    assert [s["title"] for s in state["played"]] == ["Song a"]


async def test_the_last_viewer_leaving_keeps_the_song_that_was_playing(bot, ws, guild_data, monkeypatch):
    helpers.set_current_for_activity(guild_data, make_song("x"))
    guild_data["queue"].append(make_song("y"))
    monkeypatch.setattr(ws, "has_connections", lambda _guild_id: False)

    await helpers.end_activity_session(bot, ws, GUILD_ID)

    assert guild_data["current"] is None
    assert [s.title for s in guild_data["played"]] == ["Song x"]
    assert [s.title for s in guild_data["queue"]] == ["Song y"]


# Playlists (#435, #38, #196, #26)

async def create(bot, name, user=USER):
    await routes.create_playlist(GUILD_ID, routes.CreateBody(name=name), user=user, bot=bot)


async def save(bot, name, song, user=USER, owner_id=None):
    known_songs.remember([{"title": song.title, "duration": 100, "webpage_url": song.webpage_url}])
    body = routes.AddSongBody(song_url=song.webpage_url, owner_id=owner_id)
    return await routes.add_to_playlist(guild_id=GUILD_ID, name=name, body=body, user=user, bot=bot)


async def share(bot, name, permission):
    collaborator = type("M", (), {"id": 77, "bot": False, "display_name": "other"})()
    guild = type("G", (), {"get_member": lambda self, uid: collaborator if uid == 77 else None})()
    original = bot.get_guild
    bot.get_guild = lambda gid: guild
    try:
        await routes.add_collaborator(GUILD_ID, name, routes.CollabBody(user_id="77", permission=permission),
                                      user=USER, bot=bot)
    finally:
        bot.get_guild = original


@pytest.mark.parametrize(("permission", "can_add", "can_remove"), [
    ("view", False, False),
    ("append", True, False),
    ("edit", True, True),
])
async def test_collaborator_levels(bot, permission, can_add, can_remove):
    await create(bot, "mix")
    await save(bot, "mix", make_song("a"))
    await share(bot, "mix", permission)

    shown = await routes.show_playlist(GUILD_ID, "mix", user=OTHER, bot=bot, global_mode=False, owner_id=42)
    assert [s["title"] for s in shown["songs"]] == ["Song a"]

    async def attempt(action):
        try:
            await action
            return True
        except HTTPException as e:
            assert e.status_code == 403
            return False

    assert await attempt(save(bot, "mix", make_song("b"), user=OTHER, owner_id=42)) is can_add
    removal = routes.remove_from_playlist(GUILD_ID, "mix", 0, user=OTHER, bot=bot, global_mode=False, owner_id=42,
                                          url=make_song("a").webpage_url)
    assert await attempt(removal) is can_remove


async def test_same_named_playlists_of_different_owners_can_be_copied(bot):
    await create(bot, "mix")
    await save(bot, "mix", make_song("a"))
    await create(bot, "mix", user=OTHER)
    await share(bot, "mix", "view")

    result = await routes.copy_playlist(
        GUILD_ID, "mix", routes.CopyBody(target="mix", owner_id=42), user=OTHER, bot=bot,
    )
    assert result["added"] == 1


@pytest.mark.parametrize("name", ["", "   ", "Chill/2", "x" * 51])
async def test_unusable_playlist_names_are_refused(bot, name):
    with pytest.raises(HTTPException) as refused:
        await create(bot, name)
    assert refused.value.status_code == 400


def test_existing_names_with_a_slash_are_renamed_once(tmp_path, monkeypatch):
    import sqlite3

    from bot import MusicBot
    monkeypatch.chdir(tmp_path)
    MusicBot.init_database()
    db = sqlite3.connect("music_bot.db")
    db.execute("INSERT INTO playlists (user_id, guild_id, name, songs) VALUES (1, 1, 'Chill/2', '[]')")
    db.execute("INSERT INTO playlists (user_id, guild_id, name, songs) VALUES (1, 1, 'Chill-2', '[]')")
    db.commit()
    db.close()

    MusicBot.init_database()

    db = sqlite3.connect("music_bot.db")
    names = sorted(row[0] for row in db.execute("SELECT name FROM playlists"))
    db.close()
    assert names == ["Chill-2", "Chill-2 (2)"]


async def test_a_stale_playlist_position_moves_the_song_the_client_meant(bot):
    await create(bot, "mix")
    for vid in "abc":
        await save(bot, "mix", make_song(vid))
    body = routes.MoveBody(from_pos=0, to_pos=0, webpage_url=make_song("c").webpage_url)
    await routes.move_in_playlist(guild_id=GUILD_ID, name="mix", body=body, user=USER, bot=bot)

    shown = await routes.show_playlist(GUILD_ID, "mix", user=USER, bot=bot, global_mode=False, owner_id=None)
    assert [s["title"] for s in shown["songs"]] == ["Song c", "Song a", "Song b"]


# Image proxy (#277, #320)

@pytest.fixture
async def images(monkeypatch):
    async def png(request):
        return web.Response(body=b"\x89PNG", content_type="image/png")

    async def svg(request):
        return web.Response(text="<svg onload=alert(1)>", content_type="image/svg+xml")

    async def big(request):
        return web.Response(body=b"x" * (image_proxy._MAX_BYTES + 1), content_type="image/png")

    app = web.Application()
    app.router.add_get("/img.png", png)
    app.router.add_get("/evil.svg", svg)
    app.router.add_get("/big.png", big)
    async def bounce(request):
        raise web.HTTPFound(f"http://localhost:{request.url.port}/img.png")

    app.router.add_get("/bounce", bounce)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setattr(image_proxy, "_host_allowed", lambda host: host == "127.0.0.1")
    monkeypatch.setattr(image_proxy, "_resolves_to_private", lambda host: False)
    yield server
    await server.close()


@pytest.fixture
def img_request(bot):
    return type("R", (), {"app": type("A", (), {"state": type("S", (), {"bot": bot})()})()})()


@pytest.mark.parametrize(("path", "status"), [
    ("/img.png", 200),
    ("/evil.svg", 415),
    ("/big.png", 413),
    ("/bounce", 400),
])
async def test_image_proxy_checks(images, img_request, path, status):
    response = await image_proxy.proxy_image(img_request, url=str(images.make_url(path)))
    assert response.status_code == status
    if status == 200:
        assert response.headers["x-content-type-options"] == "nosniff"


def test_known_songs_from_saved_data_only_serve_their_owner():
    known_songs.remember([{"title": "Fake", "duration": 1, "webpage_url": "https://u"}], owner=1)
    assert known_songs.lookup("https://u", owner=1)["title"] == "Fake"
    assert known_songs.lookup("https://u", owner=2) is None
    assert known_songs.lookup("https://u") is None



async def test_loading_a_playlist_that_exactly_fills_the_queue_is_not_reported_as_cut_short(
        bot, ws, guild_data, monkeypatch, no_precache):
    await create(bot, "mix")
    for video_id in "ab":
        await save(bot, "mix", make_song(video_id))
    monkeypatch.setattr(routes, "MAX_QUEUE_SIZE", 2)

    result = await routes.load_playlist(GUILD_ID, "mix", routes.LoadBody(), user=USER, bot=bot, ws=ws)
    assert (result["added"], result["capped"]) == (2, False)

    guild_data["queue"].clear()
    guild_data["current"] = None
    monkeypatch.setattr(routes, "MAX_QUEUE_SIZE", 1)
    result = await routes.load_playlist(GUILD_ID, "mix", routes.LoadBody(), user=USER, bot=bot, ws=ws)
    assert (result["added"], result["capped"]) == (1, True)
