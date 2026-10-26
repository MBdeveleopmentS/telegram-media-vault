"""Connect a Telegram account without leaving the bot.

1. Phone: the user taps a "share my phone" button (request_contact). We accept
   only a contact whose user_id equals the sender, so nobody can start a login
   for someone else's number.
2. Code: entered on an inline keypad. It never becomes a chat message
   (Telegram invalidates login codes that are sent in chats).
3. 2FA password (only if required): sent as a message, deleted immediately.

The Telethon login itself runs in the auth-web process (ChatLoginService);
the bot never holds API_HASH or the session encryption key.
"""

from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from vault import i18n
from vault.authweb.chat_login import AuthRpcClient, AuthRpcError
from vault.bot import keyboards as kb
from vault.bot.deps import Deps
from vault.bot.handlers.menu import is_connected, respond

router = Router(name="login")
MAX_CODE_LEN = 8


class Login(StatesGroup):
    contact = State()
    code = State()
    password = State()


class KeyCB(CallbackData, prefix="k"):
    a: str  # d (digit) | del | ok | cancel
    v: int = 0


def keypad() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for d in (1, 2, 3, 4, 5, 6, 7, 8, 9):
        b.button(text=str(d), callback_data=KeyCB(a="d", v=d))
    b.button(text=i18n.BTN_KEY_DEL, callback_data=KeyCB(a="del"))
    b.button(text="0", callback_data=KeyCB(a="d", v=0))
    b.button(text=i18n.BTN_KEY_OK, callback_data=KeyCB(a="ok"))
    b.button(text=i18n.BTN_LOGIN_CANCEL, callback_data=KeyCB(a="cancel"))
    b.adjust(3, 3, 3, 3, 1)
    return b.as_markup()


def phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=i18n.BTN_SHARE_PHONE, request_contact=True)],
            [KeyboardButton(text=i18n.BTN_LOGIN_CANCEL)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def code_display(code: str) -> str:
    # Show progress without echoing the digits back into the chat.
    return ("●" * len(code)) or "—"


def error_text(e: AuthRpcError) -> str:
    template = i18n.LOGIN_ERRORS.get(e.code, i18n.LOGIN_GENERIC_ERROR)
    return template.format(minutes=max(1, e.seconds // 60 + 1))


def _rpc(deps: Deps) -> AuthRpcClient:
    return AuthRpcClient(deps.redis)


@router.message(Command("connect"))
@router.callback_query(kb.MenuCB.filter(F.a == "connect"))
async def start_login(event: Message | CallbackQuery, deps: Deps, db_user_id: int, state: FSMContext) -> None:
    if await is_connected(deps, db_user_id):
        await respond(event, i18n.ALREADY_CONNECTED, kb.back_home())
        return
    await state.clear()
    await state.set_state(Login.contact)
    if isinstance(event, CallbackQuery):
        await event.answer()
        target = event.message
        if not isinstance(target, Message):
            return
    else:
        target = event
    await target.answer(i18n.LOGIN_SHARE_PHONE, reply_markup=phone_keyboard())


@router.message(StateFilter(Login.contact, Login.code, Login.password), F.text == i18n.BTN_LOGIN_CANCEL)
async def cancel_by_text(message: Message, deps: Deps, db_user_id: int, state: FSMContext) -> None:
    await _cancel(deps, db_user_id, state)
    await message.answer(i18n.LOGIN_CANCELLED, reply_markup=ReplyKeyboardRemove())


async def _cancel(deps: Deps, db_user_id: int, state: FSMContext) -> None:
    await state.clear()
    with contextlib.suppress(AuthRpcError):
        await _rpc(deps).call("cancel", db_user_id, wait_seconds=5)


@router.message(StateFilter(Login.contact), F.contact)
async def got_contact(message: Message, deps: Deps, db_user_id: int, state: FSMContext) -> None:
    contact = message.contact
    assert contact is not None and message.from_user is not None
    if contact.user_id != message.from_user.id:
        await message.answer(i18n.LOGIN_NOT_OWN_CONTACT, reply_markup=phone_keyboard())
        return
    await message.answer(i18n.LOGIN_SENDING, reply_markup=ReplyKeyboardRemove())
    try:
        await _rpc(deps).call("send_code", db_user_id, phone=contact.phone_number)
    except AuthRpcError as e:
        await state.clear()
        await message.answer(error_text(e), reply_markup=kb.main_menu(False))
        return
    await state.set_state(Login.code)
    await state.update_data(code="")
    await message.answer(i18n.LOGIN_ENTER_CODE.format(display=code_display("")), reply_markup=keypad())


@router.message(StateFilter(Login.contact))
async def contact_expected(message: Message) -> None:
    await message.answer(i18n.LOGIN_NOT_OWN_CONTACT, reply_markup=phone_keyboard())


@router.callback_query(KeyCB.filter())
async def keypad_press(
    cq: CallbackQuery, callback_data: KeyCB, deps: Deps, db_user_id: int, state: FSMContext
) -> None:
    if await state.get_state() != Login.code.state:
        await cq.answer()
        return
    code = str((await state.get_data()).get("code", ""))
    if callback_data.a == "cancel":
        await _cancel(deps, db_user_id, state)
        await respond(cq, i18n.LOGIN_CANCELLED, kb.main_menu(False))
        return
    if callback_data.a == "d" and len(code) < MAX_CODE_LEN:
        code += str(callback_data.v % 10)
    elif callback_data.a == "del":
        code = code[:-1]
    elif callback_data.a == "ok":
        await state.update_data(code="")
        await _submit_code(cq, deps, db_user_id, state, code)
        return
    await state.update_data(code=code)
    await cq.answer()
    if isinstance(cq.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await cq.message.edit_text(
                i18n.LOGIN_ENTER_CODE.format(display=code_display(code)), reply_markup=keypad()
            )


@router.message(StateFilter(Login.code), F.text)
async def code_as_text(message: Message, deps: Deps, db_user_id: int, state: FSMContext) -> None:
    """Fallback if the user types the code anyway (e.g. with spaces). Delete it right away."""
    with contextlib.suppress(TelegramBadRequest):
        await message.delete()
    await _submit_code(message, deps, db_user_id, state, message.text or "")


async def _submit_code(
    event: Message | CallbackQuery, deps: Deps, db_user_id: int, state: FSMContext, code: str
) -> None:
    try:
        result = await _rpc(deps).call("code", db_user_id, code=code)
    except AuthRpcError as e:
        if e.code in ("invalid_code", "flood_wait"):
            await respond(
                event,
                f"{error_text(e)}\n\n" + i18n.LOGIN_ENTER_CODE.format(display=code_display("")),
                keypad(),
            )
            return
        await state.clear()
        await respond(event, error_text(e), kb.main_menu(False))
        return
    if result == "password_needed":
        await state.set_state(Login.password)
        await respond(event, i18n.LOGIN_ASK_2FA)
        return
    await state.clear()
    await respond(event, i18n.CONNECTED_OK, kb.main_menu(True))


@router.message(StateFilter(Login.password), F.text)
async def got_password(message: Message, deps: Deps, db_user_id: int, state: FSMContext) -> None:
    password = message.text or ""
    with contextlib.suppress(TelegramBadRequest):
        await message.delete()
    try:
        await _rpc(deps).call("password", db_user_id, password=password)
    except AuthRpcError as e:
        if e.code in ("invalid_password", "flood_wait"):
            await message.answer(f"{error_text(e)}\n\n{i18n.LOGIN_ASK_2FA}")
            return
        await state.clear()
        await message.answer(error_text(e), reply_markup=kb.main_menu(False))
        return
    finally:
        del password
    await state.clear()
    await message.answer(i18n.CONNECTED_OK, reply_markup=kb.main_menu(True))
