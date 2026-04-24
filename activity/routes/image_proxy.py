import asyncio
import logging

import aiohttp
from fastapi import APIRouter, Query
from fastapi.responses import Response

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["proxy"])

# Shared session + concurrency limit to avoid overwhelming the network
_session: aiohttp.ClientSession | None = None
_semaphore = asyncio.Semaphore(6)  # max 6 concurrent image fetches


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            connector=aiohttp.TCPConnector(limit=20, keepalive_timeout=60),
        )
    return _session


@router.get("/img")
async def proxy_image(url: str = Query(...)):
    if not url.startswith("http"):
        return Response(status_code=400)

    async with _semaphore:
        try:
            session = _get_session()
            async with session.get(url) as resp:
                if resp.status != 200:
                    return Response(status_code=resp.status)

                content_type = resp.headers.get("Content-Type", "image/jpeg")
                body = await resp.read()

                return Response(
                    content=body,
                    media_type=content_type,
                    headers={"Cache-Control": "public, max-age=86400"},
                )
        except asyncio.TimeoutError:
            return Response(status_code=504)
        except Exception as e:
            logger.debug(f"Image proxy error: {e}")
            return Response(status_code=502)
