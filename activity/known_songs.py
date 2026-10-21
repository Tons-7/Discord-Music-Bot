"""Metadata of songs already listed to Activity clients (search, favorites, playlists).

Lets /queue/add enqueue a listed song without extracting it again; playback
resolves the stream URL when the song starts.

What the server resolved itself (search results) is shared. Songs listed from a user's saved data
(favorites, playlists) carry metadata that client requests once supplied, so they only serve that user:
nobody can attach a fake title to a URL other people will add.
"""

from collections import OrderedDict

_MAX_ENTRIES = 2000
_FIELDS = ("title", "duration", "thumbnail", "uploader", "album")

_songs: OrderedDict[str, dict] = OrderedDict()
_user_songs: OrderedDict[tuple[int, str], dict] = OrderedDict()


def remember(songs: list[dict], owner: int | None = None) -> None:
    """Remember listed songs: shared when the server resolved them, else only for ``owner``."""
    store: OrderedDict = _songs if owner is None else _user_songs
    for song in songs:
        url = song.get("webpage_url")
        # No duration means a live stream, which needs a full extraction.
        if not url or not song.get("title") or not song.get("duration"):
            continue
        key = url if owner is None else (owner, url)
        store[key] = {field: song[field] for field in _FIELDS if song.get(field)}
        store.move_to_end(key)

    while len(store) > _MAX_ENTRIES:
        store.popitem(last=False)


def lookup(url: str, owner: int | None = None) -> dict | None:
    known = _songs.get(url)
    if known is None and owner is not None:
        known = _user_songs.get((owner, url))
    if known is None:
        return None
    return {**known, "url": url, "webpage_url": url}


def clear() -> None:
    _songs.clear()
    _user_songs.clear()
