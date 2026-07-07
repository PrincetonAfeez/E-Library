"""Resolve effective circulation rules from the (patron_type × material_type) matrix.

Backward compatible: when a tenant has configured no ``CirculationPolicy`` rows
(and patrons/editions have no type), resolution falls back to the legacy flat
rules on ``Branch``/``PatronProfile`` — so behavior is unchanged until a library
opts into the matrix.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import CirculationPolicy


@dataclass(frozen=True)
class EffectivePolicy:
    loan_days: int
    max_renewals: int
    hold_pickup_days: int
    holdable: bool
    max_loans: int
    max_holds: int
    daily_overdue_cents: int | None
    max_overdue_cents: int | None


def _lookup(organization, patron_type, material_type) -> CirculationPolicy | None:
    # Most specific first, then wildcard fallbacks, then global default.
    candidates = [
        (patron_type, material_type),
        (patron_type, None),
        (None, material_type),
        (None, None),
    ]
    for pt, mt in candidates:
        policy = CirculationPolicy.objects.filter(
            organization=organization, patron_type=pt, material_type=mt
        ).first()
        if policy is not None:
            return policy
    return None


def resolve_policy(*, organization, patron=None, edition=None, branch=None) -> EffectivePolicy:
    patron_type = getattr(patron, "patron_type", None) if patron else None
    material_type = getattr(edition, "material_type", None) if edition else None
    if branch is None and patron is not None:
        branch = patron.home_branch
    policy = _lookup(organization, patron_type, material_type)

    # Loan/hold rule fallbacks: matrix cell -> branch defaults -> hard defaults.
    if policy is not None:
        loan_days = policy.loan_days
        max_renewals = policy.max_renewals
        hold_pickup_days = policy.hold_pickup_days
        holdable = policy.holdable
        daily_overdue = policy.daily_overdue_cents
        max_overdue = policy.max_overdue_cents
    else:
        loan_days = branch.loan_days if branch else 21
        max_renewals = branch.max_renewals if branch else 2
        hold_pickup_days = branch.hold_pickup_days if branch else 7
        holdable = True
        daily_overdue = None
        max_overdue = None

    # Per-patron allowances: patron type -> patron flat fields -> hard defaults.
    if patron_type is not None:
        max_loans = patron_type.max_loans
        max_holds = patron_type.max_holds
    elif patron is not None:
        max_loans = patron.max_loans
        max_holds = patron.max_holds
    else:
        max_loans, max_holds = 12, 8

    return EffectivePolicy(
        loan_days=loan_days,
        max_renewals=max_renewals,
        hold_pickup_days=hold_pickup_days,
        holdable=holdable,
        max_loans=max_loans,
        max_holds=max_holds,
        daily_overdue_cents=daily_overdue,
        max_overdue_cents=max_overdue,
    )


def work_is_holdable(*, organization, patron, work) -> bool:
    """A work is holdable if any of its editions resolves to a holdable policy."""
    editions = list(work.editions.all())
    if not editions:
        return resolve_policy(organization=organization, patron=patron).holdable
    return any(
        resolve_policy(organization=organization, patron=patron, edition=e).holdable
        for e in editions
    )
