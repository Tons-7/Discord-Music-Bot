from fastapi import APIRouter, HTTPException

from activity.dependencies import Bot, Member
from utils.lyrics import LyricsServiceUnavailable, lyrics_for_song

router = APIRouter(prefix="/api/guild/{guild_id}", tags=["lyrics"])


def _find_song(guild_data: dict, url: str):
    current = guild_data.get("current")
    if current and current.webpage_url == url:
        return current
    for bucket in ("queue", "history"):
        for song in guild_data.get(bucket) or []:
            if song.webpage_url == url:
                return song
    return None


@router.get("/lyrics")
async def get_lyrics(guild_id: int, user: Member, bot: Bot, url: str | None = None):
    guild_data = bot.get_guild_data(guild_id)

    if url:
        song = _find_song(guild_data, url)
        if not song:
            raise HTTPException(status_code=404, detail="Song not found")
    else:
        song = guild_data.get("current")
        if not song:
            raise HTTPException(status_code=404, detail="Nothing is playing")

    # One lookup per song for every caller (the /lyrics command shares the cache); an instrumental comes back
    # with empty lyrics and instrumental=True.
    try:
        payload = await lyrics_for_song(song, session=bot.http_session)
    except LyricsServiceUnavailable as e:
        raise HTTPException(status_code=503, detail="Lyrics service is temporarily unavailable") from e
    if not payload:
        raise HTTPException(status_code=404, detail="Lyrics not found")
    return payload
