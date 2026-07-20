"""Tests for tenant onboarding + subscription billing (Increment 2)."""

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from library import billing
from library.models import (
    Branch,
    Copy,
    Edition,
    FeePolicy,
    Invoice,
    Organization,
    Plan,
    StaffMembership,
    StaffRole,
    Subscription,
    SubscriptionStatus,
    Work,
)

pytestmark = pytest.mark.django_db(transaction=True)


def _api(user):
    client = APIClient(enforce_csrf_checks=False)
    client.force_authenticate(user=user)
    client.defaults["secure"] = True
    return client


def make_plans():
    Plan.objects.create(slug="trial", name="Trial", price_cents=0, max_copies=1000)
    return Plan.objects.create(slug="pro", name="Pro", price_cents=29900, features=["*"])


# --------------------------------------------------------------------------- #
# Provisioning
# --------------------------------------------------------------------------- #
def test_provision_tenant_creates_full_stack():
    make_plans()
    owner = get_user_model().objects.create_user(username="owner")
    org = billing.provision_tenant(name="Metro", slug="metro", owner_user=owner)

    assert Organization.objects.filter(slug="metro").exists()
    assert Branch.objects.filter(organization=org).count() == 1
    membership = StaffMembership.objects.get(user=owner, organization=org)
    assert membership.role == StaffRole.ADMIN and membership.branch_id is None
    assert FeePolicy.objects.filter(organization=org).exists()
    sub = Subscription.objects.get(organization=org)
    assert sub.status == SubscriptionStatus.TRIALING
    assert sub.trial_ends_at is not None


def test_signup_view_provisions_and_logs_in(client):
    make_plans()
    resp = client.post(
        "/signup/",
        {
            "organization_name": "Riverside Library",
            "organization_slug": "riverside",
            "username": "boss",
            "email": "boss@example.test",
            "password1": "s3cretPass99X",
            "password2": "s3cretPass99X",
        },
        secure=True,
    )
    assert resp.status_code == 302
    org = Organization.objects.get(slug="riverside")
    assert StaffMembership.objects.filter(
        user__username="boss", organization=org, role=StaffRole.ADMIN
    ).exists()
    assert Subscription.objects.filter(organization=org).exists()
    assert "_auth_user_id" in client.session


def test_signup_rejects_duplicate_slug(client):
    make_plans()
    Organization.objects.create(name="Taken", slug="taken")
    resp = client.post(
        "/signup/",
        {
            "organization_name": "Another",
            "organization_slug": "taken",
            "username": "boss2",
            "email": "boss2@example.test",
            "password1": "s3cretPass99X",
            "password2": "s3cretPass99X",
        },
        secure=True,
    )
    assert resp.status_code == 200  # re-rendered with the error
    assert not get_user_model().objects.filter(username="boss2").exists()


# --------------------------------------------------------------------------- #
# Subscription lifecycle
# --------------------------------------------------------------------------- #
def test_subscribe_records_invoice():
    pro = make_plans()
    org = Organization.objects.create(name="Lib", slug="lib")
    billing.add_payment_method(organization=org, last4="4242")
    billing.subscribe(organization=org, plan=pro)
    sub = Subscription.objects.get(organization=org)
    assert sub.status == SubscriptionStatus.ACTIVE
    assert Invoice.objects.filter(organization=org, amount_cents=pro.price_cents).exists()


def test_change_plan_downgrade_guard():
    make_plans()
    small = Plan.objects.create(slug="small", name="Small", max_copies=1)
    pro = Plan.objects.get(slug="pro")
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title="W", slug="w")
    edition = Edition.objects.create(work=work, isbn_13="9780000000001")
    Copy.objects.create(organization=org, edition=edition, branch=branch, barcode="A")
    Copy.objects.create(organization=org, edition=edition, branch=branch, barcode="B")
    billing.add_payment_method(organization=org, last4="4242")
    sub = billing.subscribe(organization=org, plan=pro)
    # 2 copies > small.max_copies (1) -> downgrade must be refused.
    with pytest.raises(billing.BillingError):
        billing.change_plan(subscription=sub, new_plan=small)


def test_cancel_subscription():
    pro = make_plans()
    org = Organization.objects.create(name="Lib", slug="lib")
    billing.add_payment_method(organization=org, last4="4242")
    sub = billing.subscribe(organization=org, plan=pro)
    billing.cancel_subscription(subscription=sub)
    sub.refresh_from_db()
    assert sub.status == SubscriptionStatus.CANCELED


def test_webhook_event_sets_past_due():
    pro = make_plans()
    org = Organization.objects.create(name="Lib", slug="lib")
    billing.add_payment_method(organization=org, last4="4242")
    sub = billing.subscribe(organization=org, plan=pro)
    handled = billing.handle_gateway_event(
        {
            "id": "evt_onboarding_failed",
            "type": "invoice.payment_failed",
            "data": {"object": {"subscription": sub.external_subscription_id}},
        }
    )
    assert handled is True
    sub.refresh_from_db()
    assert sub.status == SubscriptionStatus.PAST_DUE


# --------------------------------------------------------------------------- #
# Billing UI + API (admin-only)
# --------------------------------------------------------------------------- #
def _admin_org():
    make_plans()
    owner = get_user_model().objects.create_user(username="owner", password="s3cretPass99X")
    org = billing.provision_tenant(name="Metro", slug="metro", owner_user=owner)
    return org, owner


def test_billing_dashboard_admin_only(client):
    org, owner = _admin_org()
    client.force_login(owner)
    assert client.get("/billing/", secure=True).status_code == 200

    librarian = get_user_model().objects.create_user(username="liv", password="x")
    StaffMembership.objects.create(
        user=librarian, organization=org, branch=None, role=StaffRole.LIBRARIAN
    )
    client.force_login(librarian)
    assert client.get("/billing/", secure=True).status_code == 403


def test_billing_api_overview_and_change_plan():
    org, owner = _admin_org()
    client = _api(owner)
    resp = client.get("/api/v1/billing/", secure=True)
    assert resp.status_code == 200
    assert "usage" in resp.json()

    resp = client.post(
        "/api/v1/billing/change-plan/", {"plan": "pro"}, format="json", secure=True
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["plan"] == "pro"


def test_webhook_endpoint_requires_secret_outside_debug(settings):
    settings.DEBUG = False
    settings.STRIPE_WEBHOOK_SECRET = ""
    client = APIClient()
    resp = client.post(
        "/api/v1/billing/webhook/stripe/", {"type": "noop.event"}, format="json", secure=True
    )
    assert resp.status_code == 503


def test_webhook_endpoint_debug_allows_unsigned(settings):
    settings.DEBUG = True
    settings.STRIPE_WEBHOOK_SECRET = ""
    client = APIClient()
    resp = client.post(
        "/api/v1/billing/webhook/stripe/", {"type": "noop.event"}, format="json", secure=True
    )
    assert resp.status_code == 200
    assert resp.json()["handled"] is False
