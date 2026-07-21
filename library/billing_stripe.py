"""Stripe payment gateway adapter

Used when ``STRIPE_SECRET_KEY`` is set. Never invents payment-method ids and
never treats simulated last4 rules as a successful Stripe charge.
"""

from __future__ import annotations

import logging

import stripe
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist

logger = logging.getLogger("library")


def _billing_error(message: str):
    from .billing import BillingError

    raise BillingError(message)


def _stripe_price_id(plan) -> str:
    price = (getattr(plan, "external_price_id", None) or "").strip()
    if not price:
        _billing_error(
            f"Plan '{plan.slug}' has no Stripe price id (external_price_id). "
            "Configure it before using the Stripe gateway."
        )
    return price


class StripeGateway:
    name = "stripe"

    def __init__(self):
        stripe.api_key = settings.STRIPE_SECRET_KEY

    def create_customer(self, organization) -> str:
        customer = stripe.Customer.create(
            name=organization.name,
            metadata={"organization_id": str(organization.pk), "slug": organization.slug},
        )
        return customer.id

    def _customer_id(self, organization) -> str:
        from django.core.exceptions import ObjectDoesNotExist

        try:
            sub = organization.subscription
        except ObjectDoesNotExist:
            sub = None
        cust = (sub.external_customer_id if sub else "") or ""
        if cust:
            return cust
        return self.create_customer(organization)

    def create_subscription(self, organization, plan) -> str:
        # The application owns subscription periods, invoices, and dunning. Stripe
        # only stores the customer and payment method used for direct charges.
        self._customer_id(organization)
        from django.core.exceptions import ObjectDoesNotExist

        try:
            external_id = organization.subscription.external_subscription_id
        except ObjectDoesNotExist:
            external_id = ""
        if external_id and not external_id.startswith(("sub_sim_", "sub_app_")):
            return external_id
        return f"sub_app_{organization.pk}_{plan.slug}"

    def create_checkout_session(self, organization, plan, token) -> dict[str, str]:
        customer = self._customer_id(organization)
        session = stripe.checkout.Session.create(
            mode="setup",
            success_url=getattr(
                settings, "BILLING_SUCCESS_URL", "https://example.test/billing/?ok=1"
            ),
            cancel_url=getattr(
                settings, "BILLING_CANCEL_URL", "https://example.test/billing/?cancel=1"
            ),
            customer=customer,
            client_reference_id=token,
            metadata={"organization_id": str(organization.pk), "plan": plan.slug},
        )
        return {"id": session.id, "url": session.url or ""}

    def attach_payment_method(self, organization, last4) -> str:
        # Cards must be collected via Checkout / SetupIntent. Storing last4 alone
        # does not create a chargeable Stripe PaymentMethod.
        _billing_error(
            "Stripe payment methods must be attached via Checkout or SetupIntent; "
            "cannot invent a payment-method id from last4."
        )
        return ""  # unreachable; satisfies type checkers

    def cancel(self, subscription) -> None:
        sub_id = subscription.external_subscription_id
        if sub_id and not str(sub_id).startswith(("sub_sim_", "sub_app_")):
            stripe.Subscription.delete(sub_id)

    def charge(
        self,
        payment_method,
        amount_cents: int,
        *,
        idempotency_key: str = "",
        customer_id: str = "",
    ) -> str | None:
        if amount_cents <= 0:
            return "ch_zero"
        if payment_method is None:
            return None
        ref = (getattr(payment_method, "gateway_ref", "") or "").strip()
        # Only real Stripe PaymentMethod ids (pm_…) — never pm_stripe_* placeholders
        # and never last4 simulation under a live Stripe key.
        if not ref.startswith("pm_") or ref.startswith("pm_stripe_"):
            logger.error(
                "Stripe charge refused: payment method has no real gateway_ref (%r)", ref
            )
            return None
        customer_id = (customer_id or "").strip()
        if not customer_id:
            organization = getattr(payment_method, "organization", None)
            subscription = None
            if organization:
                try:
                    subscription = organization.subscription
                except ObjectDoesNotExist:
                    subscription = None
            customer_id = (getattr(subscription, "external_customer_id", "") or "").strip()
        if not customer_id:
            logger.error("Stripe charge refused: no customer id for payment method %r", ref)
            return None
        try:
            params = {
                "amount": amount_cents,
                "currency": "usd",
                "payment_method": ref,
                "customer": customer_id,
                "confirm": True,
                "off_session": True,
            }
            if idempotency_key:
                params["idempotency_key"] = idempotency_key
            intent = stripe.PaymentIntent.create(
                **params,
            )
            return intent.id if intent.status == "succeeded" else None
        except stripe.error.StripeError as exc:
            logger.warning("Stripe charge failed: %s", exc)
            return None

    def refund(self, charge_id: str, amount_cents: int) -> str:
        if charge_id.startswith(("ch_sim_", "ch_zero")):
            return f"re_sim_{charge_id}_{amount_cents}"
        try:
            params = {"amount": amount_cents}
            if charge_id.startswith("pi_"):
                params["payment_intent"] = charge_id
            elif charge_id.startswith("ch_"):
                params["charge"] = charge_id
            else:
                _billing_error("Stripe refund requires a PaymentIntent or Charge id.")
            refund = stripe.Refund.create(**params)
            return refund.id
        except stripe.error.StripeError as exc:
            _billing_error(f"Stripe refund failed: {exc}")

    def payment_method_from_checkout_session(self, session_obj: dict) -> str:
        """Return the PaymentMethod collected by a setup-mode Checkout session."""
        payment_method = session_obj.get("payment_method") or ""
        if isinstance(payment_method, dict):
            payment_method = payment_method.get("id") or ""
        if payment_method:
            return str(payment_method)
        setup_intent = session_obj.get("setup_intent") or ""
        if isinstance(setup_intent, dict):
            setup_intent = setup_intent.get("id") or ""
        if not setup_intent:
            return ""
        try:
            intent = stripe.SetupIntent.retrieve(setup_intent)
        except stripe.error.StripeError as exc:
            logger.warning("Could not retrieve Stripe SetupIntent %s: %s", setup_intent, exc)
            return ""
        payment_method = getattr(intent, "payment_method", "") or ""
        if isinstance(payment_method, dict):
            payment_method = payment_method.get("id") or ""
        return str(payment_method)
