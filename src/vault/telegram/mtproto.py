"""Telethon (MTProto) adapter for one connected user account.

All Telethon specifics live here; the rest of the codebase works with
``IncomingMedia``, ``DialogInfo`` and the errors in ``vault.telegram.errors``.

Important: an MTProto user session is *not* chat-scoped. The application
restricts itself to approved chats in ``vault.engine.ingest``; this adapter
must never be called with a chat that hasn't been verified for the account.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telethon import TelegramClient, errors, utils
from telethon.sessions import StringSession
from telethon.tl import types

from vault.db.models import ChatType
from vault.domain.media import DocumentInfo, IncomingMedia, PhotoInfo, classify
from vault.telegram.errors import (
    SourceChatInaccessible,
    SourceError,
    SourceFloodWait,
    SourceGone,
    SourceProtected,
    SourceSessionInvalid,
    SourceTooLarge,
)

DEVICE_MODEL = "Media Vault"
APP_VERSION = "0.1.0"

SESSION_INVALID_ERRORS: tuple[type[BaseException], ...] = (
    errors.AuthKeyUnregisteredError,
    errors.SessionRevokedError,
    errors.UserDeactivatedError,
    errors.UserDeactivatedBanError,
    errors.AuthKeyDuplicatedError,
    errors.AuthKeyInvalidError,
)
CHAT_INACCESSIBLE_ERRORS: tuple[type[BaseException], ...] = (
    errors.ChannelPrivateError,
    errors.ChannelInvalidError,
    errors.PeerIdInvalidError,
    errors.ChatForbiddenError,
)


def make_client(session_string: str, api_id: int, api_hash: str) -> TelegramClient:
    return TelegramClient(
        StringSession(session_string),
        api_id,
        api_hash,
        device_model=DEVICE_MODEL,
        app_version=APP_VERSION,
        system_version="server",
        flood_sleep_threshold=0,  # never sleep silently inside handlers; we schedule retries
        receive_updates=True,
    )


@dataclass(frozen=True)
class DialogInfo:
    chat_id: int
    access_hash: int | None
    chat_type: ChatType
    title: str
    last_message_id: int
    protected: bool


def map_error(exc: BaseException) -> SourceError:
    if isinstance(exc, SourceError):
        return exc
    if isinstance(exc, (errors.FloodWaitError, errors.FloodPremiumWaitError)):
        return SourceFloodWait(float(getattr(exc, "seconds", 30)))
    if isinstance(exc, SESSION_INVALID_ERRORS):
        return SourceSessionInvalid()
    if isinstance(exc, CHAT_INACCESSIBLE_ERRORS):
        return SourceChatInaccessible()
    return SourceError(type(exc).__name__)


# ----------------------------------------------------------------- conversion
def _largest_photo_size(photo: types.Photo) -> tuple[Any, int | None]:
    best: Any = None
    best_bytes: int | None = None
    for size in photo.sizes:
        if isinstance(size, types.PhotoSizeProgressive):
            nbytes: int | None = max(size.sizes) if size.sizes else None
        elif isinstance(size, types.PhotoSize):
            nbytes = size.size
        else:
            continue  # stripped/cached/empty thumbnails
        if best is None or (nbytes or 0) >= (best_bytes or 0):
            best, best_bytes = size, nbytes
    return best, best_bytes


def to_incoming(message: Any, chat_protected: bool = False) -> IncomingMedia:
    media = getattr(message, "media", None)
    photo: PhotoInfo | None = None
    document: DocumentInfo | None = None
    if isinstance(media, types.MessageMediaPhoto) and isinstance(media.photo, types.Photo):
        _, nbytes = _largest_photo_size(media.photo)
        photo = PhotoInfo(id=media.photo.id, size=nbytes)
    elif isinstance(media, types.MessageMediaDocument) and isinstance(media.document, types.Document):
        doc = media.document
        file_name = None
        is_video = is_round = is_animated = is_sticker = is_voice = False
        for attr in doc.attributes:
            if isinstance(attr, types.DocumentAttributeFilename):
                file_name = attr.file_name
            elif isinstance(attr, types.DocumentAttributeVideo):
                is_video = True
                is_round = bool(attr.round_message)
            elif isinstance(attr, types.DocumentAttributeAnimated):
                is_animated = True
            elif isinstance(attr, types.DocumentAttributeSticker):
                is_sticker = True
            elif isinstance(attr, types.DocumentAttributeAudio):
                is_voice = bool(attr.voice)
        document = DocumentInfo(
            id=doc.id,
            mime_type=doc.mime_type,
            size=doc.size,
            file_name=file_name,
            is_video_attr=is_video,
            is_round=is_round,
            is_animated=is_animated,
            is_sticker=is_sticker,
            is_voice=is_voice,
        )
    return IncomingMedia(
        chat_id=utils.get_peer_id(message.peer_id),
        message_id=message.id,
        date=message.date,
        grouped_id=getattr(message, "grouped_id", None),
        noforwards=bool(getattr(message, "noforwards", False)) or chat_protected,
        photo=photo,
        document=document,
    )


def input_peer(chat_id: int, access_hash: int | None) -> Any:
    real_id, peer_type = utils.resolve_id(chat_id)
    if peer_type is types.PeerUser:
        return types.InputPeerUser(real_id, access_hash or 0)
    if peer_type is types.PeerChat:
        return types.InputPeerChat(real_id)
    return types.InputPeerChannel(real_id, access_hash or 0)


def entity_chat_type(entity: Any) -> ChatType | None:
    if isinstance(entity, types.User):
        return ChatType.PRIVATE
    if isinstance(entity, types.Chat):
        return ChatType.GROUP
    if isinstance(entity, types.Channel):
        return ChatType.SUPERGROUP if entity.megagroup else ChatType.CHANNEL
    return None  # forbidden/left chats


def _open_exclusive(path: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags, 0o600)


# ----------------------------------------------------------------- account
class TelethonAccount:
    """Operations performed with one user's authorized MTProto client."""

    def __init__(self, client: TelegramClient, bot_user_id: int) -> None:
        self.client = client
        self.bot_user_id = bot_user_id

    async def fetch_message(self, chat_id: int, access_hash: int | None, message_id: int) -> Any:
        try:
            msg = await self.client.get_messages(input_peer(chat_id, access_hash), ids=message_id)
        except Exception as exc:
            raise map_error(exc) from None
        if msg is None or isinstance(msg, types.MessageEmpty) or getattr(msg, "media", None) is None:
            raise SourceGone()
        return msg

    async def download(
        self,
        chat_id: int,
        access_hash: int | None,
        message_id: int,
        media_key: str,
        dest: Path,
        max_bytes: int,
    ) -> tuple[int, str | None]:
        """Re-fetch the message (fresh file_reference) and stream it to ``dest``.

        Returns (bytes_written, original_filename). Enforces ``max_bytes`` while
        streaming, so unknown-size media cannot exceed the entitlement.
        """
        msg = await self.fetch_message(chat_id, access_hash, message_id)
        incoming = to_incoming(msg, chat_protected=bool(getattr(msg.chat, "noforwards", False)))
        if incoming.noforwards:
            raise SourceProtected()
        classified = classify(incoming)
        if classified is None or classified.media_key != media_key:
            raise SourceGone()
        if classified.size is not None and classified.size > max_bytes:
            raise SourceTooLarge()

        media = msg.media
        if isinstance(media, types.MessageMediaPhoto):
            size, nbytes = _largest_photo_size(media.photo)
            location: Any = types.InputPhotoFileLocation(
                id=media.photo.id,
                access_hash=media.photo.access_hash,
                file_reference=media.photo.file_reference,
                thumb_size=size.type,
            )
            dc_id = media.photo.dc_id
            file_name = None
        else:
            doc = media.document
            location = types.InputDocumentFileLocation(
                id=doc.id, access_hash=doc.access_hash, file_reference=doc.file_reference, thumb_size=""
            )
            dc_id, nbytes = doc.dc_id, doc.size
            file_name = incoming.document.file_name if incoming.document else None

        written = 0
        fd = _open_exclusive(dest)
        try:
            with os.fdopen(fd, "wb") as fh:
                async for chunk in self.client.iter_download(location, dc_id=dc_id, file_size=nbytes):
                    written += len(chunk)
                    if written > max_bytes:
                        raise SourceTooLarge()
                    fh.write(chunk)
        except SourceError:
            raise
        except Exception as exc:
            raise map_error(exc) from None
        return written, file_name

    async def list_dialogs(self, limit: int = 400) -> list[DialogInfo]:
        out: list[DialogInfo] = []
        try:
            async for d in self.client.iter_dialogs(limit=limit):
                info = self._dialog_info(d.entity, d.message.id if d.message else 0, d.name)
                if info is not None:
                    out.append(info)
        except Exception as exc:
            raise map_error(exc) from None
        return out

    def _dialog_info(self, entity: Any, last_id: int, name: str | None) -> DialogInfo | None:
        ctype = entity_chat_type(entity)
        if ctype is None:
            return None
        chat_id = utils.get_peer_id(entity)
        # Never allow monitoring the bot conversation itself (delivery loop).
        if ctype == ChatType.PRIVATE and chat_id == self.bot_user_id:
            return None
        return DialogInfo(
            chat_id=chat_id,
            access_hash=getattr(entity, "access_hash", None),
            chat_type=ctype,
            title=(name or utils.get_display_name(entity) or "—")[:128],
            last_message_id=last_id,
            protected=bool(getattr(entity, "noforwards", False)),
        )

    async def resolve_dialog(self, chat_id: int) -> DialogInfo | None:
        """Verify ``chat_id`` is one of this account's dialogs (ownership check)."""
        for d in await self.list_dialogs(limit=1000):
            if d.chat_id == chat_id:
                return d
        return None

    async def iter_since(
        self, chat_id: int, access_hash: int | None, min_id: int, limit: int
    ) -> AsyncIterator[IncomingMedia]:
        """Catch-up for messages that arrived while the service was offline."""
        try:
            async for msg in self.client.iter_messages(
                input_peer(chat_id, access_hash), min_id=min_id, limit=limit, reverse=True
            ):
                if getattr(msg, "media", None) is not None:
                    yield to_incoming(msg)
        except Exception as exc:
            raise map_error(exc) from None
