"""Tests for staff productivity workflows (Increment 14)."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from library import workflows
from library.models import (
    Branch,
    Copy,
    CopyCondition,
    CopyStatus,
    Edition,
    Fee,
    FeeType,
    InventoryStatus,
    Loan,
    LoanStatus,
    Organization,
    PatronProfile,
    Work,
)
from library.services import DomainError

pytestmark = pytest.mark.django_db(transaction=True)


def make_env():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title="Dune", slug="dune")
    edition = Edition.objects.create(work=work, isbn_13="9780000000001")
    user = get_user_model().objects.create_user(username="reader", email="r@x.test")
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number="C1", home_branch=branch
    )
    return org, branch, work, edition, patron


def add_copy(org, edition, branch, barcode, status=CopyStatus.AVAILABLE):
    return Copy.objects.create(
        organization=org, edition=edition, branch=branch, barcode=barcode, status=status
    )


def active_loan(org, copy, patron):
    copy.status = CopyStatus.LOANED
    copy.save(update_fields=["status"])
    return Loan.objects.create(
        organization=org, copy=copy, patron=patron,
        due_at=timezone.now() + timedelta(days=7), status=LoanStatus.ACTIVE,
    )


# --------------------------------------------------------------------------- #
# Bulk operations
# --------------------------------------------------------------------------- #
def test_bulk_update_skips_in_flight_copies():
    org, branch, work, edition, patron = make_env()
    a = add_copy(org, edition, branch, "A")
    b = add_copy(org, edition, branch, "B")
    loaned = add_copy(org, edition, branch, "L", status=CopyStatus.LOANED)
    result = workflows.bulk_update_copies(
        organization=org, barcodes=["A", "B", "L", "ZZZ"], status=CopyStatus.REPAIR
    )
    assert result["updated"] == 2
    assert result["skipped"] == ["L"]
    assert result["not_found"] == ["ZZZ"]
    a.refresh_from_db()
    b.refresh_from_db()
    loaned.refresh_from_db()
    assert a.status == CopyStatus.REPAIR and b.status == CopyStatus.REPAIR
    assert loaned.status == CopyStatus.LOANED


def test_bulk_update_rejects_unsafe_status():
    org, branch, work, edition, patron = make_env()
    add_copy(org, edition, branch, "A")
    with pytest.raises(DomainError):
        workflows.bulk_update_copies(organization=org, barcodes=["A"], status=CopyStatus.LOANED)


def test_weed_retires_shelf_copies_only():
    org, branch, work, edition, patron = make_env()
    add_copy(org, edition, branch, "A")
    loaned = add_copy(org, edition, branch, "L", status=CopyStatus.LOANED)
    result = workflows.weed_copies(organization=org, barcodes=["A", "L"])
    assert result["retired"] == 1 and result["skipped"] == ["L"]
    assert Copy.objects.get(barcode="A").status == CopyStatus.RETIRED
    assert Copy.objects.get(pk=loaned.pk).status == CopyStatus.LOANED


# --------------------------------------------------------------------------- #
# Inventory / stocktake
# --------------------------------------------------------------------------- #
def test_inventory_reports_missing_and_unexpected():
    org, branch, work, edition, patron = make_env()
    add_copy(org, edition, branch, "SHELF-1")
    add_copy(org, edition, branch, "SHELF-2")
    session = workflows.start_inventory(organization=org, branch=branch)
    assert workflows.scan_inventory(session=session, barcode="SHELF-1") == "ok"
    assert workflows.scan_inventory(session=session, barcode="SHELF-1") == "duplicate"
    assert workflows.scan_inventory(session=session, barcode="STRANGER") == "unexpected"

    session = workflows.close_inventory(session=session)
    assert session.status == InventoryStatus.CLOSED
    assert session.missing_barcodes == ["SHELF-2"]  # never scanned
    assert session.unexpected_barcodes == ["STRANGER"]  # not owned here


def test_inventory_scan_after_close_rejected():
    org, branch, work, edition, patron = make_env()
    session = workflows.start_inventory(organization=org, branch=branch)
    workflows.close_inventory(session=session)
    with pytest.raises(DomainError):
        workflows.scan_inventory(session=session, barcode="X")


# --------------------------------------------------------------------------- #
# Lost / claims-returned / damaged
# --------------------------------------------------------------------------- #
def test_mark_lost_bills_replacement():
    org, branch, work, edition, patron = make_env()
    copy = add_copy(org, edition, branch, "A")
    loan = active_loan(org, copy, patron)
    fee = workflows.mark_loan_lost(loan=loan, actor=None)
    assert fee.fee_type == FeeType.LOST and fee.amount_cents > 0
    loan.refresh_from_db()
    copy.refresh_from_db()
    assert loan.status == LoanStatus.LOST and copy.status == CopyStatus.LOST


def test_claims_returned_no_fine():
    org, branch, work, edition, patron = make_env()
    copy = add_copy(org, edition, branch, "A")
    loan = active_loan(org, copy, patron)
    workflows.mark_claims_returned(loan=loan, actor=None)
    loan.refresh_from_db()
    assert loan.status == LoanStatus.CLAIMS_RETURNED
    assert not Fee.objects.filter(loan=loan).exists()


def test_return_damaged_bills_and_flags_copy():
    org, branch, work, edition, patron = make_env()
    copy = add_copy(org, edition, branch, "A")
    loan = active_loan(org, copy, patron)
    fee = workflows.return_damaged(loan=loan, actor=None, fee_cents=500)
    assert fee.fee_type == FeeType.DAMAGED and fee.amount_cents == 500
    loan.refresh_from_db()
    copy.refresh_from_db()
    assert loan.status == LoanStatus.RETURNED
    assert copy.status == CopyStatus.REPAIR and copy.condition == CopyCondition.DAMAGED
