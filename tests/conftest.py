from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import fakeredis
import pytest

from tests.fakes import FakeDelivery, FakeRegistry, FakeSource
from vault.config import Settings
from vault.db.models import (
    AccountStatus,
    Base,
    ChatStatus,
    ChatType,
    EncryptedSession,
    MonitoredChat,
    Subscription,
    TelegramAccount,
    Tier,
    User,
)
from vault.db.session import Database
from vault.engine.worker import TransferWorker, WorkerPoolState
from vault.queue.redis_queue import JobQueue, WeightedPicker
from vault.security.crypto import Hasher, SessionCipher, generate_key_b64, session_aad

BOT_ID = 7000000001
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
KEY = generate_key_b64()
APP_SECRET = "test-app-secret-key-0123456789-abcdefghijklmnop"


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        bot_token=f"{BOT_ID}:AAH-test-token-not-real-0123456789abcdefg",
        telegram_api_id=12345,
        telegram_api_hash="0123456789abcdef0123456789abcdef",
        session_encryption_keys=f"k1:{KEY}",
        session_encryption_active_key_id="k1",
        app_secret_key=APP_SECRET,
        public_base_url="http://localhost:8085",
        temp_dir=str(tmp_path / "vault-tmp"),
        retry_base_seconds=5,
        job_max_attempts=4,
        job_lease_seconds=60,
        per_user_concurrency=2,
    )


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    database = Database(url)
    async with database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield database
    await database.dispose()


@pytest.fixture
async def redis() -> AsyncIterator[fakeredis.FakeAsyncRedis]:
    r = fakeredis.FakeAsyncRedis()
    yield r
    await r.flushall()
    await r.aclose()


@pytest.fixture
def queue(redis: fakeredis.FakeAsyncRedis) -> JobQueue:
    return JobQueue(redis)


@pytest.fixture
def cipher(settings: Settings) -> SessionCipher:
    return SessionCipher(settings.encryption_keys(), settings.active_key_id())


@pytest.fixture
def hasher() -> Hasher:
    return Hasher(APP_SECRET)


@dataclass
class Tenant:
    user: User
    account: TelegramAccount
    source: FakeSource


async def seed_tenant(
    db: Database,
    cipher: SessionCipher,
    tg_user_id: int,
    *,
    premium_until: datetime | None = None,
    status: AccountStatus = AccountStatus.ACTIVE,
) -> tuple[User, TelegramAccount]:
    async with db.transaction() as s:
        user = User(telegram_user_id=tg_user_id, created_at=NOW)
        s.add(user)
        await s.flush()
        s.add(
            Subscription(
                user_id=user.id,
                tier=Tier.PREMIUM if premium_until else Tier.FREE,
                premium_expires_at=premium_until,
                reminders_sent=[],
            )
        )
        acc = TelegramAccount(
            user_id=user.id, tg_user_id=tg_user_id, status=status, shard=0, connected_at=NOW
        )
        s.add(acc)
        await s.flush()
        sealed = cipher.seal("1SESSION-" + str(tg_user_id), session_aad(acc.id, tg_user_id))
        s.add(
            EncryptedSession(
                account_id=acc.id, key_id=sealed.key_id, nonce=sealed.nonce, ciphertext=sealed.ciphertext
            )
        )
    return user, acc


async def seed_chat(
    db: Database,
    user: User,
    account: TelegramAccount,
    chat_id: int,
    title: str = "קבוצת בדיקה",
    *,
    status: ChatStatus = ChatStatus.ACTIVE,
    created_at: datetime = NOW,
    keep_on_free: bool = False,
) -> MonitoredChat:
    async with db.transaction() as s:
        c = MonitoredChat(
            user_id=user.id,
            account_id=account.id,
            chat_id=chat_id,
            access_hash=99,
            chat_type=ChatType.SUPERGROUP,
            title=title,
            status=status,
            consent_at=NOW,
            created_at=created_at,
            keep_on_free=keep_on_free,
        )
        s.add(c)
    return c


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


@pytest.fixture
def delivery() -> FakeDelivery:
    return FakeDelivery()


@pytest.fixture
def make_worker(db, queue, registry, delivery, settings, clock):  # type: ignore[no-untyped-def]
    from vault.engine.tempfiles import ensure_root

    pool = WorkerPoolState(per_user_limit=settings.per_user_concurrency)
    picker = WeightedPicker(settings.premium_weight, settings.free_weight)
    root = ensure_root(settings.temp_dir)

    def _make(worker_id: str = "w1") -> TransferWorker:
        return TransferWorker(
            worker_id=worker_id,
            db=db,
            queue=queue,
            sources=registry,
            delivery=delivery,
            settings=settings,
            pool=pool,
            picker=picker,
            temp_root=root,
            clock=clock,
        )

    return _make


@pytest.fixture(autouse=True, scope="session")
def _logging() -> None:
    from vault.logging import configure_logging

    configure_logging("test")
