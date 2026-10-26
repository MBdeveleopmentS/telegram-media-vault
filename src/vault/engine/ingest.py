"""Event ingestion: turn an incoming MTProto message into a durable job.

Runs inside the Telethon event handler, so it must be fast: classify, a few
indexed queries, one INSERT, one Redis RPUSH. No downloads here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from vault.config import Settings
from vault.db.models import (
    AccountStatus,
    ChatStatus,
    JobState,
    MediaJob,
    MonitoredChat,
    TelegramAccount,
    User,
)
from vault.db.session import Database
from vault.domain.jobs import ACTIVE_STATES, SkipReason
from vault.domain.media import IncomingMedia, classify, precheck
from vault.domain.plans import limits_for
from vault.domain.subscriptions import current_tier
from vault.logging import get_logger
from vault.queue.redis_queue import JobQueue

log = get_logger(__name__)


class IngestOutcome(StrEnum):
    IGNORED = "ignored"  # not media / not an approved chat
    QUEUED = "queued"
    SKIPPED = "skipped"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class AccountContext:
    account_id: int
    user_id: int
    shard: int
    bot_user_id: int


async def ingest(
    db: Database,
    queue: JobQueue,
    ctx: AccountContext,
    incoming: IncomingMedia,
    now: datetime,
    s: Settings,
) -> IngestOutcome:
    if incoming.chat_id == ctx.bot_user_id:
        return IngestOutcome.IGNORED  # never back up the destination itself
    classified = classify(incoming)

    job_id: int | None = None
    outcome: IngestOutcome
    async with db.transaction() as session:
        row = (
            await session.execute(
                select(MonitoredChat, TelegramAccount, User)
                .join(TelegramAccount, TelegramAccount.id == MonitoredChat.account_id)
                .join(User, User.id == MonitoredChat.user_id)
                .where(
                    MonitoredChat.account_id == ctx.account_id,
                    MonitoredChat.user_id == ctx.user_id,
                    MonitoredChat.chat_id == incoming.chat_id,
                )
            )
        ).first()
        if row is None:
            return IngestOutcome.IGNORED
        chat, account, user = row
        if chat.status != ChatStatus.ACTIVE or account.status != AccountStatus.ACTIVE:
            return IngestOutcome.IGNORED
        if incoming.message_id > chat.last_seen_message_id:
            chat.last_seen_message_id = incoming.message_id
        if classified is None:
            return IngestOutcome.IGNORED

        tier = await current_tier(session, ctx.user_id, now)
        limits = limits_for(tier, s)
        reason = precheck(incoming, classified, limits.max_file_bytes)
        if reason is None and not user.destination_ok:
            reason = SkipReason.DESTINATION_UNAVAILABLE
        if reason is None:
            pending = await session.scalar(
                select(func.count())
                .select_from(MediaJob)
                .where(MediaJob.user_id == ctx.user_id, MediaJob.state.in_(ACTIVE_STATES))
            )
            if int(pending or 0) >= s.max_pending_jobs_per_user:
                reason = SkipReason.BACKLOG_FULL

        job = MediaJob(
            user_id=ctx.user_id,
            account_id=ctx.account_id,
            monitored_chat_id=chat.id,
            shard=ctx.shard,
            source_chat_id=incoming.chat_id,
            source_message_id=incoming.message_id,
            media_key=classified.media_key,
            media_kind=classified.kind,
            mime_type=classified.mime_type,
            declared_size=classified.size,
            grouped_id=incoming.grouped_id,
            source_date=incoming.date,
            priority=tier,
            max_bytes=limits.max_file_bytes,
            state=JobState.SKIPPED if reason else JobState.QUEUED,
            skip_reason=reason,
            queued_at=None if reason else now,
            finished_at=now if reason else None,
            created_at=now,
        )
        try:
            async with session.begin_nested():
                session.add(job)
                await session.flush()
        except IntegrityError:
            return IngestOutcome.DUPLICATE
        if reason:
            chat.skipped_count += 1
            outcome = IngestOutcome.SKIPPED
        else:
            job_id = job.id
            outcome = IngestOutcome.QUEUED

    # Committed. Only now is the job announced to workers (persist before enqueue).
    if job_id is not None:
        try:
            await queue.push(ctx.shard, tier, job_id)
        except Exception as exc:  # Redis down: recovery loop will re-enqueue from PostgreSQL.
            log.warning("enqueue_failed", job_id=job_id, error_code=type(exc).__name__)
    log.info("ingested", outcome=outcome.value, job_id=job_id, account_id=ctx.account_id)
    return outcome
