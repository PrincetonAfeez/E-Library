"""Staff multi-factor authentication: self-contained TOTP (RFC 6238).

Implemented with the standard library only (hmac/hashlib), so it works offline
and interoperates with any authenticator app (Google Authenticator, Authy, …)
via the standard ``otpauth://`` provisioning URI. No third-party OTP dependency.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from django.conf import settings
from django.utils import timezone

from .models import StaffTotpDevice
from .services import DomainError, audit_action

STEP_SECONDS = 30
DIGITS = 6
_ENC_PREFIX = "enc1:"


# --------------------------------------------------------------------------- #
# At-rest encryption for the shared secret (SECRET_KEY-derived keystream).
# Removes plaintext exposure in a DB dump; no third-party dependency.
# --------------------------------------------------------------------------- #
def _keystream(nonce: bytes, length: int) -> bytes:
    key = settings.SECRET_KEY.encode("utf-8")
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:length])


def encrypt_secret(plaintext: str) -> str:
    nonce = secrets.token_bytes(12)
    data = plaintext.encode("utf-8")
    keystream = _keystream(nonce, len(data))
    ciphertext = bytes(a ^ b for a, b in zip(data, keystream, strict=True))
    return _ENC_PREFIX + base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def decrypt_secret(stored: str) -> str:
    if not stored.startswith(_ENC_PREFIX):
        return stored  # tolerate a legacy plaintext secret
    raw = base64.urlsafe_b64decode(stored[len(_ENC_PREFIX):].encode("ascii"))
    nonce, ciphertext = raw[:12], raw[12:]
    keystream = _keystream(nonce, len(ciphertext))
    return bytes(a ^ b for a, b in zip(ciphertext, keystream, strict=True)).decode("utf-8")


def generate_secret() -> str:
    """A base32 shared secret (no padding), suitable for authenticator apps."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _hotp(secret: str, counter: int, *, digits: int = DIGITS) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10**digits)).zfill(digits)


def totp(secret: str, *, timestamp: float | None = None, step: int = STEP_SECONDS) -> str:
    ts = int(timestamp if timestamp is not None else time.time())
    return _hotp(secret, ts // step)


def verify_code(secret: str, code: str, *, timestamp: float | None = None, window: int = 1) -> bool:
    """Verify a code against the current window ± ``window`` (clock-skew tolerance)."""
    code = (code or "").strip()
    if not code.isdigit():
        return False
    ts = int(timestamp if timestamp is not None else time.time())
    counter = ts // STEP_SECONDS
    return any(
        hmac.compare_digest(_hotp(secret, counter + offset), code)
        for offset in range(-window, window + 1)
    )


def provisioning_uri(secret: str, account: str, *, issuer: str = "E-Library") -> str:
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&digits={DIGITS}&period={STEP_SECONDS}"


# --------------------------------------------------------------------------- #
# Enrollment / verification service
# --------------------------------------------------------------------------- #
def begin_enrollment(*, user) -> dict:
    """(Re)issue an unconfirmed TOTP secret and return its provisioning URI."""
    secret = generate_secret()
    device, _ = StaffTotpDevice.objects.update_or_create(
        user=user, defaults={"secret": encrypt_secret(secret), "confirmed_at": None}
    )
    audit_action(action="mfa.enroll_begin", entity=device, actor=user, source="mfa")
    return {
        "secret": secret,
        "otpauth_uri": provisioning_uri(secret, user.get_username()),
    }


def confirm_enrollment(*, user, code: str) -> StaffTotpDevice:
    device = StaffTotpDevice.objects.filter(user=user).first()
    if device is None:
        raise DomainError("Start MFA enrollment first.")
    if not verify_code(decrypt_secret(device.secret), code):
        raise DomainError("That code is incorrect. Try again.")
    device.confirmed_at = timezone.now()
    device.last_verified_at = timezone.now()
    device.save(update_fields=["confirmed_at", "last_verified_at", "updated_at"])
    audit_action(action="mfa.enroll_confirm", entity=device, actor=user, source="mfa")
    return device


def user_has_mfa(user) -> bool:
    return StaffTotpDevice.objects.filter(user=user, confirmed_at__isnull=False).exists()


def verify_login(*, user, code: str) -> bool:
    """Verify a login-time code against the user's confirmed device."""
    device = StaffTotpDevice.objects.filter(user=user, confirmed_at__isnull=False).first()
    if device is None:
        return False
    if verify_code(decrypt_secret(device.secret), code):
        device.last_verified_at = timezone.now()
        device.save(update_fields=["last_verified_at", "updated_at"])
        return True
    return False


def disable_mfa(*, user, actor=None) -> None:
    StaffTotpDevice.objects.filter(user=user).delete()
    audit_action(
        action="mfa.disable", entity=user, actor=actor or user, source="mfa"
    )
