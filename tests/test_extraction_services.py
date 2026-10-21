import contextlib
import json

import pytest

from services import music_service as ms
from services.music_service import spotify_collection, track_collection


@pytest.fixture
def service(bot):
    return bot.music


def entry(video_id, title, duration, uploader="Channel"):
    return {"id": video_id, "title": title, "duration": duration, "uploader": uploader,
            "url": f"https://www.youtube.com/watch?v={video_id}", "webpage_url": f"https://www.youtube.com/watch?v={video_id}"}


@pytest.fixture
def searches(service, monkeypatch):
    """YouTube search results by query; records (query, limit, executor)."""
    answers, calls = {}, []

    async def fake_search(query, limit, executor=None):
        calls.append((query, limit, executor))
        return [dict(e) for e in answers.get(query, [])]

    monkeypatch.setattr(service, "search_youtube_many", fake_search)
    return answers, calls


@pytest.fixture
def pages(service, monkeypatch):
    """Page and JSON fetches by URL; records every URL asked for."""
    served, asked = {}, []

    async def fake_fetch(url, hosts, **kwargs):
        asked.append(url)
        return served.get(url)

    monkeypatch.setattr(service, "_fetch_text", fake_fetch)
    return served, asked


# Duration-aware matching

def test_track_match_prefers_the_matching_runtime(service):
    entries = [
        entry("mv", "Artist - Song (Official Music Video)", 262),
        entry("topic", "Song", 214, "Artist - Topic"),
        entry("lyrics", "Artist - Song (Lyrics)", 215),
    ]

    picked = service._pick_track_match(entries, {"title": "Song", "uploader": "Artist", "duration": 214})

    assert picked["id"] == "topic"


def test_track_match_skips_junk_with_the_right_runtime(service):
    entries = [
        entry("mv", "Artist - Song (Official Video)", 250),
        entry("fast", "Artist - Song (Sped Up)", 214),
        entry("audio", "Artist - Song (Audio)", 220),
    ]

    picked = service._pick_track_match(entries, {"title": "Song", "uploader": "Artist", "duration": 214})

    assert picked["id"] == "audio"


@pytest.mark.parametrize("track", [
    {"title": "Song", "uploader": "Artist", "duration": 0},
    {"title": "Song", "uploader": "Artist", "duration": 600},
    {"title": "Interlude", "uploader": "Artist", "duration": 214},
])
def test_track_match_falls_back_to_the_top_result(service, track):
    entries = [entry("top", "Artist - Song", 250), entry("other", "Artist - Song (Live)", 214)]

    assert service._pick_track_match(entries, track)["id"] == "top"


async def test_imports_search_five_results_on_the_import_workers(bot, service, searches):
    answers, calls = searches
    answers["Song Artist"] = [entry("mv", "Artist - Song", 260), entry("topic", "Song", 200, "Artist - Topic")]

    found = [s async for s in service.iter_youtube_matches([{"title": "Song", "uploader": "Artist, Guest", "duration": 200}])]

    assert [s["id"] for s in found] == ["topic"]
    assert calls == [("Song Artist", ms.YOUTUBE_MATCH_CANDIDATES, bot.import_executor)]


# Spotify

def embed(entity: dict) -> str:
    data = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'


@pytest.mark.parametrize(("url", "expected"), [
    ("https://open.spotify.com/playlist/PL1?si=x", ("playlist", "PL1")),
    ("https://open.spotify.com/intl-de/album/AL1", ("album", "AL1")),
    ("https://open.spotify.com/embed/artist/AR1", ("artist", "AR1")),
    ("https://open.spotify.com/track/TR1", None),
    ("https://evil.example/spotify.com/playlist/PL1", None),
    ("spotify.com wrapped playlist/", None),
])
def test_spotify_collection(url, expected):
    assert spotify_collection(url) == expected


async def test_spotify_track_uses_the_small_embed_page_and_its_duration(bot, service, searches, monkeypatch):
    bot.spotify = None
    fetched = []

    async def fake_page(url):
        fetched.append(url)
        if url == "https://open.spotify.com/embed/track/TR1":
            return embed({"title": "expectations", "duration": 223_935,
                          "artists": [{"name": "Olivia Rodrigo"}, {"name": "Chappell Roan"}]})
        return None

    monkeypatch.setattr(service, "_fetch_spotify_page", fake_page)
    answers, _ = searches
    answers["expectations Olivia Rodrigo"] = [
        entry("mv", "Olivia Rodrigo - expectations (Official Video)", 260),
        entry("audio", "expectations", 224, "Olivia Rodrigo - Topic"),
    ]

    song = await service.get_song_info("https://open.spotify.com/track/TR1?si=1")

    assert song["id"] == "audio"
    assert fetched == ["https://open.spotify.com/embed/track/TR1"]


async def test_spotify_playlist_skips_podcast_episodes(service, monkeypatch):
    async def fake_page(url):
        return embed({"trackList": [
            {"title": "Song", "subtitle": "Artist", "uri": "spotify:track:t1", "duration": 200_000, "entityType": "track"},
            {"title": "Episode 4", "subtitle": "Podcast", "uri": "spotify:episode:e1", "duration": 3_000_000,
             "entityType": "episode"},
        ]})

    monkeypatch.setattr(service, "_fetch_spotify_page", fake_page)

    tracks = await service.spotify_tracks("https://open.spotify.com/artist/AR1")

    assert [t["title"] for t in tracks] == ["Song"]


async def test_spotify_page_fetch_only_goes_to_spotify(service, pages):
    _, asked = pages
    await service._fetch_spotify_page("https://open.spotify.com/track/x?si=1")
    assert asked == ["https://open.spotify.com/track/x"]


# Apple Music

def itunes(*results) -> str:
    return json.dumps({"resultCount": len(results), "results": list(results)})


def itunes_song(track_id, name, artist, ms_, disc=1, number=1):
    return {"wrapperType": "track", "kind": "song", "trackId": track_id, "trackName": name, "artistName": artist,
            "trackTimeMillis": ms_, "discNumber": disc, "trackNumber": number}


async def test_apple_song_uses_the_storefront_and_runtime(service, pages, searches):
    served, asked = pages
    answers, _ = searches
    served_url = "https://itunes.apple.com/lookup?id=456&country=gb&entity=song"
    served[served_url] = itunes(itunes_song(456, "Song", "Artist", 214_000))
    answers["Song Artist"] = [entry("mv", "Artist - Song", 260), entry("topic", "Song", 214, "Artist - Topic")]

    song = await service.get_song_info("https://music.apple.com/gb/album/song/123?i=456")

    assert song["id"] == "topic"
    assert asked == [served_url]


async def test_apple_album_lists_every_track_in_order(service, pages, searches):
    served, _ = pages
    answers, _ = searches
    served["https://itunes.apple.com/lookup?id=123&country=us&entity=song&limit=200"] = itunes(
        {"wrapperType": "collection", "collectionId": 123},
        itunes_song(2, "Second", "Artist", 200_000, number=2),
        itunes_song(1, "First", "Artist", 180_000, number=1),
    )
    answers["First Artist"] = [entry("one", "Artist - First", 180)]
    answers["Second Artist"] = [entry("two", "Artist - Second", 200)]
    url = "https://music.apple.com/us/album/name/123"

    tracks = await service.collection_tracks(url)
    songs = await service.get_song_info(url)

    assert track_collection(url) == ("apple", "album", "123")
    assert [(t["title"], t["duration"], t["webpage_url"]) for t in tracks] == [
        ("First", 180, "https://music.apple.com/us/song/1"), ("Second", 200, "https://music.apple.com/us/song/2"),
    ]
    assert [s["id"] for s in songs] == ["one", "two"]


async def test_apple_playlist_is_read_from_the_page(service, pages):
    served, _ = pages
    ld = {"@type": "MusicPlaylist", "track": [
        {"@type": "MusicRecording", "name": "expectations", "url": "https://music.apple.com/us/song/expectations/11",
         "duration": "PT3M43S"},
        {"@type": "MusicRecording", "name": "Housewife", "url": "https://music.apple.com/us/song/housewife/22",
         "duration": "PT3M28S"},
    ]}
    served["https://music.apple.com/us/playlist/todays-hits/pl.abc"] = (
        f'<script id=schema:music-playlist type="application/ld+json">{json.dumps(ld)}</script>'
    )
    served["https://itunes.apple.com/lookup?id=11%2C22&country=us"] = itunes(itunes_song(11, "expectations", "Olivia Rodrigo", 223_935))
    url = "https://music.apple.com/us/playlist/todays-hits/pl.abc"

    tracks = await service.collection_tracks(url)

    assert track_collection(url) == ("apple", "playlist", "pl.abc")
    assert [(t["title"], t["uploader"], t["duration"]) for t in tracks] == [
        ("expectations", "Olivia Rodrigo", 223), ("Housewife", "", 208),
    ]


async def test_apple_link_without_a_lookup_falls_back_to_the_page_title(service, pages, searches):
    served, _ = pages
    answers, _ = searches
    served["https://music.apple.com/us/artist/olivia-rodrigo/979458609"] = (
        '<meta property="og:title" content="Olivia Rodrigo on Apple Music">'
    )
    answers["Olivia Rodrigo"] = [entry("x", "Olivia Rodrigo - drivers license", 242)]

    song = await service.get_song_info("https://music.apple.com/us/artist/olivia-rodrigo/979458609")

    assert song["id"] == "x"


# Tidal

def tidal_page(block: dict, og_title: str = "") -> str:
    return (f'<meta property="og:title" content="{og_title}">'
            f'<script type="application/ld+json">{json.dumps(block)}</script>')


async def test_tidal_track_uses_its_artist_and_runtime(service, pages, searches):
    served, asked = pages
    answers, _ = searches
    served["https://tidal.com/track/77"] = tidal_page({
        "@type": "MusicRecording", "name": "I&#39;M THAT GIRL", "duration": "PT3M28S",
        "byArtist": [{"@type": "MusicGroup", "name": "Beyoncé"}],
    })
    answers["I'M THAT GIRL Beyoncé"] = [entry("a", "Beyoncé - I'M THAT GIRL", 208)]

    song = await service.get_song_info("https://listen.tidal.com/album/1/track/77")

    assert song["id"] == "a"
    assert asked == ["https://tidal.com/track/77"]


async def test_tidal_album_is_listed_through_itunes(service, pages, searches):
    served, asked = pages
    answers, _ = searches
    served["https://tidal.com/album/9"] = tidal_page(
        {"@type": "MusicAlbum", "name": "RENAISSANCE", "byArtist": [{"name": "Beyoncé"}]}
    )
    search_url = "https://itunes.apple.com/search?term=Beyonc%C3%A9+RENAISSANCE&entity=album&limit=5"
    served[search_url] = itunes({"collectionId": 5, "collectionName": "RENAISSANCE", "artistName": "Beyoncé"})
    served["https://itunes.apple.com/lookup?id=5&country=us&entity=song&limit=200"] = itunes(
        itunes_song(1, "I'M THAT GIRL", "Beyoncé", 208_000),
    )
    answers["I'M THAT GIRL Beyoncé"] = [entry("a", "Beyoncé - I'M THAT GIRL", 208)]

    songs = await service.get_song_info("https://tidal.com/browse/album/9")

    assert [s["id"] for s in songs] == ["a"]
    assert search_url in asked


async def test_tidal_album_without_a_track_list_still_plays_something(service, pages, searches):
    served, _ = pages
    answers, _ = searches
    served["https://tidal.com/album/9"] = tidal_page(
        {"@type": "MusicAlbum", "name": "RENAISSANCE", "byArtist": [{"name": "Beyoncé"}]}
    )
    answers["Beyoncé RENAISSANCE"] = [entry("full", "Beyoncé - RENAISSANCE", 3000)]

    song = await service.get_song_info("https://tidal.com/album/9")

    assert song["id"] == "full"


async def test_tidal_playlist_title_is_cleaned_before_searching(service, pages, searches):
    served, _ = pages
    answers, calls = searches
    served["https://tidal.com/playlist/abc"] = "<title>Summer &amp; Sun by TIDAL on TIDAL</title>"
    answers["Summer & Sun by TIDAL"] = [entry("s", "Summer Song", 200)]

    song = await service.get_song_info("https://tidal.com/playlist/abc")

    assert song["id"] == "s" and calls[0][0] == "Summer & Sun by TIDAL"


# Bounded, allowlisted fetches

class FakeContent:
    def __init__(self, chunks, read):
        self.chunks, self.read = chunks, read

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            self.read.append(len(chunk))
            yield chunk


class FakeResponse:
    def __init__(self, status=200, chunks=(), headers=None, content_type="text/html", read=None):
        self.status, self.headers, self.content_type, self.charset = status, headers or {}, content_type, "utf-8"
        self.content = FakeContent(list(chunks), read if read is not None else [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses, self.urls = responses, []

    def get(self, url, **kwargs):
        self.urls.append((url, kwargs.get("allow_redirects")))
        return self.responses[url]


@pytest.fixture
def session(service, monkeypatch):
    holder = {}

    @contextlib.asynccontextmanager
    async def fake_http():
        yield holder["session"]

    monkeypatch.setattr(service, "_http", fake_http)
    return holder


async def test_redirects_off_the_allowlist_are_refused(service, session):
    session["session"] = FakeSession({
        "https://tidal.com/track/1": FakeResponse(301, headers={"Location": "http://169.254.169.254/latest"}),
    })

    assert await service._fetch_text("https://tidal.com/track/1", ("tidal.com",)) is None
    assert session["session"].urls == [("https://tidal.com/track/1", False)]


async def test_redirects_within_the_site_are_followed(service, session):
    session["session"] = FakeSession({
        "https://tidal.com/browse/track/1": FakeResponse(301, headers={"Location": "/track/1"}),
        "https://tidal.com/track/1": FakeResponse(chunks=[b"<title>ok</title>"]),
    })

    assert await service._fetch_text("https://tidal.com/browse/track/1", ("tidal.com",)) == "<title>ok</title>"


async def test_body_is_read_only_up_to_the_cap(service, session):
    read = []
    session["session"] = FakeSession({
        "https://tidal.com/x": FakeResponse(chunks=[b"a" * 65536] * 100, read=read),
    })

    text = await service._fetch_text("https://tidal.com/x", ("tidal.com",), cap=100_000)

    assert len(text) == 100_000 and len(read) == 2


async def test_title_scrape_ignores_non_html(service, session):
    session["session"] = FakeSession({
        "https://tidal.com/playlist/abc": FakeResponse(chunks=[b"\x00" * 10], content_type="application/octet-stream"),
    })

    assert await service._search_from_url_path("https://tidal.com/playlist/abc") is None


def test_iso_durations():
    assert ms._iso_seconds("PT3M43S") == 223
    assert ms._iso_seconds("PT1H2M3S") == 3723
    assert ms._iso_seconds("") == 0


def test_tidal_lookup_url_is_canonical():
    assert ms._tidal_parts("https://listen.tidal.com/album/1/track/77?u") == ("track", "77")
    assert ms._tidal_parts("https://evil.example/tidal.com/track/1") is None
