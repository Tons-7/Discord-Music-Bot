import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time

import config

logger = logging.getLogger(__name__)

SESSION_TTL = 6 * 3600  # 6 hours

# Per-process fallback secret, generated lazily so we only warn once.
_FALLBACK_SECRET: str | None = None


def _get_secret() -> str:
    """Resolve the signing secret.

    Precedence: ACTIVITY_SECRET env var -> DISCORD_CLIENT_SECRET (config) ->
    a per-process random secret (tokens will not survive a restart).
    """
    secret = os.getenv("ACTIVITY_SECRET")
    if secret:
        return secret

    if config.DISCORD_CLIENT_SECRET:
        return config.DISCORD_CLIENT_SECRET

    global _FALLBACK_SECRET
    if _FALLBACK_SECRET is None:
        _FALLBACK_SECRET = secrets.token_hex(32)
        logger.warning(
            "No ACTIVITY_SECRET or DISCORD_CLIENT_SECRET set; using a per-process "
            "random secret. Session tokens will not survive a restart."
        )
    return _FALLBACK_SECRET


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _sign(payload_b64: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256
    ).hexdigest()


def mint_session_token(user_id: int, guild_id: int, ttl: int = SESSION_TTL) -> str:
    """Create an HMAC-signed session token for query-string contexts."""
    payload = {
        "uid": int(user_id),
        "gid": int(guild_id),
        "exp": int(time.time()) + int(ttl),
    }
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _sign(payload_b64, _get_secret())
    return f"{payload_b64}.{signature}"


def verify_session_token(token: str) -> dict | None:
    """Verify a session token, returning {"user_id", "guild_id"} or None.

    Returns None for malformed, expired, or bad-signature tokens.
    """
    try:
        payload_b64, signature = token.split(".", 1)

        expected = _sign(payload_b64, _get_secret())
        if not hmac.compare_digest(signature, expected):
            return None

        payload = json.loads(_b64url_decode(payload_b64))

        if int(payload["exp"]) < int(time.time()):
            return None

        return {"user_id": int(payload["uid"]), "guild_id": int(payload["gid"])}
    except Exception:
        return None
