"""Consortia & resource sharing: union catalog + inter-library loan (ILL).
 
Members of a :class:`Consortium` can borrow physical copies from one another. A
borrowed copy stays owned by the lender; the full loan lifecycle lives on the
:class:`IllRequest` (rather than a cross-tenant ``Loan``), and the lender's copy
moves ``AVAILABLE -> ILL -> AVAILABLE`` so it never double-allocates to local
circulation while it is away.

Lifecycle::

    request -> REQUESTED --ship--> SHIPPED --receive--> ON_LOAN
            \-> UNFILLED (no lender had it)
    ON_LOAN --return--> RETURNING --check in--> COMPLETED
    (REQUESTED/SHIPPED) --cancel--> CANCELLED  (reserved copy released)
"""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from .models import (
    ConsortiumMembership,
    Copy,
    CopyStatus,
    IllRequest,
    IllStatus,
    PatronProfile,
    Work,
    stable_patron_hash,
)
from .services import (
    DomainError,
    advisory_xact_lock,
    assert_patron_can_act,
    audit_action,
    emit_domain_event,
)

# Statuses in which a patron already has an active request for a title.
OPEN_STATUSES = [
    IllStatus.UNFILLED,
    IllStatus.REQUESTED,
    IllStatus.SHIPPED,
    IllStatus.ON_LOAN,
    IllStatus.RETURNING,
]
DEFAULT_ILL_LOAN_DAYS = 21


# --------------------------------------------------------------------------- #
# Membership helpers
# --------------------------------------------------------------------------- #
def member_org_ids(consortium, *, lends=None, borrows=None, exclude_org=None) -> list[int]:
    qs = ConsortiumMembership.objects.filter(consortium=consortium)
    if lends is not None:
        qs = qs.filter(lends=lends)
    if borrows is not None:
        qs = qs.filter(borrows=borrows)
    if exclude_org is not None:
        qs = qs.exclude(organization=exclude_org)
    return list(qs.values_list("organization_id", flat=True))


def union_availability(consortium, work) -> list[dict]:
    """Per-member available-copy counts for a work across the consortium."""
    rows = (
        Copy.objects.filter(
            organization__consortium_memberships__consortium=consortium,
            edition__work=work,
            status=CopyStatus.AVAILABLE,
            public_visible=True,
        )
        .values("organization_id", "organization__name")
        .annotate(available=Count("id"))
        .order_by("organization__name")
    )
    return [
        {
            "organization_id": r["organization_id"],
            "organization": r["organization__name"],
            "available": r["available"],
        }
        for r in rows
    ]


def union_search(consortium, query: str, *, limit: int = 20):
    """Union-catalog search: works held anywhere in the consortium."""
    member_ids = member_org_ids(consortium)
    works = (
        Work.objects.filter(
            editions__copies__organization_id__in=member_ids,
            editions__copies__public_visible=True,
        )
        .distinct()
        .prefetch_related("authors", "editions")
    )
    query = (query or "").strip()
    if query:
        works = works.filter(
            Q(canonical_title__icontains=query)
            | Q(authors__name__icontains=query)
            | Q(editions__isbn_13__icontains=query)
        ).distinct()
    return list(works[:limit])


# --------------------------------------------------------------------------- #
# Requesting
# --------------------------------------------------------------------------- #
def _reserve_lender_copy(consortium, work, *, exclude_org):
    """Lock and reserve an available copy at a lending member (or return None)."""
    lender_ids = member_org_ids(consortium, lends=True, exclude_org=exclude_org)
    if not lender_ids:
        return None
    copy = (
        Copy.objects.select_for_update(skip_locked=True)
        .filter(
            organization_id__in=lender_ids,
            edition__work=work,
            status=CopyStatus.AVAILABLE,
            public_visible=True,
        )
        .order_by("organization_id", "id")
        .first()
    )
    if copy is None:
        return None
    copy.status = CopyStatus.ILL
    copy.save(update_fields=["status", "updated_at"])
    return copy


def request_ill(*, patron: PatronProfile, work: Work, consortium, actor=None, source="web") -> IllRequest:
    """Create an ILL request, reserving a lender's copy when one is available."""
    assert_patron_can_act(patron)
    org = patron.organization
    if not consortium.allow_ill:
        raise DomainError("This consortium does not permit inter-library loans.")
    if not ConsortiumMembership.objects.filter(
        consortium=consortium, organization=org, borrows=True
    ).exists():
        raise DomainError("Your library is not a borrowing member of this consortium.")

    with transaction.atomic():
        PatronProfile.objects.select_for_update().get(pk=patron.pk)
        advisory_xact_lock(88, work.pk)
        if IllRequest.objects.filter(
            requesting_patron=patron, work=work, status__in=OPEN_STATUSES
        ).exists():
            raise DomainError("You already have an open request for this title.")
        # A locally available copy means no ILL is needed.
        if Copy.objects.filter(
            organization=org, edition__work=work, status=CopyStatus.AVAILABLE, public_visible=True
        ).exists():
            raise DomainError("This title is available at your own library.")

        copy = _reserve_lender_copy(consortium, work, exclude_org=org)
        ill = IllRequest.objects.create(
            consortium=consortium,
            work=work,
            requesting_org=org,
            requesting_patron=patron,
            lending_org=copy.organization if copy else None,
            lending_copy=copy,
            status=IllStatus.REQUESTED if copy else IllStatus.UNFILLED,
        )
        audit_action(action="ill.request", entity=ill, actor=actor, source=source)
        emit_domain_event(
            event_type="ill.requested",
            aggregate=ill,
            payload={"work_id": work.pk, "status": ill.status, "lending_org": ill.lending_org_id},
            actor=actor,
            source=source,
        )
        return ill


# --------------------------------------------------------------------------- #
# Fulfilment lifecycle
# --------------------------------------------------------------------------- #
def ship_ill(*, ill: IllRequest, actor=None, source="staff") -> IllRequest:
    with transaction.atomic():
        ill = IllRequest.objects.select_for_update().get(pk=ill.pk)
        if ill.status != IllStatus.REQUESTED:
            raise DomainError("Only a requested ILL can be shipped.")
        ill.status = IllStatus.SHIPPED
        ill.shipped_at = timezone.now()
        ill.save(update_fields=["status", "shipped_at", "updated_at"])
        audit_action(action="ill.ship", entity=ill, actor=actor, source=source)
        emit_domain_event(
            event_type="ill.shipped", aggregate=ill, payload={}, actor=actor, source=source
        )
        return ill


def receive_ill(*, ill: IllRequest, actor=None, source="staff", loan_days=DEFAULT_ILL_LOAN_DAYS) -> IllRequest:
    with transaction.atomic():
        ill = IllRequest.objects.select_for_update().get(pk=ill.pk)
        if ill.status != IllStatus.SHIPPED:
            raise DomainError("Only a shipped ILL can be received.")
        now = timezone.now()
        ill.status = IllStatus.ON_LOAN
        ill.borrowed_at = now
        ill.due_at = now + timedelta(days=loan_days)
        ill.save(update_fields=["status", "borrowed_at", "due_at", "updated_at"])
        audit_action(action="ill.receive", entity=ill, actor=actor, source=source)
        emit_domain_event(
            event_type="ill.on_loan",
            aggregate=ill,
            payload={"due_at": ill.due_at.isoformat()},
            actor=actor,
            source=source,
        )
        return ill


def return_ill(*, ill: IllRequest, actor=None, source="staff") -> IllRequest:
    with transaction.atomic():
        ill = IllRequest.objects.select_for_update().get(pk=ill.pk)
        if ill.status != IllStatus.ON_LOAN:
            raise DomainError("Only an on-loan ILL can be returned.")
        ill.status = IllStatus.RETURNING
        ill.returned_at = timezone.now()
        ill.save(update_fields=["status", "returned_at", "updated_at"])
        audit_action(action="ill.return", entity=ill, actor=actor, source=source)
        emit_domain_event(
            event_type="ill.returning", aggregate=ill, payload={}, actor=actor, source=source
        )
        return ill


def _scrub_patron(ill: IllRequest) -> None:
    patron = ill.requesting_patron
    ill.patron_hash = stable_patron_hash(patron)
    if patron is not None and not patron.retain_loan_history:
        ill.requesting_patron = None


def checkin_ill(*, ill: IllRequest, actor=None, source="staff") -> IllRequest:
    """Lender receives the returned copy: complete the ILL and free the copy."""
    with transaction.atomic():
        ill = IllRequest.objects.select_for_update(of=("self",)).select_related(
            "lending_copy", "requesting_patron"
        ).get(pk=ill.pk)
        if ill.status != IllStatus.RETURNING:
            raise DomainError("Only a returning ILL can be checked in.")
        if ill.lending_copy_id:
            copy = Copy.objects.select_for_update().get(pk=ill.lending_copy_id)
            if copy.status == CopyStatus.ILL:
                copy.status = CopyStatus.AVAILABLE
                copy.save(update_fields=["status", "updated_at"])
        ill.status = IllStatus.COMPLETED
        ill.completed_at = timezone.now()
        _scrub_patron(ill)
        ill.save(
            update_fields=[
                "status", "completed_at", "patron_hash", "requesting_patron", "updated_at",
            ]
        )
        audit_action(action="ill.checkin", entity=ill, actor=actor, source=source)
        emit_domain_event(
            event_type="ill.completed", aggregate=ill, payload={}, actor=actor, source=source
        )
        return ill


def cancel_ill(*, ill: IllRequest, actor=None, source="staff") -> IllRequest:
    with transaction.atomic():
        ill = IllRequest.objects.select_for_update(of=("self",)).select_related(
            "lending_copy", "requesting_patron"
        ).get(pk=ill.pk)
        if ill.status not in (IllStatus.UNFILLED, IllStatus.REQUESTED, IllStatus.SHIPPED):
            raise DomainError("This ILL can no longer be cancelled.")
        if ill.lending_copy_id:
            copy = Copy.objects.select_for_update().get(pk=ill.lending_copy_id)
            if copy.status == CopyStatus.ILL:
                copy.status = CopyStatus.AVAILABLE
                copy.save(update_fields=["status", "updated_at"])
        ill.status = IllStatus.CANCELLED
        _scrub_patron(ill)
        ill.save(update_fields=["status", "patron_hash", "requesting_patron", "updated_at"])
        audit_action(action="ill.cancel", entity=ill, actor=actor, source=source)
        emit_domain_event(
            event_type="ill.cancelled", aggregate=ill, payload={}, actor=actor, source=source
        )
        return ill
