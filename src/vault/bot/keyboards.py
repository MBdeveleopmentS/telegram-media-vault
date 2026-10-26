from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from vault import i18n
from vault.db.models import ChatStatus, MonitoredChat


class MenuCB(CallbackData, prefix="m"):
    a: str  # chats | add | status | premium | privacy | settings | help | connect | home | disconnect


class ChatCB(CallbackData, prefix="c"):
    a: str  # view | pause | resume | remove | remove_ok | keep | unkeep
    id: int


class DialogCB(CallbackData, prefix="d"):
    a: str  # page | pick | ok | search | list
    p: int = 0
    cid: int = 0


class PayCB(CallbackData, prefix="p"):
    a: str  # buy


def main_menu(connected: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if not connected:
        b.button(text=i18n.BTN_CONNECT, callback_data=MenuCB(a="connect"))
    b.button(text=i18n.BTN_MY_CHATS, callback_data=MenuCB(a="chats"))
    b.button(text=i18n.BTN_ADD_CHAT, callback_data=MenuCB(a="add"))
    b.button(text=i18n.BTN_STATUS, callback_data=MenuCB(a="status"))
    b.button(text=i18n.BTN_PREMIUM, callback_data=MenuCB(a="premium"))
    b.button(text=i18n.BTN_PRIVACY, callback_data=MenuCB(a="privacy"))
    b.button(text=i18n.BTN_SETTINGS, callback_data=MenuCB(a="settings"))
    b.button(text=i18n.BTN_HELP, callback_data=MenuCB(a="help"))
    b.adjust(*([1] if not connected else []), 2, 2, 2, 1)
    return b.as_markup()


def back_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=i18n.BTN_MENU, callback_data=MenuCB(a="home").pack())]]
    )


def login_link(url: str) -> InlineKeyboardMarkup:
    """Opens the login page as a Telegram Mini App: a window inside Telegram, not a chat
    message (login codes typed into chats are invalidated by Telegram)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=i18n.BTN_OPEN_LOGIN, web_app=WebAppInfo(url=url))]]
    )


def chats_list(chats: list[MonitoredChat]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for c in chats:
        icon = "🟢" if c.status == ChatStatus.ACTIVE else "⏸"
        b.button(text=f"{icon} {c.title}"[:60], callback_data=ChatCB(a="view", id=c.id))
    b.button(text=i18n.BTN_ADD_CHAT, callback_data=MenuCB(a="add"))
    b.button(text=i18n.BTN_MENU, callback_data=MenuCB(a="home"))
    b.adjust(*([1] * len(chats)), 2)
    return b.as_markup()


def chat_actions(chat: MonitoredChat) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if chat.status == ChatStatus.ACTIVE:
        b.button(text=i18n.BTN_PAUSE, callback_data=ChatCB(a="pause", id=chat.id))
    else:
        b.button(text=i18n.BTN_RESUME, callback_data=ChatCB(a="resume", id=chat.id))
    b.button(text=i18n.BTN_REMOVE, callback_data=ChatCB(a="remove", id=chat.id))
    if chat.keep_on_free:
        b.button(text=i18n.BTN_UNKEEP_ON_FREE, callback_data=ChatCB(a="unkeep", id=chat.id))
    else:
        b.button(text=i18n.BTN_KEEP_ON_FREE, callback_data=ChatCB(a="keep", id=chat.id))
    b.button(text=i18n.BTN_BACK, callback_data=MenuCB(a="chats"))
    b.adjust(2, 1, 1)
    return b.as_markup()


def confirm_remove(chat_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=i18n.BTN_CONFIRM_REMOVE, callback_data=ChatCB(a="remove_ok", id=chat_id))
    b.button(text=i18n.BTN_CANCEL, callback_data=ChatCB(a="view", id=chat_id))
    return b.as_markup()


def pick_chats(chats: list[MonitoredChat], action: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for c in chats:
        b.button(text=c.title[:60], callback_data=ChatCB(a=action, id=c.id))
    b.button(text=i18n.BTN_MENU, callback_data=MenuCB(a="home"))
    b.adjust(1)
    return b.as_markup()


def add_chat_intro() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=i18n.BTN_FROM_LIST, callback_data=DialogCB(a="page", p=0))
    b.button(text=i18n.BTN_MENU, callback_data=MenuCB(a="home"))
    b.adjust(1)
    return b.as_markup()


def dialogs_page(items: list[tuple[int, str]], page: int, pages: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for cid, title in items:
        b.button(text=title[:60], callback_data=DialogCB(a="pick", p=page, cid=cid))
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(text=i18n.BTN_PREV, callback_data=DialogCB(a="page", p=page - 1).pack())
        )
    if page + 1 < pages:
        nav.append(
            InlineKeyboardButton(text=i18n.BTN_NEXT, callback_data=DialogCB(a="page", p=page + 1).pack())
        )
    b.adjust(1)
    markup = b.as_markup()
    if nav:
        markup.inline_keyboard.append(nav)
    markup.inline_keyboard.append(
        [
            InlineKeyboardButton(text=i18n.BTN_SEARCH, callback_data=DialogCB(a="search").pack()),
            InlineKeyboardButton(text=i18n.BTN_MENU, callback_data=MenuCB(a="home").pack()),
        ]
    )
    return markup


def confirm_add(chat_id: int, from_forward: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=i18n.BTN_CONFIRM_ADD, callback_data=DialogCB(a="ok", cid=chat_id))
    if from_forward:
        b.button(text=i18n.BTN_FROM_LIST, callback_data=DialogCB(a="page", p=0))
    b.button(text=i18n.BTN_CANCEL, callback_data=MenuCB(a="home"))
    b.adjust(1)
    return b.as_markup()


def premium(price: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=i18n.BTN_BUY_PREMIUM.format(price=price), callback_data=PayCB(a="buy"))
    b.button(text=i18n.BTN_TERMS, callback_data=MenuCB(a="terms"))
    b.button(text=i18n.BTN_MENU, callback_data=MenuCB(a="home"))
    b.adjust(1)
    return b.as_markup()


def settings_menu(connected: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if connected:
        b.button(text=i18n.BTN_DISCONNECT, callback_data=MenuCB(a="disconnect"))
    else:
        b.button(text=i18n.BTN_CONNECT, callback_data=MenuCB(a="connect"))
    b.button(text=i18n.BTN_PRIVACY, callback_data=MenuCB(a="privacy"))
    b.button(text=i18n.BTN_MENU, callback_data=MenuCB(a="home"))
    b.adjust(1)
    return b.as_markup()


def confirm_disconnect() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=i18n.BTN_CONFIRM_DISCONNECT, callback_data=MenuCB(a="disconnect_ok"))
    b.button(text=i18n.BTN_CANCEL, callback_data=MenuCB(a="home"))
    b.adjust(1)
    return b.as_markup()


def privacy_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=i18n.BTN_SOURCE, callback_data=MenuCB(a="source"))
    b.button(text=i18n.BTN_MENU, callback_data=MenuCB(a="home"))
    b.adjust(1)
    return b.as_markup()
