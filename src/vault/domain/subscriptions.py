"""Users, subscriptions, entitlement changes and downgrade policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vault.config import Settings
from vault.db.models import ChatStatus, MonitoredChat, Subscription, Tier, User
from vault.domain.plans import effective_tier, limits_for

REMINDER_DAYS = (7, 3, 1)


async def get_or_create_user(session: AsyncSession, telegram_user_id: int) -> User:
    user = await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
    if user is None:
        user = User(telegram_user_id=telegram_user_id)
        session.add(user)
        await session.flush()
        session.add(Subscription(user_id=user.id, tier=Tier.FREE, reminders_sent=[]))
        await session.flush()
    return user


async def get_user(session: AsyncSession, telegram_user_id: int) -> User | None:
    return await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))


async def get_subscription(session: AsyncSession, user_id: int) -> Subscription:
    sub = await session.scalar(select(Subscription).where(Subscription.user_id == user_id))
    if sub is None:
        sub = Subscription(user_id=user_id, tier=Tier.FREE, reminders_sent=[])
        session.add(sub)
        await session.flush()
    return sub


async def current_tier(session: AsyncSession, user_id: int, now: datetime) -> Tier:
    return effective_tier(await get_subscription(session, user_id), now)


def extend_premium(sub: Subscription, period_days: int, now: datetime) -> tuple[datetime, datetime]:
    """Early renewal extends from max(current expiry, now). Returns (from, until)."""
    start = now
    if sub.tier == Tier.PREMIUM and sub.premium_expires_at is not None and sub.premium_expires_at > now:
        start = sub.premium_expires_at
    until = start + timedelta(days=period_days)
    sub.tier = Tier.PREMIUM
    sub.premium_expires_at = until
    sub.reminders_sent = []
    return start, until


def revoke_premium_period(sub: Subscription, period_days: int, now: datetime) -> None:
    """Undo one purchased period (refund). If nothing remains, expiry becomes ``now``
    and the regular downgrade task applies the Free-tier policy."""
    if sub.premium_expires_at is None:
        return
    sub.premium_expires_at = max(sub.premium_expires_at - timedelta(days=period_days), now)
    sub.reminders_sent = []


def due_reminder(sub: Subscription, now: datetime) -> int | None:
    """Return the reminder day (7/3/1) that should be sent now, if any."""
    if sub.tier != Tier.PREMIUM or sub.premium_expires_at is None:
        return None
    remaining = sub.premium_expires_at - now
    if remaining <= timedelta(0):
        return None
    sent = set(sub.reminders_sent or [])
    # Pick the smallest threshold we've crossed and not yet sent.
    candidates = [d for d in REMINDER_DAYS if remaining <= timedelta(days=d) and d not in sent]
    return min(candidates) if candidates else None


def mark_reminder_sent(sub: Subscription, day: int) -> None:
    # Mark this and all larger thresholds as sent so a late run doesn't send stale reminders.
    sub.reminders_sent = sorted({*(sub.reminders_sent or []), *(d for d in REMINDER_DAYS if d >= day)})


@dataclass
class DowngradeResult:
    user_id: int
    kept: list[int] = field(default_factory=list)
    paused: list[int] = field(default_factory=list)


def choose_chats_to_keep(
    chats: list[MonitoredChat], limit: int
) -> tuple[list[MonitoredChat], list[MonitoredChat]]:
    """Deterministic, user-driven selection.

    1. Chats the user explicitly marked ``keep_on_free`` (oldest first).
    2. Remaining slots: oldest-added active chats (the ones that existed under Free).
    Never random, never based on activity, always announced to the user.
    """
    active = [c for c in chats if c.status == ChatStatus.ACTIVE]
    active.sort(key=lambda c: (not c.keep_on_free, c.created_at, c.id))
    return active[:limit], active[limit:]


async def downgrade_if_expired(
    session: AsyncSession, sub: Subscription, now: datetime, s: Settings
) -> DowngradeResult | None:
    if sub.tier != Tier.PREMIUM:
        return None
    if sub.premium_expires_at is not None and sub.premium_expires_at > now:
        return None
    sub.tier = Tier.FREE
    sub.reminders_sent = []
    chats = list(
        (await session.scalars(select(MonitoredChat).where(MonitoredChat.user_id == sub.user_id))).all()
    )
    keep, pause = choose_chats_to_keep(chats, limits_for(Tier.FREE, s).max_chats)
    for c in pause:
        c.status = ChatStatus.PAUSED_TIER
    return DowngradeResult(sub.user_id, kept=[c.id for c in keep], paused=[c.id for c in pause])
