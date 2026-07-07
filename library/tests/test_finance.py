"""Tests for financial depth (Increment 15): refunds, plans, amnesty, GL, encumbrance."""

import pytest
from django.contrib.auth import get_user_model

from library import acquisitions, finance
from library.models import (
    Branch,
    Edition,
    Fee,
    FeeStatus,
    FeeType,
    Fund,
    Organization,
    PatronProfile,
    PaymentPlanStatus,
    PurchaseOrderStatus,
    Vendor,
    Work,
)
from library.services import DomainError, record_payment

pytestmark = pytest.mark.django_db(transaction=True)


def make_patron():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    user = get_user_model().objects.create_user(username="p", email="p@x.test")
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number="C1", home_branch=branch
    )
    return org, branch, patron


def make_fee(org, patron, amount=1000, fee_type=FeeType.MANUAL):
    return Fee.objects.create(
        organization=org, patron=patron, fee_type=fee_type, amount_cents=amount,
        description="test fee",
    )


# --------------------------------------------------------------------------- #
# Refunds
# --------------------------------------------------------------------------- #
def test_refund_reopens_paid_fee():
    org, branch, patron = make_patron()
    fee = make_fee(org, patron, 1000)
    payment = record_payment(patron=patron, amount_cents=1000)
    fee.refresh_from_db()
    assert fee.status == FeeStatus.PAID

    refund = finance.refund_payment(payment=payment, actor=None, reason="goodwill")
    assert refund.kind == "refund" and refund.amount_cents == 1000
    fee.refresh_from_db()
    assert fee.status == FeeStatus.OUTSTANDING and fee.paid_cents == 0


def test_refund_rejects_over_amount():
    org, branch, patron = make_patron()
    make_fee(org, patron, 1000)
    payment = record_payment(patron=patron, amount_cents=1000)
    with pytest.raises(DomainError):
        finance.refund_payment(payment=payment, amount_cents=5000)


# --------------------------------------------------------------------------- #
# Payment plans
# --------------------------------------------------------------------------- #
def test_payment_plan_installments_complete():
    org, branch, patron = make_patron()
    make_fee(org, patron, 1000)
    plan = finance.create_payment_plan(patron=patron, total_cents=1000, installments=2)
    assert plan.installment_cents == 500

    finance.pay_installment(plan=plan, actor=None)
    plan.refresh_from_db()
    assert plan.paid_cents == 500 and plan.status == PaymentPlanStatus.ACTIVE

    finance.pay_installment(plan=plan, actor=None)
    plan.refresh_from_db()
    assert plan.paid_cents == 1000 and plan.status == PaymentPlanStatus.COMPLETED
    # Overpayment is impossible once complete.
    with pytest.raises(DomainError):
        finance.pay_installment(plan=plan, actor=None)


# --------------------------------------------------------------------------- #
# Amnesty + GL export
# --------------------------------------------------------------------------- #
def test_amnesty_waives_overdue_only():
    org, branch, patron = make_patron()
    make_fee(org, patron, 500, fee_type=FeeType.OVERDUE)
    make_fee(org, patron, 900, fee_type=FeeType.LOST)
    staff = get_user_model().objects.create_user(username="admin", is_staff=True)
    waived = finance.run_amnesty(organization=org, actor=staff)
    assert waived == 1
    assert Fee.objects.filter(fee_type=FeeType.OVERDUE, status=FeeStatus.WAIVED).exists()
    assert Fee.objects.filter(fee_type=FeeType.LOST, status=FeeStatus.OUTSTANDING).exists()


def test_gl_export_includes_fees_and_payments():
    org, branch, patron = make_patron()
    make_fee(org, patron, 1000)
    record_payment(patron=patron, amount_cents=400)
    rows = finance.gl_export(organization=org)
    types = {r["type"] for r in rows}
    assert "fee" in types and "payment" in types


# --------------------------------------------------------------------------- #
# Fund encumbrance
# --------------------------------------------------------------------------- #
def _po_env():
    org = Organization.objects.create(name="Acq", slug="acq")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title="W", slug="w")
    edition = Edition.objects.create(work=work, isbn_13="9780000000002")
    vendor = Vendor.objects.create(organization=org, code="v", name="V")
    fund = Fund.objects.create(organization=org, code="f", name="F", budget_cents=10000)
    return org, branch, edition, vendor, fund


def test_encumbrance_commits_then_converts_to_spend():
    org, branch, edition, vendor, fund = _po_env()
    po = acquisitions.create_purchase_order(organization=org, vendor=vendor, fund=fund)
    line = acquisitions.add_line(
        purchase_order=po, edition=edition, branch=branch, quantity=2, unit_cost_cents=1000
    )
    acquisitions.place_order(purchase_order=po)
    fund.refresh_from_db()
    assert fund.encumbered_cents == 2000 and fund.available_cents == 8000

    acquisitions.receive_line(line=line, quantity=1)
    fund.refresh_from_db()
    assert fund.spent_cents == 1000 and fund.encumbered_cents == 1000

    acquisitions.receive_line(line=line, quantity=1)
    fund.refresh_from_db()
    assert fund.spent_cents == 2000 and fund.encumbered_cents == 0


def test_cancel_releases_encumbrance():
    org, branch, edition, vendor, fund = _po_env()
    po = acquisitions.create_purchase_order(organization=org, vendor=vendor, fund=fund)
    acquisitions.add_line(
        purchase_order=po, edition=edition, branch=branch, quantity=2, unit_cost_cents=1000
    )
    acquisitions.place_order(purchase_order=po)
    po = acquisitions.cancel_order(purchase_order=po)
    fund.refresh_from_db()
    assert fund.encumbered_cents == 0
    assert po.status == PurchaseOrderStatus.CANCELLED
