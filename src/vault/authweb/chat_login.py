"""In-bot account connection (login without leaving Telegram).

The bot collects the phone number (shared contact), the login code (inline
keypad, so the code is never sent as a chat message — Telegram invalidates
codes sent in chats) and, if needed, the 2FA password (the bot deletes that
message immediately). It forwards each step over Redis to this service, which
runs inside the auth-web process: the only process holding API_HASH and the
session encryption key besides the engine.

Codes and passwords live only in transient Redis list entries (consumed with
BLPOP within milliseconds) and in function arguments; they're never stored
or logged.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from redis.asyncio import Redis

from vault.authweb.login import (
    CodeExpired,
    FloodWait,
    InvalidCode,
    InvalidPassword,
    InvalidPhone,
    LoginClient,
    LoginError,
    PasswordNeeded,
    PhoneBanned,
    SignUpRequired,
)
from vault.config import Settings
from vault.db.models import User, utcnow
from vault.db.session import Database
from vault.domain.accounts import IdentityMismatch, store_connected_account
from vault.logging import get_logger
from vault.queue.redis_queue import CONTROL_CHANNEL
from vault.security import events
from vault.security.crypto import Hasher, SessionCipher
from vault.security.ratelimit import RateLimiter

log = get_logger(__name__)

REQUEST_KEY = "vault:authrpc"
RESPONSE_TTL = 30


def _resp_key(req_id: str) -> str:
    return f"vault:authrpc:resp:{req_id}"


class AuthRpcError(Exception):
    def __init__(self, code: str, seconds: int = 0) -> None:
        super().__init__(code)
        self.code = code
        self.seconds = seconds


class AuthRpcClient:
    """Used by the bot."""

    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def call(self, method: str, user_id: int, wait_seconds: float = 30, **params: Any) -> str:
        req_id = uuid.uuid4().hex
        payload = json.dumps({"id": req_id, "method": method, "user_id": user_id, "params": params})
        pipe = self.redis.pipeline(transaction=True)
        pipe.rpush(REQUEST_KEY, payload)
        pipe.expire(REQUEST_KEY, 60)
        await pipe.execute()
        res = await self.redis.blpop([_resp_key(req_id)], timeout=wait_seconds)
        if res is None:
            raise AuthRpcError("timeout")
        data = json.loads(res[1])
        if not data.get("ok"):
            raise AuthRpcError(str(data.get("error", "error")), int(data.get("seconds", 0)))
        return str(data.get("result", ""))


@dataclass
class _Pending:
    client: LoginClient
    phone: str
    code_hash: str
    code_attempts: int = 0
    password_attempts: int = 0
    password_needed: bool = False
    created: float = field(default_factory=time.monotonic)


class ChatLoginService:
    """Runs in auth-web. Keyed by the internal user id the bot derived from the update sender."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        redis: Redis,
        cipher: SessionCipher,
        hasher: Hasher,
        login_factory: Callable[[], LoginClient],
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.s = settings
        self.db = db
        self.redis = redis
        self.cipher = cipher
        self.hasher = hasher
        self.login_factory = login_factory
        self.clock = clock
        self.limiter = RateLimiter(redis, prefix="rl:chatlogin")
        self.pending: dict[int, _Pending] = {}
        self._inflight: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ loop
    async def serve_forever(self) -> None:
        while True:
            try:
                item = await self.redis.blpop([REQUEST_KEY], timeout=5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("authrpc_poll_error", error_code=type(exc).__name__)
                await asyncio.sleep(1)
                continue
            if item is not None:
                task = asyncio.create_task(self._handle(item[1]))
                self._inflight.add(task)
                task.add_done_callback(self._inflight.discard)
            await self.expire()

    async def expire(self) -> None:
        now = time.monotonic()
        for uid in [u for u, p in self.pending.items() if now - p.created > self.s.auth_flow_ttl_seconds]:
            await self._drop(uid)

    async def _drop(self, user_id: int) -> None:
        p = self.pending.pop(user_id, None)
        if p is not None:
            await p.client.close()

    async def _handle(self, raw: bytes | str) -> None:
        try:
            req = json.loads(raw)
            req_id = str(req["id"])
            if len(req_id) != 32:
                return
            method, user_id, params = str(req["method"]), int(req["user_id"]), dict(req.get("params") or {})
        except (ValueError, KeyError, TypeError):
            return
        resp: dict[str, Any]
        try:
            resp = {"ok": True, "result": await self.dispatch(method, user_id, params)}
        except AuthRpcError as e:
            resp = {"ok": False, "error": e.code, "seconds": e.seconds}
        except Exception as exc:
            log.error("authrpc_error", error_code=type(exc).__name__)
            resp = {"ok": False, "error": "internal"}
        pipe = self.redis.pipeline(transaction=True)
        pipe.rpush(_resp_key(req_id), json.dumps(resp))
        pipe.expire(_resp_key(req_id), RESPONSE_TTL)
        await pipe.execute()

    # ------------------------------------------------------------------ steps
    async def dispatch(self, method: str, user_id: int, params: dict[str, Any]) -> str:
        if method == "send_code":
            return await self.send_code(user_id, str(params.get("phone", "")))
        if method == "code":
            return await self.submit_code(user_id, str(params.get("code", "")))
        if method == "password":
            return await self.submit_password(user_id, str(params.get("password", "")))
        if method == "cancel":
            await self._drop(user_id)
            return "cancelled"
        raise AuthRpcError("unknown_method")

    async def send_code(self, user_id: int, phone: str) -> str:
        phone = "+" + "".join(ch for ch in phone if ch.isdigit())
        if not 8 <= len(phone) <= 16:
            raise AuthRpcError("invalid_phone")
        if not await self.limiter.hit(f"user:{user_id}", self.s.auth_max_flows_per_user_per_hour, 3600):
            raise AuthRpcError("rate_limited")
        if not await self.limiter.hit(f"phone:{self.hasher.hash('phone', phone)}", 3, 3600):
            raise AuthRpcError("rate_limited")
        await self._drop(user_id)
        client = self.login_factory()
        try:
            code_hash = await client.send_code(phone)
        except FloodWait as e:
            await client.close()
            raise AuthRpcError("flood_wait", e.seconds) from None
        except (InvalidPhone, PhoneBanned) as e:
            await client.close()
            raise AuthRpcError(e.code) from None
        except LoginError:
            await client.close()
            raise AuthRpcError("telegram_error") from None
        self.pending[user_id] = _Pending(client=client, phone=phone, code_hash=code_hash)
        async with self.db.transaction() as session:
            await events.record(session, "chat_login_code_sent", user_id=user_id)
        return "code_sent"

    def _get(self, user_id: int) -> _Pending:
        p = self.pending.get(user_id)
        if p is None or time.monotonic() - p.created > self.s.auth_flow_ttl_seconds:
            raise AuthRpcError("restart_required")
        return p

    async def submit_code(self, user_id: int, code: str) -> str:
        p = self._get(user_id)
        code = "".join(ch for ch in code if ch.isdigit())
        p.code_attempts += 1
        if p.code_attempts > self.s.auth_max_code_attempts:
            await self._drop(user_id)
            raise AuthRpcError("too_many_attempts")
        if not 4 <= len(code) <= 8:
            raise AuthRpcError("invalid_code")
        try:
            tg_user_id = await p.client.sign_in_code(p.phone, code, p.code_hash)
        except PasswordNeeded:
            p.password_needed = True
            return "password_needed"
        except InvalidCode:
            raise AuthRpcError("invalid_code") from None
        except CodeExpired:
            await self._drop(user_id)
            raise AuthRpcError("code_expired") from None
        except SignUpRequired:
            await self._drop(user_id)
            raise AuthRpcError("signup_required") from None
        except FloodWait as e:
            raise AuthRpcError("flood_wait", e.seconds) from None
        except LoginError:
            await self._drop(user_id)
            raise AuthRpcError("telegram_error") from None
        return await self._finalize(user_id, p, tg_user_id)

    async def submit_password(self, user_id: int, password: str) -> str:
        p = self._get(user_id)
        if not p.password_needed:
            raise AuthRpcError("restart_required")
        p.password_attempts += 1
        if p.password_attempts > self.s.auth_max_password_attempts:
            await self._drop(user_id)
            raise AuthRpcError("too_many_attempts")
        if not password or len(password) > 256:
            raise AuthRpcError("invalid_password")
        try:
            tg_user_id = await p.client.sign_in_password(password)
        except InvalidPassword:
            raise AuthRpcError("invalid_password") from None
        except FloodWait as e:
            raise AuthRpcError("flood_wait", e.seconds) from None
        except LoginError:
            await self._drop(user_id)
            raise AuthRpcError("telegram_error") from None
        return await self._finalize(user_id, p, tg_user_id)

    async def _finalize(self, user_id: int, p: _Pending, tg_user_id: int) -> str:
        session_string = p.client.session_string()
        async with self.db.transaction() as session:
            user = await session.get(User, user_id)
            if user is None:
                raise AuthRpcError("restart_required")
            try:
                acc = await store_connected_account(
                    session, self.cipher, user, tg_user_id, session_string, self.clock(), self.s
                )
            except IdentityMismatch:
                acc = None
                await events.record(
                    session,
                    "identity_mismatch",
                    severity="high",
                    user_id=user_id,
                    details={"actual_user_matches": False},
                )
            else:
                await events.record(
                    session,
                    "account_connected",
                    user_id=user_id,
                    details={"account_id": acc.id if acc else None},
                )
        del session_string
        if acc is None:
            await p.client.log_out()  # never keep someone else's authorization
            await self._drop(user_id)
            raise AuthRpcError("identity_mismatch")
        # Release the auth key before the engine connects with it (no concurrent use).
        await self._drop(user_id)
        await self.redis.publish(
            CONTROL_CHANNEL, json.dumps({"type": "account_connected", "shard": acc.shard})
        )
        return "connected"
