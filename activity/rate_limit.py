import time
from collections import deque

from fastapi import HTTPException

# (requests, seconds) per user for each kind of request that starts a yt-dlp extraction or download
LIMITS = {
    "search": (20, 10.0),
    "queue_add": (30, 10.0),
    "playlist_import": (10, 60.0),
}

_hits: dict[tuple[str, int], deque[float]] = {}


def check_rate(kind: str, user_id: int) -> None:
    """Refuse a user's request once they exceed the limit for this kind of request (429 with Retry-After)."""
    count, window = LIMITS[kind]
    now = time.monotonic()
    hits = _hits.setdefault((kind, user_id), deque())
    while hits and now - hits[0] > window:
        hits.popleft()
    if len(hits) >= count:
        retry_after = max(1, int(window - (now - hits[0])) + 1)
        raise HTTPException(
            status_code=429, detail="Slow down a little and try again in a moment",
            headers={"Retry-After": str(retry_after)},
        )
    hits.append(now)
    if len(_hits) > 5000:
        for key in [k for k, v in _hits.items() if not v or now - v[-1] > 300]:
            del _hits[key]
