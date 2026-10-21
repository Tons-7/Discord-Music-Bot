import asyncio

import pytest

from activity import known_songs
from activity.routes.queue_routes import AddBody, add_to_queue
from activity.routes.search_routes import search_songs
from tests.conftest import GUILD_ID, USER, make_song

URL = "https://www.youtube.com/watch?v=listed"
LISTED = {"title": "Listed", "webpage_url": URL, "duration": 200, "uploader": "Channel"}


@pytest.fixture
def extractions(bot, monkeypatch):
    """Record every URL that falls through to a full yt-dlp extraction."""
    calls = []

    async def fake_extract(query):
        calls.append(query)
        return {"title": "Extracted", "webpage_url": query, "url": "https://cdn/stream", "duration": 90}

    monkeypatch.setattr(bot.music, "get_song_info_cached", fake_extract)
    return calls


@pytest.fixture
def playing(guild_data):
    """Something is already playing, so adds only append to the queue."""
    guild_data["current"] = make_song("current")
    return guild_data


async def add(bot, ws, url):
    return await add_to_queue(GUILD_ID, AddBody(query=url), user=USER, bot=bot, ws=ws)


async def test_listed_song_is_added_without_extraction(bot, ws, playing, extractions):
    known_songs.remember([LISTED])

    result = await add(bot, ws, URL)

    assert result["added"] == 1
    assert extractions == []
    queued = playing["queue"][0]
    assert (queued.title, queued.duration, queued.webpage_url) == ("Listed", 200, URL)
    assert queued.requested_by == "<@42>"


async def test_search_result_is_added_without_extraction(bot, ws, playing, extractions, monkeypatch):
    async def fake_search(query, limit, executor=None):
        return [LISTED]

    monkeypatch.setattr(bot.music, "search_youtube_many", fake_search)

    found = await search_songs(GUILD_ID, q="listed", limit=5, user=USER, bot=bot)
    await add(bot, ws, found["results"][0]["webpage_url"])

    assert extractions == []
    assert playing["queue"][0].title == "Listed"


async def test_unlisted_song_is_extracted(bot, ws, playing, extractions):
    result = await add(bot, ws, URL)

    assert result["added"] == 1
    assert extractions == [URL]
    assert playing["queue"][0].title == "Extracted"


async def test_live_stream_is_never_remembered(bot, ws, playing, extractions):
    known_songs.remember([{**LISTED, "duration": 0}])

    await add(bot, ws, URL)

    assert extractions == [URL]


async def test_played_song_is_added_from_history(bot, ws, playing, extractions):
    playing["history"].append(make_song("old", title="Played before"))

    await add(bot, ws, "https://www.youtube.com/watch?v=old")

    assert extractions == []
    assert playing["queue"][0].title == "Played before"


async def test_duplicate_is_reported_not_queued(bot, ws, playing, extractions):
    known_songs.remember([LISTED])
    await add(bot, ws, URL)

    result = await add(bot, ws, URL)

    assert result == {"ok": True, "added": 0, "duplicate": True, "title": "Listed", "position": 1}
    assert len(playing["queue"]) == 1


async def test_songs_clicked_in_a_row_all_land_in_order(bot, ws, playing, extractions):
    urls = [f"https://www.youtube.com/watch?v={i}" for i in range(6)]
    known_songs.remember([{**LISTED, "title": f"Song {i}", "webpage_url": url} for i, url in enumerate(urls)])

    results = await asyncio.gather(*(add(bot, ws, url) for url in urls))

    assert all(r["added"] == 1 for r in results)
    assert [song.webpage_url for song in playing["queue"]] == urls
    assert extractions == []


async def test_remembered_songs_are_bounded(monkeypatch):
    monkeypatch.setattr(known_songs, "_MAX_ENTRIES", 3)

    known_songs.remember([{**LISTED, "webpage_url": f"u{i}"} for i in range(5)])

    assert known_songs.lookup("u0") is None
    assert known_songs.lookup("u4") is not None
    known_songs._songs.clear()
