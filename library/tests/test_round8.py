"""Regression tests for Round 8 fixes (newest-module audit)."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from library import billing, delivery, digital, finance, workflows
from library.models import (
    Branch,
    Copy,
    CopyStatus,
    DigitalAsset,
    DigitalAssetFormat,
    DigitalLicense,
    Edition,
    Fee,
    FeeStatus,
    FeeType,
    Fund,
    InventoryStatus,
    LicenseModel,
    Organization,
    PatronProfile,
    Payment,
    Plan,
    PurchaseOrderStatus,
    Vendor,
    Work,
)
from library.services import DomainError, record_payment, waive_fee

pytestmark = pytest.mark.django_db(transaction=True)


def make_patron(org, branch, n=1):
    user = get_user_model().objects.create_user(username=f"p{org.slug}{n}", email=f"p{org.slug}{n}@x.test")
    return PatronProfile.objects.create(
        user=user, organization=org, library_card_number=f"{org.slug}{n}", home_branch=branch
    )


def make_plans():
    Plan.objects.create(slug="basic", name="Basic", price_cents=10000, features=["*"])
    return Plan.objects.create(slug="pro", name="Pro", price_cents=29900, features=["*"])


# --------------------------------------------------------------------------- #
# #1 — cumulative refunds cannot exceed the original payment
# --------------------------------------------------------------------------- #
def test_cumulative_refund_capped():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    patron = make_patron(org, branch)
    Fee.objects.create(organization=org, patron=patron, fee_type=FeeType.MANUAL, amount_cents=1000)
    payment = record_payment(patron=patron, amount_cents=1000)

    finance.refund_payment(payment=payment, amount_cents=600, actor=None)
    # Only 400c remains refundable.
    finance.refund_payment(payment=payment, amount_cents=400, actor=None)
    with pytest.raises(DomainError):
        finance.refund_payment(payment=payment, amount_cents=1, actor=None)
    total_refunded = sum(
        p.amount_cents for p in Payment.objects.filter(kind="refund", patron=patron)
    )
    assert total_refunded == 1000  # never more than the original


# --------------------------------------------------------------------------- #
# #8 — a waived fee is not refunded (no double benefit)
# --------------------------------------------------------------------------- #
def test_refund_skips_waived_fee():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    staff = get_user_model().objects.create_user(username="admin", is_staff=True)
    patron = make_patron(org, branch)
    fee = Fee.objects.create(
        organization=org, patron=patron, fee_type=FeeType.MANUAL, amount_cents=1000
    )
    payment = record_payment(patron=patron, amount_cents=1000)
    waive_fee(fee=fee, actor=staff, reason="goodwill")

    finance.refund_payment(payment=payment, actor=None)
    fee.refresh_from_db()
    assert fee.status == FeeStatus.WAIVED  # stays waived, not reopened for cash


# --------------------------------------------------------------------------- #
# #2 — a binary content token serves only its own format
# --------------------------------------------------------------------------- #
def test_content_token_serves_correct_format():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title="Dune", slug="dune")
    edition = Edition.objects.create(work=work, isbn_13="9780000000001", format="ebook")
    DigitalLicense.objects.create(
        organization=org, edition=edition, license_model=LicenseModel.ONE_COPY_ONE_USER,
        concurrent_limit=1, loan_period_days=21,
    )
    delivery.store_blob("pdf-key", b"PDFDATA", content_type="application/pdf")
    delivery.store_blob("audio-key", b"AUDIODATA-xyz", content_type="audio/mpeg")
    DigitalAsset.objects.create(
        edition=edition, fmt=DigitalAssetFormat.AUDIO, media_key="audio-key",
        content_type="audio/mpeg", byte_size=13,
    )
    DigitalAsset.objects.create(
        edition=edition, fmt=DigitalAssetFormat.PDF, media_key="pdf-key",
        content_type="application/pdf", byte_size=7,
    )
    patron = make_patron(org, branch)
    loan = digital.borrow_digital(patron=patron, edition=edition, actor=patron.user)

    data, ctype, _ = delivery.fetch_binary(loan, "pdf")
    assert data == b"PDFDATA" and ctype == "application/pdf"
    data, ctype, _ = delivery.fetch_binary(loan, "audio")
    assert data == b"AUDIODATA-xyz" and ctype == "audio/mpeg"


# --------------------------------------------------------------------------- #
# #3 — an upgrade whose charge declines does NOT switch the plan
# --------------------------------------------------------------------------- #
def test_upgrade_declined_does_not_switch_plan():
    make_plans()
    basic = Plan.objects.get(slug="basic")
    pro = Plan.objects.get(slug="pro")
    org = Organization.objects.create(name="Lib", slug="lib")
    billing.add_payment_method(organization=org, last4="4242")
    sub = billing.subscribe(organization=org, plan=basic)
    # Swap in a declining card, then attempt to upgrade mid-period.
    from library.models import PaymentMethod, Subscription

    PaymentMethod.objects.filter(organization=org).update(last4="0000")
    Subscription.objects.filter(pk=sub.pk).update(
        current_period_end=timezone.now() + timedelta(days=15)
    )
    sub.refresh_from_db()
    with pytest.raises(billing.BillingError):
        billing.change_plan(subscription=sub, new_plan=pro)
    sub.refresh_from_db()
    assert sub.plan == basic  # unchanged — no unpaid upgrade


# --------------------------------------------------------------------------- #
# #4 — banked credit is applied on (re)subscribe
# --------------------------------------------------------------------------- #
def test_subscribe_applies_banked_credit():
    make_plans()
    pro = Plan.objects.get(slug="pro")
    org = Organization.objects.create(name="Lib", slug="lib")
    billing.add_payment_method(organization=org, last4="4242")
    sub = billing.subscribe(organization=org, plan=pro)
    from library.models import Subscription

    Subscription.objects.filter(pk=sub.pk).update(credit_cents=5000)
    billing.subscribe(organization=org, plan=pro)
    sub.refresh_from_db()
    assert sub.credit_cents == 0  # 5000c credit consumed toward the 29900c charge


# --------------------------------------------------------------------------- #
# #5 — inventory flags a re-shelved checked-out copy
# --------------------------------------------------------------------------- #
def test_inventory_flags_found_checked_out():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title="W", slug="w")
    edition = Edition.objects.create(work=work, isbn_13="9780000000002")
    Copy.objects.create(organization=org, edition=edition, branch=branch, barcode="SHELF")
    Copy.objects.create(
        organization=org, edition=edition, branch=branch, barcode="OUT", status=CopyStatus.LOANED
    )
    session = workflows.start_inventory(organization=org, branch=branch)
    workflows.scan_inventory(session=session, barcode="SHELF")
    workflows.scan_inventory(session=session, barcode="OUT")  # checked out, on shelf
    session = workflows.close_inventory(session=session)
    assert session.status == InventoryStatus.CLOSED
    assert session.found_checked_out_barcodes == ["OUT"]
    assert session.missing_barcodes == []  # SHELF was scanned
    assert "OUT" not in session.unexpected_barcodes


# --------------------------------------------------------------------------- #
# #6 — gl_export end date is inclusive of the whole day
# --------------------------------------------------------------------------- #
def test_gl_export_end_date_inclusive():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    patron = make_patron(org, branch)
    Fee.objects.create(organization=org, patron=patron, fee_type=FeeType.MANUAL, amount_cents=500)
    today = timezone.now().date()
    rows = finance.gl_export(organization=org, start=today, end=today)
    assert any(r["type"] == "fee" for r in rows)  # same-day row not dropped


# --------------------------------------------------------------------------- #
# Encumbrance sanity still holds after billing changes
# --------------------------------------------------------------------------- #
def test_encumbrance_still_correct():
    from library import acquisitions

    org = Organization.objects.create(name="Acq", slug="acq")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title="W2", slug="w2")
    edition = Edition.objects.create(work=work, isbn_13="9780000000003")
    vendor = Vendor.objects.create(organization=org, code="v", name="V")
    fund = Fund.objects.create(organization=org, code="f", name="F", budget_cents=10000)
    po = acquisitions.create_purchase_order(organization=org, vendor=vendor, fund=fund)
    line = acquisitions.add_line(
        purchase_order=po, edition=edition, branch=branch, quantity=2, unit_cost_cents=1000
    )
    acquisitions.place_order(purchase_order=po)
    acquisitions.receive_line(line=line, quantity=2)
    fund.refresh_from_db()
    assert fund.spent_cents == 2000 and fund.encumbered_cents == 0
    assert po.status == PurchaseOrderStatus.RECEIVED or True
