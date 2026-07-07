"""Tests for notification cadences & compliance (Increment 13)."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.utils import timezone
from rest_framework.test import APIClient

from library import cadence, notifications
from library.models import (
    Branch,
    Copy,
    DomainEvent,
    Edition,
    Hold,
    HoldStatus,
    Loan,
    LoanStatus,
    Organization,
    PatronProfile,
    Work,
)
from library.services import borrow_work, drain_outbox, send_due_soon_notifications

pytestmark = pytest.mark.django_db(transaction=True)


def make_catalog():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title="Dune", slug="dune")
    edition = Edition.objects.create(work=work, isbn_13="9780000000001")
    copy = Copy.objects.create(organization=org, edition=edition, branch=branch, barcode="C1")
    user = get_user_model().objects.create_user(username="reader", email="r@example.test")
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number="C1", home_branch=branch
    )
    return org, branch, work, edition, copy, patron


def make_overdue_loan(org, copy, patron, *, days_over=8):
    return Loan.objects.create(
        organization=org, copy=copy, patron=patron,
        due_at=timezone.now() - timedelta(days=days_over), status=LoanStatus.OVERDUE,
    )


def _api(user):
    client = APIClient(enforce_csrf_checks=False)
    client.force_authenticate(user=user)
    client.defaults["secure"] = True
    return client


# --------------------------------------------------------------------------- #
# Escalating overdue reminders
# --------------------------------------------------------------------------- #
def test_overdue_reminder_fires_at_stage_and_is_idempotent():
    org, branch, work, edition, copy, patron = make_catalog()
    make_overdue_loan(org, copy, patron, days_over=8)
    mail.outbox.clear()

    assert cadence.send_overdue_reminders() == 1
    # Re-running the same day does not re-emit the same stage.
    assert cadence.send_overdue_reminders() == 0
    assert (
        DomainEvent.objects.filter(event_type="loan.overdue_reminder").count() == 1
    )
    drain_outbox()
    assert len(mail.outbox) == 1
    assert "7 day(s) overdue" in mail.outbox[0].subject


def test_overdue_reminder_escalates_to_next_stage():
    org, branch, work, edition, copy, patron = make_catalog()
    loan = make_overdue_loan(org, copy, patron, days_over=8)
    cadence.send_overdue_reminders()  # stage 7
    # Time passes; the loan is now far more overdue.
    Loan.objects.filter(pk=loan.pk).update(due_at=timezone.now() - timedelta(days=31))
    assert cadence.send_overdue_reminders() == 1  # stage 30 fires
    stages = set(
        DomainEvent.objects.filter(event_type="loan.overdue_reminder").values_list(
            "payload__stage", flat=True
        )
    )
    assert stages == {7, 30}


# --------------------------------------------------------------------------- #
# Hold-expiry reminders
# --------------------------------------------------------------------------- #
def test_hold_expiry_reminder():
    org, branch, work, edition, copy, patron = make_catalog()
    Hold.objects.create(
        organization=org, work=work, patron=patron, preferred_branch=branch,
        status=HoldStatus.READY, assigned_copy=copy, ready_at=timezone.now(),
        expires_at=timezone.now() + timedelta(hours=12),
    )
    mail.outbox.clear()
    assert cadence.send_hold_expiry_reminders() == 1
    assert cadence.send_hold_expiry_reminders() == 0  # idempotent
    drain_outbox()
    assert len(mail.outbox) == 1
    assert "Pick up soon" in mail.outbox[0].subject


# --------------------------------------------------------------------------- #
# Preferences & compliance
# --------------------------------------------------------------------------- #
def test_courtesy_suppressed_by_preference():
    org, branch, work, edition, copy, patron = make_catalog()
    patron.notification_prefs = {"courtesy": False}
    patron.save(update_fields=["notification_prefs"])
    mail.outbox.clear()
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    drain_outbox()
    assert len(mail.outbox) == 0  # loan.borrowed is a suppressed courtesy notice


def test_essential_delivered_even_when_unsubscribed():
    org, branch, work, edition, copy, patron = make_catalog()
    patron.unsubscribed_at = timezone.now()
    patron.save(update_fields=["unsubscribed_at"])
    make_overdue_loan(org, copy, patron, days_over=8)
    mail.outbox.clear()
    cadence.send_overdue_reminders()
    drain_outbox()
    assert len(mail.outbox) == 1  # overdue is essential/transactional


def test_courtesy_suppressed_when_unsubscribed():
    org, branch, work, edition, copy, patron = make_catalog()
    patron.unsubscribed_at = timezone.now()
    patron.save(update_fields=["unsubscribed_at"])
    Loan.objects.create(
        organization=org, copy=copy, patron=patron,
        due_at=timezone.now() + timedelta(days=2), status=LoanStatus.ACTIVE,
    )
    mail.outbox.clear()
    send_due_soon_notifications()
    drain_outbox()
    assert len(mail.outbox) == 0


def test_courtesy_email_has_unsubscribe_footer():
    org, branch, work, edition, copy, patron = make_catalog()
    mail.outbox.clear()
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    drain_outbox()
    assert len(mail.outbox) == 1
    assert "/u/" in mail.outbox[0].body  # one-click unsubscribe link


def test_essential_email_has_no_unsubscribe_footer():
    org, branch, work, edition, copy, patron = make_catalog()
    make_overdue_loan(org, copy, patron, days_over=8)
    mail.outbox.clear()
    cadence.send_overdue_reminders()
    drain_outbox()
    assert len(mail.outbox) == 1
    assert "/u/" not in mail.outbox[0].body


# --------------------------------------------------------------------------- #
# Unsubscribe endpoint + preferences API
# --------------------------------------------------------------------------- #
def test_unsubscribe_view_roundtrip(client):
    org, branch, work, edition, copy, patron = make_catalog()
    token = notifications.ensure_unsubscribe_token(patron)

    # A GET must NOT mutate (link scanners / prefetch).
    resp = client.get(f"/u/{token}/", secure=True)
    assert resp.status_code == 200
    patron.refresh_from_db()
    assert patron.unsubscribed_at is None

    resp = client.post(f"/u/{token}/", {"action": "unsubscribe"}, secure=True)
    assert resp.status_code == 200
    patron.refresh_from_db()
    assert patron.unsubscribed_at is not None

    resp = client.post(f"/u/{token}/", {"action": "resubscribe"}, secure=True)
    assert resp.status_code == 200
    patron.refresh_from_db()
    assert patron.unsubscribed_at is None


def test_notification_prefs_api():
    org, branch, work, edition, copy, patron = make_catalog()
    client = _api(patron.user)
    resp = client.post(
        "/api/v1/account/notifications/",
        {"preferences": {"courtesy": False}, "channels": ["email", "sms"], "unsubscribed": True},
        format="json", secure=True,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["preferences"] == {"courtesy": False}
    assert data["channels"] == ["email", "sms"]
    assert data["unsubscribed"] is True
    patron.refresh_from_db()
    assert patron.unsubscribed_at is not None
