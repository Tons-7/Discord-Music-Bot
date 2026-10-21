import logging
import os

logger = logging.getLogger(__name__)

BAN_FILE = "banned_users.txt"

# In-memory cache so we don't read the file on every single interaction
_banned_cache: set[int] | None = None


def _load_cache() -> set[int]:
    """Load banned user IDs from the file; a malformed line is skipped, never the whole list."""
    global _banned_cache
    banned: set[int] = set()
    try:
        with open(BAN_FILE, encoding="utf-8") as f:
            for number, line in enumerate(f, 1):
                text = line.strip()
                if not text or text.startswith("#"):
                    continue
                try:
                    banned.add(int(text))
                except ValueError:
                    logger.warning(f"{BAN_FILE}:{number}: not a user ID, skipped: {text[:40]!r}")
    except FileNotFoundError:
        pass
    _banned_cache = banned
    return banned


def _cache() -> set[int]:
    return _banned_cache if _banned_cache is not None else _load_cache()


def _write(ids: set[int]) -> None:
    temp = f"{BAN_FILE}.tmp"
    with open(temp, "w", encoding="utf-8") as f:
        f.writelines(f"{user_id}\n" for user_id in sorted(ids))
    os.replace(temp, BAN_FILE)


def is_banned(user_id: int) -> bool:
    return user_id in _cache()


def banned_ids() -> list[int]:
    return sorted(_cache())


def ban_user_id(user_id: int) -> bool:
    banned = _cache()
    if user_id in banned:
        return False
    banned.add(user_id)
    _write(banned)
    return True


def unban_user_id(user_id: int) -> bool:
    banned = _cache()
    if user_id not in banned:
        return False
    banned.discard(user_id)
    _write(banned)
    return True
