"""Financial depth: refunds, payment plans, fine amnesty, GL export, encumbrance.
 
Builds on the existing fees/payments layer in :mod:`library.services`. Fund
encumbrance (committing budget when an order is placed, converting it to spend
on receipt) is applied in :mod:`library.acquisitions`.
"""

from __future__ import annotations

import math

from django.db import transaction
from django.db.models import Sum

from .models import (
    Fee,
    FeeStatus,
    FeeType,
    Fund,
    Payment,
    PaymentAllocation,
    PaymentPlan,
    PaymentPlanStatus,
)
from .services import (
    DomainError,
    audit_action,
    emit_domain_event,
    patron_balance_cents,
    record_payment,
    waive_fee,
)


# --------------------------------------------------------------------------- #
# Refunds
# --------------------------------------------------------------------------- #
def _refunded_so_far(payment: Payment) -> int:
    return (
        Payment.objects.filter(
            kind="refund", reference=f"refund of payment #{payment.pk}"
        ).aggregate(total=Sum("amount_cents"))["total"]
        or 0
    )


def refund_payment(*, payment: Payment, amount_cents=None, actor=None, reason="") -> Payment:
    """Reverse (part of) a payment, re-opening the fees it had paid down."""
    from . import billing

    if payment.kind == "refund":
        raise DomainError("A refund cannot itself be refunded.")
    with transaction.atomic():
        # Lock the payment row so concurrent refunds can't each pass the cap.
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        allocations = (
            PaymentAllocation.objects.select_for_update()
            .filter(payment=payment)
            .select_related("fee")
            .order_by("-pk")
        )
        allocations = list(allocations)
        reversible = sum(
            allocation.remaining_cents
            for allocation in allocations
            if allocation.fee.status != FeeStatus.WAIVED
        )
        allocated = sum(allocation.amount_cents for allocation in allocations)
        unallocated = max(0, payment.amount_cents - allocated)
        # A prior refund can consume either allocated or unapplied value, but
        # never permit total refunds above the original payment.
        refundable = min(
            reversible + unallocated,
            max(0, payment.amount_cents - _refunded_so_far(payment)),
        )
        amount = refundable if amount_cents is None else int(amount_cents)
        if amount <= 0 or amount > refundable:
            raise DomainError(
                f"Invalid refund amount (at most {refundable}c refundable on this payment)."
            )
        # Write the ledger row before contacting the gateway.  This prevents a
        # successful external refund from existing without an internal record.
        refund = Payment.objects.create(
            organization=payment.organization,
            patron=payment.patron,
            amount_cents=amount,
            method="refund",
            kind="refund",
            reference=f"refund of payment #{payment.pk}",
            gateway_refund_id="",
            actor=actor,
        )
        if payment.method == "online" and payment.gateway_charge_id:
            try:
                gateway_refund_id = billing.get_gateway().refund(payment.gateway_charge_id, amount) or ""
            except billing.BillingError as exc:
                refund.delete()
                raise DomainError(str(exc)) from exc
            refund.gateway_refund_id = gateway_refund_id
            refund.save(update_fields=["gateway_refund_id", "updated_at"])
        # Reverse exactly the fees THIS payment paid, using its allocations.
        # A fee that has since been WAIVED is skipped — don't hand back cash for
        # a charge that was already forgiven.
        remaining = amount
        for allocation in allocations:
            if remaining <= 0:
                break
            fee = allocation.fee
            if fee.status == FeeStatus.WAIVED:
                continue
            reversible = min(allocation.remaining_cents, remaining)
            if reversible <= 0:
                continue
            fee.paid_cents = max(0, fee.paid_cents - reversible)
            if fee.status == FeeStatus.PAID and fee.paid_cents < fee.amount_cents:
                fee.status = FeeStatus.OUTSTANDING
            fee.save(update_fields=["paid_cents", "status", "updated_at"])
            allocation.reversed_cents += reversible
            allocation.save(update_fields=["reversed_cents", "updated_at"])
            remaining -= reversible
        audit_action(
            action="payment.refund", entity=refund, actor=actor,
            after={"amount_cents": amount, "reason": reason, "of_payment": payment.pk},
        )
        emit_domain_event(
            event_type="payment.refunded", aggregate=refund,
            payload={"amount_cents": amount, "patron_id": payment.patron_id}, actor=actor,
        )
        return refund


# --------------------------------------------------------------------------- #
# Payment plans
# --------------------------------------------------------------------------- #
def create_payment_plan(*, patron, total_cents, installments, actor=None) -> PaymentPlan:
    if total_cents <= 0 or installments < 1:
        raise DomainError("A payment plan needs a positive total and at least one installment.")
    balance = patron_balance_cents(patron)
    if balance <= 0:
        raise DomainError("This patron has no outstanding balance.")
    if total_cents > balance:
        raise DomainError("A payment plan cannot exceed the outstanding balance.")
    plan = PaymentPlan.objects.create(
        organization=patron.organization,
        patron=patron,
        total_cents=total_cents,
        installment_cents=math.ceil(total_cents / installments),
    )
    audit_action(action="plan.create", entity=plan, actor=actor)
    return plan


def pay_installment(
    *, plan: PaymentPlan, amount_cents=None, method="online", actor=None
) -> Payment:
    """Pay one installment (or a custom amount), allocating it to outstanding fees."""
    with transaction.atomic():
        plan = PaymentPlan.objects.select_for_update().get(pk=plan.pk)
        if plan.status != PaymentPlanStatus.ACTIVE:
            raise DomainError("This payment plan is not active.")
        want = plan.installment_cents if amount_cents is None else int(amount_cents)
        amount = min(plan.remaining_cents, max(0, want))
        if amount <= 0:
            raise DomainError("Nothing left to pay on this plan.")
        method = (method or "cash").strip().lower()
        if method == "online":
            from . import billing

            try:
                charge_result = billing.charge_online_amount(
                    organization=plan.organization,
                    amount_cents=amount,
                    idempotency_key=f"plan_pay:{plan.pk}:{amount}:{plan.remaining_cents}",
                )
                ok, charge_id = (
                    charge_result if isinstance(charge_result, tuple) else (charge_result, "")
                )
                if not ok:
                    raise DomainError("Card was declined.")
            except billing.BillingError as exc:
                raise DomainError(str(exc)) from exc
            payment = record_payment(
                patron=plan.patron,
                amount_cents=amount,
                method="online",
                reference=f"plan #{plan.pk}",
                actor=actor,
                captured=True,
                gateway_charge_id=charge_id,
            )
        else:
            payment = record_payment(
                patron=plan.patron,
                amount_cents=amount,
                method=method,
                reference=f"plan #{plan.pk}",
                actor=actor,
            )
        plan.paid_cents += amount
        if plan.paid_cents >= plan.total_cents:
            plan.status = PaymentPlanStatus.COMPLETED
        plan.save(update_fields=["paid_cents", "status", "updated_at"])
        return payment


# --------------------------------------------------------------------------- #
# Fine amnesty
# --------------------------------------------------------------------------- #
def run_amnesty(*, organization, fee_types=(FeeType.OVERDUE,), actor=None, reason="amnesty") -> int:
    """Waive all outstanding fees of the given types (a fine-forgiveness program)."""
    waived = 0
    fees = Fee.objects.filter(
        organization=organization, status=FeeStatus.OUTSTANDING, fee_type__in=list(fee_types)
    )
    for fee in fees:
        waive_fee(fee=fee, actor=actor, reason=reason)
        waived += 1
    audit_action(
        action="fee.amnesty", entity=organization, actor=actor, after={"waived": waived}
    )
    return waived


# --------------------------------------------------------------------------- #
# General-ledger export
# --------------------------------------------------------------------------- #
def gl_export(*, organization, start=None, end=None) -> list[dict]:
    """Journal-style rows for accounting/BI: fees assessed and payments/refunds."""
    rows: list[dict] = []
    fee_qs = Fee.objects.filter(organization=organization)
    pay_qs = Payment.objects.filter(organization=organization)
    if start:
        fee_qs = fee_qs.filter(created_at__date__gte=start)
        pay_qs = pay_qs.filter(created_at__date__gte=start)
    if end:
        # Inclusive of the whole end day (date comparison, not midnight cutoff).
        fee_qs = fee_qs.filter(created_at__date__lte=end)
        pay_qs = pay_qs.filter(created_at__date__lte=end)
    for fee in fee_qs.order_by("created_at", "id"):
        rows.append({
            "date": fee.created_at.date().isoformat(),
            "type": "fee",
            "category": fee.fee_type,
            "amount_cents": fee.amount_cents,
            "status": fee.status,
            "reference": f"fee:{fee.pk}",
        })
    for payment in pay_qs.order_by("created_at", "id"):
        rows.append({
            "date": payment.created_at.date().isoformat(),
            "type": payment.kind,
            "category": payment.method,
            "amount_cents": payment.amount_cents,
            "status": "posted",
            "reference": f"payment:{payment.pk}",
        })
    rows.sort(key=lambda r: (r["date"], r["reference"]))
    return rows


# --------------------------------------------------------------------------- #
# Fund encumbrance (used by acquisitions)
# --------------------------------------------------------------------------- #
def encumber(*, fund: Fund, amount_cents: int) -> None:
    if amount_cents <= 0:
        return
    fund = Fund.objects.select_for_update().get(pk=fund.pk)
    if fund.available_cents < amount_cents:
        raise DomainError("Insufficient available budget to commit this order.")
    fund.encumbered_cents += amount_cents
    fund.save(update_fields=["encumbered_cents", "updated_at"])


def release_encumbrance(*, fund: Fund, amount_cents: int) -> None:
    if amount_cents <= 0:
        return
    fund = Fund.objects.select_for_update().get(pk=fund.pk)
    fund.encumbered_cents = max(0, fund.encumbered_cents - amount_cents)
    fund.save(update_fields=["encumbered_cents", "updated_at"])


def spend_encumbered(*, fund: Fund, amount_cents: int) -> None:
    """Convert committed budget into actual spend on receipt."""
    if amount_cents <= 0:
        return
    fund = Fund.objects.select_for_update().get(pk=fund.pk)
    fund.encumbered_cents = max(0, fund.encumbered_cents - amount_cents)
    fund.spent_cents += amount_cents
    fund.save(update_fields=["encumbered_cents", "spent_cents", "updated_at"])


__all__ = [
    "create_payment_plan",
    "encumber",
    "gl_export",
    "pay_installment",
    "refund_payment",
    "release_encumbrance",
    "run_amnesty",
    "spend_encumbered",
]
