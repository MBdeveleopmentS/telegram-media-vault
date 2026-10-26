"""Main menu, account connection, chat management, status, privacy and help."""

from __future__ import annotations

import contextlib
import re
from urllib.parse import urlparse

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from vault import i18n
from vault.authweb.flows import FlowRateLimited, create_flow
from vault.bot import keyboards as kb
from vault.bot.deps import Deps
from vault.bot.views import chat_details, status_text
from vault.db.models import ChatStatus, Tier
from vault.domain import chats as chat_svc
from vault.domain.accounts import disconnect_account
from vault.domain.plans import limits_for
from vault.domain.subscriptions import current_tier
from vault.security import events

router = Router(name="menu")
LOGIN_CODE_RE = re.compile(r"^\s*\d(?:[\s.\-]?\d){4,6}\s*$")


async def respond(
    target: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    if isinstance(target, CallbackQuery):
        await target.answer()
        msg = target.message
        if isinstance(msg, Message):
            try:
                await msg.edit_text(text, reply_markup=markup)
                return
            except TelegramBadRequest:
                pass
            await msg.answer(text, reply_markup=markup)
        return
    await target.answer(text, reply_markup=markup)


async def is_connected(deps: Deps, user_id: int) -> bool:
    async with deps.db.session() as session:
        return await chat_svc.active_account(session, user_id) is not None


# ---------------------------------------------------------------- start / menu
@router.message(CommandStart())
async def cmd_start(message: Message, deps: Deps, db_user_id: int) -> None:
    connected = await is_connected(deps, db_user_id)
    await message.answer(i18n.WELCOME, reply_markup=kb.main_menu(connected))


@router.callback_query(kb.MenuCB.filter(F.a == "home"))
async def cb_home(cq: CallbackQuery, deps: Deps, db_user_id: int) -> None:
    await respond(cq, i18n.MAIN_MENU, kb.main_menu(await is_connected(deps, db_user_id)))


@router.message(Command("help"))
@router.callback_query(kb.MenuCB.filter(F.a == "help"))
async def show_help(event: Message | CallbackQuery) -> None:
    await respond(event, i18n.HELP, kb.back_home())


@router.message(Command("privacy"))
@router.callback_query(kb.MenuCB.filter(F.a == "privacy"))
async def show_privacy(event: Message | CallbackQuery) -> None:
    await respond(event, i18n.PRIVACY, kb.privacy_menu())


@router.message(Command("source"))
@router.callback_query(kb.MenuCB.filter(F.a == "source"))
async def show_source(event: Message | CallbackQuery, deps: Deps) -> None:
    await respond(event, i18n.SOURCE.format(url=deps.settings.source_code_url), kb.back_home())


@router.message(Command("terms"))
@router.callback_query(kb.MenuCB.filter(F.a == "terms"))
async def show_terms(event: Message | CallbackQuery, deps: Deps) -> None:
    s = deps.settings
    await respond(
        event, i18n.TERMS.format(days=s.premium_period_days, support=s.support_contact), kb.back_home()
    )


@router.message(Command("paysupport"))
async def cmd_paysupport(message: Message, deps: Deps) -> None:
    await message.answer(i18n.PAYSUPPORT.format(support=deps.settings.support_contact))


@router.message(Command("support"))
async def cmd_support(message: Message, deps: Deps) -> None:
    await message.answer(i18n.SUPPORT.format(support=deps.settings.support_contact))


@router.callback_query(kb.MenuCB.filter(F.a == "settings"))
async def show_settings(cq: CallbackQuery, deps: Deps, db_user_id: int) -> None:
    await respond(cq, i18n.SETTINGS, kb.settings_menu(await is_connected(deps, db_user_id)))


# ---------------------------------------------------------------- connect
def _login_markup_or_text(url: str, intro: str) -> tuple[str, InlineKeyboardMarkup | None]:
    host = urlparse(url).hostname or ""
    # Telegram rejects non-public / non-HTTPS URLs in buttons; dev mode shows the link as text.
    if url.startswith("https://") and host not in {"localhost", "127.0.0.1"}:
        return intro, kb.login_link(url)
    return f"{intro}\n\n(מצב פיתוח) {url}", None


async def connect_via_link(event: Message | CallbackQuery, deps: Deps, db_user_id: int) -> None:
    """Alternative connection flow through the HTTPS auth page (needs a public https URL).
    Not registered by default; the in-chat flow lives in ``handlers/login.py``."""
    s = deps.settings
    if await is_connected(deps, db_user_id):
        await respond(event, i18n.ALREADY_CONNECTED, kb.back_home())
        return
    try:
        async with deps.db.transaction() as session:
            flow, token = await create_flow(
                session,
                deps.hasher,
                db_user_id,
                deps.clock(),
                s.auth_flow_ttl_seconds,
                s.auth_max_flows_per_user_per_hour,
            )
            await events.record(
                session, "auth_flow_created", user_id=db_user_id, details={"flow_id": flow.id}
            )
    except FlowRateLimited:
        await respond(event, i18n.CONNECT_RATE_LIMITED, kb.back_home())
        return
    url = f"{s.public_base_url}/auth/{token}"
    text, markup = _login_markup_or_text(
        url, i18n.CONNECT_INTRO.format(minutes=s.auth_flow_ttl_seconds // 60)
    )
    if isinstance(event, CallbackQuery):
        await event.answer()
        target = event.message
        if not isinstance(target, Message):
            return
    else:
        target = event
    # protect_content: the personal one-time link can't be forwarded or saved.
    await target.answer(text, reply_markup=markup, protect_content=True, disable_web_page_preview=True)


# ---------------------------------------------------------------- disconnect
@router.message(Command("disconnect"))
@router.callback_query(kb.MenuCB.filter(F.a == "disconnect"))
async def disconnect(event: Message | CallbackQuery, deps: Deps, db_user_id: int) -> None:
    if not await is_connected(deps, db_user_id):
        await respond(event, i18n.NOT_CONNECTED_SHORT, kb.back_home())
        return
    await respond(event, i18n.DISCONNECT_CONFIRM, kb.confirm_disconnect())


@router.callback_query(kb.MenuCB.filter(F.a == "disconnect_ok"))
async def disconnect_ok(cq: CallbackQuery, deps: Deps, db_user_id: int) -> None:
    async with deps.db.transaction() as session:
        acc = await disconnect_account(session, db_user_id, deps.clock())
        shard = acc.shard if acc else None
        if acc is not None:
            await events.record(
                session, "account_disconnected", user_id=db_user_id, details={"account_id": acc.id}
            )
    if acc is not None:
        await deps.publish_control("account_disconnected", shard)
    deps.dialogs.clear(db_user_id)
    await respond(cq, i18n.DISCONNECTED, kb.main_menu(False))


# ---------------------------------------------------------------- status
@router.message(Command("status"))
@router.callback_query(kb.MenuCB.filter(F.a == "status"))
async def show_status(event: Message | CallbackQuery, deps: Deps, db_user_id: int) -> None:
    async with deps.db.session() as session:
        text = await status_text(session, db_user_id, deps.settings, deps.clock())
    await respond(event, text, kb.back_home())


# ---------------------------------------------------------------- chats
@router.message(Command("chats"))
@router.callback_query(kb.MenuCB.filter(F.a == "chats"))
async def show_chats(event: Message | CallbackQuery, deps: Deps, db_user_id: int) -> None:
    now = deps.clock()
    async with deps.db.session() as session:
        chats = await chat_svc.list_chats(session, db_user_id)
        tier = await current_tier(session, db_user_id, now)
    if not chats:
        await respond(event, i18n.CHATS_EMPTY, kb.add_chat_intro())
        return
    header = i18n.CHATS_HEADER.format(
        active=sum(1 for c in chats if c.status == ChatStatus.ACTIVE),
        limit=limits_for(tier, deps.settings).max_chats,
        tier=i18n.TIER_LABELS[tier.value],
    )
    await respond(event, header, kb.chats_list(chats))


@router.callback_query(kb.ChatCB.filter())
async def chat_action(cq: CallbackQuery, callback_data: kb.ChatCB, deps: Deps, db_user_id: int) -> None:
    s, now, a = deps.settings, deps.clock(), callback_data.a
    note = ""
    try:
        async with deps.db.transaction() as session:
            # get_owned_chat enforces chat.user_id == db_user_id (IDOR protection).
            if a == "pause":
                await chat_svc.pause_chat(session, db_user_id, callback_data.id)
                note = i18n.CHAT_PAUSED
            elif a == "resume":
                await chat_svc.resume_chat(session, db_user_id, callback_data.id, now, s)
                note = i18n.CHAT_RESUMED
            elif a == "remove":
                chat = await chat_svc.get_owned_chat(session, db_user_id, callback_data.id)
                await respond(
                    cq, i18n.CHAT_REMOVE_CONFIRM.format(title=chat.title), kb.confirm_remove(chat.id)
                )
                return
            elif a == "remove_ok":
                await chat_svc.remove_chat(session, db_user_id, callback_data.id)
            elif a in ("keep", "unkeep"):
                await chat_svc.set_keep_on_free(session, db_user_id, callback_data.id, a == "keep", s)
            chat_row = (
                None
                if a == "remove_ok"
                else await chat_svc.get_owned_chat(session, db_user_id, callback_data.id)
            )
            tier = await current_tier(session, db_user_id, now)
            acc = await chat_svc.active_account(session, db_user_id)
            shard = acc.shard if acc else None
    except chat_svc.ChatNotFound:
        await events_idor(deps, db_user_id)
        await respond(cq, i18n.CHAT_NOT_FOUND, kb.back_home())
        return
    except chat_svc.ChatLimitReached:
        if a == "keep":
            await cq.answer(i18n.KEEP_LIMIT.format(limit=s.free_max_chats), show_alert=True)
        else:
            limit = limits_for(await _tier(deps, db_user_id), s).max_chats
            await cq.answer(
                i18n.ADD_LIMIT_REACHED.format(limit=limit, premium_limit=s.premium_max_chats)[:200],
                show_alert=True,
            )
        return
    except chat_svc.NoActiveAccount:
        await respond(cq, i18n.NOT_CONNECTED, kb.main_menu(False))
        return
    if a in ("pause", "resume", "remove_ok"):
        await deps.publish_control("chats_changed", shard)
    if chat_row is None:
        await show_chats(cq, deps, db_user_id)
        return
    text = chat_details(chat_row, tier, s)
    if note:
        text = f"{note}\n\n{text}"
    await respond(cq, text, kb.chat_actions(chat_row))


async def _tier(deps: Deps, db_user_id: int) -> Tier:
    async with deps.db.session() as session:
        return await current_tier(session, db_user_id, deps.clock())


async def events_idor(deps: Deps, db_user_id: int) -> None:
    async with deps.db.transaction() as session:
        await events.record(session, "chat_access_denied", severity="warning", user_id=db_user_id)


async def _pick(
    message: Message, deps: Deps, db_user_id: int, action: str, label: str, want_active: bool | None
) -> None:
    async with deps.db.session() as session:
        chats = await chat_svc.list_chats(session, db_user_id)
    if want_active is not None:
        chats = [c for c in chats if (c.status == ChatStatus.ACTIVE) == want_active]
    if not chats:
        await message.answer(i18n.NOTHING_TO_DO, reply_markup=kb.back_home())
        return
    await message.answer(i18n.CHOOSE_CHAT_FOR.format(action=label), reply_markup=kb.pick_chats(chats, action))


@router.message(Command("pause"))
async def cmd_pause(message: Message, deps: Deps, db_user_id: int) -> None:
    await _pick(message, deps, db_user_id, "pause", i18n.ACTION_PAUSE, True)


@router.message(Command("resume"))
async def cmd_resume(message: Message, deps: Deps, db_user_id: int) -> None:
    await _pick(message, deps, db_user_id, "resume", i18n.ACTION_RESUME, False)


@router.message(Command("remove"))
async def cmd_remove(message: Message, deps: Deps, db_user_id: int) -> None:
    await _pick(message, deps, db_user_id, "remove", i18n.ACTION_REMOVE, None)


# ---------------------------------------------------------------- safety net
@router.message(F.text.regexp(LOGIN_CODE_RE))
async def looks_like_login_code(message: Message) -> None:
    """Users sometimes paste their login code into the chat. Delete it and warn."""
    with contextlib.suppress(TelegramBadRequest):
        await message.delete()
    await message.answer(i18n.CODE_IN_CHAT_WARNING)
