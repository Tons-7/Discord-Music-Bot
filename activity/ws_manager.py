import asyncio
import logging
from typing import Any, Callable, Coroutine

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self):
        self._connections: dict[int, set[WebSocket]] = {}
        self._ws_user_ids: dict[int, dict[WebSocket, int]] = {}  # guild_id -> {ws: user_id}
        self._on_last_disconnect: Callable[[int], Coroutine] | None = None

    def set_on_last_disconnect(self, callback: Callable[[int], Coroutine]):
        self._on_last_disconnect = callback

    async def connect(self, websocket: WebSocket, guild_id: int, user_id: int = 0):
        await websocket.accept()
        if guild_id not in self._connections:
            self._connections[guild_id] = set()
            self._ws_user_ids[guild_id] = {}
        self._connections[guild_id].add(websocket)
        self._ws_user_ids[guild_id][websocket] = user_id
        logger.info(f"Activity WS connected for guild {guild_id} (total: {len(self._connections[guild_id])})")

    def disconnect(self, websocket: WebSocket, guild_id: int):
        if guild_id in self._connections:
            # Capture user IDs before cleanup (needed for stats recording)
            all_user_ids = set(self._ws_user_ids.get(guild_id, {}).values())

            self._connections[guild_id].discard(websocket)
            if guild_id in self._ws_user_ids:
                self._ws_user_ids[guild_id].pop(websocket, None)
            if not self._connections[guild_id]:
                del self._connections[guild_id]
                self._ws_user_ids.pop(guild_id, None)
                if self._on_last_disconnect:
                    asyncio.create_task(self._on_last_disconnect(guild_id, all_user_ids))
        logger.info(f"Activity WS disconnected for guild {guild_id}")

    def get_connected_user_ids(self, guild_id: int) -> set[int]:
        if guild_id not in self._ws_user_ids:
            return set()
        return set(self._ws_user_ids[guild_id].values())

    def has_connections(self, guild_id: int) -> bool:
        return guild_id in self._connections and len(self._connections[guild_id]) > 0

    def get_guild_ids_with_connections(self) -> list[int]:
        return list(self._connections.keys())

    async def broadcast(self, guild_id: int, event_type: str, data: Any):
        if not self.has_connections(guild_id):
            return

        message = {"type": event_type, "data": data}
        connections = list(self._connections.get(guild_id, set()))
        if not connections:
            return

        results = await asyncio.gather(
            *(ws.send_json(message) for ws in connections),
            return_exceptions=True,
        )
        for ws, result in zip(connections, results):
            if isinstance(result, Exception):
                logger.debug(f"WS send failed, disconnecting: {result}")
                self.disconnect(ws, guild_id)
