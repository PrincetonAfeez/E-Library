"""Staff multi-factor authentication: self-contained TOTP (RFC 6238)

Implemented with the standard library only (hmac/hashlib), so it works offline
and interoperates with any authenticator app (Google Authenticator, Authy, …)
via the standard ``otpauth://`` provisioning URI. No third-party OTP dependency.
Secrets at rest use Fernet via :mod:`library.crypto`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from django.utils import timezone

from .crypto import decrypt_value, encrypt_value
from .models import StaffTotpDevice
from .services import DomainError, audit_action

STEP_SECONDS = 30
DIGITS = 6


def encrypt_secret(plaintext: str) -> str:
    return encrypt_value(plaintext)


def decrypt_secret(stored: str) -> str:
    return decrypt_value(stored, allow_plaintext=False)


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
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
        f"&digits={DIGITS}&period={STEP_SECONDS}"
    )


def begin_enrollment(*, user, current_code: str | None = None) -> dict:
    """(Re)issue an unconfirmed TOTP secret and return its provisioning URI.

    If the user already has a confirmed device, ``current_code`` must verify
    against it (step-up) before the secret is rotated.
    """
    existing = StaffTotpDevice.objects.filter(user=user, confirmed_at__isnull=False).first()
    if existing is not None:
        if not current_code or not verify_code(decrypt_secret(existing.secret), current_code):
            raise DomainError("Enter a current MFA code to re-enroll.")
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


def disable_mfa(*, user, code: str, actor=None) -> None:
    device = StaffTotpDevice.objects.filter(user=user, confirmed_at__isnull=False).first()
    if device is None:
        StaffTotpDevice.objects.filter(user=user).delete()
        return
    if not verify_code(decrypt_secret(device.secret), code):
        raise DomainError("Enter a current MFA code to disable MFA.")
    StaffTotpDevice.objects.filter(user=user).delete()
    audit_action(action="mfa.disable", entity=user, actor=actor or user, source="mfa")


def mark_session_verified(request, *, organization=None) -> None:
    """Record a bound MFA verification on the session (user + org + time)."""
    request.session["mfa_verified"] = {
        "user_id": request.user.pk,
        "org_id": getattr(organization, "pk", None),
        "verified_at": timezone.now().isoformat(),
    }


def session_mfa_ok(request, *, organization=None, max_age_seconds: int = 12 * 3600) -> bool:
    """True when the session carries a fresh, identity-bound MFA verification."""
    payload = request.session.get("mfa_verified")
    if not payload:
        return False
    if not isinstance(payload, dict):
        # Legacy boolean flag — treat as invalid so users re-verify.
        return False
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated or payload.get("user_id") != user.pk:
        return False
    if organization is not None and payload.get("org_id") != organization.pk:
        return False
    verified_at = payload.get("verified_at") or ""
    try:
        from django.utils.dateparse import parse_datetime

        when = parse_datetime(verified_at)
        if when is None:
            return False
        if timezone.is_naive(when):
            when = timezone.make_aware(when, timezone.utc)
        if (timezone.now() - when).total_seconds() > max_age_seconds:
            return False
    except (TypeError, ValueError):
        return False
    return True
