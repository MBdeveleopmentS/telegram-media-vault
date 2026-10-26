"""Media detection, entitlement checks and filename hygiene.

The Telethon adapter (``vault.telegram.mtproto``) converts raw MTProto messages
into the backend-agnostic ``IncomingMedia`` dataclass below so that all
decisions here are pure and unit-testable.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from vault.db.models import MediaKind
from vault.domain.jobs import SkipReason


@dataclass(frozen=True)
class DocumentInfo:
    id: int
    mime_type: str | None
    size: int | None
    file_name: str | None = None
    is_video_attr: bool = False
    is_round: bool = False
    is_animated: bool = False  # GIF-style animation
    is_sticker: bool = False
    is_voice: bool = False


@dataclass(frozen=True)
class PhotoInfo:
    id: int
    size: int | None  # bytes of the largest size, if known


@dataclass(frozen=True)
class IncomingMedia:
    chat_id: int
    message_id: int
    date: datetime | None
    grouped_id: int | None = None
    noforwards: bool = False
    photo: PhotoInfo | None = None
    document: DocumentInfo | None = None


@dataclass(frozen=True)
class Classified:
    kind: MediaKind
    media_key: str
    mime_type: str | None
    size: int | None


VIDEO_MIME_RE = re.compile(r"^video/[a-z0-9.+-]+$")
IMAGE_MIME_RE = re.compile(r"^image/[a-z0-9.+-]+$")
# Types that Telegram renders natively as video in sendVideo.
NATIVE_VIDEO_MIMES = frozenset({"video/mp4"})


def classify(m: IncomingMedia) -> Classified | None:
    """Return the supported media item in the message, or None if not a photo/video."""
    if m.photo is not None:
        return Classified(MediaKind.PHOTO, f"p:{m.photo.id}", "image/jpeg", m.photo.size)
    d = m.document
    if d is None:
        return None
    if d.is_sticker or d.is_voice or d.is_animated:
        return None
    mime = (d.mime_type or "").lower().strip()
    key = f"d:{d.id}"
    if d.is_video_attr or VIDEO_MIME_RE.match(mime):
        if d.is_video_attr and mime in NATIVE_VIDEO_MIMES:
            return Classified(MediaKind.VIDEO, key, mime, d.size)
        if VIDEO_MIME_RE.match(mime):
            return Classified(MediaKind.DOCUMENT_VIDEO, key, mime, d.size)
        return None
    if IMAGE_MIME_RE.match(mime):
        return Classified(MediaKind.DOCUMENT_IMAGE, key, mime, d.size)
    return None


def precheck(m: IncomingMedia, c: Classified, max_bytes: int) -> str | None:
    """Return a skip reason if the item must not be downloaded, else None."""
    if m.noforwards:
        # Respect Telegram's protected-content restriction: never copy it out.
        return SkipReason.PROTECTED
    if c.size is not None and c.size > max_bytes:
        return SkipReason.TOO_LARGE
    return None


_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
    "video/3gpp": ".3gp",
    "video/x-msvideo": ".avi",
}
_SAFE_CHARS_RE = re.compile(r"[^\w.\- ]+", re.UNICODE)


def safe_upload_name(original: str | None, mime: str | None, job_id: int) -> str:
    """Filename shown to the user in Telegram. Never used as a local path.

    Strips directory components, control characters, RTL overrides and
    anything outside a conservative allow-list; forces an extension that
    matches the declared MIME type to avoid misleading double extensions.
    """
    ext = _EXT_BY_MIME.get((mime or "").lower(), ".bin")
    stem = ""
    if original:
        base = original.replace("\\", "/").rsplit("/", 1)[-1]
        base = "".join(ch for ch in base if unicodedata.category(ch)[0] != "C")  # drops controls + bidi
        base = _SAFE_CHARS_RE.sub("_", base).strip(" ._")
        stem = base.rsplit(".", 1)[0] if "." in base else base
        stem = stem.replace(".", "_")[:60].strip(" ._")
    if not stem:
        stem = f"media_{job_id}"
    return f"{stem}{ext}"
