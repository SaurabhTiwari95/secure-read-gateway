"""Nonce store and rate limiter.

Redis versions are used when REDIS_URL is set, so any number of gateway instances share one view.
In-memory versions are for tests and single-instance local runs only.
"""

from __future__ import annotations

import time
from typing import Protocol


class NonceStore(Protocol):
    async def claim(self, key: str, ttl_seconds: int) -> bool:
        """True the first time a key is seen within its TTL, False on any repeat."""

    async def ping(self) -> bool: ...


class RateLimiter(Protocol):
    async def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        """Count one request; True if still within the limit."""

    async def ping(self) -> bool: ...


class MemoryNonceStore:
    def __init__(self, clock=time.monotonic):
        self._seen: dict[str, float] = {}
        self._clock = clock

    async def claim(self, key: str, ttl_seconds: int) -> bool:
        now = self._clock()
        if len(self._seen) > 10_000:
            self._seen = {k: exp for k, exp in self._seen.items() if exp > now}
        expires = self._seen.get(key)
        if expires is not None and expires > now:
            return False
        self._seen[key] = now + ttl_seconds
        return True

    async def ping(self) -> bool:
        return True


class MemoryRateLimiter:
    def __init__(self, clock=time.monotonic):
        self._windows: dict[str, tuple[int, int]] = {}
        self._clock = clock

    async def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        window = int(self._clock() // window_seconds)
        start, count = self._windows.get(key, (window, 0))
        if start != window:
            start, count = window, 0
        count += 1
        self._windows[key] = (start, count)
        return count <= limit

    async def ping(self) -> bool:
        return True


class RedisNonceStore:
    def __init__(self, redis):
        self._redis = redis

    async def claim(self, key: str, ttl_seconds: int) -> bool:
        return bool(await self._redis.set(f"nonce:{key}", "1", nx=True, ex=ttl_seconds))

    async def ping(self) -> bool:
        return bool(await self._redis.ping())


class RedisRateLimiter:
    """Fixed window per key: INCR, and set the expiry only when the window starts."""

    def __init__(self, redis):
        self._redis = redis

    async def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        window = int(time.time() // window_seconds)
        redis_key = f"rate:{key}:{window}"
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(redis_key)
            pipe.expire(redis_key, window_seconds + 1, nx=True)
            count, _ = await pipe.execute()
        return int(count) <= limit

    async def ping(self) -> bool:
        return bool(await self._redis.ping())
