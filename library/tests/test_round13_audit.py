"""Regression tests for Round-3 audit fixes."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from library import billing, finance, privacy, sso
from library.models import (
    Branch,
    Fee,
    FeeType,
    Organization,
    OutboxEvent,
    OutboxStatus,
    PatronProfile,
    Plan,
    Subscription,
    SubscriptionStatus,
    WebhookDelivery,
    WebhookEndpoint,
)
from library.services import DomainError
from library import webhooks

pytestmark = pytest.mark.django_db(transaction=True)


def test_subscribe_requires_card_for_paid_plan():
    org = Organization.objects.create(name="Lib", slug="lib")
    plan = Plan.objects.create(slug="pro", name="Pro", price_cents=1000)
    with pytest.raises(billing.BillingError, match="card is required"):
        billing.subscribe(organization=org, plan=plan)


def test_subscribe_free_plan_without_card():
    org = Organization.objects.create(name="Lib", slug="lib")
    plan = Plan.objects.create(slug="free", name="Free", price_cents=0)
    sub = billing.subscribe(organization=org, plan=plan)
    assert sub.status == SubscriptionStatus.ACTIVE


def test_webhook_event_id_is_idempotent():
    org = Organization.objects.create(name="Lib", slug="lib")
    plan = Plan.objects.create(slug="pro", name="Pro", price_cents=100)
    billing.add_payment_method(organization=org)
    sub = billing.subscribe(organization=org, plan=plan)
    event = {
        "id": "evt_grace_1",
        "type": "invoice.payment_failed",
        "data": {"object": {"subscription": sub.external_subscription_id}},
    }
    assert billing.handle_gateway_event(event) is True
    assert billing.handle_gateway_event(event) is False


def test_webhook_does_not_reopen_expired_grace():
    org = Organization.objects.create(name="Lib", slug="lib")
    plan = Plan.objects.create(slug="pro", name="Pro", price_cents=100)
    billing.add_payment_method(organization=org)
    sub = billing.subscribe(organization=org, plan=plan)
    past = timezone.now() - timedelta(days=1)
    Subscription.objects.filter(pk=sub.pk).update(
        status=SubscriptionStatus.PAST_DUE, grace_until=past
    )
    sub.refresh_from_db()
    assert billing.handle_gateway_event(
        {
            "id": "evt_r13_failed",
            "type": "invoice.payment_failed",
            "data": {"object": {"subscription": sub.external_subscription_id}},
        }
    )
    sub.refresh_from_db()
    assert sub.grace_until == past


def test_access_content_omits_durable_url():
    from library import digital
    from library.models import DigitalLicense, Edition, LicenseModel, Work

    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title="N", slug="n")
    edition = Edition.objects.create(work=work, isbn_13="9780000000999")
    DigitalLicense.objects.create(
        organization=org,
        edition=edition,
        license_model=LicenseModel.ONE_COPY_ONE_USER,
        concurrent_limit=1,
        content_url="https://cdn.example/secret.epub",
    )
    user = get_user_model().objects.create_user(username="r", email="r@x.test")
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number="C1", home_branch=branch
    )
    loan = digital.borrow_digital(patron=patron, edition=edition, actor=user)
    manifest = digital.access_content(access_token=loan.access_token)
    assert "content_url" not in manifest
    assert manifest.get("content_token") or manifest.get("format") == "external"


def test_webhook_enqueue_idempotent_on_retry():
    org = Organization.objects.create(name="Lib", slug="lib")
    WebhookEndpoint.objects.create(
        organization=org, url="https://hooks.example/x", event_types=["*"], active=True
    )
    event = OutboxEvent.objects.create(
        organization=org,
        event_type="loan.created",
        payload={},
        status=OutboxStatus.PENDING,
    )
    assert webhooks.enqueue_for_outbox_event(event) == 1
    assert webhooks.enqueue_for_outbox_event(event) == 0
    assert WebhookDelivery.objects.filter(outbox_event_id=event.pk).count() == 1


def test_create_payment_plan_capped_to_balance():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    user = get_user_model().objects.create_user(username="p", email="p@x.test")
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number="C1", home_branch=branch
    )
    Fee.objects.create(
        organization=org, patron=patron, fee_type=FeeType.MANUAL, amount_cents=500
    )
    with pytest.raises(DomainError):
        finance.create_payment_plan(patron=patron, total_cents=600, installments=2)
    plan = finance.create_payment_plan(patron=patron, total_cents=500, installments=2)
    assert plan.total_cents == 500
