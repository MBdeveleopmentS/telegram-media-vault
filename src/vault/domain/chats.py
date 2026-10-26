"""Monitored-chat management. Every query is scoped by ``user_id`` (tenant isolation)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vault.config import Settings
from vault.db.models import AccountStatus, ChatStatus, ChatType, MonitoredChat, TelegramAccount
from vault.domain.plans import limits_for
from vault.domain.subscriptions import current_tier


class ChatError(Exception):
    code = "chat_error"


class ChatLimitReached(ChatError):
    code = "limit_reached"


class ChatAlreadyMonitored(ChatError):
    code = "already_monitored"


class NoActiveAccount(ChatError):
    code = "no_account"


class ChatNotFound(ChatError):
    code = "not_found"


@dataclass(frozen=True)
class VerifiedChat:
    """A chat confirmed by the engine to be in *this user's* dialogs."""

    chat_id: int
    access_hash: int | None
    chat_type: ChatType
    title: str
    last_message_id: int


async def active_account(session: AsyncSession, user_id: int) -> TelegramAccount | None:
    return await session.scalar(
        select(TelegramAccount).where(
            TelegramAccount.user_id == user_id, TelegramAccount.status == AccountStatus.ACTIVE
        )
    )


async def count_active(session: AsyncSession, user_id: int) -> int:
    n = await session.scalar(
        select(func.count())
        .select_from(MonitoredChat)
        .where(MonitoredChat.user_id == user_id, MonitoredChat.status == ChatStatus.ACTIVE)
    )
    return int(n or 0)


async def list_chats(session: AsyncSession, user_id: int) -> list[MonitoredChat]:
    rows = await session.scalars(
        select(MonitoredChat).where(MonitoredChat.user_id == user_id).order_by(MonitoredChat.created_at)
    )
    return list(rows.all())


async def get_owned_chat(session: AsyncSession, user_id: int, chat_row_id: int) -> MonitoredChat:
    chat = await session.scalar(
        select(MonitoredChat).where(MonitoredChat.id == chat_row_id, MonitoredChat.user_id == user_id)
    )
    if chat is None:
        raise ChatNotFound()
    return chat


async def add_chat(
    session: AsyncSession, user_id: int, verified: VerifiedChat, now: datetime, s: Settings
) -> MonitoredChat:
    account = await active_account(session, user_id)
    if account is None:
        raise NoActiveAccount()
    existing = await session.scalar(
        select(MonitoredChat).where(
            MonitoredChat.user_id == user_id, MonitoredChat.chat_id == verified.chat_id
        )
    )
    tier = await current_tier(session, user_id, now)
    limit = limits_for(tier, s).max_chats
    if existing is not None and existing.status == ChatStatus.ACTIVE:
        raise ChatAlreadyMonitored()
    if await count_active(session, user_id) >= limit:
        raise ChatLimitReached()
    if existing is not None:
        existing.status = ChatStatus.ACTIVE
        existing.account_id = account.id
        existing.access_hash = verified.access_hash
        existing.title = verified.title[:128]
        existing.consent_at = now
        existing.last_seen_message_id = max(existing.last_seen_message_id, verified.last_message_id)
        return existing
    chat = MonitoredChat(
        user_id=user_id,
        account_id=account.id,
        chat_id=verified.chat_id,
        access_hash=verified.access_hash,
        chat_type=verified.chat_type,
        title=verified.title[:128] or "—",
        status=ChatStatus.ACTIVE,
        consent_at=now,
        # Start from "now": historical media is out of scope for V1.
        last_seen_message_id=verified.last_message_id,
        created_at=now,
    )
    session.add(chat)
    await session.flush()
    return chat


async def pause_chat(session: AsyncSession, user_id: int, chat_row_id: int) -> MonitoredChat:
    chat = await get_owned_chat(session, user_id, chat_row_id)
    if chat.status == ChatStatus.ACTIVE:
        chat.status = ChatStatus.PAUSED
    return chat


async def resume_chat(
    session: AsyncSession, user_id: int, chat_row_id: int, now: datetime, s: Settings
) -> MonitoredChat:
    chat = await get_owned_chat(session, user_id, chat_row_id)
    if chat.status == ChatStatus.ACTIVE:
        return chat
    if await active_account(session, user_id) is None:
        raise NoActiveAccount()
    tier = await current_tier(session, user_id, now)
    if await count_active(session, user_id) >= limits_for(tier, s).max_chats:
        raise ChatLimitReached()
    chat.status = ChatStatus.ACTIVE
    chat.consent_at = now
    return chat


async def remove_chat(session: AsyncSession, user_id: int, chat_row_id: int) -> None:
    """Deletes the chat config and (via FK cascade) its job metadata. Delivered media
    in the user's bot conversation is untouched."""
    chat = await get_owned_chat(session, user_id, chat_row_id)
    await session.delete(chat)


async def set_keep_on_free(
    session: AsyncSession, user_id: int, chat_row_id: int, keep: bool, s: Settings
) -> MonitoredChat:
    chat = await get_owned_chat(session, user_id, chat_row_id)
    if keep:
        n = await session.scalar(
            select(func.count())
            .select_from(MonitoredChat)
            .where(MonitoredChat.user_id == user_id, MonitoredChat.keep_on_free.is_(True))
        )
        if int(n or 0) >= s.free_max_chats and not chat.keep_on_free:
            raise ChatLimitReached()
    chat.keep_on_free = keep
    return chat
