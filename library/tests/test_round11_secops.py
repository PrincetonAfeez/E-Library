"""Security-hardening tests: real OIDC network path, XML hardening, URL validation."""

import json

import pytest
from defusedxml.common import EntitiesForbidden

from library import marc, sso
from library.models import Organization, SsoConnection, SsoIdentity
from library.net import UnsafeUrlError, validate_outbound_url
from library.services import DomainError

pytestmark = pytest.mark.django_db(transaction=True)


class _Resp:
    """Minimal stand-in for a urlopen response context manager."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _connection(org, **overrides):
    return SsoConnection.objects.create(
        organization=org,
        client_id="cid",
        client_secret="secret",
        authorize_url="https://idp.test/authorize",
        token_url=overrides.get("token_url", "https://idp.test/token"),
        userinfo_url=overrides.get("userinfo_url", "https://idp.test/userinfo"),
    )


# --------------------------------------------------------------------------- #
# ENT-sso: exercise the REAL _default_exchange/_default_userinfo network code
# against a simulated IdP (previously only the injected stubs were covered).
# --------------------------------------------------------------------------- #
def test_oidc_real_network_path_against_mock_idp(monkeypatch):
    from library.models import Branch

    org = Organization.objects.create(name="Lib", slug="lib")
    Branch.objects.create(organization=org, name="Main", slug="main")
    conn = _connection(org)

    def fake_safe_urlopen(url, *, data=None, headers=None, method="GET", timeout=8):
        if "token" in url:
            return _Resp({"access_token": "AT", "token_type": "Bearer"})
        return _Resp(
            {
                "sub": "idp-xyz",
                "email": "sso@x.test",
                "email_verified": True,
                "given_name": "Ada",
            }
        )

    monkeypatch.setattr("library.sso.safe_urlopen", fake_safe_urlopen)
    # No exchange/fetch stubs -> the real IdP-facing functions run.
    user = sso.handle_callback(conn, code="code123", redirect_uri="https://app/cb")
    assert user.email == "sso@x.test"
    assert SsoIdentity.objects.filter(connection=conn, subject="idp-xyz", user=user).exists()


def test_oidc_missing_access_token_errors(monkeypatch):
    org = Organization.objects.create(name="Lib", slug="lib")
    conn = _connection(org)
    monkeypatch.setattr(
        "library.sso.safe_urlopen",
        lambda url, **kwargs: _Resp({}),
    )
    with pytest.raises(DomainError):
        sso.handle_callback(conn, code="c", redirect_uri="https://app/cb")


def test_oidc_rejects_non_http_idp_url():
    org = Organization.objects.create(name="Lib", slug="lib")
    conn = _connection(org, token_url="file:///etc/passwd")
    with pytest.raises(UnsafeUrlError):
        sso.handle_callback(conn, code="c", redirect_uri="https://app/cb")


# --------------------------------------------------------------------------- #
# Outbound URL validation (SSRF scheme guard)
# --------------------------------------------------------------------------- #
def test_validate_outbound_url(settings):
    # Clear the test-only loopback allowlist so private-IP blocking is exercised.
    settings.OUTBOUND_URL_ALLOW_HOSTS = []
    assert validate_outbound_url("https://example.test/hook") == "https://example.test/hook"
    for bad in (
        "file:///etc/passwd",
        "gopher://x",
        "ftp://x/y",
        "data:text/plain,hi",
        "http://127.0.0.1/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost/admin",
    ):
        with pytest.raises(UnsafeUrlError):
            validate_outbound_url(bad)


# --------------------------------------------------------------------------- #
# MARCXML import hardened against XXE / entity expansion
# --------------------------------------------------------------------------- #
def test_marcxml_rejects_entity_expansion():
    payload = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE records [<!ENTITY lol "lol">]>'
        b'<records><record>&lol;</record></records>'
    )
    with pytest.raises(EntitiesForbidden):
        marc.parse_marcxml(payload)


def test_marcxml_parses_valid_record():
    payload = (
        b'<?xml version="1.0"?>'
        b'<records xmlns="http://www.loc.gov/MARC21/slim">'
        b'<record><datafield tag="245" ind1=" " ind2=" ">'
        b'<subfield code="a">Dune</subfield></datafield></record></records>'
    )
    records = marc.parse_marcxml(payload)
    assert len(records) == 1
