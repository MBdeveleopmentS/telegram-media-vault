"""Bot API delivery into the user's private conversation with the bot."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from vault.db.models import MediaKind

# Bot API limits (https://core.telegram.org/bots/api#sendphoto / #senddocument):
# photos up to 10 MB via sendPhoto, other files up to 50 MB via multipart upload.
BOT_PHOTO_MAX_BYTES = 10 * 1000 * 1000
BOT_UPLOAD_MAX_BYTES = 50 * 1000 * 1000


class DeliveryError(Exception):
    code = "delivery_error"
    retryable = True


class DestinationUnavailable(DeliveryError):
    """User blocked the bot or never started it. Not retryable until the user returns."""

    code = "destination_unavailable"
    retryable = False


class DeliveryRateLimited(DeliveryError):
    code = "bot_rate_limited"

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"retry after {retry_after}s")
        self.retry_after = retry_after


class DeliveryRejected(DeliveryError):
    """Telegram rejected the content (bad request). Not retryable."""

    code = "delivery_rejected"
    retryable = False


@dataclass(frozen=True)
class DeliveryResult:
    message_id: int
    file_unique_id: str | None
    sent_as: str


@dataclass(frozen=True)
class OutgoingMedia:
    path: Path
    kind: MediaKind
    filename: str
    size: int
    caption: str


def choose_send_method(kind: MediaKind, size: int) -> str:
    """Preserve type without transcoding; fall back to document where Bot API requires it."""
    if kind == MediaKind.PHOTO:
        return "photo" if size <= BOT_PHOTO_MAX_BYTES else "document"
    if kind == MediaKind.VIDEO:
        return "video"
    return "document"


class MediaDelivery(Protocol):
    async def send(self, destination_chat_id: int, media: OutgoingMedia) -> DeliveryResult: ...

    async def send_text(self, destination_chat_id: int, text: str) -> None: ...


class AiogramDelivery:
    """Real Bot API sender."""

    def __init__(self, bot: object) -> None:
        from aiogram import Bot

        assert isinstance(bot, Bot)
        self._bot = bot

    async def send(self, destination_chat_id: int, media: OutgoingMedia) -> DeliveryResult:
        from aiogram.exceptions import (
            TelegramBadRequest,
            TelegramForbiddenError,
            TelegramNetworkError,
            TelegramRetryAfter,
            TelegramServerError,
        )
        from aiogram.types import FSInputFile

        method = choose_send_method(media.kind, media.size)
        upload = FSInputFile(media.path, filename=media.filename)
        try:
            if method == "photo":
                msg = await self._bot.send_photo(destination_chat_id, upload, caption=media.caption)
                fuid = msg.photo[-1].file_unique_id if msg.photo else None
            elif method == "video":
                msg = await self._bot.send_video(
                    destination_chat_id, upload, caption=media.caption, supports_streaming=True
                )
                fuid = msg.video.file_unique_id if msg.video else None
            else:
                msg = await self._bot.send_document(
                    destination_chat_id, upload, caption=media.caption, disable_content_type_detection=True
                )
                fuid = msg.document.file_unique_id if msg.document else None
        except TelegramRetryAfter as e:
            raise DeliveryRateLimited(float(e.retry_after)) from None
        except TelegramForbiddenError:
            raise DestinationUnavailable() from None
        except TelegramBadRequest as e:
            if "chat not found" in str(e).lower():
                raise DestinationUnavailable() from None
            raise DeliveryRejected() from None
        except (TelegramNetworkError, TelegramServerError):
            raise DeliveryError() from None
        return DeliveryResult(msg.message_id, fuid, method)

    async def send_text(self, destination_chat_id: int, text: str) -> None:
        from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

        try:
            await self._bot.send_message(destination_chat_id, text)
        except TelegramForbiddenError:
            raise DestinationUnavailable() from None
        except TelegramRetryAfter as e:
            raise DeliveryRateLimited(float(e.retry_after)) from None
