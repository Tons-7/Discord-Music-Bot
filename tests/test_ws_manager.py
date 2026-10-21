import asyncio
import json

from activity import tasks, ws_manager
from tests.conftest import GUILD_ID


class FakeSocket:
    def __init__(self, stuck: bool = False):
        self.stuck = stuck
        self.received = []
        self.close_code = None

    async def accept(self):
        pass

    async def send_text(self, text):
        if self.stuck:
            await asyncio.sleep(3600)
        self.received.append(json.loads(text))

    async def close(self, code=1000, reason=None):
        self.close_code = code


async def test_stuck_client_is_dropped_without_blocking_the_rest(monkeypatch):
    monkeypatch.setattr(ws_manager, "SEND_TIMEOUT", 0.1)
    manager = ws_manager.ConnectionManager()
    healthy, stuck = FakeSocket(), FakeSocket(stuck=True)
    await manager.connect(healthy, GUILD_ID)
    await manager.connect(stuck, GUILD_ID)

    await asyncio.wait_for(manager.broadcast(GUILD_ID, "POSITION_UPDATE", {"position": 1}), timeout=1)
    await asyncio.sleep(0.05)

    assert len(healthy.received) == 1
    assert manager.get_connected_user_ids(GUILD_ID) == {0}
    assert stuck not in manager._connections[GUILD_ID]
    assert stuck.close_code == 1011

    await tasks.cancel_all()
