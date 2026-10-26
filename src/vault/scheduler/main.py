"""Scheduler process: subscription reminders/downgrades, digests, retention."""

from __future__ import annotations

import asyncio

from aiogram import Bot

from vault.config import Component, Settings, settings
from vault.db.models import utcnow
from vault.db.session import Database
from vault.health import touch
from vault.logging import configure_logging, get_logger
from vault.scheduler import tasks
from vault.telegram.delivery import AiogramDelivery, MediaDelivery

log = get_logger(__name__)


async def run_forever(
    db: Database, notifier: MediaDelivery, s: Settings, component: str = "scheduler"
) -> None:
    """Subscription lifecycle, digests and retention, once a minute. Hosted by the
    engine process in the default deployment (saves a Python process)."""
    tick = 0
    while True:
        now = utcnow()
        for name, coro in (
            ("subscriptions", tasks.subscription_lifecycle(db, notifier, s, now)),
            ("digests", tasks.issue_digests(db, notifier, s, now)),
        ):
            try:
                await coro
            except Exception as exc:
                log.error("scheduler_task_failed", task=name, error_code=type(exc).__name__)
        if tick % 10 == 0:
            try:
                await tasks.retention(db, now)
            except Exception as exc:
                log.error("scheduler_task_failed", task="retention", error_code=type(exc).__name__)
        touch(component)
        tick += 1
        await asyncio.sleep(60)


async def main() -> None:
    """Standalone scheduler process (optional; only for split deployments)."""
    s = settings()
    configure_logging("scheduler", s.log_level)
    s.require_for(Component.SCHEDULER)
    assert s.bot_token is not None
    db = Database.from_settings(s)
    bot = Bot(s.bot_token.get_secret_value())
    try:
        await run_forever(db, AiogramDelivery(bot), s)
    finally:
        await bot.session.close()
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
