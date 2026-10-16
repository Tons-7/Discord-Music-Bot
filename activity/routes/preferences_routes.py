from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from activity.dependencies import get_bot, get_current_user

router = APIRouter(prefix="/api/me/preferences", tags=["preferences"])


class PreferencesBody(BaseModel):
    # Theme ids are defined by the frontend (lib/themes.ts); unknown ones fall back to the default there.
    theme: str = Field(pattern=r"^[a-z0-9-]{1,32}$")


@router.get("")
async def get_preferences(user=Depends(get_current_user), bot=Depends(get_bot)):
    return {"theme": await bot.get_user_theme(int(user["id"]))}


@router.put("")
async def set_preferences(body: PreferencesBody, user=Depends(get_current_user), bot=Depends(get_bot)):
    await bot.set_user_theme(int(user["id"]), body.theme)
    return {"theme": body.theme}
