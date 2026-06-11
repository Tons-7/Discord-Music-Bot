import logging
import time

import httpx
from fastapi import HTTPException

from activity.session import verify_session_token
from config import DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"

# Cache user info per access_token to avoid hammering Discord API
_user_cache: dict[str, tuple[dict, float]] = {}
_USER_CACHE_TTL = 300  # 5 minutes


async def exchange_code(code: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        data = resp.json()
        if resp.status_code != 200:
            logger.error(f"Token exchange failed: {data}")
        return data


async def get_discord_user(access_token: str) -> dict | None:
    now = time.time()

    cached = _user_cache.get(access_token)
    if cached and now - cached[1] < _USER_CACHE_TTL:
        return cached[0]

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code != 200:
            logger.warning(f"Failed to get user: {resp.status_code}")
            return None

        user = resp.json()
        _user_cache[access_token] = (user, now)

        # Evict stale cache entries
        if len(_user_cache) > 200:
            cutoff = now - _USER_CACHE_TTL
            stale = [k for k, (_, t) in _user_cache.items() if t < cutoff]
            for k in stale:
                del _user_cache[k]

        return user


async def authenticate_query_or_header(
    token_header: str | None, token_query: str | None, guild_id: int, bot
) -> int:
    """Authenticate a request that may carry credentials in a query string.

    Used by stream/ws/img endpoints where the client (``<audio>``, WebSocket,
    ``<img>``) cannot send an ``Authorization`` header. Prefers a short-lived
    HMAC-signed session token (no Discord round-trip), falling back to a raw
    Discord access token. Returns the verified user id. Raises HTTPException on
    failure.
    """
    # Fast path: signed session token scoped to this guild (no Discord call).
    if token_query:
        session = verify_session_token(token_query)
        if session and session.get("guild_id") == guild_id:
            return session["user_id"]

    # Lazy import to avoid an import cycle (dependencies -> auth -> dependencies).
    from activity.dependencies import require_guild_member
    from activity.permissions import check_banned

    raw_token = None
    if token_header and token_header.startswith("Bearer "):
        raw_token = token_header[7:]
    elif token_query:
        raw_token = token_query

    if not raw_token:
        raise HTTPException(status_code=401, detail="Missing access token")

    user = await get_discord_user(raw_token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid access token")

    user_id = int(user["id"])
    if check_banned(user_id):
        raise HTTPException(status_code=403, detail="You are banned from using this bot")

    require_guild_member(bot, guild_id, user_id)
    return user_id
