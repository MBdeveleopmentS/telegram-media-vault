"""MTProto login step adapter (Telethon) with a test-friendly interface.

Passwords and login codes only ever exist as function arguments here; they
are not logged, stored or put on any queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Protocol

from telethon import errors

from vault.telegram.mtproto import make_client


class LoginError(Exception):
    code = "login_error"


class InvalidPhone(LoginError):
    code = "invalid_phone"


class PhoneBanned(LoginError):
    code = "phone_banned"


class InvalidCode(LoginError):
    code = "invalid_code"


class CodeExpired(LoginError):
    code = "code_expired"


class PasswordNeeded(LoginError):
    code = "password_needed"


class InvalidPassword(LoginError):
    code = "invalid_password"


class FloodWait(LoginError):
    code = "flood_wait"

    def __init__(self, seconds: int) -> None:
        super().__init__(f"flood {seconds}")
        self.seconds = seconds


class SignUpRequired(LoginError):
    code = "signup_required"


class LoginClient(Protocol):
    async def send_code(self, phone: str) -> str: ...

    async def sign_in_code(self, phone: str, code: str, phone_code_hash: str) -> int: ...

    async def sign_in_password(self, password: str) -> int: ...

    def session_string(self) -> str: ...

    async def log_out(self) -> None: ...

    async def close(self) -> None: ...


def _map(exc: BaseException) -> LoginError:
    if isinstance(exc, errors.SessionPasswordNeededError):
        return PasswordNeeded()
    if isinstance(exc, (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError)):
        return InvalidCode()
    if isinstance(exc, errors.PhoneCodeExpiredError):
        return CodeExpired()
    if isinstance(exc, errors.PasswordHashInvalidError):
        return InvalidPassword()
    if isinstance(exc, errors.PhoneNumberInvalidError):
        return InvalidPhone()
    if isinstance(exc, (errors.PhoneNumberBannedError,)):
        return PhoneBanned()
    if isinstance(exc, errors.PhoneNumberUnoccupiedError):
        return SignUpRequired()
    if isinstance(exc, (errors.FloodWaitError, errors.PhoneNumberFloodError, errors.PhonePasswordFloodError)):
        return FloodWait(int(getattr(exc, "seconds", 3600) or 3600))
    return LoginError(type(exc).__name__)


class TelethonLoginClient:
    def __init__(self, api_id: int, api_hash: str) -> None:
        self._client = make_client("", api_id, api_hash)
        self._connected = False

    async def _ensure(self) -> None:
        if not self._connected:
            await self._client.connect()
            self._connected = True

    async def send_code(self, phone: str) -> str:
        await self._ensure()
        try:
            sent = await self._client.send_code_request(phone)
        except Exception as exc:
            raise _map(exc) from None
        return str(sent.phone_code_hash)

    async def sign_in_code(self, phone: str, code: str, phone_code_hash: str) -> int:
        await self._ensure()
        try:
            user = await self._client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except Exception as exc:
            raise _map(exc) from None
        uid = getattr(user, "id", None)
        if uid is None:  # sign-up required (auth.authorizationSignUpRequired)
            raise SignUpRequired()
        return int(uid)

    async def sign_in_password(self, password: str) -> int:
        await self._ensure()
        try:
            user = await self._client.sign_in(password=password)
        except Exception as exc:
            raise _map(exc) from None
        return int(user.id)

    def session_string(self) -> str:
        return str(self._client.session.save())

    async def log_out(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.log_out()

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.disconnect()


@dataclass
class PendingLogin:
    client: LoginClient
    phone: str = ""
    phone_code_hash: str = ""
    created: float = field(default_factory=time.monotonic)


class PendingLogins:
    """In-memory, per-process store of in-progress MTProto logins.

    Kept in memory (not Redis/DB) so the half-authenticated auth key and the
    phone number are never persisted. Consequence: auth-web runs as a single
    process; a restart aborts in-progress logins (user simply requests a new link).
    """

    def __init__(self, ttl_seconds: int) -> None:
        self._items: dict[int, PendingLogin] = {}
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()

    def get(self, flow_id: int) -> PendingLogin | None:
        return self._items.get(flow_id)

    def put(self, flow_id: int, p: PendingLogin) -> None:
        self._items[flow_id] = p

    async def drop(self, flow_id: int) -> None:
        p = self._items.pop(flow_id, None)
        if p is not None:
            await p.client.close()

    async def expire(self) -> int:
        now = time.monotonic()
        stale = [fid for fid, p in self._items.items() if now - p.created > self._ttl]
        for fid in stale:
            await self.drop(fid)
        return len(stale)
