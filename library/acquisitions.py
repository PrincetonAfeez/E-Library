"""Acquisitions: vendors, funds/budgets, purchase orders, and receiving.

Receiving a line spends its fund and materializes physical ``Copy`` rows (barcode
auto-generated), respecting the tenant's plan copy limit. Budgets are enforced at
receipt.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from . import entitlements
from .models import (
    Copy,
    Fund,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
)
from .services import DomainError, audit_action, emit_domain_event


def create_purchase_order(*, organization, vendor, fund, created_by=None) -> PurchaseOrder:
    if vendor.organization_id != organization.pk or fund.organization_id != organization.pk:
        raise DomainError("Vendor and fund must belong to this organization.")
    po = PurchaseOrder.objects.create(
        organization=organization, vendor=vendor, fund=fund, created_by=created_by
    )
    audit_action(action="acq.po_create", entity=po, actor=created_by, source="acquisitions")
    return po


def add_line(*, purchase_order, edition=None, title_text="", branch, quantity=1, unit_cost_cents=0):
    if purchase_order.status != PurchaseOrderStatus.DRAFT:
        raise DomainError("Lines can only be added to a draft order.")
    if quantity < 1:
        raise DomainError("Quantity must be at least 1.")
    if branch.organization_id != purchase_order.organization_id:
        raise DomainError("Branch must belong to this organization.")
    return PurchaseOrderLine.objects.create(
        purchase_order=purchase_order,
        edition=edition,
        title_text=title_text or (str(edition.work.canonical_title) if edition else ""),
        branch=branch,
        quantity=quantity,
        unit_cost_cents=unit_cost_cents,
    )


def order_total_cents(purchase_order) -> int:
    return sum(line.quantity * line.unit_cost_cents for line in purchase_order.lines.all())


def place_order(*, purchase_order, actor=None) -> PurchaseOrder:
    with transaction.atomic():
        po = PurchaseOrder.objects.select_for_update().get(pk=purchase_order.pk)
        if po.status != PurchaseOrderStatus.DRAFT:
            raise DomainError("Only draft orders can be placed.")
        if not po.lines.exists():
            raise DomainError("Add at least one line before ordering.")
        # Commit (encumber) the order total against the fund's available budget.
        from . import finance

        finance.encumber(fund=po.fund, amount_cents=order_total_cents(po))
        po.status = PurchaseOrderStatus.ORDERED
        po.ordered_at = timezone.now()
        po.save(update_fields=["status", "ordered_at", "updated_at"])
        audit_action(action="acq.po_place", entity=po, actor=actor, source="acquisitions")
        emit_domain_event(
            event_type="acq.order_placed",
            aggregate=po,
            payload={"total_cents": order_total_cents(po)},
            actor=actor,
            source="acquisitions",
        )
        return po


def receive_line(*, line: PurchaseOrderLine, quantity: int, actor=None) -> PurchaseOrderLine:
    """Receive ``quantity`` items: spend the fund and create physical copies."""
    with transaction.atomic():
        line = (
            # of=("self",): edition is nullable, so select_related("edition") is a
            # LEFT JOIN that PostgreSQL refuses to lock. Lock just the line row.
            PurchaseOrderLine.objects.select_for_update(of=("self",))
            .select_related("purchase_order__fund", "purchase_order", "edition", "branch")
            .get(pk=line.pk)
        )
        po = line.purchase_order
        if po.status not in (PurchaseOrderStatus.ORDERED, PurchaseOrderStatus.RECEIVED):
            raise DomainError("Only ordered items can be received.")
        if quantity < 1 or quantity > line.outstanding:
            raise DomainError("Invalid receive quantity.")
        if line.edition is None:
            raise DomainError("Cannot shelve a line without a catalog edition.")

        try:
            entitlements.assert_within_limit(po.organization, "copies", adding=quantity)
        except entitlements.EntitlementError as exc:
            raise DomainError(str(exc)) from exc

        from . import finance

        fund = Fund.objects.select_for_update().get(pk=po.fund_id)
        cost = quantity * line.unit_cost_cents
        if fund.spent_cents + cost > fund.budget_cents:
            raise DomainError("Insufficient fund budget for this receipt.")

        # Materialize copies with generated barcodes.
        start = line.received_quantity
        for n in range(quantity):
            Copy.objects.create(
                organization=po.organization,
                edition=line.edition,
                branch=line.branch,
                barcode=f"ACQ-{po.pk}-{line.pk}-{start + n + 1}",
            )
        # Convert the committed (encumbered) amount for this receipt into spend.
        finance.spend_encumbered(fund=fund, amount_cents=cost)
        line.received_quantity += quantity
        line.save(update_fields=["received_quantity", "updated_at"])

        if all(ln.outstanding == 0 for ln in po.lines.all()):
            po.status = PurchaseOrderStatus.RECEIVED
            po.received_at = timezone.now()
            po.save(update_fields=["status", "received_at", "updated_at"])

        audit_action(
            action="acq.receive",
            entity=line,
            actor=actor,
            after={"quantity": quantity, "cost_cents": cost},
            source="acquisitions",
        )
        emit_domain_event(
            event_type="acq.received",
            aggregate=po,
            payload={"line_id": line.pk, "quantity": quantity},
            actor=actor,
            source="acquisitions",
        )
        return line


def cancel_order(*, purchase_order, actor=None) -> PurchaseOrder:
    with transaction.atomic():
        po = PurchaseOrder.objects.select_for_update().get(pk=purchase_order.pk)
        if po.status == PurchaseOrderStatus.RECEIVED:
            raise DomainError("A fully received order cannot be cancelled.")
        # Release the encumbrance for the still-outstanding (unreceived) portion.
        if po.status == PurchaseOrderStatus.ORDERED:
            from . import finance

            outstanding_cents = sum(
                ln.outstanding * ln.unit_cost_cents for ln in po.lines.all()
            )
            finance.release_encumbrance(fund=po.fund, amount_cents=outstanding_cents)
        po.status = PurchaseOrderStatus.CANCELLED
        po.save(update_fields=["status", "updated_at"])
        audit_action(action="acq.po_cancel", entity=po, actor=actor, source="acquisitions")
        return po
