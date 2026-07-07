"""Tests for consortia & resource sharing (Increment 12): union catalog, ILL, floating."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from library import consortia, services
from library.models import (
    Branch,
    Consortium,
    ConsortiumMembership,
    Copy,
    CopyStatus,
    Edition,
    IllStatus,
    Loan,
    Organization,
    PatronProfile,
    StaffMembership,
    StaffRole,
    Work,
)
from library.services import DomainError

pytestmark = pytest.mark.django_db(transaction=True)


def make_patron(org, branch, n=1):
    user = get_user_model().objects.create_user(username=f"patron{org.slug}{n}")
    return PatronProfile.objects.create(
        user=user, organization=org, library_card_number=f"{org.slug}-{n}", home_branch=branch
    )


def make_staff(org, n=1):
    user = get_user_model().objects.create_user(username=f"staff{org.slug}{n}", is_staff=True)
    StaffMembership.objects.create(user=user, organization=org, branch=None, role=StaffRole.ADMIN)
    return user


def build_consortium():
    cons = Consortium.objects.create(name="Metro Net", slug="metro-net")
    org_a = Organization.objects.create(name="Alpha", slug="a")
    org_b = Organization.objects.create(name="Beta", slug="b")
    branch_a = Branch.objects.create(organization=org_a, name="A Main", slug="a-main")
    branch_b = Branch.objects.create(organization=org_b, name="B Main", slug="b-main")
    ConsortiumMembership.objects.create(consortium=cons, organization=org_a)
    ConsortiumMembership.objects.create(consortium=cons, organization=org_b)
    work = Work.objects.create(canonical_title="Shared Title", slug="shared")
    edition = Edition.objects.create(work=work, isbn_13="9780000000009")
    copy_b = Copy.objects.create(organization=org_b, edition=edition, branch=branch_b, barcode="B-1")
    return {
        "cons": cons, "a": org_a, "b": org_b, "ba": branch_a, "bb": branch_b,
        "work": work, "edition": edition, "copy_b": copy_b,
    }


def _api(user):
    client = APIClient(enforce_csrf_checks=False)
    client.force_authenticate(user=user)
    client.defaults["secure"] = True
    return client


# --------------------------------------------------------------------------- #
# Union catalog
# --------------------------------------------------------------------------- #
def test_union_availability():
    env = build_consortium()
    rows = consortia.union_availability(env["cons"], env["work"])
    assert rows == [{"organization_id": env["b"].pk, "organization": "Beta", "available": 1}]


# --------------------------------------------------------------------------- #
# Requesting
# --------------------------------------------------------------------------- #
def test_request_reserves_lender_copy():
    env = build_consortium()
    patron = make_patron(env["a"], env["ba"])
    ill = consortia.request_ill(
        patron=patron, work=env["work"], consortium=env["cons"], actor=patron.user
    )
    assert ill.status == IllStatus.REQUESTED
    assert ill.lending_org_id == env["b"].pk
    env["copy_b"].refresh_from_db()
    assert env["copy_b"].status == CopyStatus.ILL  # reserved, out of local circulation


def test_request_refused_when_available_locally():
    env = build_consortium()
    Copy.objects.create(
        organization=env["a"], edition=env["edition"], branch=env["ba"], barcode="A-1"
    )
    patron = make_patron(env["a"], env["ba"])
    with pytest.raises(DomainError):
        consortia.request_ill(
            patron=patron, work=env["work"], consortium=env["cons"], actor=patron.user
        )


def test_request_unfilled_when_no_lender_has_it():
    env = build_consortium()
    env["copy_b"].status = CopyStatus.LOANED
    env["copy_b"].save(update_fields=["status"])
    patron = make_patron(env["a"], env["ba"])
    ill = consortia.request_ill(
        patron=patron, work=env["work"], consortium=env["cons"], actor=patron.user
    )
    assert ill.status == IllStatus.UNFILLED and ill.lending_copy_id is None


def test_duplicate_open_request_rejected():
    env = build_consortium()
    Copy.objects.create(
        organization=env["b"], edition=env["edition"], branch=env["bb"], barcode="B-2"
    )
    patron = make_patron(env["a"], env["ba"])
    consortia.request_ill(patron=patron, work=env["work"], consortium=env["cons"], actor=patron.user)
    with pytest.raises(DomainError):
        consortia.request_ill(
            patron=patron, work=env["work"], consortium=env["cons"], actor=patron.user
        )


# --------------------------------------------------------------------------- #
# Fulfilment lifecycle
# --------------------------------------------------------------------------- #
def test_full_ill_lifecycle_and_privacy_scrub():
    env = build_consortium()
    patron = make_patron(env["a"], env["ba"])
    ill = consortia.request_ill(
        patron=patron, work=env["work"], consortium=env["cons"], actor=patron.user
    )

    ill = consortia.ship_ill(ill=ill, actor=None)
    assert ill.status == IllStatus.SHIPPED and ill.shipped_at is not None

    ill = consortia.receive_ill(ill=ill, actor=None)
    assert ill.status == IllStatus.ON_LOAN and ill.due_at is not None

    ill = consortia.return_ill(ill=ill, actor=None)
    assert ill.status == IllStatus.RETURNING

    ill = consortia.checkin_ill(ill=ill, actor=None)
    assert ill.status == IllStatus.COMPLETED
    env["copy_b"].refresh_from_db()
    assert env["copy_b"].status == CopyStatus.AVAILABLE
    # Patron privacy-scrubbed (default retain_loan_history=False).
    assert ill.requesting_patron_id is None and ill.patron_hash


def test_cancel_releases_reserved_copy():
    env = build_consortium()
    patron = make_patron(env["a"], env["ba"])
    ill = consortia.request_ill(
        patron=patron, work=env["work"], consortium=env["cons"], actor=patron.user
    )
    consortia.cancel_ill(ill=ill, actor=None)
    ill.refresh_from_db()
    env["copy_b"].refresh_from_db()
    assert ill.status == IllStatus.CANCELLED
    assert env["copy_b"].status == CopyStatus.AVAILABLE


def test_out_of_order_transition_rejected():
    env = build_consortium()
    patron = make_patron(env["a"], env["ba"])
    ill = consortia.request_ill(
        patron=patron, work=env["work"], consortium=env["cons"], actor=patron.user
    )
    # Cannot receive before shipping.
    with pytest.raises(DomainError):
        consortia.receive_ill(ill=ill, actor=None)


# --------------------------------------------------------------------------- #
# Floating collections
# --------------------------------------------------------------------------- #
def test_floating_copy_rehomes_on_return():
    org = Organization.objects.create(name="Float Lib", slug="float")
    b1 = Branch.objects.create(organization=org, name="One", slug="one")
    b2 = Branch.objects.create(organization=org, name="Two", slug="two")
    work = Work.objects.create(canonical_title="Floater", slug="floater")
    edition = Edition.objects.create(work=work, isbn_13="9780000000010")
    copy = Copy.objects.create(
        organization=org, edition=edition, branch=b1, barcode="F1",
        floating=True, status=CopyStatus.LOANED,
    )
    patron = make_patron(org, b1)
    loan = Loan.objects.create(
        organization=org, copy=copy, patron=patron, due_at=timezone.now() + timedelta(days=7)
    )
    services.return_loan(loan=loan, settle_branch=b2)
    copy.refresh_from_db()
    assert copy.branch_id == b2.pk and copy.status == CopyStatus.AVAILABLE


def test_non_floating_copy_not_rehomed():
    org = Organization.objects.create(name="Fixed Lib", slug="fixed")
    b1 = Branch.objects.create(organization=org, name="One", slug="one")
    b2 = Branch.objects.create(organization=org, name="Two", slug="two")
    work = Work.objects.create(canonical_title="Fixed", slug="fixed-w")
    edition = Edition.objects.create(work=work, isbn_13="9780000000011")
    copy = Copy.objects.create(
        organization=org, edition=edition, branch=b1, barcode="X1", status=CopyStatus.LOANED
    )
    patron = make_patron(org, b1)
    loan = Loan.objects.create(
        organization=org, copy=copy, patron=patron, due_at=timezone.now() + timedelta(days=7)
    )
    services.return_loan(loan=loan, settle_branch=b2)
    copy.refresh_from_db()
    assert copy.branch_id == b1.pk  # stays home


# --------------------------------------------------------------------------- #
# API surface + org-side authorization
# --------------------------------------------------------------------------- #
def test_ill_api_lifecycle_and_authorization():
    env = build_consortium()
    patron = make_patron(env["a"], env["ba"])
    staff_a = make_staff(env["a"])
    staff_b = make_staff(env["b"])

    resp = _api(patron.user).post(
        "/api/v1/consortium/metro-net/request/", {"work": "shared"}, format="json", secure=True
    )
    assert resp.status_code == 201
    ill_id = resp.json()["data"]["id"]

    # Wrong side: A is the borrower, so A cannot ship (lending is B).
    resp = _api(staff_a).post(
        f"/api/v1/librarian/ill/{ill_id}/ship/?org=a", {}, format="json", secure=True
    )
    assert resp.status_code == 403

    # Lender (B) ships; borrower (A) receives.
    assert _api(staff_b).post(
        f"/api/v1/librarian/ill/{ill_id}/ship/?org=b", {}, format="json", secure=True
    ).status_code == 200
    resp = _api(staff_a).post(
        f"/api/v1/librarian/ill/{ill_id}/receive/?org=a", {}, format="json", secure=True
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == IllStatus.ON_LOAN

    # Borrower's ILL list shows the request.
    listing = _api(staff_a).get("/api/v1/librarian/ill/?org=a", secure=True)
    assert any(row["id"] == ill_id for row in listing.json()["data"])
