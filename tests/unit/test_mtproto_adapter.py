"""Telethon TL object conversion, using real Telethon types (offline)."""

from datetime import UTC, datetime

from telethon.tl import types

from vault.db.models import ChatType, MediaKind
from vault.domain.media import classify
from vault.telegram.mtproto import TelethonAccount, entity_chat_type, input_peer, to_incoming

D = datetime(2026, 1, 1, tzinfo=UTC)


def message(media, peer=None, **kw):
    return types.Message(id=10, peer_id=peer or types.PeerChannel(123), date=D, message="", media=media, **kw)


def photo(sizes):
    return types.Photo(id=55, access_hash=1, file_reference=b"r", date=D, sizes=sizes, dc_id=2)


def test_photo_largest_size():
    p = photo(
        [
            types.PhotoStrippedSize("i", b"x"),
            types.PhotoSize("m", 320, 320, 20_000),
            types.PhotoSizeProgressive("y", 1280, 1280, [10_000, 50_000, 900_000]),
        ]
    )
    inc = to_incoming(message(types.MessageMediaPhoto(photo=p)))
    assert inc.chat_id == -1000000000123 and inc.photo is not None and inc.photo.size == 900_000
    c = classify(inc)
    assert c is not None and c.kind == MediaKind.PHOTO


def doc(mime, attrs, size=1234):
    return types.Document(
        id=77,
        access_hash=1,
        file_reference=b"r",
        date=D,
        mime_type=mime,
        size=size,
        dc_id=2,
        attributes=attrs,
    )


def test_video_document():
    d = doc(
        "video/mp4", [types.DocumentAttributeVideo(5, 640, 480), types.DocumentAttributeFilename("a.mp4")]
    )
    inc = to_incoming(message(types.MessageMediaDocument(document=d)))
    c = classify(inc)
    assert c is not None and c.kind == MediaKind.VIDEO and inc.document.file_name == "a.mp4"  # type: ignore[union-attr]


def test_gif_and_sticker_ignored():
    gif = doc("video/mp4", [types.DocumentAttributeVideo(1, 1, 1), types.DocumentAttributeAnimated()])
    st = doc("image/webp", [types.DocumentAttributeSticker(alt="x", stickerset=types.InputStickerSetEmpty())])
    for d in (gif, st):
        assert classify(to_incoming(message(types.MessageMediaDocument(document=d)))) is None


def test_protected_message_and_chat():
    p = photo([types.PhotoSize("m", 1, 1, 1)])
    assert to_incoming(message(types.MessageMediaPhoto(photo=p), noforwards=True)).noforwards
    assert to_incoming(message(types.MessageMediaPhoto(photo=p)), chat_protected=True).noforwards


def test_input_peers():
    assert isinstance(input_peer(-1001234567890, 9), types.InputPeerChannel)
    assert isinstance(input_peer(-1234, None), types.InputPeerChat)
    assert isinstance(input_peer(1234, 9), types.InputPeerUser)


def test_entity_types_and_bot_chat_excluded():
    u = types.User(id=500, access_hash=1, first_name="Bot")
    ch = types.Channel(id=9, title="c", photo=types.ChatPhotoEmpty(), date=D, access_hash=3, megagroup=True)
    assert entity_chat_type(u) == ChatType.PRIVATE and entity_chat_type(ch) == ChatType.SUPERGROUP
    acct = TelethonAccount(client=None, bot_user_id=500)  # type: ignore[arg-type]
    assert acct._dialog_info(u, 1, "Bot") is None  # the destination bot can never be monitored
    info = acct._dialog_info(ch, 42, "c")
    assert info is not None and info.chat_id == -1000000000009 and info.last_message_id == 42
