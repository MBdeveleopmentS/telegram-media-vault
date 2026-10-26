"""End-to-end pipeline: event -> durable job -> worker -> Bot API delivery -> cleanup.

Telegram is faked (FakeSource / FakeDelivery); PostgreSQL is SQLite by default
(set TEST_DATABASE_URL to run against PostgreSQL); Redis is fakeredis.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from tests.conftest import BOT_ID, seed_chat, seed_tenant
from tests.fakes import FakeMedia, FakeSource, SimulatedCrash
from vault.db.models import (
    AccountStatus,
    ChatStatus,
    JobState,
    MediaDelivery,
    MediaJob,
    MonitoredChat,
    TelegramAccount,
    User,
)
from vault.domain.media import DocumentInfo, IncomingMedia, PhotoInfo
from vault.engine.ingest import AccountContext, IngestOutcome, ingest
from vault.engine.recovery import recover_once
from vault.telegram.delivery import DeliveryError, DestinationUnavailable
from vault.telegram.errors import SourceFloodWait, SourceSessionInvalid

MIB = 1024 * 1024
CHAT = -1001111111111


def photo_event(
    msg_id: int, size: int | None, photo_id: int | None = None, chat: int = CHAT, **kw
) -> IncomingMedia:
    return IncomingMedia(
        chat_id=chat, message_id=msg_id, date=None, photo=PhotoInfo(id=photo_id or msg_id, size=size), **kw
    )


def video_event(msg_id: int, size: int | None, chat: int = CHAT) -> IncomingMedia:
    return IncomingMedia(
        chat_id=chat,
        message_id=msg_id,
        date=None,
        document=DocumentInfo(
            id=500 + msg_id, mime_type="video/mp4", size=size, is_video_attr=True, file_name="clip.mp4"
        ),
    )


@pytest.fixture
async def tenant(db, cipher, registry):
    user, acc = await seed_tenant(db, cipher, 111)
    chat = await seed_chat(db, user, acc, CHAT)
    src = FakeSource()
    registry.sources[acc.id] = src
    ctx = AccountContext(acc.id, user.id, 0, BOT_ID)
    return user, acc, chat, src, ctx


async def job_states(db) -> list[JobState]:
    async with db.session() as s:
        return list((await s.scalars(select(MediaJob.state).order_by(MediaJob.id))).all())


def temp_is_empty(settings) -> bool:
    return list(Path(settings.temp_dir).iterdir()) == []


async def test_photo_delivered_to_owner_and_temp_removed(
    db, queue, settings, clock, tenant, delivery, make_worker
):
    user, acc, chat, src, ctx = tenant
    data = b"\xff\xd8" + b"p" * (MIB - 2)
    src.put(CHAT, 10, FakeMedia(data, "p:10"))
    assert await ingest(db, queue, ctx, photo_event(10, MIB), clock(), settings) == IngestOutcome.QUEUED
    assert await job_states(db) == [JobState.QUEUED]  # durable before any processing

    w = make_worker()
    assert await w.run_once()
    assert await job_states(db) == [JobState.DELIVERED]
    assert len(delivery.sent) == 1
    sent = delivery.sent[0]
    assert sent.chat_id == user.telegram_user_id  # the owner's private bot chat
    assert sent.kind == "photo" and sent.content == data and sent.path_existed_during_send
    assert "קבוצת בדיקה" in sent.caption
    assert temp_is_empty(settings)
    assert not src.observed_paths[0].exists()
    async with db.session() as s:
        d = await s.scalar(select(MediaDelivery))
        c = await s.get(MonitoredChat, chat.id)
    assert d is not None and d.bytes == MIB and d.destination_chat_id == user.telegram_user_id
    assert c is not None and c.delivered_count == 1 and c.last_delivered_at == clock()


async def test_video_delivered_as_video(db, queue, settings, clock, tenant, delivery, make_worker):
    *_, src, ctx = tenant
    src.put(CHAT, 11, FakeMedia(b"v" * 2048, "d:511", file_name="../../clip.mp4"))
    await ingest(db, queue, ctx, video_event(11, 2048), clock(), settings)
    await make_worker().run_once()
    assert delivery.sent[0].kind == "video"
    assert delivery.sent[0].filename == "clip.mp4"


async def test_duplicate_events_create_one_job(db, queue, settings, clock, tenant, delivery, make_worker):
    *_, src, ctx = tenant
    src.put(CHAT, 12, FakeMedia(b"x" * 100, "p:12"))
    outcomes = [await ingest(db, queue, ctx, photo_event(12, 100), clock(), settings) for _ in range(3)]
    assert outcomes == [IngestOutcome.QUEUED, IngestOutcome.DUPLICATE, IngestOutcome.DUPLICATE]
    w = make_worker()
    while await w.run_once():
        pass
    assert len(delivery.sent) == 1


async def test_unapproved_chat_and_bot_chat_ignored(db, queue, settings, clock, tenant):
    *_, ctx = tenant
    assert (
        await ingest(db, queue, ctx, photo_event(1, 10, chat=-1009999), clock(), settings)
        == IngestOutcome.IGNORED
    )
    assert (
        await ingest(db, queue, ctx, photo_event(1, 10, chat=BOT_ID), clock(), settings)
        == IngestOutcome.IGNORED
    )
    assert await job_states(db) == []


async def test_paused_chat_ignored_and_queued_job_skipped_after_pause(
    db, queue, settings, clock, tenant, make_worker, delivery
):
    _, _, chat, src, ctx = tenant
    src.put(CHAT, 13, FakeMedia(b"x", "p:13"))
    await ingest(db, queue, ctx, photo_event(13, 1), clock(), settings)
    async with db.transaction() as s:
        (await s.get(MonitoredChat, chat.id)).status = ChatStatus.PAUSED  # type: ignore[union-attr]
    await make_worker().run_once()
    assert await job_states(db) == [JobState.SKIPPED]
    assert delivery.sent == [] and src.downloads == []
    assert await ingest(db, queue, ctx, photo_event(14, 1), clock(), settings) == IngestOutcome.IGNORED


async def test_failed_delivery_retries_with_backoff(
    db, queue, settings, clock, tenant, delivery, make_worker
):
    *_, src, ctx = tenant
    src.put(CHAT, 20, FakeMedia(b"x" * 10, "p:20"))
    await ingest(db, queue, ctx, photo_event(20, 10), clock(), settings)
    delivery.fail_next = [DeliveryError()]
    w = make_worker()
    await w.run_once()
    assert await job_states(db) == [JobState.RETRY_WAIT]
    assert temp_is_empty(settings)
    rep = await recover_once(db, queue, settings, clock)
    assert rep.promoted == 0  # backoff not elapsed
    clock.advance(seconds=settings.retry_max_seconds)
    rep = await recover_once(db, queue, settings, clock)
    assert rep.promoted == 1
    await w.run_once()
    assert await job_states(db) == [JobState.DELIVERED]
    assert len(delivery.sent) == 1


async def test_retry_exhaustion_marks_failed(db, queue, settings, clock, tenant, delivery, make_worker):
    _, _, chat, src, ctx = tenant
    src.put(CHAT, 21, FakeMedia(b"x", "p:21"))
    await ingest(db, queue, ctx, photo_event(21, 1), clock(), settings)
    delivery.fail_next = [DeliveryError() for _ in range(settings.job_max_attempts)]
    w = make_worker()
    for _ in range(settings.job_max_attempts):
        await w.run_once()
        clock.advance(seconds=settings.retry_max_seconds + 1)
        await recover_once(db, queue, settings, clock)
    assert await job_states(db) == [JobState.FAILED]
    async with db.session() as s:
        assert (await s.get(MonitoredChat, chat.id)).failed_count == 1  # type: ignore[union-attr]


async def test_crash_after_upload_is_recovered_as_ambiguous(
    db, queue, settings, clock, tenant, delivery, make_worker
):
    *_, src, ctx = tenant
    src.put(CHAT, 30, FakeMedia(b"x" * 10, "p:30"))
    await ingest(db, queue, ctx, photo_event(30, 10), clock(), settings)
    delivery.crash_after_send = True
    with pytest.raises(SimulatedCrash):
        await make_worker("crashy").run_once()
    assert await job_states(db) == [JobState.UPLOADING]
    assert temp_is_empty(settings)  # cleaned even on hard failure
    delivery.crash_after_send = False

    # Lease still valid: recovery must not steal an in-flight job.
    assert (await recover_once(db, queue, settings, clock)).reclaimed == 0
    clock.advance(seconds=settings.job_lease_seconds + 1)
    rep = await recover_once(db, queue, settings, clock)
    assert rep.reclaimed == 1 and rep.ambiguous == 1
    await make_worker("w2").run_once()
    assert await job_states(db) == [JobState.DELIVERED]
    assert len(delivery.sent) == 2  # at-least-once: the documented duplicate risk
    assert "ייתכן שהקובץ הזה כבר נשלח" in delivery.sent[1].caption


async def test_crash_during_download_requeues_without_ambiguity(
    db, queue, settings, clock, tenant, delivery, make_worker
):
    *_, src, ctx = tenant
    src.put(CHAT, 31, FakeMedia(b"x" * 10, "p:31"))
    await ingest(db, queue, ctx, photo_event(31, 10), clock(), settings)
    src.fail_next = [SimulatedCrash()]
    with pytest.raises(SimulatedCrash):
        await make_worker().run_once()
    assert await job_states(db) == [JobState.DOWNLOADING]
    clock.advance(seconds=settings.job_lease_seconds + 1)
    rep = await recover_once(db, queue, settings, clock)
    assert rep.reclaimed == 1 and rep.ambiguous == 0
    await make_worker("w2").run_once()
    assert len(delivery.sent) == 1 and "ייתכן" not in delivery.sent[0].caption


async def test_restart_does_not_redeliver_completed_jobs(
    db, queue, settings, clock, tenant, delivery, make_worker
):
    *_, src, ctx = tenant
    for i in (40, 41):
        src.put(CHAT, i, FakeMedia(b"x", f"p:{i}"))
        await ingest(db, queue, ctx, photo_event(i, 1), clock(), settings)
    w = make_worker()
    while await w.run_once():
        pass
    assert len(delivery.sent) == 2
    # "restart": new worker, recovery pass, replayed events from catch-up
    clock.advance(minutes=10)
    await recover_once(db, queue, settings, clock)
    for i in (40, 41):
        assert await ingest(db, queue, ctx, photo_event(i, 1), clock(), settings) == IngestOutcome.DUPLICATE
    w2 = make_worker("after-restart")
    while await w2.run_once():
        pass
    assert len(delivery.sent) == 2


async def test_redis_data_loss_recovered_from_postgres(
    db, queue, redis, settings, clock, tenant, delivery, make_worker
):
    *_, src, ctx = tenant
    src.put(CHAT, 50, FakeMedia(b"x", "p:50"))
    await ingest(db, queue, ctx, photo_event(50, 1), clock(), settings)
    await redis.flushall()  # Redis restarted without persistence
    w = make_worker()
    assert not await w.run_once()
    rep = await recover_once(db, queue, settings, clock)
    assert rep.repushed == 1
    assert await w.run_once()
    assert len(delivery.sent) == 1


async def test_redis_unavailable_during_ingest_still_persists_job(db, settings, clock, tenant):
    *_, ctx = tenant

    class DownQueue:
        async def push(self, *a):
            raise ConnectionError("redis down")

    assert await ingest(db, DownQueue(), ctx, photo_event(51, 1), clock(), settings) == IngestOutcome.QUEUED  # type: ignore[arg-type]
    assert await job_states(db) == [JobState.QUEUED]


@pytest.mark.parametrize(
    ("size", "expected"), [(5 * MIB - 1, JobState.DELIVERED), (5 * MIB + 1, JobState.SKIPPED)]
)
async def test_free_tier_size_boundary(
    db, queue, settings, clock, tenant, delivery, make_worker, size, expected
):
    *_, src, ctx = tenant
    src.put(CHAT, 60, FakeMedia(b"x" * size, "d:560"))
    await ingest(db, queue, ctx, video_event(60, size), clock(), settings)
    await make_worker().run_once()
    assert await job_states(db) == [expected]
    if expected == JobState.SKIPPED:
        assert src.downloads == []  # skipped from metadata, never downloaded


async def test_unknown_size_enforced_while_streaming(
    db, queue, settings, clock, tenant, delivery, make_worker
):
    *_, src, ctx = tenant
    src.put(CHAT, 61, FakeMedia(b"x" * (6 * MIB), "d:561"))
    await ingest(db, queue, ctx, video_event(61, None), clock(), settings)
    await make_worker().run_once()
    async with db.session() as s:
        job = await s.scalar(select(MediaJob))
    assert job is not None and job.state == JobState.SKIPPED and job.skip_reason == "too_large"
    assert delivery.sent == [] and temp_is_empty(settings)


async def test_premium_allows_15_mib(db, queue, settings, clock, cipher, registry, delivery, make_worker):
    user, acc = await seed_tenant(db, cipher, 222, premium_until=clock() + timedelta(days=30))
    await seed_chat(db, user, acc, CHAT)
    src = FakeSource()
    registry.sources[acc.id] = src
    ctx = AccountContext(acc.id, user.id, 0, BOT_ID)
    src.put(CHAT, 70, FakeMedia(b"x" * (12 * MIB), "d:570"))
    await ingest(db, queue, ctx, video_event(70, 12 * MIB), clock(), settings)
    src.put(CHAT, 71, FakeMedia(b"x", "d:571"))
    assert await ingest(db, queue, ctx, video_event(71, 16 * MIB), clock(), settings) == IngestOutcome.SKIPPED
    await make_worker().run_once()
    assert len(delivery.sent) == 1 and delivery.sent[0].size == 12 * MIB


async def test_flood_wait_is_scheduled_not_slept(db, queue, settings, clock, tenant, make_worker, registry):
    _, acc, _, src, ctx = tenant
    src.put(CHAT, 80, FakeMedia(b"x", "p:80"))
    await ingest(db, queue, ctx, photo_event(80, 1), clock(), settings)
    src.fail_next = [SourceFloodWait(120)]
    await make_worker().run_once()
    async with db.session() as s:
        job = await s.scalar(select(MediaJob))
    assert job is not None and job.state == JobState.RETRY_WAIT
    assert job.attempts == 0  # FloodWait doesn't burn an attempt
    assert job.next_attempt_at is not None and job.next_attempt_at - clock() >= timedelta(seconds=120)
    assert registry.paused_until(acc.id) > 0


async def test_destination_unavailable(db, queue, settings, clock, tenant, delivery, make_worker):
    user, *_, src, ctx = tenant
    src.put(CHAT, 90, FakeMedia(b"x", "p:90"))
    await ingest(db, queue, ctx, photo_event(90, 1), clock(), settings)
    delivery.fail_next = [DestinationUnavailable()]
    await make_worker().run_once()
    async with db.session() as s:
        u = await s.get(User, user.id)
        job = await s.scalar(select(MediaJob))
    assert u is not None and not u.destination_ok
    assert job is not None and job.skip_reason == "destination_unavailable"
    assert await ingest(db, queue, ctx, photo_event(91, 1), clock(), settings) == IngestOutcome.SKIPPED


async def test_expired_session_pauses_account_and_notifies(
    db, queue, settings, clock, tenant, delivery, make_worker, registry
):
    user, acc, _, src, ctx = tenant
    src.put(CHAT, 95, FakeMedia(b"x", "p:95"))
    await ingest(db, queue, ctx, photo_event(95, 1), clock(), settings)
    src.fail_next = [SourceSessionInvalid()]
    await make_worker().run_once()
    async with db.session() as s:
        a = await s.get(TelegramAccount, acc.id)
    assert a is not None and a.status == AccountStatus.EXPIRED
    assert registry.invalidated == [acc.id]
    assert delivery.texts and delivery.texts[0][0] == user.telegram_user_id
    assert await ingest(db, queue, ctx, photo_event(96, 1), clock(), settings) == IngestOutcome.IGNORED


async def test_burst_with_concurrent_workers_delivers_each_once(
    db, queue, settings, clock, tenant, delivery, make_worker
):
    *_, src, ctx = tenant
    n = 25
    for i in range(100, 100 + n):
        src.put(CHAT, i, FakeMedia(bytes([i % 256]) * 1000, f"p:{i}"))
    await asyncio.gather(
        *(ingest(db, queue, ctx, photo_event(i, 1000), clock(), settings) for i in range(100, 100 + n))
    )
    workers = [make_worker(f"w{i}") for i in range(3)]

    async def drain(w):
        idle = 0
        while idle < 3:
            idle = 0 if await w.run_once() else idle + 1
            await asyncio.sleep(0)

    await asyncio.gather(*(drain(w) for w in workers))
    assert len(delivery.sent) == n
    assert set(await job_states(db)) == {JobState.DELIVERED}
    assert temp_is_empty(settings)


async def test_album_items_each_delivered_with_group_id(
    db, queue, settings, clock, tenant, delivery, make_worker
):
    *_, src, ctx = tenant
    for i in (200, 201, 202):
        src.put(CHAT, i, FakeMedia(b"a", f"p:{i}"))
        await ingest(db, queue, ctx, photo_event(i, 1, grouped_id=999), clock(), settings)
    w = make_worker()
    while await w.run_once():
        pass
    assert len(delivery.sent) == 3
    async with db.session() as s:
        assert set((await s.scalars(select(MediaJob.grouped_id))).all()) == {999}


async def test_two_users_receive_only_their_own_media(
    db, queue, settings, clock, cipher, registry, delivery, make_worker
):
    users = []
    for tg in (301, 302):
        u, a = await seed_tenant(db, cipher, tg)
        await seed_chat(db, u, a, CHAT)  # same source chat, both members
        src = FakeSource()
        src.put(CHAT, 1, FakeMedia(f"content-of-{tg}".encode(), "p:1"))
        registry.sources[a.id] = src
        users.append((u, AccountContext(a.id, u.id, 0, BOT_ID)))
    for _, ctx in users:
        await ingest(db, queue, ctx, photo_event(1, 20), clock(), settings)
    w = make_worker()
    while await w.run_once():
        pass
    by_chat = {s.chat_id: s.content for s in delivery.sent}
    assert by_chat == {301: b"content-of-301", 302: b"content-of-302"}


async def test_per_user_backlog_cap(db, queue, settings, clock, tenant):
    *_, ctx = tenant
    settings.max_pending_jobs_per_user = 2
    out = [await ingest(db, queue, ctx, photo_event(i, 1), clock(), settings) for i in (1, 2, 3)]
    assert out == [IngestOutcome.QUEUED, IngestOutcome.QUEUED, IngestOutcome.SKIPPED]


async def test_per_user_concurrency_requeues_instead_of_blocking(
    db, queue, settings, clock, tenant, make_worker
):
    user, *_, src, ctx = tenant
    src.put(CHAT, 1, FakeMedia(b"x", "p:1"))
    await ingest(db, queue, ctx, photo_event(1, 1), clock(), settings)
    w = make_worker()
    for _ in range(settings.per_user_concurrency):
        assert w.pool.try_acquire(user.id)
    assert await w.run_once()  # consumed and put back
    assert await job_states(db) == [JobState.QUEUED]
    assert await queue.redis.llen("vault:q:0:free") == 1
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(MediaDelivery)) == 0
