"""In-chat login service (runs in the engine/auth-web process) with a fake MTProto client."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from tests.fakes import FakeLoginClient
from vault.authweb.chat_login import AuthRpcError, ChatLoginService
from vault.db.models import AccountStatus, EncryptedSession, TelegramAccount
from vault.domain.subscriptions import get_or_create_user

TG = 4242


@pytest.fixture
async def svc(db, redis, settings, cipher, hasher, clock):
    holder = {"client": FakeLoginClient(user_id=TG)}
    service = ChatLoginService(
        settings=settings,
        db=db,
        redis=redis,
        cipher=cipher,
        hasher=hasher,
        login_factory=lambda: holder["client"],
        clock=clock,
    )
    async with db.transaction() as s:
        user = await get_or_create_user(s, TG)
    return service, holder, user.id


async def test_code_login_connects_account(svc, db):
    service, holder, uid = svc
    assert await service.dispatch("send_code", uid, {"phone": "+972 50-123-4567"}) == "code_sent"
    assert await service.dispatch("code", uid, {"code": "1 2 3 4 5"}) == "connected"
    async with db.session() as s:
        acc = await s.scalar(select(TelegramAccount))
        assert acc is not None and acc.status == AccountStatus.ACTIVE
        assert await s.scalar(select(EncryptedSession)) is not None
    assert holder["client"].closed and uid not in service.pending


async def test_two_factor(svc):
    service, holder, uid = svc
    holder["client"] = FakeLoginClient(user_id=TG, password="pw")
    await service.dispatch("send_code", uid, {"phone": "+972501234567"})
    assert await service.dispatch("code", uid, {"code": "12345"}) == "password_needed"
    with pytest.raises(AuthRpcError) as e:
        await service.dispatch("password", uid, {"password": "bad"})
    assert e.value.code == "invalid_password"
    assert await service.dispatch("password", uid, {"password": "pw"}) == "connected"


async def test_identity_mismatch_logged_out(svc, db):
    service, holder, uid = svc
    holder["client"] = FakeLoginClient(user_id=999)
    await service.dispatch("send_code", uid, {"phone": "+972501234567"})
    with pytest.raises(AuthRpcError) as e:
        await service.dispatch("code", uid, {"code": "12345"})
    assert e.value.code == "identity_mismatch" and holder["client"].logged_out
    async with db.session() as s:
        assert await s.scalar(select(TelegramAccount)) is None


async def test_code_attempt_limit(svc, settings):
    service, _, uid = svc
    await service.dispatch("send_code", uid, {"phone": "+972501234567"})
    for _ in range(settings.auth_max_code_attempts):
        with pytest.raises(AuthRpcError):
            await service.dispatch("code", uid, {"code": "00000"})
    with pytest.raises(AuthRpcError) as e:
        await service.dispatch("code", uid, {"code": "12345"})
    assert e.value.code == "too_many_attempts"


async def test_code_without_pending_login(svc):
    service, _, uid = svc
    with pytest.raises(AuthRpcError) as e:
        await service.dispatch("code", uid, {"code": "12345"})
    assert e.value.code == "restart_required"


async def test_phone_rate_limit(svc):
    service, _, uid = svc
    for _ in range(3):
        await service.dispatch("send_code", uid, {"phone": "+972501234567"})
    with pytest.raises(AuthRpcError) as e:
        await service.dispatch("send_code", uid, {"phone": "+972501234567"})
    assert e.value.code == "rate_limited"
