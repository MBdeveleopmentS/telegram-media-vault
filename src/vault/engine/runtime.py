"""Account runtime: owns the live Telethon clients of one engine shard.

Why downloads happen in the same process as the listener: an MTProto
authorization key must not be used concurrently from several processes
(Telegram may answer AUTH_KEY_DUPLICATED and revoke it). Each account is
therefore owned by exactly one engine shard (``telegram_accounts.shard``), and
that shard runs both the listener and the transfer workers for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from telethon import TelegramClient, events

from vault import i18n
from vault.config import Settings
from vault.db.models import (
    AccountStatus,
    ChatStatus,
    EncryptedSession,
    MonitoredChat,
    TelegramAccount,
    User,
    utcnow,
)
from vault.db.session import Database
from vault.domain.media import IncomingMedia
from vault.engine.ingest import AccountContext, ingest
from vault.logging import get_logger
from vault.queue.redis_queue import CONTROL_CHANNEL, JobQueue
from vault.security import events as sec_events
from vault.security.crypto import DecryptionError, SealedSession, SessionCipher, session_aad
from vault.telegram.delivery import MediaDelivery
from vault.telegram.errors import SourceError, SourceSessionInvalid
from vault.telegram.mtproto import SESSION_INVALID_ERRORS, TelethonAccount, make_client, to_incoming

log = get_logger(__name__)

INBOX_MAX = 10_000
_INVALID_SESSION: tuple[type[BaseException], ...] = (SourceSessionInvalid, *SESSION_INVALID_ERRORS)


@dataclass
class LiveAccount:
    ctx: AccountContext
    client: TelegramClient
    adapter: TelethonAccount
    approved: set[int] = field(default_factory=set)
    paused_until: float = 0.0


class AccountRuntime:
    def __init__(
        self,
        *,
        db: Database,
        queue: JobQueue,
        settings: Settings,
        cipher: SessionCipher,
        bot_user_id: int,
        notifier: MediaDelivery,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.db = db
        self.queue = queue
        self.s = settings
        self.cipher = cipher
        self.bot_user_id = bot_user_id
        self.notifier = notifier
        self.clock = clock
        self.live: dict[int, LiveAccount] = {}
        self.inbox: asyncio.Queue[tuple[AccountContext, IncomingMedia]] = asyncio.Queue(INBOX_MAX)
        self._lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[Any]] = []

    # ------------------------------------------------ SourceRegistry protocol
    def get(self, account_id: int) -> TelethonAccount | None:
        la = self.live.get(account_id)
        return la.adapter if la else None

    def paused_until(self, account_id: int) -> float:
        la = self.live.get(account_id)
        return la.paused_until if la else 0.0

    def pause(self, account_id: int, seconds: float) -> None:
        la = self.live.get(account_id)
        if la:
            la.paused_until = max(la.paused_until, time.monotonic() + seconds)

    async def on_session_invalid(self, account_id: int) -> None:
        await self.stop_account(account_id, logout=False)

    def account_for_user(self, user_id: int) -> LiveAccount | None:
        for la in self.live.values():
            if la.ctx.user_id == user_id:
                return la
        return None

    # ------------------------------------------------------------- lifecycle
    def start_background(self) -> None:
        for _ in range(2):
            self._tasks.append(asyncio.create_task(self._ingest_consumer()))
        self._tasks.append(asyncio.create_task(self._control_listener()))

    async def shutdown(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        for account_id in list(self.live):
            await self.stop_account(account_id, logout=False)

    async def sync(self) -> None:
        """Converge live clients with the database (source of truth)."""
        async with self._lock:
            async with self.db.session() as session:
                accounts = (
                    await session.scalars(
                        select(TelegramAccount).where(TelegramAccount.shard == self.s.engine_shard)
                    )
                ).all()
            desired = {a.id: a for a in accounts if a.status == AccountStatus.ACTIVE}
            for account_id in list(self.live):
                if account_id not in desired:
                    acc = next((a for a in accounts if a.id == account_id), None)
                    # User-initiated disconnect (or row deleted): also revoke at Telegram.
                    logout = acc is None or acc.status == AccountStatus.DISCONNECTED
                    await self.stop_account(account_id, logout=logout)
            for acc in accounts:
                if acc.status == AccountStatus.DISCONNECTED:
                    await self._revoke_and_purge(acc)
            for account_id, acc in desired.items():
                if account_id not in self.live:
                    await self.start_account(acc)
            for la in self.live.values():
                await self._refresh_approved(la)

    async def _refresh_approved(self, la: LiveAccount) -> None:
        async with self.db.session() as session:
            rows = await session.scalars(
                select(MonitoredChat.chat_id).where(
                    MonitoredChat.account_id == la.ctx.account_id, MonitoredChat.status == ChatStatus.ACTIVE
                )
            )
            la.approved = set(rows.all())

    async def start_account(self, acc: TelegramAccount) -> None:
        async with self.db.session() as session:
            sealed_row = await session.scalar(
                select(EncryptedSession).where(EncryptedSession.account_id == acc.id)
            )
            user = await session.get(User, acc.user_id)
        if sealed_row is None or user is None:
            await self._mark_expired(acc.id, acc.user_id, "session_missing", notify_chat=None)
            return
        if acc.tg_user_id != user.telegram_user_id:
            async with self.db.transaction() as session:
                await sec_events.record(
                    session,
                    "identity_invariant_violation",
                    severity="critical",
                    user_id=user.id,
                    details={"account_id": acc.id},
                )
            return
        aad = session_aad(acc.id, acc.tg_user_id)
        sealed = SealedSession(sealed_row.key_id, sealed_row.nonce, sealed_row.ciphertext)
        try:
            session_str = self.cipher.open(sealed, aad)
        except DecryptionError:
            async with self.db.transaction() as session:
                await sec_events.record(
                    session,
                    "session_decrypt_failed",
                    severity="critical",
                    user_id=user.id,
                    details={"account_id": acc.id},
                )
            return
        assert self.s.telegram_api_id is not None and self.s.telegram_api_hash is not None
        client = make_client(session_str, self.s.telegram_api_id, self.s.telegram_api_hash.get_secret_value())
        del session_str
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise SourceSessionInvalid()
            me = await client.get_me()
            if me is None or me.id != acc.tg_user_id:
                raise SourceSessionInvalid()
        except _INVALID_SESSION:
            with contextlib.suppress(Exception):
                await client.disconnect()
            await self._mark_expired(acc.id, user.id, "session_invalid", notify_chat=user.telegram_user_id)
            return
        except Exception as exc:  # network etc.: try again on next sync
            log.warning("account_connect_failed", account_id=acc.id, error_code=type(exc).__name__)
            with contextlib.suppress(Exception):
                await client.disconnect()
            return

        # Re-seal if the key rotated or Telethon changed the session (e.g. DC migration).
        new_str = client.session.save()
        if self.cipher.needs_rotation(sealed) or new_str != self.cipher.open(sealed, aad):
            resealed = self.cipher.seal(new_str, aad)
            async with self.db.transaction() as session:
                row = await session.scalar(
                    select(EncryptedSession).where(EncryptedSession.account_id == acc.id)
                )
                if row is not None:
                    row.key_id, row.nonce, row.ciphertext = (
                        resealed.key_id,
                        resealed.nonce,
                        resealed.ciphertext,
                    )
        del new_str

        ctx = AccountContext(acc.id, user.id, acc.shard, self.bot_user_id)
        la = LiveAccount(ctx=ctx, client=client, adapter=TelethonAccount(client, self.bot_user_id))
        self.live[acc.id] = la
        await self._refresh_approved(la)

        async def on_new_message(event: Any) -> None:
            # Hot path: cheap membership check, then hand off. No I/O here.
            if event.chat_id not in la.approved or getattr(event.message, "media", None) is None:
                return
            protected = bool(getattr(event.chat, "noforwards", False)) if event.chat else False
            item = to_incoming(event.message, chat_protected=protected)
            try:
                self.inbox.put_nowait((la.ctx, item))
            except asyncio.QueueFull:
                log.error("inbox_full_dropped", account_id=la.ctx.account_id)

        client.add_event_handler(on_new_message, events.NewMessage())
        log.info("account_started", account_id=acc.id)
        task = asyncio.create_task(self._catch_up(la))
        self._tasks.append(task)

    async def _catch_up(self, la: LiveAccount) -> None:
        async with self.db.session() as session:
            chats = (
                await session.scalars(
                    select(MonitoredChat).where(
                        MonitoredChat.account_id == la.ctx.account_id,
                        MonitoredChat.status == ChatStatus.ACTIVE,
                    )
                )
            ).all()
        for chat in chats:
            if not chat.last_seen_message_id:
                continue
            try:
                async for item in la.adapter.iter_since(
                    chat.chat_id, chat.access_hash, chat.last_seen_message_id, self.s.catchup_max_messages
                ):
                    await ingest(self.db, self.queue, la.ctx, item, self.clock(), self.s)
            except SourceError as e:
                log.warning("catch_up_failed", account_id=la.ctx.account_id, error_code=e.code)

    async def _revoke_and_purge(self, acc: TelegramAccount) -> None:
        """For a user-disconnected account whose client is not live: log out at Telegram
        using the stored session (best effort), then destroy the ciphertext."""
        async with self.db.session() as session:
            row = await session.scalar(select(EncryptedSession).where(EncryptedSession.account_id == acc.id))
        if row is None:
            return
        try:
            session_str = self.cipher.open(
                SealedSession(row.key_id, row.nonce, row.ciphertext), session_aad(acc.id, acc.tg_user_id)
            )
            assert self.s.telegram_api_id is not None and self.s.telegram_api_hash is not None
            client = make_client(
                session_str, self.s.telegram_api_id, self.s.telegram_api_hash.get_secret_value()
            )
            del session_str
            await client.connect()
            if await client.is_user_authorized():
                await client.log_out()
            else:
                await client.disconnect()
        except Exception as exc:
            log.warning("revoke_failed", account_id=acc.id, error_code=type(exc).__name__)
        async with self.db.transaction() as session:
            row = await session.scalar(select(EncryptedSession).where(EncryptedSession.account_id == acc.id))
            if row is not None:
                await session.delete(row)
        log.info("session_purged", account_id=acc.id)

    async def stop_account(self, account_id: int, *, logout: bool) -> None:
        la = self.live.pop(account_id, None)
        if la is None:
            return
        try:
            if logout:
                # Terminates this authorization at Telegram (removes the device).
                await la.client.log_out()
            else:
                await la.client.disconnect()
        except Exception as exc:
            log.warning("account_stop_error", account_id=account_id, error_code=type(exc).__name__)
        log.info("account_stopped", account_id=account_id, logout=logout)

    async def _mark_expired(self, account_id: int, user_id: int, code: str, notify_chat: int | None) -> None:
        async with self.db.transaction() as session:
            acc = await session.get(TelegramAccount, account_id)
            if acc is None or acc.status != AccountStatus.ACTIVE:
                return
            acc.status = AccountStatus.EXPIRED
            acc.last_error_code = code
            await sec_events.record(
                session,
                "session_invalid",
                severity="warning",
                user_id=user_id,
                details={"account_id": account_id, "reason": code},
            )
        if notify_chat is not None:
            with contextlib.suppress(Exception):
                await self.notifier.send_text(notify_chat, i18n.SESSION_EXPIRED)

    # ------------------------------------------------------------- loops
    async def _ingest_consumer(self) -> None:
        while True:
            ctx, item = await self.inbox.get()
            try:
                await ingest(self.db, self.queue, ctx, item, self.clock(), self.s)
            except Exception as exc:
                log.error("ingest_failed", account_id=ctx.account_id, error_code=type(exc).__name__)
            finally:
                self.inbox.task_done()

    async def _control_listener(self) -> None:
        """React immediately to connect/disconnect/chat changes published by bot/auth-web."""
        while True:
            try:
                pubsub = self.queue.redis.pubsub()
                await pubsub.subscribe(CONTROL_CHANNEL)
                async for msg in pubsub.listen():
                    if msg.get("type") != "message":
                        continue
                    try:
                        data = json.loads(msg["data"])
                    except (ValueError, TypeError):
                        continue
                    if data.get("shard") not in (None, self.s.engine_shard):
                        continue
                    await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("control_listener_error", error_code=type(exc).__name__)
                await asyncio.sleep(2)
