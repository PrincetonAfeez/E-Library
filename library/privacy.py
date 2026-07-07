"""GDPR/CCPA tooling: patron data export (portability) and erasure (right to be
forgotten)."""

from __future__ import annotations

import secrets

from django.db import transaction

from .services import audit_action, emit_domain_event, stable_patron_hash


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
    """Right to be forgotten: anonymize circulation records that must be retained
    for statistics, delete owned personal data, and scrub the login account."""
    user = patron.user
    scrub_hash = stable_patron_hash(patron)

    # Anonymize loans/digital loans (retained, patron detached).
    for loan in patron.loans.all():
        loan.patron_hash = scrub_hash
        loan.patron = None
        loan.save(update_fields=["patron", "patron_hash", "updated_at"])
    for dloan in patron.digital_loans.all():
        dloan.patron_hash = scrub_hash
        dloan.patron = None
        dloan.save(update_fields=["patron", "patron_hash", "updated_at"])

    audit_action(action="patron.erase", entity=patron, actor=actor, source="privacy")
    emit_domain_event(
        event_type="patron.erased",
        aggregate=patron,
        payload={"organization": patron.organization.slug},
        actor=actor,
        source="privacy",
    )

    # Delete owned personal data (holds, fees, payments, reviews, lists, digital
    # holds all cascade from the profile).
    patron.delete()

    # Scrub the login account so no PII remains, and disable it.
    user.username = f"erased-{secrets.token_hex(8)}"
    user.email = ""
    user.first_name = ""
    user.last_name = ""
    user.set_unusable_password()
    user.is_active = False
    user.save()
