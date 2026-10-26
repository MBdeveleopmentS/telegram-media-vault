"""Redis queue coordination.

Redis only carries job *ids*; PostgreSQL holds the job record and is the source
of truth. Losing Redis (restart without persistence, FLUSHALL, failover) loses
nothing: the recovery loop re-enqueues QUEUED jobs whose ``queued_at`` is stale.
Duplicated ids in Redis are harmless because workers claim jobs with a
conditional UPDATE in PostgreSQL.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator

from redis.asyncio import Redis

from vault.db.models import Tier

CONTROL_CHANNEL = "vault:control"


def queue_key(shard: int, tier: Tier) -> str:
    return f"vault:q:{shard}:{tier.value.lower()}"


def enqueued_key(shard: int) -> str:
    """Set of job ids currently sitting in this shard's lists (for loss detection)."""
    return f"vault:enq:{shard}"


# LPOP + SREM atomically, so a popped id is never left marked as enqueued.
_POP_LUA = """
local id = redis.call('LPOP', KEYS[1])
if id then redis.call('SREM', KEYS[2], id) end
return id
"""


class WeightedPicker:
    """Smooth weighted round-robin over tiers.

    With weights 3:1 the order is P,P,P,F repeating, so under sustained load
    Premium gets ~75% of dequeues and Free ~25%. When the preferred queue is
    empty the other queue is used, so idle capacity is never wasted and Free is
    never starved.
    """

    def __init__(self, premium_weight: int, free_weight: int) -> None:
        if premium_weight < 1 or free_weight < 1:
            raise ValueError("weights must be >= 1")
        pattern = self._smooth(premium_weight, free_weight)
        self._cycle: Iterator[Tier] = itertools.cycle(pattern)

    @staticmethod
    def _smooth(pw: int, fw: int) -> list[Tier]:
        weights = {Tier.PREMIUM: pw, Tier.FREE: fw}
        current = dict.fromkeys(weights, 0)
        total = pw + fw
        seq: list[Tier] = []
        for _ in range(total):
            for t in current:
                current[t] += weights[t]
            best = max(current, key=lambda t: current[t])
            current[best] -= total
            seq.append(best)
        return seq

    def order(self) -> tuple[Tier, Tier]:
        first = next(self._cycle)
        return (first, Tier.FREE if first == Tier.PREMIUM else Tier.PREMIUM)


class JobQueue:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self._pop = redis.register_script(_POP_LUA)

    async def push(self, shard: int, tier: Tier, job_id: int) -> None:
        pipe = self.redis.pipeline(transaction=True)
        pipe.sadd(enqueued_key(shard), job_id)
        pipe.rpush(queue_key(shard, tier), job_id)
        await pipe.execute()

    async def pop(self, shard: int, picker: WeightedPicker) -> tuple[int, Tier] | None:
        for tier in picker.order():
            raw = await self._pop(keys=[queue_key(shard, tier), enqueued_key(shard)])
            if raw is None:
                continue
            try:
                return int(raw), tier
            except (TypeError, ValueError):
                continue  # queue poisoning: drop entries that aren't job ids
        return None

    async def missing(self, shard: int, job_ids: list[int]) -> list[int]:
        """Return the ids that are NOT currently enqueued in Redis."""
        if not job_ids:
            return []
        flags = await self.redis.smismember(enqueued_key(shard), job_ids)
        return [jid for jid, present in zip(job_ids, flags, strict=True) if not present]

    async def lengths(self, shard: int) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in Tier:
            out[t.value] = int(await self.redis.llen(queue_key(shard, t)))
        return out


def make_redis(url: str) -> Redis:
    """Redis client suitable for blocking pops and pub/sub.

    redis-py defaults to a 5s socket timeout, which races with server-side
    blocking commands (BLPOP) and idle pub/sub reads. We rely on TCP keepalive
    plus periodic health checks to detect dead connections instead.
    """
    return Redis.from_url(url, socket_timeout=None, socket_keepalive=True, health_check_interval=30)
