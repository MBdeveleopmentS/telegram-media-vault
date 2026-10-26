"""Chat selection: by forwarded message or from the account's dialog list.

Callback data and forwarded metadata are only *hints*. Before showing the
confirmation and again before saving, the chat is re-verified by the engine
against the connected account's own dialogs (``resolve_dialog``).
"""

from __future__ import annotations

import math
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
)

from vault import i18n
from vault.bot import keyboards as kb
from vault.bot.deps import Deps
from vault.bot.handlers.menu import respond
from vault.db.models import ChatType
from vault.domain import chats as chat_svc
from vault.domain.plans import limits_for
from vault.domain.subscriptions import current_tier
from vault.engine.rpc import RpcError
from vault.security import events

router = Router(name="add_chat")
PAGE_SIZE = 8


class AddChat(StatesGroup):
    searching = State()


async def _account_shard(deps: Deps, user_id: int) -> int | None:
    async with deps.db.session() as session:
        acc = await chat_svc.active_account(session, user_id)
        return acc.shard if acc else None


async def _limit_ok(deps: Deps, user_id: int) -> tuple[bool, int]:
    async with deps.db.session() as session:
        tier = await current_tier(session, user_id, deps.clock())
        limit = limits_for(tier, deps.settings).max_chats
        return await chat_svc.count_active(session, user_id) < limit, limit


async def _rpc(deps: Deps, user_id: int, method: str, **params: Any) -> Any:
    shard = await _account_shard(deps, user_id)
    if shard is None:
        raise RpcError("no_account")
    return await deps.rpc.call(shard, method, user_id, **params)


@router.message(Command("add"))
@router.callback_query(kb.MenuCB.filter(F.a == "add"))
async def add_intro(event: Message | CallbackQuery, deps: Deps, db_user_id: int, state: FSMContext) -> None:
    await state.clear()
    if await _account_shard(deps, db_user_id) is None:
        await respond(event, i18n.NOT_CONNECTED, kb.main_menu(False))
        return
    ok, limit = await _limit_ok(deps, db_user_id)
    if not ok:
        await respond(
            event,
            i18n.ADD_LIMIT_REACHED.format(limit=limit, premium_limit=deps.settings.premium_max_chats),
            kb.back_home(),
        )
        return
    await respond(event, i18n.ADD_CHAT_INTRO, kb.add_chat_intro())


async def _dialogs(deps: Deps, user_id: int) -> list[dict[str, Any]]:
    cached = deps.dialogs.get(user_id)
    if cached is not None:
        return cached
    dialogs = await _rpc(deps, user_id, "list_dialogs")
    deps.dialogs.put(user_id, dialogs)
    return list(dialogs)


async def _show_page(
    event: Message | CallbackQuery, deps: Deps, user_id: int, page: int, query: str | None
) -> None:
    try:
        dialogs = await _dialogs(deps, user_id)
    except RpcError as e:
        await respond(
            event, i18n.NOT_CONNECTED if e.code == "no_account" else i18n.ENGINE_UNAVAILABLE, kb.back_home()
        )
        return
    if query:
        q = query.casefold()
        dialogs = [d for d in dialogs if q in str(d["title"]).casefold()]
    if not dialogs:
        await respond(event, i18n.DIALOGS_EMPTY, kb.add_chat_intro())
        return
    pages = max(1, math.ceil(len(dialogs) / PAGE_SIZE))
    page = min(max(0, page), pages - 1)
    chunk = dialogs[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    items = [(int(d["chat_id"]), ("🔒 " if d["protected"] else "") + str(d["title"])) for d in chunk]
    header = i18n.DIALOGS_TITLE.format(page=page + 1, pages=pages, query=f' · "{query}"' if query else "")
    await respond(event, header, kb.dialogs_page(items, page, pages))


@router.callback_query(kb.DialogCB.filter(F.a == "page"))
async def dialogs_page(
    cq: CallbackQuery, callback_data: kb.DialogCB, deps: Deps, db_user_id: int, state: FSMContext
) -> None:
    data = await state.get_data()
    await _show_page(cq, deps, db_user_id, callback_data.p, data.get("query"))


@router.callback_query(kb.DialogCB.filter(F.a == "search"))
async def dialogs_search(cq: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddChat.searching)
    await respond(cq, i18n.DIALOGS_SEARCH_PROMPT)


@router.message(StateFilter(AddChat.searching), F.text)
async def dialogs_search_query(message: Message, deps: Deps, db_user_id: int, state: FSMContext) -> None:
    query = (message.text or "").strip()[:64]
    await state.set_state(None)
    await state.update_data(query=query)
    await _show_page(message, deps, db_user_id, 0, query)


async def _confirm(
    event: Message | CallbackQuery,
    deps: Deps,
    user_id: int,
    chat_id: int,
    from_forward: bool,
    not_found_text: str = i18n.ADD_NOT_FOUND,
) -> None:
    try:
        d = await _rpc(deps, user_id, "resolve_dialog", chat_id=chat_id)
    except RpcError as e:
        await respond(
            event, i18n.NOT_CONNECTED if e.code == "no_account" else i18n.ENGINE_UNAVAILABLE, kb.back_home()
        )
        return
    if d is None:
        async with deps.db.transaction() as session:
            await events.record(session, "chat_verification_failed", severity="info", user_id=user_id)
        await respond(event, not_found_text, kb.add_chat_intro())
        return
    if d["protected"]:
        await respond(event, i18n.ADD_PROTECTED, kb.add_chat_intro())
        return
    text = i18n.ADD_CONFIRM.format(title=d["title"], ctype=i18n.CHAT_TYPE_LABELS.get(d["chat_type"], ""))
    await respond(event, text, kb.confirm_add(int(d["chat_id"]), from_forward))


@router.callback_query(kb.DialogCB.filter(F.a == "pick"))
async def dialog_pick(cq: CallbackQuery, callback_data: kb.DialogCB, deps: Deps, db_user_id: int) -> None:
    await _confirm(cq, deps, db_user_id, callback_data.cid, from_forward=False)


@router.callback_query(kb.DialogCB.filter(F.a == "ok"))
async def dialog_ok(
    cq: CallbackQuery, callback_data: kb.DialogCB, deps: Deps, db_user_id: int, state: FSMContext
) -> None:
    try:
        d = await _rpc(deps, db_user_id, "resolve_dialog", chat_id=callback_data.cid)
    except RpcError:
        await respond(cq, i18n.ENGINE_UNAVAILABLE, kb.back_home())
        return
    if d is None or d["protected"]:
        await respond(cq, i18n.ADD_PROTECTED if d else i18n.ADD_NOT_FOUND, kb.add_chat_intro())
        return
    verified = chat_svc.VerifiedChat(
        chat_id=int(d["chat_id"]),
        access_hash=d["access_hash"],
        chat_type=ChatType(d["chat_type"]),
        title=str(d["title"]),
        last_message_id=int(d["last_message_id"]),
    )
    try:
        async with deps.db.transaction() as session:
            chat = await chat_svc.add_chat(session, db_user_id, verified, deps.clock(), deps.settings)
            await events.record(session, "chat_added", user_id=db_user_id, details={"chat_ref": chat.id})
            shard = (await chat_svc.active_account(session, db_user_id)).shard  # type: ignore[union-attr]
    except chat_svc.ChatAlreadyMonitored:
        await respond(cq, i18n.ADD_ALREADY, kb.back_home())
        return
    except chat_svc.ChatLimitReached:
        _, limit = await _limit_ok(deps, db_user_id)
        await respond(
            cq,
            i18n.ADD_LIMIT_REACHED.format(limit=limit, premium_limit=deps.settings.premium_max_chats),
            kb.back_home(),
        )
        return
    except chat_svc.NoActiveAccount:
        await respond(cq, i18n.NOT_CONNECTED, kb.main_menu(False))
        return
    await state.clear()
    await deps.publish_control("chats_changed", shard)
    await respond(cq, i18n.ADD_DONE.format(title=verified.title), kb.main_menu(True))


@router.message(F.forward_origin)
async def forwarded(message: Message, deps: Deps, db_user_id: int) -> None:
    if await _account_shard(deps, db_user_id) is None:
        await message.answer(i18n.NOT_CONNECTED, reply_markup=kb.main_menu(False))
        return
    origin = message.forward_origin
    candidate: int | None = None
    if isinstance(origin, MessageOriginChannel):
        candidate = origin.chat.id
    elif isinstance(origin, MessageOriginChat):
        candidate = origin.sender_chat.id
    elif isinstance(origin, MessageOriginUser):
        # Could be the private chat with this user, or a group message by them.
        # We propose the private chat and offer the list as an alternative; never guess silently.
        candidate = origin.sender_user.id
    elif isinstance(origin, MessageOriginHiddenUser):
        await message.answer(i18n.ADD_FORWARD_HIDDEN, reply_markup=kb.add_chat_intro())
        return
    if candidate is None:
        await message.answer(i18n.ADD_FORWARD_HIDDEN, reply_markup=kb.add_chat_intro())
        return
    not_found = i18n.ADD_FORWARD_GROUP if isinstance(origin, MessageOriginUser) else i18n.ADD_NOT_FOUND
    await _confirm(message, deps, db_user_id, candidate, from_forward=True, not_found_text=not_found)
