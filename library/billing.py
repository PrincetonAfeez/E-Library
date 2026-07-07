"""Subscription billing + tenant provisioning.

The payment gateway is abstracted so the app runs fully without a real
processor. A ``SimulatedGateway`` models a complete lifecycle — hosted
checkout, cards on file, charges, proration, dunning and auto-renewal —
deterministically and offline, so the whole product is exercisable in tests.

Test convention (mirrors Stripe's test cards): a payment method whose
``last4`` is ``"0000"`` always declines; any other card succeeds.

When ``STRIPE_SECRET_KEY`` is set and a ``billing_stripe.StripeGateway`` is
available it is used instead — the service layer is written against the
abstraction — but no real gateway is required.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.utils.text import slugify

from .models import (
    Branch,
    CheckoutSession,
    CheckoutStatus,
    FeePolicy,
    Invoice,
    InvoiceLineItem,
    InvoiceStatus,
    Organization,
    PaymentMethod,
    Plan,
    StaffMembership,
    StaffRole,
    Subscription,
    SubscriptionStatus,
)
from .notifications import ensure_default_templates
from .services import audit_action, emit_domain_event

logger = logging.getLogger("library")

# Length of a billing period, in days, for a monthly plan.
PERIOD_DAYS = 30
# Days a past-due tenant keeps service while dunning retries run.
GRACE_DAYS = 7
# Failed charges tolerated before a past-due subscription is canceled.
MAX_DUNNING = 3

# Gateway event type -> subscription status transition.
GATEWAY_STATUS_MAP = {
    "invoice.paid": SubscriptionStatus.ACTIVE,
    "invoice.payment_succeeded": SubscriptionStatus.ACTIVE,
    "customer.subscription.updated": SubscriptionStatus.ACTIVE,
    "invoice.payment_failed": SubscriptionStatus.PAST_DUE,
    "customer.subscription.deleted": SubscriptionStatus.CANCELED,
}


class BillingError(Exception):
    pass


class SimulatedGateway:
    """A deterministic, offline payment processor modelling a full lifecycle."""

    name = "simulated"

    def create_customer(self, organization) -> str:
        return f"cus_sim_{organization.pk}"

    def create_subscription(self, organization, plan) -> str:
        return f"sub_sim_{organization.pk}_{plan.slug}"

    def create_checkout_session(self, organization, plan, token) -> str:
        return f"cs_sim_{token[:16]}"

    def attach_payment_method(self, organization, last4) -> str:
        return f"pm_sim_{organization.pk}_{last4}"

    def cancel(self, subscription) -> None:
        return None

    def charge(self, payment_method, amount_cents: int) -> bool:
        """Return True if the charge succeeds. Zero-amount always succeeds."""
        if amount_cents <= 0:
            return True
        if payment_method is None:
            return False
        # The 4000-0000-0000-0000 test card (last4 "0000") always declines.
        return payment_method.last4 != "0000"


# Kept for backwards compatibility; behaves like SimulatedGateway with no card.
class ManualGateway(SimulatedGateway):
    name = "manual"


def get_gateway():
    """Return the configured payment gateway (Stripe when available, else simulated)."""
    if getattr(settings, "STRIPE_SECRET_KEY", ""):
        try:  # pragma: no cover - only when Stripe is configured
            import stripe

            from .billing_stripe import StripeGateway

            stripe.api_key = settings.STRIPE_SECRET_KEY
            return StripeGateway()
        except ImportError:
            logger.warning("STRIPE_SECRET_KEY set but stripe SDK/gateway unavailable; using simulated.")
    return SimulatedGateway()


def default_plan() -> Plan | None:
    return (
        Plan.objects.filter(slug="trial", active=True).first()
        or Plan.objects.filter(active=True).order_by("price_cents").first()
    )


def _usage(organization) -> dict:
    from .models import Copy, PatronProfile

    return {
        "branches": Branch.objects.filter(organization=organization).count(),
        "patrons": PatronProfile.objects.filter(organization=organization).count(),
        "copies": Copy.objects.filter(organization=organization).count(),
    }


# --------------------------------------------------------------------------- #
# Payment methods
# --------------------------------------------------------------------------- #
def default_payment_method(organization) -> PaymentMethod | None:
    return (
        PaymentMethod.objects.filter(organization=organization, is_default=True).first()
        or PaymentMethod.objects.filter(organization=organization).first()
    )


def add_payment_method(
    *, organization, brand="visa", last4="4242", exp_month=12, exp_year=2030, make_default=True,
    actor=None,
) -> PaymentMethod:
    """Store a simulated card on file. Never persists a real PAN — brand + last4 only."""
    last4 = str(last4)[-4:].rjust(4, "0")
    with transaction.atomic():
        if make_default:
            PaymentMethod.objects.filter(organization=organization).update(is_default=False)
        method = PaymentMethod.objects.create(
            organization=organization,
            gateway_ref=get_gateway().attach_payment_method(organization, last4),
            brand=brand,
            last4=last4,
            exp_month=exp_month,
            exp_year=exp_year,
            is_default=make_default or not PaymentMethod.objects.filter(organization=organization).exists(),
        )
    audit_action(action="billing.payment_method.add", entity=method, actor=actor, source="billing")
    return method


# --------------------------------------------------------------------------- #
# Invoicing
# --------------------------------------------------------------------------- #
def _issue_invoice(
    subscription, *, amount_cents, description, paid, lines=None, period_start=None, period_end=None,
) -> Invoice:
    now = timezone.now()
    invoice = Invoice.objects.create(
        organization=subscription.organization,
        subscription=subscription,
        amount_cents=max(0, amount_cents),
        status=InvoiceStatus.PAID if paid else InvoiceStatus.OPEN,
        description=description,
        period_start=period_start,
        period_end=period_end,
        paid_at=now if paid else None,
    )
    for line_desc, line_amount in lines or [(description, amount_cents)]:
        InvoiceLineItem.objects.create(
            invoice=invoice, description=line_desc, amount_cents=line_amount
        )
    return invoice


# --------------------------------------------------------------------------- #
# Trials & activation
# --------------------------------------------------------------------------- #
def start_trial(*, organization, plan, trial_days: int = 30, actor=None) -> Subscription:
    gateway = get_gateway()
    now = timezone.now()
    ends = now + timedelta(days=trial_days)
    subscription, _ = Subscription.objects.update_or_create(
        organization=organization,
        defaults={
            "plan": plan,
            "status": SubscriptionStatus.TRIALING,
            "trial_ends_at": ends,
            "current_period_end": ends,
            "dunning_attempts": 0,
            "grace_until": None,
            "external_customer_id": gateway.create_customer(organization),
        },
    )
    audit_action(action="subscription.trial", entity=subscription, actor=actor, source="billing")
    emit_domain_event(
        event_type="subscription.trial_started",
        aggregate=subscription,
        payload={"plan": plan.slug, "trial_ends_at": ends.isoformat()},
        actor=actor,
        source="billing",
    )
    return subscription


def subscribe(*, organization, plan, actor=None) -> Subscription:
    """Activate a subscription, charging the card on file (if any) for the period."""
    gateway = get_gateway()
    now = timezone.now()
    period_end = now + timedelta(days=PERIOD_DAYS)
    existing = get_subscription(organization)
    method = default_payment_method(organization)
    # Apply any banked account credit (e.g. from a prior downgrade) first.
    available_credit = existing.credit_cents if existing else 0
    credit_used = min(available_credit, plan.price_cents)
    net = plan.price_cents - credit_used
    # With no card on file the plan is comped/activated (e.g. admin action);
    # with a card, a paid plan must clear before it goes active.
    charged = True
    if net > 0 and method is not None:
        charged = gateway.charge(method, net)
    consumed_credit = credit_used if (net == 0 or charged) else 0
    status = SubscriptionStatus.ACTIVE if charged else SubscriptionStatus.PAST_DUE
    subscription, _ = Subscription.objects.update_or_create(
        organization=organization,
        defaults={
            "plan": plan,
            "status": status,
            "current_period_end": period_end,
            "dunning_attempts": 0 if charged else 1,
            "grace_until": None if charged else now + timedelta(days=GRACE_DAYS),
            "credit_cents": available_credit - consumed_credit,
            "external_customer_id": (
                existing.external_customer_id if existing else gateway.create_customer(organization)
            ),
            "external_subscription_id": gateway.create_subscription(organization, plan),
        },
    )
    paid = net == 0 or (method is not None and charged)
    lines = [(f"{plan.name} subscription", plan.price_cents)]
    if consumed_credit:
        lines.append(("Account credit applied", -consumed_credit))
    invoice = _issue_invoice(
        subscription,
        amount_cents=net,
        description=f"{plan.name} subscription",
        paid=paid,
        lines=lines,
        period_start=now,
        period_end=period_end,
    )
    audit_action(
        action="subscription.subscribe",
        entity=subscription,
        actor=actor,
        after={"plan": plan.slug, "invoice_id": invoice.pk, "charged": charged},
        source="billing",
    )
    emit_domain_event(
        event_type="subscription.activated" if charged else "subscription.payment_failed",
        aggregate=subscription,
        payload={"plan": plan.slug},
        actor=actor,
        source="billing",
    )
    return subscription


# --------------------------------------------------------------------------- #
# Hosted checkout (simulated)
# --------------------------------------------------------------------------- #
def create_checkout(*, organization, plan, actor=None) -> CheckoutSession:
    """Open a simulated hosted-checkout session for a plan."""
    token = secrets.token_urlsafe(24)
    session = CheckoutSession.objects.create(organization=organization, plan=plan, token=token)
    get_gateway().create_checkout_session(organization, plan, token)
    audit_action(action="billing.checkout.create", entity=session, actor=actor, source="billing")
    return session


@transaction.atomic
def complete_checkout(
    *, session: CheckoutSession, brand="visa", last4="4242", exp_month=12, exp_year=2030, actor=None,
) -> Subscription:
    """Complete checkout: store the card, charge it, activate — or raise on decline."""
    session = CheckoutSession.objects.select_for_update().get(pk=session.pk)
    if session.status != CheckoutStatus.OPEN:
        raise BillingError("This checkout session has already been used.")
    add_payment_method(
        organization=session.organization,
        brand=brand,
        last4=last4,
        exp_month=exp_month,
        exp_year=exp_year,
        make_default=True,
        actor=actor,
    )
    # subscribe() is the single charge point (it charges the default card). A
    # decline leaves the subscription PAST_DUE; raising rolls the whole atomic
    # block back so no card, subscription, or invoice persists.
    subscription = subscribe(organization=session.organization, plan=session.plan, actor=actor)
    if subscription.status == SubscriptionStatus.PAST_DUE:
        raise BillingError("Your card was declined. Please try a different card.")
    session.status = CheckoutStatus.COMPLETED
    session.completed_at = timezone.now()
    session.save(update_fields=["status", "completed_at", "updated_at"])
    return subscription


# --------------------------------------------------------------------------- #
# Renewal & dunning
# --------------------------------------------------------------------------- #
def _enter_dunning(subscription: Subscription, *, actor=None) -> None:
    now = timezone.now()
    subscription.dunning_attempts += 1
    subscription.status = SubscriptionStatus.PAST_DUE
    if subscription.grace_until is None or subscription.grace_until <= now:
        subscription.grace_until = now + timedelta(days=GRACE_DAYS)
    subscription.save(
        update_fields=["dunning_attempts", "status", "grace_until", "updated_at"]
    )
    emit_domain_event(
        event_type="subscription.payment_failed",
        aggregate=subscription,
        payload={"attempt": subscription.dunning_attempts},
        actor=actor,
        source="billing",
    )


def _apply_credit(subscription: Subscription, gross_cents: int) -> tuple[int, int]:
    """Return (amount to charge the card, credit consumed) given account credit."""
    credit_used = min(subscription.credit_cents, gross_cents)
    return gross_cents - credit_used, credit_used


def _open_renewal_invoice(subscription: Subscription) -> Invoice | None:
    """The current outstanding renewal invoice, reused across dunning retries."""
    return (
        Invoice.objects.filter(
            subscription=subscription,
            status=InvoiceStatus.OPEN,
            description__endswith="renewal",
        )
        .order_by("-created_at")
        .first()
    )


def charge_subscription(*, subscription: Subscription, actor=None) -> bool:
    """Attempt to renew a period: charge the card on file and issue an invoice."""
    plan = subscription.plan
    now = timezone.now()
    period_end = now + timedelta(days=PERIOD_DAYS)
    net, credit_used = _apply_credit(subscription, plan.price_cents)
    method = default_payment_method(subscription.organization)
    charged = net == 0 or (method is not None and get_gateway().charge(method, net))

    # Reuse the outstanding renewal invoice on a dunning retry instead of piling
    # up a fresh unpaid invoice every cycle.
    invoice = _open_renewal_invoice(subscription)
    if invoice is None:
        _issue_invoice(
            subscription,
            amount_cents=plan.price_cents,
            description=f"{plan.name} renewal",
            paid=charged,
            period_start=now,
            period_end=period_end,
        )
    else:
        invoice.status = InvoiceStatus.PAID if charged else InvoiceStatus.OPEN
        invoice.paid_at = now if charged else None
        invoice.period_start = now
        invoice.period_end = period_end
        invoice.save(
            update_fields=["status", "paid_at", "period_start", "period_end", "updated_at"]
        )

    if charged:
        if credit_used:
            Subscription.objects.filter(pk=subscription.pk).update(
                credit_cents=F("credit_cents") - credit_used
            )
            subscription.credit_cents = max(0, subscription.credit_cents - credit_used)
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.current_period_end = period_end
        subscription.dunning_attempts = 0
        subscription.grace_until = None
        subscription.save(
            update_fields=[
                "status", "current_period_end", "dunning_attempts", "grace_until", "updated_at",
            ]
        )
        emit_domain_event(
            event_type="subscription.renewed",
            aggregate=subscription,
            payload={"plan": plan.slug, "credit_used": credit_used},
            actor=actor,
            source="billing",
        )
    else:
        _enter_dunning(subscription, actor=actor)
    return charged


def run_billing_cycle(*, now=None, max_dunning: int = MAX_DUNNING) -> dict:
    """Renew due subscriptions and advance dunning. Safe to run daily/idempotently."""
    now = now or timezone.now()
    result = {"renewed": 0, "dunning": 0, "canceled": 0}
    due = Subscription.objects.filter(
        status__in=[SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING],
        current_period_end__lte=now,
    ).select_related("plan", "organization")
    for subscription in due:
        if charge_subscription(subscription=subscription):
            result["renewed"] += 1
        else:
            result["dunning"] += 1
    for subscription in Subscription.objects.filter(
        status=SubscriptionStatus.PAST_DUE
    ).select_related("plan", "organization"):
        exhausted = subscription.dunning_attempts >= max_dunning and (
            subscription.grace_until is None or subscription.grace_until <= now
        )
        if exhausted:
            cancel_subscription(subscription=subscription)
            result["canceled"] += 1
        elif charge_subscription(subscription=subscription):
            result["renewed"] += 1
        else:
            result["dunning"] += 1
    return result


# --------------------------------------------------------------------------- #
# Plan changes (with proration) & cancellation
# --------------------------------------------------------------------------- #
def _prorate(subscription: Subscription, old_plan: Plan, new_plan: Plan, now) -> dict:
    """Credit the unused portion of the old plan and charge the prorated new plan."""
    remaining_days = 0
    if subscription.current_period_end and subscription.current_period_end > now:
        remaining_days = (subscription.current_period_end - now).days
    fraction = remaining_days / PERIOD_DAYS
    credit = int(round(old_plan.price_cents * fraction))
    charge = int(round(new_plan.price_cents * fraction))
    lines = []
    if credit:
        lines.append((f"Credit: unused {old_plan.name}", -credit))
    if charge:
        lines.append((f"Charge: {new_plan.name} (prorated)", charge))
    return {"net_cents": charge - credit, "lines": lines}


def change_plan(*, subscription: Subscription, new_plan: Plan, actor=None) -> Subscription:
    """Switch plans (with proration), refusing a downgrade current usage won't fit."""
    usage = _usage(subscription.organization)
    for resource, attr in (
        ("branches", "max_branches"), ("patrons", "max_patrons"), ("copies", "max_copies")
    ):
        cap = getattr(new_plan, attr)
        if cap is not None and usage[resource] > cap:
            raise BillingError(
                f"Cannot switch to {new_plan.name}: current {resource} ({usage[resource]}) "
                f"exceeds the plan limit ({cap})."
            )
    old_plan = subscription.plan
    now = timezone.now()
    # A trial isn't paying yet, so switching plans mid-trial is free — payment
    # (and any proration) happens when the trial converts at renewal. Proration
    # and upfront charging apply only to an active, paying subscription.
    is_trial = subscription.status == SubscriptionStatus.TRIALING
    proration = {"net_cents": 0, "lines": []} if is_trial else _prorate(
        subscription, old_plan, new_plan, now
    )
    net = proration["net_cents"]
    # For an upgrade the prorated amount must clear BEFORE the higher plan is
    # granted — never deliver the new tier on an unpaid invoice.
    paid = True
    if net > 0:
        method = default_payment_method(subscription.organization)
        paid = method is not None and get_gateway().charge(method, net)
        if not paid:
            raise BillingError("Your card was declined for the plan change.")
    subscription.plan = new_plan
    if not is_trial:
        subscription.status = SubscriptionStatus.ACTIVE
    subscription.save(update_fields=["plan", "status", "updated_at"])
    if proration["lines"]:
        if net < 0:
            # Downgrade: the unused-time credit is banked as account credit and
            # applied to the next renewal, rather than silently discarded.
            subscription.credit_cents = F("credit_cents") + (-net)
            subscription.save(update_fields=["credit_cents", "updated_at"])
            subscription.refresh_from_db(fields=["credit_cents"])
        _issue_invoice(
            subscription,
            amount_cents=max(0, net),
            description=f"Plan change to {new_plan.name}",
            paid=paid,
            lines=proration["lines"],
            period_start=now,
            period_end=subscription.current_period_end,
        )
    audit_action(
        action="subscription.change_plan",
        entity=subscription,
        actor=actor,
        before={"plan": old_plan.slug},
        after={"plan": new_plan.slug, "proration_cents": proration["net_cents"]},
        source="billing",
    )
    return subscription


def cancel_subscription(*, subscription: Subscription, actor=None) -> Subscription:
    get_gateway().cancel(subscription)
    subscription.status = SubscriptionStatus.CANCELED
    subscription.save(update_fields=["status", "updated_at"])
    audit_action(action="subscription.cancel", entity=subscription, actor=actor, source="billing")
    emit_domain_event(
        event_type="subscription.canceled",
        aggregate=subscription,
        payload={},
        actor=actor,
        source="billing",
    )
    return subscription


def get_subscription(organization) -> Subscription | None:
    return Subscription.objects.filter(organization=organization).select_related("plan").first()


def billing_overview(organization) -> dict:
    subscription = get_subscription(organization)
    usage = _usage(organization)
    limits = {}
    if subscription:
        plan = subscription.plan
        limits = {
            "branches": plan.max_branches,
            "patrons": plan.max_patrons,
            "copies": plan.max_copies,
        }
    return {
        "subscription": subscription,
        "usage": usage,
        "limits": limits,
        "invoices": list(Invoice.objects.filter(organization=organization)[:24]),
        "payment_methods": list(PaymentMethod.objects.filter(organization=organization)),
    }


def handle_gateway_event(event: dict) -> bool:
    """Apply a Stripe-style webhook event to the matching subscription. Idempotent."""
    event_type = event.get("type")
    new_status = GATEWAY_STATUS_MAP.get(event_type)
    if new_status is None:
        return False
    obj = (event.get("data") or {}).get("object") or {}
    sub_id = obj.get("subscription") or obj.get("id") or ""
    customer_id = obj.get("customer") or ""
    subscription = None
    if sub_id:
        subscription = Subscription.objects.filter(external_subscription_id=sub_id).first()
    if subscription is None and customer_id:
        subscription = Subscription.objects.filter(external_customer_id=customer_id).first()
    if subscription is None:
        logger.warning("Billing webhook %s matched no subscription", event_type)
        return False
    if subscription.status != new_status:
        subscription.status = new_status
        subscription.save(update_fields=["status", "updated_at"])
        audit_action(
            action="subscription.webhook",
            entity=subscription,
            after={"event": event_type, "status": new_status},
            source="billing",
        )
    return True


@transaction.atomic
def provision_tenant(
    *, name: str, slug: str, owner_user, plan: Plan | None = None, branch_name: str = "Main",
    trial_days: int = 30,
) -> Organization:
    """Create a new library tenant: org + first branch + owner (admin) + trial."""
    organization = Organization.objects.create(name=name, slug=slug)
    Branch.objects.create(
        organization=organization, name=branch_name, slug=slugify(branch_name) or "main"
    )
    StaffMembership.objects.create(
        user=owner_user, organization=organization, branch=None, role=StaffRole.ADMIN
    )
    FeePolicy.objects.create(organization=organization)
    ensure_default_templates(organization)
    chosen = plan or default_plan()
    if chosen is not None:
        start_trial(organization=organization, plan=chosen, trial_days=trial_days, actor=owner_user)
    audit_action(action="tenant.provisioned", entity=organization, actor=owner_user, source="billing")
    emit_domain_event(
        event_type="tenant.provisioned",
        aggregate=organization,
        payload={"slug": slug, "plan": chosen.slug if chosen else None},
        actor=owner_user,
        source="billing",
    )
    return organization


# Re-exported so callers importing from billing get a single surface.
__all__ = [
    "BillingError",
    "add_payment_method",
    "billing_overview",
    "cancel_subscription",
    "change_plan",
    "charge_subscription",
    "complete_checkout",
    "create_checkout",
    "default_payment_method",
    "get_subscription",
    "handle_gateway_event",
    "provision_tenant",
    "run_billing_cycle",
    "start_trial",
    "subscribe",
]
