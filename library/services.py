from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.postgres.search import SearchVector
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from . import entitlements, policies
from .models import (
    AuditLog,
    Copy,
    CopyMovement,
    CopyStatus,
    DomainEvent,
    Edition,
    Fee,
    FeePolicy,
    FeeStatus,
    FeeType,
    Hold,
    HoldStatus,
    LibrarianOverride,
    Loan,
    LoanStatus,
    OutboxEvent,
    OutboxStatus,
    PatronProfile,
    PatronStatus,
    Payment,
    PaymentAllocation,
    PublicStatus,
    Renewal,
    Work,
    WorkSearchDocument,
    stable_patron_hash,
)
from .notifications import deliver as deliver_notification

logger = logging.getLogger("library")


class DomainError(ValueError):
    pass


@dataclass(frozen=True)
class ServiceResult:
    message: str
    entity: object | None = None


def advisory_xact_lock(namespace: int, key: int) -> None:
    if connection.vendor != "postgresql":
        return
    # Use the single-bigint form: the two-int4 form would raise "integer out of
    # range" once key (a BigAutoField pk) exceeds 2**31. Fold namespace + key
    # into a 63-bit lock id.
    lock_id = (namespace * 1_000_000_007 + key) & 0x7FFFFFFFFFFFFFFF
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_id])


def emit_domain_event(
    *,
    event_type: str,
    aggregate,
    payload: dict | None = None,
    actor=None,
    source: str = "web",
    outbox_payload: dict | None = None,
):
    """Record a domain event and its transactional-outbox row.

    ``payload`` is persisted on the durable :class:`DomainEvent`. ``outbox_payload``
    rides only the transient :class:`OutboxEvent` — use it for delivery-only data
    (e.g. a recipient email) that must not be retained in the permanent event log.
    """
    organization = getattr(aggregate, "organization", None)
    event = DomainEvent.objects.create(
        organization=organization,
        event_type=event_type,
        aggregate_type=aggregate.__class__.__name__,
        aggregate_id=str(aggregate.pk),
        payload=payload or {},
        actor=actor,
        source=source,
    )
    OutboxEvent.objects.create(
        organization=organization,
        event_type=event_type,
        payload={"domain_event_id": event.pk, **(payload or {}), **(outbox_payload or {})},
    )
    return event


def audit_action(
    *,
    action: str,
    entity,
    actor=None,
    before: dict | None = None,
    after: dict | None = None,
    reason: str = "",
    source: str = "web",
):
    return AuditLog.objects.create(
        organization=getattr(entity, "organization", None),
        actor=actor,
        action=action,
        entity_type=entity.__class__.__name__,
        entity_id=str(entity.pk),
        before=before or {},
        after=after or {},
        reason=reason,
        source=source,
    )


def record_librarian_override(
    *, organization, actor, reason: str, entity, before: dict | None = None, after: dict | None = None
) -> LibrarianOverride:
    """Persist an explicit staff override of policy/circulation state.

    Paired with an audit entry in the same transaction as the state change so
    every override is attributable (see E-Library spec §2.3, §7.1).
    """
    if actor is None:
        raise DomainError("An override requires a staff actor.")
    if not reason:
        raise DomainError("An override requires a reason.")
    override = LibrarianOverride.objects.create(
        organization=organization,
        actor=actor,
        reason=reason,
        entity_type=entity.__class__.__name__,
        entity_id=str(entity.pk),
        before=before or {},
        after=after or {},
    )
    audit_action(
        action="librarian.override",
        entity=entity,
        actor=actor,
        before=before,
        after=after,
        reason=reason,
        source="librarian",
    )
    return override


def _generate_card_number(organization) -> str:
    return f"C{organization.pk:03d}-{secrets.token_hex(4).upper()}"


def register_patron(
    *, user, organization, home_branch=None, notification_email: str = "", source: str = "web"
) -> PatronProfile:
    """Create a patron profile for a newly self-registered user."""
    try:
        entitlements.assert_within_limit(organization, "patrons")
    except entitlements.EntitlementError as exc:
        raise DomainError(str(exc)) from exc
    for _ in range(5):
        try:
            with transaction.atomic():
                profile = PatronProfile.objects.create(
                    user=user,
                    organization=organization,
                    home_branch=home_branch,
                    library_card_number=_generate_card_number(organization),
                    notification_email=notification_email or user.email,
                )
            break
        except IntegrityError as exc:
            # Retry only on a card-number collision (unique per org); anything
            # else (e.g. the user already has a profile) is a real error.
            if "uniq_card_per_org" in str(exc):
                continue
            raise
    else:
        raise DomainError("Could not allocate a library card number. Please try again.")

    audit_action(action="patron.register", entity=profile, actor=user, source=source)
    emit_domain_event(
        event_type="patron.registered",
        aggregate=profile,
        payload={"patron_id": profile.pk},
        actor=user,
        source=source,
    )
    return profile


# --------------------------------------------------------------------------- #
# Fines, fees, and payments
# --------------------------------------------------------------------------- #
def fee_policy_for(organization) -> FeePolicy:
    policy, _ = FeePolicy.objects.get_or_create(organization=organization)
    return policy


def patron_balance_cents(patron: PatronProfile) -> int:
    """Total outstanding (owed) balance for a patron, in cents."""
    total = 0
    for fee in Fee.objects.filter(patron=patron).exclude(status=FeeStatus.WAIVED):
        total += max(0, fee.amount_cents - fee.paid_cents)
    return total


def assess_overdue_fine(*, loan: Loan, now=None) -> Fee | None:
    """Create/update the accruing overdue fee for a loan.

    Idempotent: one 'overdue' fee per loan whose amount tracks days overdue
    (never lowered below what has already been paid). Returns the fee or None
    when nothing is owed.
    """
    now = now or timezone.now()
    if loan.patron_id is None:
        return None
    reference = loan.returned_at or now
    days_overdue = (reference.date() - loan.due_at.date()).days
    fee_policy = fee_policy_for(loan.organization)
    # The circulation matrix can override the per-day rate and cap per material.
    circ = policies.resolve_policy(
        organization=loan.organization,
        patron=loan.patron,
        edition=loan.copy.edition,
        branch=loan.copy.branch,
    )
    daily = (
        circ.daily_overdue_cents
        if circ.daily_overdue_cents is not None
        else fee_policy.daily_overdue_cents
    )
    cap = (
        circ.max_overdue_cents
        if circ.max_overdue_cents is not None
        else fee_policy.max_overdue_cents
    )
    billable_days = max(0, days_overdue - fee_policy.grace_days)
    amount = min(cap, billable_days * daily)
    existing = Fee.objects.filter(loan=loan, fee_type=FeeType.OVERDUE).first()
    if amount <= 0 and existing is None:
        return None
    if existing is None:
        fee = Fee.objects.create(
            organization=loan.organization,
            patron_id=loan.patron_id,
            loan=loan,
            fee_type=FeeType.OVERDUE,
            amount_cents=amount,
            description="Overdue fine",
        )
        audit_action(action="fee.assess", entity=fee, source="scheduler")
        return fee
    # Never reduce below already-paid; keep it monotonic as it accrues.
    new_amount = max(existing.paid_cents, amount, existing.amount_cents if loan.returned_at else amount)
    if loan.returned_at:  # finalize at return: exact accrued amount (>= paid)
        new_amount = max(existing.paid_cents, amount)
    if new_amount != existing.amount_cents:
        existing.amount_cents = new_amount
        if existing.paid_cents >= existing.amount_cents and existing.amount_cents > 0:
            existing.status = FeeStatus.PAID
        existing.save(update_fields=["amount_cents", "status", "updated_at"])
    return existing


def assess_overdue_fines(*, now=None) -> int:
    """Sweep: accrue overdue fines for all still-outstanding overdue loans."""
    now = now or timezone.now()
    count = 0
    loans = Loan.objects.select_related("organization").filter(
        status=LoanStatus.OVERDUE, patron__isnull=False
    )
    for loan in loans:
        if assess_overdue_fine(loan=loan, now=now) is not None:
            count += 1
    return count


def assess_lost_item_fee(*, loan: Loan, actor=None) -> Fee:
    policy = fee_policy_for(loan.organization)
    fee = Fee.objects.create(
        organization=loan.organization,
        patron_id=loan.patron_id,
        loan=loan,
        fee_type=FeeType.LOST,
        amount_cents=policy.lost_item_fee_cents,
        description="Lost item fee",
    )
    audit_action(action="fee.lost", entity=fee, actor=actor)
    return fee


def record_payment(
    *, patron: PatronProfile, amount_cents: int, method: str = "online", reference: str = "", actor=None
) -> Payment:
    """Record a payment and allocate it across outstanding fees oldest-first."""
    if amount_cents <= 0:
        raise DomainError("Payment amount must be positive.")
    with transaction.atomic():
        payment = Payment.objects.create(
            organization=patron.organization,
            patron=patron,
            amount_cents=amount_cents,
            method=method,
            reference=reference,
            actor=actor,
        )
        remaining = amount_cents
        fees = (
            Fee.objects.select_for_update()
            .filter(patron=patron, status=FeeStatus.OUTSTANDING)
            .order_by("created_at", "id")
        )
        for fee in fees:
            if remaining <= 0:
                break
            owed = max(0, fee.amount_cents - fee.paid_cents)
            applied = min(owed, remaining)
            if applied <= 0:
                continue
            fee.paid_cents += applied
            remaining -= applied
            if fee.paid_cents >= fee.amount_cents:
                fee.status = FeeStatus.PAID
            fee.save(update_fields=["paid_cents", "status", "updated_at"])
            # Record exactly which fee this payment paid, so a later refund can
            # reverse the right fees instead of guessing.
            PaymentAllocation.objects.create(payment=payment, fee=fee, amount_cents=applied)
        audit_action(
            action="payment.record",
            entity=payment,
            actor=actor,
            after={"amount_cents": amount_cents, "unapplied_cents": remaining},
        )
        emit_domain_event(
            event_type="payment.recorded",
            aggregate=payment,
            payload={"patron_id": patron.pk, "amount_cents": amount_cents},
            actor=actor,
        )
        return payment


def waive_fee(*, fee: Fee, actor=None, reason: str = "") -> Fee:
    if fee.status == FeeStatus.WAIVED:
        return fee
    fee.status = FeeStatus.WAIVED
    fee.waived_reason = reason
    fee.save(update_fields=["status", "waived_reason", "updated_at"])
    record_librarian_override(
        organization=fee.organization,
        actor=actor,
        reason=reason or "fee waived",
        entity=fee,
        after={"waived": True, "amount_cents": fee.amount_cents},
    )
    return fee


def rebuild_work_search_document(work_id: int) -> WorkSearchDocument:
    work = (
        Work.objects.prefetch_related("authors", "subjects", "editions").filter(pk=work_id).first()
    )
    if work is None:
        raise DomainError("Work does not exist.")

    authors = " ".join([author.name for author in work.authors.all()])
    aliases = " ".join(alias for author in work.authors.all() for alias in (author.aliases or []))
    subjects = " ".join([subject.name for subject in work.subjects.filter(public=True)])
    editions = Edition.objects.filter(work=work)
    edition_text = " ".join(
        " ".join(
            filter(
                None,
                [
                    edition.isbn_10,
                    edition.isbn_13,
                    edition.publisher,
                    edition.edition_statement,
                    edition.format,
                    edition.description,
                    str(edition.publication_year or ""),
                ],
            )
        )
        for edition in editions
    )
    document = " ".join(
        filter(
            None,
            [
                work.canonical_title,
                work.subtitle,
                work.normalized_title,
                authors,
                aliases,
                subjects,
                work.summary,
                edition_text,
            ],
        )
    )
    from .search import embed_text

    row, _ = WorkSearchDocument.objects.update_or_create(
        work=work,
        defaults={"search_document": document, "embedding": embed_text(document)},
    )
    if connection.vendor == "postgresql":
        WorkSearchDocument.objects.filter(pk=row.pk).update(
            search_vector=SearchVector("search_document", config=settings.SEARCH_CONFIG)
        )
        row.refresh_from_db()
    return row


def reindex_author_works(author_id: int) -> int:
    count = 0
    for work_id in Work.objects.filter(authors__id=author_id).values_list("id", flat=True):
        rebuild_work_search_document(work_id)
        count += 1
    return count


def available_copies_for_work(*, organization, work, branch=None):
    qs = Copy.objects.select_related("edition", "branch").filter(
        organization=organization,
        edition__work=work,
        edition__public_status=PublicStatus.PUBLISHED,
        public_visible=True,
        status=CopyStatus.AVAILABLE,
    )
    if branch is not None:
        qs = qs.filter(branch=branch)
    return qs.order_by("branch__name", "barcode")


def _loan_due_at(copy: Copy, patron: PatronProfile) -> timezone.datetime:
    policy = policies.resolve_policy(
        organization=copy.organization, patron=patron, edition=copy.edition, branch=copy.branch
    )
    return timezone.now() + timedelta(days=policy.loan_days)


def _hold_expires_at(copy: Copy, patron: PatronProfile) -> timezone.datetime:
    policy = policies.resolve_policy(
        organization=copy.organization, patron=patron, edition=copy.edition, branch=copy.branch
    )
    return timezone.now() + timedelta(days=policy.hold_pickup_days)


def assert_patron_can_act(patron: PatronProfile) -> None:
    if patron.status != PatronStatus.ACTIVE:
        raise DomainError("This patron account is not active.")


def assert_patron_below_fine_threshold(patron: PatronProfile) -> None:
    policy = fee_policy_for(patron.organization)
    if policy.block_threshold_cents <= 0:
        return
    if patron_balance_cents(patron) >= policy.block_threshold_cents:
        raise DomainError(
            "Outstanding fines exceed the borrowing limit. Please settle your balance."
        )


def borrow_work(
    *,
    patron: PatronProfile,
    work: Work,
    branch=None,
    actor=None,
    source: str = "web",
    override_reason: str = "",
) -> Loan:
    """Check a copy out to ``patron``.

    ``override_reason`` (staff only) records a :class:`LibrarianOverride` and
    bypasses the duplicate-loan and hold-queue policy blocks, per the spec's
    "unless a librarian override explicitly allows it" rule. The hard loan-limit
    is never bypassed.
    """
    assert_patron_can_act(patron)
    organization = patron.organization
    branch = branch or patron.home_branch
    override = bool(override_reason)
    if override and actor is None:
        raise DomainError("An override requires a staff actor.")
    if not override:
        # Staff may override a fine block (recorded as a LibrarianOverride below).
        assert_patron_below_fine_threshold(patron)

    with transaction.atomic():
        # Serialize concurrent circulation for this patron and this work. Locking
        # the patron row closes the limit-check race (counting FOR UPDATE locks
        # nothing when the patron currently has zero loans); the advisory lock
        # keeps borrow and hold placement from racing over the same copy.
        PatronProfile.objects.select_for_update().get(pk=patron.pk)
        advisory_xact_lock(75, work.pk)

        active_count = Loan.objects.filter(
            organization=organization,
            patron=patron,
            status__in=[LoanStatus.ACTIVE, LoanStatus.OVERDUE],
        ).count()
        max_loans = policies.resolve_policy(organization=organization, patron=patron).max_loans
        if active_count >= max_loans:
            raise DomainError("Loan limit reached.")

        duplicate = Loan.objects.filter(
            organization=organization,
            patron=patron,
            copy__edition__work=work,
            status__in=[LoanStatus.ACTIVE, LoanStatus.OVERDUE],
        ).exists()
        if duplicate and not override:
            raise DomainError("This patron already has this work on loan.")

        ready_hold = (
            # of=("self",) locks only the hold row; without it, select_related on
            # the nullable assigned_copy produces a LEFT JOIN that PostgreSQL
            # refuses to lock ("FOR UPDATE cannot be applied to the nullable side
            # of an outer join"). The copy is locked separately below.
            Hold.objects.select_for_update(of=("self",))
            .filter(
                organization=organization,
                patron=patron,
                work=work,
                status=HoldStatus.READY,
            )
            .select_related("assigned_copy")
            .order_by("ready_at", "id")
            .first()
        )

        if ready_hold:
            copy = Copy.objects.select_for_update().get(pk=ready_hold.assigned_copy_id)
            if copy.status != CopyStatus.ON_HOLD:
                raise DomainError("The held copy is not available for checkout.")
        else:
            waiting_ahead = Hold.objects.filter(
                organization=organization,
                work=work,
                status=HoldStatus.WAITING,
            ).exists()
            if waiting_ahead and not override:
                raise DomainError("A hold queue exists for this work.")
            copy_qs = available_copies_for_work(organization=organization, work=work, branch=branch)
            if not copy_qs.exists() and branch is not None:
                copy_qs = available_copies_for_work(organization=organization, work=work)
            copy = copy_qs.select_for_update(skip_locked=True).first()
            if copy is None:
                raise DomainError("No available copy can be loaned.")

        copy.status = CopyStatus.LOANED
        copy.save(update_fields=["status", "updated_at"])
        loan = Loan.objects.create(
            organization=organization,
            copy=copy,
            patron=patron,
            due_at=_loan_due_at(copy, patron),
        )
        if ready_hold:
            ready_hold.status = HoldStatus.FULFILLED
            ready_hold.loan = loan
            ready_hold.save(update_fields=["status", "loan", "updated_at"])
        if override:
            record_librarian_override(
                organization=organization,
                actor=actor,
                reason=override_reason,
                entity=loan,
                before={"duplicate_blocked": duplicate},
                after={"loan_id": loan.pk, "copy_id": copy.pk},
            )
        audit_action(action="loan.borrow", entity=loan, actor=actor, source=source)
        emit_domain_event(
            event_type="loan.borrowed",
            aggregate=loan,
            payload={"work_id": work.pk, "copy_id": copy.pk, "patron_id": patron.pk},
            actor=actor,
            source=source,
        )
        return loan


def _mark_hold_ready(hold: Hold, copy: Copy, *, actor, source: str) -> None:
    """Reserve ``copy`` for ``hold`` at its pickup branch and notify the patron."""
    copy.status = CopyStatus.ON_HOLD
    copy.save(update_fields=["branch", "status", "updated_at"])
    hold.assigned_copy = copy
    hold.status = HoldStatus.READY
    hold.ready_at = timezone.now()
    hold.expires_at = _hold_expires_at(copy, hold.patron)
    hold.save(update_fields=["assigned_copy", "status", "ready_at", "expires_at", "updated_at"])
    audit_action(
        action="hold.ready",
        entity=hold,
        actor=actor,
        after={"assigned_copy_id": copy.pk, "copy_branch_id": copy.branch_id},
        source=source,
    )
    emit_domain_event(
        event_type="hold.ready",
        aggregate=hold,
        payload={"copy_id": copy.pk, "work_id": hold.work_id, "patron_id": hold.patron_id},
        actor=actor,
        source=source,
    )


def assign_copy_to_next_hold(*, copy: Copy, actor=None, source: str = "system") -> Hold | None:
    work = copy.edition.work
    organization = copy.organization
    advisory_xact_lock(75, work.pk)
    next_hold = (
        Hold.objects.select_for_update(skip_locked=True)
        # Skip holds that already have a copy assigned (e.g. one in transit) so
        # a freed copy is never double-assigned.
        .filter(
            organization=organization,
            work=work,
            status=HoldStatus.WAITING,
            assigned_copy__isnull=True,
        )
        .select_related("preferred_branch")
        .order_by("created_at", "id")
        .first()
    )
    if next_hold is None:
        return None

    if copy.branch_id != next_hold.preferred_branch_id:
        # Cross-branch: put the copy in transit and keep the hold WAITING until a
        # staff member receives it at the pickup branch (see receive_copy_in_transit).
        CopyMovement.objects.create(
            organization=organization,
            copy=copy,
            from_branch=copy.branch,
            to_branch=next_hold.preferred_branch,
            from_status=copy.status,
            to_status=CopyStatus.IN_TRANSIT,
            reason="hold_transfer",
            actor=actor,
        )
        copy.status = CopyStatus.IN_TRANSIT
        copy.save(update_fields=["status", "updated_at"])
        next_hold.assigned_copy = copy
        next_hold.save(update_fields=["assigned_copy", "updated_at"])
        audit_action(
            action="hold.in_transit",
            entity=next_hold,
            actor=actor,
            after={"assigned_copy_id": copy.pk, "to_branch_id": next_hold.preferred_branch_id},
            source=source,
        )
        emit_domain_event(
            event_type="hold.in_transit",
            aggregate=next_hold,
            payload={"copy_id": copy.pk, "work_id": work.pk, "patron_id": next_hold.patron_id},
            actor=actor,
            source=source,
        )
        return next_hold

    _mark_hold_ready(next_hold, copy, actor=actor, source=source)
    return next_hold


def receive_copy_in_transit(*, copy: Copy, actor=None, source: str = "staff") -> Hold | None:
    """Receive a transferred copy at its destination branch and ready the hold."""
    with transaction.atomic():
        copy = Copy.objects.select_for_update().get(pk=copy.pk)
        if copy.status != CopyStatus.IN_TRANSIT:
            raise DomainError("This copy is not in transit.")
        hold = (
            Hold.objects.select_for_update(of=("self",))
            .filter(assigned_copy=copy, status=HoldStatus.WAITING)
            .select_related("preferred_branch")
            .order_by("created_at", "id")
            .first()
        )
        if hold is None:
            # No waiting hold still wants it — return it to the shelf.
            copy.status = CopyStatus.AVAILABLE
            copy.save(update_fields=["status", "updated_at"])
            return None
        CopyMovement.objects.create(
            organization=copy.organization,
            copy=copy,
            from_branch=copy.branch,
            to_branch=hold.preferred_branch,
            from_status=CopyStatus.IN_TRANSIT,
            to_status=CopyStatus.ON_HOLD,
            reason="hold_received",
            actor=actor,
        )
        copy.branch = hold.preferred_branch
        _mark_hold_ready(hold, copy, actor=actor, source=source)
        return hold


def staff_checkin(*, copy: Copy, actor=None, source: str = "staff"):
    """Desk check-in: receive an in-transit copy, or return the active loan on it."""
    copy = Copy.objects.get(pk=copy.pk)
    if copy.status == CopyStatus.IN_TRANSIT:
        return receive_copy_in_transit(copy=copy, actor=actor, source=source)
    active_loan = (
        Loan.objects.filter(
            copy=copy, status__in=[LoanStatus.ACTIVE, LoanStatus.OVERDUE]
        )
        .order_by("-borrowed_at")
        .first()
    )
    if active_loan is not None:
        return return_loan(loan=active_loan, actor=actor, source=source)
    raise DomainError("This copy has no active loan and is not in transit.")


def retire_copy(*, copy: Copy, actor=None, reason: str = "", source: str = "staff") -> Copy:
    with transaction.atomic():
        copy = Copy.objects.select_for_update().get(pk=copy.pk)
        if Loan.objects.filter(
            copy=copy, status__in=[LoanStatus.ACTIVE, LoanStatus.OVERDUE]
        ).exists():
            raise DomainError("Cannot retire a copy that is on loan.")
        if copy.status in [CopyStatus.ON_HOLD, CopyStatus.IN_TRANSIT]:
            raise DomainError("Cannot retire a copy reserved for a hold; cancel the hold first.")
        before = copy.status
        copy.status = CopyStatus.RETIRED
        copy.public_visible = False
        copy.save(update_fields=["status", "public_visible", "updated_at"])
        CopyMovement.objects.create(
            organization=copy.organization,
            copy=copy,
            from_branch=copy.branch,
            to_branch=copy.branch,
            from_status=before,
            to_status=CopyStatus.RETIRED,
            reason=reason or "retired",
            actor=actor,
        )
        audit_action(
            action="copy.retire",
            entity=copy,
            actor=actor,
            before={"status": before},
            after={"status": CopyStatus.RETIRED},
            reason=reason,
            source=source,
        )
        emit_domain_event(
            event_type="copy.retired",
            aggregate=copy,
            payload={"copy_id": copy.pk},
            actor=actor,
            source=source,
        )
        return copy


def move_copy(*, copy: Copy, to_branch, actor=None, reason: str = "", source: str = "staff") -> Copy:
    with transaction.atomic():
        copy = Copy.objects.select_for_update().get(pk=copy.pk)
        if copy.status in [CopyStatus.LOANED, CopyStatus.ON_HOLD, CopyStatus.IN_TRANSIT]:
            raise DomainError("Cannot move a copy that is loaned, on hold, or in transit.")
        from_branch = copy.branch
        if from_branch.id == to_branch.id:
            return copy
        CopyMovement.objects.create(
            organization=copy.organization,
            copy=copy,
            from_branch=from_branch,
            to_branch=to_branch,
            from_status=copy.status,
            to_status=copy.status,
            reason=reason or "manual_move",
            actor=actor,
        )
        copy.branch = to_branch
        copy.save(update_fields=["branch", "updated_at"])
        audit_action(
            action="copy.move",
            entity=copy,
            actor=actor,
            before={"branch_id": from_branch.id},
            after={"branch_id": to_branch.id},
            reason=reason,
            source=source,
        )
        emit_domain_event(
            event_type="copy.moved",
            aggregate=copy,
            payload={"copy_id": copy.pk, "to_branch_id": to_branch.id},
            actor=actor,
            source=source,
        )
        return copy


def return_loan(*, loan: Loan, actor=None, source: str = "web", settle_branch=None) -> Loan:
    """Return a loan. If ``settle_branch`` is given and the copy floats, the copy
    is re-homed to that branch instead of belonging to its previous branch."""
    with transaction.atomic():
        loan = (
            # of=("self",): patron is nullable (privacy scrubbing sets it NULL),
            # so select_related("patron") is a LEFT JOIN that cannot be locked.
            Loan.objects.select_for_update(of=("self",))
            .select_related("copy", "copy__edition__work", "patron")
            .get(pk=loan.pk)
        )
        if loan.status not in [LoanStatus.ACTIVE, LoanStatus.OVERDUE]:
            raise DomainError("Only active or overdue loans can be returned.")

        copy = Copy.objects.select_for_update().get(pk=loan.copy_id)
        patron = loan.patron
        # Capture the recipient/title now, before privacy scrubbing detaches the
        # patron, so the async return receipt can still be delivered.
        notify_recipient = ""
        if patron:
            notify_recipient = patron.notification_email or patron.user.email
        notify_title = loan.copy.edition.work.canonical_title
        loan.status = LoanStatus.RETURNED
        loan.returned_at = timezone.now()
        loan.patron_hash = stable_patron_hash(patron)
        # Finalize any overdue fine while the patron is still attached (a fee owed
        # survives privacy scrubbing because the patron owes the money).
        if patron is not None:
            assess_overdue_fine(loan=loan)
        if patron and not patron.retain_loan_history:
            loan.patron = None
        loan.save(update_fields=["status", "returned_at", "patron_hash", "patron", "updated_at"])

        copy.status = CopyStatus.AVAILABLE
        # Floating collections: a floating item settles where it is returned.
        if (
            settle_branch is not None
            and copy.floating
            and settle_branch.organization_id == copy.organization_id
            and copy.branch_id != settle_branch.pk
        ):
            from_branch = copy.branch
            copy.branch = settle_branch
            copy.save(update_fields=["status", "branch", "updated_at"])
            CopyMovement.objects.create(
                organization=copy.organization,
                copy=copy,
                from_branch=from_branch,
                to_branch=settle_branch,
                from_status=CopyStatus.LOANED,
                to_status=CopyStatus.AVAILABLE,
                reason="floating settle",
                actor=actor,
            )
        else:
            copy.save(update_fields=["status", "updated_at"])
        assigned_hold = assign_copy_to_next_hold(copy=copy, actor=actor, source=source)
        audit_action(action="loan.return", entity=loan, actor=actor, source=source)
        emit_domain_event(
            event_type="loan.returned",
            aggregate=loan,
            payload={
                "copy_id": copy.pk,
                "assigned_hold_id": assigned_hold.pk if assigned_hold else None,
            },
            # Recipient/title are delivery-only: they must not persist in the
            # durable DomainEvent after the patron has been privacy-scrubbed.
            outbox_payload={"recipient": notify_recipient, "title": notify_title},
            actor=actor,
            source=source,
        )
        return loan


def place_hold(
    *, patron: PatronProfile, work: Work, preferred_branch=None, actor=None, source: str = "web"
) -> Hold:
    assert_patron_can_act(patron)
    organization = patron.organization
    preferred_branch = preferred_branch or patron.home_branch
    if preferred_branch is None:
        raise DomainError("A pickup branch is required.")
    if not policies.work_is_holdable(organization=organization, patron=patron, work=work):
        raise DomainError("This material cannot be placed on hold.")

    with transaction.atomic():
        # Lock the patron row so the max-holds check cannot be raced by a
        # concurrent hold for a different work (the advisory lock below only
        # serializes activity for *this* work).
        PatronProfile.objects.select_for_update().get(pk=patron.pk)
        advisory_xact_lock(75, work.pk)
        active_holds = Hold.objects.filter(
            organization=organization,
            patron=patron,
            status__in=[HoldStatus.WAITING, HoldStatus.READY],
        )
        max_holds = policies.resolve_policy(organization=organization, patron=patron).max_holds
        if active_holds.count() >= max_holds:
            raise DomainError("Hold limit reached.")
        if active_holds.filter(work=work).exists():
            raise DomainError("This patron already has an active hold for this work.")

        hold = Hold.objects.create(
            organization=organization,
            work=work,
            patron=patron,
            preferred_branch=preferred_branch,
            status=HoldStatus.WAITING,
        )
        if (
            not Hold.objects.filter(
                organization=organization,
                work=work,
                status=HoldStatus.WAITING,
            )
            .exclude(pk=hold.pk)
            .exists()
        ):
            copy = (
                available_copies_for_work(
                    organization=organization, work=work, branch=preferred_branch
                )
                .select_for_update(skip_locked=True)
                .first()
            )
            if copy is None:
                copy = (
                    available_copies_for_work(organization=organization, work=work)
                    .select_for_update(skip_locked=True)
                    .first()
                )
            if copy:
                if copy.branch_id != preferred_branch.id:
                    # Cross-branch: ship it and leave the hold WAITING until it is
                    # received at the pickup branch (no premature "ready" notice).
                    CopyMovement.objects.create(
                        organization=organization,
                        copy=copy,
                        from_branch=copy.branch,
                        to_branch=preferred_branch,
                        from_status=copy.status,
                        to_status=CopyStatus.IN_TRANSIT,
                        reason="hold_transfer",
                        actor=actor,
                    )
                    copy.status = CopyStatus.IN_TRANSIT
                    copy.save(update_fields=["status", "updated_at"])
                    hold.assigned_copy = copy
                    hold.save(update_fields=["assigned_copy", "updated_at"])
                    emit_domain_event(
                        event_type="hold.in_transit",
                        aggregate=hold,
                        payload={"copy_id": copy.pk, "work_id": work.pk, "patron_id": patron.pk},
                        actor=actor,
                        source=source,
                    )
                else:
                    _mark_hold_ready(hold, copy, actor=actor, source=source)

        audit_action(action="hold.place", entity=hold, actor=actor, source=source)
        emit_domain_event(
            event_type="hold.placed",
            aggregate=hold,
            payload={"work_id": work.pk, "patron_id": patron.pk, "status": hold.status},
            actor=actor,
            source=source,
        )
        return hold


def cancel_hold(*, hold: Hold, actor=None, source: str = "web", reason: str = "") -> Hold:
    with transaction.atomic():
        hold = (
            # of=("self",): assigned_copy is nullable, so lock only the hold row.
            Hold.objects.select_for_update(of=("self",))
            .select_related("assigned_copy", "work")
            .get(pk=hold.pk)
        )
        if hold.status not in [HoldStatus.WAITING, HoldStatus.READY]:
            raise DomainError("Only active holds can be cancelled.")
        copy = hold.assigned_copy
        hold.status = HoldStatus.CANCELLED
        hold.save(update_fields=["status", "updated_at"])
        if copy:
            copy = Copy.objects.select_for_update().get(pk=copy.pk)
            copy.status = CopyStatus.AVAILABLE
            copy.save(update_fields=["status", "updated_at"])
            assign_copy_to_next_hold(copy=copy, actor=actor, source=source)
        audit_action(action="hold.cancel", entity=hold, actor=actor, reason=reason, source=source)
        emit_domain_event(
            event_type="hold.cancelled",
            aggregate=hold,
            payload={"work_id": hold.work_id, "patron_id": hold.patron_id},
            actor=actor,
            source=source,
        )
        return hold


def renew_loan(*, loan: Loan, actor=None, source: str = "web") -> Renewal:
    with transaction.atomic():
        loan = (
            Loan.objects.select_for_update()
            .select_related("copy", "copy__edition__work")
            .get(pk=loan.pk)
        )
        if loan.status not in [LoanStatus.ACTIVE, LoanStatus.OVERDUE]:
            raise DomainError("Only active or overdue loans can be renewed.")
        if loan.patron is not None:
            assert_patron_can_act(loan.patron)
        policy = policies.resolve_policy(
            organization=loan.organization,
            patron=loan.patron,
            edition=loan.copy.edition,
            branch=loan.copy.branch,
        )
        if loan.renewal_count >= policy.max_renewals:
            raise DomainError("Renewal limit reached.")
        waiting_holds = Hold.objects.filter(
            organization=loan.organization,
            work=loan.copy.edition.work,
            status=HoldStatus.WAITING,
        ).exists()
        if waiting_holds:
            raise DomainError("Renewal is blocked because patrons are waiting.")
        old_due_at = loan.due_at
        # Extend from now for an overdue loan so a renewal never lands in the
        # past (which would leave it immediately overdue again).
        base = max(loan.due_at, timezone.now())
        loan.due_at = base + timedelta(days=policy.loan_days)
        loan.status = LoanStatus.ACTIVE
        loan.renewal_count += 1
        loan.save(update_fields=["due_at", "status", "renewal_count", "updated_at"])
        renewal = Renewal.objects.create(
            loan=loan,
            renewed_by=actor,
            old_due_at=old_due_at,
            new_due_at=loan.due_at,
            source=source,
        )
        audit_action(
            action="loan.renew",
            entity=loan,
            actor=actor,
            before={"due_at": old_due_at.isoformat()},
            after={"due_at": loan.due_at.isoformat()},
            source=source,
        )
        emit_domain_event(
            event_type="loan.renewed",
            aggregate=loan,
            payload={"renewal_id": renewal.pk, "old_due_at": old_due_at.isoformat()},
            actor=actor,
            source=source,
        )
        return renewal


def expire_ready_holds(*, now=None, actor=None) -> int:
    now = now or timezone.now()
    expired_count = 0
    for hold_id in Hold.objects.filter(status=HoldStatus.READY, expires_at__lte=now).values_list(
        "id", flat=True
    ):
        with transaction.atomic():
            hold = (
                # of=("self",): assigned_copy is nullable, so lock only the hold row.
                Hold.objects.select_for_update(of=("self",))
                .select_related("assigned_copy")
                .get(pk=hold_id)
            )
            if hold.status != HoldStatus.READY:
                continue
            copy = hold.assigned_copy
            hold.status = HoldStatus.EXPIRED
            hold.save(update_fields=["status", "updated_at"])
            if copy:
                copy = Copy.objects.select_for_update().get(pk=copy.pk)
                copy.status = CopyStatus.AVAILABLE
                copy.save(update_fields=["status", "updated_at"])
                assign_copy_to_next_hold(copy=copy, actor=actor, source="scheduler")
            emit_domain_event(
                event_type="hold.expired",
                aggregate=hold,
                payload={"work_id": hold.work_id, "patron_id": hold.patron_id},
                actor=actor,
                source="scheduler",
            )
            expired_count += 1
    return expired_count


MAX_TRANSIT_ATTEMPTS = 3


def expire_stale_transits(
    *, now=None, actor=None, max_transit_days: int = 14, max_attempts: int = MAX_TRANSIT_ATTEMPTS
) -> int:
    """Recover copies stuck IN_TRANSIT past ``max_transit_days``.

    Each timeout re-attempts the transfer; after ``max_attempts`` the copy is
    pulled from circulation (status REPAIR) for staff investigation and its hold
    is left WAITING (so a different copy can serve it), rather than silently
    re-shipping a lost copy forever.
    """
    now = now or timezone.now()
    cutoff = now - timedelta(days=max_transit_days)
    recovered = 0
    for copy_id in Copy.objects.filter(
        status=CopyStatus.IN_TRANSIT, updated_at__lt=cutoff
    ).values_list("id", flat=True):
        with transaction.atomic():
            copy = Copy.objects.select_for_update().get(pk=copy_id)
            if copy.status != CopyStatus.IN_TRANSIT:
                continue
            hold = (
                Hold.objects.select_for_update(of=("self",))
                .filter(assigned_copy=copy, status=HoldStatus.WAITING)
                .first()
            )
            give_up = False
            if hold is not None:
                hold.transit_attempts += 1
                give_up = hold.transit_attempts >= max_attempts
                hold.assigned_copy = None
                hold.save(update_fields=["assigned_copy", "transit_attempts", "updated_at"])

            if give_up:
                copy.status = CopyStatus.REPAIR
                copy.save(update_fields=["status", "updated_at"])
                audit_action(
                    action="copy.transit_failed", entity=copy, actor=actor, source="scheduler"
                )
                emit_domain_event(
                    event_type="copy.transit_failed",
                    aggregate=copy,
                    payload={"copy_id": copy.pk, "attempts": hold.transit_attempts},
                    actor=actor,
                    source="scheduler",
                )
                logger.warning(
                    "Copy %s pulled for review after %s failed transit attempts",
                    copy.pk,
                    hold.transit_attempts,
                )
            else:
                copy.status = CopyStatus.AVAILABLE
                copy.save(update_fields=["status", "updated_at"])
                audit_action(
                    action="copy.transit_expired", entity=copy, actor=actor, source="scheduler"
                )
                assign_copy_to_next_hold(copy=copy, actor=actor, source="scheduler")
            recovered += 1
    return recovered


def reconcile_holds(*, actor=None, max_assignments: int = 500) -> int:
    """Match available copies to waiting-and-unassigned holds.

    assign_copy_to_next_hold only fires on circulation events; this sweep repairs
    the invariant if an AVAILABLE copy ever coexists with a WAITING hold (e.g. a
    hold placed while another hold's transfer was already in flight). Bounded by
    ``max_assignments`` per run and logs what it moved."""
    assigned = 0
    pairs = (
        Hold.objects.filter(status=HoldStatus.WAITING, assigned_copy__isnull=True)
        .values_list("organization_id", "work_id")
        .distinct()
    )
    for org_id, work_id in list(pairs):
        if assigned >= max_assignments:
            break
        skipped: set = set()
        while assigned < max_assignments:
            candidate = (
                Copy.objects.filter(
                    organization_id=org_id,
                    edition__work_id=work_id,
                    status=CopyStatus.AVAILABLE,
                    public_visible=True,
                )
                .exclude(pk__in=skipped)
                .only("id")
                .first()
            )
            if candidate is None:
                break
            with transaction.atomic():
                copy = (
                    Copy.objects.select_for_update(skip_locked=True)
                    .filter(pk=candidate.pk, status=CopyStatus.AVAILABLE)
                    .select_related("edition__work")
                    .first()
                )
                if copy is None:
                    # Locked by another txn — don't re-pick it and spin.
                    skipped.add(candidate.pk)
                    continue
                result = assign_copy_to_next_hold(copy=copy, actor=actor, source="scheduler")
            if result is None:
                break
            assigned += 1
    if assigned:
        logger.info("reconcile_holds assigned %s copies to waiting holds", assigned)
    return assigned


def flag_overdue_loans(*, now=None) -> int:
    now = now or timezone.now()
    overdue_ids = list(
        Loan.objects.filter(status=LoanStatus.ACTIVE, due_at__lt=now).values_list(
            "id", flat=True
        )
    )
    if not overdue_ids:
        return 0
    Loan.objects.filter(id__in=overdue_ids).update(status=LoanStatus.OVERDUE, updated_at=now)
    for loan in Loan.objects.filter(id__in=overdue_ids):
        emit_domain_event(
            event_type="loan.overdue",
            aggregate=loan,
            payload={"copy_id": loan.copy_id, "due_at": loan.due_at.isoformat()},
            source="scheduler",
        )
    return len(overdue_ids)


def send_due_soon_notifications(*, now=None, window_days: int = 3) -> int:
    """Emit a due-soon event per eligible loan; the outbox worker delivers them.

    This keeps SMTP off the synchronous sweep and uses the same delivery path as
    every other notification. Emission is idempotent per loan per due window.
    """
    now = now or timezone.now()
    until = now + timedelta(days=window_days)
    emitted = 0
    loans = Loan.objects.select_related(
        "patron", "patron__user", "organization", "copy", "copy__edition__work"
    ).filter(
        status=LoanStatus.ACTIVE,
        patron__isnull=False,
        due_at__range=(now, until),
    )
    for loan in loans:
        email = loan.patron.notification_email or loan.patron.user.email
        if not email:
            continue
        already_emitted = DomainEvent.objects.filter(
            event_type="loan.due_soon",
            aggregate_type="Loan",
            aggregate_id=str(loan.pk),
            created_at__gte=loan.due_at - timedelta(days=window_days),
        ).exists()
        if already_emitted:
            continue
        emit_domain_event(
            event_type="loan.due_soon",
            aggregate=loan,
            payload={"due_at": loan.due_at.isoformat(), "copy_id": loan.copy_id},
            outbox_payload={
                "recipient": email,
                "title": loan.copy.edition.work.canonical_title,
            },
            source="scheduler",
        )
        emitted += 1
    return emitted


def process_outbox_event(event: OutboxEvent) -> None:
    # Side effects (patron notifications + outbound webhooks) happen here; a
    # raised exception propagates to drain_outbox, which applies retry/backoff.
    from . import webhooks

    deliver_notification(event)
    webhooks.enqueue_for_outbox_event(event)
    event.status = OutboxStatus.PROCESSED
    event.processed_at = timezone.now()
    event.save(update_fields=["status", "processed_at", "updated_at"])


def reclaim_stale_outbox_events(*, older_than_minutes: int = 15, now=None) -> int:
    """Return events stuck in PROCESSING (from a crashed worker) to PENDING."""
    now = now or timezone.now()
    cutoff = now - timedelta(minutes=older_than_minutes)
    return OutboxEvent.objects.filter(
        status=OutboxStatus.PROCESSING, updated_at__lt=cutoff
    ).update(status=OutboxStatus.PENDING, next_attempt_at=now, updated_at=now)


def drain_outbox(*, batch_size: int = 100) -> int:
    processed = 0
    now = timezone.now()
    # Recover any events a previous worker claimed but never finished before we
    # claim a fresh batch, so they are not stranded in PROCESSING forever.
    reclaim_stale_outbox_events(now=now)
    with transaction.atomic():
        qs = OutboxEvent.objects.select_for_update(skip_locked=True).filter(
            status=OutboxStatus.PENDING,
            next_attempt_at__lte=now,
        )[:batch_size]
        events = list(qs)
        for event in events:
            event.status = OutboxStatus.PROCESSING
            event.attempts += 1
            event.save(update_fields=["status", "attempts", "updated_at"])

    for event in events:
        try:
            process_outbox_event(event)
            processed += 1
        except Exception as exc:  # pragma: no cover - operational backoff
            dead_lettered = event.attempts >= 5
            event.status = OutboxStatus.FAILED if dead_lettered else OutboxStatus.PENDING
            event.last_error = str(exc)
            event.next_attempt_at = timezone.now() + timedelta(minutes=2**event.attempts)
            event.save(update_fields=["status", "last_error", "next_attempt_at", "updated_at"])
            if dead_lettered:
                logger.error(
                    "Outbox event %s (%s) dead-lettered after %s attempts: %s",
                    event.pk,
                    event.event_type,
                    event.attempts,
                    exc,
                )
            else:
                logger.warning(
                    "Outbox event %s (%s) failed (attempt %s), will retry: %s",
                    event.pk,
                    event.event_type,
                    event.attempts,
                    exc,
                )
    return processed
