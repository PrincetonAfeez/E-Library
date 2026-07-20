"""Tests for digital/e-content lending (Increment 6)."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from library import digital
from library.models import (
    Branch,
    DigitalHoldStatus,
    DigitalLicense,
    DigitalLoan,
    DigitalLoanStatus,
    Edition,
    LicenseModel,
    Organization,
    PatronProfile,
    Work,
)
from library.services import DomainError

pytestmark = pytest.mark.django_db(transaction=True)


def make_digital(concurrent=1, model=LicenseModel.ONE_COPY_ONE_USER, **kw):
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title="Neuromancer", slug="neuromancer")
    edition = Edition.objects.create(work=work, isbn_13="9780000000001", format="ebook")
    lic = DigitalLicense.objects.create(
        organization=org,
        edition=edition,
        license_model=model,
        concurrent_limit=concurrent,
        content_url="https://cdn/neuromancer.epub",
        loan_period_days=21,
        **kw,
    )
    return org, branch, work, edition, lic


def make_patron(org, branch, n=1):
    user = get_user_model().objects.create_user(username=f"reader{n}")
    return PatronProfile.objects.create(
        user=user, organization=org, library_card_number=f"C{n}", home_branch=branch
    )


def _api(user):
    client = APIClient(enforce_csrf_checks=False)
    client.force_authenticate(user=user)
    client.defaults["secure"] = True
    return client


# --------------------------------------------------------------------------- #
# Availability + borrow/return
# --------------------------------------------------------------------------- #
def test_borrow_and_return_frees_slot():
    org, branch, work, edition, lic = make_digital(concurrent=1)
    p1 = make_patron(org, branch, 1)
    p2 = make_patron(org, branch, 2)

    loan = digital.borrow_digital(patron=p1, edition=edition, actor=p1.user)
    assert loan.status == DigitalLoanStatus.ACTIVE
    assert loan.expires_at > timezone.now()
    assert digital.available_slots_for_edition(edition, org) == 0

    # Second patron can't borrow the only OCOU license.
    with pytest.raises(DomainError):
        digital.borrow_digital(patron=p2, edition=edition, actor=p2.user)

    digital.return_digital(loan=loan, actor=p1.user)
    assert digital.available_slots_for_edition(edition, org) == 1
    # Now p2 can borrow.
    digital.borrow_digital(patron=p2, edition=edition, actor=p2.user)


def test_simultaneous_use_unlimited():
    org, branch, work, edition, lic = make_digital(
        concurrent=None, model=LicenseModel.SIMULTANEOUS
    )
    for i in range(5):
        p = make_patron(org, branch, i)
        digital.borrow_digital(patron=p, edition=edition, actor=p.user)
    assert DigitalLoan.objects.filter(status=DigitalLoanStatus.ACTIVE).count() == 5


def test_metered_checkouts_deplete():
    org, branch, work, edition, lic = make_digital(
        concurrent=1, model=LicenseModel.METERED_CHECKOUTS, checkouts_allowed=2
    )
    for i in range(2):
        p = make_patron(org, branch, i)
        loan = digital.borrow_digital(patron=p, edition=edition, actor=p.user)
        digital.return_digital(loan=loan, actor=p.user)
    lic.refresh_from_db()
    assert lic.checkouts_used == 2
    # Depleted — no more checkouts even though concurrent slot is free.
    assert digital.available_slots_for_edition(edition, org) == 0
    p3 = make_patron(org, branch, 3)
    with pytest.raises(DomainError):
        digital.borrow_digital(patron=p3, edition=edition, actor=p3.user)


def test_expiry_sweep_closes_loan_and_promotes_hold():
    org, branch, work, edition, lic = make_digital(concurrent=1)
    p1 = make_patron(org, branch, 1)
    p2 = make_patron(org, branch, 2)
    loan = digital.borrow_digital(patron=p1, edition=edition, actor=p1.user)
    hold = digital.place_digital_hold(patron=p2, edition=edition, actor=p2.user)
    assert hold.status == DigitalHoldStatus.WAITING  # p1 has the license

    # Force the loan to expire.
    DigitalLoan.objects.filter(pk=loan.pk).update(expires_at=timezone.now() - timedelta(hours=1))
    assert digital.expire_digital_loans() == 1
    loan.refresh_from_db()
    hold.refresh_from_db()
    assert loan.status == DigitalLoanStatus.EXPIRED
    assert hold.status == DigitalHoldStatus.READY  # promoted


def test_ready_hold_lets_holder_borrow_over_queue():
    org, branch, work, edition, lic = make_digital(concurrent=1)
    p1 = make_patron(org, branch, 1)
    p2 = make_patron(org, branch, 2)
    p3 = make_patron(org, branch, 3)
    loan = digital.borrow_digital(patron=p1, edition=edition, actor=p1.user)
    h2 = digital.place_digital_hold(patron=p2, edition=edition, actor=p2.user)
    digital.place_digital_hold(patron=p3, edition=edition, actor=p3.user)
    digital.return_digital(loan=loan, actor=p1.user)
    h2.refresh_from_db()
    assert h2.status == DigitalHoldStatus.READY
    # p2 (ready) can borrow despite p3 waiting; p3 cannot jump the queue.
    digital.borrow_digital(patron=p2, edition=edition, actor=p2.user)
    with pytest.raises(DomainError):
        digital.borrow_digital(patron=p3, edition=edition, actor=p3.user)


def test_access_token_gates_content():
    org, branch, work, edition, lic = make_digital(concurrent=1)
    p1 = make_patron(org, branch, 1)
    loan = digital.borrow_digital(patron=p1, edition=edition, actor=p1.user)
    info = digital.access_content(access_token=loan.access_token)
    assert info.get("content_token") or info["format"] == "external"
    # An expired loan denies access.
    DigitalLoan.objects.filter(pk=loan.pk).update(status=DigitalLoanStatus.EXPIRED)
    with pytest.raises(DomainError):
        digital.access_content(access_token=loan.access_token)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def test_digital_api_flow():
    org, branch, work, edition, lic = make_digital(concurrent=1)
    p1 = make_patron(org, branch, 1)
    client = _api(p1.user)

    resp = client.post(f"/api/v1/digital/editions/{edition.pk}/borrow/", {}, format="json", secure=True)
    assert resp.status_code == 201
    loan_id = resp.json()["data"]["id"]

    resp = client.get(f"/api/v1/digital/loans/{loan_id}/access/", secure=True)
    assert resp.status_code == 200
    assert (
        "content_token" in resp.json()["data"]
        or resp.json()["data"]["format"] == "external"
    )

    resp = client.get("/api/v1/digital/", secure=True)
    assert len(resp.json()["loans"]) == 1

    resp = client.post(f"/api/v1/digital/loans/{loan_id}/return/", {}, format="json", secure=True)
    assert resp.status_code == 204


def test_digital_feature_gated_by_plan():
    from library.models import Plan, Subscription, SubscriptionStatus

    org, branch, work, edition, lic = make_digital(concurrent=1)
    # A plan WITHOUT the "digital" feature blocks borrowing.
    plan = Plan.objects.create(slug="basic", name="Basic", features=["catalog", "circulation"])
    Subscription.objects.create(organization=org, plan=plan, status=SubscriptionStatus.ACTIVE)
    p1 = make_patron(org, branch, 1)
    resp = _api(p1.user).post(
        f"/api/v1/digital/editions/{edition.pk}/borrow/", {}, format="json", secure=True
    )
    assert resp.status_code == 403
