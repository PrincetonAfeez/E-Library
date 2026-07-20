"""Focused regressions for Round-4 tenancy, SSO, and billing fixes."""

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from library import billing, sso
from library.api import staff_api_organization
from library.billing_stripe import StripeGateway
from library.models import (
    Branch,
    Organization,
    PatronProfile,
    Plan,
    StaffMembership,
    StaffRole,
    SubscriptionStatus,
    SsoConnection,
)
from library.tenancy import get_current_organization

pytestmark = pytest.mark.django_db(transaction=True)


def test_webhook_does_not_resurrect_canceled_subscription():
    org = Organization.objects.create(name="Lib", slug="lib")
    plan = Plan.objects.create(slug="pro", name="Pro", price_cents=100)
    billing.add_payment_method(organization=org)
    sub = billing.subscribe(organization=org, plan=plan)
    billing.cancel_subscription(subscription=sub)
    sub.refresh_from_db()
    assert sub.status == SubscriptionStatus.CANCELED
    assert (
        billing.handle_gateway_event(
            {
                "id": "evt_resurrect",
                "type": "invoice.payment_failed",
                "data": {"object": {"subscription": sub.external_subscription_id}},
            }
        )
        is False
    )
    sub.refresh_from_db()
    assert sub.status == SubscriptionStatus.CANCELED
    assert sub.is_serviceable is False


def test_webhook_setup_extracts_payment_method(monkeypatch, settings):
    from library.models import CheckoutSession, CheckoutStatus

    settings.STRIPE_SECRET_KEY = "sk_test_x"
    org = Organization.objects.create(name="Lib", slug="lib-setup")
    plan = Plan.objects.create(slug="pro", name="Pro", price_cents=500)
    session = CheckoutSession.objects.create(
        organization=org, plan=plan, token="tok-setup-1", status=CheckoutStatus.OPEN
    )

    def fake_pm(self, session_obj):
        return "pm_from_setup"

    called = {}

    def fake_complete(**kwargs):
        called.update(kwargs)
        raise billing.BillingError("stop after extract")

    monkeypatch.setattr(StripeGateway, "payment_method_from_checkout_session", fake_pm)
    monkeypatch.setattr("library.billing.complete_checkout", fake_complete)
    assert (
        billing.handle_gateway_event(
            {
                "id": "evt_setup_1",
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "client_reference_id": session.token,
                        "setup_intent": "seti_abc",
                    }
                },
            }
        )
        is False
    )
    assert called.get("gateway_ref") == "pm_from_setup"


def test_explicit_org_is_ignored_for_authenticated_non_member():
    alpha = Organization.objects.create(name="Alpha", slug="alpha")
    beta = Organization.objects.create(name="Beta", slug="beta")
    user = get_user_model().objects.create_user(username="reader")
    PatronProfile.objects.create(user=user, organization=beta, library_card_number="P1")
    request = RequestFactory().get("/?org=alpha")
    request.user = user
    request.session = {}

    assert get_current_organization(request) == beta
    assert alpha != beta


def test_sso_callback_creates_patron_profile():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    connection = SsoConnection.objects.create(
        organization=org,
        client_id="client",
        client_secret="secret",
        authorize_url="https://idp.test/authorize",
        token_url="https://idp.test/token",
        userinfo_url="https://idp.test/userinfo",
    )

    user = sso.handle_callback(
        connection,
        code="code",
        redirect_uri="https://app.test/callback",
        exchange=lambda *_: {"access_token": "token"},
        fetch_userinfo=lambda *_: {"sub": "Subject / 123", "email_verified": False},
    )

    profile = PatronProfile.objects.get(user=user)
    assert profile.organization == org
    assert profile.home_branch == branch
    assert "subject" in profile.library_card_number.lower() or profile.library_card_number


def test_stripe_refund_uses_payment_intent(monkeypatch, settings):
    settings.STRIPE_SECRET_KEY = "sk_test_x"
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return type("Refund", (), {"id": "re_123"})()

    monkeypatch.setattr("stripe.Refund.create", create)
    assert StripeGateway().refund("pi_123", 250) == "re_123"
    assert captured == {"payment_intent": "pi_123", "amount": 250}


def test_staff_api_prefers_staff_over_patron_organization():
    patron_org = Organization.objects.create(name="Patron", slug="patron")
    staff_org = Organization.objects.create(name="Staff", slug="staff")
    user = get_user_model().objects.create_user(username="both", is_staff=True)
    PatronProfile.objects.create(user=user, organization=patron_org, library_card_number="P1")
    StaffMembership.objects.create(
        user=user, organization=staff_org, role=StaffRole.ADMIN, active=True
    )
    request = RequestFactory().get("/")
    request.user = user
    request.session = {}

    assert staff_api_organization(request) == staff_org


def test_charge_refuses_without_customer(monkeypatch, settings):
    settings.STRIPE_SECRET_KEY = "sk_test_x"
    pm = type("PM", (), {"last4": "4242", "gateway_ref": "pm_abc", "organization": None})()
    assert StripeGateway().charge(pm, 100) is None
