"""Regression tests for the audit fixes."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.utils import timezone

from library.models import (
    Author,
    Branch,
    Copy,
    CopyStatus,
    Edition,
    Loan,
    LoanStatus,
    NotificationDelivery,
    Organization,
    OutboxEvent,
    OutboxStatus,
    PatronProfile,
    StaffMembership,
    StaffRole,
    Subject,
    Work,
)
from library.notifications import deliver
from library.permissions import user_is_staff_for_org
from library.services import (
    borrow_work,
    drain_outbox,
    reclaim_stale_outbox_events,
    renew_loan,
    return_loan,
    send_due_soon_notifications,
)

pytestmark = pytest.mark.django_db(transaction=True)


def make_catalog(loan_days=21, max_renewals=2):
    User = get_user_model()
    org = Organization.objects.create(name="Test Library", slug="test")
    branch = Branch.objects.create(
        organization=org, name="Main", slug="main", loan_days=loan_days, max_renewals=max_renewals
    )
    author = Author.objects.create(name="Test Author")
    subject = Subject.objects.create(name="Fiction", slug="fiction")
    work = Work.objects.create(canonical_title="The Test Book", slug="test-book")
    work.authors.add(author)
    work.subjects.add(subject)
    edition = Edition.objects.create(work=work, isbn_13="9780000000001")
    Copy.objects.create(organization=org, edition=edition, branch=branch, barcode="COPY-1")
    user = User.objects.create_user(
        username="reader", password="demo12345", email="reader@example.test"
    )
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number="CARD-1", home_branch=branch
    )
    return org, branch, work, patron


def test_borrow_emits_and_outbox_delivers_notification():
    _org, branch, work, patron = make_catalog()
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)

    # borrow_work wrote an outbox event; draining it should deliver an email
    # and record a truthful NotificationDelivery (C2/F3).
    assert OutboxEvent.objects.filter(
        event_type="loan.borrowed", status=OutboxStatus.PENDING
    ).exists()
    processed = drain_outbox()
    assert processed >= 1

    delivery = NotificationDelivery.objects.get(template_key="loan_borrowed")
    assert delivery.status == "sent"
    assert delivery.recipient == "reader@example.test"
    assert len(mail.outbox) == 1
    assert OutboxEvent.objects.filter(event_type="loan.borrowed").first().status == (
        OutboxStatus.PROCESSED
    )


def test_notification_delivery_is_idempotent():
    _org, branch, work, patron = make_catalog()
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    event = OutboxEvent.objects.get(event_type="loan.borrowed")

    deliver(event)
    deliver(event)  # second delivery must not double-send

    assert NotificationDelivery.objects.filter(template_key="loan_borrowed").count() == 1


def test_due_soon_notifications_are_not_resent():
    _org, branch, work, patron = make_catalog()
    loan = borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    loan.due_at = timezone.now() + timedelta(days=1)
    loan.save(update_fields=["due_at"])

    # Due-soon now emits one outbox event per loan per window (idempotent);
    # the outbox worker performs the actual delivery.
    first = send_due_soon_notifications()
    second = send_due_soon_notifications()
    assert first == 1
    assert second == 0
    from library.services import drain_outbox

    drain_outbox()
    assert NotificationDelivery.objects.filter(template_key="due_soon", status="sent").count() == 1


def test_renew_overdue_loan_extends_from_now():
    _org, branch, work, patron = make_catalog(loan_days=21)
    loan = borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    # Force the loan into the past / overdue.
    loan.due_at = timezone.now() - timedelta(days=5)
    loan.status = LoanStatus.OVERDUE
    loan.save(update_fields=["due_at", "status"])

    renew_loan(loan=loan, actor=patron.user)
    loan.refresh_from_db()
    assert loan.status == LoanStatus.ACTIVE
    # New due date must be in the future (extended from now, not the past due).
    assert loan.due_at > timezone.now() + timedelta(days=20)


def test_renew_limit_uses_branch_setting():
    _org, branch, work, patron = make_catalog(max_renewals=1)
    loan = borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    renew_loan(loan=loan, actor=patron.user)
    loan.refresh_from_db()
    from library.services import DomainError

    with pytest.raises(DomainError):
        renew_loan(loan=loan, actor=patron.user)


def test_reclaim_stale_outbox_events():
    _org, branch, work, patron = make_catalog()
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    event = OutboxEvent.objects.first()
    event.status = OutboxStatus.PROCESSING
    event.save(update_fields=["status"])
    OutboxEvent.objects.filter(pk=event.pk).update(
        updated_at=timezone.now() - timedelta(hours=1)
    )

    reclaimed = reclaim_stale_outbox_events(older_than_minutes=15)
    assert reclaimed == 1
    event.refresh_from_db()
    assert event.status == OutboxStatus.PENDING


def test_staff_authorization_is_org_scoped():
    org, branch, _work, _patron = make_catalog()
    other_org = Organization.objects.create(name="Other", slug="other")
    User = get_user_model()
    librarian = User.objects.create_user(username="liv", password="demo12345", is_staff=True)
    StaffMembership.objects.create(
        user=librarian, organization=org, branch=branch, role=StaffRole.LIBRARIAN
    )

    assert user_is_staff_for_org(librarian, org) is True
    # is_staff flag must NOT grant access to a different tenant.
    assert user_is_staff_for_org(librarian, other_org) is False


def test_returned_loan_scrubs_patron_and_hashes():
    _org, branch, work, patron = make_catalog()
    loan = borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    returned = return_loan(loan=loan, actor=patron.user)
    assert returned.status == LoanStatus.RETURNED
    assert returned.patron is None
    assert returned.patron_hash
    copy = Copy.objects.get(barcode="COPY-1")
    assert copy.status == CopyStatus.AVAILABLE
    assert Loan.objects.get(pk=loan.pk).returned_at is not None
