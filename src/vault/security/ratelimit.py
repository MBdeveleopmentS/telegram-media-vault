from __future__ import annotations

from redis.asyncio import Redis


class RateLimiter:
    """Fixed-window counter in Redis.

    Fails *closed* for authentication endpoints: if Redis is unavailable the
    caller receives an exception and should refuse the request.
    """

    def __init__(self, redis: Redis, prefix: str = "rl") -> None:
        self._r = redis
        self._prefix = prefix

    async def hit(self, bucket: str, limit: int, window_seconds: int) -> bool:
        """Count one hit. Returns True if the request is allowed."""
        key = f"{self._prefix}:{bucket}"
        pipe = self._r.pipeline(transaction=True)
        pipe.incr(key)
        pipe.expire(key, window_seconds, nx=True)
        count, _ = await pipe.execute()
        return int(count) <= limit
