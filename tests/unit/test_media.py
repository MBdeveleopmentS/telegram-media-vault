import pytest

from vault.db.models import MediaKind
from vault.domain.jobs import SkipReason
from vault.domain.media import DocumentInfo, IncomingMedia, PhotoInfo, classify, precheck, safe_upload_name
from vault.telegram.delivery import BOT_PHOTO_MAX_BYTES, choose_send_method

MIB = 1024 * 1024


def msg(**kw) -> IncomingMedia:
    return IncomingMedia(chat_id=-100123, message_id=5, date=None, **kw)


def doc(**kw) -> DocumentInfo:
    base = dict(id=42, mime_type="video/mp4", size=1000)
    base.update(kw)
    return DocumentInfo(**base)


def test_photo():
    c = classify(msg(photo=PhotoInfo(id=7, size=1 * MIB)))
    assert c is not None and c.kind == MediaKind.PHOTO and c.media_key == "p:7" and c.size == MIB


def test_native_video():
    c = classify(msg(document=doc(is_video_attr=True)))
    assert c is not None and c.kind == MediaKind.VIDEO and c.media_key == "d:42"


@pytest.mark.parametrize("mime", ["video/quicktime", "video/x-matroska", "video/webm"])
def test_other_video_types_sent_as_document_without_transcoding(mime):
    c = classify(msg(document=doc(mime_type=mime, is_video_attr=True)))
    assert c is not None and c.kind == MediaKind.DOCUMENT_VIDEO


@pytest.mark.parametrize("mime", ["image/jpeg", "image/png", "image/heic", "image/webp"])
def test_images_sent_as_documents(mime):
    c = classify(msg(document=doc(mime_type=mime)))
    assert c is not None and c.kind == MediaKind.DOCUMENT_IMAGE


@pytest.mark.parametrize(
    "d",
    [
        doc(mime_type="image/webp", is_sticker=True),
        doc(mime_type="audio/ogg", is_voice=True),
        doc(mime_type="video/mp4", is_video_attr=True, is_animated=True),  # GIF
        doc(mime_type="application/pdf"),
        doc(mime_type="application/zip"),
        doc(mime_type=None),
    ],
)
def test_non_photo_video_ignored(d):
    assert classify(msg(document=d)) is None


def test_text_message_ignored():
    assert classify(msg()) is None


def test_size_entitlement():
    m = msg(photo=PhotoInfo(id=1, size=5 * MIB - 1))
    c = classify(m)
    assert c is not None
    assert precheck(m, c, 5 * MIB) is None
    m2 = msg(photo=PhotoInfo(id=1, size=5 * MIB + 1))
    c2 = classify(m2)
    assert c2 is not None
    assert precheck(m2, c2, 5 * MIB) == SkipReason.TOO_LARGE
    assert precheck(m2, c2, 15 * MIB) is None


def test_unknown_size_passes_precheck_but_is_enforced_at_download():
    m = msg(document=doc(size=None, is_video_attr=True))
    c = classify(m)
    assert c is not None and precheck(m, c, 5 * MIB) is None


def test_protected_content_is_skipped():
    m = msg(photo=PhotoInfo(id=1, size=10), noforwards=True)
    c = classify(m)
    assert c is not None and precheck(m, c, 5 * MIB) == SkipReason.PROTECTED


def test_large_photo_sent_as_document():
    assert choose_send_method(MediaKind.PHOTO, BOT_PHOTO_MAX_BYTES) == "photo"
    assert choose_send_method(MediaKind.PHOTO, BOT_PHOTO_MAX_BYTES + 1) == "document"
    assert choose_send_method(MediaKind.VIDEO, 10) == "video"
    assert choose_send_method(MediaKind.DOCUMENT_VIDEO, 10) == "document"


@pytest.mark.parametrize(
    ("original", "mime", "expected"),
    [
        ("../../etc/passwd", "image/jpeg", "passwd.jpg"),
        ("..\\..\\windows\\evil.exe", "video/mp4", "evil.mp4"),
        ("holiday.photo.exe", "image/png", "holiday_photo.png"),
        ("‮gnp.exe", "image/png", "gnp.png"),  # RTL override stripped
        ("a\x00b\nc.mp4", "video/mp4", "abc.mp4"),
        (None, "video/mp4", "media_9.mp4"),
        ("....", "image/jpeg", "media_9.jpg"),
        ("x" * 500 + ".mp4", "video/mp4", "x" * 60 + ".mp4"),
        ("חופשה בים.jpg", "image/jpeg", "חופשה בים.jpg"),
        ("file.mp4", "application/x-sh", "file.bin"),
    ],
)
def test_safe_upload_name(original, mime, expected):
    out = safe_upload_name(original, mime, 9)
    assert out == expected
    assert "/" not in out and "\\" not in out and ".." not in out
