import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from activity import auth, session
from activity.app import FrontendStaticFiles
from activity.routes import api_router
from activity.routes import ws_routes
from activity.ws_manager import ConnectionManager
from tests.conftest import GUILD_ID

ALICE, BOB, CAROL = 1, 2, 3
DJ_ROLE = 900


class FakeChannel(SimpleNamespace):
    pass


def member(user_id, *, roles=(), admin=False, channel=None):
    return SimpleNamespace(
        id=user_id, bot=False, display_name=f"user{user_id}", name=f"user{user_id}",
        roles=[SimpleNamespace(id=r) for r in roles],
        guild_permissions=SimpleNamespace(administrator=admin),
        voice=SimpleNamespace(channel=channel) if channel else None,
        display_avatar=None,
    )


class FakeGuild:
    def __init__(self, members, role_ids=(DJ_ROLE,)):
        self.id = GUILD_ID
        self.members = list(members)
        self._roles = {rid: SimpleNamespace(id=rid, name="DJ") for rid in role_ids}

    def get_member(self, user_id):
        return next((m for m in self.members if m.id == user_id), None)

    def get_role(self, role_id):
        return self._roles.get(role_id)


TOKENS = {"alice-token": ALICE, "bob-token": BOB, "carol-token": CAROL}


@pytest.fixture
def guild(bot, monkeypatch):
    g = FakeGuild([member(ALICE), member(BOB, roles=[DJ_ROLE])])
    monkeypatch.setattr(bot, "get_guild", lambda gid: g if gid == GUILD_ID else None)

    async def fake_user(_session, token):
        user_id = TOKENS.get(token)
        return {"id": str(user_id), "username": f"user{user_id}"} if user_id else None

    monkeypatch.setattr(auth, "get_discord_user", fake_user)
    monkeypatch.setattr("activity.dependencies.get_discord_user", fake_user)
    return g


@pytest.fixture
async def client(bot, ws, guild):
    app = FastAPI()
    app.include_router(api_router)
    app.state.bot = bot
    app.state.ws_manager = ws
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


# Route permissions through the real dependency chain (#430)

async def test_requests_without_a_token_are_refused(client):
    assert (await client.get(f"/api/guild/{GUILD_ID}/state")).status_code == 401


async def test_non_members_are_refused(client):
    response = await client.get(f"/api/guild/{GUILD_ID}/state", headers=bearer("carol-token"))
    assert response.status_code == 403


async def test_banned_users_are_refused(client, monkeypatch):
    monkeypatch.setattr("activity.permissions.is_banned", lambda uid: uid == ALICE)
    monkeypatch.setattr("activity.dependencies.check_banned", lambda uid: uid == ALICE)
    response = await client.get(f"/api/guild/{GUILD_ID}/state", headers=bearer("alice-token"))
    assert response.status_code == 403


async def test_dj_only_controls_need_the_dj_role(client, guild_data):
    guild_data["dj_role_id"] = DJ_ROLE
    refused = await client.post(f"/api/guild/{GUILD_ID}/shuffle", headers=bearer("alice-token"))
    allowed = await client.post(f"/api/guild/{GUILD_ID}/shuffle", headers=bearer("bob-token"))
    assert refused.status_code == 403 and "DJ role" in refused.json()["detail"]
    assert allowed.status_code == 200


async def test_a_deleted_dj_role_no_longer_locks_people_out(client, guild_data, guild):
    guild_data["dj_role_id"] = 12345  # not a role of the guild any more
    assert (await client.post(f"/api/guild/{GUILD_ID}/shuffle", headers=bearer("alice-token"))).status_code == 200
    settings = await client.get(f"/api/guild/{GUILD_ID}/settings", headers=bearer("alice-token"))
    assert settings.json()["dj_role_id"] is None


async def test_voice_playback_is_controlled_from_its_channel(client, guild_data, guild):
    room = FakeChannel(id=77, name="Lounge")
    guild_data["voice_client"] = SimpleNamespace(is_connected=lambda: True, channel=room)
    guild.members[1] = member(BOB, channel=room)

    outside = await client.post(f"/api/guild/{GUILD_ID}/shuffle", headers=bearer("alice-token"))
    inside = await client.post(f"/api/guild/{GUILD_ID}/shuffle", headers=bearer("bob-token"))
    assert outside.status_code == 403 and outside.json()["detail"] == "Join Lounge to control playback"
    assert inside.status_code == 200


async def test_activity_only_playback_is_open_to_every_member(client, guild_data):
    assert (await client.post(f"/api/guild/{GUILD_ID}/shuffle", headers=bearer("alice-token"))).status_code == 200


# Token lookups never flood Discord (#135, #139, #367)

class FakeDiscord:
    """Answers /users/@me like Discord: 200 for known tokens, 401 otherwise, or a fixed status."""

    def __init__(self, status=None, delay=0.0):
        self.calls = []
        self.status = status
        self.delay = delay

    def get(self, url, headers=None, timeout=None):
        token = headers["Authorization"].split(" ", 1)[1]
        self.calls.append(token)
        outer = self

        class Response:
            status = outer.status or (200 if token in TOKENS else 401)

            async def json(self):
                return {"id": str(TOKENS.get(token, 0)), "retry_after": 7}

            async def __aenter__(self):
                await asyncio.sleep(outer.delay)
                return self

            async def __aexit__(self, *exc):
                return False

        return Response()


@pytest.fixture
def fresh_auth(monkeypatch):
    monkeypatch.setattr(auth, "_users", type(auth._users)())
    monkeypatch.setattr(auth, "_rejected", type(auth._rejected)())
    monkeypatch.setattr(auth, "_inflight", {})
    monkeypatch.setattr(auth, "_lookups", type(auth._lookups)())
    monkeypatch.setattr(auth, "_discord_paused_until", 0.0)


async def test_our_own_session_tokens_are_never_sent_to_discord(fresh_auth):
    discord_api = FakeDiscord()
    token = session.mint_session_token(ALICE, GUILD_ID)
    assert await auth.get_discord_user(discord_api, token) is None
    assert discord_api.calls == []


async def test_a_rejected_token_is_not_sent_again(fresh_auth):
    discord_api = FakeDiscord()
    assert await auth.get_discord_user(discord_api, "junk") is None
    assert await auth.get_discord_user(discord_api, "junk") is None
    assert discord_api.calls == ["junk"]


async def test_concurrent_lookups_of_one_token_share_a_request(fresh_auth):
    discord_api = FakeDiscord(delay=0.05)
    users = await asyncio.gather(*(auth.get_discord_user(discord_api, "alice-token") for _ in range(5)))
    assert all(u["id"] == str(ALICE) for u in users)
    assert discord_api.calls == ["alice-token"]


async def test_unknown_tokens_stop_at_the_lookup_budget(fresh_auth, monkeypatch):
    monkeypatch.setattr(auth, "_LOOKUP_BUDGET", 3)
    discord_api = FakeDiscord()
    for i in range(3):
        await auth.get_discord_user(discord_api, f"junk{i}")
    with pytest.raises(auth.HTTPException) as refused:
        await auth.get_discord_user(discord_api, "junk-more")
    assert refused.value.status_code == 503
    assert len(discord_api.calls) == 3


async def test_a_discord_rate_limit_pauses_lookups(fresh_auth):
    with pytest.raises(auth.HTTPException) as limited:
        await auth.get_discord_user(FakeDiscord(status=429), "alice-token")
    assert limited.value.status_code == 503
    quiet = FakeDiscord()
    with pytest.raises(auth.HTTPException):
        await auth.get_discord_user(quiet, "bob-token")
    assert quiet.calls == []


async def test_a_session_token_for_another_guild_is_refused_without_discord(bot, guild):
    token = session.mint_session_token(ALICE, GUILD_ID + 1)
    with pytest.raises(auth.HTTPException) as refused:
        await auth.user_id_from_token(bot, GUILD_ID, token)
    assert refused.value.status_code == 401


async def test_stream_tokens_go_through_the_ban_and_membership_checks(bot, guild):
    stranger = session.mint_session_token(CAROL, GUILD_ID)
    with pytest.raises(auth.HTTPException) as refused:
        await auth.authenticate_query_or_header(None, stranger, GUILD_ID, bot)
    assert refused.value.status_code == 403


# Session tokens (#99)

def test_session_tokens_round_trip_and_expire(monkeypatch):
    token = session.mint_session_token(ALICE, GUILD_ID, ttl=60)
    verified = session.verify_session_token(token)
    assert (verified["user_id"], verified["guild_id"]) == (ALICE, GUILD_ID)
    assert auth.is_session_token(token)

    payload, signature = token.split(".")
    assert session.verify_session_token(f"{payload}.{'0' * 64}") is None
    monkeypatch.setattr(time, "time", lambda: verified["expires_at"] + 1)
    assert session.verify_session_token(token) is None


# WebSocket refusals reach the browser as close codes (#234, #2021)

class FakeWebSocket:
    def __init__(self, app, token=None, messages=()):
        self.app = app
        self.query_params = {"token": token} if token else {}
        self.accepted = False
        self.closed = None
        self.sent = []
        self._messages = list(messages)

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=None):
        self.closed = (code, reason)

    async def send_json(self, data):
        self.sent.append(data)

    async def send_text(self, text):
        self.sent.append(text)

    async def receive_json(self):
        if self.closed:
            raise ws_routes.WebSocketDisconnect(self.closed[0])
        if not self._messages:
            raise ws_routes.WebSocketDisconnect(1000)
        step = self._messages.pop(0)
        if callable(step):
            step()
            return {"type": "PING"}
        return step


@pytest.fixture
def ws_app(bot):
    manager = ConnectionManager()
    return SimpleNamespace(state=SimpleNamespace(bot=bot, ws_manager=manager))


@pytest.mark.parametrize(("token", "code"), [
    (None, 4001),
    ("junk", 4001),
    ("carol-token", 4003),
])
async def test_refused_sockets_are_closed_with_a_reason(ws_app, guild, token, code):
    socket = FakeWebSocket(ws_app, token)
    await ws_routes.guild_websocket(socket, GUILD_ID)
    assert socket.accepted and socket.closed[0] == code and socket.closed[1]


async def test_a_bot_that_left_the_server_closes_with_4004(ws_app, guild, monkeypatch, bot):
    monkeypatch.setattr(bot, "get_guild", lambda gid: None)
    socket = FakeWebSocket(ws_app, session.mint_session_token(ALICE, GUILD_ID))
    await ws_routes.guild_websocket(socket, GUILD_ID)
    assert socket.closed[0] == 4004


async def test_a_member_kicked_mid_session_is_closed_on_the_next_ping(ws_app, guild):
    def kick():
        guild.members[:] = [m for m in guild.members if m.id != ALICE]

    socket = FakeWebSocket(ws_app, session.mint_session_token(ALICE, GUILD_ID),
                           messages=[{"type": "PING"}, kick])
    await ws_routes.guild_websocket(socket, GUILD_ID)

    assert socket.sent[0]["type"] == "STATE_UPDATE"
    assert {"type": "PONG"} in socket.sent
    assert socket.closed[0] == 4003
    assert not ws_app.state.ws_manager.has_connections(GUILD_ID)


# Static export cache policy (#99, #140)

@pytest.fixture
async def static_client(tmp_path):
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "404.html").write_text("<html>not found</html>")
    (tmp_path / "_next").mkdir()
    (tmp_path / "_next" / "app.js").write_text("console.log(1)")
    app = FastAPI()
    app.mount("/", FrontendStaticFiles(directory=str(tmp_path), html=True), name="frontend")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_versioned_chunks_are_immutable_and_pages_revalidate(static_client):
    chunk = await static_client.get("/v-202610210000/_next/app.js")
    missing = await static_client.get("/v-202610210000/_next/gone.js")
    page = await static_client.get("/")
    assert chunk.status_code == 200 and "immutable" in chunk.headers["cache-control"]
    assert missing.status_code == 404 and "immutable" not in missing.headers["cache-control"]
    assert page.headers["cache-control"] == "no-cache"
