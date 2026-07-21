"""GDPR/CCPA tooling: patron data export (portability) and erasure (right to be
forgotten)."""

from __future__ import annotations

import secrets

from django.db import transaction

from .models import FeeStatus, LoanStatus
from .services import DomainError, audit_action, emit_domain_event, stable_patron_hash


def export_patron_data(patron) -> dict:
    """A portable snapshot of everything the library holds about a patron."""
    user = patron.user
    return {
        "profile": {
            "username": user.get_username(),
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "library_card_number": patron.library_card_number,
            "organization": patron.organization.slug,
            "notification_email": patron.notification_email,
            "sms_number": patron.sms_number,
            "notification_channels": patron.notification_channels,
            "retain_loan_history": patron.retain_loan_history,
        },
        "loans": [
            {
                "title": loan.copy.edition.work.canonical_title,
                "borrowed_at": loan.borrowed_at.isoformat(),
                "due_at": loan.due_at.isoformat(),
                "returned_at": loan.returned_at.isoformat() if loan.returned_at else None,
                "status": loan.status,
            }
            for loan in patron.loans.select_related("copy__edition__work")
        ],
        "holds": [
            {"title": h.work.canonical_title, "status": h.status, "created_at": h.created_at.isoformat()}
            for h in patron.holds.select_related("work")
        ],
        "digital_loans": [
            {
                "title": dl.license.edition.work.canonical_title,
                "started_at": dl.started_at.isoformat(),
                "expires_at": dl.expires_at.isoformat(),
                "status": dl.status,
            }
            for dl in patron.digital_loans.select_related("license__edition__work")
        ],
        "fees": [
            {
                "type": f.fee_type,
                "amount_cents": f.amount_cents,
                "paid_cents": f.paid_cents,
                "status": f.status,
                "created_at": f.created_at.isoformat(),
            }
            for f in patron.fees.all()
        ],
        "payments": [
            {"amount_cents": p.amount_cents, "method": p.method, "created_at": p.created_at.isoformat()}
            for p in patron.payments.all()
        ],
        "reviews": [
            {"work": r.work.canonical_title, "rating": r.rating, "body": r.body}
            for r in patron.reviews.select_related("work")
        ],
        "reading_lists": [
            {"name": rl.name, "public": rl.public, "works": [w.canonical_title for w in rl.works.all()]}
            for rl in patron.reading_lists.prefetch_related("works")
        ],
    }


@transaction.atomic
def erase_patron(*, patron, actor=None) -> None:
    """Right to be forgotten: anonymize retained ledger rows, delete PII, scrub login."""
    from .models import (
        DigitalHold,
        DigitalLoan,
        DigitalLoanStatus,
        EventRegistration,
        Fee,
        Hold,
        IllRequest,
        IllStatus,
        Loan,
        NotificationDelivery,
        Payment,
        PaymentPlan,
        PatronProfile,
        RegistrationStatus,
        Review,
        RoomReservation,
        ReservationStatus,
        ScopedApiToken,
        SsoIdentity,
        StaffMembership,
        StaffTotpDevice,
    )

    if Loan.objects.filter(
        patron=patron, status__in=[LoanStatus.ACTIVE, LoanStatus.OVERDUE]
    ).exists():
        raise DomainError("Return all loans before erasing the account.")
    if DigitalLoan.objects.filter(
        patron=patron, status=DigitalLoanStatus.ACTIVE
    ).exists():
        raise DomainError("Return all digital loans before erasing the account.")
    if Fee.objects.filter(patron=patron, status=FeeStatus.OUTSTANDING).exists():
        raise DomainError("Settle outstanding fees before erasing the account.")
    if IllRequest.objects.filter(requesting_patron=patron).exclude(
        status__in=[IllStatus.COMPLETED, IllStatus.CANCELLED]
    ).exists():
        raise DomainError("Resolve active inter-library loan requests before erasing the account.")

    user = patron.user
    scrub_hash = stable_patron_hash(patron)
    emails = {
        e
        for e in (
            user.email,
            patron.notification_email,
        )
        if e
    }

    for loan in patron.loans.all():
        loan.patron_hash = scrub_hash
        loan.patron = None
        loan.save(update_fields=["patron", "patron_hash", "updated_at"])
    for dloan in patron.digital_loans.all():
        dloan.patron_hash = scrub_hash
        dloan.patron = None
        dloan.access_token = f"revoked-{secrets.token_urlsafe(16)}"
        dloan.save(update_fields=["patron", "patron_hash", "access_token", "updated_at"])

    for fee in Fee.objects.filter(patron=patron):
        fee.patron_hash = scrub_hash
        fee.patron = None
        fee.save(update_fields=["patron", "patron_hash", "updated_at"])
    for payment in Payment.objects.filter(patron=patron):
        payment.patron_hash = scrub_hash
        payment.patron = None
        payment.save(update_fields=["patron", "patron_hash", "updated_at"])
    for plan in PaymentPlan.objects.filter(patron=patron):
        plan.patron_hash = scrub_hash
        plan.patron = None
        plan.save(update_fields=["patron", "patron_hash", "updated_at"])
    IllRequest.objects.filter(requesting_patron=patron).update(
        requesting_patron=None, patron_hash=scrub_hash
    )

    # Scrub notification recipients that could re-identify the person.
    if emails:
        NotificationDelivery.objects.filter(
            organization=patron.organization, recipient__in=list(emails)
        ).update(recipient="erased")

    from django.utils import timezone

    # Revoke only this organization's access. Other organizations can continue
    # to use the shared account.
    SsoIdentity.objects.filter(user=user, connection__organization=patron.organization).delete()
    StaffMembership.objects.filter(
        user=user, organization=patron.organization, active=True
    ).update(active=False)
    ScopedApiToken.objects.filter(
        user=user, organization=patron.organization, revoked_at__isnull=True
    ).update(
        revoked_at=timezone.now()
    )
    has_other_staff = StaffMembership.objects.filter(user=user, active=True).exists()
    has_other_patron = PatronProfile.objects.filter(user=user).exclude(pk=patron.pk).exists()
    if not has_other_staff:
        StaffTotpDevice.objects.filter(user=user).delete()

    audit_action(action="patron.erase", entity=patron, actor=actor, source="privacy")
    emit_domain_event(
        event_type="patron.erased",
        aggregate=patron,
        payload={"organization": patron.organization.slug},
        actor=actor,
        source="privacy",
    )

    # Use the domain cancellation operations so a cancelled registration can
    # promote the event waitlist.
    from .events import cancel_registration, cancel_reservation

    for registration in EventRegistration.objects.filter(
        patron=patron, status__in=[RegistrationStatus.REGISTERED, RegistrationStatus.WAITLISTED]
    ):
        cancel_registration(registration=registration, actor=actor)
    for reservation in RoomReservation.objects.filter(
        patron=patron, status=ReservationStatus.BOOKED
    ):
        cancel_reservation(reservation=reservation, actor=actor)
    Hold.objects.filter(patron=patron).delete()
    DigitalHold.objects.filter(patron=patron).delete()
    Review.objects.filter(patron=patron).delete()
    patron.reading_lists.all().delete()
    patron.delete()

    if not has_other_patron and not has_other_staff:
        user.username = f"erased-{secrets.token_hex(8)}"
        user.email = ""
        user.first_name = ""
        user.last_name = ""
        user.set_unusable_password()
        user.is_active = False
        user.save()
