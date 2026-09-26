"""Fire-and-forget tasks that are actually kept until they finish.

asyncio holds only a WEAK reference to a running task. A bare
`asyncio.create_task(...)` whose result nobody stores can be garbage
collected mid-flight, and the work simply stops -- no exception, no log
line. For a critical-incident email, or a platform response carrying a
page of search results, that is exactly the silent failure this codebase
exists to rule out.

`spawn` keeps a strong reference in a module-level set and drops it when
the task completes, so the set never grows beyond what is in flight.
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine

_live: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """Schedule `coro` and hold it until it is done. Returns the task."""
    task = asyncio.create_task(coro)
    _live.add(task)
    task.add_done_callback(_live.discard)
    return task


def in_flight() -> int:
    """How many spawned tasks are still running. For tests and diagnostics."""
    return len(_live)
