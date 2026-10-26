"""Telegram Stars (XTR) payments: invoices, validation, idempotent fulfilment, refunds.

Flow (https://core.telegram.org/bots/payments-stars):
  sendInvoice(currency="XTR", provider_token="") -> pre_checkout_query (answer < 10s)
  -> message.successful_payment (only now is Premium granted) -> store
  telegram_payment_charge_id (needed for refundStarPayment).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vault.config import Settings
from vault.db.models import Payment, PaymentStatus, User
from vault.domain.plans import PREMIUM_PRODUCT, premium_offer
from vault.domain.subscriptions import extend_premium, get_subscription, revoke_premium_period
from vault.security import events

CURRENCY = "XTR"
PAYLOAD_PREFIX = "pv1_"


class PaymentProvider(Protocol):
    async def send_invoice(self, chat_id: int, payment: Payment, title: str, description: str) -> None: ...

    async def refund(self, telegram_user_id: int, charge_id: str) -> None: ...


class StarsProvider:
    def __init__(self, bot: object) -> None:
        from aiogram import Bot

        assert isinstance(bot, Bot)
        self._bot = bot

    async def send_invoice(self, chat_id: int, payment: Payment, title: str, description: str) -> None:
        from aiogram.types import LabeledPrice

        await self._bot.send_invoice(
            chat_id=chat_id,
            title=title[:32],
            description=description[:255],
            payload=payment.invoice_payload,
            currency=CURRENCY,
            prices=[LabeledPrice(label=title[:32], amount=payment.amount)],
            provider_token="",
        )

    async def refund(self, telegram_user_id: int, charge_id: str) -> None:
        await self._bot.refund_star_payment(user_id=telegram_user_id, telegram_payment_charge_id=charge_id)


class FakePaymentProvider:
    """Local provider for tests and dry runs. Never contacts Telegram."""

    def __init__(self) -> None:
        self.invoices: list[tuple[int, str, int]] = []
        self.refunds: list[tuple[int, str]] = []

    async def send_invoice(self, chat_id: int, payment: Payment, title: str, description: str) -> None:
        self.invoices.append((chat_id, payment.invoice_payload, payment.amount))

    async def refund(self, telegram_user_id: int, charge_id: str) -> None:
        self.refunds.append((telegram_user_id, charge_id))


class Fulfilment(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


@dataclass(frozen=True)
class FulfilmentResult:
    outcome: Fulfilment
    until: datetime | None = None
    error: str | None = None


async def create_invoice(session: AsyncSession, user_id: int, s: Settings, now: datetime) -> Payment:
    offer = premium_offer(s)
    p = Payment(
        user_id=user_id,
        invoice_payload=PAYLOAD_PREFIX + secrets.token_hex(16),
        product=offer.product,
        currency=CURRENCY,
        amount=offer.price_stars,
        period_days=offer.period_days,
        status=PaymentStatus.PENDING,
        expires_at=now + timedelta(hours=s.invoice_ttl_hours),
        created_at=now,
    )
    session.add(p)
    await session.flush()
    return p


async def _load(session: AsyncSession, payload: str) -> tuple[Payment, User] | None:
    if not payload.startswith(PAYLOAD_PREFIX) or len(payload) > 64:
        return None
    row = (
        await session.execute(
            select(Payment, User)
            .join(User, User.id == Payment.user_id)
            .where(Payment.invoice_payload == payload)
            .with_for_update()
        )
    ).first()
    return (row[0], row[1]) if row else None


async def validate_pre_checkout(
    session: AsyncSession, *, from_tg_user_id: int, payload: str, currency: str, amount: int, now: datetime
) -> str | None:
    """Return None if the checkout may proceed, else a non-sensitive error code."""
    loaded = await _load(session, payload)
    if loaded is None:
        await events.record(session, "payment_unknown_payload", severity="warning")
        return "unknown_invoice"
    payment, user = loaded
    if user.telegram_user_id != from_tg_user_id:
        await events.record(
            session,
            "payment_user_mismatch",
            severity="high",
            user_id=user.id,
            details={"payment_id": payment.id},
        )
        return "user_mismatch"
    if currency != CURRENCY or amount != payment.amount or payment.product != PREMIUM_PRODUCT:
        await events.record(
            session,
            "payment_amount_mismatch",
            severity="high",
            user_id=user.id,
            details={"payment_id": payment.id, "amount": amount, "currency": currency},
        )
        return "amount_mismatch"
    if payment.status != PaymentStatus.PENDING:
        return "not_pending"
    if payment.expires_at <= now:
        payment.status = PaymentStatus.EXPIRED
        return "expired"
    return None


async def apply_successful_payment(
    session: AsyncSession,
    *,
    from_tg_user_id: int,
    payload: str,
    currency: str,
    amount: int,
    telegram_charge_id: str,
    provider_charge_id: str | None,
    now: datetime,
) -> FulfilmentResult:
    """Idempotent: the unique telegram_payment_charge_id and the PENDING->PAID
    transition guarantee a replayed update never extends Premium twice."""
    if not telegram_charge_id or len(telegram_charge_id) > 128:
        return FulfilmentResult(Fulfilment.REJECTED, error="bad_charge_id")
    existing = await session.scalar(
        select(Payment).where(Payment.telegram_payment_charge_id == telegram_charge_id)
    )
    if existing is not None:
        await events.record(
            session,
            "payment_replay",
            severity="warning",
            user_id=existing.user_id,
            details={"payment_id": existing.id},
        )
        sub = await get_subscription(session, existing.user_id)
        return FulfilmentResult(Fulfilment.DUPLICATE, until=sub.premium_expires_at)
    loaded = await _load(session, payload)
    if loaded is None:
        await events.record(session, "payment_unknown_payload", severity="high")
        return FulfilmentResult(Fulfilment.REJECTED, error="unknown_invoice")
    payment, user = loaded
    if user.telegram_user_id != from_tg_user_id or currency != CURRENCY or amount != payment.amount:
        await events.record(
            session,
            "payment_fulfilment_mismatch",
            severity="critical",
            user_id=user.id,
            details={"payment_id": payment.id, "amount": amount, "currency": currency},
        )
        return FulfilmentResult(Fulfilment.REJECTED, error="mismatch")
    if payment.status == PaymentStatus.PAID:
        sub = await get_subscription(session, user.id)
        return FulfilmentResult(Fulfilment.DUPLICATE, until=sub.premium_expires_at)
    if payment.status not in (PaymentStatus.PENDING, PaymentStatus.EXPIRED):
        # A real charge for a refunded/odd invoice: never silently grant; flag for support.
        await events.record(
            session,
            "payment_unexpected_state",
            severity="high",
            user_id=user.id,
            details={"payment_id": payment.id},
        )
        return FulfilmentResult(Fulfilment.REJECTED, error="bad_state")
    # Telegram has charged the user (an EXPIRED invoice still passed pre-checkout
    # moments ago), so we honour it.
    sub = await get_subscription(session, user.id)
    start, until = extend_premium(sub, payment.period_days, now)
    payment.status = PaymentStatus.PAID
    payment.telegram_payment_charge_id = telegram_charge_id
    payment.provider_payment_charge_id = (provider_charge_id or "")[:128] or None
    payment.paid_at = now
    payment.entitlement_from = start
    payment.entitlement_until = until
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        return FulfilmentResult(Fulfilment.DUPLICATE)
    await events.record(session, "payment_applied", user_id=user.id, details={"payment_id": payment.id})
    return FulfilmentResult(Fulfilment.APPLIED, until=until)


class RefundError(Exception):
    pass


async def refund_payment(
    session: AsyncSession, provider: PaymentProvider, payment_id: int, now: datetime
) -> Payment:
    payment = await session.get(Payment, payment_id, with_for_update=True)
    if payment is None or payment.status != PaymentStatus.PAID or not payment.telegram_payment_charge_id:
        raise RefundError("payment not refundable")
    user = await session.get(User, payment.user_id)
    assert user is not None
    await provider.refund(user.telegram_user_id, payment.telegram_payment_charge_id)
    payment.status = PaymentStatus.REFUNDED
    payment.refunded_at = now
    sub = await get_subscription(session, user.id)
    revoke_premium_period(sub, payment.period_days, now)
    await events.record(
        session, "payment_refunded", severity="info", user_id=user.id, details={"payment_id": payment.id}
    )
    return payment
