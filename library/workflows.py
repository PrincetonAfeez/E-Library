"""Staff productivity workflows: bulk copy ops, inventory/stocktake, lost/damaged

These are the daily-driver desk tools: batch-editing and weeding copies,
running a barcode stocktake that reports missing/unexpected items, and handling
lost / claims-returned / damaged loans (billing replacement or damage fees).
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .models import (
    Copy,
    CopyCondition,
    CopyStatus,
    Fee,
    FeeType,
    InventorySession,
    InventoryStatus,
    Loan,
    LoanStatus,
    stable_patron_hash,
)
from .services import (
    DomainError,
    assess_overdue_fine,
    audit_action,
    emit_domain_event,
    fee_policy_for,
)

# Copy statuses that are safe to bulk-mutate (never touch in-flight items).
_MUTABLE_STATUSES = {CopyStatus.AVAILABLE, CopyStatus.REPAIR}


# --------------------------------------------------------------------------- #
# Bulk copy operations
# --------------------------------------------------------------------------- #
def bulk_update_copies(
    *, organization, barcodes, status=None, shelf_location=None, public_visible=None, actor=None,
) -> dict:
    """Batch-edit copies by barcode. In-flight copies (loaned/held/transit) are skipped."""
    if status is not None and status not in _MUTABLE_STATUSES:
        raise DomainError("Bulk status changes are limited to available/repair.")
    updated, skipped = 0, []
    with transaction.atomic():
        copies = (
            Copy.objects.select_for_update()
            .filter(organization=organization, barcode__in=list(barcodes))
        )
        found = {c.barcode for c in copies}
        for copy in copies:
            if copy.status not in _MUTABLE_STATUSES:
                skipped.append(copy.barcode)
                continue
            fields = []
            if status is not None:
                copy.status = status
                fields.append("status")
            if public_visible is not None:
                copy.public_visible = public_visible
                fields.append("public_visible")
            if shelf_location is not None:
                copy.shelf_location = shelf_location
                fields.append("shelf_location")
            if fields:
                fields.append("updated_at")
                copy.save(update_fields=fields)
                updated += 1
        missing = [b for b in barcodes if b not in found]
    audit_action(
        action="copy.bulk_update", entity=organization, actor=actor,
        after={"updated": updated, "skipped": len(skipped)}, source="workflows",
    )
    return {"updated": updated, "skipped": skipped, "not_found": missing}


def weed_copies(*, organization, barcodes, reason="weeded", actor=None) -> dict:
    """Retire (withdraw) copies not currently on loan/hold/transit."""
    retired, skipped = 0, []
    with transaction.atomic():
        copies = (
            Copy.objects.select_for_update()
            .filter(organization=organization, barcode__in=list(barcodes))
        )
        for copy in copies:
            if copy.status in (
                CopyStatus.LOANED, CopyStatus.ON_HOLD, CopyStatus.IN_TRANSIT, CopyStatus.ILL,
            ):
                skipped.append(copy.barcode)
                continue
            copy.status = CopyStatus.RETIRED
            copy.public_visible = False
            copy.save(update_fields=["status", "public_visible", "updated_at"])
            retired += 1
    audit_action(
        action="copy.weed", entity=organization, actor=actor,
        after={"retired": retired, "reason": reason}, source="workflows",
    )
    return {"retired": retired, "skipped": skipped}


# --------------------------------------------------------------------------- #
# Inventory / stocktake
# --------------------------------------------------------------------------- #
def start_inventory(*, organization, branch, actor=None) -> InventorySession:
    if branch.organization_id != organization.pk:
        raise DomainError("Branch must belong to this organization.")
    session = InventorySession.objects.create(
        organization=organization, branch=branch, started_by=actor
    )
    audit_action(action="inventory.start", entity=session, actor=actor, source="workflows")
    return session


def scan_inventory(*, session: InventorySession, barcode: str) -> str:
    """Record a scanned barcode. Returns 'ok', 'unexpected', or 'duplicate'."""
    barcode = (barcode or "").strip()
    if not barcode:
        raise DomainError("Empty barcode.")
    with transaction.atomic():
        session = InventorySession.objects.select_for_update().get(pk=session.pk)
        if session.status != InventoryStatus.OPEN:
            raise DomainError("This inventory session is closed.")
        if barcode in session.scanned_barcodes:
            return "duplicate"
        exists = Copy.objects.filter(
            organization=session.organization, branch=session.branch, barcode=barcode
        ).exists()
        session.scanned_barcodes = [*session.scanned_barcodes, barcode]
        session.save(update_fields=["scanned_barcodes", "updated_at"])
        return "ok" if exists else "unexpected"


def close_inventory(*, session: InventorySession, actor=None) -> InventorySession:
    """Close the session and compute missing/unexpected reports."""
    with transaction.atomic():
        session = InventorySession.objects.select_for_update().get(pk=session.pk)
        if session.status != InventoryStatus.OPEN:
            raise DomainError("This inventory session is already closed.")
        scanned = set(session.scanned_barcodes)
        # Expected: copies that should physically be on the shelf here.
        expected = set(
            Copy.objects.filter(
                organization=session.organization,
                branch=session.branch,
                status__in=[CopyStatus.AVAILABLE, CopyStatus.ON_HOLD, CopyStatus.REPAIR],
            ).values_list("barcode", flat=True)
        )
        all_here = set(
            Copy.objects.filter(
                organization=session.organization, branch=session.branch
            ).values_list("barcode", flat=True)
        )
        # Scanned on the shelf but still checked out / in transit here — found,
        # but needs a check-in to be resolved.
        checked_out = set(
            Copy.objects.filter(
                organization=session.organization,
                branch=session.branch,
                barcode__in=list(scanned),
                status__in=[CopyStatus.LOANED, CopyStatus.IN_TRANSIT],
            ).values_list("barcode", flat=True)
        )
        session.missing_barcodes = sorted(expected - scanned)
        session.unexpected_barcodes = sorted(scanned - all_here)
        session.found_checked_out_barcodes = sorted(checked_out)
        session.status = InventoryStatus.CLOSED
        session.closed_at = timezone.now()
        session.save(
            update_fields=[
                "missing_barcodes", "unexpected_barcodes", "found_checked_out_barcodes",
                "status", "closed_at", "updated_at",
            ]
        )
    audit_action(
        action="inventory.close", entity=session, actor=actor,
        after={
            "missing": len(session.missing_barcodes),
            "unexpected": len(session.unexpected_barcodes),
            "found_checked_out": len(session.found_checked_out_barcodes),
        },
        source="workflows",
    )
    return session


# --------------------------------------------------------------------------- #
# Lost / claims-returned / damaged
# --------------------------------------------------------------------------- #
def _active_loan_locked(loan_id):
    loan = (
        Loan.objects.select_for_update(of=("self",))
        .select_related("copy", "patron")
        .get(pk=loan_id)
    )
    if loan.status not in (LoanStatus.ACTIVE, LoanStatus.OVERDUE):
        raise DomainError("Only an active or overdue loan can be updated.")
    return loan


def mark_loan_lost(*, loan: Loan, actor=None, source="staff") -> Fee:
    """Declare a loan lost: bill the replacement fee and mark the copy lost."""
    with transaction.atomic():
        loan = _active_loan_locked(loan.pk)
        if loan.patron_id is None:
            raise DomainError("Loan has no patron to bill.")
        assess_overdue_fine(loan=loan)  # finalize accrued overdue while attached
        policy = fee_policy_for(loan.organization)
        fee = Fee.objects.create(
            organization=loan.organization,
            patron_id=loan.patron_id,
            loan=loan,
            fee_type=FeeType.LOST,
            amount_cents=policy.lost_item_fee_cents,
            description="Lost item replacement",
        )
        loan.status = LoanStatus.LOST
        loan.save(update_fields=["status", "updated_at"])
        copy = Copy.objects.select_for_update().get(pk=loan.copy_id)
        copy.status = CopyStatus.LOST
        copy.save(update_fields=["status", "updated_at"])
        audit_action(action="loan.lost", entity=loan, actor=actor, source=source)
        emit_domain_event(
            event_type="loan.lost", aggregate=loan,
            payload={"fee_cents": fee.amount_cents}, actor=actor, source=source,
        )
        return fee


def mark_claims_returned(*, loan: Loan, actor=None, source="staff") -> Loan:
    """Patron claims a return we can't find: close the loan for investigation, no fine.

    The copy is moved to LOST (missing) so it leaves circulation and can be
    recovered via the normal found-item path — never left stuck in LOANED with
    no active loan.
    """
    with transaction.atomic():
        loan = _active_loan_locked(loan.pk)
        loan.status = LoanStatus.CLAIMS_RETURNED
        loan.save(update_fields=["status", "updated_at"])
        copy = Copy.objects.select_for_update().get(pk=loan.copy_id)
        if copy.status == CopyStatus.LOANED:
            copy.status = CopyStatus.LOST
            copy.save(update_fields=["status", "updated_at"])
        audit_action(action="loan.claims_returned", entity=loan, actor=actor, source=source)
        emit_domain_event(
            event_type="loan.claims_returned", aggregate=loan, payload={}, actor=actor, source=source
        )
        return loan


def return_damaged(*, loan: Loan, actor=None, fee_cents=None, source="staff") -> Fee:
    """Return an item that came back damaged: close the loan and bill a damage fee."""
    with transaction.atomic():
        loan = _active_loan_locked(loan.pk)
        patron = loan.patron
        if patron is None:
            raise DomainError("Loan has no patron to bill.")
        assess_overdue_fine(loan=loan)
        policy = fee_policy_for(loan.organization)
        amount = policy.lost_item_fee_cents // 2 if fee_cents is None else max(0, int(fee_cents))
        fee = Fee.objects.create(
            organization=loan.organization,
            patron_id=patron.pk,
            loan=loan,
            fee_type=FeeType.DAMAGED,
            amount_cents=amount,
            description="Damaged item fee",
        )
        loan.status = LoanStatus.RETURNED
        loan.returned_at = timezone.now()
        loan.patron_hash = stable_patron_hash(patron)
        if not patron.retain_loan_history:
            loan.patron = None
        loan.save(update_fields=["status", "returned_at", "patron_hash", "patron", "updated_at"])
        copy = Copy.objects.select_for_update().get(pk=loan.copy_id)
        copy.status = CopyStatus.REPAIR
        copy.condition = CopyCondition.DAMAGED
        copy.save(update_fields=["status", "condition", "updated_at"])
        audit_action(action="loan.return_damaged", entity=loan, actor=actor, source=source)
        emit_domain_event(
            event_type="loan.return_damaged", aggregate=loan,
            payload={"fee_cents": amount}, actor=actor, source=source,
        )
        return fee


# Re-exported for a stable import surface.
__all__ = [
    "bulk_update_copies",
    "close_inventory",
    "mark_claims_returned",
    "mark_loan_lost",
    "return_damaged",
    "scan_inventory",
    "start_inventory",
    "weed_copies",
]
