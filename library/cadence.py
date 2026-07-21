"""Notification cadences: escalating overdue reminders and hold-expiry notices.
 
Each sweep emits a domain event (delivered asynchronously by the outbox worker,
so no SMTP runs on the sweep) and is idempotent: a given loan/hold fires at most
once per cadence stage. Preference/compliance filtering happens at delivery time
in :mod:`library.notifications`.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from .models import DomainEvent, Hold, HoldStatus, Loan, LoanStatus
from .services import emit_domain_event

# Days-overdue thresholds at which an escalating reminder is sent.
OVERDUE_STAGES = (1, 7, 14, 30)
# How far ahead of a ready-hold's expiry to send a pickup reminder.
HOLD_EXPIRY_HOURS_BEFORE = 24


def _patron_email(patron) -> str:
    return patron.notification_email or patron.user.email


def send_overdue_reminders(*, now=None, stages=OVERDUE_STAGES) -> int:
    """Emit an escalating reminder per overdue loan at each day-past-due stage."""
    now = now or timezone.now()
    emitted = 0
    loans = Loan.objects.select_related(
        "patron__user", "organization", "copy__edition__work", "copy__branch"
    ).filter(status=LoanStatus.OVERDUE, patron__isnull=False)
    for loan in loans:
        days_over = (now - loan.due_at).days
        stage = max((s for s in stages if s <= days_over), default=None)
        if stage is None:
            continue
        email = _patron_email(loan.patron)
        if not email:
            continue
        # One reminder per (loan, stage) — later stages still fire as they arrive.
        if DomainEvent.objects.filter(
            event_type="loan.overdue_reminder",
            aggregate_type="Loan",
            aggregate_id=str(loan.pk),
            payload__stage=stage,
        ).exists():
            continue
        emit_domain_event(
            event_type="loan.overdue_reminder",
            aggregate=loan,
            payload={"stage": stage, "due_at": loan.due_at.isoformat()},
            outbox_payload={
                "recipient": email,
                "title": loan.copy.edition.work.canonical_title,
            },
            source="scheduler",
        )
        emitted += 1
    return emitted


def send_hold_expiry_reminders(*, now=None, hours_before=HOLD_EXPIRY_HOURS_BEFORE) -> int:
    """Remind patrons to collect a ready hold shortly before it expires."""
    now = now or timezone.now()
    until = now + timedelta(hours=hours_before)
    emitted = 0
    holds = Hold.objects.select_related(
        "patron__user", "work", "preferred_branch"
    ).filter(
        status=HoldStatus.READY,
        patron__isnull=False,
        expires_at__isnull=False,
        expires_at__range=(now, until),
    )
    for hold in holds:
        email = _patron_email(hold.patron)
        if not email:
            continue
        if DomainEvent.objects.filter(
            event_type="hold.expiring_soon",
            aggregate_type="Hold",
            aggregate_id=str(hold.pk),
        ).exists():
            continue
        emit_domain_event(
            event_type="hold.expiring_soon",
            aggregate=hold,
            payload={"expires_at": hold.expires_at.isoformat()},
            outbox_payload={
                "recipient": email,
                "title": hold.work.canonical_title,
            },
            source="scheduler",
        )
        emitted += 1
    return emitted
