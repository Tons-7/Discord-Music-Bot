"""Metadata of songs already listed to Activity clients (search, favorites, playlists).

Lets /queue/add enqueue a listed song without extracting it again; playback
resolves the stream URL when the song starts.
"""

from collections import OrderedDict

_MAX_ENTRIES = 2000
_FIELDS = ("title", "duration", "thumbnail", "uploader", "album")

_songs: OrderedDict[str, dict] = OrderedDict()


def remember(songs: list[dict]) -> None:
    for song in songs:
        url = song.get("webpage_url")
        # No duration means a live stream, which needs a full extraction.
        if not url or not song.get("title") or not song.get("duration"):
            continue
        _songs[url] = {field: song[field] for field in _FIELDS if song.get(field)}
        _songs.move_to_end(url)

    while len(_songs) > _MAX_ENTRIES:
        _songs.popitem(last=False)


def lookup(url: str) -> dict | None:
    known = _songs.get(url)
    if known is None:
        return None
    return {**known, "url": url, "webpage_url": url}
