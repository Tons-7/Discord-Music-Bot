import pytest
from fastapi import HTTPException

from activity.routes import playlist_routes as routes
from activity.routes.search_routes import search_songs
from tests.conftest import GUILD_ID, USER, make_song

LINK = "https://www.youtube.com/playlist?list=PLmix"


def entry(video_id: str, **overrides) -> dict:
    return {"title": f"Song {video_id}", "url": f"https://www.youtube.com/watch?v={video_id}", "duration": 120, **overrides}


@pytest.fixture
def lookups(bot, monkeypatch):
    """Serve links from a dict and count how often each one is resolved."""
    links = {LINK: {"entries": [entry("a"), entry("b"), entry("gone", title=None)]}}
    calls = []

    async def fake_lookup(url):
        calls.append(url)
        return links.get(url)

    monkeypatch.setattr(bot._music_service, "get_song_info", fake_lookup)
    return links, calls


@pytest.fixture
async def playlist(bot):
    await routes.create_playlist(GUILD_ID, routes.CreateBody(name="mix"), user=USER, bot=bot)
    return "mix"


async def save_link(bot, name, url=LINK):
    return await routes.add_url_to_playlist(
        guild_id=GUILD_ID, name=name, body=routes.AddUrlBody(url=url), user=USER, bot=bot,
    )


async def saved_songs(bot, name):
    shown = await routes.show_playlist(GUILD_ID, name, global_mode=False, owner_id=None, user=USER, bot=bot)
    return shown["songs"]


async def test_link_songs_are_saved_to_playlist(bot, playlist, lookups):
    result = await save_link(bot, playlist)

    assert (result["added"], result["skipped"], result["dropped"]) == (2, 0, 0)
    songs = await saved_songs(bot, playlist)
    assert [s["webpage_url"] for s in songs] == [entry("a")["url"], entry("b")["url"]]
    assert all(s["requested_by"] == "<@42>" for s in songs)


async def test_songs_already_in_playlist_are_skipped(bot, playlist, lookups):
    links, _ = lookups
    await save_link(bot, playlist)
    other = "https://www.youtube.com/playlist?list=PLother"
    links[other] = {"entries": [entry("b"), entry("c")]}

    result = await save_link(bot, playlist, other)

    assert (result["added"], result["skipped"]) == (1, 1)
    assert len(await saved_songs(bot, playlist)) == 3


async def test_saving_the_same_link_twice_is_refused(bot, playlist, lookups):
    await save_link(bot, playlist)

    with pytest.raises(HTTPException) as refused:
        await save_link(bot, playlist)

    assert refused.value.status_code == 400


async def test_full_playlist_keeps_what_fits(bot, playlist, lookups, monkeypatch):
    monkeypatch.setattr(routes, "MAX_PLAYLIST_SIZE", 1)

    result = await save_link(bot, playlist)

    assert (result["added"], result["dropped"], result["truncated"]) == (1, 1, True)


async def test_single_song_link_is_refused(bot, playlist, lookups):
    links, _ = lookups
    single = "https://www.youtube.com/watch?v=one"
    links[single] = entry("one")

    with pytest.raises(HTTPException) as refused:
        await save_link(bot, playlist, single)

    assert refused.value.status_code == 400


async def test_searched_link_is_not_resolved_again(bot, playlist, lookups):
    _, calls = lookups

    await search_songs(GUILD_ID, q=LINK, limit=5, user=USER, bot=bot)
    await save_link(bot, playlist)

    assert calls == [LINK]


async def test_saving_the_queue_keeps_what_fits(bot, guild_data, playlist, monkeypatch):
    monkeypatch.setattr(routes, "MAX_PLAYLIST_SIZE", 2)
    guild_data["current"] = make_song("now")
    guild_data["queue"].extend([make_song("next"), make_song("later")])

    result = await routes.add_all_queue_to_playlist(
        guild_id=GUILD_ID, name=playlist, body=routes.AddAllQueueBody(), user=USER, bot=bot,
    )

    assert (result["added"], result["dropped"], result["truncated"]) == (2, 1, True)
