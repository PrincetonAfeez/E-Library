"""OpenID Connect SSO: per-tenant connection, authorize URL, and callback

Trust root is the userinfo response over TLS to a validated token/userinfo URL.
ID tokens are not used for authorization decisions (signature/JWKS verification
is not implemented); nonce is still sent for IdP compatibility.
"""

from __future__ import annotations

import json
import secrets
import urllib.parse

from django.contrib.auth import get_user_model
from django.db import models
from django.utils.text import slugify

from .crypto import decrypt_value, encrypt_value
from .models import Branch, PatronProfile, SsoIdentity
from .net import safe_urlopen
from .services import DomainError

User = get_user_model()


def build_authorize_url(connection, *, redirect_uri: str, state: str, nonce: str | None = None) -> str:
    params = {
        "response_type": "code",
        "client_id": connection.client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid email profile",
        "state": state,
    }
    if nonce:
        params["nonce"] = nonce
    sep = "&" if "?" in connection.authorize_url else "?"
    return f"{connection.authorize_url}{sep}{urllib.parse.urlencode(params)}"


def _client_secret(connection) -> str:
    return decrypt_value(connection.client_secret or "")


def encrypt_client_secret(plaintext: str) -> str:
    return encrypt_value(plaintext) if plaintext else plaintext


def _default_exchange(connection, code: str, redirect_uri: str) -> dict:
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": connection.client_id,
            "client_secret": _client_secret(connection),
        }
    ).encode("utf-8")
    with safe_urlopen(
        connection.token_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
        timeout=8,
    ) as response:
        return json.loads(response.read())


def _default_userinfo(connection, access_token: str) -> dict:
    with safe_urlopen(
        connection.userinfo_url,
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
        timeout=8,
    ) as response:
        return json.loads(response.read())


def _email_verified(info: dict) -> bool:
    value = info.get("email_verified")
    return value in (True, "true", "True", "1", 1)


def _unique_username(seed: str) -> str:
    base = (seed.split("@")[0] or "user").strip() or "user"
    username = base
    while User.objects.filter(username=username).exists():
        username = f"{base}-{secrets.token_hex(3)}"
    return username


def _jit_card_number(connection, subject: str) -> str:
    """Return a stable, per-organization-unique card number for an SSO subject."""
    base = (slugify(subject) or "sso-user")[:52]
    candidate = f"SSO-{base}"[:64]
    suffix = 1
    while PatronProfile.objects.filter(
        organization=connection.organization, library_card_number=candidate
    ).exists():
        suffix += 1
        candidate = f"SSO-{base[: 64 - len(str(suffix)) - 1]}-{suffix}"
    return candidate


def _ensure_connection_membership(connection, user, subject: str) -> None:
    has_patron = PatronProfile.objects.filter(
        user=user, organization=connection.organization
    ).exists()
    has_staff = user.staff_memberships.filter(
        organization=connection.organization, active=True
    ).exists()
    if has_patron or has_staff:
        return
    branch = Branch.objects.filter(organization=connection.organization).order_by("name", "pk").first()
    if branch is None:
        raise DomainError("SSO requires a branch for this organization.")
    PatronProfile.objects.create(
        user=user,
        organization=connection.organization,
        library_card_number=_jit_card_number(connection, subject),
        home_branch=branch,
        notification_email=user.email,
    )


def handle_callback(
    connection,
    *,
    code: str,
    redirect_uri: str,
    exchange=None,
    fetch_userinfo=None,
    expected_nonce: str | None = None,  # retained for API compat; IdP may echo in id_token
):
    del expected_nonce  # trust userinfo over TLS; do not authorize from unverified id_token
    exchange = exchange or _default_exchange
    fetch_userinfo = fetch_userinfo or _default_userinfo

    tokens = exchange(connection, code, redirect_uri)
    access_token = tokens.get("access_token")
    if not access_token:
        raise DomainError("SSO token exchange failed.")
    info = fetch_userinfo(connection, access_token)
    subject = str(info.get("sub") or "").strip()
    email = (info.get("email") or "").strip()
    if not subject:
        raise DomainError("SSO response is missing a subject.")

    identity = (
        SsoIdentity.objects.filter(connection=connection, subject=subject)
        .select_related("user")
        .first()
    )
    if identity is not None:
        if not identity.user.is_active:
            raise DomainError("This account is disabled.")
        _ensure_connection_membership(connection, identity.user, subject)
        return identity.user

    user = None
    if email and _email_verified(info):
        user = (
            User.objects.filter(email__iexact=email)
            .filter(
                models.Q(patron_profile__organization=connection.organization)
                | models.Q(
                    staff_memberships__organization=connection.organization,
                    staff_memberships__active=True,
                )
            )
            .distinct()
            .first()
        )
        if user is not None and not user.is_active:
            raise DomainError("This account is disabled.")
    if user is None:
        # Never attach to a global email match outside this org (account takeover).
        # If that email is already taken elsewhere, omit it on the new user.
        email_to_store = email if _email_verified(info) else ""
        if email_to_store and User.objects.filter(email__iexact=email_to_store).exists():
            email_to_store = ""
        user = User.objects.create_user(
            username=_unique_username(email or subject),
            email=email_to_store,
            first_name=info.get("given_name", ""),
            last_name=info.get("family_name", ""),
        )
        user.set_unusable_password()
        user.save()
    _ensure_connection_membership(connection, user, subject)
    SsoIdentity.objects.create(connection=connection, user=user, subject=subject)
    return user
