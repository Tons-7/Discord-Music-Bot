import logging

from fastapi import APIRouter, Depends, HTTPException
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


@router.delete("/{position}")
async def remove_favorite(position: int, user=Depends(get_current_user), bot=Depends(get_bot)):
    uid = int(user["id"])
    success = await bot.remove_favorite(uid, position)
    if not success:
        raise HTTPException(status_code=404, detail="Invalid position")
    return {"ok": True}
