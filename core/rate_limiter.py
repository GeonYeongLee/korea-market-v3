"""
Token Bucket Rate Limiter
Ensures KIS Open API rate limits (20 req/s max) are never breached.
"""
from __future__ import annotations

import time
import asyncio
import threading
from typing import Optional


class RateLimiter:
    """
    Thread-safe and Async-safe Token Bucket Rate Limiter.
    Default rate is 18.0 requests/second to provide a safety margin under KIS 20 req/s.
    """

    def __init__(self, rate: float = 18.0, capacity: Optional[float] = None):
        self.rate = float(rate)
        self.capacity = float(capacity or rate)
        self.tokens = self.capacity
        self.last_update = time.monotonic()
        self._lock = threading.Lock()
        self._async_lock: Optional[asyncio.Lock] = None

    def _add_tokens(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_update
        self.last_update = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

    def acquire_sync(self, tokens: float = 1.0) -> None:
        """Synchronously acquire token(s), blocking if necessary."""
        while True:
            with self._lock:
                self._add_tokens()
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return
                # Calculate required wait time
                needed = tokens - self.tokens
                wait_time = needed / self.rate

            time.sleep(max(0.005, wait_time))

    async def acquire_async(self, tokens: float = 1.0) -> None:
        """Asynchronously acquire token(s), awaiting if necessary."""
        if self._async_lock is None:
            with self._lock:
                if self._async_lock is None:
                    self._async_lock = asyncio.Lock()

        while True:
            async with self._async_lock:
                with self._lock:
                    self._add_tokens()
                    if self.tokens >= tokens:
                        self.tokens -= tokens
                        return
                    needed = tokens - self.tokens
                    wait_time = needed / self.rate

            await asyncio.sleep(max(0.005, wait_time))


def get_rate_limiter(env: str = "real") -> RateLimiter:
    """
    Returns appropriate rate limiter based on environment.
    KIS Live (real): 20 TPS max -> 18.0 TPS safe limit
    KIS Mock (vts): 2 TPS max -> 1.8 TPS safe limit (prevents 429 EGW00201)
    """
    if env.lower() == "vts":
        return RateLimiter(rate=1.8, capacity=2.0)
    return RateLimiter(rate=18.0, capacity=20.0)
