import logging

import uvicorn
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)

if __name__ == "__main__":
    from config import ACTIVITY_PORT

    uvicorn.run(
        "activity.app:app",
        host="0.0.0.0",
        port=ACTIVITY_PORT,
        log_level="info",
    )