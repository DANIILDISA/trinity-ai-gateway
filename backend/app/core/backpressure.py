"""Preemptive per-endpoint concurrency backpressure (tr-3hs, audit §H2).

A request that can't get a slot is rejected NOW with a 503 + Retry-After instead of
queueing on the DB pool_timeout (10s) and then the 30s request timeout. Mirrors the
chat SSE semaphore's fast-fail, but raises OverloadedError so it goes through the
app's uniform TrinityError → 503+Retry-After handler (main.py).

Per-worker in-process semaphores (like the chat one): zero hot-path latency, no Redis
hop. The global cap is per-worker-limit × worker count. A Redis-global limiter would
be more precise but is YAGNI for shedding load.
"""
import asyncio
from collections.abc import AsyncGenerator

from app.core.exceptions import OverloadedError

# name → semaphore. First get_limiter(name, size) wins; the event loop is single-
# threaded so this lazy init has no race.
_LIMITERS: dict[str, asyncio.Semaphore] = {}


def get_limiter(name: str, size: int) -> asyncio.Semaphore:
    sem = _LIMITERS.get(name)
    if sem is None:
        sem = _LIMITERS[name] = asyncio.Semaphore(size)
    return sem


def backpressure(name: str, size: int, *, retry_after: int = 2):
    """Build a FastAPI dependency that sheds (503+Retry-After) when `name` is at
    capacity. Call ONCE per route group at import time so the semaphore is shared."""
    sem = get_limiter(name, size)

    async def _dep() -> AsyncGenerator[None, None]:
        # .locked() (value == 0) is a sync check; in single-threaded asyncio nothing
        # runs between it and the acquire below, so check-then-acquire is atomic
        # (same guarantee the chat semaphore relies on).
        if sem.locked():
            raise OverloadedError(retry_after=retry_after, code=f"overloaded_{name}")
        await sem.acquire()
        try:
            yield
        finally:
            sem.release()

    return _dep
