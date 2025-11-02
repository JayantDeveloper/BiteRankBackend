import asyncio
import time
from collections import deque
import logging

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Rate limiter to prevent exceeding API limits
    Gemini free tier: ~60 requests per minute
    """

    def __init__(self, max_requests: int = 50, time_window: int = 60):
        """
        Args:
            max_requests: Maximum requests allowed in time_window
            time_window: Time window in seconds (default 60 = 1 minute)
        """
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = deque()
        self.lock = asyncio.Lock()

    async def acquire(self):
        """Wait if necessary to respect rate limits"""
        async with self.lock:
            now = time.time()

            # Remove requests outside the time window
            while self.requests and self.requests[0] < now - self.time_window:
                self.requests.popleft()

            # If we've hit the limit, wait
            if len(self.requests) >= self.max_requests:
                sleep_time = self.requests[0] + self.time_window - now + 0.1
                if sleep_time > 0:
                    logger.warning(
                        f"⏸️  Rate limit reached ({self.max_requests} req/{self.time_window}s). "
                        f"Waiting {sleep_time:.1f}s..."
                    )
                    await asyncio.sleep(sleep_time)

                    # Clean up old requests after waiting
                    now = time.time()
                    while self.requests and self.requests[0] < now - self.time_window:
                        self.requests.popleft()

            # Record this request
            self.requests.append(now)


# Global rate limiter instance (50 requests per minute to be safe)
gemini_rate_limiter = RateLimiter(max_requests=50, time_window=60)
