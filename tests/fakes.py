"""Deterministic fakes for Telegram (MTProto source, Bot API delivery, login)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from vault.authweb.login import (
    CodeExpired,
    FloodWait,
    InvalidCode,
    InvalidPassword,
    PasswordNeeded,
)
from vault.telegram.delivery import DeliveryResult, OutgoingMedia
from vault.telegram.errors import SourceGone, SourceTooLarge


@dataclass
class FakeMedia:
    data: bytes
    media_key: str
    file_name: str | None = None
    # When True, the fake streams more bytes than declared (simulates unknown/lying size).
    lie_about_size: bool = False


class FakeSource:
    """Fake MTProto account. Messages keyed by (chat_id, message_id)."""

    def __init__(self) -> None:
        self.media: dict[tuple[int, int], FakeMedia] = {}
        self.fail_next: list[BaseException] = []
        self.downloads: list[tuple[int, int]] = []
        self.observed_paths: list[Path] = []

    def put(self, chat_id: int, message_id: int, media: FakeMedia) -> None:
        self.media[(chat_id, message_id)] = media

    async def download(
        self,
        chat_id: int,
        access_hash: int | None,
        message_id: int,
        media_key: str,
        dest: Path,
        max_bytes: int,
    ) -> tuple[int, str | None]:
        self.downloads.append((chat_id, message_id))
        self.observed_paths.append(dest)
        if self.fail_next:
            raise self.fail_next.pop(0)
        m = self.media.get((chat_id, message_id))
        if m is None or m.media_key != media_key:
            raise SourceGone()
        written = 0
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            for i in range(0, len(m.data), 64 * 1024):
                chunk = m.data[i : i + 64 * 1024]
                written += len(chunk)
                if written > max_bytes:
                    raise SourceTooLarge()
                fh.write(chunk)
        return written, m.file_name


@dataclass
class FakeRegistry:
    sources: dict[int, FakeSource] = field(default_factory=dict)
    paused: dict[int, float] = field(default_factory=dict)
    invalidated: list[int] = field(default_factory=list)

    def get(self, account_id: int) -> FakeSource | None:
        return self.sources.get(account_id)

    def paused_until(self, account_id: int) -> float:
        return self.paused.get(account_id, 0.0)

    def pause(self, account_id: int, seconds: float) -> None:
        import time

        self.paused[account_id] = time.monotonic() + seconds

    async def on_session_invalid(self, account_id: int) -> None:
        self.invalidated.append(account_id)
        self.sources.pop(account_id, None)


@dataclass
class Sent:
    chat_id: int
    kind: str
    filename: str
    size: int
    caption: str
    content: bytes
    path_existed_during_send: bool


class FakeDelivery:
    def __init__(self) -> None:
        self.sent: list[Sent] = []
        self.texts: list[tuple[int, str]] = []
        self.fail_next: list[BaseException] = []
        self.crash_after_send = False
        self._mid = 1000

    async def send(self, destination_chat_id: int, media: OutgoingMedia) -> DeliveryResult:
        if self.fail_next:
            raise self.fail_next.pop(0)
        content = media.path.read_bytes()
        from vault.telegram.delivery import choose_send_method

        method = choose_send_method(media.kind, media.size)
        self.sent.append(
            Sent(
                destination_chat_id,
                method,
                media.filename,
                media.size,
                media.caption,
                content,
                media.path.exists(),
            )
        )
        self._mid += 1
        if self.crash_after_send:
            raise SimulatedCrash()
        return DeliveryResult(self._mid, f"fu{self._mid}", method)

    async def send_text(self, destination_chat_id: int, text: str) -> None:
        self.texts.append((destination_chat_id, text))


class SimulatedCrash(BaseException):
    """BaseException so it escapes the worker's error handling, like a process kill."""


class FakeLoginClient:
    """Scripted Telegram login. Records only *that* calls happened, never the secrets."""

    def __init__(
        self,
        *,
        user_id: int,
        code: str = "12345",
        password: str | None = None,
        send_error: BaseException | None = None,
        code_expired: bool = False,
    ) -> None:
        self.user_id = user_id
        self._code = code
        self._password = password
        self._send_error = send_error
        self._code_expired = code_expired
        self.closed = False
        self.logged_out = False
        self.calls: list[str] = []

    async def send_code(self, phone: str) -> str:
        self.calls.append("send_code")
        if self._send_error:
            raise self._send_error
        return "hash-abc"

    async def sign_in_code(self, phone: str, code: str, phone_code_hash: str) -> int:
        self.calls.append("sign_in_code")
        if self._code_expired:
            raise CodeExpired()
        if phone_code_hash != "hash-abc" or code != self._code:
            raise InvalidCode()
        if self._password is not None:
            raise PasswordNeeded()
        return self.user_id

    async def sign_in_password(self, password: str) -> int:
        self.calls.append("sign_in_password")
        if password != self._password:
            raise InvalidPassword()
        return self.user_id

    def session_string(self) -> str:
        return "1FAKESESSION" + "A" * 300

    async def log_out(self) -> None:
        self.logged_out = True

    async def close(self) -> None:
        self.closed = True


__all__ = [
    "FakeDelivery",
    "FakeLoginClient",
    "FakeMedia",
    "FakeRegistry",
    "FakeSource",
    "FloodWait",
    "SimulatedCrash",
]
