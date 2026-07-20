"""Additional HTML view coverage for librarian, privacy, MFA edges, and legal pages."""

import time
import uuid

import pytest
from django.contrib.auth import get_user_model

from library import mfa
from library.models import (
    Branch,
    Copy,
    Edition,
    Organization,
    PatronProfile,
    StaffMembership,
    StaffRole,
    Work,
)
from library.services import borrow_work, place_hold

pytestmark = pytest.mark.django_db(transaction=True)


def _slug(prefix="vw"):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _catalog():
    slug = _slug()
    org = Organization.objects.create(name="View Org", slug=slug)
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title="View Book", slug=f"book-{slug}")
    edition = Edition.objects.create(work=work, isbn_13=f"978{uuid.uuid4().int % 10**10:010d}")
    copy = Copy.objects.create(
        organization=org, edition=edition, branch=branch, barcode=f"B-{slug}"
    )
    user = get_user_model().objects.create_user(
        username=f"patron-{slug}", password="demo12345", email=f"{slug}@ex.test"
    )
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number=f"C-{slug}", home_branch=branch
    )
    return org, branch, work, copy, patron


def test_readyz_and_legal_pages(client):
    assert client.get("/readyz/", secure=True).status_code == 200
    assert client.get("/terms/", secure=True).status_code == 200
    assert client.get("/privacy/", secure=True).status_code == 200
    assert client.get("/status/", secure=True).status_code == 200


def test_catalog_search_and_work_detail(client):
    org, _branch, work, _copy, _patron = _catalog()
    session = client.session
    session["organization_slug"] = org.slug
    session.save()
    resp = client.get("/", secure=True)
    assert resp.status_code == 200
    resp = client.get(f"/works/{work.slug}/", secure=True)
    assert resp.status_code == 200
    assert work.canonical_title.encode() in resp.content


def test_place_hold_and_return_html(client):
    org, branch, work, copy, patron = _catalog()
    # Occupy the only copy so hold queues.
    other = get_user_model().objects.create_user(username=f"o-{_slug()}", password="demo12345")
    other_patron = PatronProfile.objects.create(
        user=other, organization=org, library_card_number=f"O-{_slug()}", home_branch=branch
    )
    borrow_work(patron=other_patron, work=work, branch=branch, actor=other)
    assert client.login(username=patron.user.username, password="demo12345")
    session = client.session
    session["organization_slug"] = org.slug
    session.save()
    resp = client.post(f"/works/{work.slug}/hold/", {"branch": "main"}, secure=True)
    assert resp.status_code == 302
    hold = patron.holds.first()
    assert hold is not None
    resp = client.post(f"/account/holds/{hold.pk}/cancel/", secure=True)
    assert resp.status_code == 302


def test_return_loan_html(client):
    org, branch, work, _copy, patron = _catalog()
    loan = borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    assert client.login(username=patron.user.username, password="demo12345")
    resp = client.post(f"/account/loans/{loan.pk}/return/", secure=True)
    assert resp.status_code == 302
    loan.refresh_from_db()
    assert loan.status == "returned"


def test_export_and_erase_html_account(client):
    org, branch, work, _copy, patron = _catalog()
    assert client.login(username=patron.user.username, password="demo12345")
    session = client.session
    session["organization_slug"] = org.slug
    session.save()
    resp = client.get("/account/export/", secure=True)
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("application/json")
    # Erase requires no active loans/fees — patron is clean.
    resp = client.post("/account/erase/", secure=True)
    assert resp.status_code in {200, 302}


def test_librarian_dashboard_requires_staff(client):
    org, branch, _work, _copy, patron = _catalog()
    assert client.login(username=patron.user.username, password="demo12345")
    resp = client.get("/librarian/", secure=True)
    assert resp.status_code in {302, 403}


def test_librarian_dashboard_staff_ok(client):
    org, branch, work, _copy, _patron = _catalog()
    staff = get_user_model().objects.create_user(
        username=f"staff-{_slug()}", password="demo12345"
    )
    StaffMembership.objects.create(
        user=staff, organization=org, role=StaffRole.ADMIN, branch=None, active=True
    )
    assert client.login(username=staff.username, password="demo12345")
    session = client.session
    session["organization_slug"] = org.slug
    session.save()
    resp = client.get("/librarian/", secure=True)
    assert resp.status_code == 200
    resp = client.get("/librarian/reports/", secure=True)
    assert resp.status_code == 200
    resp = client.get("/librarian/imports/", secure=True)
    assert resp.status_code == 200
    resp = client.get("/billing/", secure=True)
    assert resp.status_code == 200


def test_mfa_challenge_rejects_bad_code(client):
    org, _branch, _work, _copy, _patron = _catalog()
    org.require_staff_mfa = True
    org.save(update_fields=["require_staff_mfa"])
    staff = get_user_model().objects.create_user(
        username=f"mfa-{_slug()}", password="demo12345"
    )
    StaffMembership.objects.create(
        user=staff, organization=org, role=StaffRole.ADMIN, active=True
    )
    info = mfa.begin_enrollment(user=staff)
    mfa.confirm_enrollment(user=staff, code=mfa.totp(info["secret"], timestamp=time.time()))
    assert client.login(username=staff.username, password="demo12345")
    session = client.session
    session["organization_slug"] = org.slug
    session.save()
    resp = client.post("/mfa/challenge/", {"code": "000000", "next": "/librarian/"}, secure=True)
    assert resp.status_code == 200
    assert b"incorrect" in resp.content.lower() or b"code" in resp.content.lower()


def test_submit_review_view(client):
    org, branch, work, _copy, patron = _catalog()
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    assert client.login(username=patron.user.username, password="demo12345")
    resp = client.post(
        f"/works/{work.slug}/review/",
        {"rating": "5", "body": "Great read for coverage."},
        secure=True,
    )
    assert resp.status_code in {200, 302}
