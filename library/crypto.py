"""At-rest encryption for secrets (MFA, SSO client secrets, webhook secrets).
 
Uses Fernet (AES-128-CBC + HMAC) with a key derived from ``FIELD_ENCRYPTION_KEY``
or ``SECRET_KEY``. Legacy XOR (``enc1:``) and plaintext values decrypt for
migration; new writes always use ``enc2:``.
"""

from __future__ import annotations

import base64
import hashlib

from django.conf import settings

_ENC2 = "enc2:"
_ENC1 = "enc1:"


def _fernet():
    from cryptography.fernet import Fernet

    material = (
        getattr(settings, "FIELD_ENCRYPTION_KEY", "") or settings.SECRET_KEY
    ).encode("utf-8")
    digest = hashlib.sha256(material).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_value(plaintext: str) -> str:
    if not plaintext:
        return plaintext
    return _ENC2 + _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_value(stored: str, *, allow_plaintext: bool = True) -> str:
    if not stored:
        return stored
    if stored.startswith(_ENC2):
        return _fernet().decrypt(stored[len(_ENC2) :].encode("ascii")).decode("utf-8")
    if stored.startswith(_ENC1):
        return _decrypt_legacy_xor(stored)
    if allow_plaintext and not getattr(settings, "DISALLOW_PLAINTEXT_SECRETS", False):
        return stored
    raise ValueError("Refusing to read plaintext or unknown secret encoding.")


def _decrypt_legacy_xor(stored: str) -> str:
    """Migrate-path decoder for the former SECRET_KEY keystream XOR scheme."""
    raw = base64.urlsafe_b64decode(stored[len(_ENC1) :].encode("ascii"))
    nonce, ciphertext = raw[:12], raw[12:]
    key = settings.SECRET_KEY.encode("utf-8")
    out = bytearray()
    counter = 0
    while len(out) < len(ciphertext):
        out += hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        counter += 1
    keystream = bytes(out[: len(ciphertext)])
    return bytes(a ^ b for a, b in zip(ciphertext, keystream, strict=True)).decode("utf-8")
