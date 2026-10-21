from utils.helpers import md


def _seconds(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _first_artist(data: dict) -> str:
    artists = data.get("artists")
    if isinstance(artists, list | tuple) and artists:
        return artists[0] or ""
    return ""


class Song:
    def __init__(self, data: dict):
        # yt-dlp reports missing metadata as explicit None, so `or` the defaults in.
        self.url = data.get("url") or ""
        self.title = data.get("title") or "Unknown Title"
        self.duration = _seconds(data.get("duration"))
        self.album = data.get("album") or ""
        self.track = data.get("track") or ""
        self.artist = data.get("artist") or _first_artist(data)
        self.thumbnail = data.get("thumbnail") or ""
        self.uploader = data.get("uploader") or "Unknown"
        self.webpage_url = data.get("webpage_url") or ""
        self.requested_by = data.get("requested_by") or "Unknown"
        self.is_live = bool(data.get("is_live"))
        # Where an autoplay pick came from ("lastfm" or "youtube_mix"); empty for requested songs
        self.autoplay_source = data.get("autoplay_source") or ""

    @property
    def linked_title(self):
        """Bold title as a clickable markdown link when URL is available."""
        if self.webpage_url:
            return f"**[{md(self.title)}](<{self.webpage_url}>)**"
        return f"**{md(self.title)}**"

    def __str__(self):
        return f"{self.linked_title} by {md(self.uploader)}"

    def to_dict(self):
        return {
            # The live url is an expiring stream URL or a local cache path; only the page URL is worth keeping.
            "url": self.webpage_url or self.url,
            "title": self.title,
            "duration": self.duration,
            "album": self.album,
            "track": self.track,
            "artist": self.artist,
            "thumbnail": self.thumbnail,
            "uploader": self.uploader,
            "webpage_url": self.webpage_url,
            "requested_by": self.requested_by,
            "is_live": self.is_live,
            **({"autoplay_source": self.autoplay_source} if self.autoplay_source else {}),
        }

    @classmethod
    def from_dict(cls, data: dict):
        return cls(data)
