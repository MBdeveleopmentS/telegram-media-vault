from datetime import timedelta

from tests.conftest import NOW
from vault.db.models import ChatStatus, MonitoredChat, Subscription, Tier
from vault.domain.plans import effective_tier, limits_for, premium_offer
from vault.domain.subscriptions import (
    choose_chats_to_keep,
    due_reminder,
    extend_premium,
    mark_reminder_sent,
    revoke_premium_period,
)

MIB = 1024 * 1024


def sub(tier=Tier.FREE, expires=None) -> Subscription:
    return Subscription(user_id=1, tier=tier, premium_expires_at=expires, reminders_sent=[])


def test_free_and_premium_limits(settings):
    free = limits_for(Tier.FREE, settings)
    prem = limits_for(Tier.PREMIUM, settings)
    assert (free.max_chats, free.max_file_bytes) == (3, 5 * MIB)
    assert (prem.max_chats, prem.max_file_bytes) == (10, 15 * MIB)


def test_offer_is_configurable(settings):
    settings.premium_price_stars = 175
    settings.premium_period_days = 30
    o = premium_offer(settings)
    assert (o.price_stars, o.period_days) == (175, 30)


def test_effective_tier_respects_expiry_even_before_downgrade_job():
    assert effective_tier(sub(Tier.PREMIUM, NOW + timedelta(seconds=1)), NOW) == Tier.PREMIUM
    assert effective_tier(sub(Tier.PREMIUM, NOW), NOW) == Tier.FREE
    assert effective_tier(sub(Tier.PREMIUM, None), NOW) == Tier.FREE
    assert effective_tier(None, NOW) == Tier.FREE


def test_new_purchase_starts_now():
    s = sub()
    start, until = extend_premium(s, 90, NOW)
    assert start == NOW and until == NOW + timedelta(days=90)
    assert s.tier == Tier.PREMIUM


def test_early_renewal_extends_from_current_expiry():
    s = sub(Tier.PREMIUM, NOW + timedelta(days=10))
    s.reminders_sent = [7]
    start, until = extend_premium(s, 90, NOW)
    assert start == NOW + timedelta(days=10)
    assert until == NOW + timedelta(days=100)
    assert s.reminders_sent == []


def test_renewal_after_expiry_starts_now():
    s = sub(Tier.PREMIUM, NOW - timedelta(days=3))
    start, until = extend_premium(s, 90, NOW)
    assert start == NOW and until == NOW + timedelta(days=90)


def test_refund_revokes_one_period():
    s = sub(Tier.PREMIUM, NOW + timedelta(days=150))
    revoke_premium_period(s, 90, NOW)
    assert s.premium_expires_at == NOW + timedelta(days=60)
    revoke_premium_period(s, 90, NOW)
    assert s.premium_expires_at == NOW  # never in the future, downgrade task takes over


def test_reminders_7_3_1_each_once():
    s = sub(Tier.PREMIUM, NOW + timedelta(days=10))
    assert due_reminder(s, NOW) is None
    s.premium_expires_at = NOW + timedelta(days=6, hours=23)
    assert due_reminder(s, NOW) == 7
    mark_reminder_sent(s, 7)
    assert due_reminder(s, NOW) is None
    s.premium_expires_at = NOW + timedelta(days=2)
    assert due_reminder(s, NOW) == 3
    mark_reminder_sent(s, 3)
    s.premium_expires_at = NOW + timedelta(hours=5)
    assert due_reminder(s, NOW) == 1
    mark_reminder_sent(s, 1)
    assert due_reminder(s, NOW) is None


def test_late_run_sends_only_most_relevant_reminder():
    s = sub(Tier.PREMIUM, NOW + timedelta(hours=10))
    assert due_reminder(s, NOW) == 1
    mark_reminder_sent(s, 1)
    assert sorted(s.reminders_sent) == [1, 3, 7]


def _chat(i, *, keep=False, status=ChatStatus.ACTIVE):
    return MonitoredChat(
        id=i, title=f"c{i}", status=status, keep_on_free=keep, created_at=NOW + timedelta(minutes=i)
    )


def test_downgrade_keeps_user_selected_first_then_oldest():
    chats = [_chat(i) for i in range(1, 7)]
    chats[4].keep_on_free = True  # id 5
    chats[5].keep_on_free = True  # id 6
    keep, pause = choose_chats_to_keep(chats, 3)
    assert [c.id for c in keep] == [5, 6, 1]
    assert [c.id for c in pause] == [2, 3, 4]


def test_downgrade_ignores_already_paused_chats():
    chats = [_chat(1, status=ChatStatus.PAUSED), _chat(2), _chat(3)]
    keep, pause = choose_chats_to_keep(chats, 3)
    assert [c.id for c in keep] == [2, 3] and pause == []
