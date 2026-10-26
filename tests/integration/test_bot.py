"""Bot handlers driven through the real aiogram Dispatcher with a recording fake Bot API session."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import Chat, Message, Update
from sqlalchemy import select

from tests.conftest import BOT_ID, seed_chat, seed_tenant
from vault.bot import keyboards as kb
from vault.bot.deps import Deps
from vault.bot.main import build_dispatcher
from vault.db.models import ChatStatus, MonitoredChat, Payment, Subscription, Tier
from vault.engine.rpc import RpcError
from vault.payments.service import FakePaymentProvider

ALICE, BOB = 1001, 2002
_ids = itertools.count(1)


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []

    async def close(self) -> None:
        pass

    async def stream_content(self, *a: Any, **kw: Any):  # type: ignore[override]
        if False:
            yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.calls.append(method)
        ret = method.__returning__
        if ret is bool:
            return True
        if ret is Message or "Message" in str(ret):
            chat_id = getattr(method, "chat_id", None) or ALICE
            return Message(
                message_id=next(_ids),
                date=datetime.now(UTC),
                chat=Chat(id=chat_id, type="private"),
                text=getattr(method, "text", None),
            )
        return True

    def texts(self) -> list[str]:
        return [getattr(c, "text", "") or "" for c in self.calls if hasattr(c, "text")]


class FakeRpc:
    def __init__(self) -> None:
        self.dialogs: dict[int, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, int, dict[str, Any]]] = []

    async def call(
        self, shard: int, method: str, user_id: int, wait_seconds: float = 15, **params: Any
    ) -> Any:
        self.calls.append((method, user_id, params))
        mine = self.dialogs.get(user_id)
        if mine is None:
            raise RpcError("no_account")
        if method == "list_dialogs":
            return mine
        if method == "resolve_dialog":
            return next((d for d in mine if d["chat_id"] == params["chat_id"]), None)
        raise RpcError("unknown_method")


def dialog(chat_id: int, title: str, protected: bool = False) -> dict[str, Any]:
    return {
        "chat_id": chat_id,
        "access_hash": 5,
        "chat_type": "SUPERGROUP",
        "title": title,
        "last_message_id": 77,
        "protected": protected,
    }


@pytest.fixture
async def env(db, redis, settings, hasher, clock):
    session = RecordingSession()
    bot = Bot(settings.bot_token.get_secret_value(), session=session)
    rpc = FakeRpc()
    payments = FakePaymentProvider()
    from vault.bot.handlers import add_chat, login, menu
    from vault.bot.handlers import payments as pay_handlers

    for r in (pay_handlers.router, login.router, add_chat.router, menu.router):
        r._parent_router = None  # module-level routers: allow one Dispatcher per test
    deps = Deps(settings=settings, db=db, redis=redis, rpc=rpc, hasher=hasher, payments=payments, clock=clock)  # type: ignore[arg-type]
    dp = build_dispatcher(deps)
    return dp, bot, session, rpc, payments


def user_dict(uid: int) -> dict[str, Any]:
    return {"id": uid, "is_bot": False, "first_name": "T"}


def msg_update(uid: int, text: str | None = None, **extra: Any) -> Update:
    m: dict[str, Any] = {
        "message_id": next(_ids),
        "date": int(datetime.now(UTC).timestamp()),
        "chat": {"id": uid, "type": "private"},
        "from": user_dict(uid),
    }
    if text is not None:
        m["text"] = text
        if text.startswith("/"):
            m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    m.update(extra)
    return Update.model_validate({"update_id": next(_ids), "message": m})


def cb_update(uid: int, data: str) -> Update:
    return Update.model_validate(
        {
            "update_id": next(_ids),
            "callback_query": {
                "id": str(next(_ids)),
                "from": user_dict(uid),
                "chat_instance": "ci",
                "data": data,
                "message": {
                    "message_id": next(_ids),
                    "date": int(datetime.now(UTC).timestamp()),
                    "chat": {"id": uid, "type": "private"},
                    "from": {"id": BOT_ID, "is_bot": True, "first_name": "B"},
                    "text": "x",
                },
            },
        }
    )


async def feed(env, update: Update) -> None:
    dp, bot, *_ = env
    await dp.feed_update(bot, update)


async def test_start_shows_hebrew_menu_with_connect(env):
    await feed(env, msg_update(ALICE, "/start"))
    session = env[2]
    sm = session.calls[-1]
    assert "ברוכים הבאים" in sm.text
    labels = [b.text for row in sm.reply_markup.inline_keyboard for b in row]
    assert "🔗 חיבור חשבון" in labels and "📁 הצ'אטים שלי" in labels


async def test_connect_asks_to_share_own_phone(env):
    await feed(env, msg_update(ALICE, "/connect"))
    sm = env[2].calls[-1]
    assert "שיתוף מספר הטלפון" in sm.text
    button = sm.reply_markup.keyboard[0][0]
    assert button.request_contact is True


async def test_contact_of_someone_else_is_rejected(env, redis):
    await feed(env, msg_update(ALICE, "/connect"))
    other = msg_update(ALICE, contact={"phone_number": "+972500000000", "first_name": "X", "user_id": BOB})
    await feed(env, other)
    assert "רק את החשבון שלכם" in env[2].calls[-1].text
    assert await redis.llen("vault:authrpc") == 0  # nothing sent to the login service


async def test_replayed_update_processed_once(env):
    u = msg_update(ALICE, "/connect")
    await feed(env, u)
    n = len(env[2].calls)
    await feed(env, u)
    assert len(env[2].calls) == n


async def test_group_messages_ignored(env):
    upd = Update.model_validate(
        {
            "update_id": next(_ids),
            "message": {
                "message_id": 1,
                "date": 0,
                "chat": {"id": -100, "type": "supergroup", "title": "g"},
                "from": user_dict(ALICE),
                "text": "/start",
                "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
            },
        }
    )
    await feed(env, upd)
    assert env[2].calls == []


async def test_add_chat_from_list_then_confirm(env, db, cipher):
    user, acc = await seed_tenant(db, cipher, ALICE)
    env[3].dialogs[user.id] = [dialog(-1001, "משפחה"), dialog(-1002, "עבודה", protected=True)]
    await feed(env, cb_update(ALICE, kb.DialogCB(a="page", p=0).pack()))
    edit = env[2].calls[-1]
    labels = [b.text for row in edit.reply_markup.inline_keyboard for b in row]
    assert "משפחה" in labels and "🔒 עבודה" in labels
    await feed(env, cb_update(ALICE, kb.DialogCB(a="pick", cid=-1001).pack()))
    assert "לגבות את הצ'אט הזה?" in env[2].calls[-1].text
    await feed(env, cb_update(ALICE, kb.DialogCB(a="ok", cid=-1001).pack()))
    assert "נוסף לגיבוי" in env[2].calls[-1].text
    async with db.session() as s:
        chat = await s.scalar(select(MonitoredChat))
    assert (
        chat is not None
        and chat.user_id == user.id
        and chat.chat_id == -1001
        and chat.last_seen_message_id == 77
    )


async def test_protected_chat_cannot_be_added(env, db, cipher):
    user, _ = await seed_tenant(db, cipher, ALICE)
    env[3].dialogs[user.id] = [dialog(-1002, "מוגן", protected=True)]
    await feed(env, cb_update(ALICE, kb.DialogCB(a="ok", cid=-1002).pack()))
    assert "מוגן מפני העתקה" in env[2].calls[-1].text
    async with db.session() as s:
        assert await s.scalar(select(MonitoredChat)) is None


async def test_forwarded_channel_message_identifies_chat(env, db, cipher):
    user, _ = await seed_tenant(db, cipher, ALICE)
    env[3].dialogs[user.id] = [dialog(-1003, "ערוץ חדשות")]
    upd = msg_update(
        ALICE,
        "hello",
        forward_origin={
            "type": "channel",
            "date": 0,
            "chat": {"id": -1003, "type": "channel", "title": "ערוץ חדשות"},
            "message_id": 5,
        },
    )
    await feed(env, upd)
    assert "ערוץ חדשות" in env[2].calls[-1].text and "לגבות" in env[2].calls[-1].text


async def test_forward_with_hidden_origin_never_guesses(env, db, cipher):
    user, _ = await seed_tenant(db, cipher, ALICE)
    env[3].dialogs[user.id] = []
    upd = msg_update(
        ALICE, "x", forward_origin={"type": "hidden_user", "date": 0, "sender_user_name": "Anon"}
    )
    await feed(env, upd)
    assert "לא מנחשים" in env[2].calls[-1].text
    assert env[3].calls == []


async def test_cross_tenant_chat_actions_denied(env, db, cipher):
    """Bob forges callback data containing Alice's monitored-chat row id."""
    alice, a_acc = await seed_tenant(db, cipher, ALICE)
    await seed_tenant(db, cipher, BOB)
    alice_chat = await seed_chat(db, alice, a_acc, -100500, "הצ'אט של אליס")
    for action in ("view", "pause", "resume", "remove", "remove_ok", "keep"):
        await feed(env, cb_update(BOB, kb.ChatCB(a=action, id=alice_chat.id).pack()))
        last = env[2].calls[-1]
        assert "הצ'אט של אליס" not in (getattr(last, "text", "") or "")
    async with db.session() as s:
        c = await s.get(MonitoredChat, alice_chat.id)
    assert c is not None and c.status == ChatStatus.ACTIVE and not c.keep_on_free


async def test_cross_tenant_dialog_confirm_uses_own_account_only(env, db, cipher):
    """Bob can't register Alice's chat by forging DialogCB: verification runs against Bob's dialogs."""
    alice, _ = await seed_tenant(db, cipher, ALICE)
    bob, _ = await seed_tenant(db, cipher, BOB)
    env[3].dialogs[alice.id] = [dialog(-1009, "סודי של אליס")]
    env[3].dialogs[bob.id] = []
    await feed(env, cb_update(BOB, kb.DialogCB(a="ok", cid=-1009).pack()))
    assert all(uid == bob.id for _, uid, _ in env[3].calls)
    async with db.session() as s:
        assert await s.scalar(select(MonitoredChat)) is None


async def test_status_lists_no_other_users_data(env, db, cipher):
    alice, a_acc = await seed_tenant(db, cipher, ALICE)
    bob, b_acc = await seed_tenant(db, cipher, BOB)
    await seed_chat(db, alice, a_acc, -1, "AliceOnly")
    await seed_chat(db, bob, b_acc, -2, "BobOnly")
    await feed(env, cb_update(BOB, kb.MenuCB(a="chats").pack()))
    labels = [b.text for row in env[2].calls[-1].reply_markup.inline_keyboard for b in row]
    assert any("BobOnly" in t for t in labels) and not any("AliceOnly" in t for t in labels)
    await feed(env, msg_update(BOB, "/status"))
    assert "צ'אטים פעילים: 1/3" in env[2].calls[-1].text


async def test_login_code_pasted_in_chat_is_deleted(env):
    await feed(env, msg_update(ALICE, "12345"))
    names = [type(c).__name__ for c in env[2].calls]
    assert "DeleteMessage" in names
    assert "לעולם אל תשלחו קודי כניסה" in env[2].calls[-1].text


async def test_purchase_via_bot(env, db, cipher):
    user, _ = await seed_tenant(db, cipher, ALICE)
    await feed(env, cb_update(ALICE, kb.PayCB(a="buy").pack()))
    payments = env[4]
    assert len(payments.invoices) == 1
    chat_id, payload, amount = payments.invoices[0]
    assert (chat_id, amount) == (ALICE, 150)
    pcq = Update.model_validate(
        {
            "update_id": next(_ids),
            "pre_checkout_query": {
                "id": "q1",
                "from": user_dict(ALICE),
                "currency": "XTR",
                "total_amount": 150,
                "invoice_payload": payload,
            },
        }
    )
    await feed(env, pcq)
    ans = env[2].calls[-1]
    assert type(ans).__name__ == "AnswerPreCheckoutQuery" and ans.ok is True
    paid = msg_update(
        ALICE,
        successful_payment={
            "currency": "XTR",
            "total_amount": 150,
            "invoice_payload": payload,
            "telegram_payment_charge_id": "tg_ch_1",
            "provider_payment_charge_id": "",
        },
    )
    await feed(env, paid)
    assert "הפרימיום פעיל" in env[2].calls[-1].text
    # Replay of the same successful_payment (new update id) does not extend twice.
    paid2 = msg_update(
        ALICE,
        successful_payment={
            "currency": "XTR",
            "total_amount": 150,
            "invoice_payload": payload,
            "telegram_payment_charge_id": "tg_ch_1",
            "provider_payment_charge_id": "",
        },
    )
    await feed(env, paid2)
    async with db.session() as s:
        sub = await s.scalar(select(Subscription).where(Subscription.user_id == user.id))
        pays = (await s.scalars(select(Payment))).all()
    assert sub is not None and sub.tier == Tier.PREMIUM and len(pays) == 1


async def test_pre_checkout_from_other_user_rejected(env, db, cipher):
    await seed_tenant(db, cipher, ALICE)
    await feed(env, cb_update(ALICE, kb.PayCB(a="buy").pack()))
    payload = env[4].invoices[0][1]
    pcq = Update.model_validate(
        {
            "update_id": next(_ids),
            "pre_checkout_query": {
                "id": "q2",
                "from": user_dict(BOB),
                "currency": "XTR",
                "total_amount": 150,
                "invoice_payload": payload,
            },
        }
    )
    await feed(env, pcq)
    assert env[2].calls[-1].ok is False


async def test_disconnect_flow(env, db, cipher, redis):
    user, acc = await seed_tenant(db, cipher, ALICE)
    await seed_chat(db, user, acc, -5)
    await feed(env, msg_update(ALICE, "/disconnect"))
    assert "לנתק את החשבון" in env[2].calls[-1].text
    await feed(env, cb_update(ALICE, kb.MenuCB(a="disconnect_ok").pack()))
    assert "נותק" in env[2].calls[-1].text
    async with db.session() as s:
        assert (await s.scalar(select(MonitoredChat))).status == ChatStatus.PAUSED_ACCOUNT  # type: ignore[union-attr]
