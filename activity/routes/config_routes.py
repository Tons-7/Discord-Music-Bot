from fastapi import APIRouter

from config import DISCORD_CLIENT_ID

router = APIRouter(prefix="/api", tags=["config"])


@router.get("/config")
async def get_config():
    return {"client_id": DISCORD_CLIENT_ID}
