from types import SimpleNamespace

import pylast
import pytest

from models.song import Song
from services import music_service as ms

SEED_URL = "https://www.youtube.com/watch?v=seed"


class FakeTrack:
    def __init__(self, network, name, artist, similar=(), tags=()):
        self.network, self.name, self.artist = network, name, artist
        self.similar, self.tags = list(similar), list(tags)

    def get_name(self):
        return self.name

    def get_artist(self):
        return FakeArtist(self.network, self.artist)

    def get_similar(self, limit=None):
        self.network.calls.append(("track.getSimilar", self.artist, self.name))
        return [SimpleNamespace(item=FakeTrack(self.network, n, a)) for n, a in self.similar][:limit]

    def get_top_tags(self, limit=None):
        self.network.calls.append(("track.getTopTags", self.artist, self.name))
        return [SimpleNamespace(item=SimpleNamespace(get_name=lambda t=t: t)) for t in self.tags][:limit]

    def get_duration(self):
        return 200_000


class FakeArtist:
    def __init__(self, network, name):
        self.network, self.name = network, name

    def get_name(self):
        return self.name

    def get_similar(self, limit=None):
        self.network.calls.append(("artist.getSimilar", self.name))
        return [SimpleNamespace(item=FakeArtist(self.network, n)) for n in self.network.similar_artists.get(self.name, [])]

    def get_top_tracks(self, limit=None):
        self.network.calls.append(("artist.getTopTracks", self.name))
        return [SimpleNamespace(item=FakeTrack(self.network, n, self.name)) for n in self.network.top_tracks.get(self.name, [])]


class FakeLastfm:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []
        self.tracks = {}
        self.similar_artists = {}
        self.top_tracks = {}

    def add(self, artist, title, similar=(), tags=()):
        self.tracks[(artist.lower(), title.lower())] = FakeTrack(self, title, artist, similar, tags)

    def get_track(self, artist, title):
        self.calls.append(("get_track", artist, title))
        if self.fail or (artist.lower(), title.lower()) not in self.tracks:
            raise pylast.WSError(self, "6", "Track not found")
        return self.tracks[(artist.lower(), title.lower())]

    def get_artist(self, name):
        if self.fail:
            raise pylast.WSError(self, "6", "Artist not found")
        return FakeArtist(self, name)

    def get_tag(self, name):
        raise pylast.WSError(self, "6", "Tag not found")

    def search_for_track(self, artist, title):
        raise pylast.WSError(self, "6", "No results")


def video(video_id, title, uploader, duration=200, **extra):
    return {"id": video_id, "title": title, "uploader": uploader, "duration": duration,
            "url": f"https://www.youtube.com/watch?v={video_id}", **extra}


@pytest.fixture(autouse=True)
def fresh_caches(monkeypatch):
    ms._related_cache.clear()
    ms._mix_cache.clear()

    async def no_sleep(seconds):
        pass

    monkeypatch.setattr(ms.asyncio, "sleep", no_sleep)
    yield
    ms._related_cache.clear()
    ms._mix_cache.clear()


@pytest.fixture
def service(bot):
    return bot.music


@pytest.fixture
def lastfm(bot):
    bot.lastfm = FakeLastfm()
    return bot.lastfm


@pytest.fixture
def searches(service, monkeypatch):
    answers, queries = {}, []

    async def fake_search(query, limit, executor=None):
        queries.append(query)
        return [ms.MusicService._normalize_youtube_entry(e) for e in answers.get(query, [])]

    monkeypatch.setattr(service, "search_youtube_many", fake_search)
    return answers, queries


@pytest.fixture
def mix(service, monkeypatch):
    entries, calls = [], []

    def fake_extract(target, opts=None, **kwargs):
        calls.append((target, opts))
        return {"_type": "playlist", "entries": entries}

    monkeypatch.setattr(service, "_extract", fake_extract)
    return entries, calls


def seed(**overrides):
    return Song({"title": "Post Malone - Circles", "uploader": "PostMaloneVEVO", "webpage_url": SEED_URL,
                 "duration": 215, **overrides})


def circles_similar(lastfm):
    lastfm.add("Post Malone", "Circles", similar=[("Sunflower", "Post Malone"), ("Blinding Lights", "The Weeknd")])


async def test_lastfm_pick_prefers_another_artist(service, lastfm, searches):
    circles_similar(lastfm)
    answers, _ = searches
    answers["Sunflower Post Malone"] = [video("sun", "Post Malone - Sunflower", "PostMaloneVEVO")]
    answers["Blinding Lights The Weeknd"] = [video("bl", "The Weeknd - Blinding Lights", "TheWeekndVEVO")]

    related = await service.get_related_songs(seed(), limit=1)

    assert [s["title"] for s in related] == ["The Weeknd - Blinding Lights"]
    assert related[0]["autoplay_source"] == "lastfm"
    assert ("get_track", "Post Malone", "Circles") in lastfm.calls  # not 'PMalone'


async def test_excluded_songs_are_skipped_while_searching(service, lastfm, searches):
    circles_similar(lastfm)
    answers, _ = searches
    answers["Sunflower Post Malone"] = [video("sun", "Post Malone - Sunflower", "PostMaloneVEVO")]
    answers["Blinding Lights The Weeknd"] = [video("bl", "The Weeknd - Blinding Lights", "TheWeekndVEVO")]

    by_url = await service.get_related_songs(seed(), exclude_urls={"https://www.youtube.com/watch?v=bl"})
    by_filter = await service.get_related_songs(seed(), exclude=lambda s: "Blinding" in s["title"])

    assert [s["title"] for s in by_url] == ["Post Malone - Sunflower"]
    assert [s["title"] for s in by_filter] == ["Post Malone - Sunflower"]


async def test_the_next_round_for_a_seed_reuses_its_results(service, lastfm, searches):
    circles_similar(lastfm)
    answers, queries = searches
    answers["Sunflower Post Malone"] = [video("sun", "Post Malone - Sunflower", "PostMaloneVEVO")]
    answers["Blinding Lights The Weeknd"] = [video("bl", "The Weeknd - Blinding Lights", "TheWeekndVEVO")]

    await service.get_related_songs(seed(), limit=3)
    lastfm_calls, searched = len(lastfm.calls), len(queries)
    again = await service.get_related_songs(seed(), limit=3)

    assert {s["title"] for s in again} == {"Post Malone - Sunflower", "The Weeknd - Blinding Lights"}
    assert (len(lastfm.calls), len(queries)) == (lastfm_calls, searched)


async def test_failed_search_is_retried_next_round(service, lastfm, searches):
    lastfm.add("Post Malone", "Circles", similar=[("Blinding Lights", "The Weeknd")])
    answers, queries = searches

    assert await service.get_related_songs(seed()) == []
    answers["Blinding Lights The Weeknd"] = [video("bl", "The Weeknd - Blinding Lights", "TheWeekndVEVO")]
    related = await service.get_related_songs(seed())

    assert [s["title"] for s in related] == ["The Weeknd - Blinding Lights"]
    assert queries.count("Blinding Lights The Weeknd") == 2


async def test_youtube_music_metadata_seeds_lastfm(service, lastfm, searches):
    lastfm.add("Post Malone", "Circles", similar=[("Blinding Lights", "The Weeknd")])
    answers, _ = searches
    answers["Blinding Lights The Weeknd"] = [video("bl", "The Weeknd - Blinding Lights", "TheWeekndVEVO")]

    song = seed(title="Circles (Official Music Video)", uploader="Some Uploads", track="Circles", artist="Post Malone")
    related = await service.get_related_songs(song)

    assert related and lastfm.calls[0] == ("get_track", "Post Malone", "Circles")


MIX = [
    video("seed", "Post Malone - Circles", "PostMaloneVEVO"),
    video("lyr", "Post Malone - Circles (Lyrics)", "7clouds"),
    video("sun", "Post Malone - Sunflower", "PostMaloneVEVO"),
    video("loop", "Lofi Mix 1 hour", "Lofi Girl"),
    video("long", "Some Long Track", "Other", duration=3600),
    video("live", "Radio", "Station", live_status="is_live"),
    video("bl", "The Weeknd - Blinding Lights", "TheWeekndVEVO"),
]


async def test_without_lastfm_the_youtube_mix_is_used(bot, service, mix):
    bot.lastfm = None
    entries, calls = mix
    entries.extend(MIX)

    related = await service.get_related_songs(seed(), limit=2)

    assert [s["title"] for s in related] == ["The Weeknd - Blinding Lights", "Post Malone - Sunflower"]
    assert all(s["autoplay_source"] == "youtube_mix" for s in related)
    target, opts = calls[0]
    assert target == "https://www.youtube.com/watch?v=seed&list=RDseed"
    assert opts["playlistend"] == 25 and opts["noplaylist"] is False


async def test_mix_comes_before_an_unverified_lastfm_pick(service, lastfm, searches, mix):
    lastfm.add("Post Malone", "Circles", similar=[("Blinding Lights", "The Weeknd")])
    answers, _ = searches
    answers["Blinding Lights The Weeknd"] = [video("x", "Totally Different Video", "Someone")]
    entries, _ = mix
    entries.extend(MIX)

    related = await service.get_related_songs(seed())

    assert related[0]["autoplay_source"] == "youtube_mix"


async def test_unverified_lastfm_pick_is_the_last_resort(service, lastfm, searches, mix):
    lastfm.add("Post Malone", "Circles", similar=[("Blinding Lights", "The Weeknd")])
    answers, _ = searches
    answers["Blinding Lights The Weeknd"] = [video("x", "Totally Different Video", "Someone")]

    related = await service.get_related_songs(seed())

    assert [s["title"] for s in related] == ["Totally Different Video"]


async def test_lastfm_errors_fall_back_to_the_mix(bot, service, mix):
    bot.lastfm = FakeLastfm(fail=True)
    entries, _ = mix
    entries.extend(MIX)

    related = await service.get_related_songs(seed())

    assert [s["title"] for s in related] == ["The Weeknd - Blinding Lights"]


async def test_nothing_to_go_on_gives_nothing(bot, service, mix):
    bot.lastfm = None

    assert await service.get_related_songs(seed(webpage_url="https://soundcloud.com/a/b")) == []
    assert await service.get_related_songs(None) == []
    assert mix[1] == []
