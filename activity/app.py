import asyncio
import logging
import os
import random
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import discord
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders

from activity.ws_manager import ConnectionManager
from bot import MusicBot
from cogs.music_commands import MusicCommands
from cogs.playlist_commands import PlaylistCommands

load_dotenv()
logger = logging.getLogger(__name__)

# The longest wait between attempts to log in or reconnect to Discord
MAX_RETRY_DELAY = 300


def _windows_exception_handler(loop, context):
    exception = context.get("exception")
    if isinstance(exception, ConnectionResetError):
        return
    loop.default_exception_handler(context)


async def _login(bot: MusicBot, token: str) -> None:
    """Log in, waiting out network and Discord outages; a rejected token is the only error that stops it."""
    delay = 5
    while True:
        try:
            await bot.login(token)
            return
        except discord.LoginFailure:
            raise
        except (OSError, aiohttp.ClientError, TimeoutError, discord.HTTPException) as e:
            logger.warning(f"Discord login failed ({e!r}); retrying in {delay}s")
            await asyncio.sleep(delay + random.uniform(0, delay / 2))
            delay = min(delay * 2, MAX_RETRY_DELAY)


async def _stay_connected(bot: MusicBot) -> None:
    """bot.connect(), restarted after failures discord.py doesn't retry itself (it retries network errors)."""
    delay = 5
    while not bot.is_closed():
        try:
            await bot.connect()
        except discord.PrivilegedIntentsRequired:
            raise
        except Exception:
            # discord.py closes the client on a bad token or a close code it can't resume from; a restart is needed.
            if bot.is_closed():
                raise
            logger.exception(f"Discord connection failed; reconnecting in {delay}s")
            await asyncio.sleep(delay + random.uniform(0, delay / 2))
            delay = min(delay * 2, MAX_RETRY_DELAY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        logger.error("BOT_TOKEN environment variable not found")
        raise RuntimeError("BOT_TOKEN is required")

    logger.info("Starting Discord Music Bot + Activity server...")
    app.state.shutting_down = False

    bot = MusicBot()

    if sys.platform == "win32":
        asyncio.get_running_loop().set_exception_handler(_windows_exception_handler)

    ws_manager = ConnectionManager()
    bot.ws_manager = ws_manager

    await bot.add_cog(MusicCommands(bot))
    await bot.add_cog(PlaylistCommands(bot))
    logger.info("Commands loaded successfully")

    # Initialize bot internals (same as `async with bot:` does),
    # then login + connect in background
    await bot._async_setup_hook()
    await _login(bot, bot_token)
    bot_task = asyncio.create_task(_stay_connected(bot), name="discord-bot-connect")

    def _on_bot_task_done(task: asyncio.Task):
        if task.cancelled() or app.state.shutting_down:
            return
        # The bot is offline for good: exit, so the supervisor (Wispbyte, Docker) restarts the process.
        logger.critical("Discord connection ended; shutting down for a restart", exc_info=task.exception())
        signal.raise_signal(signal.SIGTERM)

    bot_task.add_done_callback(_on_bot_task_done)

    # Wait for the bot to be ready before installing hooks; uvicorn serves nothing until then.
    ready_task = asyncio.create_task(bot.wait_until_ready(), name="bot-wait-until-ready")
    while True:
        done, _pending = await asyncio.wait({ready_task, bot_task}, return_when=asyncio.FIRST_COMPLETED, timeout=60)
        if ready_task in done:
            break
        if bot_task in done:
            ready_task.cancel()
            exc = bot_task.exception() if not bot_task.cancelled() else None
            raise RuntimeError(f"Bot failed to connect: {exc}") from exc
        logger.warning("Still waiting for Discord to finish connecting...")

    # Install broadcast hooks so slash commands notify Activity clients
    from activity.cog_hooks import install_broadcast_hooks
    install_broadcast_hooks(bot, ws_manager)

    # Start position broadcaster for real-time progress updates
    from activity.position_broadcaster import start_position_broadcaster
    position_task = start_position_broadcaster(bot, ws_manager)

    # Stop Activity-driven playback when the last viewer closes the Activity (after a grace period)
    from activity.helpers import end_activity_session

    async def on_last_disconnect(guild_id: int, last_user_ids: set[int] | None = None):
        await end_activity_session(bot, ws_manager, guild_id, last_user_ids)

    ws_manager.set_on_last_disconnect(on_last_disconnect)

    app.state.bot = bot
    app.state.ws_manager = ws_manager

    logger.info("Activity server ready")

    yield

    # Shutdown
    logger.info("Shutting down...")
    app.state.shutting_down = True
    position_task.cancel()
    try:
        await position_task
    except asyncio.CancelledError:
        pass

    try:
        from activity.tasks import cancel_all
        await cancel_all()
    except Exception as e:
        logger.debug(f"Error cancelling background tasks: {e}")

    await bot.close()
    bot_task.cancel()
    try:
        await bot_task
    except (asyncio.CancelledError, Exception):
        pass


# The API is used by the bundled frontend only, so it publishes no OpenAPI docs and needs no CORS.
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

# Report-only first: the Activity runs inside Discord's iframe, and an enforced policy that misses a source
# would blank the page. The theme script is inline, hence 'unsafe-inline'.
_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self' wss: https:; "
    "frame-ancestors https://discord.com https://*.discord.com https://*.discordsays.com"
)


class SecurityHeaders:
    """Adds the headers to every HTTP response (plain ASGI, so audio streams pass straight through)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("Content-Security-Policy-Report-Only", _CSP)
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeaders)


# Mount API routes
from activity.routes import api_router

app.include_router(api_router)


class FrontendStaticFiles(StaticFiles):
    """Static export with explicit cache policy: versioned chunks are
    immutable; everything else (index.html) must revalidate, otherwise the
    Discord webview/CDNs cache old builds and UI updates never land."""

    async def get_response(self, path, scope):
        # Per-build assetPrefix (/v-<stamp>/_next/...) — chunk filenames are not
        # content-hashed across builds, so the unique prefix is what busts CDN
        # caches. Strip it to resolve the real file (Starlette gives Windows paths with "\").
        first, sep, rest = path.replace("\\", "/").partition("/")
        versioned = first.startswith("v-") and bool(sep)
        if versioned:
            path = rest
        response = await super().get_response(path, scope)
        if versioned and response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


# Serve Next.js static export if the build exists
frontend_dir = Path(__file__).parent.parent / "activity-frontend" / "out"
if frontend_dir.exists():
    app.mount("/", FrontendStaticFiles(directory=str(frontend_dir), html=True), name="frontend")
else:
    logger.warning(
        f"frontend build not found at {frontend_dir}; "
        "run 'cd activity-frontend && npm run build' — API up but SPA unavailable"
    )
