"""Tests for the simulated payment gateway: checkout, cards, renewal, dunning, proration."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from library import billing
from library.models import (
    CheckoutStatus,
    Invoice,
    InvoiceStatus,
    Organization,
    PaymentMethod,
    Plan,
    Subscription,
    SubscriptionStatus,
)

pytestmark = pytest.mark.django_db(transaction=True)


def _api(user):
    client = APIClient(enforce_csrf_checks=False)
    client.force_authenticate(user=user)
    client.defaults["secure"] = True
    return client


def make_plans():
    Plan.objects.create(slug="trial", name="Trial", price_cents=0, max_copies=1000)
    Plan.objects.create(slug="basic", name="Basic", price_cents=10000, features=["*"])
    return Plan.objects.create(slug="pro", name="Pro", price_cents=29900, features=["*"])


def make_org(slug="lib"):
    return Organization.objects.create(name=slug.title(), slug=slug)


# --------------------------------------------------------------------------- #
# Hosted checkout
# --------------------------------------------------------------------------- #
def test_checkout_completes_and_activates():
    pro = make_plans()
    org = make_org()
    session = billing.create_checkout(organization=org, plan=pro)
    assert session.status == CheckoutStatus.OPEN

    sub = billing.complete_checkout(session=session, last4="4242")
    assert sub.status == SubscriptionStatus.ACTIVE

    session.refresh_from_db()
    assert session.status == CheckoutStatus.COMPLETED and session.completed_at is not None

    method = PaymentMethod.objects.get(organization=org)
    assert method.last4 == "4242" and method.is_default

    invoice = Invoice.objects.filter(organization=org, status=InvoiceStatus.PAID).first()
    assert invoice is not None and invoice.amount_cents == pro.price_cents
    assert invoice.line_items.exists()


def test_checkout_declined_card_does_not_activate():
    pro = make_plans()
    org = make_org()
    session = billing.create_checkout(organization=org, plan=pro)
    with pytest.raises(billing.BillingError):
        billing.complete_checkout(session=session, last4="0000")

    session.refresh_from_db()
    assert session.status == CheckoutStatus.OPEN  # rolled back, reusable
    # The declined card add was rolled back with the transaction.
    assert not PaymentMethod.objects.filter(organization=org).exists()
    assert not Subscription.objects.filter(
        organization=org, status=SubscriptionStatus.ACTIVE
    ).exists()


def test_checkout_cannot_be_reused():
    pro = make_plans()
    org = make_org()
    session = billing.create_checkout(organization=org, plan=pro)
    billing.complete_checkout(session=session, last4="4242")
    with pytest.raises(billing.BillingError):
        billing.complete_checkout(session=session, last4="4242")


# --------------------------------------------------------------------------- #
# Renewal & dunning
# --------------------------------------------------------------------------- #
def test_billing_cycle_renews_due_subscription():
    pro = make_plans()
    org = make_org()
    billing.add_payment_method(organization=org, last4="4242")
    sub = billing.subscribe(organization=org, plan=pro)
    assert sub.status == SubscriptionStatus.ACTIVE

    Subscription.objects.filter(pk=sub.pk).update(
        current_period_end=timezone.now() - timedelta(days=1)
    )
    result = billing.run_billing_cycle()
    assert result["renewed"] >= 1

    sub.refresh_from_db()
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.current_period_end > timezone.now()
    # A trailing renewal invoice was recorded.
    assert Invoice.objects.filter(
        organization=org, description__icontains="renewal", status=InvoiceStatus.PAID
    ).exists()


def test_declined_renewal_enters_dunning_then_cancels():
    pro = make_plans()
    org = make_org()
    billing.add_payment_method(organization=org, last4="0000")  # always declines
    sub = billing.subscribe(organization=org, plan=pro)
    # A paid plan charged against a declining card never activates.
    assert sub.status == SubscriptionStatus.PAST_DUE
    assert sub.dunning_attempts == 1 and sub.grace_until is not None

    # Exhaust the dunning budget and expire the grace window.
    Subscription.objects.filter(pk=sub.pk).update(
        dunning_attempts=billing.MAX_DUNNING,
        grace_until=timezone.now() - timedelta(days=1),
    )
    result = billing.run_billing_cycle()
    assert result["canceled"] >= 1

    sub.refresh_from_db()
    assert sub.status == SubscriptionStatus.CANCELED


def test_dunning_retries_while_in_grace():
    pro = make_plans()
    org = make_org()
    billing.add_payment_method(organization=org, last4="0000")
    sub = billing.subscribe(organization=org, plan=pro)
    assert sub.dunning_attempts == 1

    # Still in grace -> a cycle retries (fails again) rather than canceling.
    result = billing.run_billing_cycle()
    assert result["dunning"] >= 1 and result["canceled"] == 0
    sub.refresh_from_db()
    assert sub.status == SubscriptionStatus.PAST_DUE
    assert sub.dunning_attempts == 2


# --------------------------------------------------------------------------- #
# Proration
# --------------------------------------------------------------------------- #
def test_change_plan_prorates_with_line_items():
    make_plans()
    basic = Plan.objects.get(slug="basic")
    pro = Plan.objects.get(slug="pro")
    org = make_org()
    billing.add_payment_method(organization=org, last4="4242")
    sub = billing.subscribe(organization=org, plan=basic)
    Subscription.objects.filter(pk=sub.pk).update(
        current_period_end=timezone.now() + timedelta(days=15)
    )
    sub.refresh_from_db()

    billing.change_plan(subscription=sub, new_plan=pro)
    sub.refresh_from_db()
    assert sub.plan == pro

    invoice = Invoice.objects.filter(
        organization=org, description__icontains="Plan change"
    ).first()
    assert invoice is not None
    # A credit line for the old plan and a charge line for the new plan.
    assert invoice.line_items.count() == 2
    assert invoice.line_items.filter(amount_cents__lt=0).exists()  # credit


# --------------------------------------------------------------------------- #
# API surface
# --------------------------------------------------------------------------- #
def _admin_org():
    make_plans()
    owner = get_user_model().objects.create_user(username="owner", password="s3cretPass99X")
    org = billing.provision_tenant(name="Metro", slug="metro", owner_user=owner)
    return org, owner


def test_checkout_api_flow():
    org, owner = _admin_org()
    client = _api(owner)
    resp = client.post("/api/v1/billing/checkout/", {"plan": "pro"}, format="json", secure=True)
    assert resp.status_code == 201
    token = resp.json()["data"]["token"]

    resp = client.post(
        f"/api/v1/billing/checkout/{token}/complete/", {"last4": "4242"}, format="json", secure=True
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "active"


def test_checkout_api_declined_returns_402():
    org, owner = _admin_org()
    client = _api(owner)
    token = (
        client.post("/api/v1/billing/checkout/", {"plan": "pro"}, format="json", secure=True)
        .json()["data"]["token"]
    )
    resp = client.post(
        f"/api/v1/billing/checkout/{token}/complete/", {"last4": "0000"}, format="json", secure=True
    )
    assert resp.status_code == 402


def test_payment_method_api_and_overview():
    org, owner = _admin_org()
    client = _api(owner)
    resp = client.post(
        "/api/v1/billing/payment-methods/", {"last4": "1111"}, format="json", secure=True
    )
    assert resp.status_code == 201

    overview = client.get("/api/v1/billing/", secure=True).json()
    assert overview["payment_methods"][0]["last4"] == "1111"
    assert "invoices" in overview


def test_billing_dashboard_add_card(client):
    org, owner = _admin_org()
    client.force_login(owner)
    resp = client.post(
        "/billing/",
        {"action": "add_card", "brand": "visa", "last4": "9999", "exp_month": "11", "exp_year": "2031"},
        secure=True,
    )
    assert resp.status_code == 302
    assert PaymentMethod.objects.filter(organization=org, last4="9999").exists()
