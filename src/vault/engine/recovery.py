"""Crash recovery and queue reconciliation for one shard.

Runs periodically in the engine process:
1. Expired leases (worker crashed / host restarted) are reclaimed.
   - DOWNLOADING / DOWNLOADED: nothing was sent yet -> re-queue.
   - UPLOADING: Telegram may or may not have accepted the upload -> re-queue
     with ``delivery_ambiguous`` (at-least-once; user is told it may be a duplicate).
2. RETRY_WAIT jobs whose backoff elapsed are re-queued.
3. QUEUED jobs that Redis no longer knows about (Redis restart/flush) are re-pushed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update

from vault.config import Settings
from vault.db.models import JobState, MediaJob, MonitoredChat, utcnow
from vault.db.session import Database
from vault.domain.jobs import LEASED_STATES, RetryPolicy
from vault.logging import get_logger
from vault.queue.redis_queue import JobQueue

log = get_logger(__name__)

BATCH = 500


@dataclass
class RecoveryReport:
    reclaimed: int = 0
    ambiguous: int = 0
    exhausted: int = 0
    promoted: int = 0
    repushed: int = 0


async def recover_once(
    db: Database, queue: JobQueue, s: Settings, clock: Callable[[], datetime] = utcnow
) -> RecoveryReport:
    now = clock()
    rep = RecoveryReport()
    policy = RetryPolicy(s.job_max_attempts, s.retry_base_seconds, s.retry_max_seconds)
    to_push: list[MediaJob] = []

    async with db.transaction() as session:
        expired = (
            await session.scalars(
                select(MediaJob)
                .where(
                    MediaJob.shard == s.engine_shard,
                    MediaJob.state.in_(LEASED_STATES),
                    MediaJob.lease_expires_at < now,
                )
                .limit(BATCH)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for job in expired:
            rep.reclaimed += 1
            if job.state == JobState.UPLOADING:
                job.delivery_ambiguous = True
                rep.ambiguous += 1
            job.lease_owner = None
            job.lease_expires_at = None
            job.last_error_code = "lease_expired"
            if policy.should_retry(job.attempts):
                job.state = JobState.QUEUED
                job.queued_at = now
                to_push.append(job)
            else:
                job.state = JobState.FAILED
                job.finished_at = now
                rep.exhausted += 1
                await session.execute(
                    update(MonitoredChat)
                    .where(MonitoredChat.id == job.monitored_chat_id)
                    .values(failed_count=MonitoredChat.failed_count + 1)
                )

        due = (
            await session.scalars(
                select(MediaJob)
                .where(
                    MediaJob.shard == s.engine_shard,
                    MediaJob.state == JobState.RETRY_WAIT,
                    MediaJob.next_attempt_at <= now,
                )
                .limit(BATCH)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for job in due:
            job.state = JobState.QUEUED
            job.queued_at = now
            job.next_attempt_at = None
            to_push.append(job)
            rep.promoted += 1

    pushed_ids = {j.id for j in to_push}
    for job in to_push:
        await queue.push(s.engine_shard, job.priority, job.id)

    # Redis loss detection: QUEUED in PostgreSQL but absent from the Redis set.
    async with db.session() as session:
        queued = (
            await session.execute(
                select(MediaJob.id, MediaJob.priority)
                .where(MediaJob.shard == s.engine_shard, MediaJob.state == JobState.QUEUED)
                .order_by(MediaJob.id)
                .limit(5000)
            )
        ).all()
    prio = {jid: p for jid, p in queued if jid not in pushed_ids}
    missing = await queue.missing(s.engine_shard, list(prio))
    for jid in missing:
        await queue.push(s.engine_shard, prio[jid], jid)
    rep.repushed = len(missing)

    if any((rep.reclaimed, rep.promoted, rep.repushed)):
        log.info("recovery", **rep.__dict__)
    return rep
