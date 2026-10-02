import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import WebSocket

from activity.tasks import spawn

logger = logging.getLogger(__name__)

# Half-open sockets never fail a send, so they would keep a guild "connected"
# forever. Clients ping every 25s; allow several misses before reaping.
STALE_AFTER = 600.0
REAP_INTERVAL = 30.0
# A stuck client would otherwise stall every broadcast until uvicorn's ping timeout.
SEND_TIMEOUT = 5.0


class ConnectionManager:
    def __init__(self):
        self._connections: dict[int, set[WebSocket]] = {}
        self._ws_user_ids: dict[int, dict[WebSocket, int]] = {}  # guild_id -> {ws: user_id}
        self._last_seen: dict[WebSocket, float] = {}
        self._reaper_task: asyncio.Task | None = None
        self._on_last_disconnect: Callable[[int, set[int]], Coroutine] | None = None

    def set_on_last_disconnect(self, callback: Callable[[int, set[int]], Coroutine]):
        self._on_last_disconnect = callback

    async def connect(self, websocket: WebSocket, guild_id: int, user_id: int = 0):
        await websocket.accept()
        if guild_id not in self._connections:
            self._connections[guild_id] = set()
            self._ws_user_ids[guild_id] = {}
        self._connections[guild_id].add(websocket)
        self._ws_user_ids[guild_id][websocket] = user_id
        self._last_seen[websocket] = time.monotonic()
        if self._reaper_task is None:
            self._reaper_task = spawn(self._reap_loop())
        logger.info(f"Activity WS connected for guild {guild_id} (total: {len(self._connections[guild_id])})")

    def touch(self, websocket: WebSocket):
        self._last_seen[websocket] = time.monotonic()

    def disconnect(self, websocket: WebSocket, guild_id: int):
        self._last_seen.pop(websocket, None)
        # The reaper and the route's finally both call this; a socket already
        # gone must not re-run the bookkeeping against a reconnected socket set.
        if websocket in self._connections.get(guild_id, ()):
            # Capture user IDs before cleanup (needed for stats recording)
            all_user_ids = set(self._ws_user_ids.get(guild_id, {}).values())

            self._connections[guild_id].discard(websocket)
            if guild_id in self._ws_user_ids:
                self._ws_user_ids[guild_id].pop(websocket, None)
            if not self._connections[guild_id]:
                del self._connections[guild_id]
                self._ws_user_ids.pop(guild_id, None)
                if self._on_last_disconnect:
                    spawn(self._on_last_disconnect(guild_id, all_user_ids))
        logger.info(f"Activity WS disconnected for guild {guild_id}")

    async def _reap_loop(self):
        try:
            while self._connections:
                await asyncio.sleep(REAP_INTERVAL)
                await self._reap_stale()
        finally:
            self._reaper_task = None

    async def _reap_stale(self):
        cutoff = time.monotonic() - STALE_AFTER
        # Snapshot first: disconnect() mutates both the set and the guild dict.
        stale = [
            (guild_id, ws)
            for guild_id, sockets in list(self._connections.items())
            for ws in list(sockets)
            if self._last_seen.get(ws, 0) < cutoff
        ]
        for guild_id, ws in stale:
            logger.info(f"Reaping stale Activity WS for guild {guild_id}")
            try:
                await asyncio.wait_for(ws.close(code=1001), SEND_TIMEOUT)
            except Exception as e:
                logger.debug(f"Stale WS close failed: {e}")
            self.disconnect(ws, guild_id)

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
            *(asyncio.wait_for(ws.send_json(message), SEND_TIMEOUT) for ws in connections),
            return_exceptions=True,
        )
        for ws, result in zip(connections, results, strict=True):
            if isinstance(result, Exception):
                logger.debug(f"WS send failed, disconnecting: {result!r}")
                self.disconnect(ws, guild_id)
                # Close so a live client reconnects for a fresh snapshot.
                spawn(self._close_quietly(ws))

    @staticmethod
    async def _close_quietly(ws: WebSocket):
        try:
            await asyncio.wait_for(ws.close(code=1011), SEND_TIMEOUT)
        except Exception as e:
            logger.debug(f"WS close after failed send: {e!r}")
