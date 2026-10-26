"""Minimal request/response RPC over Redis lists (bot -> engine).

The bot never holds session keys, so dialog listing and chat verification are
performed by the engine that owns the account. Requests carry the *internal*
user id derived by the bot from the authenticated Telegram update; the engine
only ever looks up the account belonging to that user (tenant isolation).

Responses contain only what the UI needs (id, type, title) and live in Redis
for at most RESPONSE_TTL seconds.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import asdict
from typing import Any

from redis.asyncio import Redis

from vault.engine.runtime import AccountRuntime
from vault.logging import get_logger
from vault.telegram.errors import SourceError

log = get_logger(__name__)

RESPONSE_TTL = 30


def request_key(shard: int) -> str:
    return f"vault:rpc:{shard}"


def response_key(req_id: str) -> str:
    return f"vault:rpc:resp:{req_id}"


class RpcError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RpcClient:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def call(
        self, shard: int, method: str, user_id: int, wait_seconds: float = 15, **params: Any
    ) -> Any:
        req_id = uuid.uuid4().hex
        payload = json.dumps({"id": req_id, "method": method, "user_id": user_id, "params": params})
        await self.redis.rpush(request_key(shard), payload)
        await self.redis.expire(request_key(shard), 60)
        res = await self.redis.blpop([response_key(req_id)], timeout=wait_seconds)
        if res is None:
            raise RpcError("timeout")
        data = json.loads(res[1])
        if not data.get("ok"):
            raise RpcError(str(data.get("error", "error")))
        return data.get("result")


class RpcServer:
    def __init__(self, redis: Redis, runtime: AccountRuntime, shard: int) -> None:
        self.redis = redis
        self.runtime = runtime
        self.shard = shard
        self._sem = asyncio.Semaphore(4)
        self._inflight: set[asyncio.Task[None]] = set()

    async def serve_forever(self) -> None:
        while True:
            try:
                item = await self.redis.blpop([request_key(self.shard)], timeout=5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("rpc_poll_error", error_code=type(exc).__name__)
                await asyncio.sleep(1)
                continue
            if item is None:
                continue
            task = asyncio.create_task(self._handle(item[1]))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

    async def _handle(self, raw: bytes | str) -> None:
        try:
            req = json.loads(raw)
            req_id = str(req["id"])
            if len(req_id) != 32:
                return
        except (ValueError, KeyError, TypeError):
            return
        async with self._sem:
            try:
                result = await self.dispatch(req["method"], int(req["user_id"]), req.get("params") or {})
                resp = {"ok": True, "result": result}
            except RpcError as e:
                resp = {"ok": False, "error": e.code}
            except SourceError as e:
                resp = {"ok": False, "error": e.code}
            except Exception as exc:
                log.error("rpc_error", error_code=type(exc).__name__)
                resp = {"ok": False, "error": "internal"}
        key = response_key(req_id)
        pipe = self.redis.pipeline(transaction=True)
        pipe.rpush(key, json.dumps(resp, ensure_ascii=False))
        pipe.expire(key, RESPONSE_TTL)
        await pipe.execute()

    async def dispatch(self, method: str, user_id: int, params: dict[str, Any]) -> Any:
        la = self.runtime.account_for_user(user_id)
        if la is None:
            raise RpcError("no_account")
        if method == "list_dialogs":
            dialogs = await la.adapter.list_dialogs(limit=400)
            return [asdict(d) | {"chat_type": d.chat_type.value} for d in dialogs]
        if method == "resolve_dialog":
            d = await la.adapter.resolve_dialog(int(params["chat_id"]))
            return None if d is None else asdict(d) | {"chat_type": d.chat_type.value}
        if method == "refresh":
            await self.runtime.sync()
            return True
        raise RpcError("unknown_method")
