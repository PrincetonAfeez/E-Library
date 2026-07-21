"""Digital/e-content lending: license-based loans, holds, expiry, and access.
 
Parallels physical circulation but availability comes from license models
(one-copy-one-user, metered checkouts/time, simultaneous use) rather than copies.
Digital loans auto-expire (no physical return), free a concurrent slot, and
promote the next waiting hold.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import entitlements
from .models import (
    DigitalHold,
    DigitalHoldStatus,
    DigitalLicense,
    DigitalLoan,
    DigitalLoanStatus,
    Edition,
    LicenseModel,
    PatronProfile,
    stable_patron_hash,
)
from .services import (
    DomainError,
    advisory_xact_lock,
    assert_patron_can_act,
    audit_action,
    emit_domain_event,
)

READY_HOLD_HOURS = 72


def _active_loan_count(license) -> int:
    return DigitalLoan.objects.filter(license=license, status=DigitalLoanStatus.ACTIVE).count()


def license_is_available(license, *, now=None) -> bool:
    now = now or timezone.now()
    if not license.active:
        return False
    if (
        license.license_model == LicenseModel.METERED_TIME
        and license.expires_at is not None
        and license.expires_at <= now
    ):
        return False
    if (
        license.license_model == LicenseModel.METERED_CHECKOUTS
        and license.checkouts_allowed is not None
        and license.checkouts_used >= license.checkouts_allowed
    ):
        return False
    if license.concurrent_limit is None:  # simultaneous-use, unlimited
        return True
    return _active_loan_count(license) < license.concurrent_limit


def available_slots_for_edition(edition, organization, *, now=None) -> int:
    """Total borrowable slots across a tenant's licenses for an edition
    (large number == unlimited)."""
    now = now or timezone.now()
    total = 0
    for license in DigitalLicense.objects.filter(
        organization=organization, edition=edition, active=True
    ):
        if not license_is_available(license, now=now):
            continue
        if license.concurrent_limit is None:
            return 1_000_000
        total += max(0, license.concurrent_limit - _active_loan_count(license))
    return total


def _pick_available_license(edition, organization, *, now):
    for license in (
        DigitalLicense.objects.select_for_update()
        .filter(organization=organization, edition=edition, active=True)
        .order_by("id")
    ):
        if license_is_available(license, now=now):
            return license
    return None


def borrow_digital(*, patron: PatronProfile, edition: Edition, actor=None, source: str = "web") -> DigitalLoan:
    assert_patron_can_act(patron)
    entitlements.assert_feature(patron.organization, "digital")
    organization = patron.organization
    now = timezone.now()
    with transaction.atomic():
        PatronProfile.objects.select_for_update().get(pk=patron.pk)
        advisory_xact_lock(76, edition.pk)

        if DigitalLoan.objects.filter(
            patron=patron, license__edition=edition, status=DigitalLoanStatus.ACTIVE
        ).exists():
            raise DomainError("You already have this title checked out.")

        ready_hold = (
            DigitalHold.objects.select_for_update(of=("self",))
            .filter(patron=patron, edition=edition, status=DigitalHoldStatus.READY)
            .first()
        )
        if ready_hold is None and DigitalHold.objects.filter(
            organization=organization, edition=edition, status=DigitalHoldStatus.WAITING
        ).exists():
            raise DomainError("A hold queue exists for this title.")

        license = _pick_available_license(edition, organization, now=now)
        if license is None:
            raise DomainError("No digital licenses are available.")

        loan = DigitalLoan.objects.create(
            organization=organization,
            license=license,
            patron=patron,
            expires_at=now + timedelta(days=license.loan_period_days),
            access_token=secrets.token_urlsafe(32),
        )
        if license.license_model == LicenseModel.METERED_CHECKOUTS:
            license.checkouts_used += 1
            license.save(update_fields=["checkouts_used", "updated_at"])
        if ready_hold is not None:
            ready_hold.status = DigitalHoldStatus.FULFILLED
            ready_hold.save(update_fields=["status", "updated_at"])

        audit_action(action="digital.borrow", entity=loan, actor=actor, source=source)
        emit_domain_event(
            event_type="digital.borrowed",
            aggregate=loan,
            payload={"edition_id": edition.pk, "patron_id": patron.pk},
            actor=actor,
            source=source,
        )
        return loan


def _assign_next_digital_hold(*, edition, organization, actor=None, source: str = "system"):
    """Promote the earliest waiting hold to READY when a slot is free."""
    if available_slots_for_edition(edition, organization) <= 0:
        return None
    next_hold = (
        DigitalHold.objects.select_for_update(of=("self",))
        .filter(organization=organization, edition=edition, status=DigitalHoldStatus.WAITING)
        .order_by("created_at", "id")
        .first()
    )
    if next_hold is None:
        return None
    next_hold.status = DigitalHoldStatus.READY
    next_hold.ready_at = timezone.now()
    next_hold.expires_at = timezone.now() + timedelta(hours=READY_HOLD_HOURS)
    next_hold.save(update_fields=["status", "ready_at", "expires_at", "updated_at"])
    emit_domain_event(
        event_type="digital.hold_ready",
        aggregate=next_hold,
        payload={"edition_id": edition.pk, "patron_id": next_hold.patron_id},
        actor=actor,
        source=source,
    )
    return next_hold


def _close_digital_loan(loan, *, status, actor, source):
    patron = loan.patron
    loan.status = status
    loan.returned_at = timezone.now()
    loan.patron_hash = stable_patron_hash(patron)
    # Invalidate the durable bearer so leaked tokens cannot mint manifests.
    loan.access_token = f"revoked-{secrets.token_urlsafe(24)}"
    if patron is not None and not patron.retain_loan_history:
        loan.patron = None
    loan.save(
        update_fields=[
            "status",
            "returned_at",
            "patron_hash",
            "patron",
            "access_token",
            "updated_at",
        ]
    )
    _assign_next_digital_hold(
        edition=loan.license.edition, organization=loan.organization, actor=actor, source=source
    )


def return_digital(*, loan: DigitalLoan, actor=None, source: str = "web") -> DigitalLoan:
    with transaction.atomic():
        loan = DigitalLoan.objects.select_for_update(of=("self",)).select_related(
            "license__edition", "patron"
        ).get(pk=loan.pk)
        if loan.status != DigitalLoanStatus.ACTIVE:
            raise DomainError("Only active digital loans can be returned.")
        advisory_xact_lock(76, loan.license.edition_id)
        _close_digital_loan(loan, status=DigitalLoanStatus.RETURNED, actor=actor, source=source)
        audit_action(action="digital.return", entity=loan, actor=actor, source=source)
        emit_domain_event(
            event_type="digital.returned", aggregate=loan, payload={}, actor=actor, source=source
        )
        return loan


def expire_digital_loans(*, now=None) -> int:
    now = now or timezone.now()
    count = 0
    for loan_id in DigitalLoan.objects.filter(
        status=DigitalLoanStatus.ACTIVE, expires_at__lte=now
    ).values_list("id", flat=True):
        with transaction.atomic():
            loan = DigitalLoan.objects.select_for_update(of=("self",)).select_related(
                "license__edition", "patron"
            ).get(pk=loan_id)
            if loan.status != DigitalLoanStatus.ACTIVE:
                continue
            advisory_xact_lock(76, loan.license.edition_id)
            _close_digital_loan(loan, status=DigitalLoanStatus.EXPIRED, actor=None, source="scheduler")
            count += 1
    return count


def place_digital_hold(*, patron: PatronProfile, edition: Edition, actor=None, source: str = "web") -> DigitalHold:
    assert_patron_can_act(patron)
    entitlements.assert_feature(patron.organization, "digital")
    organization = patron.organization
    with transaction.atomic():
        PatronProfile.objects.select_for_update().get(pk=patron.pk)
        advisory_xact_lock(76, edition.pk)
        if DigitalHold.objects.filter(
            organization=organization,
            patron=patron,
            edition=edition,
            status__in=[DigitalHoldStatus.WAITING, DigitalHoldStatus.READY],
        ).exists():
            raise DomainError("You already have a hold on this title.")
        if DigitalLoan.objects.filter(
            patron=patron, license__edition=edition, status=DigitalLoanStatus.ACTIVE
        ).exists():
            raise DomainError("You already have this title checked out.")
        hold = DigitalHold.objects.create(
            organization=organization, edition=edition, patron=patron, status=DigitalHoldStatus.WAITING
        )
        # If a slot is free and no one is ahead, make it ready immediately.
        ahead = (
            DigitalHold.objects.filter(
                organization=organization, edition=edition, status=DigitalHoldStatus.WAITING
            )
            .exclude(pk=hold.pk)
            .exists()
        )
        if not ahead and available_slots_for_edition(edition, organization) > 0:
            hold.status = DigitalHoldStatus.READY
            hold.ready_at = timezone.now()
            hold.expires_at = timezone.now() + timedelta(hours=READY_HOLD_HOURS)
            hold.save(update_fields=["status", "ready_at", "expires_at", "updated_at"])
        audit_action(action="digital.hold", entity=hold, actor=actor, source=source)
        emit_domain_event(
            event_type="digital.hold_placed",
            aggregate=hold,
            payload={"edition_id": edition.pk, "patron_id": patron.pk, "status": hold.status},
            actor=actor,
            source=source,
        )
        return hold


def cancel_digital_hold(*, hold: DigitalHold, actor=None, source: str = "web") -> DigitalHold:
    with transaction.atomic():
        hold = DigitalHold.objects.select_for_update(of=("self",)).select_related("edition").get(pk=hold.pk)
        if hold.status not in [DigitalHoldStatus.WAITING, DigitalHoldStatus.READY]:
            raise DomainError("Only active holds can be cancelled.")
        was_ready = hold.status == DigitalHoldStatus.READY
        hold.status = DigitalHoldStatus.CANCELLED
        hold.save(update_fields=["status", "updated_at"])
        if was_ready:
            advisory_xact_lock(76, hold.edition_id)
            _assign_next_digital_hold(
                edition=hold.edition, organization=hold.organization, actor=actor, source=source
            )
        audit_action(action="digital.hold_cancel", entity=hold, actor=actor, source=source)
        return hold


def expire_digital_ready_holds(*, now=None) -> int:
    now = now or timezone.now()
    count = 0
    for hold_id in DigitalHold.objects.filter(
        status=DigitalHoldStatus.READY, expires_at__lte=now
    ).values_list("id", flat=True):
        with transaction.atomic():
            hold = DigitalHold.objects.select_for_update(of=("self",)).select_related("edition").get(pk=hold_id)
            if hold.status != DigitalHoldStatus.READY:
                continue
            hold.status = DigitalHoldStatus.EXPIRED
            hold.save(update_fields=["status", "updated_at"])
            advisory_xact_lock(76, hold.edition_id)
            _assign_next_digital_hold(
                edition=hold.edition, organization=hold.organization, source="scheduler"
            )
            count += 1
    return count


def access_content(*, access_token: str) -> dict:
    """Validate a loan's access token and return a secure reading manifest."""
    from . import delivery

    return delivery.access_manifest(access_token=access_token)
