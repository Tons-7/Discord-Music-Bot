import asyncio
import json
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
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
# The room keeps its Activity-only playback this long after the last viewer leaves, so a reload doesn't stop it.
LAST_VIEWER_GRACE = 20.0


@dataclass
class Viewer:
    user_id: int
    instance_id: str | None = None
    channel_id: int | None = None
    # Session-token expiry (epoch seconds) when the socket authenticated with one
    expires_at: float | None = None


class ConnectionManager:
    def __init__(self):
        self._connections: dict[int, set[WebSocket]] = {}
        self._viewers: dict[int, dict[WebSocket, Viewer]] = {}  # guild_id -> {ws: viewer}
        self._last_seen: dict[WebSocket, float] = {}
        self._reaper_task: asyncio.Task | None = None
        self._grace_timers: dict[int, asyncio.Task] = {}
        self._on_last_disconnect: Callable[[int, set[int]], Coroutine] | None = None

    def set_on_last_disconnect(self, callback: Callable[[int, set[int]], Coroutine]):
        """Called LAST_VIEWER_GRACE seconds after a guild's last socket closed, unless someone came back."""
        self._on_last_disconnect = callback

    async def connect(self, websocket: WebSocket, guild_id: int, user_id: int = 0, *, accept: bool = True,
                      viewer: Viewer | None = None):
        if accept:
            await websocket.accept()
        self._connections.setdefault(guild_id, set()).add(websocket)
        self._viewers.setdefault(guild_id, {})[websocket] = viewer or Viewer(user_id)
        self._last_seen[websocket] = time.monotonic()
        timer = self._grace_timers.pop(guild_id, None)
        if timer and not timer.done():
            timer.cancel()
        if self._reaper_task is None:
            self._reaper_task = spawn(self._reap_loop())
        logger.info(f"Activity WS connected for guild {guild_id} (total: {len(self._connections[guild_id])})")

    def touch(self, websocket: WebSocket):
        self._last_seen[websocket] = time.monotonic()

    def viewer(self, websocket: WebSocket, guild_id: int) -> Viewer | None:
        return self._viewers.get(guild_id, {}).get(websocket)

    def disconnect(self, websocket: WebSocket, guild_id: int):
        self._last_seen.pop(websocket, None)
        # The reaper and the route's finally both call this; a socket already
        # gone must not re-run the bookkeeping against a reconnected socket set.
        if websocket not in self._connections.get(guild_id, ()):
            return
        # Captured before cleanup: stats are credited to the last viewers once the grace period ends.
        all_user_ids = self.get_connected_user_ids(guild_id)
        self._connections[guild_id].discard(websocket)
        self._viewers.get(guild_id, {}).pop(websocket, None)
        if not self._connections[guild_id]:
            del self._connections[guild_id]
            self._viewers.pop(guild_id, None)
            if self._on_last_disconnect:
                old = self._grace_timers.pop(guild_id, None)
                if old and not old.done():
                    old.cancel()
                self._grace_timers[guild_id] = spawn(self._after_grace(guild_id, all_user_ids))
        logger.info(f"Activity WS disconnected for guild {guild_id}")

    async def _after_grace(self, guild_id: int, user_ids: set[int]):
        try:
            await asyncio.sleep(LAST_VIEWER_GRACE)
        except asyncio.CancelledError:
            return
        if self._grace_timers.get(guild_id) is asyncio.current_task():
            del self._grace_timers[guild_id]
        if self.has_connections(guild_id) or not self._on_last_disconnect:
            return
        await self._on_last_disconnect(guild_id, user_ids)

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
            await self._close(ws, 1001)
            self.disconnect(ws, guild_id)

    async def close_user(self, guild_id: int, user_id: int, *, code: int = 4003, reason: str = ""):
        """Close every socket a user has open for this guild (kicked, banned, session over)."""
        sockets = [ws for ws, viewer in list(self._viewers.get(guild_id, {}).items()) if viewer.user_id == user_id]
        for ws in sockets:
            await self._close(ws, code, reason)
            self.disconnect(ws, guild_id)

    async def close_guild(self, guild_id: int, *, code: int = 4004, reason: str = "The music bot isn't in this server"):
        for ws in list(self._connections.get(guild_id, ())):
            await self._close(ws, code, reason)
            self.disconnect(ws, guild_id)
        timer = self._grace_timers.pop(guild_id, None)
        if timer and not timer.done():
            timer.cancel()

    def get_connected_user_ids(self, guild_id: int) -> set[int]:
        return {viewer.user_id for viewer in self._viewers.get(guild_id, {}).values()}

    def get_viewers(self, guild_id: int) -> list[Viewer]:
        return list(self._viewers.get(guild_id, {}).values())

    def has_connections(self, guild_id: int) -> bool:
        return bool(self._connections.get(guild_id))

    def get_guild_ids_with_connections(self) -> list[int]:
        return list(self._connections.keys())

    async def broadcast(self, guild_id: int, event_type: str, data: Any):
        connections = list(self._connections.get(guild_id, ()))
        if not connections:
            return

        # Encoded once for every socket
        text = json.dumps({"type": event_type, "data": data}, separators=(",", ":"))
        results = await asyncio.gather(
            *(asyncio.wait_for(ws.send_text(text), SEND_TIMEOUT) for ws in connections),
            return_exceptions=True,
        )
        for ws, result in zip(connections, results, strict=True):
            if isinstance(result, Exception):
                logger.debug(f"WS send failed, disconnecting: {result!r}")
                self.disconnect(ws, guild_id)
                # Close so a live client reconnects for a fresh snapshot.
                spawn(self._close(ws, 1011))

    @staticmethod
    async def _close(ws: WebSocket, code: int, reason: str = ""):
        try:
            await asyncio.wait_for(ws.close(code=code, reason=reason or None), SEND_TIMEOUT)
        except Exception as e:
            logger.debug(f"WS close ({code}) failed: {e!r}")
