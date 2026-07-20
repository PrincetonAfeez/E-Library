"""Tests for the commercial layer: entitlements + fines/fees/payments."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from library import entitlements
from library.models import (
    Branch,
    Copy,
    Edition,
    Fee,
    FeeStatus,
    FeeType,
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
from library.services import (
    DomainError,
    assess_overdue_fine,
    assess_overdue_fines,
    borrow_work,
    fee_policy_for,
    flag_overdue_loans,
    patron_balance_cents,
    record_payment,
    return_loan,
    waive_fee,
)

pytestmark = pytest.mark.django_db(transaction=True)


def make_catalog(loan_days=21):
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main", loan_days=loan_days)
    work = Work.objects.create(canonical_title="Dune", slug="dune")
    edition = Edition.objects.create(work=work, isbn_13="9780000000001")
    copy = Copy.objects.create(organization=org, edition=edition, branch=branch, barcode="C1")
    user = get_user_model().objects.create_user(username="reader", email="r@example.test")
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number="C1", home_branch=branch
    )
    return org, branch, work, copy, patron


def _api(user):
    client = APIClient(enforce_csrf_checks=False)
    client.force_authenticate(user=user)
    client.defaults["secure"] = True
    return client


# --------------------------------------------------------------------------- #
# Entitlements
# --------------------------------------------------------------------------- #
def test_no_subscription_is_unlimited():
    org = Organization.objects.create(name="Lib", slug="lib")
    assert entitlements.remaining(org, "patrons") is None
    assert entitlements.has_feature(org, "anything") is True
    entitlements.assert_within_limit(org, "patrons")  # no raise


def test_patron_limit_enforced():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    plan = Plan.objects.create(slug="tiny", name="Tiny", max_patrons=1)
    Subscription.objects.create(organization=org, plan=plan, status=SubscriptionStatus.ACTIVE)
    # First patron OK.
    u1 = get_user_model().objects.create_user(username="a")
    from library.services import register_patron

    register_patron(user=u1, organization=org, home_branch=branch)
    # Second exceeds the plan.
    u2 = get_user_model().objects.create_user(username="b")
    with pytest.raises(DomainError):
        register_patron(user=u2, organization=org, home_branch=branch)


def test_feature_flag_gate():
    org = Organization.objects.create(name="Lib", slug="lib")
    plan = Plan.objects.create(slug="basic", name="Basic", features=["catalog"])
    Subscription.objects.create(organization=org, plan=plan, status=SubscriptionStatus.ACTIVE)
    assert entitlements.has_feature(org, "catalog") is True
    assert entitlements.has_feature(org, "imports") is False


def test_canceled_subscription_is_restricted():
    # Non-serviceable subscriptions must not fall through to "unlimited".
    org = Organization.objects.create(name="Lib", slug="lib")
    plan = Plan.objects.create(slug="tiny", name="Tiny", max_patrons=10, features=["catalog"])
    Subscription.objects.create(organization=org, plan=plan, status=SubscriptionStatus.CANCELED)
    assert entitlements.remaining(org, "patrons") == 0
    assert entitlements.has_feature(org, "catalog") is False


# --------------------------------------------------------------------------- #
# Fines
# --------------------------------------------------------------------------- #
def test_overdue_fine_assessed_on_late_return():
    org, branch, work, copy, patron = make_catalog()
    loan = borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    # Force the loan 5 days overdue.
    loan.due_at = timezone.now() - timedelta(days=5)
    loan.status = LoanStatus.OVERDUE
    loan.save(update_fields=["due_at", "status"])

    return_loan(loan=loan, actor=patron.user)
    fee = Fee.objects.get(loan=loan, fee_type=FeeType.OVERDUE)
    # 5 days * default 25c/day = 125c (under the 2000c cap).
    assert fee.amount_cents == 125
    assert patron_balance_cents(patron) == 125


def test_overdue_fine_capped():
    org, branch, work, copy, patron = make_catalog()
    policy = fee_policy_for(org)
    loan = borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    loan.due_at = timezone.now() - timedelta(days=1000)
    loan.status = LoanStatus.OVERDUE
    loan.save(update_fields=["due_at", "status"])
    assess_overdue_fine(loan=loan)
    fee = Fee.objects.get(loan=loan, fee_type=FeeType.OVERDUE)
    assert fee.amount_cents == policy.max_overdue_cents  # capped


def test_grace_days_respected():
    org, branch, work, copy, patron = make_catalog()
    policy = fee_policy_for(org)
    policy.grace_days = 3
    policy.save(update_fields=["grace_days"])
    loan = borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    loan.due_at = timezone.now() - timedelta(days=2)  # within grace
    loan.status = LoanStatus.OVERDUE
    loan.save(update_fields=["due_at", "status"])
    assert assess_overdue_fine(loan=loan) is None
    assert not Fee.objects.filter(loan=loan).exists()


def test_fine_blocks_borrowing_and_override_bypasses():
    org, branch, work, copy, patron = make_catalog()
    # Give the patron a big outstanding fee (over the 1000c block threshold).
    Fee.objects.create(
        organization=org, patron=patron, fee_type=FeeType.MANUAL, amount_cents=1500,
        description="Big fine",
    )
    with pytest.raises(DomainError):
        borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    # A staff override bypasses the fine block.
    staff = get_user_model().objects.create_user(username="liv", is_staff=True)
    loan = borrow_work(
        patron=patron, work=work, branch=branch, actor=staff, override_reason="goodwill"
    )
    assert loan.status == LoanStatus.ACTIVE


def test_payment_allocates_oldest_first():
    org, branch, work, copy, patron = make_catalog()
    f1 = Fee.objects.create(
        organization=org, patron=patron, fee_type=FeeType.MANUAL, amount_cents=100
    )
    f2 = Fee.objects.create(
        organization=org, patron=patron, fee_type=FeeType.MANUAL, amount_cents=200
    )
    record_payment(patron=patron, amount_cents=150)
    f1.refresh_from_db()
    f2.refresh_from_db()
    assert f1.status == FeeStatus.PAID and f1.paid_cents == 100
    assert f2.status == FeeStatus.OUTSTANDING and f2.paid_cents == 50
    assert patron_balance_cents(patron) == 150


def test_waive_fee_zeroes_balance():
    org, branch, work, copy, patron = make_catalog()
    fee = Fee.objects.create(
        organization=org, patron=patron, fee_type=FeeType.MANUAL, amount_cents=500
    )
    staff = get_user_model().objects.create_user(username="liv")
    waive_fee(fee=fee, actor=staff, reason="first offense")
    fee.refresh_from_db()
    assert fee.status == FeeStatus.WAIVED
    assert patron_balance_cents(patron) == 0


def test_assess_overdue_fines_sweep():
    org, branch, work, copy, patron = make_catalog()
    loan = borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    loan.due_at = timezone.now() - timedelta(days=2)
    loan.save(update_fields=["due_at"])
    flag_overdue_loans()
    assert assess_overdue_fines() == 1
    assert Fee.objects.filter(loan=loan, fee_type=FeeType.OVERDUE).exists()


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def test_patron_fees_and_payment_api():
    from library import billing

    org, branch, work, copy, patron = make_catalog()
    billing.add_payment_method(organization=org, last4="4242", purpose="fines")
    Fee.objects.create(
        organization=org, patron=patron, fee_type=FeeType.MANUAL, amount_cents=300
    )
    client = _api(patron.user)
    resp = client.get("/api/v1/account/fees/", secure=True)
    assert resp.status_code == 200
    assert resp.json()["balance_cents"] == 300

    resp = client.post(
        "/api/v1/account/fees/pay/", {"amount_cents": 300}, format="json", secure=True
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["balance_cents"] == 0


def test_staff_waive_fee_api():
    org, branch, work, copy, patron = make_catalog()
    fee = Fee.objects.create(
        organization=org, patron=patron, fee_type=FeeType.MANUAL, amount_cents=400
    )
    staff = get_user_model().objects.create_user(username="liv", is_staff=True)
    StaffMembership.objects.create(
        user=staff, organization=org, branch=branch, role=StaffRole.LIBRARIAN
    )
    resp = _api(staff).post(
        f"/api/v1/librarian/fees/{fee.pk}/waive/",
        {"reason": "goodwill"},
        format="json",
        secure=True,
    )
    assert resp.status_code == 200
    fee.refresh_from_db()
    assert fee.status == FeeStatus.WAIVED
