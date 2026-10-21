import logging

import uvicorn
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    from config import ACTIVITY_PORT, GRACEFUL_SHUTDOWN_TIMEOUT, YTDLP_LOG_LEVEL

    try:
        logging.getLogger("yt_dlp").setLevel(YTDLP_LOG_LEVEL.upper())
    except ValueError:
        logger.warning(f"Unknown YTDLP_LOG_LEVEL {YTDLP_LOG_LEVEL!r}, using WARNING")
        logging.getLogger("yt_dlp").setLevel(logging.WARNING)

    uvicorn.run(
        "activity.app:app",
        host="0.0.0.0",
        port=ACTIVITY_PORT,
        log_level="info",
        access_log=False,
        # uvicorn's records go through the root handler above, with timestamps
        log_config=None,
        # Cut open audio streams short on shutdown so the bot's own shutdown (final queue save) still runs.
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_TIMEOUT or None,
    )
