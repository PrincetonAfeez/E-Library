"""OpenID Connect SSO: per-tenant connection, authorize URL, and callback.

The token exchange and userinfo fetch are injectable so the identity-linking
logic is testable without a live identity provider. On callback we link by OIDC
subject, else by email, else provision a new (password-less) account.
"""

from __future__ import annotations

import json
import secrets
import urllib.parse
import urllib.request

from django.contrib.auth import get_user_model

from .models import SsoIdentity
from .net import validate_outbound_url
from .services import DomainError

User = get_user_model()


def build_authorize_url(connection, *, redirect_uri: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": connection.client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid email profile",
        "state": state,
    }
    sep = "&" if "?" in connection.authorize_url else "?"
    return f"{connection.authorize_url}{sep}{urllib.parse.urlencode(params)}"


def _default_exchange(connection, code: str, redirect_uri: str) -> dict:
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": connection.client_id,
            "client_secret": connection.client_secret,
        }
    ).encode("utf-8")
    validate_outbound_url(connection.token_url)
    request = urllib.request.Request(
        connection.token_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=8) as response:  # nosec B310 - scheme validated  # noqa: S310
        return json.loads(response.read())


def _default_userinfo(connection, access_token: str) -> dict:
    validate_outbound_url(connection.userinfo_url)
    request = urllib.request.Request(
        connection.userinfo_url, headers={"Authorization": f"Bearer {access_token}"}
    )
    with urllib.request.urlopen(request, timeout=8) as response:  # nosec B310 - scheme validated  # noqa: S310
        return json.loads(response.read())


def _unique_username(seed: str) -> str:
    base = (seed.split("@")[0] or "user").strip() or "user"
    username = base
    while User.objects.filter(username=username).exists():
        username = f"{base}-{secrets.token_hex(3)}"
    return username


def handle_callback(connection, *, code: str, redirect_uri: str, exchange=None, fetch_userinfo=None):
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
        return identity.user

    user = User.objects.filter(email__iexact=email).first() if email else None
    if user is None:
        user = User.objects.create_user(
            username=_unique_username(email or subject),
            email=email,
            first_name=info.get("given_name", ""),
            last_name=info.get("family_name", ""),
        )
        user.set_unusable_password()
        user.save()
    SsoIdentity.objects.create(connection=connection, user=user, subject=subject)
    return user
