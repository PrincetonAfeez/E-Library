"""Regression tests for Round-2 audit fixes (Stripe truth, webhook grace, SSO erase, MFA tokens)."""

import time

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from library import billing, mfa, privacy, sso
from library.billing_stripe import StripeGateway
from library.crypto import decrypt_value
from library.models import (
    Branch,
    Organization,
    PatronProfile,
    Plan,
    ScopedApiToken,
    SsoConnection,
    SsoIdentity,
    StaffMembership,
    StaffRole,
    Subscription,
    SubscriptionStatus,
)
from library.services import DomainError

pytestmark = pytest.mark.django_db(transaction=True)


def test_stripe_charge_refuses_empty_gateway_ref(settings):
    settings.STRIPE_SECRET_KEY = "sk_test_x"
    gw = StripeGateway()
    pm = type("PM", (), {"last4": "4242", "gateway_ref": ""})()
    assert gw.charge(pm, 500) is None
    fake = type("PM", (), {"last4": "4242", "gateway_ref": "pm_stripe_1_4242"})()
    assert gw.charge(fake, 500) is None


def test_stripe_create_subscription_is_app_driven(settings):
    settings.STRIPE_SECRET_KEY = "sk_test_x"
    org = Organization.objects.create(name="Lib", slug="lib")
    plan = Plan.objects.create(
        slug="pro", name="Pro", price_cents=1000, external_price_id="price_abc"
    )
    Subscription.objects.create(
        organization=org,
        plan=plan,
        status=SubscriptionStatus.ACTIVE,
        external_customer_id="cus_existing",
    )
    gw = StripeGateway()
    assert gw.create_subscription(org, plan) == f"sub_app_{org.pk}_{plan.slug}"
    sub = org.subscription
    sub.external_subscription_id = "sub_real"
    sub.save(update_fields=["external_subscription_id"])
    assert gw.create_subscription(org, plan) == "sub_real"


def test_stripe_checkout_setup_does_not_require_price(settings, monkeypatch):
    settings.STRIPE_SECRET_KEY = "sk_test_x"
    org = Organization.objects.create(name="Lib", slug="lib")
    plan = Plan.objects.create(slug="pro", name="Pro", price_cents=1000)
    gw = StripeGateway()
    monkeypatch.setattr(gw, "_customer_id", lambda organization: "cus_abc")
    monkeypatch.setattr(
        "stripe.checkout.Session.create",
        lambda **kwargs: type("Session", (), {"id": "cs_abc", "url": "https://checkout.test"})(),
    )
    assert gw.create_checkout_session(org, plan, "tok") == {
        "id": "cs_abc",
        "url": "https://checkout.test",
    }


def test_webhook_past_due_sets_grace():
    plan = Plan.objects.create(slug="pro", name="Pro", price_cents=100)
    org = Organization.objects.create(name="Lib", slug="lib")
    billing.add_payment_method(organization=org)
    sub = billing.subscribe(organization=org, plan=plan)
    assert billing.handle_gateway_event(
        {
            "id": "evt_r12_failed",
            "type": "invoice.payment_failed",
            "data": {"object": {"subscription": sub.external_subscription_id}},
        }
    )
    sub.refresh_from_db()
    assert sub.status == SubscriptionStatus.PAST_DUE
    assert sub.grace_until is not None and sub.grace_until > timezone.now()
    assert sub.is_serviceable is True


def test_webhook_subscription_updated_uses_object_status():
    plan = Plan.objects.create(slug="pro", name="Pro", price_cents=100)
    org = Organization.objects.create(name="Lib", slug="lib")
    billing.add_payment_method(organization=org)
    sub = billing.subscribe(organization=org, plan=plan)
    assert billing.handle_gateway_event(
        {
            "id": "evt_r12_updated",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": sub.external_subscription_id,
                    "status": "canceled",
                    "customer": sub.external_customer_id,
                }
            },
        }
    )
    sub.refresh_from_db()
    assert sub.status == SubscriptionStatus.CANCELED


def test_erase_blocks_sso_relogin():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    user = get_user_model().objects.create_user(username="p", email="p@x.test", password="x")
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number="C1", home_branch=branch
    )
    conn = SsoConnection.objects.create(
        organization=org,
        client_id="cid",
        client_secret="secret",
        authorize_url="https://idp.test/a",
        token_url="https://idp.test/t",
        userinfo_url="https://idp.test/u",
    )
    SsoIdentity.objects.create(connection=conn, user=user, subject="sub-1")
    privacy.erase_patron(patron=patron, actor=user)
    assert not SsoIdentity.objects.filter(user=user).exists()
    user.refresh_from_db()
    assert user.is_active is False
    # Even if an identity row were reattached, login must fail for inactive users.
    SsoIdentity.objects.create(connection=conn, user=user, subject="sub-1")
    with pytest.raises(DomainError, match="disabled"):
        sso.handle_callback(
            conn,
            code="c",
            redirect_uri="https://app/cb",
            exchange=lambda *a: {"access_token": "t"},
            fetch_userinfo=lambda *a: {"sub": "sub-1", "email_verified": True},
        )


def test_mfa_bearer_requires_mfa_verified_scope():
    org = Organization.objects.create(name="Lib", slug="lib", require_staff_mfa=True)
    Branch.objects.create(organization=org, name="Main", slug="main")
    user = get_user_model().objects.create_user(username="adm", password="x", is_staff=True)
    StaffMembership.objects.create(
        user=user, organization=org, branch=None, role=StaffRole.ADMIN
    )
    info = mfa.begin_enrollment(user=user)
    mfa.confirm_enrollment(user=user, code=mfa.totp(info["secret"], timestamp=time.time()))

    raw, token = ScopedApiToken.issue(
        user=user, organization=org, name="t", scopes=["staff:read"]
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    resp = client.get("/api/v1/librarian/dashboard/", secure=True)
    assert resp.status_code == 403

    token.scopes = ["staff:read", "mfa:verified"]
    token.save(update_fields=["scopes"])
    resp = client.get("/api/v1/librarian/dashboard/", secure=True)
    assert resp.status_code == 200


def test_sip2_password_encrypted_at_rest():
    org = Organization.objects.create(
        name="Lib", slug="lib", sip2_login_user="sc", sip2_login_password="s3cret"
    )
    org.refresh_from_db()
    assert org.sip2_login_password.startswith("enc2:")
    assert decrypt_value(org.sip2_login_password) == "s3cret"


def test_sso_client_secret_encrypted_at_rest():
    org = Organization.objects.create(name="Lib", slug="lib")
    conn = SsoConnection.objects.create(
        organization=org,
        client_id="cid",
        client_secret="super-secret",
        authorize_url="https://idp.test/a",
        token_url="https://idp.test/t",
        userinfo_url="https://idp.test/u",
    )
    conn.refresh_from_db()
    assert conn.client_secret.startswith("enc2:")
    assert decrypt_value(conn.client_secret) == "super-secret"


def test_charge_online_amount_stripe_requires_pm(settings):
    settings.STRIPE_SECRET_KEY = "sk_test_x"
    org = Organization.objects.create(name="Fee library", slug="fee-library")
    with pytest.raises(billing.BillingError):
        billing.charge_online_amount(organization=org, amount_cents=100)
