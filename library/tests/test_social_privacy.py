"""Tests for discovery/social + GDPR export/erasure (Increment 8a)."""

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from library import privacy, social
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
    ReadingList,
    Review,
    Work,
)
from library.services import borrow_work, return_loan

pytestmark = pytest.mark.django_db(transaction=True)


def make_catalog(slug="dune", isbn="9780000000001", barcode="C1", title="Dune"):
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title=title, slug=slug)
    edition = Edition.objects.create(work=work, isbn_13=isbn)
    copy = Copy.objects.create(organization=org, edition=edition, branch=branch, barcode=barcode)
    return org, branch, work, edition, copy


def make_patron(org, branch, n=1):
    user = get_user_model().objects.create_user(username=f"reader{n}", email=f"r{n}@x.test")
    return PatronProfile.objects.create(
        user=user, organization=org, library_card_number=f"C{n}", home_branch=branch
    )


def _api(user):
    client = APIClient(enforce_csrf_checks=False)
    client.force_authenticate(user=user)
    client.defaults["secure"] = True
    return client


# --------------------------------------------------------------------------- #
# Reviews & ratings
# --------------------------------------------------------------------------- #
def test_review_upsert_and_rating():
    org, branch, work, edition, copy = make_catalog()
    p1 = make_patron(org, branch, 1)
    p2 = make_patron(org, branch, 2)
    social.submit_review(patron=p1, work=work, rating=4, body="Good")
    social.submit_review(patron=p2, work=work, rating=2)
    # Same patron re-reviews -> updates, not duplicates.
    social.submit_review(patron=p1, work=work, rating=5, body="Better")
    assert Review.objects.filter(work=work).count() == 2
    rating = social.work_rating(work)
    assert rating["count"] == 2
    assert rating["average"] == 3.5  # (5 + 2) / 2


def test_review_rating_validated():
    org, branch, work, edition, copy = make_catalog()
    p1 = make_patron(org, branch, 1)
    from library.services import DomainError

    with pytest.raises(DomainError):
        social.submit_review(patron=p1, work=work, rating=9)


def test_review_api_and_public_read():
    org, branch, work, edition, copy = make_catalog()
    p1 = make_patron(org, branch, 1)
    client = _api(p1.user)
    resp = client.post(
        "/api/v1/catalog/works/dune/reviews/", {"rating": 5, "body": "Loved it"}, format="json", secure=True
    )
    assert resp.status_code == 201
    # Public read (anonymous).
    anon = APIClient()
    resp = anon.get("/api/v1/catalog/works/dune/reviews/", secure=True)
    assert resp.status_code == 200
    assert resp.json()["rating"]["count"] == 1


# --------------------------------------------------------------------------- #
# Recommendations
# --------------------------------------------------------------------------- #
def test_recommendations_co_borrow():
    org, branch, work_a, ea, ca = make_catalog(slug="a", isbn="9780000000001", barcode="A1", title="A")
    _o, _b, work_b, eb, _cb = ("", "", None, None, None)
    work_b = Work.objects.create(canonical_title="B", slug="b")
    eb = Edition.objects.create(work=work_b, isbn_13="9780000000002")
    Copy.objects.create(organization=org, edition=eb, branch=branch, barcode="B1")

    patron = make_patron(org, branch, 1)
    # One patron borrows both A and B (co-borrow linkage needs the patron attached;
    # returned loans of non-retaining patrons are privacy-scrubbed and don't count).
    borrow_work(patron=patron, work=work_a, branch=branch, actor=patron.user)
    borrow_work(patron=patron, work=work_b, branch=branch, actor=patron.user)

    recs = social.recommendations_for_work(org, work_a)
    assert any(r["slug"] == "b" for r in recs)


# --------------------------------------------------------------------------- #
# Reading lists
# --------------------------------------------------------------------------- #
def test_reading_list_api():
    org, branch, work, edition, copy = make_catalog()
    p1 = make_patron(org, branch, 1)
    client = _api(p1.user)
    resp = client.post("/api/v1/account/lists/", {"name": "Summer", "public": True}, format="json", secure=True)
    assert resp.status_code == 201
    list_id = resp.json()["data"]["id"]
    resp = client.post(
        f"/api/v1/account/lists/{list_id}/items/", {"work_slug": "dune"}, format="json", secure=True
    )
    assert resp.status_code == 200
    assert ReadingList.objects.get(pk=list_id).works.count() == 1


# --------------------------------------------------------------------------- #
# GDPR
# --------------------------------------------------------------------------- #
def test_export_patron_data():
    org, branch, work, edition, copy = make_catalog()
    patron = make_patron(org, branch, 1)
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    Fee.objects.create(organization=org, patron=patron, fee_type=FeeType.MANUAL, amount_cents=200)
    social.submit_review(patron=patron, work=work, rating=5)

    data = privacy.export_patron_data(patron)
    assert data["profile"]["library_card_number"] == "C1"
    assert len(data["loans"]) == 1
    assert len(data["fees"]) == 1
    assert len(data["reviews"]) == 1


def test_erase_patron_scrubs_and_anonymizes():
    org, branch, work, edition, copy = make_catalog()
    patron = make_patron(org, branch, 1)
    user = patron.user
    patron.retain_loan_history = True  # keep the loan row attached until erasure
    patron.save(update_fields=["retain_loan_history"])
    loan = borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    return_loan(loan=loan, actor=patron.user)
    fee = Fee.objects.create(
        organization=org, patron=patron, fee_type=FeeType.MANUAL, amount_cents=200, status=FeeStatus.PAID
    )
    social.submit_review(patron=patron, work=work, rating=5)
    patron_pk = patron.pk

    privacy.erase_patron(patron=patron, actor=user)
    # Profile + owned data gone; loan + fee ledger retained but anonymized.
    assert not PatronProfile.objects.filter(pk=patron_pk).exists()
    assert not Review.objects.filter(patron_id=patron_pk).exists()
    from library.models import Loan

    loan = Loan.objects.filter(copy=copy).order_by("-id").first()
    assert loan.patron_id is None and loan.patron_hash
    assert loan.status == LoanStatus.RETURNED
    fee.refresh_from_db()
    assert fee.patron_id is None and fee.patron_hash
    # User anonymized + disabled.
    user.refresh_from_db()
    assert user.username.startswith("erased-")
    assert user.email == "" and user.is_active is False


def test_erase_api_requires_confirmation():
    org, branch, work, edition, copy = make_catalog()
    patron = make_patron(org, branch, 1)
    client = _api(patron.user)
    assert client.post("/api/v1/account/erase/", {}, format="json", secure=True).status_code == 400
    resp = client.post("/api/v1/account/erase/", {"confirm": True}, format="json", secure=True)
    assert resp.status_code == 204
    assert not PatronProfile.objects.filter(pk=patron.pk).exists()
