import asyncio
import logging

logger = logging.getLogger(__name__)

# Strong references to fire-and-forget background tasks.
# asyncio only keeps weak references to tasks, so without this set a task may be
# garbage-collected mid-flight. We discard each task once it finishes.
_BG: set = set()


def spawn(coro) -> asyncio.Task:
    """Schedule a fire-and-forget coroutine, keeping a strong reference until done."""
    task = asyncio.ensure_future(coro)
    _BG.add(task)
    task.add_done_callback(_BG.discard)
    return task


async def cancel_all() -> None:
    """Cancel all tracked background tasks (used on shutdown)."""
    tasks = list(_BG)
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _BG.clear()
