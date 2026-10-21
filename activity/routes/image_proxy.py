import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urljoin, urlparse

import aiohttp
from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["proxy"])

# Concurrency limit to avoid overwhelming the network
_semaphore = asyncio.Semaphore(6)  # max 6 concurrent image fetches
_TIMEOUT = aiohttp.ClientTimeout(total=15)
_MAX_BYTES = 5 * 1024 * 1024
_MAX_REDIRECTS = 3
# Raster formats only: an SVG or HTML file uploaded to an allowed CDN must not render on our origin.
_IMAGE_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif", "image/avif"}
_SAFE_HEADERS = {
    "Cache-Control": "public, max-age=86400",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
}

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
    # SoundCloud artwork
    "sndcdn.com",
    # Discord avatars / CDN
    "discordapp.com",
    "discord.com",
)


def _host_allowed(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == suffix or host.endswith("." + suffix) for suffix in _ALLOWED_HOST_SUFFIXES)


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


async def _allowed(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname
    if parsed.scheme not in ("http", "https") or not host or not _host_allowed(host):
        return False
    # Defense in depth: reject hosts resolving to private/loopback/etc. IPs.
    return not await asyncio.get_running_loop().run_in_executor(None, _resolves_to_private, host)


async def _read_capped(resp: aiohttp.ClientResponse) -> bytes | None:
    chunks, size = [], 0
    async for chunk in resp.content.iter_chunked(64 * 1024):
        size += len(chunk)
        if size > _MAX_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/img")
async def proxy_image(request: Request, url: str = Query(..., max_length=2048)):
    # SSRF guard: this endpoint is internet-exposed and fetches a client-supplied
    # URL, so restrict the scheme and host to known image CDNs. <img> tags cannot
    # send auth headers, so the host allowlist (not a token) is the control.
    # Redirects are followed by hand so every hop passes the same checks.
    session = request.app.state.bot.http_session
    async with _semaphore:
        try:
            for _ in range(_MAX_REDIRECTS + 1):
                if not await _allowed(url):
                    return Response(status_code=400)
                async with session.get(url, allow_redirects=False, timeout=_TIMEOUT) as resp:
                    if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                        url = urljoin(url, resp.headers["Location"])
                        continue
                    if resp.status != 200:
                        return Response(status_code=resp.status if 400 <= resp.status < 600 else 502)

                    content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
                    if content_type not in _IMAGE_TYPES:
                        return Response(status_code=415)
                    if int(resp.headers.get("Content-Length") or 0) > _MAX_BYTES:
                        return Response(status_code=413)
                    body = await _read_capped(resp)
                    if body is None:
                        return Response(status_code=413)
                    return Response(content=body, media_type=content_type, headers=_SAFE_HEADERS)
            return Response(status_code=508)
        except TimeoutError:
            return Response(status_code=504)
        except (aiohttp.ClientError, ValueError) as e:
            logger.debug(f"Image proxy error: {e}")
            return Response(status_code=502)
