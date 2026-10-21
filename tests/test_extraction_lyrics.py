import re
from types import SimpleNamespace
from typing import Any, cast

import pytest

from config import LYRICS_API_BASE
from utils import lyrics


def words(text: str | None) -> list[str]:
    return re.sub(r"[^\w\s]", " ", (text or "").lower()).split()


def record(track, artist, duration, *, synced=True, plain=True, album="", instrumental=False, last_line=None):
    last = last_line if last_line is not None else max(duration - 10, 1)
    return {
        "trackName": track,
        "artistName": artist,
        "albumName": album,
        "duration": duration,
        "instrumental": instrumental,
        "plainLyrics": None if instrumental or not plain else f"words of {track}",
        "syncedLyrics": (
            f"[00:05.00] first line\n[{int(last) // 60:02d}:{int(last) % 60:02d}.00] last line"
            if synced and not instrumental else None
        ),
    }


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}

    async def json(self, content_type=None):
        return self.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeLrclib:
    """Serves /get and /search from a record list, filtering by words the way LRCLIB does."""

    def __init__(self, *records, statuses=()):
        self.records = list(records)
        self.statuses = list(statuses)
        self.calls = []
        self.headers = []

    def get(self, url, params=None, headers=None, timeout=None):
        endpoint = url.removeprefix(LYRICS_API_BASE)
        self.calls.append((endpoint, dict(params or {})))
        self.headers.append(headers or {})
        if self.statuses:
            status, response_headers = self.statuses.pop(0)
            return FakeResponse(status, headers=response_headers)
        params = params or {}
        if endpoint == "/get":
            for r in self.records:
                if (words(r["trackName"]) == words(params["track_name"])
                        and words(r["artistName"]) == words(params["artist_name"])
                        and words(r["albumName"]) == words(params.get("album_name", r["albumName"]))
                        and ("duration" not in params or abs(r["duration"] - params["duration"]) <= 2)):
                    return FakeResponse(200, r)
            return FakeResponse(404, {"name": "TrackNotFound"})
        found = [
            r for r in self.records
            if all(w in words(r["trackName"]) for w in words(params.get("track_name")))
            and all(w in words(r["artistName"]) for w in words(params.get("artist_name")))
            and all(w in words(r["albumName"]) for w in words(params.get("album_name")))
        ]
        return FakeResponse(200, found[:20])

    @property
    def searched(self):
        return [params for endpoint, params in self.calls if endpoint == "/search"]


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(lyrics, "_blocked_until", 0.0)
    monkeypatch.setattr(lyrics.random, "uniform", lambda a, b: 0.0)
    monkeypatch.delenv("LRCLIB_APP_ID", raising=False)
    monkeypatch.delenv("LRCLIB_API_KEY", raising=False)
    lyrics._cache.clear()
    lyrics._inflight.clear()
    yield
    lyrics._cache.clear()


async def fetch(session, title, uploader="", duration=0, album="", **kw) -> Any:
    return await lyrics.fetch_lyrics(title, uploader, duration, album, session=cast(Any, session), **kw)


async def for_song(session, item) -> Any:
    return await lyrics.lyrics_for_song(item, session=cast(Any, session))


@pytest.mark.parametrize(("title", "uploader", "artist", "song"), [
    # Formats that already worked keep their split.
    ("Rick Astley - Never Gonna Give You Up (Official Video)", "Rick Astley", "Rick Astley", "Never Gonna Give You Up"),
    ("Ed Sheeran - Shape of You [Official Video]", "Ed Sheeran", "Ed Sheeran", "Shape of You"),
    ("Queen – Bohemian Rhapsody (Official Video Remastered)", "Queen Official", "Queen", "Bohemian Rhapsody"),
    ("【MV】YOASOBI - アイドル", "Ayase / YOASOBI", "YOASOBI", "アイドル"),
    ("Shape of You", "Ed Sheeran - Topic", "", "Shape of You"),
    ("Song Title - Artist Name", "Artist Name", "Artist Name", "Song Title"),
    ("The Weeknd - Blinding Lights (Official Audio)", "TheWeekndVEVO", "The Weeknd", "Blinding Lights"),
    # Japanese quote brackets hold the song name.
    ("YOASOBI「夜に駆ける」Official Music Video", "Ayase / YOASOBI", "YOASOBI", "夜に駆ける"),
    ("米津玄師 MV「Lemon」", "米津玄師", "米津玄師", "Lemon"),
    ("LiSA 『紅蓮華』 -MUSiC CLiP-", "LiSA Official YouTube", "LiSA", "紅蓮華"),
    ("Aimer「残響散歌」MUSIC VIDEO（TVアニメ「鬼滅の刃」遊郭編OPテーマ）", "Aimer", "Aimer", "残響散歌"),
    ("ヨルシカ - 晴る（OFFICIAL VIDEO）", "ヨルシカ / n-buna Official", "ヨルシカ", "晴る"),
    # K-pop quotes and pipes.
    ('TWICE "Feel Special" M/V', "JYP Entertainment", "TWICE", "Feel Special"),
    ("BTS (방탄소년단) 'Dynamite' Official MV", "HYBE LABELS", "BTS", "Dynamite"),
    ("(G)I-DLE - 'TOMBOY' Official Music Video", "(G)I-DLE (여자)아이들", "(G)I-DLE", "TOMBOY"),
    ("Never Gonna Give You Up | Rick Astley", "Rick Astley", "Rick Astley", "Never Gonna Give You Up"),
    ("Ed Sheeran - Shape of You (Official Music Video) | Ed Sheeran", "Ed Sheeran", "Ed Sheeran", "Shape of You"),
    # Remixer, cover and featured channels no longer flip artist and title.
    ("Avicii - Levels (Skrillex Remix)", "Skrillex", "Avicii", "Levels (Skrillex Remix)"),
    ("Imagine Dragons - Believer (Cover by Jada Facer)", "Jada Facer", "Imagine Dragons", "Believer (Cover by Jada Facer)"),
    ("Calvin Harris - This Is What You Came For (Official Video) ft. Rihanna", "RihannaVEVO",
     "Calvin Harris", "This Is What You Came For ft. Rihanna"),
    ("Kygo - Firestone ft. Conrad Sewell", "Conrad Sewell", "Kygo", "Firestone ft. Conrad Sewell"),
    ("Rihanna ft. Drake - Work", "RihannaVEVO", "Rihanna", "Work"),
    # Live and sped-up cuts keep their tag in the title (the bare step drops it).
    ("Queen - Bohemian Rhapsody (Live Aid 1985)", "Queen Official", "Queen", "Bohemian Rhapsody (Live Aid 1985)"),
    ("Gotye - Somebody That I Used To Know (feat. Kimbra) - official music video", "gotyemusic",
     "Gotye", "Somebody That I Used To Know (feat. Kimbra)"),
])
def test_title_parsing(title, uploader, artist, song):
    assert lyrics._split_artist_title(title, uploader) == (artist, song)


async def test_known_formats_are_still_found():
    lrclib = FakeLrclib(
        record("Never Gonna Give You Up", "Rick Astley", 213),
        record("Uptown Funk (Ft. Bruno Mars)", "Mark Ronson", 270),
    )

    rick = await fetch(lrclib, "Rick Astley - Never Gonna Give You Up (Official Video)", "Rick Astley", 213)
    funk = await fetch(lrclib, "Mark Ronson - Uptown Funk (Official Video) ft. Bruno Mars", "Mark Ronson", 270)

    assert (rick["title"], rick["artist"]) == ("Never Gonna Give You Up", "Rick Astley")
    assert funk["artist"] == "Mark Ronson"


async def test_exact_get_needs_only_title_and_artist():
    lrclib = FakeLrclib(record("Never Gonna Give You Up", "Rick Astley", 213))

    result = await fetch(lrclib, "Rick Astley - Never Gonna Give You Up (Official Video)", "Rick Astley", 213)

    assert result["title"] == "Never Gonna Give You Up"
    assert lrclib.calls == [("/get", {
        "track_name": "Never Gonna Give You Up", "artist_name": "Rick Astley", "duration": 213,
    })]


async def test_same_title_by_another_artist_with_another_runtime_is_rejected():
    lrclib = FakeLrclib(record("Hello", "Lionel Richie", 250))

    assert await fetch(lrclib, "Hello", "Small Band - Topic", 170) is None
    assert await fetch(lrclib, "Hello", "Some Channel", 170) is None


async def test_music_video_with_a_long_intro_still_matches():
    lrclib = FakeLrclib(record("Bad Habits", "Ed Sheeran", 231))

    result = await fetch(lrclib, "Bad Habits (Official Video)", "Ed Sheeran", 260)

    assert result["artist"] == "Ed Sheeran"
    assert result["synced"]  # the intro is a constant offset, so highlighting still works


async def test_instrumental_is_reported_instead_of_another_songs_lyrics():
    lrclib = FakeLrclib(
        record("Nuvole Bianche", "Ludovico Einaudi", 357, album="Una Mattina", instrumental=True),
        record("Nuvole Bianche", "Some Singer", 230),
    )

    found = await fetch(lrclib, "Nuvole Bianche", "Ludovico Einaudi - Topic", 357, "Una Mattina", instrumental_ok=True)
    legacy = await fetch(lrclib, "Nuvole Bianche", "Ludovico Einaudi - Topic", 357, "Una Mattina")

    assert found["instrumental"] is True and found["lyrics"] == "" and found["artist"] == "Ludovico Einaudi"
    assert legacy is None


async def test_instrumental_search_row_does_not_hide_real_lyrics():
    lrclib = FakeLrclib(
        record("Song (Instrumental)", "Band", 200, instrumental=True),
        record("Song (Remastered)", "Band", 200),
    )

    result = await fetch(lrclib, "Band - Song", "", 200, instrumental_ok=True)

    assert result["instrumental"] is False and result["lyrics"]


async def test_japanese_quoted_title_is_found():
    lrclib = FakeLrclib(record("夜に駆ける", "YOASOBI", 261))

    result = await fetch(lrclib, "YOASOBI「夜に駆ける」Official Music Video", "Ayase / YOASOBI", 261)

    assert result["title"] == "夜に駆ける"


async def test_kpop_quoted_title_is_found():
    lrclib = FakeLrclib(record("Feel Special", "TWICE", 207))

    result = await fetch(lrclib, 'TWICE "Feel Special" M/V', "JYP Entertainment", 207)

    assert result["artist"] == "TWICE"


async def test_title_artist_reupload_tries_the_swap():
    lrclib = FakeLrclib(record("Shape of You", "Ed Sheeran", 234))

    result = await fetch(lrclib, "Shape of You - Ed Sheeran (Lyrics)", "7clouds", 234)

    assert (result["title"], result["artist"]) == ("Shape of You", "Ed Sheeran")


async def test_album_name_mismatch_retries_without_the_album():
    lrclib = FakeLrclib(record("Perfect", "Ed Sheeran", 263, album="÷ (Deluxe)"))

    result = await fetch(lrclib, "Perfect", "Ed Sheeran - Topic", 263, "Divide")

    assert result["artist"] == "Ed Sheeran"
    assert {"track_name": "Perfect", "artist_name": "Ed Sheeran"} in lrclib.searched


async def test_cover_on_the_cover_artists_channel_finds_the_original():
    lrclib = FakeLrclib(record("Believer", "Imagine Dragons", 204))

    result = await fetch(lrclib, "Imagine Dragons - Believer (Cover by Jada Facer)", "Jada Facer", 204)

    assert result["artist"] == "Imagine Dragons"


async def test_live_cut_gets_plain_lyrics_only():
    lrclib = FakeLrclib(record("Bohemian Rhapsody", "Queen", 354))

    result = await fetch(lrclib, "Queen - Bohemian Rhapsody (Live Aid 1985)", "Queen Official", 150)

    assert result["lyrics"] and result["synced"] == ""


async def test_sped_up_upload_gets_plain_lyrics_only():
    lrclib = FakeLrclib(record("Song", "Band", 200))

    result = await fetch(lrclib, "Band - Song (Sped Up)", "Edits Channel", 160)

    assert result["lyrics"] and result["synced"] == ""


async def test_youtube_music_metadata_is_tried_first():
    lrclib = FakeLrclib(record("Blinding Lights", "The Weeknd", 200))

    result = await fetch(lrclib, "THE WEEKND || blinding lights [4K]", "Some Uploads", 200,
                         track="Blinding Lights", artist="The Weeknd")

    assert result["title"] == "Blinding Lights"
    assert lrclib.calls[0] == ("/get", {"track_name": "Blinding Lights", "artist_name": "The Weeknd", "duration": 200})


async def test_rate_limit_stops_the_lookup_and_is_honoured():
    lrclib = FakeLrclib(record("Song", "Band", 200), statuses=[(429, {"Retry-After": "60"})])

    with pytest.raises(lyrics.LyricsRateLimited):
        await fetch(lrclib, "Band - Song", "", 200)
    assert len(lrclib.calls) == 1

    with pytest.raises(lyrics.LyricsServiceUnavailable):
        await fetch(lrclib, "Band - Song", "", 200)
    assert len(lrclib.calls) == 1  # nothing is sent while blocked

    lyrics._blocked_until = 0.0
    assert (await fetch(lrclib, "Band - Song", "", 200))["artist"] == "Band"


async def test_short_overload_is_waited_out_once():
    lrclib = FakeLrclib(record("Song", "Band", 200), statuses=[(503, {"Retry-After": "0"})])

    result = await fetch(lrclib, "Band - Song", "", 200)

    assert result["artist"] == "Band"
    assert lrclib.calls[0] == lrclib.calls[1]


async def test_server_error_on_every_step_is_reported_as_an_outage():
    lrclib = FakeLrclib(statuses=[(500, {})] * 10)

    with pytest.raises(lyrics.LyricsServiceUnavailable):
        await fetch(lrclib, "Band - Song", "", 200)


def test_retry_after_accepts_seconds_and_dates():
    assert lyrics._retry_after("12", 30) == 12
    assert lyrics._retry_after(None, 30) == 30
    assert lyrics._retry_after("Wed, 21 Oct 2015 07:28:00 GMT", 30) == 0  # in the past
    assert lyrics._retry_after("garbage", 5) == 5


async def test_registration_headers_are_sent_only_when_configured(monkeypatch):
    lrclib = FakeLrclib(record("Song", "Band", 200))
    await fetch(lrclib, "Band - Song", "", 200)
    assert "Lrclib-App-Id" not in lrclib.headers[0] and "Lrclib-Api-Key" not in lrclib.headers[0]

    monkeypatch.setenv("LRCLIB_APP_ID", "app")
    monkeypatch.setenv("LRCLIB_API_KEY", "secret")
    await fetch(lrclib, "Band - Song", "", 201)
    assert lrclib.headers[-1]["Lrclib-App-Id"] == "app" and lrclib.headers[-1]["Lrclib-Api-Key"] == "secret"
    assert lrclib.headers[-1]["User-Agent"]


def song(**overrides):
    return SimpleNamespace(**{
        "title": "Band - Song", "uploader": "Band", "duration": 200, "album": "",
        "track": "", "artist": "", "webpage_url": "https://www.youtube.com/watch?v=x", **overrides,
    })


async def test_shared_cache_serves_repeat_requests():
    lrclib = FakeLrclib(record("Song", "Band", 200))

    first = await for_song(lrclib, song())
    sent = len(lrclib.calls)
    second = await for_song(lrclib, song())

    assert first == second and first["webpage_url"] == "https://www.youtube.com/watch?v=x"
    assert first["instrumental"] is False
    assert len(lrclib.calls) == sent


async def test_shared_cache_keeps_misses_but_not_outages():
    lrclib = FakeLrclib(statuses=[(429, {"Retry-After": "5"})])
    with pytest.raises(lyrics.LyricsServiceUnavailable):
        await for_song(lrclib, song())
    assert lyrics.cached_lyrics(song().webpage_url) == (False, None)

    lyrics._blocked_until = 0.0
    assert await for_song(lrclib, song()) is None
    assert lyrics.cached_lyrics(song().webpage_url) == (True, None)


async def test_shared_cache_reports_instrumentals():
    lrclib = FakeLrclib(record("Song", "Band", 200, instrumental=True))

    payload = await for_song(lrclib, song())

    assert payload["instrumental"] is True and payload["lyrics"] == ""


async def test_pipe_title_prefers_the_original_over_a_cover():
    lrclib = FakeLrclib(
        record("Never Gonna Give You Up (Rick Astley)", "Neuro Sama", 215),
        record("Never Gonna Give You Up", "Rick Astley", 213),
    )

    result = await fetch(lrclib, "Never Gonna Give You Up | Rick Astley", "Rick Astley", 213)

    assert result["artist"] == "Rick Astley"


async def test_remix_on_the_remixers_channel_finds_the_song():
    lrclib = FakeLrclib(record("Levels", "Avicii", 200))

    result = await fetch(lrclib, "Avicii - Levels (Skrillex Remix)", "Skrillex", 200)

    assert result["artist"] == "Avicii"
