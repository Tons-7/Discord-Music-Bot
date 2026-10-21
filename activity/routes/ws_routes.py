import logging
import time

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from activity.auth import is_session_token, require_member, user_id_from_token
from activity.session import verify_session_token
from activity.state_serializer import serialize_guild_state
from activity.ws_manager import Viewer

logger = logging.getLogger(__name__)
router = APIRouter(tags=["websocket"])

# Close codes the client acts on (activity-frontend/src/lib/ws.ts)
CLOSE_AUTH = 4001
CLOSE_FORBIDDEN = 4003
CLOSE_NO_BOT = 4004
CLOSE_TRY_AGAIN = 1013


def _close_code(error: HTTPException) -> int:
    if error.status_code == 401:
        return CLOSE_AUTH
    if error.status_code == 404:
        return CLOSE_NO_BOT
    if error.status_code == 403:
        return CLOSE_FORBIDDEN
    return CLOSE_TRY_AGAIN


def _optional_int(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None


def _session_expiry(token: str) -> float | None:
    session = verify_session_token(token) if is_session_token(token) else None
    return session["expires_at"] if session else None


@router.websocket("/ws/guild/{guild_id}")
async def guild_websocket(websocket: WebSocket, guild_id: int):
    bot = websocket.app.state.bot
    ws_manager = websocket.app.state.ws_manager

    # Accepted before the checks, so a refusal reaches the browser as a close code instead of a failed handshake.
    await websocket.accept()
    token = websocket.query_params.get("token")
    try:
        user_id = await user_id_from_token(bot, guild_id, token)
        require_member(bot, guild_id, user_id)
    except HTTPException as e:
        await websocket.close(code=_close_code(e), reason=str(e.detail)[:120])
        return

    viewer = Viewer(
        user_id=user_id,
        instance_id=(websocket.query_params.get("instance_id") or None),
        channel_id=_optional_int(websocket.query_params.get("channel_id")),
        expires_at=_session_expiry(token or ""),
    )
    await ws_manager.connect(websocket, guild_id, user_id, accept=False, viewer=viewer)

    try:
        await websocket.send_json({"type": "STATE_UPDATE", "data": serialize_guild_state(bot, guild_id)})

        # REST is the command channel; the socket only carries PINGs from the client.
        while True:
            data = await websocket.receive_json()
            ws_manager.touch(websocket)
            if data.get("type") != "PING":
                continue
            # Bans, kicks and expired sessions are rechecked on every ping (~25s).
            if viewer.expires_at is not None and viewer.expires_at < time.time():
                await websocket.close(code=CLOSE_AUTH, reason="Your session expired")
                return
            try:
                require_member(bot, guild_id, user_id)
            except HTTPException as e:
                await websocket.close(code=_close_code(e), reason=str(e.detail)[:120])
                return
            await websocket.send_json({"type": "PONG"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"WS error for guild {guild_id}: {e}")
    finally:
        ws_manager.disconnect(websocket, guild_id)

