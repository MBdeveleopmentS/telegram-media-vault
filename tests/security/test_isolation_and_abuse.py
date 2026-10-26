"""Adversarial multi-tenant and abuse scenarios (local fakes only)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from tests.conftest import BOT_ID, seed_chat, seed_tenant
from tests.fakes import FakeMedia, FakeSource
from vault.db.models import JobState, MediaJob, SecurityEvent, TelegramAccount
from vault.domain import chats as chat_svc
from vault.domain.media import IncomingMedia, PhotoInfo
from vault.engine.ingest import AccountContext, IngestOutcome, ingest
from vault.engine.rpc import RpcError, RpcServer
from vault.security.events import sanitize_details

CHAT = -100424242


def ev(msg_id: int, chat: int = CHAT, size: int = 10) -> IncomingMedia:
    return IncomingMedia(chat_id=chat, message_id=msg_id, date=None, photo=PhotoInfo(msg_id, size))


async def test_event_on_one_account_cannot_create_job_for_another_users_chat(
    db, queue, settings, clock, cipher
):
    alice, a_acc = await seed_tenant(db, cipher, 1)
    bob, b_acc = await seed_tenant(db, cipher, 2)
    await seed_chat(db, bob, b_acc, CHAT)  # only Bob approved this chat
    alice_ctx = AccountContext(a_acc.id, alice.id, 0, BOT_ID)
    assert await ingest(db, queue, alice_ctx, ev(1), clock(), settings) == IngestOutcome.IGNORED
    # forged context mixing Alice's user with Bob's account
    forged = AccountContext(b_acc.id, alice.id, 0, BOT_ID)
    assert await ingest(db, queue, forged, ev(2), clock(), settings) == IngestOutcome.IGNORED
    async with db.session() as s:
        assert (await s.scalars(select(MediaJob))).all() == []


async def test_tampered_job_never_delivered_to_wrong_user(
    db, queue, settings, clock, cipher, registry, delivery, make_worker
):
    """Simulate DB tampering: a job row pointing Bob's media at Alice."""
    alice, a_acc = await seed_tenant(db, cipher, 1)
    bob, b_acc = await seed_tenant(db, cipher, 2)
    await seed_chat(db, bob, b_acc, CHAT)
    src = FakeSource()
    src.put(CHAT, 1, FakeMedia(b"bob-private", "p:1"))
    registry.sources[b_acc.id] = src
    await ingest(db, queue, AccountContext(b_acc.id, bob.id, 0, BOT_ID), ev(1), clock(), settings)
    async with db.transaction() as s:
        job = await s.scalar(select(MediaJob))
        job.user_id = alice.id  # type: ignore[union-attr]
    await make_worker().run_once()
    assert delivery.sent == [] and src.downloads == []
    async with db.session() as s:
        job = await s.scalar(select(MediaJob))
        events = (await s.scalars(select(SecurityEvent.event_type))).all()
    assert job is not None and job.state == JobState.FAILED and job.last_error_code == "identity_mismatch"
    assert "destination_mismatch" in events


async def test_identity_invariant_account_must_match_user(
    db, queue, settings, clock, cipher, registry, delivery, make_worker
):
    user, acc = await seed_tenant(db, cipher, 1)
    await seed_chat(db, user, acc, CHAT)
    src = FakeSource()
    src.put(CHAT, 1, FakeMedia(b"x", "p:1"))
    registry.sources[acc.id] = src
    await ingest(db, queue, AccountContext(acc.id, user.id, 0, BOT_ID), ev(1), clock(), settings)
    async with db.transaction() as s:
        (await s.get(TelegramAccount, acc.id)).tg_user_id = 999  # type: ignore[union-attr]
    await make_worker().run_once()
    assert delivery.sent == []


async def test_queue_poisoning_is_harmless(
    db, queue, redis, settings, clock, cipher, registry, delivery, make_worker
):
    user, acc = await seed_tenant(db, cipher, 1)
    await seed_chat(db, user, acc, CHAT)
    src = FakeSource()
    src.put(CHAT, 1, FakeMedia(b"x", "p:1"))
    registry.sources[acc.id] = src
    for junk in (b"not-a-number", b"999999", b"-1", b"1; DROP TABLE media_jobs"):
        await redis.rpush("vault:q:0:premium", junk)
    await ingest(db, queue, AccountContext(acc.id, user.id, 0, BOT_ID), ev(1), clock(), settings)
    w = make_worker()
    for _ in range(10):
        await w.run_once()
    assert len(delivery.sent) == 1


async def test_job_from_other_shard_not_processed(
    db, queue, settings, clock, cipher, registry, delivery, make_worker
):
    user, acc = await seed_tenant(db, cipher, 1)
    await seed_chat(db, user, acc, CHAT)
    await ingest(db, queue, AccountContext(acc.id, user.id, 1, BOT_ID), ev(1), clock(), settings)
    async with db.session() as s:
        job = await s.scalar(select(MediaJob))
    await queue.push(0, job.priority, job.id)  # type: ignore[union-attr]
    await make_worker().run_once()
    assert delivery.sent == []


async def test_unexpected_exception_cleans_temp_and_retries(
    db, queue, settings, clock, cipher, registry, delivery, make_worker
):
    user, acc = await seed_tenant(db, cipher, 1)
    await seed_chat(db, user, acc, CHAT)
    src = FakeSource()
    src.put(CHAT, 1, FakeMedia(b"secret" * 1000, "p:1"))
    registry.sources[acc.id] = src
    await ingest(db, queue, AccountContext(acc.id, user.id, 0, BOT_ID), ev(1), clock(), settings)
    delivery.fail_next = [RuntimeError("boom")]
    await make_worker().run_once()
    assert list(Path(settings.temp_dir).iterdir()) == []
    async with db.session() as s:
        assert (await s.scalar(select(MediaJob))).state == JobState.RETRY_WAIT  # type: ignore[union-attr]


class _FakeRuntime:
    def __init__(self, mapping):
        self.mapping = mapping

    def account_for_user(self, user_id):
        return self.mapping.get(user_id)


async def test_rpc_never_serves_another_users_dialogs(redis):
    class LA:
        def __init__(self, name):
            self.adapter = self
            self.name = name

        async def list_dialogs(self, limit=400):
            return []

        async def resolve_dialog(self, chat_id):
            return None

    server = RpcServer(redis, _FakeRuntime({1: LA("alice")}), 0)  # type: ignore[arg-type]
    assert await server.dispatch("list_dialogs", 1, {}) == []
    with pytest.raises(RpcError) as e:
        await server.dispatch("list_dialogs", 2, {})
    assert e.value.code == "no_account"
    with pytest.raises(RpcError):
        await server.dispatch("download_everything", 1, {})


async def test_rpc_drops_malformed_requests(redis):
    server = RpcServer(redis, _FakeRuntime({}), 0)  # type: ignore[arg-type]
    for raw in (b"{", b"[]", b'{"id": "short", "method": "x", "user_id": 1}', b'{"id": 1}'):
        await server._handle(raw)
    assert await redis.keys("vault:rpc:resp:*") == []


async def test_get_owned_chat_enforces_tenant(db, cipher):
    alice, a_acc = await seed_tenant(db, cipher, 1)
    bob, _ = await seed_tenant(db, cipher, 2)
    c = await seed_chat(db, alice, a_acc, CHAT)
    async with db.session() as s:
        with pytest.raises(chat_svc.ChatNotFound):
            await chat_svc.get_owned_chat(s, bob.id, c.id)
        assert (await chat_svc.get_owned_chat(s, alice.id, c.id)).id == c.id


def test_security_event_details_allowlist():
    out = sanitize_details(
        {
            "reason": "x",
            "password": "hunter2",
            "code": "12345",
            "phone": "+97250",
            "session": "1AA",
            "payment_id": 5,
            "reason_long": "y" * 999,
        }
    )
    assert out == {"reason": "x", "payment_id": 5}


async def test_webhook_secret_enforced(settings):
    from aiogram import Bot, Dispatcher
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler

    bot = Bot(settings.bot_token.get_secret_value())
    h = SimpleRequestHandler(dispatcher=Dispatcher(), bot=bot, secret_token="s3cret-value")
    assert h.verify_secret("s3cret-value", bot)
    assert not h.verify_secret("", bot)
    assert not h.verify_secret("guess", bot)
    await bot.session.close()
