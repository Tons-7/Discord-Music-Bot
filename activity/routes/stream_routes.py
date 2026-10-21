from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from activity import stream
from activity.auth import authenticate_query_or_header
from activity.dependencies import Bot, Member, WsManager, require_dj
from activity.helpers import activity_advance, broadcast_state
from activity.state_serializer import serialize_song

router = APIRouter(prefix="/api/guild/{guild_id}", tags=["stream"])


@router.get("/stream")
async def stream_current(guild_id: int, request: Request, bot: Bot):
    """Serve the current song's audio: the cached M4A, else a proxy of the source."""
    await authenticate_query_or_header(
        request.headers.get("Authorization"), request.query_params.get("token"), guild_id, bot,
    )

    current = bot.get_guild_data(guild_id).get("current")
    if not current:
        raise HTTPException(status_code=404, detail="Nothing is playing")

    cached_path = stream.cached_file(current.webpage_url)
    if cached_path:
        return FileResponse(
            cached_path,
            media_type="audio/mp4",
            headers={"Cache-Control": "no-cache", "Accept-Ranges": "bytes"},
        )

    stream.cache_song(bot, current)
    return await stream.proxy_stream(bot, current.webpage_url, request.headers.get("range"))


@router.post("/play")
async def play_song(
        guild_id: int, user: Member, bot: Bot, ws: WsManager,
        ended_url: str | None = None, ended_seq: int | None = None, force: bool = False,
):
    """Advance to the next song (Activity-only playback).

    force=true is a user-initiated skip; without it the advance is idempotent: a listener's "song ended"
    names the song (and its play number) so only the first of several listeners moves the room on.
    """
    # A forced advance is a skip, so it needs what /skip needs. The unforced
    # call is the end-of-song nudge any listener's client makes.
    if force:
        require_dj(bot, guild_id, int(user["id"]))

    new_current = await activity_advance(bot, ws, guild_id, ended_url=ended_url, ended_seq=ended_seq, force=force)
    await broadcast_state(bot, ws, guild_id)
    return {"ok": True, "current": serialize_song(new_current, bot, bot.get_guild(guild_id))}
