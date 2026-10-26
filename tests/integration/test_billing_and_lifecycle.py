from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.conftest import BOT_ID, seed_chat, seed_tenant
from tests.fakes import FakeDelivery
from vault.db.models import (
    AccountStatus,
    ChatStatus,
    ChatType,
    EncryptedSession,
    JobState,
    MediaJob,
    MonitoredChat,
    Payment,
    PaymentStatus,
    Subscription,
    TelegramAccount,
    Tier,
)
from vault.domain import chats as chat_svc
from vault.domain.accounts import disconnect_account
from vault.domain.media import IncomingMedia, PhotoInfo
from vault.engine.ingest import AccountContext, IngestOutcome, ingest
from vault.payments.service import (
    FakePaymentProvider,
    Fulfilment,
    RefundError,
    apply_successful_payment,
    create_invoice,
    refund_payment,
    validate_pre_checkout,
)
from vault.scheduler import tasks

TG = 555


async def buy(db, settings, clock, user, charge="ch_1"):
    async with db.transaction() as s:
        p = await create_invoice(s, user.id, settings, clock())
    async with db.transaction() as s:
        err = await validate_pre_checkout(
            s,
            from_tg_user_id=user.telegram_user_id,
            payload=p.invoice_payload,
            currency="XTR",
            amount=p.amount,
            now=clock(),
        )
    assert err is None
    async with db.transaction() as s:
        res = await apply_successful_payment(
            s,
            from_tg_user_id=user.telegram_user_id,
            payload=p.invoice_payload,
            currency="XTR",
            amount=p.amount,
            telegram_charge_id=charge,
            provider_charge_id=None,
            now=clock(),
        )
    return p, res


async def sub_of(db, user) -> Subscription:
    async with db.session() as s:
        sub = await s.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub is not None
    return sub


async def test_purchase_grants_90_days_and_premium_limits(db, settings, clock, cipher):
    user, _ = await seed_tenant(db, cipher, TG)
    p, res = await buy(db, settings, clock, user)
    assert p.amount == 150 and p.currency == "XTR"
    assert res.outcome == Fulfilment.APPLIED and res.until == clock() + timedelta(days=90)
    sub = await sub_of(db, user)
    assert sub.tier == Tier.PREMIUM and sub.premium_expires_at == clock() + timedelta(days=90)
    async with db.session() as s:
        pay = await s.get(Payment, p.id)
    assert pay is not None and pay.status == PaymentStatus.PAID and pay.telegram_payment_charge_id == "ch_1"


async def test_early_renewal_stacks(db, settings, clock, cipher):
    user, _ = await seed_tenant(db, cipher, TG)
    await buy(db, settings, clock, user, "ch_a")
    clock.advance(days=80)
    _, res = await buy(db, settings, clock, user, "ch_b")
    assert res.until == clock() - timedelta(days=80) + timedelta(days=180)


async def test_payment_replay_is_idempotent(db, settings, clock, cipher):
    user, _ = await seed_tenant(db, cipher, TG)
    p, _ = await buy(db, settings, clock, user)
    for payload in (p.invoice_payload, "pv1_" + "0" * 32):
        async with db.transaction() as s:
            res = await apply_successful_payment(
                s,
                from_tg_user_id=TG,
                payload=payload,
                currency="XTR",
                amount=150,
                telegram_charge_id="ch_1",
                provider_charge_id=None,
                now=clock(),
            )
        assert res.outcome == Fulfilment.DUPLICATE
    assert (await sub_of(db, user)).premium_expires_at == clock() + timedelta(days=90)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("from_tg_user_id", 999, "user_mismatch"),
        ("amount", 1, "amount_mismatch"),
        ("currency", "USD", "amount_mismatch"),
        ("payload", "pv1_doesnotexist", "unknown_invoice"),
        ("payload", "evil'; DROP TABLE payments;--", "unknown_invoice"),
    ],
)
async def test_forged_pre_checkout_rejected(db, settings, clock, cipher, field, value, error):
    user, _ = await seed_tenant(db, cipher, TG)
    async with db.transaction() as s:
        p = await create_invoice(s, user.id, settings, clock())
    args = dict(from_tg_user_id=TG, payload=p.invoice_payload, currency="XTR", amount=150, now=clock())
    args[field] = value
    async with db.transaction() as s:
        assert await validate_pre_checkout(s, **args) == error


async def test_forged_successful_payment_never_grants(db, settings, clock, cipher):
    user, _ = await seed_tenant(db, cipher, TG)
    other, _ = await seed_tenant(db, cipher, TG + 1)
    async with db.transaction() as s:
        p = await create_invoice(s, user.id, settings, clock())
    async with db.transaction() as s:
        res = await apply_successful_payment(
            s,
            from_tg_user_id=other.telegram_user_id,
            payload=p.invoice_payload,
            currency="XTR",
            amount=150,
            telegram_charge_id="x1",
            provider_charge_id=None,
            now=clock(),
        )
    assert res.outcome == Fulfilment.REJECTED
    assert (await sub_of(db, user)).tier == Tier.FREE
    assert (await sub_of(db, other)).tier == Tier.FREE


async def test_expired_invoice_rejected_at_pre_checkout(db, settings, clock, cipher):
    user, _ = await seed_tenant(db, cipher, TG)
    async with db.transaction() as s:
        p = await create_invoice(s, user.id, settings, clock())
    clock.advance(hours=25)
    async with db.transaction() as s:
        assert (
            await validate_pre_checkout(
                s, from_tg_user_id=TG, payload=p.invoice_payload, currency="XTR", amount=150, now=clock()
            )
            == "expired"
        )


async def test_refund(db, settings, clock, cipher):
    user, _ = await seed_tenant(db, cipher, TG)
    p, _ = await buy(db, settings, clock, user)
    provider = FakePaymentProvider()
    clock.advance(days=1)
    async with db.transaction() as s:
        await refund_payment(s, provider, p.id, clock())
    assert provider.refunds == [(TG, "ch_1")]
    assert (await sub_of(db, user)).premium_expires_at == clock()
    with pytest.raises(RefundError):
        async with db.transaction() as s:
            await refund_payment(s, provider, p.id, clock())


async def test_expiry_downgrades_and_pauses_extra_chats(db, settings, clock, cipher):
    user, acc = await seed_tenant(db, cipher, TG, premium_until=clock() + timedelta(days=8))
    chats = [
        await seed_chat(db, user, acc, -100 - i, f"צ'אט {i}", created_at=clock() + timedelta(minutes=i))
        for i in range(6)
    ]
    async with db.transaction() as s:
        await chat_svc.set_keep_on_free(s, user.id, chats[5].id, True, settings)
    notifier = FakeDelivery()

    # reminders: 7, 3, 1 days — each once
    for advance_days, expected_new in [(1.5, 1), (0, 0), (4, 1), (2, 1), (0, 0)]:
        clock.advance(days=advance_days)
        before = len(notifier.texts)
        await tasks.subscription_lifecycle(db, notifier, settings, clock())
        assert len(notifier.texts) - before == expected_new
    assert "יום אחד" in notifier.texts[-1][1]

    clock.advance(days=1)
    await tasks.subscription_lifecycle(db, notifier, settings, clock())
    sub = await sub_of(db, user)
    assert sub.tier == Tier.FREE
    async with db.session() as s:
        rows = {c.title: c.status for c in (await s.scalars(select(MonitoredChat))).all()}
    active = sorted(t for t, st in rows.items() if st == ChatStatus.ACTIVE)
    assert active == ["צ'אט 0", "צ'אט 1", "צ'אט 5"]  # user-selected first, then oldest
    assert sum(1 for st in rows.values() if st == ChatStatus.PAUSED_TIER) == 3
    assert len(rows) == 6  # nothing deleted
    msg = notifier.texts[-1][1]
    assert "הושהו" in msg and "צ'אט 2" in msg and "שום קובץ לא נמחק" in msg


async def test_chat_limits_enforced_on_add_and_resume(db, settings, clock, cipher):
    user, acc = await seed_tenant(db, cipher, TG)

    def vc(i):
        return chat_svc.VerifiedChat(i, 1, ChatType.GROUP, f"g{i}", 10)

    async with db.transaction() as s:
        for i in range(3):
            await chat_svc.add_chat(s, user.id, vc(-i - 1), clock(), settings)
    with pytest.raises(chat_svc.ChatLimitReached):
        async with db.transaction() as s:
            await chat_svc.add_chat(s, user.id, vc(-99), clock(), settings)
    with pytest.raises(chat_svc.ChatAlreadyMonitored):
        async with db.transaction() as s:
            await chat_svc.add_chat(s, user.id, vc(-1), clock(), settings)
    async with db.transaction() as s:
        first = (await chat_svc.list_chats(s, user.id))[0]
        await chat_svc.pause_chat(s, user.id, first.id)
        await chat_svc.add_chat(s, user.id, vc(-99), clock(), settings)
    with pytest.raises(chat_svc.ChatLimitReached):
        async with db.transaction() as s:
            await chat_svc.resume_chat(s, user.id, first.id, clock(), settings)


async def test_new_chat_starts_from_current_message(db, settings, clock, cipher, queue):
    """Historical media is out of scope: last_seen is initialised to the chat's latest message."""
    user, acc = await seed_tenant(db, cipher, TG)
    async with db.transaction() as s:
        c = await chat_svc.add_chat(
            s, user.id, chat_svc.VerifiedChat(-7, 1, ChatType.GROUP, "g", 500), clock(), settings
        )
    assert c.last_seen_message_id == 500


async def test_disconnect_stops_monitoring_immediately(db, settings, clock, cipher, queue):
    user, acc = await seed_tenant(db, cipher, TG)
    chat = await seed_chat(db, user, acc, -100777)
    ctx = AccountContext(acc.id, user.id, 0, BOT_ID)
    ev = IncomingMedia(chat_id=-100777, message_id=1, date=None, photo=PhotoInfo(1, 10))
    assert await ingest(db, queue, ctx, ev, clock(), settings) == IngestOutcome.QUEUED
    async with db.transaction() as s:
        await disconnect_account(s, user.id, clock())
    async with db.session() as s:
        assert (await s.get(TelegramAccount, acc.id)).status == AccountStatus.DISCONNECTED  # type: ignore[union-attr]
        assert (await s.get(MonitoredChat, chat.id)).status == ChatStatus.PAUSED_ACCOUNT  # type: ignore[union-attr]
        job = await s.scalar(select(MediaJob))
    assert job is not None and job.state == JobState.SKIPPED and job.skip_reason == "account_inactive"
    ev2 = IncomingMedia(chat_id=-100777, message_id=2, date=None, photo=PhotoInfo(2, 10))
    assert await ingest(db, queue, ctx, ev2, clock(), settings) == IngestOutcome.IGNORED


async def test_disconnected_session_purged_by_scheduler_fallback(db, settings, clock, cipher):
    user, acc = await seed_tenant(db, cipher, TG)
    async with db.transaction() as s:
        await disconnect_account(s, user.id, clock())
    await tasks.retention(db, clock())
    async with db.session() as s:
        assert await s.scalar(select(EncryptedSession)) is not None  # engine gets a grace period to log out
    clock.advance(minutes=11)
    counts = await tasks.retention(db, clock())
    assert counts["sessions_purged"] == 1
    async with db.session() as s:
        assert await s.scalar(select(EncryptedSession)) is None


async def test_issue_digest_aggregates(db, settings, clock, cipher, queue):
    user, acc = await seed_tenant(db, cipher, TG)
    await seed_chat(db, user, acc, -1008)
    ctx = AccountContext(acc.id, user.id, 0, BOT_ID)
    for i in range(5):
        ev = IncomingMedia(
            chat_id=-1008, message_id=i + 1, date=None, photo=PhotoInfo(i + 1, 50 * 1024 * 1024)
        )
        await ingest(db, queue, ctx, ev, clock(), settings)
    notifier = FakeDelivery()
    assert await tasks.issue_digests(db, notifier, settings, clock()) == 1
    assert len(notifier.texts) == 1 and "5" in notifier.texts[0][1]
    assert await tasks.issue_digests(db, notifier, settings, clock()) == 0


async def test_retention_removes_old_job_metadata_only(db, settings, clock, cipher, queue):
    user, acc = await seed_tenant(db, cipher, TG)
    chat = await seed_chat(db, user, acc, -1009)
    ctx = AccountContext(acc.id, user.id, 0, BOT_ID)
    await ingest(db, queue, ctx, IncomingMedia(-1009, 1, None, photo=PhotoInfo(1, 10**9)), clock(), settings)
    clock.advance(days=91)
    counts = await tasks.retention(db, clock())
    assert counts["jobs_deleted"] == 1
    async with db.session() as s:
        assert (await s.get(MonitoredChat, chat.id)).skipped_count == 1  # type: ignore[union-attr]
