import asyncio
import json
import re
import time
from types import SimpleNamespace

import pytest

from activity import known_songs
from activity import stream
from activity.routes import playlist_routes
from services import playlist_service
from activity.routes.queue_routes import AddBody, add_to_queue
from activity.routes.search_routes import search_songs
from cogs.music_commands import MusicCommands
from tests.conftest import GUILD_ID, USER, settle

PLAYLIST = "https://open.spotify.com/playlist/PL123?si=abc"
TRACK = "https://open.spotify.com/track/TR123"


def search_query(target: str) -> str:
    return re.sub(r"^ytsearch\d*:", "", target)


async def wait_until(condition, timeout: float = 5, task: asyncio.Task | None = None):
    """Poll until condition() holds; fail at once if ``task`` died, and after ``timeout`` seconds at the latest."""
    async with asyncio.timeout(timeout):
        while not condition():
            if task is not None and task.done():
                task.result()
                raise AssertionError("task finished before the condition held")
            await asyncio.sleep(0.05)


def embed_page(*tracks: tuple[str, str]) -> str:
    entity = {"trackList": [
        {"title": title, "subtitle": artists, "uri": f"spotify:track:id{i}", "duration": 200_000}
        for i, (title, artists) in enumerate(tracks)
    ]}
    data = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'


TRACK_PAGE = (
    '<meta property="og:title" content="Get Lucky">'
    '<meta name="music:musician_description" content="Daft Punk, Pharrell Williams">'
)


class ApiError(Exception):
    def __init__(self, http_status: int):
        super().__init__(f"HTTP {http_status}")
        self.http_status = http_status


class FakeSpotifyApi:
    def __init__(self, refuse_with: int | None = None):
        self.refuse_with = refuse_with
        self.track_calls = 0

    def track(self, track_id):
        self.track_calls += 1
        if self.refuse_with:
            raise ApiError(self.refuse_with)
        return {"name": "Around the World", "artists": [{"name": "Daft Punk"}]}

    def __getattr__(self, name):
        raise AssertionError(f"playlists and albums must not call the Spotify API ({name})")


@pytest.fixture
def service(bot):
    return bot.music


@pytest.fixture
def pages(service, monkeypatch):
    pages = {}

    async def fake_fetch(url):
        return pages.get(url.split("?")[0])

    monkeypatch.setattr(service, "_fetch_spotify_page", fake_fetch)
    return pages


@pytest.fixture
def youtube(service, monkeypatch):
    """Answer every YouTube search with one match, slowly, recording the queries."""
    queries = []

    def fake_extract(target, **kwargs):
        query = search_query(target)
        queries.append(query)
        time.sleep(0.2)
        slug = query.replace(" ", "-")
        return {"entries": [{"title": query, "id": slug, "url": f"https://www.youtube.com/watch?v={slug}", "duration": 200}]}

    monkeypatch.setattr(service, "_extract", fake_extract)
    return queries


async def test_playlist_is_read_from_the_embed_page_without_the_api(bot, service, pages, youtube):
    bot.spotify = FakeSpotifyApi()
    pages["https://open.spotify.com/embed/playlist/PL123"] = embed_page(
        ("One More Time", "Daft Punk"), ("Get Lucky", "Daft Punk, Pharrell Williams"), ("Digital Love", "Daft Punk"),
    )

    songs = await service.get_song_info(PLAYLIST)

    assert [song["title"] for song in songs] == ["One More Time Daft Punk", "Get Lucky Daft Punk", "Digital Love Daft Punk"]


async def test_playlist_tracks_are_looked_up_in_parallel(bot, service, pages, youtube):
    bot.spotify = None
    pages["https://open.spotify.com/embed/playlist/PL123"] = embed_page(*[(f"Song {i}", "Artist") for i in range(8)])

    started = time.perf_counter()
    songs = await service.get_song_info(PLAYLIST)
    elapsed = time.perf_counter() - started

    assert len(songs) == 8
    assert elapsed < 1.0  # one at a time would take 1.6s


async def test_refused_api_falls_back_and_is_skipped_afterwards(bot, service, pages, youtube):
    bot.spotify = FakeSpotifyApi(refuse_with=403)
    pages[TRACK] = TRACK_PAGE

    first = await service.get_song_info(TRACK)
    second = await service.get_song_info(TRACK)

    assert first["title"] == second["title"] == "Get Lucky Daft Punk"
    assert bot.spotify.track_calls == 1


async def test_other_api_errors_do_not_pause_the_api(bot, service, pages, youtube):
    bot.spotify = FakeSpotifyApi(refuse_with=404)
    pages[TRACK] = TRACK_PAGE

    await service.get_song_info(TRACK)
    await service.get_song_info(TRACK)

    assert bot.spotify.track_calls == 2


async def test_working_api_is_used_for_tracks(bot, service, pages, youtube):
    bot.spotify = FakeSpotifyApi()

    song = await service.get_song_info(TRACK)

    assert song["title"] == "Around the World Daft Punk"


async def test_spotify_links_work_without_api_keys(bot, service, pages, youtube):
    bot.spotify = None
    pages[TRACK] = TRACK_PAGE

    song = await service.get_song_info(TRACK)

    assert youtube == ["Get Lucky Daft Punk"]
    assert song["title"] == "Get Lucky Daft Punk"


async def test_unreadable_embed_page_finds_nothing(bot, service, pages, youtube):
    bot.spotify = None
    pages["https://open.spotify.com/embed/album/AL1"] = "<html>changed layout</html>"

    assert await service.get_song_info("https://open.spotify.com/album/AL1") is None
    assert youtube == []


async def test_lookups_do_not_use_playbacks_workers(bot, service, pages, youtube):
    bot.spotify = None
    pages["https://open.spotify.com/embed/album/AL1"] = embed_page(("Song", "Artist"))
    playback_pool = bot.executor
    bot.executor = SimpleNamespace()  # any use of the playback pool would fail

    try:
        songs = await service.get_song_info("https://open.spotify.com/album/AL1")
    finally:
        bot.executor = playback_pool

    assert len(songs) == 1


async def test_track_list_is_read_without_youtube_lookups(bot, service, pages, youtube):
    pages["https://open.spotify.com/embed/playlist/PL123"] = embed_page(("Get Lucky", "Daft Punk, Pharrell Williams"))

    tracks = await service.spotify_tracks(PLAYLIST)

    assert tracks == [{
        "title": "Get Lucky",
        "uploader": "Daft Punk, Pharrell Williams",
        "duration": 200,
        "webpage_url": "https://open.spotify.com/track/id0",
    }]
    assert youtube == []


async def test_first_song_arrives_before_the_rest_are_found(bot, service, pages, monkeypatch):
    pages["https://open.spotify.com/embed/playlist/PL123"] = embed_page(*[(f"Song {i}", "Artist") for i in range(6)])

    def fake_extract(target, **kwargs):
        query = search_query(target)
        time.sleep(0.05 if query == "Song 0 Artist" else 1.0)
        return {"entries": [{"title": query, "id": query, "url": f"https://www.youtube.com/watch?v={query}", "duration": 1}]}

    monkeypatch.setattr(service, "_extract", fake_extract)

    started = time.perf_counter()
    songs = service.iter_youtube_matches(await service.spotify_tracks(PLAYLIST))
    first = await anext(songs)
    waited = time.perf_counter() - started
    await songs.aclose()

    assert first["title"] == "Song 0 Artist"
    assert waited < 0.5


async def test_stopping_early_cancels_lookups_not_yet_started(bot, service, pages, youtube):
    pages["https://open.spotify.com/embed/playlist/PL123"] = embed_page(*[(f"Song {i}", "Artist") for i in range(12)])

    songs = service.iter_youtube_matches(await service.spotify_tracks(PLAYLIST))
    await anext(songs)
    await songs.aclose()
    await asyncio.sleep(0.6)

    # Only the lookups already handed to a worker run: the first batch and the one that replaced it.
    assert len(youtube) <= 2 * bot.import_executor._max_workers


async def test_spotify_search_lists_tracks_without_remembering_them(bot, service, pages, youtube):
    pages["https://open.spotify.com/embed/playlist/PL123"] = embed_page(("One More Time", "Daft Punk"))

    found = await search_songs(GUILD_ID, q=PLAYLIST, limit=13, user=USER, bot=bot)

    assert [r["title"] for r in found["results"]] == ["One More Time"]
    assert youtube == []
    assert known_songs.lookup("https://open.spotify.com/track/id0") is None


async def test_add_all_queues_in_the_background_and_starts_playing(bot, ws, guild_data, service, pages, youtube, monkeypatch):
    async def no_caching(*args, **kwargs):
        pass

    monkeypatch.setattr(stream, "precache", no_caching)
    pages["https://open.spotify.com/embed/playlist/PL123"] = embed_page(*[(f"Song {i}", "Artist") for i in range(5)])

    started = time.perf_counter()
    result = await add_to_queue(GUILD_ID, AddBody(query=PLAYLIST), user=USER, bot=bot, ws=ws)
    answered_in = time.perf_counter() - started
    for _ in range(40):
        await settle()
        if len(guild_data["queue"]) == 4:
            break

    assert result["in_background"] is True
    assert answered_in < 0.1
    assert guild_data["current"].title == "Song 0 Artist"
    assert [song.title for song in guild_data["queue"]] == [f"Song {i} Artist" for i in range(1, 5)]


async def test_saving_a_spotify_row_stores_its_youtube_match(bot, service, pages, youtube):
    pages["https://open.spotify.com/track/id0"] = TRACK_PAGE
    await playlist_routes.create_playlist(GUILD_ID, playlist_routes.CreateBody(name="mix"), user=USER, bot=bot)
    body = playlist_routes.AddSongBody(
        song_url="https://open.spotify.com/track/id0", song={"title": "Get Lucky", "duration": 248},
    )

    await playlist_routes.add_to_playlist(guild_id=GUILD_ID, name="mix", body=body, user=USER, bot=bot)
    shown = await playlist_routes.show_playlist(GUILD_ID, "mix", global_mode=False, owner_id=None, user=USER, bot=bot)

    assert shown["songs"][0]["webpage_url"].startswith("https://www.youtube.com/watch?v=")
    with pytest.raises(playlist_routes.HTTPException) as refused:
        await playlist_routes.add_to_playlist(guild_id=GUILD_ID, name="mix", body=body, user=USER, bot=bot)
    assert refused.value.status_code == 409


async def test_play_command_starts_with_the_first_song_found(bot, guild_data, service, pages, monkeypatch):
    pages["https://open.spotify.com/embed/album/AL1"] = embed_page(*[(f"Song {i}", "Artist") for i in range(5)])

    def fake_extract(target, **kwargs):
        query = search_query(target)
        time.sleep(0.05 if query == "Song 0 Artist" else 1.0)
        return {"entries": [{"title": query, "id": query, "url": f"https://www.youtube.com/watch?v={query}", "duration": 1}]}

    monkeypatch.setattr(service, "_extract", fake_extract)
    cog = MusicCommands(bot)
    started = time.perf_counter()
    play_started_after, edits = [], []

    async def fake_play_next(guild_id):
        play_started_after.append(time.perf_counter() - started)

    async def record_edit(embed):
        edits.append(embed.title)

    monkeypatch.setattr(cog.playback_service, "play_next", fake_play_next)
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=GUILD_ID), user=SimpleNamespace(mention="<@42>"),
        edit_original_response=record_edit,
    )

    await cog._queue_track_collection(interaction, "https://open.spotify.com/album/AL1")

    assert len(play_started_after) == 1 and play_started_after[0] < 0.5
    assert edits == ["Adding Playlist...", "Playlist Added"]
    assert [song.title for song in guild_data["queue"]] == [f"Song {i} Artist" for i in range(5)]


@pytest.fixture
def slow_after_first(service, monkeypatch):
    """Song 0 is found at once, every other song takes a second."""

    def fake_extract(target, **kwargs):
        query = search_query(target)
        time.sleep(0.05 if query == "Song 0 Artist" else 1.0)
        return {"entries": [{"title": query, "id": query, "url": f"https://www.youtube.com/watch?v={query}", "duration": 1}]}

    monkeypatch.setattr(service, "_extract", fake_extract)


@pytest.fixture
def no_stream_caching(monkeypatch):
    async def skip(*args, **kwargs):
        pass

    monkeypatch.setattr(stream, "precache", skip)


async def test_clearing_the_queue_stops_an_activity_import(
    bot, ws, guild_data, service, pages, slow_after_first, no_stream_caching,
):
    pages["https://open.spotify.com/embed/playlist/PL123"] = embed_page(*[(f"Song {i}", "Artist") for i in range(6)])
    await add_to_queue(GUILD_ID, AddBody(query=PLAYLIST), user=USER, bot=bot, ws=ws)
    await wait_until(lambda: guild_data["current"] is not None)

    bot.playback.queue_service.clear_queue(GUILD_ID)
    await asyncio.sleep(1.3)

    assert guild_data["queue"] == []
    assert guild_data["current"].title == "Song 0 Artist"
    assert not guild_data.get("import_tasks")


async def test_play_command_reports_a_stopped_import(bot, guild_data, service, pages, slow_after_first, monkeypatch):
    pages["https://open.spotify.com/embed/album/AL1"] = embed_page(*[(f"Song {i}", "Artist") for i in range(6)])
    cog = MusicCommands(bot)
    edits = []

    async def skip_playback(guild_id):
        pass

    async def record_edit(embed):
        edits.append(embed.title)

    monkeypatch.setattr(cog.playback_service, "play_next", skip_playback)
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=GUILD_ID), user=SimpleNamespace(mention="<@42>"),
        edit_original_response=record_edit,
    )

    command = asyncio.create_task(cog._queue_track_collection(interaction, "https://open.spotify.com/album/AL1"))
    await wait_until(lambda: edits, task=command)
    cog.queue_service.clear_queue(GUILD_ID)
    await asyncio.wait_for(command, 5)

    assert edits == ["Adding Playlist...", "Playlist Stopped"]
    assert guild_data["queue"] == []


async def test_unreadable_spotify_link_is_refused_before_queueing(bot, ws, guild_data, service, pages, youtube):
    with pytest.raises(playlist_routes.HTTPException) as refused:
        await add_to_queue(GUILD_ID, AddBody(query=PLAYLIST), user=USER, bot=bot, ws=ws)

    assert refused.value.status_code == 404
    assert not guild_data.get("import_tasks")


async def test_add_all_reports_a_cut_short_playlist(bot, ws, guild_data, service, pages, youtube, no_stream_caching):
    pages["https://open.spotify.com/embed/playlist/PL123"] = embed_page(*[(f"Song {i}", "Artist") for i in range(100)])

    result = await add_to_queue(GUILD_ID, AddBody(query=PLAYLIST), user=USER, bot=bot, ws=ws)

    assert (result["total"], result["capped"]) == (100, True)


async def test_saving_a_link_to_a_full_playlist_skips_every_lookup(bot, service, pages, youtube, monkeypatch):
    monkeypatch.setattr(playlist_service, "MAX_PLAYLIST_SIZE", 1)
    pages["https://open.spotify.com/embed/playlist/PL123"] = embed_page(("Song 0", "Artist"))
    await playlist_routes.create_playlist(GUILD_ID, playlist_routes.CreateBody(name="mix"), user=USER, bot=bot)
    known_songs.remember([{"title": "X", "duration": 1, "webpage_url": "https://www.youtube.com/watch?v=x"}])
    full = playlist_routes.AddSongBody(song_url="https://www.youtube.com/watch?v=x")
    await playlist_routes.add_to_playlist(guild_id=GUILD_ID, name="mix", body=full, user=USER, bot=bot)

    with pytest.raises(playlist_routes.HTTPException) as refused:
        await playlist_routes.add_url_to_playlist(
            guild_id=GUILD_ID, name="mix", body=playlist_routes.AddUrlBody(url=PLAYLIST), user=USER, bot=bot,
        )

    assert refused.value.status_code == 400
    assert youtube == []


async def test_saving_a_link_does_not_block_other_saves(bot, service, pages, slow_after_first):
    pages["https://open.spotify.com/embed/playlist/PL123"] = embed_page(*[(f"Song {i}", "Artist") for i in range(3)])
    await playlist_routes.create_playlist(GUILD_ID, playlist_routes.CreateBody(name="mix"), user=USER, bot=bot)
    save_link = asyncio.create_task(playlist_routes.add_url_to_playlist(
        guild_id=GUILD_ID, name="mix", body=playlist_routes.AddUrlBody(url=PLAYLIST), user=USER, bot=bot,
    ))
    await asyncio.sleep(0.2)

    started = time.perf_counter()
    known_songs.remember([{"title": "Y", "duration": 1, "webpage_url": "https://www.youtube.com/watch?v=y"}])
    one_song = playlist_routes.AddSongBody(song_url="https://www.youtube.com/watch?v=y")
    await playlist_routes.add_to_playlist(guild_id=GUILD_ID, name="mix", body=one_song, user=USER, bot=bot)
    single_save = time.perf_counter() - started
    result = await save_link

    assert single_save < 0.5
    assert result["added"] == 3
