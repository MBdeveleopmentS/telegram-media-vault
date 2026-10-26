"""Session encryption and keyed hashing.

- Sessions: AES-256-GCM (authenticated encryption), random 96-bit nonce per
  encryption, AAD bound to the owning account so ciphertexts cannot be moved
  between rows. Multiple key ids are supported for rotation.
- Hashing: HMAC-SHA256 with APP_SECRET_KEY for one-time tokens, browser
  cookies and rate-limit identifiers (phone numbers, IPs). Plain SHA-256 of a
  phone number would be brute-forceable; HMAC with a server secret is not
  without the key.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class DecryptionError(Exception):
    pass


@dataclass(frozen=True)
class SealedSession:
    key_id: str
    nonce: bytes
    ciphertext: bytes


def session_aad(account_id: int, tg_user_id: int) -> bytes:
    return f"vault-session:v1:{account_id}:{tg_user_id}".encode()


class SessionCipher:
    def __init__(self, keys: dict[str, bytes], active_key_id: str) -> None:
        if active_key_id not in keys:
            raise ValueError("active key id not in key set")
        for kid, key in keys.items():
            if len(key) != 32:
                raise ValueError(f"key {kid} must be 32 bytes")
        self._keys = dict(keys)
        self._active = active_key_id

    @property
    def active_key_id(self) -> str:
        return self._active

    def seal(self, plaintext: str, aad: bytes) -> SealedSession:
        nonce = os.urandom(12)
        ct = AESGCM(self._keys[self._active]).encrypt(nonce, plaintext.encode(), aad)
        return SealedSession(self._active, nonce, ct)

    def open(self, sealed: SealedSession, aad: bytes) -> str:
        key = self._keys.get(sealed.key_id)
        if key is None:
            raise DecryptionError("unknown key id")
        try:
            return AESGCM(key).decrypt(sealed.nonce, sealed.ciphertext, aad).decode()
        except InvalidTag as exc:
            raise DecryptionError("session ciphertext failed authentication") from exc

    def needs_rotation(self, sealed: SealedSession) -> bool:
        return sealed.key_id != self._active


class Hasher:
    def __init__(self, secret: str) -> None:
        if len(secret) < 32:
            raise ValueError("secret too short")
        self._key = secret.encode()

    def hash(self, purpose: str, value: str) -> str:
        return hmac.new(self._key, f"{purpose}\x00{value}".encode(), hashlib.sha256).hexdigest()

    def verify(self, purpose: str, value: str, digest: str) -> bool:
        return hmac.compare_digest(self.hash(purpose, value), digest)


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def generate_key_b64() -> str:
    import base64

    return base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
