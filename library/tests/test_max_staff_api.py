"""Staff REST API smoke coverage with APIClient force_authenticate(secure=True)."""

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from library import billing
from library.models import (
    Branch,
    Copy,
    CopyStatus,
    Edition,
    Fee,
    FeeType,
    Loan,
    LoanStatus,
    Organization,
    PatronProfile,
    Plan,
    StaffMembership,
    StaffRole,
    Subscription,
    SubscriptionStatus,
    Work,
)
from library.services import borrow_work, record_payment

pytestmark = pytest.mark.django_db(transaction=True)


def _unique_slug(prefix="maxapi"):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def staff_setup(*, with_plan=True):
    """Org, branch, admin StaffMembership, and optional seeded plan."""
    slug = _unique_slug()
    org = Organization.objects.create(name=f"Org {slug}", slug=slug)
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    admin = get_user_model().objects.create_user(username=f"admin-{slug}", is_staff=True)
    StaffMembership.objects.create(
        user=admin, organization=org, branch=None, role=StaffRole.ADMIN
    )
    plan = None
    if with_plan:
        plan, _ = Plan.objects.update_or_create(
            slug=f"plan-{slug}",
            defaults={
                "name": "Test Plan",
                "price_cents": 0,
                "max_branches": 5,
                "max_patrons": 1000,
                "max_copies": 5000,
                "features": ["*"],
            },
        )
    return org, branch, admin, plan


def staff_client(user):
    client = APIClient(enforce_csrf_checks=False)
    client.force_authenticate(user=user)
    client.defaults["secure"] = True
    return client


def _catalog(org, branch, *, barcode="BC-1", slug=None):
    slug = slug or _unique_slug("work")
    work = Work.objects.create(canonical_title=f"Title {slug}", slug=slug)
    edition = Edition.objects.create(work=work, isbn_13="9780000000001")
    copy = Copy.objects.create(
        organization=org, edition=edition, branch=branch, barcode=barcode
    )
    return work, edition, copy


def _patron(org, branch, n=1):
    slug = org.slug
    user = get_user_model().objects.create_user(
        username=f"patron-{slug}-{n}", email=f"p{n}@{slug}.test"
    )
    return PatronProfile.objects.create(
        user=user,
        organization=org,
        library_card_number=f"CARD-{slug}-{n}",
        home_branch=branch,
    )


# --------------------------------------------------------------------------- #
# Librarian exports (CSV)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("export_type", ["loans", "overdue", "holds", "inventory"])
def test_librarian_exports_return_csv(export_type):
    org, branch, admin, _ = staff_setup()
    _catalog(org, branch)
    resp = staff_client(admin).get(
        f"/api/v1/librarian/exports/?type={export_type}", secure=True
    )
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/csv")
    body = b"".join(resp.streaming_content).decode()
    assert body.strip()


# --------------------------------------------------------------------------- #
# Billing cancel after subscribe
# --------------------------------------------------------------------------- #
def test_cancel_subscription_after_change_plan():
    org, _branch, admin, plan = staff_setup()
    Subscription.objects.create(
        organization=org, plan=plan, status=SubscriptionStatus.TRIALING
    )
    client = staff_client(admin)
    resp = client.post(
        "/api/v1/billing/change-plan/", {"plan": plan.slug}, format="json", secure=True
    )
    assert resp.status_code == 200

    resp = client.post("/api/v1/billing/cancel/", secure=True)
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == SubscriptionStatus.CANCELED


def test_cancel_subscription_non_staff_forbidden():
    org, branch, admin, plan = staff_setup()
    Subscription.objects.create(
        organization=org, plan=plan, status=SubscriptionStatus.TRIALING
    )
    patron = _patron(org, branch)
    resp = staff_client(patron.user).post("/api/v1/billing/cancel/", secure=True)
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Librarian reports
# --------------------------------------------------------------------------- #
def test_librarian_reports_circulation():
    org, branch, admin, _ = staff_setup()
    _catalog(org, branch)
    resp = staff_client(admin).get(
        "/api/v1/librarian/reports/?type=circulation&days=7", secure=True
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["type"] == "circulation"
    assert "data" in payload


# --------------------------------------------------------------------------- #
# Inventory start / scan / close
# --------------------------------------------------------------------------- #
def test_inventory_api_flow():
    org, branch, admin, _ = staff_setup()
    _catalog(org, branch, barcode="INV-1")
    client = staff_client(admin)

    start = client.post(
        "/api/v1/librarian/inventory/", {"branch": "main"}, format="json", secure=True
    )
    assert start.status_code == 201
    session_id = start.json()["data"]["id"]

    scan = client.post(
        f"/api/v1/librarian/inventory/{session_id}/scan/",
        {"barcode": "INV-1"},
        format="json",
        secure=True,
    )
    assert scan.status_code == 200
    assert scan.json()["data"]["result"] == "ok"

    close = client.post(f"/api/v1/librarian/inventory/{session_id}/close/", secure=True)
    assert close.status_code == 200
    assert "missing" in close.json()["data"]


# --------------------------------------------------------------------------- #
# Finance: refund + payment plan
# --------------------------------------------------------------------------- #
def test_refund_api_smoke():
    org, branch, admin, _ = staff_setup(with_plan=False)
    patron = _patron(org, branch)
    Fee.objects.create(
        organization=org,
        patron=patron,
        fee_type=FeeType.MANUAL,
        amount_cents=1000,
        description="desk fine",
    )
    payment = record_payment(patron=patron, amount_cents=1000)

    resp = staff_client(admin).post(
        "/api/v1/librarian/finance/refund/",
        {"payment_id": payment.pk, "reason": "goodwill"},
        format="json",
        secure=True,
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["amount_cents"] == 1000


def test_payment_plan_api_smoke():
    org, branch, admin, _ = staff_setup(with_plan=False)
    patron = _patron(org, branch)
    Fee.objects.create(
        organization=org,
        patron=patron,
        fee_type=FeeType.MANUAL,
        amount_cents=1000,
        description="plan fee",
    )
    billing.add_payment_method(organization=org, purpose="fines")

    resp = staff_client(admin).post(
        "/api/v1/librarian/finance/plans/",
        {"patron_id": patron.pk, "total_cents": 1000, "installments": 2},
        format="json",
        secure=True,
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["installment_cents"] == 500


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("report", ["circulation", "turnover", "purchase-suggestions"])
def test_librarian_analytics_reports(report):
    org, branch, admin, _ = staff_setup()
    _catalog(org, branch)
    resp = staff_client(admin).get(
        f"/api/v1/librarian/analytics/{report}/", secure=True
    )
    assert resp.status_code == 200
    assert "data" in resp.json()


# --------------------------------------------------------------------------- #
# Amnesty + GL export
# --------------------------------------------------------------------------- #
def test_amnesty_api_waives_overdue_fees():
    org, branch, admin, _ = staff_setup(with_plan=False)
    patron = _patron(org, branch)
    Fee.objects.create(
        organization=org,
        patron=patron,
        fee_type=FeeType.OVERDUE,
        amount_cents=500,
        description="late",
    )
    Fee.objects.create(
        organization=org,
        patron=patron,
        fee_type=FeeType.LOST,
        amount_cents=900,
        description="lost",
    )
    resp = staff_client(admin).post("/api/v1/librarian/finance/amnesty/", secure=True)
    assert resp.status_code == 200
    assert resp.json()["data"]["waived"] == 1


def test_gl_export_api_returns_rows():
    org, branch, admin, _ = staff_setup(with_plan=False)
    patron = _patron(org, branch)
    Fee.objects.create(
        organization=org,
        patron=patron,
        fee_type=FeeType.MANUAL,
        amount_cents=1000,
        description="gl fee",
    )
    record_payment(patron=patron, amount_cents=400)

    resp = staff_client(admin).get("/api/v1/librarian/finance/gl-export/", secure=True)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["count"] >= 1
    types = {row["type"] for row in payload["data"]}
    assert "fee" in types


# --------------------------------------------------------------------------- #
# Bulk copy + weed
# --------------------------------------------------------------------------- #
def test_bulk_copy_api_smoke():
    org, branch, admin, _ = staff_setup()
    _catalog(org, branch, barcode="BULK-1")
    resp = staff_client(admin).post(
        "/api/v1/librarian/copies/bulk/",
        {"barcodes": ["BULK-1"], "status": CopyStatus.REPAIR},
        format="json",
        secure=True,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["updated"] == 1


def test_weed_copies_api_smoke():
    org, branch, admin, _ = staff_setup()
    _catalog(org, branch, barcode="WEED-1")
    resp = staff_client(admin).post(
        "/api/v1/librarian/copies/weed/",
        {"barcodes": ["WEED-1"], "reason": "weeded"},
        format="json",
        secure=True,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["retired"] == 1


# --------------------------------------------------------------------------- #
# Loan exception (mark lost)
# --------------------------------------------------------------------------- #
def test_loan_exception_mark_lost():
    org, branch, admin, _ = staff_setup()
    patron = _patron(org, branch)
    _work, _edition, copy = _catalog(org, branch, barcode="LOST-1")
    copy.status = CopyStatus.LOANED
    copy.save(update_fields=["status"])
    loan = Loan.objects.create(
        organization=org,
        copy=copy,
        patron=patron,
        due_at=timezone.now() + timedelta(days=7),
        status=LoanStatus.ACTIVE,
    )

    resp = staff_client(admin).post(
        f"/api/v1/librarian/loans/{loan.pk}/lost/exception/", secure=True
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "lost"
    assert resp.json()["data"]["fee_cents"] > 0


# --------------------------------------------------------------------------- #
# Work recommendations + librarian dashboard
# --------------------------------------------------------------------------- #
def test_work_recommendations_api():
    org, branch, admin, _ = staff_setup()
    work, _edition, _copy = _catalog(org, branch)
    resp = staff_client(admin).get(
        f"/api/v1/catalog/works/{work.slug}/recommendations/", secure=True
    )
    assert resp.status_code == 200
    assert "data" in resp.json()


def test_librarian_dashboard_api():
    org, branch, admin, _ = staff_setup()
    patron = _patron(org, branch)
    work, _edition, copy = _catalog(org, branch, barcode="DASH-1")
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)

    resp = staff_client(admin).get("/api/v1/librarian/dashboard/", secure=True)
    assert resp.status_code == 200
    payload = resp.json()
    assert "overdue_loans" in payload
    assert "ready_holds" in payload
    assert "waiting_holds" in payload
