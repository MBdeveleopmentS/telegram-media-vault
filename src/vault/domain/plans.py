"""Plan entitlements. The single source of truth for tier limits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from vault.config import Settings
from vault.db.models import Subscription, Tier


@dataclass(frozen=True)
class PlanLimits:
    tier: Tier
    max_chats: int
    max_file_bytes: int


@dataclass(frozen=True)
class PremiumOffer:
    product: str
    price_stars: int
    period_days: int


PREMIUM_PRODUCT = "premium_90d"


def limits_for(tier: Tier, s: Settings) -> PlanLimits:
    if tier == Tier.PREMIUM:
        return PlanLimits(Tier.PREMIUM, s.premium_max_chats, s.premium_max_file_bytes)
    return PlanLimits(Tier.FREE, s.free_max_chats, s.free_max_file_bytes)


def premium_offer(s: Settings) -> PremiumOffer:
    return PremiumOffer(PREMIUM_PRODUCT, s.premium_price_stars, s.premium_period_days)


def effective_tier(sub: Subscription | None, now: datetime) -> Tier:
    """Premium only counts while unexpired, even before the downgrade job runs."""
    if sub is None or sub.tier != Tier.PREMIUM:
        return Tier.FREE
    if sub.premium_expires_at is None or sub.premium_expires_at <= now:
        return Tier.FREE
    return Tier.PREMIUM
