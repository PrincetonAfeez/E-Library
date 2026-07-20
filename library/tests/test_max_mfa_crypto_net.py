"""Broad coverage: MFA, permissions, crypto, net, pagination, logging, middleware."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from django.utils import timezone
from rest_framework.test import APIClient

from library import mfa
from library.crypto import decrypt_value, encrypt_value
from library.logging_utils import JsonFormatter, RequestIDFilter, SlowQueryFilter, request_id_var
from library.models import (
    Branch,
    Organization,
    PatronProfile,
    StaffMembership,
    StaffRole,
    StaffTotpDevice,
)
from library.net import UnsafeUrlError, validate_outbound_url
from library.pagination import CursorError, decode_cursor, encode_cursor
from library.permissions import (
    IsAuthenticatedPatron,
    IsLibraryStaff,
    TokenHasScope,
    staff_branch_ids_for_org,
    staff_mfa_satisfied,
    staff_permissions_for_org,
    user_can_act_on_branch,
    user_is_staff_for_org,
)
from library.services import DomainError

pytestmark = pytest.mark.django_db(transaction=True)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _org(*, require_staff_mfa: bool = False, slug: str | None = None) -> Organization:
    slug = slug or f"org-{secrets.token_hex(4)}"
    return Organization.objects.create(
        name="Lib", slug=slug, require_staff_mfa=require_staff_mfa
    )


def _staff_user(org: Organization, *, username: str = "staff", role=StaffRole.ADMIN):
    user = get_user_model().objects.create_user(username=username, password="x", is_staff=True)
    StaffMembership.objects.create(user=user, organization=org, branch=None, role=role)
    return user


def _enroll_confirmed(user):
    info = mfa.begin_enrollment(user=user)
    code = mfa.totp(info["secret"], timestamp=time.time())
    mfa.confirm_enrollment(user=user, code=code)
    return info


def _api_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    client.defaults["secure"] = True
    return client


def _encrypt_legacy_xor(plaintext: str) -> str:
    """Build an ``enc1:`` payload compatible with :func:`library.crypto._decrypt_legacy_xor`."""
    from django.conf import settings

    nonce = secrets.token_bytes(12)
    key = settings.SECRET_KEY.encode("utf-8")
    ciphertext = plaintext.encode("utf-8")
    out = bytearray()
    counter = 0
    while len(out) < len(ciphertext):
        out += hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        counter += 1
    keystream = bytes(out[: len(ciphertext)])
    encrypted = bytes(a ^ b for a, b in zip(ciphertext, keystream, strict=True))
    raw = nonce + encrypted
    return "enc1:" + base64.urlsafe_b64encode(raw).decode("ascii")


# --------------------------------------------------------------------------- #
# MFA (library.mfa)
# --------------------------------------------------------------------------- #
class TestMfaService:
    def test_disable_mfa_correct_code_deletes_device(self):
        user = get_user_model().objects.create_user(username="mfa-disable-ok")
        info = _enroll_confirmed(user)
        assert mfa.user_has_mfa(user)
        mfa.disable_mfa(user=user, code=mfa.totp(info["secret"], timestamp=time.time()))
        assert not StaffTotpDevice.objects.filter(user=user).exists()
        assert not mfa.user_has_mfa(user)

    def test_disable_mfa_wrong_code_raises(self):
        user = get_user_model().objects.create_user(username="mfa-disable-bad")
        _enroll_confirmed(user)
        with pytest.raises(DomainError, match="current MFA code"):
            mfa.disable_mfa(user=user, code="000000")
        assert mfa.user_has_mfa(user)

    def test_disable_mfa_no_confirmed_device_is_noop(self):
        user = get_user_model().objects.create_user(username="mfa-disable-pending")
        mfa.begin_enrollment(user=user)
        assert StaffTotpDevice.objects.filter(user=user).exists()
        mfa.disable_mfa(user=user, code="000000")
        assert not StaffTotpDevice.objects.filter(user=user).exists()

    def test_begin_enrollment_reenroll_requires_current_code(self):
        user = get_user_model().objects.create_user(username="mfa-reenroll")
        info = _enroll_confirmed(user)
        with pytest.raises(DomainError, match="current MFA code"):
            mfa.begin_enrollment(user=user)
        with pytest.raises(DomainError, match="current MFA code"):
            mfa.begin_enrollment(user=user, current_code="000000")
        renewed = mfa.begin_enrollment(
            user=user, current_code=mfa.totp(info["secret"], timestamp=time.time())
        )
        assert renewed["secret"] != info["secret"]

    def test_verify_login_false_without_device(self):
        user = get_user_model().objects.create_user(username="mfa-no-device")
        assert mfa.verify_login(user=user, code="123456") is False

    def test_provisioning_uri_contains_otpauth_and_secret(self):
        secret = mfa.generate_secret()
        uri = mfa.provisioning_uri(secret, "alice@lib.test")
        assert uri.startswith("otpauth://")
        assert f"secret={secret}" in uri

    def test_session_mfa_ok_empty_session(self):
        request = RequestFactory().get("/")
        request.user = get_user_model().objects.create_user(username="sess-empty")
        request.session = {}
        assert mfa.session_mfa_ok(request) is False

    def test_session_mfa_ok_rejects_legacy_bool(self):
        request = RequestFactory().get("/")
        user = get_user_model().objects.create_user(username="sess-legacy")
        request.user = user
        request.session = {"mfa_verified": True}
        assert mfa.session_mfa_ok(request) is False

    def test_session_mfa_ok_true_after_mark_session_verified(self):
        org = _org()
        request = RequestFactory().get("/")
        user = get_user_model().objects.create_user(username="sess-ok")
        request.user = user
        request.session = {}
        mfa.mark_session_verified(request, organization=org)
        assert mfa.session_mfa_ok(request, organization=org) is True

    def test_session_mfa_ok_false_wrong_org(self):
        org_a = _org(slug="org-a")
        org_b = _org(slug="org-b")
        request = RequestFactory().get("/")
        user = get_user_model().objects.create_user(username="sess-wrong-org")
        request.user = user
        request.session = {}
        mfa.mark_session_verified(request, organization=org_a)
        assert mfa.session_mfa_ok(request, organization=org_b) is False

    def test_session_mfa_ok_false_expired_verified_at(self):
        org = _org()
        request = RequestFactory().get("/")
        user = get_user_model().objects.create_user(username="sess-expired")
        request.user = user
        old = (timezone.now() - timedelta(hours=24)).isoformat()
        request.session = {
            "mfa_verified": {"user_id": user.pk, "org_id": org.pk, "verified_at": old}
        }
        assert mfa.session_mfa_ok(request, organization=org) is False


class TestMfaApi:
    def test_mfa_verify_api(self):
        user = get_user_model().objects.create_user(username="api-verify")
        info = _enroll_confirmed(user)
        client = _api_client(user)
        bad = client.post("/api/v1/account/mfa/verify/", {"code": "000000"}, format="json", secure=True)
        assert bad.status_code == 401
        assert bad.json()["data"]["verified"] is False

        ok = client.post(
            "/api/v1/account/mfa/verify/",
            {"code": mfa.totp(info["secret"], timestamp=time.time())},
            format="json",
            secure=True,
        )
        assert ok.status_code == 200
        assert ok.json()["data"]["verified"] is True
        assert isinstance(client.session.get("mfa_verified"), dict)

    def test_mfa_disable_api(self):
        user = get_user_model().objects.create_user(username="api-disable")
        info = _enroll_confirmed(user)
        client = _api_client(user)
        bad = client.post("/api/v1/account/mfa/disable/", {"code": "000000"}, format="json", secure=True)
        assert bad.status_code == 403
        assert mfa.user_has_mfa(user)

        ok = client.post(
            "/api/v1/account/mfa/disable/",
            {"code": mfa.totp(info["secret"], timestamp=time.time())},
            format="json",
            secure=True,
        )
        assert ok.status_code == 200
        assert ok.json()["data"]["enabled"] is False
        assert not mfa.user_has_mfa(user)


class TestMfaHtmlViews:
    def test_mfa_challenge_get_and_post(self, client):
        org = _org(require_staff_mfa=True)
        Branch.objects.create(organization=org, name="Main", slug="main")
        user = _staff_user(org, username="html-challenge")
        info = _enroll_confirmed(user)
        client.force_login(user)

        get_resp = client.get("/mfa/challenge/", secure=True)
        assert get_resp.status_code == 200

        post_resp = client.post(
            "/mfa/challenge/",
            {"code": mfa.totp(info["secret"], timestamp=time.time()), "next": "/librarian/"},
            secure=True,
        )
        assert post_resp.status_code == 302
        assert post_resp["Location"].endswith("/librarian/")

    def test_mfa_enroll_begin_and_confirm(self, client):
        org = _org(require_staff_mfa=True)
        user = _staff_user(org, username="html-enroll")
        client.force_login(user)

        begin = client.post("/mfa/enroll/", {"action": "begin"}, secure=True)
        assert begin.status_code == 200
        secret = client.session.get("mfa_enroll_secret")
        assert secret

        confirm = client.post(
            "/mfa/enroll/",
            {"action": "confirm", "code": mfa.totp(secret, timestamp=time.time())},
            secure=True,
        )
        assert confirm.status_code == 302
        assert mfa.user_has_mfa(user)


# --------------------------------------------------------------------------- #
# Permissions (library.permissions)
# --------------------------------------------------------------------------- #
class TestPermissions:
    def test_user_is_staff_for_org(self):
        org = _org()
        member = _staff_user(org, username="member", role=StaffRole.LIBRARIAN)
        superuser = get_user_model().objects.create_superuser(
            username="su", email="su@x.test", password="x"
        )
        assert user_is_staff_for_org(AnonymousUser(), org) is False
        assert user_is_staff_for_org(None, org) is False
        assert user_is_staff_for_org(member, None) is False
        assert user_is_staff_for_org(member, org) is True
        assert user_is_staff_for_org(superuser, org) is True

    def test_staff_permissions_for_org_librarian_vs_admin(self):
        org = _org()
        librarian = _staff_user(org, username="lib-role", role=StaffRole.LIBRARIAN)
        admin = _staff_user(org, username="adm-role", role=StaffRole.ADMIN)
        lib_perms = staff_permissions_for_org(librarian, org)
        admin_perms = staff_permissions_for_org(admin, org)
        assert "circulation" in lib_perms
        assert "catalog" in lib_perms
        assert "*" not in lib_perms
        assert admin_perms == {"*"}

    def test_staff_branch_ids_for_org(self):
        org = _org()
        branch_a = Branch.objects.create(organization=org, name="A", slug="a")
        branch_b = Branch.objects.create(organization=org, name="B", slug="b")
        admin = _staff_user(org, username="branch-admin", role=StaffRole.ADMIN)
        librarian = get_user_model().objects.create_user(username="branch-lib", is_staff=True)
        StaffMembership.objects.create(
            user=librarian, organization=org, branch=branch_a, role=StaffRole.LIBRARIAN
        )
        assert staff_branch_ids_for_org(admin, org) is None
        assert staff_branch_ids_for_org(librarian, org) == {branch_a.id}
        assert user_can_act_on_branch(librarian, org, branch_a.id) is True
        assert user_can_act_on_branch(librarian, org, branch_b.id) is False

    def test_staff_mfa_satisfied(self):
        org_off = _org(require_staff_mfa=False, slug="mfa-off")
        org_on = _org(require_staff_mfa=True, slug="mfa-on")
        user = _staff_user(org_on, username="mfa-sat")
        request = RequestFactory().get("/librarian/")
        request.user = user
        request.session = {}
        request.auth = None

        assert staff_mfa_satisfied(request, org_off) is True
        assert staff_mfa_satisfied(request, org_on) is False
        mfa.mark_session_verified(request, organization=org_on)
        assert staff_mfa_satisfied(request, org_on) is True

    def test_token_has_scope(self):
        perm = TokenHasScope()
        view = SimpleNamespace(required_scope="staff:read")
        session_req = SimpleNamespace(auth=None, auth_scopes=[])
        assert perm.has_permission(session_req, view) is True

        token_req = SimpleNamespace(auth=object(), auth_scopes=["other:scope"])
        assert perm.has_permission(token_req, view) is False

        scoped_req = SimpleNamespace(auth=object(), auth_scopes=["staff:read"])
        assert perm.has_permission(scoped_req, view) is True

        wildcard_req = SimpleNamespace(auth=object(), auth_scopes=["*"])
        assert perm.has_permission(wildcard_req, view) is True

    def test_is_authenticated_patron_and_library_staff(self):
        org = _org(require_staff_mfa=False)
        branch = Branch.objects.create(organization=org, name="Main", slug="main")
        patron_user = get_user_model().objects.create_user(username="patron-user")
        PatronProfile.objects.create(
            user=patron_user,
            organization=org,
            library_card_number="P1",
            home_branch=branch,
        )
        staff_user = _staff_user(org, username="staff-user", role=StaffRole.ADMIN)
        anon = AnonymousUser()

        patron_perm = IsAuthenticatedPatron()
        staff_perm = IsLibraryStaff()
        patron_req = RequestFactory().get("/")
        patron_req.user = patron_user
        staff_req = RequestFactory().get("/librarian/")
        staff_req.user = staff_user
        staff_req.session = {}
        staff_req.auth = None
        staff_req.organization = org
        anon_req = RequestFactory().get("/")
        anon_req.user = anon

        assert patron_perm.has_permission(patron_req, None) is True
        assert patron_perm.has_permission(anon_req, None) is False
        assert staff_perm.has_permission(staff_req, None) is True
        assert staff_perm.has_permission(anon_req, None) is False


# --------------------------------------------------------------------------- #
# Crypto (library.crypto)
# --------------------------------------------------------------------------- #
class TestCrypto:
    def test_encrypt_decrypt_roundtrip(self):
        plain = "super-secret-value"
        stored = encrypt_value(plain)
        assert stored.startswith("enc2:")
        assert decrypt_value(stored) == plain

    def test_empty_string_passthrough(self):
        assert encrypt_value("") == ""
        assert decrypt_value("") == ""

    def test_decrypt_plaintext_when_allowed(self, settings):
        settings.DISALLOW_PLAINTEXT_SECRETS = False
        assert decrypt_value("plain-secret", allow_plaintext=True) == "plain-secret"

    def test_decrypt_plaintext_raises_when_disallowed(self, settings):
        settings.DISALLOW_PLAINTEXT_SECRETS = True
        with pytest.raises(ValueError, match="Refusing to read plaintext"):
            decrypt_value("plain-secret", allow_plaintext=True)

    def test_legacy_enc1_xor_roundtrip(self):
        plain = "legacy-mfa-secret"
        stored = _encrypt_legacy_xor(plain)
        assert stored.startswith("enc1:")
        assert decrypt_value(stored) == plain


# --------------------------------------------------------------------------- #
# Net (library.net)
# --------------------------------------------------------------------------- #
class TestNet:
    def test_validate_outbound_url_rejects_blocked_targets(self, settings):
        settings.OUTBOUND_URL_ALLOW_HOSTS = []
        for bad in (
            "file:///etc/passwd",
            "http://localhost/admin",
            "http://127.0.0.1/x",
            "http://169.254.169.254/latest/meta-data/",
        ):
            with pytest.raises(UnsafeUrlError):
                validate_outbound_url(bad)

    def test_validate_outbound_url_allows_public_https(self, settings):
        settings.OUTBOUND_URL_ALLOW_HOSTS = []
        assert validate_outbound_url("https://example.com/path") == "https://example.com/path"

    def test_validate_outbound_url_allow_host_override(self, settings):
        settings.OUTBOUND_URL_ALLOW_HOSTS = ["127.0.0.1"]
        assert validate_outbound_url("http://127.0.0.1/local") == "http://127.0.0.1/local"


# --------------------------------------------------------------------------- #
# Pagination (library.pagination)
# --------------------------------------------------------------------------- #
class TestPagination:
    def test_encode_decode_cursor_roundtrip(self):
        payload = {"query": "cyberpunk", "filters": {"branch": "main"}, "page": 3}
        assert decode_cursor(encode_cursor(payload)) == payload

    def test_decode_cursor_none_returns_empty_dict(self):
        assert decode_cursor(None) == {}

    def test_decode_cursor_bad_signature_raises(self):
        cursor = encode_cursor({"page": 1}) + "tampered"
        with pytest.raises(CursorError):
            decode_cursor(cursor)


# --------------------------------------------------------------------------- #
# logging_utils
# --------------------------------------------------------------------------- #
class TestLoggingUtils:
    def test_request_id_filter_sets_request_id(self):
        token = request_id_var.set("req-abc")
        try:
            record = logging.LogRecord(
                name="test", level=logging.INFO, pathname=__file__, lineno=1,
                msg="hello", args=(), exc_info=None,
            )
            assert RequestIDFilter().filter(record) is True
            assert record.request_id == "req-abc"
        finally:
            request_id_var.reset(token)

    def test_slow_query_filter_threshold(self):
        filt = SlowQueryFilter(threshold_ms=100)
        slow = logging.LogRecord(
            name="django.db.backends", level=logging.DEBUG, pathname=__file__, lineno=1,
            msg="SELECT 1", args=(), exc_info=None,
        )
        slow.duration = 0.15
        fast = logging.LogRecord(
            name="django.db.backends", level=logging.DEBUG, pathname=__file__, lineno=1,
            msg="SELECT 1", args=(), exc_info=None,
        )
        fast.duration = 0.05
        assert filt.filter(slow) is True
        assert filt.filter(fast) is False

    def test_json_formatter_produces_json_with_level_and_message(self):
        record = logging.LogRecord(
            name="library", level=logging.WARNING, pathname=__file__, lineno=1,
            msg="something happened", args=(), exc_info=None,
        )
        record.request_id = "rid-1"
        payload = json.loads(JsonFormatter().format(record))
        assert payload["level"] == "WARNING"
        assert payload["message"] == "something happened"
        assert payload["request_id"] == "rid-1"


# --------------------------------------------------------------------------- #
# Middleware: StaffMfaMiddleware
# --------------------------------------------------------------------------- #
class TestStaffMfaMiddleware:
    def test_redirects_to_challenge_without_session_mfa(self, client):
        org = _org(require_staff_mfa=True, slug="mw-org")
        Branch.objects.create(organization=org, name="Main", slug="main")
        user = _staff_user(org, username="mw-staff")
        _enroll_confirmed(user)
        client.force_login(user)

        resp = client.get("/librarian/", secure=True)
        assert resp.status_code == 302
        assert "/mfa/challenge/" in resp["Location"]

    def test_allows_access_after_mark_session_verified(self, client):
        org = _org(require_staff_mfa=True, slug="mw-ok")
        Branch.objects.create(organization=org, name="Main", slug="main")
        user = _staff_user(org, username="mw-verified")
        info = _enroll_confirmed(user)
        client.force_login(user)

        challenge = client.post(
            "/mfa/challenge/",
            {"code": mfa.totp(info["secret"], timestamp=time.time()), "next": "/librarian/"},
            secure=True,
        )
        assert challenge.status_code == 302

        resp = client.get("/librarian/", secure=True)
        assert resp.status_code != 302 or "/mfa/challenge/" not in resp.get("Location", "")
