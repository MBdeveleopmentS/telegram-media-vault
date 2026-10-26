from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery, TelegramObject, Update

from vault.bot.deps import Deps
from vault.domain.subscriptions import get_or_create_user
from vault.logging import get_logger

log = get_logger(__name__)


class UpdateDedupeMiddleware(BaseMiddleware):
    """Replay protection: each update_id is processed at most once (24h window)."""

    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Update):
            fresh = await self.deps.redis.set(f"vault:upd:{event.update_id}", 1, nx=True, ex=86400)
            if not fresh:
                log.warning("duplicate_update_dropped")
                return None
        return await handler(event, data)


class UserMiddleware(BaseMiddleware):
    """Private chats only; resolves (or creates) the internal user for the sender.

    The internal user is *always* derived from the authenticated update sender
    (``from_user.id``), never from user-supplied data, which is the root of
    tenant isolation in the bot.
    """

    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from_user = getattr(event, "from_user", None)
        if from_user is None or from_user.is_bot:
            return None
        if isinstance(event, Message) and event.chat.type != "private":
            return None
        if (
            isinstance(event, CallbackQuery)
            and event.message is not None
            and event.message.chat.type != "private"
        ):
            return None
        async with self.deps.db.transaction() as session:
            user = await get_or_create_user(session, from_user.id)
            if not user.destination_ok and not isinstance(event, PreCheckoutQuery):
                user.destination_ok = True  # the user is talking to us, so the chat is reachable
            data["db_user_id"] = user.id
        data["deps"] = self.deps
        return await handler(event, data)
