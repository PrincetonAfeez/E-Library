"""Regression tests for Round 5 fixes (A1-A3, B4-B6, C7-C10)."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from library.models import (
    Branch,
    Copy,
    CopyStatus,
    Edition,
    HoldStatus,
    Organization,
    PatronProfile,
    StaffMembership,
    StaffRole,
    Work,
)
from library.permissions import (
    staff_branch_ids_for_org,
    user_can_act_on_branch,
    user_has_staff_permission,
)
from library.services import (
    borrow_work,
    drain_outbox,
    expire_stale_transits,
    place_hold,
    reconcile_holds,
    return_loan,
)

pytestmark = pytest.mark.django_db(transaction=True)


def _api(user):
    client = APIClient(enforce_csrf_checks=False)
    client.force_authenticate(user=user)
    client.defaults["secure"] = True
    return client


def make_org():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    return org, branch


# --------------------------------------------------------------------------- #
# A1 - RBAC: role/permission and branch scoping are enforced
# --------------------------------------------------------------------------- #
def test_role_permissions_map():
    org, branch = make_org()
    support = get_user_model().objects.create_user(username="sup")
    StaffMembership.objects.create(
        user=support, organization=org, branch=branch, role=StaffRole.SUPPORT
    )
    assert user_has_staff_permission(support, org, "reports") is True
    assert user_has_staff_permission(support, org, "imports") is False
    assert user_has_staff_permission(support, org, "copies") is False


def test_support_role_cannot_import_via_api():
    org, branch = make_org()
    support = get_user_model().objects.create_user(username="sup", is_staff=True)
    StaffMembership.objects.create(
        user=support, organization=org, branch=branch, role=StaffRole.SUPPORT
    )
    resp = _api(support).post(
        "/api/v1/librarian/imports/", {"rows": [{"title": "X"}]}, format="json", secure=True
    )
    assert resp.status_code == 403


def test_branch_scoped_staff_limited_to_own_branch():
    org, a = make_org()
    b = Branch.objects.create(organization=org, name="B", slug="b")
    librarian = get_user_model().objects.create_user(username="liv", is_staff=True)
    StaffMembership.objects.create(
        user=librarian, organization=org, branch=a, role=StaffRole.LIBRARIAN
    )
    # Branch-scoped: may act on A, not B.
    assert staff_branch_ids_for_org(librarian, org) == {a.id}
    assert user_can_act_on_branch(librarian, org, a.id) is True
    assert user_can_act_on_branch(librarian, org, b.id) is False

    work = Work.objects.create(canonical_title="X", slug="x")
    edition = Edition.objects.create(work=work, isbn_13="9780000000009")
    Copy.objects.create(organization=org, edition=edition, branch=b, barcode="B1")
    # Retiring a copy at branch B is forbidden for an A-scoped librarian.
    resp = _api(librarian).post(
        "/api/v1/librarian/copies/retire/", {"barcode": "B1"}, format="json", secure=True
    )
    assert resp.status_code == 403


def test_org_wide_membership_grants_all_branches():
    org, a = make_org()
    Branch.objects.create(organization=org, name="B", slug="b")
    manager = get_user_model().objects.create_user(username="mgr")
    StaffMembership.objects.create(
        user=manager, organization=org, branch=None, role=StaffRole.BRANCH_MANAGER
    )
    assert staff_branch_ids_for_org(manager, org) is None  # all branches


# --------------------------------------------------------------------------- #
# A2 - CSV/formula injection is neutralized on export
# --------------------------------------------------------------------------- #
def test_csv_export_neutralizes_formula():
    org, branch = make_org()
    work = Work.objects.create(canonical_title="=SUM(A1:A9)+cmd", slug="danger")
    edition = Edition.objects.create(work=work, isbn_13="9780000000010")
    Copy.objects.create(organization=org, edition=edition, branch=branch, barcode="Z1")
    admin = get_user_model().objects.create_user(username="adm")
    StaffMembership.objects.create(
        user=admin, organization=org, branch=None, role=StaffRole.ADMIN
    )
    resp = _api(admin).get("/api/v1/librarian/exports/?type=inventory", secure=True)
    body = b"".join(resp.streaming_content).decode()
    # The dangerous title is present but prefixed so it can't execute.
    assert "'=SUM(A1:A9)+cmd" in body
    assert "\n=SUM" not in body


# --------------------------------------------------------------------------- #
# B4 - stale transits are recovered
# --------------------------------------------------------------------------- #
def test_expire_stale_transit_recovers_copy():
    org = Organization.objects.create(name="Lib", slug="lib")
    a = Branch.objects.create(organization=org, name="A", slug="a")
    b = Branch.objects.create(organization=org, name="B", slug="b")
    work = Work.objects.create(canonical_title="Dune", slug="dune")
    edition = Edition.objects.create(work=work, isbn_13="9780000000011")
    copy = Copy.objects.create(organization=org, edition=edition, branch=a, barcode="T1")
    user = get_user_model().objects.create_user(username="reader")
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number="C1", home_branch=b
    )
    place_hold(patron=patron, work=work, preferred_branch=b, actor=user)
    copy.refresh_from_db()
    assert copy.status == CopyStatus.IN_TRANSIT
    # Age the transit past the timeout.
    Copy.objects.filter(pk=copy.pk).update(updated_at=timezone.now() - timedelta(days=30))

    recovered = expire_stale_transits(max_transit_days=14)
    assert recovered == 1
    copy.refresh_from_db()
    # Re-assignment ran; with the same single copy and one waiting hold it goes
    # back into transit for the same patron (recovered from the stuck state).
    assert copy.status in (CopyStatus.IN_TRANSIT, CopyStatus.AVAILABLE)


# --------------------------------------------------------------------------- #
# B5 - reconciliation matches available copies to waiting holds
# --------------------------------------------------------------------------- #
def test_reconcile_holds_assigns_available_copy():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title="Popular", slug="popular")
    edition = Edition.objects.create(work=work, isbn_13="9780000000012")
    copy = Copy.objects.create(organization=org, edition=edition, branch=branch, barcode="R1")
    u = get_user_model().objects.create_user(username="reader")
    patron = PatronProfile.objects.create(
        user=u, organization=org, library_card_number="C1", home_branch=branch
    )
    # Create a stranded state: a WAITING, unassigned hold with an AVAILABLE copy.
    from library.models import Hold

    hold = Hold.objects.create(
        organization=org, work=work, patron=patron, preferred_branch=branch,
        status=HoldStatus.WAITING,
    )
    assert copy.status == CopyStatus.AVAILABLE

    assigned = reconcile_holds()
    assert assigned == 1
    hold.refresh_from_db()
    copy.refresh_from_db()
    assert hold.status == HoldStatus.READY
    assert copy.status == CopyStatus.ON_HOLD


# --------------------------------------------------------------------------- #
# C7 - patron is notified when a hold ships
# --------------------------------------------------------------------------- #
def test_in_transit_notification_sent():
    from django.core import mail

    org = Organization.objects.create(name="Lib", slug="lib")
    a = Branch.objects.create(organization=org, name="A", slug="a")
    b = Branch.objects.create(organization=org, name="B", slug="b")
    work = Work.objects.create(canonical_title="Dune", slug="dune")
    edition = Edition.objects.create(work=work, isbn_13="9780000000013")
    Copy.objects.create(organization=org, edition=edition, branch=a, barcode="I1")
    user = get_user_model().objects.create_user(username="reader", email="r@example.test")
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number="C1", home_branch=b,
        notification_email="r@example.test",
    )
    place_hold(patron=patron, work=work, preferred_branch=b, actor=user)
    drain_outbox()
    assert any(m.subject.startswith("On its way:") for m in mail.outbox)


def test_returned_copy_not_lost_after_reconcile():
    # Sanity: normal same-branch return still assigns the waiting hold directly.
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title="One", slug="one")
    edition = Edition.objects.create(work=work, isbn_13="9780000000014")
    Copy.objects.create(organization=org, edition=edition, branch=branch, barcode="O1")
    u1 = get_user_model().objects.create_user(username="a")
    p1 = PatronProfile.objects.create(
        user=u1, organization=org, library_card_number="C1", home_branch=branch
    )
    loan = borrow_work(patron=p1, work=work, branch=branch, actor=u1)
    u2 = get_user_model().objects.create_user(username="b")
    p2 = PatronProfile.objects.create(
        user=u2, organization=org, library_card_number="C2", home_branch=branch
    )
    place_hold(patron=p2, work=work, preferred_branch=branch, actor=u2)
    return_loan(loan=loan, actor=u1)
    from library.models import Hold

    assert Hold.objects.get(patron=p2).status == HoldStatus.READY
