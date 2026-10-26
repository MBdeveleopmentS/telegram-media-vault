"""Transfer workers: claim -> download (temp) -> Bot API upload -> record -> cleanup.

Delivery semantics: at-least-once with duplicate minimization.
Telegram's acceptance of an upload and our DB commit are two separate systems,
so a crash between them is ambiguous. We record ``UPLOADING`` with
``upload_started_at`` *before* sending; if the lease expires in that state the
recovery loop marks the job ``delivery_ambiguous`` and retries, and the retried
caption tells the user the file may be a duplicate. See docs/ARCHITECTURE.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from sqlalchemy import select, update

from vault import i18n
from vault.config import Settings
from vault.db.models import (
    AccountStatus,
    ChatStatus,
    JobState,
    MediaDelivery,
    MediaJob,
    MonitoredChat,
    TelegramAccount,
    User,
    utcnow,
)
from vault.db.session import Database
from vault.domain.jobs import TERMINAL_STATES, RetryPolicy, SkipReason, check_transition
from vault.domain.media import safe_upload_name
from vault.engine.tempfiles import job_workspace
from vault.logging import get_logger
from vault.queue.redis_queue import JobQueue, WeightedPicker
from vault.security import events
from vault.telegram.delivery import (
    DeliveryError,
    DeliveryRateLimited,
    DestinationUnavailable,
    OutgoingMedia,
)
from vault.telegram.delivery import (
    MediaDelivery as DeliveryPort,
)
from vault.telegram.errors import (
    SourceChatInaccessible,
    SourceError,
    SourceFloodWait,
    SourceSessionInvalid,
    SourceUnavailable,
)

log = get_logger(__name__)


class SourceAccount(Protocol):
    async def download(
        self,
        chat_id: int,
        access_hash: int | None,
        message_id: int,
        media_key: str,
        dest: Path,
        max_bytes: int,
    ) -> tuple[int, str | None]: ...


class SourceRegistry(Protocol):
    def get(self, account_id: int) -> SourceAccount | None: ...

    def paused_until(self, account_id: int) -> float: ...

    def pause(self, account_id: int, seconds: float) -> None: ...

    async def on_session_invalid(self, account_id: int) -> None: ...


@dataclass
class WorkerPoolState:
    """Shared by all workers in one engine process."""

    per_user_limit: int
    active_per_user: dict[int, int] = field(default_factory=dict)
    delivery_paused_until: float = 0.0

    def try_acquire(self, user_id: int) -> bool:
        if self.active_per_user.get(user_id, 0) >= self.per_user_limit:
            return False
        self.active_per_user[user_id] = self.active_per_user.get(user_id, 0) + 1
        return True

    def release(self, user_id: int) -> None:
        n = self.active_per_user.get(user_id, 0) - 1
        if n <= 0:
            self.active_per_user.pop(user_id, None)
        else:
            self.active_per_user[user_id] = n


@dataclass
class JobContext:
    job: MediaJob
    chat: MonitoredChat
    account: TelegramAccount
    user: User


class LeaseLost(Exception):
    pass


class TransferWorker:
    def __init__(
        self,
        *,
        worker_id: str,
        db: Database,
        queue: JobQueue,
        sources: SourceRegistry,
        delivery: DeliveryPort,
        settings: Settings,
        pool: WorkerPoolState,
        picker: WeightedPicker,
        temp_root: Path,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.worker_id = worker_id
        self.db = db
        self.queue = queue
        self.sources = sources
        self.delivery = delivery
        self.s = settings
        self.pool = pool
        self.picker = picker
        self.temp_root = temp_root
        self.clock = clock
        self.retry = RetryPolicy(
            settings.job_max_attempts, settings.retry_base_seconds, settings.retry_max_seconds
        )
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    async def run_forever(self, idle_sleep: float = 0.5) -> None:
        while not self._stopping:
            try:
                worked = await self.run_once()
            except Exception as exc:  # never let one bad job kill the worker
                log.error("worker_loop_error", error_code=type(exc).__name__)
                worked = False
                await asyncio.sleep(1.0)
            if not worked:
                await asyncio.sleep(idle_sleep)

    # ----------------------------------------------------------------- core
    async def run_once(self) -> bool:
        """Process at most one job. Returns True if a queue item was consumed."""
        if time.monotonic() < self.pool.delivery_paused_until:
            return False
        popped = await self.queue.pop(self.s.engine_shard, self.picker)
        if popped is None:
            return False
        job_id, tier = popped

        async with self.db.session() as session:
            job = await session.get(MediaJob, job_id)
        if job is None or job.state != JobState.QUEUED or job.shard != self.s.engine_shard:
            return True  # stale or duplicate queue entry
        if time.monotonic() < self.sources.paused_until(job.account_id) or not self.pool.try_acquire(
            job.user_id
        ):
            # Fairness / FloodWait: put it back at the tail and let other users proceed.
            await self.queue.push(self.s.engine_shard, tier, job_id)
            return True
        try:
            if not await self._claim(job_id):
                return True
            await self._process(job_id)
        finally:
            self.pool.release(job.user_id)
        return True

    async def _claim(self, job_id: int) -> bool:
        now = self.clock()
        async with self.db.transaction() as session:
            res = await session.execute(
                update(MediaJob)
                .where(MediaJob.id == job_id, MediaJob.state == JobState.QUEUED)
                .values(
                    state=JobState.DOWNLOADING,
                    lease_owner=self.worker_id,
                    lease_expires_at=now + timedelta(seconds=self.s.job_lease_seconds),
                    attempts=MediaJob.attempts + 1,
                    updated_at=now,
                )
            )
            return bool(res.rowcount)  # type: ignore[attr-defined]

    async def _load(self, job_id: int) -> JobContext:
        async with self.db.session() as session:
            row = (
                await session.execute(
                    select(MediaJob, MonitoredChat, TelegramAccount, User)
                    .join(MonitoredChat, MonitoredChat.id == MediaJob.monitored_chat_id)
                    .join(TelegramAccount, TelegramAccount.id == MediaJob.account_id)
                    .join(User, User.id == MediaJob.user_id)
                    .where(MediaJob.id == job_id)
                )
            ).one()
        return JobContext(*row)

    async def _set_state(self, job_id: int, new: JobState, **values: object) -> None:
        """Lease-guarded transition. Raises LeaseLost if recovery reassigned the job."""
        async with self.db.transaction() as session:
            job = await session.get(MediaJob, job_id, with_for_update=True)
            if job is None or job.lease_owner != self.worker_id or job.state in TERMINAL_STATES:
                raise LeaseLost()
            check_transition(job.state, new)
            job.state = new
            job.updated_at = self.clock()
            for k, v in values.items():
                setattr(job, k, v)

    async def _heartbeat(self, job_id: int) -> None:
        interval = max(1.0, self.s.job_lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            async with self.db.transaction() as session:
                await session.execute(
                    update(MediaJob)
                    .where(MediaJob.id == job_id, MediaJob.lease_owner == self.worker_id)
                    .values(lease_expires_at=self.clock() + timedelta(seconds=self.s.job_lease_seconds))
                )

    async def _process(self, job_id: int) -> None:
        ctx = await self._load(job_id)
        job, chat, account, user = ctx.job, ctx.chat, ctx.account, ctx.user

        # --- authorization re-checks at processing time (state may have changed since ingest)
        if account.status != AccountStatus.ACTIVE:
            await self._finish_skipped(ctx, SkipReason.ACCOUNT_INACTIVE)
            return
        if chat.status != ChatStatus.ACTIVE:
            await self._finish_skipped(ctx, SkipReason.CHAT_INACTIVE)
            return
        if not user.destination_ok:
            await self._finish_skipped(ctx, SkipReason.DESTINATION_UNAVAILABLE)
            return
        # Destination ownership: the only allowed destination is the private chat
        # between the bot and the account owner, and the MTProto identity must match.
        if account.tg_user_id != user.telegram_user_id or chat.user_id != user.id or job.user_id != user.id:
            async with self.db.transaction() as session:
                await events.record(
                    session,
                    "destination_mismatch",
                    severity="critical",
                    user_id=user.id,
                    details={"job_id": job.id},
                )
            await self._finish_failed(ctx, "identity_mismatch")
            return
        destination_chat_id = user.telegram_user_id

        source = self.sources.get(account.id)
        heartbeat = asyncio.create_task(self._heartbeat(job_id))
        try:
            if source is None:
                raise SourceUnavailable()
            with job_workspace(self.temp_root, job.id) as payload:
                size, original_name = await source.download(
                    chat.chat_id,
                    chat.access_hash,
                    job.source_message_id,
                    job.media_key,
                    payload,
                    job.max_bytes,
                )
                await self._set_state(job_id, JobState.DOWNLOADED)
                await self._set_state(job_id, JobState.UPLOADING, upload_started_at=self.clock())
                caption = i18n.CAPTION.format(title=chat.title, date=i18n.fmt_dt(job.source_date))
                if job.delivery_ambiguous:
                    caption += i18n.CAPTION_AMBIGUOUS
                result = await self.delivery.send(
                    destination_chat_id,
                    OutgoingMedia(
                        path=payload,
                        kind=job.media_kind,
                        filename=safe_upload_name(original_name, job.mime_type, job.id),
                        size=size,
                        caption=caption[:1000],
                    ),
                )
                await self._finish_delivered(
                    ctx, destination_chat_id, result.message_id, result.file_unique_id, size, result.sent_as
                )
            # payload directory removed here (also on every exception path)
        except LeaseLost:
            log.warning("lease_lost", job_id=job_id)
        except SourceFloodWait as e:
            self.sources.pause(account.id, e.seconds)
            await self._retry(ctx, e.code, min_wait=e.seconds, count_attempt=False)
        except DeliveryRateLimited as e:
            self.pool.delivery_paused_until = time.monotonic() + e.retry_after
            await self._retry(ctx, e.code, min_wait=e.retry_after, count_attempt=False)
        except SourceSessionInvalid as e:
            await self._on_session_invalid(ctx)
            await self._finish_skipped(ctx, SkipReason.ACCOUNT_INACTIVE, error_code=e.code)
        except SourceChatInaccessible as e:
            await self._on_chat_inaccessible(ctx)
            await self._finish_skipped(ctx, SkipReason.CHAT_INACTIVE, error_code=e.code)
        except DestinationUnavailable as e:
            async with self.db.transaction() as session:
                u = await session.get(User, user.id)
                if u is not None:
                    u.destination_ok = False
            await self._finish_skipped(ctx, SkipReason.DESTINATION_UNAVAILABLE, error_code=e.code)
        except (SourceError, DeliveryError) as e:
            if e.retryable:
                await self._retry(ctx, e.code)
            elif e.code in {SkipReason.TOO_LARGE, SkipReason.PROTECTED, SkipReason.SOURCE_DELETED}:
                await self._finish_skipped(ctx, e.code)
            else:
                await self._finish_failed(ctx, e.code)
        except Exception as e:  # unexpected: retry with backoff, never crash the worker
            log.error("job_unexpected_error", job_id=job_id, error_code=type(e).__name__)
            await self._retry(ctx, "internal_error")
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    # ----------------------------------------------------------------- outcomes
    async def _finish_delivered(
        self, ctx: JobContext, dest: int, message_id: int, file_unique_id: str | None, size: int, sent_as: str
    ) -> None:
        now = self.clock()
        async with self.db.transaction() as session:
            job = await session.get(MediaJob, ctx.job.id, with_for_update=True)
            if job is None or job.state == JobState.DELIVERED:
                return
            if job.lease_owner == self.worker_id:
                check_transition(job.state, JobState.DELIVERED)
            else:
                # Lease was reclaimed while we were uploading, but Telegram accepted the
                # media. Record it anyway ("late confirmation") so it isn't resent.
                log.warning("late_delivery_confirmation", job_id=job.id, state=job.state.value)
            job.state = JobState.DELIVERED
            job.finished_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            job.last_error_code = None
            session.add(
                MediaDelivery(
                    job_id=job.id,
                    user_id=job.user_id,
                    destination_chat_id=dest,
                    bot_message_id=message_id,
                    file_unique_id=file_unique_id,
                    bytes=size,
                    sent_as=sent_as,
                    delivered_at=now,
                )
            )
            await session.execute(
                update(MonitoredChat)
                .where(MonitoredChat.id == job.monitored_chat_id)
                .values(delivered_count=MonitoredChat.delivered_count + 1, last_delivered_at=now)
            )
        log.info("delivered", job_id=ctx.job.id, media_kind=ctx.job.media_kind.value, bytes=size)

    async def _finish_skipped(self, ctx: JobContext, reason: str, error_code: str | None = None) -> None:
        await self._terminal(ctx, JobState.SKIPPED, skip_reason=reason, error_code=error_code)

    async def _finish_failed(self, ctx: JobContext, error_code: str) -> None:
        await self._terminal(ctx, JobState.FAILED, skip_reason=None, error_code=error_code)

    async def _terminal(
        self, ctx: JobContext, state: JobState, *, skip_reason: str | None, error_code: str | None
    ) -> None:
        now = self.clock()
        async with self.db.transaction() as session:
            job = await session.get(MediaJob, ctx.job.id, with_for_update=True)
            if job is None or job.lease_owner != self.worker_id:
                return
            check_transition(job.state, state)
            job.state = state
            job.skip_reason = skip_reason
            job.last_error_code = error_code
            job.finished_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            col = MonitoredChat.skipped_count if state == JobState.SKIPPED else MonitoredChat.failed_count
            await session.execute(
                update(MonitoredChat).where(MonitoredChat.id == job.monitored_chat_id).values({col: col + 1})
            )
        log.info(
            "job_terminal",
            job_id=ctx.job.id,
            state=state.value,
            skip_reason=skip_reason,
            error_code=error_code,
        )

    async def _retry(
        self, ctx: JobContext, error_code: str, *, min_wait: float = 0, count_attempt: bool = True
    ) -> None:
        now = self.clock()
        async with self.db.transaction() as session:
            job = await session.get(MediaJob, ctx.job.id, with_for_update=True)
            if job is None or job.lease_owner != self.worker_id:
                return
            if not count_attempt:
                job.attempts = max(0, job.attempts - 1)
            if not self.retry.should_retry(job.attempts):
                check_transition(job.state, JobState.FAILED)
                job.state = JobState.FAILED
                job.finished_at = now
                await session.execute(
                    update(MonitoredChat)
                    .where(MonitoredChat.id == job.monitored_chat_id)
                    .values(failed_count=MonitoredChat.failed_count + 1)
                )
            else:
                check_transition(job.state, JobState.RETRY_WAIT)
                job.state = JobState.RETRY_WAIT
                job.next_attempt_at = self.retry.next_attempt_at(
                    now, max(1, job.attempts), min_wait_seconds=min_wait
                )
            job.last_error_code = error_code
            job.lease_owner = None
            job.lease_expires_at = None
            final = job.state
        log.info("job_retry", job_id=ctx.job.id, state=final.value, error_code=error_code)

    async def _on_session_invalid(self, ctx: JobContext) -> None:
        async with self.db.transaction() as session:
            acc = await session.get(TelegramAccount, ctx.account.id)
            if acc is not None and acc.status == AccountStatus.ACTIVE:
                acc.status = AccountStatus.EXPIRED
                acc.last_error_code = "session_invalid"
                await events.record(
                    session,
                    "session_invalid",
                    severity="warning",
                    user_id=ctx.user.id,
                    details={"account_id": acc.id},
                )
                notify = True
            else:
                notify = False
        await self.sources.on_session_invalid(ctx.account.id)
        if notify:
            await self._notify(ctx.user.telegram_user_id, i18n.SESSION_EXPIRED)

    async def _on_chat_inaccessible(self, ctx: JobContext) -> None:
        async with self.db.transaction() as session:
            chat = await session.get(MonitoredChat, ctx.chat.id)
            changed = chat is not None and chat.status == ChatStatus.ACTIVE
            if chat is not None and changed:
                chat.status = ChatStatus.INACCESSIBLE
        if changed:
            await self._notify(
                ctx.user.telegram_user_id, i18n.CHAT_INACCESSIBLE_NOTICE.format(title=ctx.chat.title)
            )

    async def _notify(self, chat_id: int, text: str) -> None:
        try:
            await self.delivery.send_text(chat_id, text)
        except Exception as exc:
            log.warning("notify_failed", error_code=type(exc).__name__)
