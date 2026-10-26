from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery

from vault import i18n
from vault.bot import keyboards as kb
from vault.bot.deps import Deps
from vault.bot.handlers.menu import respond
from vault.db.models import Tier
from vault.domain.plans import effective_tier
from vault.domain.subscriptions import get_subscription
from vault.logging import get_logger
from vault.payments.service import Fulfilment, apply_successful_payment, create_invoice, validate_pre_checkout

router = Router(name="payments")
log = get_logger(__name__)


@router.message(Command("premium"))
@router.callback_query(kb.MenuCB.filter(F.a == "premium"))
async def show_premium(event: Message | CallbackQuery, deps: Deps, db_user_id: int) -> None:
    s, now = deps.settings, deps.clock()
    async with deps.db.session() as session:
        sub = await get_subscription(session, db_user_id)
    common = dict(
        premium_chats=s.premium_max_chats,
        premium_mib=s.premium_max_file_mib,
        price=s.premium_price_stars,
        days=s.premium_period_days,
    )
    if effective_tier(sub, now) == Tier.PREMIUM and sub.premium_expires_at:
        days_left = max(0, (sub.premium_expires_at - now).days)
        text = i18n.PREMIUM_INFO_ACTIVE.format(
            days_left=days_left, until=i18n.fmt_date(sub.premium_expires_at), **common
        )
    else:
        text = i18n.PREMIUM_INFO_FREE.format(
            free_chats=s.free_max_chats, free_mib=s.free_max_file_mib, **common
        )
    await respond(event, text, kb.premium(s.premium_price_stars))


@router.callback_query(kb.PayCB.filter(F.a == "buy"))
async def buy(cq: CallbackQuery, deps: Deps, db_user_id: int) -> None:
    s = deps.settings
    await cq.answer()
    async with deps.db.transaction() as session:
        payment = await create_invoice(session, db_user_id, s, deps.clock())
    await deps.payments.send_invoice(
        cq.from_user.id,
        payment,
        i18n.INVOICE_TITLE.format(days=s.premium_period_days),
        i18n.INVOICE_DESCRIPTION.format(
            chats=s.premium_max_chats, mib=s.premium_max_file_mib, days=s.premium_period_days
        ),
    )


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery, deps: Deps) -> None:
    # Must be answered within 10 seconds.
    async with deps.db.transaction() as session:
        err = await validate_pre_checkout(
            session,
            from_tg_user_id=q.from_user.id,
            payload=q.invoice_payload,
            currency=q.currency,
            amount=q.total_amount,
            now=deps.clock(),
        )
    if err is None:
        await q.answer(ok=True)
    else:
        await q.answer(ok=False, error_message=i18n.PAYMENT_ERROR)


@router.message(F.successful_payment)
async def successful_payment(message: Message, deps: Deps) -> None:
    sp = message.successful_payment
    assert sp is not None and message.from_user is not None
    now = deps.clock()
    async with deps.db.transaction() as session:
        res = await apply_successful_payment(
            session,
            from_tg_user_id=message.from_user.id,
            payload=sp.invoice_payload,
            currency=sp.currency,
            amount=sp.total_amount,
            telegram_charge_id=sp.telegram_payment_charge_id,
            provider_charge_id=sp.provider_payment_charge_id,
            now=now,
        )
    if res.outcome == Fulfilment.APPLIED and res.until:
        await message.answer(
            i18n.PAYMENT_OK.format(until=i18n.fmt_date(res.until), days_left=(res.until - now).days),
            reply_markup=kb.main_menu(True),
        )
    elif res.outcome == Fulfilment.DUPLICATE and res.until:
        await message.answer(i18n.PAYMENT_DUPLICATE.format(until=i18n.fmt_date(res.until)))
    else:
        log.error("payment_rejected", error_code=res.error)
        await message.answer(i18n.PAYSUPPORT.format(support=deps.settings.support_contact))
