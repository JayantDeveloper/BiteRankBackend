"""Simple async rate limiter."""
from __future__ import annotations

import asyncio
import time
from collections import deque
import logging
from typing import Deque

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Async-safe sliding-window rate limiter.

    Key fixes vs the old version:
    - Never sleeps while holding the lock (prevents serialized "lock convoy" delays).
    - Uses time.monotonic() (immune to system clock changes).
    - Loops until a slot is available.
    """

    def __init__(self, max_requests: int = 50, time_window: float = 60.0):
        """
        Args:
            max_requests: Maximum requests allowed in time_window.
            time_window: Time window in seconds (default 60 = 1 minute).
        """
        if max_requests <= 0:
            raise ValueError("max_requests must be > 0")
        if time_window <= 0:
            raise ValueError("time_window must be > 0")

        self.max_requests = int(max_requests)
        self.time_window = float(time_window)

        self._requests: Deque[float] = deque()
        self._lock = asyncio.Lock()

    def _cleanup(self, now: float) -> None:
        """Drop timestamps outside the rolling window."""
        cutoff = now - self.time_window
        while self._requests and self._requests[0] <= cutoff:
            self._requests.popleft()

    async def acquire(self) -> None:
        """
        Wait until a request slot is available, then reserve it.

        This is a reservation-based limiter: once acquire() returns,
        you're counted against the window.
        """
        while True:
            sleep_time = 0.0

            async with self._lock:
                now = time.monotonic()
                self._cleanup(now)

                if len(self._requests) < self.max_requests:
                    self._requests.append(now)
                    return

                oldest = self._requests[0]
                sleep_time = (oldest + self.time_window) - now

                if sleep_time < 0:
                    sleep_time = 0.0
                sleep_time += 0.05

                logger.warning(
                    "⏸️ Rate limit reached (%d req / %.0fs). Waiting %.2fs...",
                    self.max_requests,
                    self.time_window,
                    sleep_time,
                )

            await asyncio.sleep(sleep_time)

