"""Tests for staff MFA (Increment 17) and the AI assistant (Increment 18)."""

import time

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from library import assistant, mfa
from library.models import (
    Branch,
    Copy,
    Edition,
    Loan,
    LoanStatus,
    Organization,
    PatronProfile,
    StaffTotpDevice,
    Work,
)
from library.services import DomainError, rebuild_work_search_document

pytestmark = pytest.mark.django_db(transaction=True)


# --------------------------------------------------------------------------- #
# MFA / TOTP
# --------------------------------------------------------------------------- #
def test_totp_verifies_current_code():
    secret = mfa.generate_secret()
    now = 1_700_000_000
    code = mfa.totp(secret, timestamp=now)
    assert mfa.verify_code(secret, code, timestamp=now)
    # A code from far in the past no longer verifies.
    assert not mfa.verify_code(secret, code, timestamp=now + 600)
    assert not mfa.verify_code(secret, "000000", timestamp=now + 1)


def test_enrollment_confirm_and_login():
    user = get_user_model().objects.create_user(username="staff")
    info = mfa.begin_enrollment(user=user)
    assert info["secret"] and info["otpauth_uri"].startswith("otpauth://totp/")
    assert not mfa.user_has_mfa(user)

    with pytest.raises(DomainError):
        mfa.confirm_enrollment(user=user, code="000000")

    code = mfa.totp(info["secret"], timestamp=time.time())
    mfa.confirm_enrollment(user=user, code=code)
    assert mfa.user_has_mfa(user)
    assert StaffTotpDevice.objects.get(user=user).confirmed

    assert mfa.verify_login(user=user, code=mfa.totp(info["secret"], timestamp=time.time()))


def test_mfa_api_roundtrip():
    user = get_user_model().objects.create_user(username="staff2")
    client = APIClient()
    client.force_authenticate(user=user)
    client.defaults["secure"] = True
    resp = client.post("/api/v1/account/mfa/enroll/", {}, format="json", secure=True)
    assert resp.status_code == 201
    secret = resp.json()["data"]["secret"]
    code = mfa.totp(secret, timestamp=time.time())
    resp = client.post("/api/v1/account/mfa/confirm/", {"code": code}, format="json", secure=True)
    assert resp.status_code == 200 and resp.json()["data"]["confirmed"] is True


# --------------------------------------------------------------------------- #
# AI assistant
# --------------------------------------------------------------------------- #
def _catalog():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    user = get_user_model().objects.create_user(username="reader", email="r@x.test")
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number="C1", home_branch=branch
    )
    return org, branch, patron


def _add(org, branch, title, slug, summary, isbn):
    work = Work.objects.create(canonical_title=title, slug=slug, summary=summary)
    edition = Edition.objects.create(work=work, isbn_13=isbn)
    copy = Copy.objects.create(organization=org, edition=edition, branch=branch, barcode=f"B{slug}")
    rebuild_work_search_document(work.pk)
    return work, edition, copy


def test_recommendations_from_history():
    org, branch, patron = _catalog()
    cy1, e1, c1 = _add(org, branch, "Neuromancer", "neuro",
                       "A cyberpunk hacker in cyberspace.", "9780000000001")
    cy2, e2, c2 = _add(org, branch, "Snow Crash", "snow",
                       "A cyberpunk hacker in the metaverse.", "9780000000002")
    _add(org, branch, "Garden Guide", "garden",
         "How to grow vegetables in your garden.", "9780000000003")
    # Patron has read one cyberpunk title.
    Loan.objects.create(
        organization=org, copy=c1, patron=patron,
        due_at="2026-01-01T00:00:00Z", status=LoanStatus.RETURNED, returned_at="2026-01-02T00:00:00Z",
    )
    recs = assistant.recommend_for_patron(patron, limit=3)
    titles = [w.canonical_title for w in recs]
    assert "Snow Crash" in titles  # semantically closest to what they read
    assert "Neuromancer" not in titles  # never recommend an already-read title


def test_catalog_assist_extracts_metadata():
    result = assistant.catalog_assist(
        text="Cyberpunk hackers navigate a dystopian metaverse. The metaverse is vast."
    )
    assert "metaverse" in result["keywords"]
    assert result["summary"]
    assert result["reading_level"] > 0


def test_parse_nl_query():
    parsed = assistant.parse_query("show me available cyberpunk books")
    assert parsed["filters"].get("availability") == "available"
    assert "cyberpunk" in parsed["q"]
    assert "available" not in parsed["q"] and "books" not in parsed["q"]


# --------------------------------------------------------------------------- #
# Status page (Increment 17)
# --------------------------------------------------------------------------- #
def test_status_page_ok(client):
    resp = client.get("/status/", secure=True)
    assert resp.status_code == 200
    assert b"operational" in resp.content.lower()
