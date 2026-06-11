import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from activity.dependencies import get_bot, get_current_user
from models.song import Song

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/favorites", tags=["favorites"])


@router.get("")
async def get_favorites(user=Depends(get_current_user), bot=Depends(get_bot)):
    uid = int(user["id"])
    favorites = await bot.get_favorites(uid)
    return {"favorites": favorites}


class FavoriteBody(BaseModel):
    title: str
    url: str = ""
    duration: int = 0
    thumbnail: str = ""
    uploader: str = ""
    webpage_url: str = ""


@router.post("")
async def add_favorite(body: FavoriteBody, user=Depends(get_current_user), bot=Depends(get_bot)):
    uid = int(user["id"])
    song = Song({
        "url": body.url,
        "title": body.title,
        "duration": body.duration,
        "thumbnail": body.thumbnail,
        "uploader": body.uploader,
        "webpage_url": body.webpage_url or body.url,
        "requested_by": f"<@{uid}>",
    })
    success = await bot.add_favorite(uid, song)
    if not success:
        raise HTTPException(status_code=409, detail="Song already in favorites")
    return {"ok": True}


async def _remove_favorite_by_url(bot, uid: int, url: str):
    """Remove the favorite whose webpage_url matches `url`.

    Returns (success, title) so callers can surface the removed title.
    remove_favorite expects a 1-based position, matching get_favorites order.
    """
    favorites = await bot.get_favorites(uid)
    for index, fav in enumerate(favorites):
        if (fav.get("webpage_url") or fav.get("url")) == url:
            success = await bot.remove_favorite(uid, index + 1)
            return success, fav.get("title")
    return False, None


@router.delete("")
async def remove_favorite_by_url(url: str = Query(...), user=Depends(get_current_user), bot=Depends(get_bot)):
    uid = int(user["id"])
    success, title = await _remove_favorite_by_url(bot, uid, url)
    if not success:
        raise HTTPException(status_code=404, detail="Favorite not found")
    return {"ok": True, "title": title}


@router.delete("/{position}")
async def remove_favorite(
    position: int,
    url: str | None = Query(None),
    user=Depends(get_current_user),
    bot=Depends(get_bot),
):
    uid = int(user["id"])

    if url is not None:
        success, title = await _remove_favorite_by_url(bot, uid, url)
        if not success:
            raise HTTPException(status_code=404, detail="Favorite not found")
        return {"ok": True, "title": title}

    success = await bot.remove_favorite(uid, position)
    if not success:
        raise HTTPException(status_code=404, detail="Invalid position")
    return {"ok": True}
