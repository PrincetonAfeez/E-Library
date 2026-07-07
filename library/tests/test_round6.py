"""Regression tests for Round 6 fixes (A1-A3, B4-B5)."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory
from django.utils import timezone
from rest_framework.test import APIClient

from library.auth import ScopedTokenAuthentication
from library.models import (
    Branch,
    Copy,
    CopyStatus,
    Edition,
    HoldStatus,
    Organization,
    PatronProfile,
    ScopedApiToken,
    StaffMembership,
    StaffRole,
    Work,
)
from library.services import expire_stale_transits, place_hold

pytestmark = pytest.mark.django_db(transaction=True)


def _api(user):
    client = APIClient(enforce_csrf_checks=False)
    client.force_authenticate(user=user)
    client.defaults["secure"] = True
    return client


def transit_setup(barcode="A1"):
    org = Organization.objects.create(name="Lib", slug="lib")
    a = Branch.objects.create(organization=org, name="A", slug="a")
    b = Branch.objects.create(organization=org, name="B", slug="b")
    work = Work.objects.create(canonical_title="Dune", slug="dune")
    edition = Edition.objects.create(work=work, isbn_13="9780000000001")
    copy_a = Copy.objects.create(organization=org, edition=edition, branch=a, barcode=barcode)
    user = get_user_model().objects.create_user(username="reader", email="r@example.test")
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number="C1", home_branch=b
    )
    place_hold(patron=patron, work=work, preferred_branch=b, actor=user)
    copy_a.refresh_from_db()
    assert copy_a.status == CopyStatus.IN_TRANSIT
    return org, a, b, work, copy_a, patron


def _staff(org, branch, role=StaffRole.LIBRARIAN, username="staff"):
    user = get_user_model().objects.create_user(username=username, is_staff=True)
    StaffMembership.objects.create(user=user, organization=org, branch=branch, role=role)
    return user


# --------------------------------------------------------------------------- #
# A1 - the destination-branch staff can check in an in-transit copy
# --------------------------------------------------------------------------- #
def test_destination_branch_staff_can_checkin_transit_copy():
    org, a, b, _work, copy_a, _patron = transit_setup()
    staff_b = _staff(org, b)  # scoped to the destination branch
    resp = _api(staff_b).post(
        "/api/v1/librarian/checkin/", {"barcode": "A1"}, format="json", secure=True
    )
    assert resp.status_code == 200
    copy_a.refresh_from_db()
    assert copy_a.status == CopyStatus.ON_HOLD
    assert copy_a.branch_id == b.id


def test_unrelated_branch_staff_cannot_checkin_transit_copy():
    org, a, b, _work, _copy_a, _patron = transit_setup()
    c = Branch.objects.create(organization=org, name="C", slug="c")
    staff_c = _staff(org, c, username="other")  # neither source nor destination
    resp = _api(staff_c).post(
        "/api/v1/librarian/checkin/", {"barcode": "A1"}, format="json", secure=True
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# A2 - stale transit gives up after max attempts
# --------------------------------------------------------------------------- #
def test_stale_transit_gives_up_after_max_attempts():
    from library.models import Hold

    org, a, b, _work, copy_a, _patron = transit_setup()
    hold = Hold.objects.get(assigned_copy=copy_a)
    Copy.objects.filter(pk=copy_a.pk).update(updated_at=timezone.now() - timedelta(days=30))

    expire_stale_transits(max_transit_days=14, max_attempts=1)
    copy_a.refresh_from_db()
    hold.refresh_from_db()
    assert copy_a.status == CopyStatus.REPAIR  # pulled for staff review
    assert hold.assigned_copy_id is None  # detached, still WAITING
    assert hold.status == HoldStatus.WAITING
    assert hold.transit_attempts == 1


# --------------------------------------------------------------------------- #
# A3 - a valid token authenticates despite a prefix collision
# --------------------------------------------------------------------------- #
def test_token_prefix_collision_still_authenticates():
    org = Organization.objects.create(name="Lib", slug="lib")
    u = get_user_model().objects.create_user(username="u")
    raw_b, token_b = ScopedApiToken.issue(user=u, organization=org, name="b", scopes=["*"])
    raw_a, token_a = ScopedApiToken.issue(user=u, organization=org, name="a", scopes=["*"])
    # Force both tokens to share token_b's prefix; token_a is newer so a naive
    # .first() (ordered -created_at) would pick the wrong one.
    ScopedApiToken.objects.filter(pk__in=[token_a.pk, token_b.pk]).update(prefix=raw_b[:12])

    request = RequestFactory().get("/", HTTP_AUTHORIZATION=f"Bearer {raw_b}")
    user, token = ScopedTokenAuthentication().authenticate(request)
    assert token.pk == token_b.pk


# --------------------------------------------------------------------------- #
# B4 - the imports link is gated on the imports permission
# --------------------------------------------------------------------------- #
def test_imports_link_hidden_for_plain_librarian(client):
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    librarian = _staff(org, branch, role=StaffRole.LIBRARIAN, username="liv")
    client.force_login(librarian)
    resp = client.get("/librarian/", secure=True)
    assert resp.status_code == 200
    assert b"Catalog imports" not in resp.content


def test_imports_link_shown_for_branch_manager(client):
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    manager = _staff(org, branch, role=StaffRole.BRANCH_MANAGER, username="mgr")
    client.force_login(manager)
    resp = client.get("/librarian/", secure=True)
    assert resp.status_code == 200
    assert b"Catalog imports" in resp.content


# --------------------------------------------------------------------------- #
# B5 - branch-scoped checkout without a branch is constrained
# --------------------------------------------------------------------------- #
def test_checkout_without_branch_requires_choice_when_multi_branch():
    org = Organization.objects.create(name="Lib", slug="lib")
    a = Branch.objects.create(organization=org, name="A", slug="a")
    b = Branch.objects.create(organization=org, name="B", slug="b")
    work = Work.objects.create(canonical_title="Dune", slug="dune")
    edition = Edition.objects.create(work=work, isbn_13="9780000000002")
    Copy.objects.create(organization=org, edition=edition, branch=a, barcode="C1")
    user = get_user_model().objects.create_user(username="reader")
    PatronProfile.objects.create(
        user=user, organization=org, library_card_number="CARD-9", home_branch=a
    )
    # Librarian scoped to two branches, no default -> must specify.
    staff = get_user_model().objects.create_user(username="staff", is_staff=True)
    StaffMembership.objects.create(user=staff, organization=org, branch=a, role=StaffRole.LIBRARIAN)
    StaffMembership.objects.create(user=staff, organization=org, branch=b, role=StaffRole.LIBRARIAN)

    resp = _api(staff).post(
        "/api/v1/librarian/checkout/",
        {"card_number": "CARD-9", "work_slug": "dune"},
        format="json",
        secure=True,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "branch_required"


def test_checkout_without_branch_defaults_to_single_branch():
    org = Organization.objects.create(name="Lib", slug="lib")
    a = Branch.objects.create(organization=org, name="A", slug="a")
    work = Work.objects.create(canonical_title="Dune", slug="dune")
    edition = Edition.objects.create(work=work, isbn_13="9780000000003")
    Copy.objects.create(organization=org, edition=edition, branch=a, barcode="C1")
    user = get_user_model().objects.create_user(username="reader")
    PatronProfile.objects.create(
        user=user, organization=org, library_card_number="CARD-9", home_branch=a
    )
    staff = _staff(org, a, role=StaffRole.LIBRARIAN, username="staff")
    resp = _api(staff).post(
        "/api/v1/librarian/checkout/",
        {"card_number": "CARD-9", "work_slug": "dune"},
        format="json",
        secure=True,
    )
    assert resp.status_code == 201
    assert Copy.objects.get(barcode="C1").status == CopyStatus.LOANED
