"""Tests for the patron-type × material-type circulation policy matrix (Increment 5)."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from library import policies
from library.models import (
    Branch,
    CirculationPolicy,
    Copy,
    Edition,
    Fee,
    FeeType,
    LoanStatus,
    MaterialType,
    Organization,
    PatronProfile,
    PatronType,
    StaffMembership,
    StaffRole,
    Work,
)
from library.services import (
    DomainError,
    assess_overdue_fine,
    borrow_work,
    place_hold,
    renew_loan,
)

pytestmark = pytest.mark.django_db(transaction=True)


def make_catalog(loan_days=21, max_renewals=2):
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(
        organization=org, name="Main", slug="main", loan_days=loan_days, max_renewals=max_renewals
    )
    work = Work.objects.create(canonical_title="Dune", slug="dune")
    edition = Edition.objects.create(work=work, isbn_13="9780000000001")
    copy = Copy.objects.create(organization=org, edition=edition, branch=branch, barcode="C1")
    user = get_user_model().objects.create_user(username="reader")
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number="C1", home_branch=branch
    )
    return org, branch, work, edition, copy, patron


# --------------------------------------------------------------------------- #
# Resolution + backward compatibility
# --------------------------------------------------------------------------- #
def test_no_matrix_falls_back_to_branch_and_patron_defaults():
    org, branch, work, edition, copy, patron = make_catalog(loan_days=14, max_renewals=1)
    eff = policies.resolve_policy(organization=org, patron=patron, edition=edition, branch=branch)
    assert eff.loan_days == 14  # from branch
    assert eff.max_renewals == 1
    assert eff.max_loans == patron.max_loans
    assert eff.holdable is True


def test_matrix_cell_overrides_and_specificity():
    org, branch, work, edition, copy, patron = make_catalog()
    pt = PatronType.objects.create(organization=org, code="child", name="Child", max_loans=3)
    mt = MaterialType.objects.create(organization=org, code="dvd", name="DVD")
    patron.patron_type = pt
    patron.save(update_fields=["patron_type"])
    edition.material_type = mt
    edition.save(update_fields=["material_type"])
    # Wildcard default and a specific (child, dvd) cell; the specific one wins.
    CirculationPolicy.objects.create(organization=org, loan_days=21)
    CirculationPolicy.objects.create(
        organization=org, patron_type=pt, material_type=mt, loan_days=5, max_renewals=0
    )
    eff = policies.resolve_policy(organization=org, patron=patron, edition=edition, branch=branch)
    assert eff.loan_days == 5
    assert eff.max_renewals == 0
    assert eff.max_loans == 3  # from patron type


# --------------------------------------------------------------------------- #
# Engine integration
# --------------------------------------------------------------------------- #
def test_borrow_uses_matrix_loan_period():
    org, branch, work, edition, copy, patron = make_catalog(loan_days=21)
    mt = MaterialType.objects.create(organization=org, code="ref", name="Reference")
    edition.material_type = mt
    edition.save(update_fields=["material_type"])
    CirculationPolicy.objects.create(
        organization=org, material_type=mt, loan_days=3, holdable=True
    )
    loan = borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    # Due in ~3 days, not the branch's 21.
    assert (loan.due_at - timezone.now()).days == 2  # 3 days minus a hair


def test_non_holdable_material_blocks_hold():
    org, branch, work, edition, copy, patron = make_catalog()
    mt = MaterialType.objects.create(organization=org, code="ref", name="Reference")
    edition.material_type = mt
    edition.save(update_fields=["material_type"])
    CirculationPolicy.objects.create(organization=org, material_type=mt, holdable=False)
    # Loan out the copy so a hold would otherwise queue.
    other = get_user_model().objects.create_user(username="o")
    p2 = PatronProfile.objects.create(
        user=other, organization=org, library_card_number="C2", home_branch=branch
    )
    borrow_work(patron=p2, work=work, branch=branch, actor=other)
    with pytest.raises(DomainError):
        place_hold(patron=patron, work=work, preferred_branch=branch, actor=patron.user)


def test_renewal_cap_from_matrix():
    org, branch, work, edition, copy, patron = make_catalog(max_renewals=5)
    mt = MaterialType.objects.create(organization=org, code="dvd", name="DVD")
    edition.material_type = mt
    edition.save(update_fields=["material_type"])
    CirculationPolicy.objects.create(organization=org, material_type=mt, max_renewals=1, loan_days=7)
    loan = borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    renew_loan(loan=loan, actor=patron.user)  # 1st renewal OK
    loan.refresh_from_db()
    with pytest.raises(DomainError):
        renew_loan(loan=loan, actor=patron.user)  # cap is 1


def test_matrix_overrides_fine_rate():
    org, branch, work, edition, copy, patron = make_catalog()
    mt = MaterialType.objects.create(organization=org, code="dvd", name="DVD")
    edition.material_type = mt
    edition.save(update_fields=["material_type"])
    # DVDs fine at 100c/day (vs the FeePolicy default 25c).
    CirculationPolicy.objects.create(
        organization=org, material_type=mt, daily_overdue_cents=100, max_overdue_cents=10000
    )
    loan = borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    loan.due_at = timezone.now() - timedelta(days=4)
    loan.status = LoanStatus.OVERDUE
    loan.save(update_fields=["due_at", "status"])
    assess_overdue_fine(loan=loan)
    fee = Fee.objects.get(loan=loan, fee_type=FeeType.OVERDUE)
    assert fee.amount_cents == 400  # 4 days * 100c


def test_max_loans_from_patron_type():
    org, branch, work, edition, copy, patron = make_catalog()
    pt = PatronType.objects.create(organization=org, code="kid", name="Kid", max_loans=1)
    patron.patron_type = pt
    patron.save(update_fields=["patron_type"])
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    # A second work; the patron-type cap of 1 blocks it.
    w2 = Work.objects.create(canonical_title="Two", slug="two")
    e2 = Edition.objects.create(work=w2, isbn_13="9780000000002")
    Copy.objects.create(organization=org, edition=e2, branch=branch, barcode="C2")
    with pytest.raises(DomainError):
        borrow_work(patron=patron, work=w2, branch=branch, actor=patron.user)


def test_policies_api():
    org, branch, work, edition, copy, patron = make_catalog()
    CirculationPolicy.objects.create(organization=org, loan_days=21)
    staff = get_user_model().objects.create_user(username="adm", is_staff=True)
    StaffMembership.objects.create(user=staff, organization=org, branch=None, role=StaffRole.ADMIN)
    client = APIClient(enforce_csrf_checks=False)
    client.force_authenticate(user=staff)
    resp = client.get("/api/v1/librarian/policies/", secure=True)
    assert resp.status_code == 200
    assert len(resp.json()["matrix"]) == 1


def test_seed_policies_command():
    org, branch, work, edition, copy, patron = make_catalog()
    from django.core.management import call_command

    call_command("seed_policies", "--org", "lib")
    assert PatronType.objects.filter(organization=org, code="adult").exists()
    assert MaterialType.objects.filter(organization=org, code="reference").exists()
    # Reference is not holdable in the seeded matrix.
    ref = MaterialType.objects.get(organization=org, code="reference")
    eff = policies.resolve_policy(
        organization=org,
        patron=patron,
        edition=Edition(material_type=ref, work=work),
    )
    assert eff.holdable is False
    assert eff.loan_days == 3
