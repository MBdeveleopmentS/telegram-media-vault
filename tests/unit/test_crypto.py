import base64
import os

import pytest

from vault.security.crypto import DecryptionError, Hasher, SealedSession, SessionCipher, session_aad


def key() -> bytes:
    return os.urandom(32)


def test_roundtrip_and_nonce_uniqueness():
    c = SessionCipher({"k1": key()}, "k1")
    aad = session_aad(1, 100)
    a, b = c.seal("secret-session", aad), c.seal("secret-session", aad)
    assert a.nonce != b.nonce and a.ciphertext != b.ciphertext
    assert b"secret-session" not in a.ciphertext
    assert c.open(a, aad) == "secret-session"


def test_tampering_detected():
    c = SessionCipher({"k1": key()}, "k1")
    aad = session_aad(1, 100)
    s = c.seal("x", aad)
    bad = SealedSession(s.key_id, s.nonce, bytes([s.ciphertext[0] ^ 1]) + s.ciphertext[1:])
    with pytest.raises(DecryptionError):
        c.open(bad, aad)


def test_ciphertext_bound_to_account_row():
    """A DB attacker cannot move user A's ciphertext onto user B's account row."""
    c = SessionCipher({"k1": key()}, "k1")
    s = c.seal("A-session", session_aad(1, 100))
    with pytest.raises(DecryptionError):
        c.open(s, session_aad(2, 200))


def test_wrong_key_fails():
    s = SessionCipher({"k1": key()}, "k1").seal("x", b"aad")
    with pytest.raises(DecryptionError):
        SessionCipher({"k1": key()}, "k1").open(s, b"aad")


def test_key_rotation():
    k1, k2 = key(), key()
    old = SessionCipher({"k1": k1}, "k1").seal("x", b"a")
    new_cipher = SessionCipher({"k1": k1, "k2": k2}, "k2")
    assert new_cipher.open(old, b"a") == "x"
    assert new_cipher.needs_rotation(old)
    assert not new_cipher.needs_rotation(new_cipher.seal("x", b"a"))


def test_unknown_key_id():
    c = SessionCipher({"k1": key()}, "k1")
    with pytest.raises(DecryptionError):
        c.open(SealedSession("k9", b"0" * 12, b"x" * 32), b"a")


def test_bad_key_length_rejected():
    with pytest.raises(ValueError):
        SessionCipher({"k1": b"short"}, "k1")


def test_hasher_is_keyed_and_purpose_separated():
    h1 = Hasher("a" * 40)
    h2 = Hasher("b" * 40)
    assert h1.hash("phone", "972501234567") != h2.hash("phone", "972501234567")
    assert h1.hash("phone", "x") != h1.hash("ip", "x")
    assert h1.verify("phone", "x", h1.hash("phone", "x"))


def test_settings_key_parsing(settings):
    keys = settings.encryption_keys()
    assert list(keys) == ["k1"] and len(keys["k1"]) == 32
    raw = base64.urlsafe_b64encode(os.urandom(16)).decode()
    from vault.config import ConfigError, Settings

    with pytest.raises(ConfigError):
        Settings(session_encryption_keys=f"k1:{raw}").encryption_keys()
