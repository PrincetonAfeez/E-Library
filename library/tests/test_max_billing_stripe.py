"""Maximize coverage of library.billing_stripe.StripeGateway via mocked Stripe SDK."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from library.billing import BillingError
from library.billing_stripe import StripeGateway, _stripe_price_id
from library.models import Organization, Plan, Subscription, SubscriptionStatus

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def stripe_settings(settings):
    settings.STRIPE_SECRET_KEY = "sk_test_coverage"
    return settings


def test_stripe_price_id_requires_external_price(stripe_settings):
    plan = Plan.objects.create(slug="no-price", name="No Price", price_cents=100, external_price_id="")
    with pytest.raises(BillingError, match="no Stripe price id"):
        _stripe_price_id(plan)


def test_create_customer_uses_stripe_sdk(stripe_settings, monkeypatch):
    org = Organization.objects.create(name="Stripe Org", slug="stripe-org-cov")
    created = SimpleNamespace(id="cus_cov123")
    monkeypatch.setattr(
        "library.billing_stripe.stripe.Customer.create",
        lambda **kwargs: created,
    )
    assert StripeGateway().create_customer(org) == "cus_cov123"


def test_create_subscription_returns_app_driven_id(stripe_settings, monkeypatch):
    org = Organization.objects.create(name="Sub Org", slug="stripe-sub-cov")
    plan = Plan.objects.create(
        slug="pro-cov", name="Pro", price_cents=100, external_price_id="price_x"
    )
    Subscription.objects.create(
        organization=org,
        plan=plan,
        status=SubscriptionStatus.ACTIVE,
        external_customer_id="cus_existing",
        external_subscription_id="",
    )
    monkeypatch.setattr(
        StripeGateway,
        "create_customer",
        lambda self, organization: "cus_should_not_call",
    )
    result = StripeGateway().create_subscription(org, plan)
    assert result == f"sub_app_{org.pk}_{plan.slug}"


def test_create_subscription_keeps_native_stripe_id(stripe_settings):
    org = Organization.objects.create(name="Native Org", slug="stripe-native-cov")
    plan = Plan.objects.create(
        slug="nat-cov", name="Nat", price_cents=100, external_price_id="price_x"
    )
    Subscription.objects.create(
        organization=org,
        plan=plan,
        status=SubscriptionStatus.ACTIVE,
        external_customer_id="cus_n",
        external_subscription_id="sub_native_123",
    )
    assert StripeGateway().create_subscription(org, plan) == "sub_native_123"


def test_create_checkout_session(stripe_settings, monkeypatch):
    org = Organization.objects.create(name="Chk Org", slug="stripe-chk-cov")
    plan = Plan.objects.create(
        slug="chk-cov", name="Chk", price_cents=100, external_price_id="price_x"
    )
    Subscription.objects.create(
        organization=org,
        plan=plan,
        status=SubscriptionStatus.TRIALING,
        external_customer_id="cus_chk",
    )
    session = SimpleNamespace(id="cs_test", url="https://checkout.example/s")
    monkeypatch.setattr(
        "library.billing_stripe.stripe.checkout.Session.create",
        lambda **kwargs: session,
    )
    result = StripeGateway().create_checkout_session(org, plan, "tok-abc")
    assert result == {"id": "cs_test", "url": "https://checkout.example/s"}


def test_attach_payment_method_refuses_last4(stripe_settings):
    org = Organization.objects.create(name="PM Org", slug="stripe-pm-cov")
    with pytest.raises(BillingError, match="Checkout or SetupIntent"):
        StripeGateway().attach_payment_method(org, "4242")


def test_cancel_deletes_native_subscription(stripe_settings, monkeypatch):
    deleted = []
    monkeypatch.setattr(
        "library.billing_stripe.stripe.Subscription.delete",
        lambda sub_id: deleted.append(sub_id),
    )
    sub = SimpleNamespace(external_subscription_id="sub_live_99")
    StripeGateway().cancel(sub)
    assert deleted == ["sub_live_99"]


def test_cancel_skips_app_driven_ids(stripe_settings, monkeypatch):
    monkeypatch.setattr(
        "library.billing_stripe.stripe.Subscription.delete",
        lambda sub_id: (_ for _ in ()).throw(AssertionError("must not delete")),
    )
    StripeGateway().cancel(SimpleNamespace(external_subscription_id="sub_app_1_plan"))


def test_charge_zero_and_missing_pm(stripe_settings):
    gw = StripeGateway()
    assert gw.charge(None, 0) == "ch_zero"
    assert gw.charge(None, 100) is None


def test_charge_refuses_placeholder_ref(stripe_settings):
    pm = SimpleNamespace(gateway_ref="pm_stripe_fake", organization=None)
    assert StripeGateway().charge(pm, 500) is None


def test_charge_succeeds_with_payment_intent(stripe_settings, monkeypatch):
    intent = SimpleNamespace(id="pi_ok", status="succeeded")
    monkeypatch.setattr(
        "library.billing_stripe.stripe.PaymentIntent.create",
        lambda **kwargs: intent,
    )
    pm = SimpleNamespace(gateway_ref="pm_live_1", organization=None)
    assert StripeGateway().charge(pm, 250, customer_id="cus_1", idempotency_key="k1") == "pi_ok"


def test_charge_handles_stripe_error(stripe_settings, monkeypatch):
    import stripe

    def boom(**kwargs):
        raise stripe.error.StripeError("declined")

    monkeypatch.setattr("library.billing_stripe.stripe.PaymentIntent.create", boom)
    pm = SimpleNamespace(gateway_ref="pm_live_2", organization=None)
    assert StripeGateway().charge(pm, 100, customer_id="cus_2") is None


def test_refund_sim_and_payment_intent(stripe_settings, monkeypatch):
    gw = StripeGateway()
    assert gw.refund("ch_sim_x", 10).startswith("re_sim_")
    refund = SimpleNamespace(id="re_live")
    monkeypatch.setattr(
        "library.billing_stripe.stripe.Refund.create",
        lambda **kwargs: refund,
    )
    assert gw.refund("pi_abc", 50) == "re_live"


def test_refund_rejects_unknown_id(stripe_settings):
    with pytest.raises(BillingError, match="PaymentIntent or Charge"):
        StripeGateway().refund("weird_id", 10)


def test_payment_method_from_checkout_session_paths(stripe_settings, monkeypatch):
    gw = StripeGateway()
    assert gw.payment_method_from_checkout_session({"payment_method": "pm_direct"}) == "pm_direct"
    assert (
        gw.payment_method_from_checkout_session({"payment_method": {"id": "pm_dict"}}) == "pm_dict"
    )
    intent = MagicMock()
    intent.payment_method = "pm_from_si"
    monkeypatch.setattr(
        "library.billing_stripe.stripe.SetupIntent.retrieve",
        lambda sid: intent,
    )
    assert (
        gw.payment_method_from_checkout_session({"setup_intent": "seti_1"}) == "pm_from_si"
    )
    assert gw.payment_method_from_checkout_session({}) == ""
