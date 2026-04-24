import logging
import time

import httpx

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
