"""Tests for outbound webhooks + OIDC SSO (Increment 8b)."""

import hashlib
import hmac
import json

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from library import sso, webhooks
from library.models import (
    Branch,
    Copy,
    Edition,
    Organization,
    PatronProfile,
    SsoConnection,
    SsoIdentity,
    StaffMembership,
    StaffRole,
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookEndpoint,
    Work,
)
from library.services import borrow_work, drain_outbox

pytestmark = pytest.mark.django_db(transaction=True)


def make_catalog():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title="Dune", slug="dune")
    edition = Edition.objects.create(work=work, isbn_13="9780000000001")
    Copy.objects.create(organization=org, edition=edition, branch=branch, barcode="C1")
    user = get_user_model().objects.create_user(username="reader", email="r@x.test")
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number="C1", home_branch=branch
    )
    return org, branch, work, edition, patron


# --------------------------------------------------------------------------- #
# Webhooks
# --------------------------------------------------------------------------- #
def test_webhook_enqueued_and_delivered_signed():
    org, branch, work, edition, patron = make_catalog()
    endpoint = WebhookEndpoint.objects.create(
        organization=org, url="https://hooks.test/x", secret="s3cret", event_types=["loan.borrowed"]
    )
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    drain_outbox()  # processes outbox -> enqueues webhook deliveries
    assert WebhookDelivery.objects.filter(endpoint=endpoint, event_type="loan.borrowed").exists()

    captured = {}

    def fake_post(url, body, headers):
        captured["url"] = url
        captured["sig"] = headers.get("X-Elibrary-Signature")
        captured["body"] = body
        return 200

    delivered = webhooks.deliver_webhooks(post=fake_post)
    assert delivered == 1
    # HMAC signature is correct.
    expected = hmac.new(b"s3cret", captured["body"], hashlib.sha256).hexdigest()
    assert captured["sig"] == expected
    payload = json.loads(captured["body"])
    assert payload["event"] == "loan.borrowed"
    assert payload["organization"] == "lib"

    d = WebhookDelivery.objects.get(endpoint=endpoint)
    assert d.status == WebhookDeliveryStatus.DELIVERED


def test_webhook_event_type_filter():
    org, branch, work, edition, patron = make_catalog()
    WebhookEndpoint.objects.create(
        organization=org, url="https://hooks.test/x", event_types=["hold.ready"]
    )
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    drain_outbox()
    # loan.borrowed doesn't match a hold.ready-only endpoint.
    assert not WebhookDelivery.objects.exists()


def test_webhook_retry_then_dead_letter():
    org, branch, work, edition, patron = make_catalog()
    ep = WebhookEndpoint.objects.create(organization=org, url="https://hooks.test/x")
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    drain_outbox()

    def failing_post(url, body, headers):
        raise RuntimeError("boom")

    for _ in range(webhooks.MAX_ATTEMPTS):
        # Reset next_attempt so each retry is due immediately.
        WebhookDelivery.objects.filter(endpoint=ep).update(next_attempt_at=timezone.now())
        webhooks.deliver_webhooks(post=failing_post)
    d = WebhookDelivery.objects.get(endpoint=ep)
    assert d.status == WebhookDeliveryStatus.FAILED
    assert d.attempts == webhooks.MAX_ATTEMPTS


def test_webhook_admin_api_permission():
    org, branch, work, edition, patron = make_catalog()
    # A branch manager lacks the admin-only 'webhooks' permission.
    mgr = get_user_model().objects.create_user(username="mgr", is_staff=True)
    StaffMembership.objects.create(user=mgr, organization=org, branch=None, role=StaffRole.BRANCH_MANAGER)
    c = APIClient(enforce_csrf_checks=False)
    c.force_authenticate(user=mgr)
    c.defaults["secure"] = True
    assert c.get("/api/v1/librarian/webhooks/", secure=True).status_code == 403

    admin = get_user_model().objects.create_user(username="adm", is_staff=True)
    StaffMembership.objects.create(user=admin, organization=org, branch=None, role=StaffRole.ADMIN)
    c2 = APIClient(enforce_csrf_checks=False)
    c2.force_authenticate(user=admin)
    resp = c2.post(
        "/api/v1/librarian/webhooks/", {"url": "https://hooks.test/y"}, format="json", secure=True
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["secret"]  # auto-generated secret returned once


# --------------------------------------------------------------------------- #
# SSO / OIDC
# --------------------------------------------------------------------------- #
def _connection(org):
    return SsoConnection.objects.create(
        organization=org,
        client_id="cid",
        client_secret="csecret",
        authorize_url="https://idp.test/authorize",
        token_url="https://idp.test/token",
        userinfo_url="https://idp.test/userinfo",
    )


def test_build_authorize_url():
    org = Organization.objects.create(name="Lib", slug="lib")
    conn = _connection(org)
    url = sso.build_authorize_url(conn, redirect_uri="https://app/cb", state="xyz")
    assert url.startswith("https://idp.test/authorize?")
    assert "client_id=cid" in url and "state=xyz" in url


def test_handle_callback_creates_and_links():
    org = Organization.objects.create(name="Lib", slug="lib")
    conn = _connection(org)

    def fake_exchange(connection, code, redirect_uri):
        return {"access_token": "tok"}

    def fake_userinfo(connection, access_token):
        return {"sub": "idp-123", "email": "sso@example.test", "given_name": "Ada"}

    user = sso.handle_callback(
        conn, code="c", redirect_uri="https://app/cb", exchange=fake_exchange, fetch_userinfo=fake_userinfo
    )
    assert user.email == "sso@example.test"
    assert SsoIdentity.objects.filter(connection=conn, subject="idp-123").exists()

    # Second login with the same subject returns the same user (no duplicate).
    again = sso.handle_callback(
        conn, code="c2", redirect_uri="https://app/cb", exchange=fake_exchange, fetch_userinfo=fake_userinfo
    )
    assert again.pk == user.pk
    assert SsoIdentity.objects.filter(connection=conn).count() == 1


def test_handle_callback_links_existing_email():
    org = Organization.objects.create(name="Lib", slug="lib")
    conn = _connection(org)
    existing = get_user_model().objects.create_user(username="existing", email="dup@example.test")

    user = sso.handle_callback(
        conn,
        code="c",
        redirect_uri="https://app/cb",
        exchange=lambda *a: {"access_token": "t"},
        fetch_userinfo=lambda *a: {"sub": "s1", "email": "dup@example.test"},
    )
    assert user.pk == existing.pk


def test_sso_login_redirects_to_idp(client):
    org = Organization.objects.create(name="Lib", slug="lib")
    _connection(org)
    resp = client.get("/sso/lib/login/", secure=True)
    assert resp.status_code == 302
    assert resp["Location"].startswith("https://idp.test/authorize?")


def test_sso_callback_bad_state(client):
    org = Organization.objects.create(name="Lib", slug="lib")
    _connection(org)
    resp = client.get("/sso/callback/?state=wrong&code=abc", secure=True)
    assert resp.status_code == 400
