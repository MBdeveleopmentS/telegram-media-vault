"""Text rendering for bot screens (pure functions + scoped queries)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vault import i18n
from vault.config import Settings
from vault.db.models import ChatStatus, JobState, MediaJob, MonitoredChat, Subscription, TelegramAccount, Tier
from vault.domain.jobs import ACTIVE_STATES
from vault.domain.plans import effective_tier, limits_for
from vault.domain.subscriptions import get_subscription


def chat_details(chat: MonitoredChat, tier: Tier, s: Settings) -> str:
    return i18n.CHAT_DETAILS.format(
        title=chat.title,
        status=i18n.STATUS_LABELS.get(chat.status.value, chat.status.value),
        delivered=chat.delivered_count,
        skipped=chat.skipped_count,
        failed=chat.failed_count,
        last=i18n.fmt_dt(chat.last_delivered_at),
        max_mib=limits_for(tier, s).max_file_bytes // (1024 * 1024),
        keep=i18n.CHAT_KEEP_NOTE if chat.keep_on_free else "",
    )


async def status_text(session: AsyncSession, user_id: int, s: Settings, now: datetime) -> str:
    sub: Subscription = await get_subscription(session, user_id)
    tier = effective_tier(sub, now)
    limits = limits_for(tier, s)
    account = await session.scalar(select(TelegramAccount).where(TelegramAccount.user_id == user_id))
    chats = list((await session.scalars(select(MonitoredChat).where(MonitoredChat.user_id == user_id))).all())
    delivered = sum(c.delivered_count for c in chats)
    skipped = sum(c.skipped_count for c in chats)
    failed = sum(c.failed_count for c in chats)
    last = max((c.last_delivered_at for c in chats if c.last_delivered_at), default=None)
    pending = await session.scalar(
        select(func.count())
        .select_from(MediaJob)
        .where(MediaJob.user_id == user_id, MediaJob.state.in_(ACTIVE_STATES))
    )
    expiry = ""
    if tier == Tier.PREMIUM and sub.premium_expires_at:
        expiry = f" (עד {i18n.fmt_date(sub.premium_expires_at)})"
    text = i18n.STATUS_SUMMARY.format(
        account=i18n.ACCOUNT_LABELS.get(account.status.value if account else None, "—"),
        tier=i18n.TIER_LABELS[tier.value],
        expiry=expiry,
        active=sum(1 for c in chats if c.status == ChatStatus.ACTIVE),
        limit=limits.max_chats,
        max_mib=limits.max_file_bytes // (1024 * 1024),
        delivered=delivered,
        pending=int(pending or 0),
        skipped=skipped,
        failed=failed,
        last=i18n.fmt_dt(last),
    )
    reasons = (
        await session.execute(
            select(MediaJob.skip_reason, func.count())
            .where(
                MediaJob.user_id == user_id,
                MediaJob.state == JobState.SKIPPED,
                MediaJob.finished_at > now - timedelta(days=7),
            )
            .group_by(MediaJob.skip_reason)
        )
    ).all()
    if reasons:
        text += i18n.STATUS_SKIP_REASONS_HEADER
        for reason, n in reasons:
            text += f"\n• {i18n.SKIP_REASON_LABELS.get(reason or '', reason or '')}: {n}"
    if delivered == 0 and chats:
        text += i18n.STATUS_NO_FILES
    return text
