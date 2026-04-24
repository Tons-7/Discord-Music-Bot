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
    bot_task.add_done_callback(lambda t: t.exception() if not t.cancelled() and t.exception() else None)

    # Wait for bot to be fully connected before installing hooks
    await bot.wait_until_ready()

    # Install broadcast hooks so slash commands notify Activity clients
    from activity.cog_hooks import install_broadcast_hooks
    install_broadcast_hooks(bot, ws_manager)

    # Start position broadcaster for real-time progress updates
    from activity.position_broadcaster import start_position_broadcaster
    position_task = start_position_broadcaster(bot, ws_manager)

    # Stop Activity-driven playback when last user closes the Activity
    async def on_last_disconnect(guild_id: int, last_user_ids: set[int] | None = None):
        # Small delay to handle quick reconnects (user refreshing the Activity)
        await asyncio.sleep(2)

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
            from activity.helpers import record_activity_listening
            await record_activity_listening(bot, ws_manager, guild_id, user_ids=last_user_ids)

            guild_data["current"] = None
            guild_data["start_time"] = None
            guild_data["seek_offset"] = 0
            guild_data["pause_position"] = None
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

# Serve Next.js static export if the build exists
frontend_dir = Path(__file__).parent.parent / "activity-frontend" / "out"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
