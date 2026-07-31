import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from bot import MusicBot
from cogs.music_commands import MusicCommands
from cogs.playlist_commands import PlaylistCommands
from activity.ws_manager import ConnectionManager

load_dotenv()
logger = logging.getLogger(__name__)


def _windows_exception_handler(loop, context):
    exception = context.get("exception")
    if isinstance(exception, ConnectionResetError):
        return
    loop.default_exception_handler(context)


@asynccontextmanager
async def lifespan(app: FastAPI):
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        logger.error("BOT_TOKEN environment variable not found")
        raise RuntimeError("BOT_TOKEN is required")

    logger.info("Starting Discord Music Bot + Activity server...")

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
    await bot.login(bot_token)
    bot_task = asyncio.create_task(bot.connect(), name="discord-bot-connect")

    def _log_bot_task_exception(task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Discord bot connection task failed", exc_info=exc)

    bot_task.add_done_callback(_log_bot_task_exception)

    # Wait for bot to be fully connected before installing hooks.
    # Race wait_until_ready against bot_task so a failed connect() can't hang
    # uvicorn forever, and bound it with a timeout.
    ready_task = asyncio.create_task(bot.wait_until_ready(), name="bot-wait-until-ready")
    done, _pending = await asyncio.wait(
        {ready_task, bot_task},
        return_when=asyncio.FIRST_COMPLETED,
        timeout=60,
    )

    if bot_task in done:
        # connect() returned/raised before the bot became ready
        ready_task.cancel()
        exc = bot_task.exception() if not bot_task.cancelled() else None
        if exc is not None:
            logger.error("Bot failed to connect during startup", exc_info=exc)
            raise RuntimeError(f"Bot failed to connect: {exc}") from exc
        logger.error("Discord bot connection ended before becoming ready")
        raise RuntimeError("Bot connection ended before becoming ready")

    if ready_task not in done:
        # Neither ready nor failed within the timeout
        ready_task.cancel()
        logger.error("Timed out waiting for the Discord bot to become ready (60s)")
        raise RuntimeError("Timed out waiting for the Discord bot to become ready")

    # Install broadcast hooks so slash commands notify Activity clients
    from activity.cog_hooks import install_broadcast_hooks
    install_broadcast_hooks(bot, ws_manager)

    # Start position broadcaster for real-time progress updates
    from activity.position_broadcaster import start_position_broadcaster
    position_task = start_position_broadcaster(bot, ws_manager)

    # Stop Activity-driven playback when last user closes the Activity
    async def on_last_disconnect(guild_id: int, last_user_ids: set[int] | None = None):
        await asyncio.sleep(20)

        # If someone reconnected during the delay, abort cleanup
        if ws_manager.has_connections(guild_id):
            return

        guild_data = bot.get_guild_data(guild_id)
        vc = guild_data.get("voice_client")
        if vc and vc.is_connected():
            return  # Bot is in voice — playback continues through VC

        if guild_data.get("current"):
            # Record listening stats before clearing (pass saved user IDs
            # since WS connections are already gone at this point)
            from activity.helpers import clear_activity_playback, record_activity_listening
            await record_activity_listening(bot, ws_manager, guild_id, user_ids=last_user_ids)

            clear_activity_playback(guild_data, cancel_prefetch=True)
            await bot.save_guild_queue(guild_id)
            logger.info(f"Activity closed for guild {guild_id}, cleared playback state")

    ws_manager.set_on_last_disconnect(on_last_disconnect)

    app.state.bot = bot
    app.state.ws_manager = ws_manager

    logger.info("Activity server ready")

    yield

    # Shutdown
    logger.info("Shutting down...")
    position_task.cancel()
    try:
        await position_task
    except asyncio.CancelledError:
        pass

    # Close lazily-created aiohttp sessions and cancel tracked background tasks.
    # These helpers are defined by other modules — call them defensively.
    try:
        from activity.routes import stream_routes
        if hasattr(stream_routes, "close_proxy_session"):
            await stream_routes.close_proxy_session()
    except Exception as e:
        logger.debug(f"Error closing stream proxy session: {e}")

    try:
        from activity.routes import image_proxy
        if hasattr(image_proxy, "close_session"):
            await image_proxy.close_session()
    except Exception as e:
        logger.debug(f"Error closing image proxy session: {e}")

    try:
        from activity.tasks import cancel_all
        await cancel_all()
    except Exception as e:
        logger.debug(f"Error cancelling background tasks: {e}")

    await bot.close()
    bot_task.cancel()
    try:
        await bot_task
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
        # caches. Strip it to resolve the real file.
        versioned = path.startswith("v-") and "/" in path
        if versioned:
            path = path.split("/", 1)[1]
        response = await super().get_response(path, scope)
        if versioned:
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
