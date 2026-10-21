import logging
import sqlite3
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, BeforeValidator, Field

from activity import known_songs
from activity.dependencies import Bot, User
from bot import FavoritesFull
from models.song import Song

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/favorites", tags=["favorites"])


def _whole_seconds(value):
    """SoundCloud reports fractional durations; favorites store whole seconds."""
    if isinstance(value, float):
        return round(value)
    return value


@router.get("")
async def get_favorites(user: User, bot: Bot):
    uid = int(user["id"])
    favorites = await bot.get_favorites(uid)
    known_songs.remember(favorites, owner=uid)
    return {"favorites": favorites}


class FavoriteBody(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    url: str = Field("", max_length=2048)
    duration: Annotated[int, BeforeValidator(_whole_seconds), Field(ge=0, le=7 * 24 * 3600)] = 0
    thumbnail: str = Field("", max_length=2048)
    uploader: str = Field("", max_length=200)
    webpage_url: str = Field("", max_length=2048)


@router.post("")
async def add_favorite(body: FavoriteBody, user: User, bot: Bot):
    uid = int(user["id"])
    webpage_url = body.webpage_url or body.url
    if not webpage_url.startswith(("https://", "http://")):
        raise HTTPException(status_code=400, detail="That song has no link to save")

    # What the server resolved for this URL beats what the client sent.
    known = known_songs.lookup(webpage_url) or {}
    song = Song({
        "title": known.get("title") or body.title,
        "duration": known.get("duration") or body.duration,
        "thumbnail": known.get("thumbnail") or body.thumbnail,
        "uploader": known.get("uploader") or body.uploader,
        "webpage_url": webpage_url,
        "requested_by": f"<@{uid}>",
    })
    try:
        added = await bot.add_favorite(uid, song)
    except FavoritesFull as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail="Couldn't save the favorite. Try again.") from e
    if not added:
        raise HTTPException(status_code=409, detail="Song already in favorites")
    return {"ok": True}


@router.delete("")
async def remove_favorite_by_url(user: User, bot: Bot, url: str = Query(...)):
    success, title = await bot.remove_favorite_by_url(int(user["id"]), url)
    if not success:
        raise HTTPException(status_code=404, detail="Favorite not found")
    return {"ok": True, "title": title}


@router.delete("/{position}")
async def remove_favorite(position: int, user: User, bot: Bot, url: str | None = Query(None)):
    uid = int(user["id"])

    # The URL names the song exactly; a position from a stale list could remove another one.
    if url is not None:
        success, title = await bot.remove_favorite_by_url(uid, url)
        if not success:
            raise HTTPException(status_code=404, detail="Favorite not found")
        return {"ok": True, "title": title}

    if not await bot.remove_favorite(uid, position):
        raise HTTPException(status_code=404, detail="Invalid position")
    return {"ok": True}
