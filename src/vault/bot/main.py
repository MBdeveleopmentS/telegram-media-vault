"""Bot process (aiogram). Holds BOT_TOKEN and APP_SECRET_KEY, never session keys."""

from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from vault.bot.deps import Deps
from vault.bot.handlers import add_chat, login, menu, payments
from vault.bot.middlewares import UpdateDedupeMiddleware, UserMiddleware
from vault.config import Component, Settings, settings
from vault.db.session import Database
from vault.engine.rpc import RpcClient
from vault.health import touch
from vault.logging import configure_logging, get_logger
from vault.payments.service import StarsProvider
from vault.queue.redis_queue import make_redis
from vault.security.crypto import Hasher

log = get_logger(__name__)

COMMANDS = [
    ("start", "תפריט ראשי"),
    ("connect", "חיבור חשבון"),
    ("chats", "הצ'אטים שלי"),
    ("add", "הוספת צ'אט"),
    ("remove", "הסרת צ'אט"),
    ("pause", "השהיית צ'אט"),
    ("resume", "חידוש צ'אט"),
    ("status", "סטטוס הגיבויים"),
    ("premium", "פרימיום"),
    ("privacy", "אבטחה ופרטיות"),
    ("disconnect", "ניתוק החשבון"),
    ("help", "עזרה"),
    ("terms", "תנאי שימוש"),
    ("paysupport", "תמיכה בתשלומים"),
    ("source", "קוד פתוח"),
]


def build_dispatcher(deps: Deps) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(UpdateDedupeMiddleware(deps))
    user_mw = UserMiddleware(deps)
    for observer in (dp.message, dp.callback_query, dp.pre_checkout_query):
        observer.outer_middleware(user_mw)
    # Order matters: payments and forwarded-message handling before the generic text handlers.
    dp.include_router(payments.router)
    dp.include_router(login.router)
    dp.include_router(add_chat.router)
    dp.include_router(menu.router)
    return dp


def build_deps(s: Settings, bot: Bot) -> Deps:
    assert s.app_secret_key is not None
    redis = make_redis(s.redis_url)
    return Deps(
        settings=s,
        db=Database.from_settings(s),
        redis=redis,
        rpc=RpcClient(redis),
        hasher=Hasher(s.app_secret_key.get_secret_value()),
        payments=StarsProvider(bot),
    )


async def _heartbeat() -> None:
    while True:
        touch("bot")
        await asyncio.sleep(15)


async def main() -> None:
    s = settings()
    configure_logging("bot", s.log_level)
    s.require_for(Component.BOT)
    assert s.bot_token is not None
    bot = Bot(s.bot_token.get_secret_value())  # no default parse_mode: plain text only
    deps = build_deps(s, bot)
    dp = build_dispatcher(deps)
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
    hb = asyncio.create_task(_heartbeat())
    try:
        if s.bot_mode == "webhook":
            await _run_webhook(s, bot, dp)
        else:
            await bot.delete_webhook(drop_pending_updates=False)
            log.info("bot_polling")
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        hb.cancel()
        await deps.redis.aclose()
        await deps.db.dispose()
        await bot.session.close()


async def _run_webhook(s: Settings, bot: Bot, dp: Dispatcher) -> None:
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
    from aiohttp import web

    assert s.webhook_secret is not None
    secret = s.webhook_secret.get_secret_value()
    # Telegram sends this value in X-Telegram-Bot-Api-Secret-Token; requests without it are rejected.
    await bot.set_webhook(
        f"{s.public_base_url}{s.webhook_path}",
        secret_token=secret,
        allowed_updates=dp.resolve_used_update_types(),
    )
    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret).register(app, path=s.webhook_path)
    setup_application(app, dp, bot=bot)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host="0.0.0.0", port=8081).start()  # noqa: S104 - container-internal port
    log.info("bot_webhook_listening")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
