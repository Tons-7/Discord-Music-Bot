import threading
from typing import Any, cast

import pytest
from yt_dlp.utils import DownloadError, ExtractorError

from services import music_service as ms
from services.music_service import ExtractionError, link_platform


@pytest.fixture
def service(bot):
    return bot.music


@pytest.fixture
def no_backoff(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(ms.asyncio, "sleep", fake_sleep)
    return slept


@pytest.fixture
def extractions(service, monkeypatch):
    """Answer _extract from a dict of target -> result (or exception), recording target, opts and thread."""
    answers, calls = {}, []

    def fake_extract(target, opts=None, **kwargs):
        calls.append({"target": target, "opts": opts or {}, "thread": threading.current_thread().name, **kwargs})
        answer = answers.get(target)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(service, "_extract", fake_extract)
    return answers, calls


def download_error(message: str, expected: bool = True) -> DownloadError:
    cause = ExtractorError(message, expected=expected)
    return DownloadError(f"ERROR: {message}", cast(Any, (type(cause), cause, None)))


VIDEO = {
    "id": "abc", "title": "Song", "url": "https://rr1.googlevideo.com/stream", "duration": 211.6,
    "webpage_url": "https://www.youtube.com/watch?v=abc", "uploader": "Artist", "channel": "Artist",
    "thumbnail": "https://i.ytimg.com/vi/abc/maxresdefault.webp", "formats": [{"height": 1080}],
    "available_at": 1700000000, "http_headers": {"User-Agent": "yt-dlp"},
    "track": "Song", "artists": ["Artist", "Guest"], "album": "Album",
    "automatic_captions": {"en": [{"url": "x"}] * 50}, "heatmap": [{}] * 100,
}


@pytest.mark.parametrize(("text", "platform"), [
    ("https://www.youtube.com/watch?v=abc", "youtube"),
    ("https://m.youtube.com/watch?v=abc", "youtube"),
    ("https://music.youtube.com/watch?v=abc", "youtube"),
    ("youtu.be/abc", "youtube"),
    ("<https://youtu.be/abc>", "youtube"),
    ("https://on.soundcloud.com/xyz", "soundcloud"),
    ("https://soundcloud.com/artist/sets/album", "soundcloud"),
    ("https://open.spotify.com/track/abc", "spotify"),
    ("spotify:album:abc", "spotify"),
    ("https://music.apple.com/us/album/x/1", "apple"),
    ("https://listen.tidal.com/track/1", "tidal"),
    # Anything else is a text search, never a fetch.
    ("http://10.0.0.5/x?tidal.com", None),
    ("http://169.254.169.254/latest?youtube.com", None),
    ("https://evil.example/spotify.com/track/x", None),
    ("https://youtube.com.evil.example/watch?v=x", None),
    ("https://youtube.com@evil.example/", None),
    ("youtube.com rewind 2018", None),
    ("spotify.com wrapped", None),
    ("lofi hip hop radio", None),
    ("ftp://youtube.com/x", None),
])
def test_links_are_routed_by_host(text, platform):
    found = link_platform(text)
    assert (found[0] if found else None) == platform


async def test_text_that_mentions_a_site_is_searched(service, extractions):
    answers, calls = extractions
    answers["ytsearch:youtube.com rewind 2018"] = {"entries": [{"id": "r", "title": "Rewind", "url": "https://www.youtube.com/watch?v=r"}]}

    song = await service.get_song_info("youtube.com rewind 2018")

    assert song["title"] == "Rewind"
    assert [c["target"] for c in calls] == ["ytsearch:youtube.com rewind 2018"]


async def test_internal_address_is_never_fetched(service, extractions, monkeypatch):
    fetched = []

    async def fake_fetch(url, *args, **kwargs):
        fetched.append(url)

    monkeypatch.setattr(service, "_fetch_text", fake_fetch)
    _, calls = extractions

    await service.get_song_info("http://10.0.0.5:8080/x?tidal.com")
    await service.get_song_info("https://10.0.0.5/spotify.com/track/abc")

    assert fetched == []
    assert all(c["target"].startswith("ytsearch:") for c in calls)


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=abc&list=RDabc&start_radio=1",
    "https://youtu.be/abc?list=PL123",
    "https://www.youtube.com/watch?v=abc&list=PL123&index=3",
])
async def test_video_link_with_a_playlist_resolves_to_the_video(service, extractions, url):
    answers, calls = extractions
    answers[url] = VIDEO

    song = await service.get_song_info(url)

    opts = calls[0]["opts"]
    assert opts["noplaylist"] is True and opts["extract_flat"] == "in_playlist"
    assert opts["allowed_extractors"] == ["youtube.*", "soundcloud.*"]
    assert "no-youtube-unavailable-videos" in opts["compat_opts"]
    assert opts["playlistend"] == ms.MAX_COLLECTION_ENTRIES
    assert song["webpage_url"] == "https://www.youtube.com/watch?v=abc" and "entries" not in song


async def test_soundcloud_short_link_may_use_the_redirect_extractor(service, extractions):
    answers, calls = extractions
    answers["https://on.soundcloud.com/xyz"] = {**VIDEO, "webpage_url": "https://soundcloud.com/a/song"}

    song = await service.get_song_info("https://on.soundcloud.com/xyz")

    assert "generic" in calls[0]["opts"]["allowed_extractors"]
    assert song["webpage_url"] == "https://soundcloud.com/a/song"


async def test_single_video_keeps_only_what_callers_use(service, extractions):
    answers, _ = extractions
    answers["https://www.youtube.com/watch?v=abc"] = VIDEO

    song = await service.get_song_info("https://www.youtube.com/watch?v=abc")

    assert song["url"] == "https://rr1.googlevideo.com/stream"
    assert song["available_at"] == 1700000000 and song["http_headers"] == {"User-Agent": "yt-dlp"}
    assert (song["track"], song["artist"], song["album"]) == ("Song", "Artist", "Album")
    assert song["duration"] == 212
    assert song["thumbnail"].endswith("maxresdefault.webp")
    assert not {"formats", "automatic_captions", "heatmap"} & song.keys()


async def test_missing_stream_url_is_not_replaced_by_the_page(service, extractions):
    answers, _ = extractions
    answers["https://www.youtube.com/watch?v=abc"] = {**VIDEO, "url": None}

    song = await service.get_song_info("https://www.youtube.com/watch?v=abc")

    assert song["url"] == ""


async def test_maxres_thumbnail_of_a_low_resolution_video_is_replaced(service, extractions):
    answers, _ = extractions
    answers["https://www.youtube.com/watch?v=abc"] = {**VIDEO, "formats": [{"height": 360}]}

    song = await service.get_song_info("https://www.youtube.com/watch?v=abc")

    assert song["thumbnail"] == "https://i.ytimg.com/vi/abc/hqdefault.jpg"


FLAT_PLAYLIST = {
    "_type": "playlist", "id": "PL123", "title": "My Mix", "uploader": "Someone",
    "entries": [
        {"id": "a", "title": "Song A", "url": "https://www.youtube.com/watch?v=a", "duration": 200,
         "uploader": "A", "thumbnails": [{"url": "https://i.ytimg.com/vi/a/hq720.jpg", "width": 1280, "height": 720},
                                         {"url": "https://i.ytimg.com/vi/a/hqdefault.jpg", "width": 480, "height": 360}]},
        {"id": "p", "title": "[Private video]", "url": "https://www.youtube.com/watch?v=p", "duration": None},
        {"id": "d", "title": "[Deleted video]", "url": "https://www.youtube.com/watch?v=d"},
        {"id": "m", "title": "Members", "url": "https://www.youtube.com/watch?v=m", "availability": "subscriber_only"},
        {"id": "l", "title": "Radio", "url": "https://www.youtube.com/watch?v=l", "live_status": "is_live",
         "channel": "Station", "uploader": None},
        {"id": "b", "title": "Song B", "url": "https://www.youtube.com/watch?v=b", "duration": 180},
    ],
}


async def test_explicit_playlist_is_listed_without_unavailable_entries(service, extractions):
    answers, _ = extractions
    url = "https://www.youtube.com/playlist?list=PL123"
    answers[url] = FLAT_PLAYLIST

    result = await service.get_song_info(url)

    assert result["_type"] == "playlist" and result["title"] == "My Mix" and result["capped"] is False
    assert [e["title"] for e in result["entries"]] == ["Song A", "Radio", "Song B"]
    first, live, last = result["entries"]
    assert first["thumbnail"] == "https://i.ytimg.com/vi/a/hq720.jpg"
    assert last["thumbnail"] == "https://i.ytimg.com/vi/b/hqdefault.jpg"
    assert live["is_live"] is True and live["uploader"] == "Station"
    assert first["webpage_url"] == "https://www.youtube.com/watch?v=a"


async def test_listing_reports_a_capped_playlist(service, extractions, monkeypatch):
    monkeypatch.setattr(ms, "MAX_COLLECTION_ENTRIES", 2)
    answers, _ = extractions
    url = "https://www.youtube.com/playlist?list=PL123"
    answers[url] = {**FLAT_PLAYLIST, "entries": FLAT_PLAYLIST["entries"][:2]}

    result = await service.get_song_info(url)

    assert result["capped"] is True


async def test_soundcloud_set_entries_are_resolved_on_the_import_workers(service, extractions):
    answers, calls = extractions
    url = "https://soundcloud.com/artist/sets/album"
    answers[url] = {"_type": "playlist", "id": "1", "title": "Album", "entries": [
        {"_type": "url", "url": "https://api-v2.soundcloud.com/tracks/1", "id": "1"},
        {"_type": "url", "url": "https://soundcloud.com/artist/two", "id": "2"},
        {"_type": "url", "url": "https://evil.example/three", "id": "3"},
    ]}
    answers["https://api-v2.soundcloud.com/tracks/1"] = {
        "id": "1", "title": "One", "url": "https://cf-media/1", "webpage_url": "https://soundcloud.com/artist/one",
        "duration": 120.4, "uploader": "Artist",
    }
    answers["https://soundcloud.com/artist/two"] = download_error("HTTP Error 404: Not Found")

    result = await service.get_song_info(url)

    assert [e["title"] for e in result["entries"]] == ["One"]
    assert result["entries"][0]["webpage_url"] == "https://soundcloud.com/artist/one"
    pool = {c["thread"].rsplit("_", 1)[0] for c in calls if c["target"] != url}
    assert len(pool) == 1 and pool != {calls[0]["thread"].rsplit("_", 1)[0]}  # import pool, not playback's
    assert "https://evil.example/three" not in [c["target"] for c in calls]


async def test_youtube_playlist_is_paged_on_the_worker_and_capped(service, extractions):
    answers, calls = extractions
    url = "https://www.youtube.com/playlist?list=PL123"
    answers[url] = FLAT_PLAYLIST

    songs = await service.handle_youtube_playlist(url, limit=50)

    call = calls[0]
    assert call["thread"] != threading.main_thread().name
    assert call["opts"]["playlistend"] == 50 and call["opts"]["extract_flat"] == "in_playlist"
    assert "process" not in call
    assert [s["title"] for s in songs] == ["Song A", "Radio", "Song B"]
    assert all("maxresdefault" not in s["thumbnail"] for s in songs)
    assert songs[1]["is_live"] is True


async def test_youtube_playlist_path_refuses_other_hosts(service, extractions):
    _, calls = extractions

    assert await service.handle_youtube_playlist("https://evil.example/playlist?youtube.com") == []
    assert calls == []


async def test_unavailable_video_is_not_retried(service, extractions, no_backoff):
    answers, calls = extractions
    url = "https://www.youtube.com/watch?v=gone"
    answers[url] = download_error("[youtube] gone: Video unavailable. This video has been removed")

    assert await service.get_song_info(url) is None
    with pytest.raises(ExtractionError) as raised:
        await service.get_song_info(url, raise_errors=True)

    assert raised.value.permanent is True and raised.value.bot_check is False
    assert "Video unavailable" in raised.value.reason
    assert len(calls) == 2 and no_backoff == []


async def test_bot_check_is_not_retried(service, extractions, no_backoff):
    answers, calls = extractions
    url = "https://www.youtube.com/watch?v=abc"
    answers[url] = download_error("[youtube] abc: Sign in to confirm you’re not a bot")

    with pytest.raises(ExtractionError) as raised:
        await service.get_song_info(url, raise_errors=True)

    assert raised.value.bot_check is True and raised.value.permanent is False
    assert len(calls) == 1


async def test_transient_failure_is_retried_with_backoff(service, extractions, no_backoff):
    answers, calls = extractions
    url = "https://www.youtube.com/watch?v=abc"
    answers[url] = download_error("Unable to download webpage: The read operation timed out", expected=False)

    with pytest.raises(ExtractionError) as raised:
        await service.get_song_info_cached(url, raise_errors=True)

    assert raised.value.permanent is False
    assert len(calls) == 3 and no_backoff == [1, 2]


async def test_search_and_other_services_keep_returning_none(service, extractions):
    answers, _ = extractions
    answers["ytsearch:nothing"] = download_error("search failed", expected=False)

    assert await service.get_song_info("nothing", raise_errors=True) is None


async def test_cached_results_are_copies(service, extractions):
    answers, calls = extractions
    url = "https://www.youtube.com/playlist?list=PL123"
    answers[url] = FLAT_PLAYLIST

    first = await service.get_song_info_cached(url)
    first["entries"][0]["requested_by"] = "someone"
    first["entries"].clear()
    second = await service.get_song_info_cached(url)

    assert len(calls) == 1
    assert len(second["entries"]) == 3 and "requested_by" not in second["entries"][0]


async def test_search_result_shapes(service, extractions):
    answers, _ = extractions
    entries = [{"id": str(i), "title": f"Hit {i}", "url": f"https://www.youtube.com/watch?v={i}"} for i in range(3)]
    answers["ytsearch:hits"] = {"entries": entries[:1]}
    answers["ytsearch3:hits"] = {"entries": entries}

    top = await service.search_youtube("hits")
    many = await service.search_youtube("hits", limit=3)
    listed = await service.search_youtube_many("hits", 3)

    assert top["title"] == "Hit 0"
    assert [e["title"] for e in many["entries"]] == ["Hit 0", "Hit 1", "Hit 2"]
    assert [e["title"] for e in listed] == ["Hit 0", "Hit 1", "Hit 2"]


def test_flat_live_entries_are_flagged():
    entry = ms.MusicService._normalize_youtube_entry(
        {"id": "x", "title": "Radio", "url": "https://www.youtube.com/watch?v=x", "live_status": "is_upcoming"}
    )
    assert entry is not None and entry["is_live"] is True
    assert ms.MusicService.is_livestream({"live_status": "is_live"})
    assert not ms.MusicService.is_livestream({"live_status": "was_live", "duration": 0})


def test_extract_uses_the_bots_option_builder(bot, monkeypatch):
    seen = {}

    class FakeYDL:
        def __init__(self, params):
            seen.update(params)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, target, **kwargs):
            return {"target": target}

    monkeypatch.setattr(ms.yt_dlp, "YoutubeDL", FakeYDL)
    bot.music._extract("ytsearch:x", opts={"playlistend": 7}, download=False)

    assert seen["playlistend"] == 7 and seen["ignoreerrors"] is False and "logger" in seen


@pytest.mark.parametrize(("url", "expected"), [
    ("https://www.youtube.com/watch?v=abc&list=PL123&index=3", "https://www.youtube.com/playlist?list=PL123"),
    ("https://youtu.be/abc?list=PL123", "https://www.youtube.com/playlist?list=PL123"),
    ("https://www.youtube.com/watch?v=abc&list=RDabc&start_radio=1", "https://www.youtube.com/watch?v=abc&list=RDabc"),
    ("https://www.youtube.com/watch?v=abc", None),
    ("https://evil.example/watch?v=abc&list=PL123", None),
])
def test_list_link_of_a_video_link(url, expected):
    assert ms.youtube_list_link(url) == expected
