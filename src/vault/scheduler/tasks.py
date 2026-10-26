"""Periodic jobs: subscription lifecycle, aggregated notifications, retention."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update

from vault import i18n
from vault.config import Settings
from vault.db.models import (
    AccountStatus,
    AuthenticationFlow,
    EncryptedSession,
    FlowState,
    JobState,
    MediaJob,
    MonitoredChat,
    Payment,
    PaymentStatus,
    SecurityEvent,
    Subscription,
    TelegramAccount,
    Tier,
    User,
)
from vault.db.session import Database
from vault.domain.jobs import SkipReason
from vault.domain.subscriptions import downgrade_if_expired, due_reminder, mark_reminder_sent
from vault.logging import get_logger
from vault.telegram.delivery import DestinationUnavailable, MediaDelivery

log = get_logger(__name__)

# Reasons worth telling the user about (others are consequences of the user's own actions).
NOTIFY_REASONS = {
    SkipReason.TOO_LARGE,
    SkipReason.PROTECTED,
    SkipReason.SOURCE_DELETED,
    SkipReason.BACKLOG_FULL,
}

JOB_RETENTION = timedelta(days=90)
SECURITY_EVENT_RETENTION = timedelta(days=180)
FLOW_RETENTION = timedelta(days=30)
DISCONNECTED_SESSION_GRACE = timedelta(minutes=10)


@dataclass
class Notification:
    chat_id: int
    user_id: int
    text: str


async def _send(notifier: MediaDelivery, db: Database, items: list[Notification]) -> int:
    sent = 0
    for n in items:
        try:
            await notifier.send_text(n.chat_id, n.text)
            sent += 1
        except DestinationUnavailable:
            async with db.transaction() as session:
                await session.execute(update(User).where(User.id == n.user_id).values(destination_ok=False))
        except Exception as exc:
            log.warning("notify_failed", error_code=type(exc).__name__)
    return sent


async def subscription_lifecycle(db: Database, notifier: MediaDelivery, s: Settings, now: datetime) -> int:
    out: list[Notification] = []
    async with db.transaction() as session:
        rows = (
            await session.execute(
                select(Subscription, User)
                .join(User, User.id == Subscription.user_id)
                .where(Subscription.tier == Tier.PREMIUM)
                .with_for_update(of=Subscription, skip_locked=True)
            )
        ).all()
        for sub, user in rows:
            result = await downgrade_if_expired(session, sub, now, s)
            if result is not None:
                titles = {
                    c.id: c.title
                    for c in (
                        await session.scalars(select(MonitoredChat).where(MonitoredChat.user_id == user.id))
                    ).all()
                }
                kept = "\n".join(f"• {titles[i]}" for i in result.kept) or i18n.NONE_LIST
                paused = "\n".join(f"• {titles[i]}" for i in result.paused) or i18n.NONE_LIST
                out.append(
                    Notification(
                        user.telegram_user_id, user.id, i18n.DOWNGRADED.format(kept=kept, paused=paused)
                    )
                )
                continue
            day = due_reminder(sub, now)
            if day is not None and sub.premium_expires_at is not None:
                mark_reminder_sent(sub, day)
                out.append(
                    Notification(
                        user.telegram_user_id,
                        user.id,
                        i18n.REMINDER.format(
                            days_text=i18n.days_text(day),
                            until=i18n.fmt_date(sub.premium_expires_at),
                            free_chats=s.free_max_chats,
                        ),
                    )
                )
    return await _send(notifier, db, out)


async def issue_digests(db: Database, notifier: MediaDelivery, s: Settings, now: datetime) -> int:
    """One aggregated message per user instead of a notification per failure."""
    out: list[Notification] = []
    async with db.transaction() as session:
        users_due = (
            await session.scalars(
                select(User).where(
                    (User.last_issue_digest_at.is_(None))
                    | (User.last_issue_digest_at < now - timedelta(seconds=s.issue_digest_interval_seconds))
                )
            )
        ).all()
        if not users_due:
            return 0
        by_id = {u.id: u for u in users_due}
        jobs = (
            await session.scalars(
                select(MediaJob).where(
                    MediaJob.user_id.in_(list(by_id)),
                    MediaJob.issue_notified.is_(False),
                    MediaJob.state.in_([JobState.SKIPPED, JobState.FAILED]),
                )
            )
        ).all()
        per_user: dict[int, Counter[str]] = defaultdict(Counter)
        for j in jobs:
            j.issue_notified = True
            key = "failed" if j.state == JobState.FAILED else (j.skip_reason or "")
            if j.state == JobState.FAILED or key in NOTIFY_REASONS:
                per_user[j.user_id][key] += 1
        for uid, counts in per_user.items():
            user = by_id[uid]
            if not user.destination_ok:
                continue
            user.last_issue_digest_at = now
            lines = "\n".join(
                f"• {('שגיאת העברה' if k == 'failed' else i18n.SKIP_REASON_LABELS.get(k, k))}: {n}"
                for k, n in counts.most_common()
            )
            out.append(
                Notification(
                    user.telegram_user_id, uid, i18n.ISSUE_DIGEST.format(n=sum(counts.values()), lines=lines)
                )
            )
    return await _send(notifier, db, out)


async def retention(db: Database, now: datetime) -> dict[str, int]:
    counts: dict[str, int] = {}
    async with db.transaction() as session:
        r = await session.execute(
            update(AuthenticationFlow)
            .where(
                AuthenticationFlow.state.in_(
                    [FlowState.CREATED, FlowState.OPENED, FlowState.CODE_SENT, FlowState.PASSWORD_NEEDED]
                ),
                AuthenticationFlow.expires_at < now,
            )
            .values(state=FlowState.EXPIRED, failure_code="expired")
        )
        counts["flows_expired"] = r.rowcount  # type: ignore[attr-defined]
        r = await session.execute(
            delete(AuthenticationFlow).where(AuthenticationFlow.created_at < now - FLOW_RETENTION)
        )
        counts["flows_deleted"] = r.rowcount  # type: ignore[attr-defined]
        r = await session.execute(
            delete(MediaJob).where(
                MediaJob.state.in_([JobState.DELIVERED, JobState.SKIPPED, JobState.FAILED]),
                MediaJob.finished_at < now - JOB_RETENTION,
            )
        )
        counts["jobs_deleted"] = r.rowcount  # type: ignore[attr-defined]
        r = await session.execute(
            delete(SecurityEvent).where(SecurityEvent.created_at < now - SECURITY_EVENT_RETENTION)
        )
        counts["security_events_deleted"] = r.rowcount  # type: ignore[attr-defined]
        r = await session.execute(
            update(Payment)
            .where(Payment.status == PaymentStatus.PENDING, Payment.expires_at < now)
            .values(status=PaymentStatus.EXPIRED)
        )
        counts["invoices_expired"] = r.rowcount  # type: ignore[attr-defined]
        # Fallback purge: the engine normally revokes + deletes within seconds of a disconnect.
        stale_ids = select(TelegramAccount.id).where(
            TelegramAccount.status.in_([AccountStatus.DISCONNECTED]),
            TelegramAccount.disconnected_at < now - DISCONNECTED_SESSION_GRACE,
        )
        r = await session.execute(delete(EncryptedSession).where(EncryptedSession.account_id.in_(stale_ids)))
        counts["sessions_purged"] = r.rowcount  # type: ignore[attr-defined]
    if any(counts.values()):
        log.info("retention", **counts)
    return counts
