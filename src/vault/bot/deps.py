from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from redis.asyncio import Redis

from vault.config import Settings
from vault.db.models import utcnow
from vault.db.session import Database
from vault.engine.rpc import RpcClient
from vault.payments.service import PaymentProvider
from vault.queue.redis_queue import CONTROL_CHANNEL
from vault.security.crypto import Hasher


@dataclass
class DialogCache:
    """Short-lived per-user cache of the dialog list (titles only; in bot memory)."""

    ttl: float = 300
    _items: dict[int, tuple[float, list[dict[str, Any]]]] = field(default_factory=dict)

    def get(self, user_id: int) -> list[dict[str, Any]] | None:
        hit = self._items.get(user_id)
        if hit is None or time.monotonic() - hit[0] > self.ttl:
            self._items.pop(user_id, None)
            return None
        return hit[1]

    def put(self, user_id: int, dialogs: list[dict[str, Any]]) -> None:
        self._items[user_id] = (time.monotonic(), dialogs)

    def clear(self, user_id: int) -> None:
        self._items.pop(user_id, None)


@dataclass
class Deps:
    settings: Settings
    db: Database
    redis: Redis
    rpc: RpcClient
    hasher: Hasher
    payments: PaymentProvider
    dialogs: DialogCache = field(default_factory=DialogCache)
    clock: Callable[[], datetime] = utcnow

    async def publish_control(self, kind: str, shard: int | None = None) -> None:
        await self.redis.publish(CONTROL_CHANNEL, json.dumps({"type": kind, "shard": shard}))
