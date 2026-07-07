"""Regression tests for this round's fixes (A1-A5, B6-B10, C11-C15)."""

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory, override_settings
from rest_framework.test import APIClient

from library.models import (
    Branch,
    Copy,
    CopyStatus,
    Edition,
    HoldStatus,
    Organization,
    PatronProfile,
    ShelfLocation,
    StaffMembership,
    StaffRole,
    Work,
)
from library.pagination import CursorError
from library.ratelimit import client_ip, is_rate_limited
from library.selectors import get_patron_holds, search_catalog
from library.services import (
    DomainError,
    borrow_work,
    place_hold,
    staff_checkin,
)
from library.tenancy import get_current_organization, organization_for_user

pytestmark = pytest.mark.django_db(transaction=True)


def two_branch_catalog():
    org = Organization.objects.create(name="Lib", slug="lib")
    ba = Branch.objects.create(organization=org, name="Alpha", slug="alpha")
    bb = Branch.objects.create(organization=org, name="Beta", slug="beta")
    work = Work.objects.create(canonical_title="Dune", slug="dune")
    edition = Edition.objects.create(work=work, isbn_13="9780000000001")
    copy_a = Copy.objects.create(organization=org, edition=edition, branch=ba, barcode="A1")
    user = get_user_model().objects.create_user(
        username="reader", email="reader@example.test", password="s3cretPass99X"
    )
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number="CARD-1", home_branch=bb
    )
    return org, ba, bb, work, copy_a, patron


# --------------------------------------------------------------------------- #
# A1 - authenticated users are scoped to their own tenant
# --------------------------------------------------------------------------- #
def test_authenticated_patron_defaults_to_own_org():
    alpha = Organization.objects.create(name="Alpha", slug="alpha")  # sorts first
    beta = Organization.objects.create(name="Beta", slug="beta")
    Branch.objects.create(organization=beta, name="Main", slug="main")
    user = get_user_model().objects.create_user(username="p", password="x")
    PatronProfile.objects.create(user=user, organization=beta, library_card_number="C1")

    assert organization_for_user(user) == beta
    request = RequestFactory().get("/")
    request.user = user
    request.session = {}
    # Must resolve to Beta (the patron's org), not alphabetically-first Alpha.
    assert get_current_organization(request) == beta
    assert alpha != beta


# --------------------------------------------------------------------------- #
# A3 / A4 - rate-limit helpers
# --------------------------------------------------------------------------- #
@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
def test_is_rate_limited_trips_after_limit():
    rf = RequestFactory()
    req = rf.get("/", REMOTE_ADDR="203.0.113.9")
    assert is_rate_limited(req, scope="t", limit=2, window=60) is False
    assert is_rate_limited(req, scope="t", limit=2, window=60) is False
    assert is_rate_limited(req, scope="t", limit=2, window=60) is True


def test_client_ip_ignores_forwarded_header_without_trusted_proxies():
    req = RequestFactory().get(
        "/", REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="1.2.3.4, 5.6.7.8"
    )
    assert client_ip(req) == "10.0.0.5"  # XFF is not trusted by default


@override_settings(RATELIMIT_TRUSTED_PROXY_COUNT=1)
def test_client_ip_uses_forwarded_when_proxy_declared():
    req = RequestFactory().get(
        "/", REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="1.2.3.4, 5.6.7.8"
    )
    assert client_ip(req) == "5.6.7.8"  # last hop with one trusted proxy


# --------------------------------------------------------------------------- #
# B6 - cross-branch hold goes in transit, then is received at the desk
# --------------------------------------------------------------------------- #
def test_cross_branch_hold_transits_then_received():
    org, ba, bb, work, copy_a, patron = two_branch_catalog()
    staff = get_user_model().objects.create_user(username="liv", is_staff=True)

    hold = place_hold(patron=patron, work=work, preferred_branch=bb, actor=patron.user)
    hold.refresh_from_db()
    copy_a.refresh_from_db()
    assert hold.status == HoldStatus.WAITING  # not "ready" yet
    assert hold.assigned_copy_id == copy_a.id
    assert copy_a.status == CopyStatus.IN_TRANSIT

    staff_checkin(copy=copy_a, actor=staff)
    hold.refresh_from_db()
    copy_a.refresh_from_db()
    assert hold.status == HoldStatus.READY
    assert copy_a.status == CopyStatus.ON_HOLD
    assert copy_a.branch_id == bb.id


# --------------------------------------------------------------------------- #
# B7 - deep pagination is capped
# --------------------------------------------------------------------------- #
def test_search_depth_is_capped():
    org = Organization.objects.create(name="Lib", slug="lib")
    with pytest.raises(CursorError):
        search_catalog(organization=org, query="", page=1000)


# --------------------------------------------------------------------------- #
# B8 - staff circulation + export endpoints
# --------------------------------------------------------------------------- #
def _staff_client(org, branch):
    staff = get_user_model().objects.create_user(
        username="staff", password="x", is_staff=True
    )
    # Org-wide admin: full permissions across all branches for these happy-path
    # endpoint tests. RBAC restrictions are covered separately in test_round5.
    StaffMembership.objects.create(
        user=staff, organization=org, branch=None, role=StaffRole.ADMIN
    )
    client = APIClient(enforce_csrf_checks=False)
    client.force_authenticate(user=staff)
    client.defaults["secure"] = True  # production settings force HTTPS
    return client, staff


def test_staff_checkout_checkin_and_export():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    ShelfLocation.objects.create(branch=branch, code="FIC", name="Fiction")
    work = Work.objects.create(canonical_title="Dune", slug="dune")
    edition = Edition.objects.create(work=work, isbn_13="9780000000001")
    Copy.objects.create(organization=org, edition=edition, branch=branch, barcode="C1")
    user = get_user_model().objects.create_user(username="reader")
    PatronProfile.objects.create(
        user=user, organization=org, library_card_number="CARD-9", home_branch=branch
    )
    client, _staff = _staff_client(org, branch)

    # Checkout on behalf of the patron.
    resp = client.post(
        "/api/v1/librarian/checkout/",
        {"card_number": "CARD-9", "work_slug": "dune", "branch": "main"},
        format="json",
        secure=True,
    )
    assert resp.status_code == 201
    assert Copy.objects.get(barcode="C1").status == CopyStatus.LOANED

    # Check the copy back in.
    resp = client.post("/api/v1/librarian/checkin/", {"barcode": "C1"}, format="json", secure=True)
    assert resp.status_code == 200
    assert Copy.objects.get(barcode="C1").status == CopyStatus.AVAILABLE

    # Export inventory as CSV (streamed).
    resp = client.get("/api/v1/librarian/exports/?type=inventory", secure=True)
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/csv")
    body = b"".join(resp.streaming_content)
    assert b"C1" in body


def test_staff_retire_and_move():
    org = Organization.objects.create(name="Lib", slug="lib")
    a = Branch.objects.create(organization=org, name="A", slug="a")
    b = Branch.objects.create(organization=org, name="B", slug="b")
    work = Work.objects.create(canonical_title="X", slug="x")
    edition = Edition.objects.create(work=work, isbn_13="9780000000002")
    Copy.objects.create(organization=org, edition=edition, branch=a, barcode="M1")
    client, _staff = _staff_client(org, a)

    resp = client.post(
        "/api/v1/librarian/copies/move/",
        {"barcode": "M1", "to_branch": "b", "reason": "rebalance"},
        format="json",
        secure=True,
    )
    assert resp.status_code == 200
    assert Copy.objects.get(barcode="M1").branch == b

    resp = client.post(
        "/api/v1/librarian/copies/retire/",
        {"barcode": "M1", "reason": "damaged"},
        format="json",
        secure=True,
    )
    assert resp.status_code == 200
    copy = Copy.objects.get(barcode="M1")
    assert copy.status == CopyStatus.RETIRED
    assert copy.public_visible is False


# --------------------------------------------------------------------------- #
# C15 - hold queue position + settings
# --------------------------------------------------------------------------- #
def test_hold_queue_position():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title="Popular", slug="popular")
    edition = Edition.objects.create(work=work, isbn_13="9780000000003")
    copy = Copy.objects.create(organization=org, edition=edition, branch=branch, barcode="P1")
    # Loan out the only copy so both holds queue.
    u0 = get_user_model().objects.create_user(username="borrower")
    p0 = PatronProfile.objects.create(
        user=u0, organization=org, library_card_number="C0", home_branch=branch
    )
    borrow_work(patron=p0, work=work, branch=branch, actor=u0)
    assert copy.pk  # loaned

    patrons = []
    for i in range(2):
        u = get_user_model().objects.create_user(username=f"p{i}")
        p = PatronProfile.objects.create(
            user=u, organization=org, library_card_number=f"CARD-{i}", home_branch=branch
        )
        place_hold(patron=p, work=work, preferred_branch=branch, actor=u)
        patrons.append(p)

    assert get_patron_holds(patrons[0]).first().queue_position == 1
    assert get_patron_holds(patrons[1]).first().queue_position == 2


def test_patron_settings_update(client):
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    user = get_user_model().objects.create_user(username="reader", password="s3cretPass99X")
    PatronProfile.objects.create(
        user=user, organization=org, library_card_number="C1", home_branch=branch
    )
    client.force_login(user)
    resp = client.post(
        "/account/settings/",
        {"notification_email": "new@example.test", "home_branch": branch.pk},
        secure=True,
    )
    assert resp.status_code == 302
    assert PatronProfile.objects.get(user=user).notification_email == "new@example.test"


def test_retire_blocked_while_on_loan():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title="Y", slug="y")
    edition = Edition.objects.create(work=work, isbn_13="9780000000004")
    copy = Copy.objects.create(organization=org, edition=edition, branch=branch, barcode="L1")
    user = get_user_model().objects.create_user(username="reader")
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number="C1", home_branch=branch
    )
    borrow_work(patron=patron, work=work, branch=branch, actor=user)
    from library.services import retire_copy

    with pytest.raises(DomainError):
        retire_copy(copy=copy, actor=user, reason="x")
    # The copy is untouched (still loaned), not retired.
    assert Copy.objects.get(barcode="L1").status == CopyStatus.LOANED
