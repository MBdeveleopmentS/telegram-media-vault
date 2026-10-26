"""Engine process: account runtime (listener) + transfer workers + recovery + RPC,
plus (unless disabled) the in-chat login service and the scheduler.

Hosting login and scheduler here keeps the default deployment at two Python
processes (bot + engine). The engine already holds the only secrets those
services need (API_HASH, session encryption key, bot token); the bot process
still holds none of them."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiogram import Bot

from vault.authweb.chat_login import ChatLoginService
from vault.authweb.login import TelethonLoginClient
from vault.config import Component, settings
from vault.db.session import Database
from vault.engine.recovery import recover_once
from vault.engine.rpc import RpcServer
from vault.engine.runtime import AccountRuntime
from vault.engine.tempfiles import ensure_root, sweep, sweep_all
from vault.engine.worker import TransferWorker, WorkerPoolState
from vault.health import touch
from vault.logging import configure_logging, get_logger
from vault.queue.redis_queue import JobQueue, WeightedPicker, make_redis
from vault.scheduler.main import run_forever as run_scheduler
from vault.security.crypto import Hasher, SessionCipher
from vault.telegram.delivery import AiogramDelivery

log = get_logger(__name__)


async def _periodic(name: str, interval: float, fn: Callable[[], Awaitable[object]]) -> None:
    while True:
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("periodic_error", task=name, error_code=type(exc).__name__)
        await asyncio.sleep(interval)


async def main() -> None:
    s = settings()
    configure_logging("engine", s.log_level)
    s.require_for(Component.ENGINE)
    assert s.bot_token is not None

    db = Database.from_settings(s)
    redis = make_redis(s.redis_url)
    queue = JobQueue(redis)
    bot = Bot(s.bot_token.get_secret_value())
    delivery = AiogramDelivery(bot)
    cipher = SessionCipher(s.encryption_keys(), s.active_key_id())
    bot_user_id = int(s.bot_token.get_secret_value().split(":", 1)[0])

    temp_root = ensure_root(s.temp_dir)
    removed = sweep_all(temp_root)  # anything left belongs to a crashed previous run
    log.info("engine_starting", shard=s.engine_shard, temp_leftovers_removed=removed)

    runtime = AccountRuntime(
        db=db, queue=queue, settings=s, cipher=cipher, bot_user_id=bot_user_id, notifier=delivery
    )
    runtime.start_background()
    await runtime.sync()

    pool = WorkerPoolState(per_user_limit=s.per_user_concurrency)
    picker = WeightedPicker(s.premium_weight, s.free_weight)
    workers = [
        TransferWorker(
            worker_id=f"s{s.engine_shard}-w{i}",
            db=db,
            queue=queue,
            sources=runtime,
            delivery=delivery,
            settings=s,
            pool=pool,
            picker=picker,
            temp_root=temp_root,
        )
        for i in range(s.worker_concurrency)
    ]
    rpc = RpcServer(redis, runtime, s.engine_shard)
    hasher = Hasher(s.app_secret_key.get_secret_value()) if s.app_secret_key else None

    async def recovery() -> None:
        await recover_once(db, queue, s)

    async def housekeeping() -> None:
        sweep(temp_root, s.temp_max_age_seconds)
        touch("engine")

    tasks = [asyncio.create_task(w.run_forever()) for w in workers]
    tasks += [
        asyncio.create_task(rpc.serve_forever()),
        asyncio.create_task(_periodic("recovery", s.reconcile_interval_seconds, recovery)),
        asyncio.create_task(_periodic("sync", 60, runtime.sync)),
        asyncio.create_task(_periodic("housekeeping", 10, housekeeping)),
    ]
    if s.engine_hosts_login and s.engine_shard == 0:
        assert hasher is not None and s.telegram_api_id is not None and s.telegram_api_hash is not None
        api_id, api_hash = s.telegram_api_id, s.telegram_api_hash.get_secret_value()
        chat_login = ChatLoginService(
            settings=s,
            db=db,
            redis=redis,
            cipher=cipher,
            hasher=hasher,
            login_factory=lambda: TelethonLoginClient(api_id, api_hash),
        )
        tasks.append(asyncio.create_task(chat_login.serve_forever()))
    if s.engine_hosts_scheduler and s.engine_shard == 0:
        tasks.append(asyncio.create_task(run_scheduler(db, delivery, s, component="engine-scheduler")))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    log.info("engine_stopping")
    for w in workers:
        w.stop()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await runtime.shutdown()
    await bot.session.close()
    await redis.aclose()
    await db.dispose()
    # Graceful stop: remove any remaining temp payloads.
    sweep_all(Path(s.temp_dir))


if __name__ == "__main__":
    asyncio.run(main())
