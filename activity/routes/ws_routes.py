import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from activity.auth import get_discord_user
from activity.permissions import check_banned
from activity.state_serializer import serialize_guild_state

logger = logging.getLogger(__name__)
router = APIRouter(tags=["websocket"])


@router.websocket("/ws/guild/{guild_id}")
async def guild_websocket(websocket: WebSocket, guild_id: int):
    app = websocket.app
    bot = app.state.bot
    ws_manager = app.state.ws_manager

    # Authenticate via query param
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return

    user = await get_discord_user(token)
    if not user:
        await websocket.close(code=4001, reason="Invalid token")
        return

    user_id = int(user["id"])
    if check_banned(user_id):
        await websocket.close(code=4003, reason="Banned")
        return

    # Check guild membership
    guild = bot.get_guild(guild_id)
    if not guild:
        await websocket.close(code=4004, reason="Bot not in guild")
        return

    member = guild.get_member(user_id)
    if not member:
        await websocket.close(code=4003, reason="Not a guild member")
        return

    # Accept and register
    await ws_manager.connect(websocket, guild_id, user_id)

    try:
        # Send initial state
        state = serialize_guild_state(bot, guild_id)
        await websocket.send_json({"type": "STATE_UPDATE", "data": state})

        # Listen for commands (optional, REST is primary)
        while True:
            data = await websocket.receive_json()
            # Commands over WS are handled by REST endpoints
            # This loop just keeps the connection alive
            if data.get("type") == "PING":
                await websocket.send_json({"type": "PONG"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"WS error for guild {guild_id}: {e}")
    finally:
        ws_manager.disconnect(websocket, guild_id)
