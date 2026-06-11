import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import aiohttp
from fastapi import APIRouter, Query
from fastapi.responses import Response

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["proxy"])

# Shared session + concurrency limit to avoid overwhelming the network
_session: aiohttp.ClientSession | None = None
_semaphore = asyncio.Semaphore(6)  # max 6 concurrent image fetches

# Host allowlist for SSRF protection. This proxy is internet-exposed (via
# cloudflared) and fetches client-supplied URLs, so only the known image CDNs
# the app actually uses are permitted. Hosts are matched by suffix.
_ALLOWED_HOST_SUFFIXES = (
    # YouTube thumbnails / channel art
    "ytimg.com",
    "ggpht.com",
    "googleusercontent.com",
    # Spotify cover art
    "scdn.co",
    "i.scdn.co",
    # SoundCloud artwork
    "sndcdn.com",
    # Discord avatars / CDN
    "discordapp.com",
    "discord.com",
    "cdn.discordapp.com",
)


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            connector=aiohttp.TCPConnector(limit=20, keepalive_timeout=60),
        )
    return _session


async def close_session():
    """Close the module-level aiohttp session (used by app.py shutdown)."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


def _host_allowed(host: str) -> bool:
    host = host.lower()
    return any(
        host == suffix or host.endswith("." + suffix)
        for suffix in _ALLOWED_HOST_SUFFIXES
    )


def _resolves_to_private(host: str) -> bool:
    """Resolve host and report whether any IP is non-public.

    getaddrinfo blocks, so call this via run_in_executor. Fails closed
    (returns True) on resolution failure.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return True
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return True
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
    return False


@router.get("/img")
async def proxy_image(url: str = Query(...)):
    # SSRF guard: this endpoint is internet-exposed and fetches a client-supplied
    # URL, so restrict the scheme and host to known image CDNs. <img> tags cannot
    # send auth headers, so the host allowlist (not a token) is the control.
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return Response(status_code=400)
    host = parsed.hostname
    if not host or not _host_allowed(host):
        return Response(status_code=400)

    # Defense in depth: reject hosts resolving to private/loopback/etc. IPs.
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(None, _resolves_to_private, host):
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
