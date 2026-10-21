import asyncio
import hashlib
import logging
import re
import time
from collections import OrderedDict, deque

import aiohttp
from fastapi import HTTPException

from activity.permissions import check_banned
from activity.session import verify_session_token
from config import DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"
_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Successful lookups are reused for this long; tokens Discord rejected are not sent again for _BAD_TTL.
_USER_CACHE_TTL = 300
_BAD_TTL = 120
_CACHE_SIZE = 1000
# Discord restricts an IP after 10,000 invalid requests in 10 minutes. Unknown tokens are checked at most
# this often, so junk requests can never get the bot's own traffic banned.
_LOOKUP_BUDGET = 200
_BUDGET_WINDOW = 60.0
_MAX_TOKEN_LENGTH = 512

# Our own session tokens: base64url payload, a dot, an HMAC-SHA256 hex signature (activity/session.py)
_SESSION_TOKEN = re.compile(r"[A-Za-z0-9_-]+\.[0-9a-f]{64}")

_users: OrderedDict[str, tuple[dict, float]] = OrderedDict()
_rejected: OrderedDict[str, float] = OrderedDict()
_inflight: dict[str, asyncio.Task] = {}
_lookups: deque[float] = deque()
_discord_paused_until = 0.0


def is_session_token(token: str) -> bool:
    return bool(_SESSION_TOKEN.fullmatch(token or ""))


def _key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _remember(cache: OrderedDict, key: str, value) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > _CACHE_SIZE:
        cache.popitem(last=False)


def _unavailable(detail: str = "Discord is not answering right now. Try again in a moment.") -> HTTPException:
    return HTTPException(status_code=503, detail=detail)


def _spend_lookup() -> None:
    now = time.monotonic()
    while _lookups and now - _lookups[0] > _BUDGET_WINDOW:
        _lookups.popleft()
    if len(_lookups) >= _LOOKUP_BUDGET:
        raise _unavailable("Too many sign-ins at once. Try again in a minute.")
    _lookups.append(now)


async def _fetch_user(session: aiohttp.ClientSession, token: str, key: str) -> dict | None:
    global _discord_paused_until
    try:
        async with session.get(
                f"{DISCORD_API}/users/@me", headers={"Authorization": f"Bearer {token}"}, timeout=_TIMEOUT,
        ) as resp:
            if resp.status == 200:
                user = await resp.json()
                _remember(_users, key, (user, time.monotonic()))
                return user
            if resp.status in (401, 403):
                _remember(_rejected, key, time.monotonic() + _BAD_TTL)
                return None
            if resp.status == 429:
                retry_after = 5.0
                try:
                    retry_after = float((await resp.json()).get("retry_after", retry_after))
                except (aiohttp.ContentTypeError, ValueError, TypeError):
                    pass
                _discord_paused_until = time.monotonic() + retry_after
                logger.warning(f"Discord rate-limited user lookups for {retry_after:.0f}s")
            else:
                logger.warning(f"Discord user lookup failed: HTTP {resp.status}")
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.warning(f"Discord user lookup failed: {e!r}")
    raise _unavailable()


async def get_discord_user(session: aiohttp.ClientSession, access_token: str) -> dict | None:
    """The Discord user an OAuth access token belongs to, or None when Discord rejects it.

    Raises HTTPException(503) when Discord can't be asked right now (outage, rate limit, lookup budget).
    """
    if not access_token or len(access_token) > _MAX_TOKEN_LENGTH or is_session_token(access_token):
        return None
    key = _key(access_token)
    now = time.monotonic()
    cached = _users.get(key)
    if cached and now - cached[1] < _USER_CACHE_TTL:
        return cached[0]
    if _rejected.get(key, 0) > now:
        return None

    task = _inflight.get(key)
    if task is None:
        if now < _discord_paused_until:
            raise _unavailable()
        _spend_lookup()
        task = asyncio.ensure_future(_fetch_user(session, access_token, key))
        _inflight[key] = task
        task.add_done_callback(lambda t: _inflight.pop(key, None))
        # Retrieve the result even if every waiter was cancelled, so no "never retrieved" warning.
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
    return await asyncio.shield(task)


async def exchange_code(session: aiohttp.ClientSession, code: str) -> dict:
    try:
        async with session.post(
                f"{DISCORD_API}/oauth2/token",
                data={
                    "client_id": DISCORD_CLIENT_ID,
                    "client_secret": DISCORD_CLIENT_SECRET,
                    "grant_type": "authorization_code",
                    "code": code,
                },
                timeout=_TIMEOUT,
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                logger.error(f"Token exchange failed: {data}")
            return data if isinstance(data, dict) else {}
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        logger.warning(f"Token exchange failed: {e!r}")
        raise _unavailable() from e


def require_member(bot, guild_id: int, user_id: int):
    """The member, after the ban and membership checks every authenticated request goes through."""
    if check_banned(user_id):
        raise HTTPException(status_code=403, detail="You are banned from using this bot")
    guild = bot.get_guild(guild_id)
    if not guild:
        raise HTTPException(status_code=404, detail="Bot is not in this guild")
    member = guild.get_member(user_id)
    if not member:
        raise HTTPException(status_code=403, detail="You are not a member of this guild")
    return member


async def user_id_from_token(bot, guild_id: int, token: str | None) -> int:
    """The user a session token (scoped to this guild) or a Discord access token belongs to."""
    if not token:
        raise HTTPException(status_code=401, detail="Missing access token")
    if is_session_token(token):
        session = verify_session_token(token)
        if not session or session.get("guild_id") != guild_id:
            raise HTTPException(status_code=401, detail="Your session expired")
        return session["user_id"]
    user = await get_discord_user(bot.http_session, token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid access token")
    return int(user["id"])


async def authenticate_query_or_header(
        token_header: str | None, token_query: str | None, guild_id: int, bot,
) -> int:
    """Authenticate a request that may carry its credentials in the query string.

    Used where the client (``<audio>``, WebSocket) cannot send an ``Authorization`` header. Prefers the
    short-lived session token; a Discord access token still works. Ban and membership are checked either way.
    """
    token = token_query
    if not token and token_header and token_header.startswith("Bearer "):
        token = token_header[7:]
    user_id = await user_id_from_token(bot, guild_id, token)
    require_member(bot, guild_id, user_id)
    return user_id
