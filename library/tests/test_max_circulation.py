"""Comprehensive circulation tests — services, API, and HTML."""

import json
import uuid
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
    Hold,
    HoldStatus,
    Loan,
    LoanStatus,
    Organization,
    PatronProfile,
    Work,
)
from library.services import (
    DomainError,
    borrow_work,
    cancel_hold,
    expire_ready_holds,
    place_hold,
    return_loan,
)

pytestmark = pytest.mark.django_db(transaction=True)


def _uid():
    return uuid.uuid4().hex[:8]


def make_catalog(*, password="demo12345"):
    """Build an isolated catalog with one available copy and two patrons."""
    uid = _uid()
    org = Organization.objects.create(name=f"Lib {uid}", slug=f"lib-{uid}")
    branch = Branch.objects.create(
        organization=org, name="Main", slug="main", max_renewals=2
    )
    work = Work.objects.create(canonical_title=f"Book {uid}", slug=f"book-{uid}")
    edition = Edition.objects.create(work=work, isbn_13=f"978{uid[:10]}".ljust(13, "0")[:13])
    copy = Copy.objects.create(organization=org, edition=edition, branch=branch, barcode=f"C-{uid}")
    User = get_user_model()
    user = User.objects.create_user(username=f"reader-{uid}", password=password)
    patron = PatronProfile.objects.create(
        user=user,
        organization=org,
        library_card_number=f"CARD-{uid}",
        home_branch=branch,
    )
    other_user = User.objects.create_user(username=f"other-{uid}", password=password)
    other = PatronProfile.objects.create(
        user=other_user,
        organization=org,
        library_card_number=f"CARD2-{uid}",
        home_branch=branch,
    )
    return org, branch, work, copy, patron, other


def _api(user):
    client = APIClient(enforce_csrf_checks=False)
    client.force_authenticate(user=user)
    client.defaults["secure"] = True
    return client


# --------------------------------------------------------------------------- #
# Service layer — cancel_hold
# --------------------------------------------------------------------------- #
def test_cancel_waiting_hold():
    _org, branch, work, _copy, patron, other = make_catalog()
    borrow_work(patron=other, work=work, branch=branch, actor=other.user)
    hold = place_hold(patron=patron, work=work, preferred_branch=branch, actor=patron.user)

    assert hold.status == HoldStatus.WAITING
    assert hold.assigned_copy_id is None

    cancelled = cancel_hold(hold=hold, actor=patron.user)
    cancelled.refresh_from_db()
    assert cancelled.status == HoldStatus.CANCELLED


def test_cancel_ready_hold_frees_copy():
    _org, branch, work, copy, patron, _other = make_catalog()
    hold = place_hold(patron=patron, work=work, preferred_branch=branch, actor=patron.user)
    copy.refresh_from_db()
    assert hold.status == HoldStatus.READY
    assert copy.status == CopyStatus.ON_HOLD

    cancel_hold(hold=hold, actor=patron.user)

    hold.refresh_from_db()
    copy.refresh_from_db()
    assert hold.status == HoldStatus.CANCELLED
    assert copy.status == CopyStatus.AVAILABLE


def test_cancel_already_cancelled_raises():
    _org, branch, work, _copy, patron, _other = make_catalog()
    hold = place_hold(patron=patron, work=work, preferred_branch=branch, actor=patron.user)
    cancel_hold(hold=hold, actor=patron.user)

    with pytest.raises(DomainError, match="Only active holds"):
        cancel_hold(hold=hold, actor=patron.user)


# --------------------------------------------------------------------------- #
# Service layer — expire_ready_holds
# --------------------------------------------------------------------------- #
def test_expire_ready_holds_marks_expired_and_frees_copy():
    _org, branch, work, copy, patron, _other = make_catalog()
    hold = place_hold(patron=patron, work=work, preferred_branch=branch, actor=patron.user)
    assert hold.status == HoldStatus.READY

    hold.expires_at = timezone.now() - timedelta(hours=1)
    hold.save(update_fields=["expires_at"])

    expired_count = expire_ready_holds()
    assert expired_count == 1

    hold.refresh_from_db()
    copy.refresh_from_db()
    assert hold.status == HoldStatus.EXPIRED
    assert copy.status == CopyStatus.AVAILABLE


# --------------------------------------------------------------------------- #
# API — circulation happy path
# --------------------------------------------------------------------------- #
def test_api_circulation_happy_path():
    _org, branch, work, copy, patron, other = make_catalog()
    client = _api(patron.user)

    borrow_resp = client.post(
        f"/api/v1/catalog/works/{work.slug}/borrow/",
        {"branch": "main"},
        format="json",
        secure=True,
    )
    assert borrow_resp.status_code == 201
    loan_data = borrow_resp.json()["data"]
    assert loan_data["status"] == LoanStatus.ACTIVE
    assert loan_data["title"] == work.canonical_title
    loan_id = loan_data["id"]
    copy.refresh_from_db()
    assert copy.status == CopyStatus.LOANED

    account_resp = client.get("/api/v1/account/", secure=True)
    assert account_resp.status_code == 200
    account_body = account_resp.json()
    assert len(account_body["loans"]) == 1
    assert account_body["loans"][0]["id"] == loan_id
    assert account_body["holds"] == []

    renew_resp = client.post(f"/api/v1/loans/{loan_id}/renew/", secure=True)
    assert renew_resp.status_code == 200
    renewed = renew_resp.json()["data"]
    assert renewed["renewal_count"] == 1
    assert renewed["status"] == LoanStatus.ACTIVE

    return_resp = client.post(f"/api/v1/loans/{loan_id}/return/", secure=True)
    assert return_resp.status_code == 204
    copy.refresh_from_db()
    assert copy.status == CopyStatus.AVAILABLE
    loan = Loan.objects.get(pk=loan_id)
    assert loan.status == LoanStatus.RETURNED

    borrow_work(patron=other, work=work, branch=branch, actor=other.user)
    copy.refresh_from_db()
    assert copy.status == CopyStatus.LOANED

    hold_resp = client.post(
        f"/api/v1/catalog/works/{work.slug}/hold/",
        {"branch": "main"},
        format="json",
        secure=True,
    )
    assert hold_resp.status_code == 201
    hold_data = hold_resp.json()["data"]
    assert hold_data["status"] == HoldStatus.WAITING
    hold_id = hold_data["id"]

    cancel_resp = client.post(f"/api/v1/holds/{hold_id}/cancel/", secure=True)
    assert cancel_resp.status_code == 204
    hold = Hold.objects.get(pk=hold_id)
    assert hold.status == HoldStatus.CANCELLED


def test_work_detail_api():
    _org, _branch, work, _copy, patron, _other = make_catalog()
    client = _api(patron.user)

    resp = client.get(f"/api/v1/catalog/works/{work.slug}/", secure=True)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["slug"] == work.slug
    assert data["canonical_title"] == work.canonical_title
    assert data["availability"]["available"] == 1
    assert data["availability"]["total"] == 1


def test_account_export_api():
    _org, branch, work, _copy, patron, _other = make_catalog()
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    client = _api(patron.user)

    resp = client.get("/api/v1/account/export/", secure=True)
    assert resp.status_code == 200
    assert resp["Content-Disposition"] == 'attachment; filename="my-library-data.json"'
    payload = resp.json()
    assert payload["profile"]["library_card_number"] == patron.library_card_number
    assert len(payload["loans"]) == 1
    assert payload["loans"][0]["title"] == work.canonical_title


# --------------------------------------------------------------------------- #
# HTML — health, patron journey, export
# --------------------------------------------------------------------------- #
def test_healthz_returns_ok(client):
    resp = client.get("/healthz/", secure=True)
    assert resp.status_code == 200
    assert resp.content == b"ok"


def test_html_circulation_journey(client):
    org, branch, work, copy, patron, other = make_catalog()
    password = "demo12345"

    detail_resp = client.get(f"/works/{work.slug}/?org={org.slug}", secure=True)
    assert detail_resp.status_code == 200
    assert work.canonical_title.encode() in detail_resp.content

    assert client.login(username=patron.user.username, password=password)

    borrow_resp = client.post(
        f"/works/{work.slug}/borrow/",
        {"branch": "main"},
        secure=True,
    )
    assert borrow_resp.status_code == 302
    copy.refresh_from_db()
    assert copy.status == CopyStatus.LOANED

    account_resp = client.get("/account/", secure=True)
    assert account_resp.status_code == 200
    assert work.canonical_title.encode() in account_resp.content

    loan = Loan.objects.get(patron=patron, status=LoanStatus.ACTIVE)
    renew_resp = client.post(f"/account/loans/{loan.pk}/renew/", secure=True)
    assert renew_resp.status_code == 302
    loan.refresh_from_db()
    assert loan.renewal_count == 1

    return_loan(loan=loan, actor=patron.user)
    borrow_work(patron=other, work=work, branch=branch, actor=other.user)
    hold = place_hold(patron=patron, work=work, preferred_branch=branch, actor=patron.user)
    assert hold.status == HoldStatus.WAITING

    cancel_resp = client.post(f"/account/holds/{hold.pk}/cancel/", secure=True)
    assert cancel_resp.status_code == 302
    hold.refresh_from_db()
    assert hold.status == HoldStatus.CANCELLED

    settings_resp = client.get("/account/settings/", secure=True)
    assert settings_resp.status_code == 200
    assert b"Account settings" in settings_resp.content


def test_export_my_data_html(client):
    org, branch, work, _copy, patron, _other = make_catalog()
    password = "demo12345"
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)

    assert client.login(username=patron.user.username, password=password)
    resp = client.get("/account/export/", secure=True)

    assert resp.status_code == 200
    assert 'attachment; filename="my-library-data.json"' in resp["Content-Disposition"]
    payload = json.loads(resp.content)
    assert payload["profile"]["organization"] == org.slug
    assert len(payload["loans"]) == 1
    assert payload["loans"][0]["title"] == work.canonical_title
