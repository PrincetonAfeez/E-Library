"""Tests for the production-readiness remediation (audit framework pass)."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from library import delivery, ops
from library.api import _reject_upload
from library.models import DigitalAsset, DigitalAssetFormat, Edition, StoredBlob, Work

pytestmark = pytest.mark.django_db(transaction=True)


# --------------------------------------------------------------------------- #
# Request correlation id (U6)
# --------------------------------------------------------------------------- #
def test_request_id_header_present_and_echoed(client):
    resp = client.get("/status/", secure=True)
    assert resp.headers.get("X-Request-ID")
    resp = client.get("/status/", secure=True, HTTP_X_REQUEST_ID="abc-123")
    assert resp.headers.get("X-Request-ID") == "abc-123"


# --------------------------------------------------------------------------- #
# Password reset (AUTH)
# --------------------------------------------------------------------------- #
def test_password_reset_sends_email(client):
    get_user_model().objects.create_user(username="u", email="u@example.test", password="x")
    mail.outbox.clear()
    resp = client.post("/accounts/password_reset/", {"email": "u@example.test"}, secure=True)
    assert resp.status_code == 302
    assert len(mail.outbox) == 1
    assert "/accounts/reset/" in mail.outbox[0].body  # single-use reset link


def test_password_reset_urls_resolve(client):
    assert client.get("/accounts/password_reset/", secure=True).status_code == 200
    assert client.get("/accounts/reset/done/", secure=True).status_code == 200


# --------------------------------------------------------------------------- #
# Legal pages (LAUNCH-legal)
# --------------------------------------------------------------------------- #
def test_terms_and_privacy_pages(client):
    assert client.get("/terms/", secure=True).status_code == 200
    assert client.get("/privacy/", secure=True).status_code == 200


# --------------------------------------------------------------------------- #
# Scheduler lock + dead-letter backlog (BG)
# --------------------------------------------------------------------------- #
def test_scheduler_lock_acquires():
    with ops.scheduler_lock() as acquired:
        assert acquired is True


def test_dead_letter_backlog_reports_counts():
    result = ops.dead_letter_backlog()
    assert set(result) == {"outbox_failed", "webhook_failed"}
    assert result["outbox_failed"] == 0


# --------------------------------------------------------------------------- #
# Orphan blob cleanup (FILE)
# --------------------------------------------------------------------------- #
def test_prune_orphan_blobs_respects_refs_and_grace():
    work = Work.objects.create(canonical_title="W", slug="w")
    edition = Edition.objects.create(work=work, isbn_13="9780000000001")
    delivery.store_blob("keep", b"DATA", content_type="audio/mpeg")
    DigitalAsset.objects.create(
        edition=edition, fmt=DigitalAssetFormat.AUDIO, media_key="keep", byte_size=4
    )
    delivery.store_blob("old-orphan", b"X")
    delivery.store_blob("new-orphan", b"Y")
    # Backdate one orphan past the grace window; leave the other recent.
    StoredBlob.objects.filter(key="old-orphan").update(
        created_at=timezone.now() - timedelta(hours=48)
    )

    pruned = delivery.prune_orphan_blobs(older_than_hours=24)
    assert pruned == 1
    keys = set(StoredBlob.objects.values_list("key", flat=True))
    assert "keep" in keys           # referenced -> kept
    assert "new-orphan" in keys     # within grace -> kept
    assert "old-orphan" not in keys # old + unreferenced -> pruned


# --------------------------------------------------------------------------- #
# Upload type validation (FILE)
# --------------------------------------------------------------------------- #
def test_reject_upload_blocks_wrong_extension():
    bad = SimpleUploadedFile("catalog.exe", b"MZ...", content_type="application/octet-stream")
    assert _reject_upload(bad, (".csv", ".txt")) is not None  # rejected
    good = SimpleUploadedFile("catalog.csv", b"title\nDune", content_type="text/csv")
    assert _reject_upload(good, (".csv", ".txt")) is None  # accepted
