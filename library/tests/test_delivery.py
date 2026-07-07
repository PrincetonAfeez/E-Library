"""Tests for secure digital content delivery (Increment 10): reader, watermark, tokens."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from library import delivery, digital
from library.models import (
    Branch,
    DigitalAsset,
    DigitalAssetFormat,
    DigitalLicense,
    DigitalLoan,
    DigitalLoanStatus,
    Edition,
    LicenseModel,
    Organization,
    PatronProfile,
    ReadingProgress,
    Work,
)
from library.services import DomainError

pytestmark = pytest.mark.django_db(transaction=True)


def make_env(concurrent=1):
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title="Neuromancer", slug="neuromancer")
    edition = Edition.objects.create(work=work, isbn_13="9780000000001", format="ebook")
    lic = DigitalLicense.objects.create(
        organization=org, edition=edition, license_model=LicenseModel.ONE_COPY_ONE_USER,
        concurrent_limit=concurrent, loan_period_days=21,
    )
    return org, branch, work, edition, lic


def make_patron(org, branch, n=1):
    user = get_user_model().objects.create_user(username=f"reader{n}")
    return PatronProfile.objects.create(
        user=user, organization=org, library_card_number=f"C{n}", home_branch=branch
    )


def add_text_asset(edition):
    return DigitalAsset.objects.create(
        edition=edition,
        fmt=DigitalAssetFormat.TEXT,
        title="Neuromancer",
        text_content=[
            {"title": "Chapter 1", "body": "The sky above the port..."},
            {"title": "Chapter 2", "body": "The Villa Straylight..."},
        ],
    )


def add_audio_asset(edition):
    delivery.store_blob("neuro-audio", b"ID3AUDIOBYTES-abcdef", content_type="audio/mpeg")
    return DigitalAsset.objects.create(
        edition=edition, fmt=DigitalAssetFormat.AUDIO, title="Neuromancer (Audio)",
        media_key="neuro-audio", content_type="audio/mpeg", byte_size=20, duration_seconds=3600,
    )


def _api(user):
    client = APIClient(enforce_csrf_checks=False)
    client.force_authenticate(user=user)
    client.defaults["secure"] = True
    return client


# --------------------------------------------------------------------------- #
# Manifest + watermark
# --------------------------------------------------------------------------- #
def test_text_manifest_lists_chapters_with_tokens():
    org, branch, work, edition, lic = make_env()
    add_text_asset(edition)
    patron = make_patron(org, branch)
    loan = digital.borrow_digital(patron=patron, edition=edition, actor=patron.user)

    manifest = delivery.access_manifest(access_token=loan.access_token)
    assert manifest["format"] == "text"
    assert len(manifest["chapters"]) == 2
    assert manifest["chapters"][0]["content_token"]
    assert f"card {patron.library_card_number}" in manifest["watermark"]


def test_content_token_serves_watermarked_chapter():
    org, branch, work, edition, lic = make_env()
    add_text_asset(edition)
    patron = make_patron(org, branch)
    loan = digital.borrow_digital(patron=patron, edition=edition, actor=patron.user)

    token = delivery.mint_content_token(loan, locator="chapter:0")
    resolved, locator = delivery.resolve_content_token(token)
    assert resolved.pk == loan.pk and locator == "chapter:0"

    body, title = delivery.read_text_chapter(loan, 0)
    assert "The sky above the port" in body
    assert "not for redistribution" in body.lower()  # social-DRM watermark
    assert title == "Chapter 1"


def test_expired_loan_token_denied():
    org, branch, work, edition, lic = make_env()
    add_text_asset(edition)
    patron = make_patron(org, branch)
    loan = digital.borrow_digital(patron=patron, edition=edition, actor=patron.user)
    token = delivery.mint_content_token(loan, locator="chapter:0")

    DigitalLoan.objects.filter(pk=loan.pk).update(
        status=DigitalLoanStatus.EXPIRED, expires_at=timezone.now() - timedelta(days=1)
    )
    with pytest.raises(DomainError):
        delivery.resolve_content_token(token)


def test_tampered_token_rejected():
    org, branch, work, edition, lic = make_env()
    add_text_asset(edition)
    patron = make_patron(org, branch)
    loan = digital.borrow_digital(patron=patron, edition=edition, actor=patron.user)
    token = delivery.mint_content_token(loan, locator="chapter:0")
    with pytest.raises(DomainError):
        delivery.resolve_content_token(token + "x")


# --------------------------------------------------------------------------- #
# Progress sync
# --------------------------------------------------------------------------- #
def test_progress_roundtrip_and_resume():
    org, branch, work, edition, lic = make_env()
    add_text_asset(edition)
    patron = make_patron(org, branch)
    loan = digital.borrow_digital(patron=patron, edition=edition, actor=patron.user)

    delivery.save_progress(loan, locator="chapter:1", percent=50)
    assert ReadingProgress.objects.get(loan=loan).locator == "chapter:1"
    # Idempotent update, clamps out-of-range percent.
    delivery.save_progress(loan, locator="chapter:1", percent=250)
    p = ReadingProgress.objects.get(loan=loan)
    assert p.percent == 100.0
    manifest = delivery.access_manifest(access_token=loan.access_token)
    assert manifest["progress"]["locator"] == "chapter:1"


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #
def test_reader_api_and_content_view():
    org, branch, work, edition, lic = make_env()
    add_text_asset(edition)
    patron = make_patron(org, branch)
    loan = digital.borrow_digital(patron=patron, edition=edition, actor=patron.user)
    client = _api(patron.user)

    resp = client.get(f"/api/v1/digital/loans/{loan.pk}/reader/", secure=True)
    assert resp.status_code == 200
    chapter_token = resp.json()["data"]["chapters"][0]["content_token"]

    # The content endpoint is token-gated (no auth needed) and watermarks text.
    anon = APIClient()
    resp = anon.get(f"/digital/content/{chapter_token}/", secure=True)
    assert resp.status_code == 200
    assert b"not for redistribution" in resp.content.lower()

    # Progress sync via API.
    resp = client.post(
        f"/api/v1/digital/loans/{loan.pk}/progress/",
        {"locator": "chapter:1", "percent": 42}, format="json", secure=True,
    )
    assert resp.status_code == 200
    assert ReadingProgress.objects.get(loan=loan).locator == "chapter:1"


def test_audio_range_request():
    org, branch, work, edition, lic = make_env()
    add_audio_asset(edition)
    patron = make_patron(org, branch)
    loan = digital.borrow_digital(patron=patron, edition=edition, actor=patron.user)

    token = delivery.mint_content_token(loan, locator="audio")
    anon = APIClient()
    resp = anon.get(f"/digital/content/{token}/", HTTP_RANGE="bytes=0-3", secure=True)
    assert resp.status_code == 206
    assert resp["Content-Range"].startswith("bytes 0-3/")
    assert resp["X-Content-Watermark"]
    assert len(resp.content) == 4


def test_bad_content_token_forbidden():
    anon = APIClient()
    resp = anon.get("/digital/content/not-a-real-token/", secure=True)
    assert resp.status_code == 403
