# In-memory cache so we don't read the file on every single interaction
_banned_cache: set | None = None


def _load_cache():
    """Load banned user IDs from file into memory."""
    global _banned_cache
    try:
        with open('banned_users.txt', 'r') as f:
            _banned_cache = {int(line.strip()) for line in f if line.strip()}
    except FileNotFoundError:
        _banned_cache = set()
    except ValueError:
        _banned_cache = set()


def is_banned(user_id: int) -> bool:
    if _banned_cache is None:
        _load_cache()
    return user_id in _banned_cache


def ban_user_id(user_id: int) -> bool:
    global _banned_cache
    if _banned_cache is None:
        _load_cache()

    if user_id in _banned_cache:
        return False

    _banned_cache.add(user_id)
    with open('banned_users.txt', 'a') as f:
        f.write(f"{user_id}\n")
    return True


def unban_user_id(user_id: int) -> bool:
    global _banned_cache
    if _banned_cache is None:
        _load_cache()

    if user_id not in _banned_cache:
        return False

    _banned_cache.discard(user_id)

    try:
        with open('banned_users.txt', 'r') as f:
            lines = f.readlines()
    except FileNotFoundError:
        return False

    with open('banned_users.txt', 'w') as f:
        for line in lines:
            if line.strip() != str(user_id):
                f.write(line)

    return True
