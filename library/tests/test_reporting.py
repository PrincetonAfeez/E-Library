"""Tests for reporting & analytics (Increment 4)."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from library import reporting
from library.models import (
    Branch,
    Copy,
    Edition,
    Fee,
    FeeType,
    LoanStatus,
    Organization,
    PatronProfile,
    SearchQueryLog,
    StaffMembership,
    StaffRole,
    Work,
)
from library.services import borrow_work, record_payment

pytestmark = pytest.mark.django_db(transaction=True)


def make_catalog(barcode="C1", isbn="9780000000001"):
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title="Dune", slug="dune")
    edition = Edition.objects.create(work=work, isbn_13=isbn)
    copy = Copy.objects.create(organization=org, edition=edition, branch=branch, barcode=barcode)
    user = get_user_model().objects.create_user(username="reader", email="r@example.test")
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number="C1", home_branch=branch
    )
    return org, branch, work, copy, patron


def _staff_client(org, role=StaffRole.ADMIN):
    staff = get_user_model().objects.create_user(username="adm", is_staff=True, email="a@x.test")
    StaffMembership.objects.create(user=staff, organization=org, branch=None, role=role)
    client = APIClient(enforce_csrf_checks=False)
    client.force_authenticate(user=staff)
    client.defaults["secure"] = True
    return client, staff


# --------------------------------------------------------------------------- #
# Selectors
# --------------------------------------------------------------------------- #
def test_circulation_and_popular():
    org, branch, work, copy, patron = make_catalog()
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    start, end = reporting.default_window(30)

    circ = reporting.circulation_summary(org, start, end)
    assert circ["borrowed"] == 1
    assert circ["active_now"] == 1
    assert circ["unique_borrowers"] == 1

    popular = reporting.popular_titles(org, start, end)
    assert popular[0]["title"] == "Dune" and popular[0]["loans"] == 1


def test_collection_stats():
    org, branch, work, copy, patron = make_catalog()
    stats = reporting.collection_stats(org)
    assert stats["total_copies"] == 1
    assert stats["total_works"] == 1
    assert stats["by_status"].get("available") == 1


def test_overdue_aging_buckets():
    org, branch, work, copy, patron = make_catalog()
    loan = borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    loan.due_at = timezone.now() - timedelta(days=40)
    loan.status = LoanStatus.OVERDUE
    loan.save(update_fields=["due_at", "status"])
    aging = reporting.overdue_aging(org)
    assert aging["31-90"] == 1
    assert aging["1-7"] == 0


def test_fines_summary():
    org, branch, work, copy, patron = make_catalog()
    Fee.objects.create(organization=org, patron=patron, fee_type=FeeType.MANUAL, amount_cents=500)
    record_payment(patron=patron, amount_cents=200)
    start, end = reporting.default_window(30)
    summary = reporting.fines_summary(org, start, end)
    assert summary["assessed_cents"] == 500
    assert summary["collected_cents"] == 200
    assert summary["outstanding_cents"] == 300


def test_search_analytics():
    org, branch, work, copy, patron = make_catalog()
    SearchQueryLog.objects.create(organization=org, query="dune", result_count=3, latency_ms=10)
    SearchQueryLog.objects.create(organization=org, query="zzz", result_count=0, latency_ms=20)
    start, end = reporting.default_window(30)
    sa = reporting.search_analytics(org, start, end)
    assert sa["volume"] == 2
    assert sa["zero_result"] == 1
    assert sa["avg_latency_ms"] == 15.0


# --------------------------------------------------------------------------- #
# API + HTML + email
# --------------------------------------------------------------------------- #
def test_reports_api():
    org, branch, work, copy, patron = make_catalog()
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    client, _ = _staff_client(org)
    resp = client.get("/api/v1/librarian/reports/?type=circulation&days=30", secure=True)
    assert resp.status_code == 200
    assert resp.json()["data"]["borrowed"] == 1

    resp = client.get("/api/v1/librarian/reports/?type=nonsense", secure=True)
    assert resp.status_code == 400


def test_reports_api_requires_reports_permission():
    org, branch, work, copy, patron = make_catalog()
    # A support staffer has 'reports'; give them access.
    client, _ = _staff_client(org, role=StaffRole.SUPPORT)
    assert client.get("/api/v1/librarian/reports/?type=collection", secure=True).status_code == 200


def test_reports_dashboard_html(client):
    org, branch, work, copy, patron = make_catalog()
    _api_client, staff = _staff_client(org)
    client.force_login(staff)
    resp = client.get("/librarian/reports/", secure=True)
    assert resp.status_code == 200
    assert b"Circulation" in resp.content


def test_email_reports_command():
    org, branch, work, copy, patron = make_catalog()
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    # Admin recipient with an email.
    admin = get_user_model().objects.create_user(username="boss", email="boss@example.test")
    StaffMembership.objects.create(user=admin, organization=org, branch=None, role=StaffRole.ADMIN)

    call_command("email_reports", "--org", "lib", "--days", "30")
    assert len(mail.outbox) == 1
    assert "boss@example.test" in mail.outbox[0].to
    assert "Dune" in mail.outbox[0].body
