from collections import Counter

import pytest

from vault.db.models import Tier
from vault.queue.redis_queue import JobQueue, WeightedPicker, enqueued_key


def test_weighted_order_is_75_25():
    p = WeightedPicker(3, 1)
    firsts = Counter(p.order()[0] for _ in range(400))
    assert firsts[Tier.PREMIUM] == 300 and firsts[Tier.FREE] == 100


def test_weights_must_be_positive():
    with pytest.raises(ValueError):
        WeightedPicker(3, 0)


async def test_premium_preferred_under_load_but_free_not_starved(queue: JobQueue):
    for i in range(100):
        await queue.push(0, Tier.PREMIUM, 1000 + i)
        await queue.push(0, Tier.FREE, 2000 + i)
    picker = WeightedPicker(3, 1)
    got = Counter()
    for _ in range(80):
        res = await queue.pop(0, picker)
        assert res is not None
        got[res[1]] += 1
    assert got[Tier.PREMIUM] == 60 and got[Tier.FREE] == 20


async def test_falls_back_when_preferred_queue_empty(queue: JobQueue):
    for i in range(5):
        await queue.push(0, Tier.FREE, i)
    picker = WeightedPicker(3, 1)
    popped = [await queue.pop(0, picker) for _ in range(5)]
    assert [p[0] for p in popped if p] == [0, 1, 2, 3, 4]
    assert await queue.pop(0, picker) is None


async def test_enqueued_set_tracks_membership(queue: JobQueue, redis):
    await queue.push(0, Tier.FREE, 1)
    await queue.push(0, Tier.FREE, 2)
    assert await queue.missing(0, [1, 2, 3]) == [3]
    await queue.pop(0, WeightedPicker(1, 1))
    assert await queue.missing(0, [1, 2]) == [1]
    await redis.flushall()  # simulated Redis data loss
    assert await queue.missing(0, [2]) == [2]
    assert not await redis.exists(enqueued_key(0))


async def test_shards_are_isolated(queue: JobQueue):
    await queue.push(1, Tier.FREE, 9)
    assert await queue.pop(0, WeightedPicker(1, 1)) is None
    assert await queue.pop(1, WeightedPicker(1, 1)) == (9, Tier.FREE)
