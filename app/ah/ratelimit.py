"""Serialising rate limiter: at most one in-flight AH request, spaced >= min_interval.

Holding the lock for the whole request (not just the wait) is deliberate: it makes
parallel fan-out to AH impossible from a single client instance (CLAUDE.md rule 3).
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from types import TracebackType


class RateLimiter:
    def __init__(
        self,
        min_interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last_finished: float | None = None

    async def __aenter__(self) -> None:
        await self._lock.acquire()
        if self._last_finished is not None:
            wait = self._last_finished + self._min_interval - self._clock()
            if wait > 0:
                await self._sleep(wait)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._last_finished = self._clock()
        self._lock.release()

    async def pause(self, seconds: float) -> None:
        """Back-off wait, used while the lock is held."""
        await self._sleep(seconds)
