"""Connected Telegram account lifecycle (connect / disconnect)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vault.config import Settings
from vault.db.models import (
    AccountStatus,
    ChatStatus,
    EncryptedSession,
    JobState,
    MediaJob,
    MonitoredChat,
    TelegramAccount,
    User,
)
from vault.domain.jobs import SkipReason
from vault.domain.plans import limits_for
from vault.domain.subscriptions import current_tier
from vault.security.crypto import SessionCipher, session_aad


class IdentityMismatch(Exception):
    """The authenticated MTProto account is not the bot user who started the flow."""


def shard_for(user_id: int, shards: int) -> int:
    return user_id % shards


async def store_connected_account(
    session: AsyncSession,
    cipher: SessionCipher,
    user: User,
    tg_user_id: int,
    session_string: str,
    now: datetime,
    s: Settings,
) -> TelegramAccount:
    # Identity binding: Bot API user ids and MTProto user ids are the same space.
    if tg_user_id != user.telegram_user_id:
        raise IdentityMismatch()
    acc = await session.scalar(
        select(TelegramAccount).where(TelegramAccount.user_id == user.id).with_for_update()
    )
    if acc is None:
        acc = TelegramAccount(
            user_id=user.id,
            tg_user_id=tg_user_id,
            status=AccountStatus.ACTIVE,
            shard=shard_for(user.id, s.engine_shards),
            connected_at=now,
        )
        session.add(acc)
        await session.flush()
    else:
        acc.tg_user_id = tg_user_id
        acc.status = AccountStatus.ACTIVE
        acc.connected_at = now
        acc.disconnected_at = None
        acc.last_error_code = None
    sealed = cipher.seal(session_string, session_aad(acc.id, tg_user_id))
    row = await session.scalar(select(EncryptedSession).where(EncryptedSession.account_id == acc.id))
    if row is None:
        session.add(
            EncryptedSession(
                account_id=acc.id,
                key_id=sealed.key_id,
                nonce=sealed.nonce,
                ciphertext=sealed.ciphertext,
                created_at=now,
            )
        )
    else:
        row.key_id, row.nonce, row.ciphertext, row.created_at = (
            sealed.key_id,
            sealed.nonce,
            sealed.ciphertext,
            now,
        )

    # Restore chats paused by a previous disconnect, within the current plan limit.
    chats = (
        await session.scalars(
            select(MonitoredChat).where(MonitoredChat.user_id == user.id).order_by(MonitoredChat.created_at)
        )
    ).all()
    limit = limits_for(await current_tier(session, user.id, now), s).max_chats
    active = sum(1 for c in chats if c.status == ChatStatus.ACTIVE)
    for c in chats:
        c.account_id = acc.id
        if c.status == ChatStatus.PAUSED_ACCOUNT:
            if active < limit:
                c.status = ChatStatus.ACTIVE
                active += 1
            else:
                c.status = ChatStatus.PAUSED_TIER
    user.destination_ok = True
    return acc


async def disconnect_account(session: AsyncSession, user_id: int, now: datetime) -> TelegramAccount | None:
    """Stop monitoring immediately. The engine revokes the Telegram authorization and
    deletes the ciphertext; the scheduler purges the ciphertext as a fallback."""
    acc = await session.scalar(
        select(TelegramAccount).where(TelegramAccount.user_id == user_id).with_for_update()
    )
    if acc is None or acc.status == AccountStatus.DISCONNECTED:
        return None
    acc.status = AccountStatus.DISCONNECTED
    acc.disconnected_at = now
    await session.execute(
        update(MonitoredChat)
        .where(MonitoredChat.user_id == user_id, MonitoredChat.status == ChatStatus.ACTIVE)
        .values(status=ChatStatus.PAUSED_ACCOUNT)
    )
    await session.execute(
        update(MediaJob)
        .where(
            MediaJob.user_id == user_id,
            MediaJob.state.in_([JobState.QUEUED, JobState.RETRY_WAIT, JobState.DISCOVERED]),
        )
        .values(state=JobState.SKIPPED, skip_reason=SkipReason.ACCOUNT_INACTIVE, finished_at=now)
    )
    return acc
