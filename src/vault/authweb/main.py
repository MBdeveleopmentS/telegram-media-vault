"""Auth-web process entrypoint. Runs a single uvicorn worker (see PendingLogins)."""

from __future__ import annotations

import asyncio

import uvicorn
from aiogram import Bot
from fastapi import FastAPI

from vault import i18n
from vault.authweb.app import create_app
from vault.authweb.chat_login import ChatLoginService
from vault.authweb.login import TelethonLoginClient
from vault.bot import keyboards as kb
from vault.config import Component, settings
from vault.db.session import Database
from vault.logging import configure_logging, get_logger
from vault.queue.redis_queue import make_redis
from vault.security.crypto import Hasher, SessionCipher

log = get_logger(__name__)


def build() -> FastAPI:
    s = settings()
    configure_logging("authweb", s.log_level)
    s.require_for(Component.AUTHWEB)
    assert s.bot_token and s.app_secret_key and s.telegram_api_id and s.telegram_api_hash
    bot = Bot(s.bot_token.get_secret_value())
    api_id, api_hash = s.telegram_api_id, s.telegram_api_hash.get_secret_value()

    async def notifier(chat_id: int, kind: str, show_menu: bool) -> None:
        text = i18n.CONNECTED_OK if kind == "connected" else i18n.CONNECT_FAILED_IDENTITY
        try:
            await bot.send_message(chat_id, text, reply_markup=kb.main_menu(True) if show_menu else None)
        except Exception as exc:
            log.warning("notify_failed", error_code=type(exc).__name__)

    db = Database.from_settings(s)
    redis = make_redis(s.redis_url)
    cipher = SessionCipher(s.encryption_keys(), s.active_key_id())
    hasher = Hasher(s.app_secret_key.get_secret_value())
    chat_login = ChatLoginService(
        settings=s,
        db=db,
        redis=redis,
        cipher=cipher,
        hasher=hasher,
        login_factory=lambda: TelethonLoginClient(api_id, api_hash),
    )
    return create_app(
        settings=s,
        db=db,
        redis=redis,
        cipher=cipher,
        hasher=hasher,
        login_factory=lambda: TelethonLoginClient(api_id, api_hash),
        notifier=notifier,
        # Only one process may consume login requests (pending logins live in memory).
        background=[] if s.engine_hosts_login else [chat_login.serve_forever],
    )


def main() -> None:
    s = settings()
    config = uvicorn.Config(
        build(),
        host=s.authweb_bind_host,
        port=s.authweb_port,
        workers=1,
        access_log=False,
        proxy_headers=s.trust_proxy_headers,
        server_header=False,
        log_config=None,
    )
    asyncio.run(uvicorn.Server(config).serve())


if __name__ == "__main__":
    main()
