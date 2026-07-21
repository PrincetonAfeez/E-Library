"""Collection-development analytics and BI export (Increment 16).
 
Turns raw circulation into the numbers that drive purchasing and reporting:
collection turnover, holds-to-copies pressure (→ purchase suggestions), a
circulation time series, and a compact BI export.
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import (
    Copy,
    CopyStatus,
    Hold,
    HoldStatus,
    Loan,
    LoanStatus,
    PublicStatus,
    Work,
)

ACTIVE_LOAN_STATUSES = [LoanStatus.ACTIVE, LoanStatus.OVERDUE, LoanStatus.RETURNED]


def _copies_for_work(organization):
    return Copy.objects.filter(organization=organization).exclude(status=CopyStatus.RETIRED)


def collection_turnover(organization, *, days: int = 365, limit: int = 50) -> list[dict]:
    """Per-title turnover: loans in the window ÷ owned copies (higher = hotter)."""
    since = timezone.now() - timedelta(days=days)
    works = (
        Work.objects.filter(editions__copies__organization=organization)
        .distinct()
        .annotate(
            copies=Count(
                "editions__copies",
                filter=Q(editions__copies__organization=organization)
                & ~Q(editions__copies__status=CopyStatus.RETIRED),
                distinct=True,
            ),
            loans=Count(
                "editions__copies__loans",
                filter=Q(editions__copies__organization=organization)
                & Q(editions__copies__loans__borrowed_at__gte=since),
                distinct=True,
            ),
        )
        .filter(copies__gt=0)
    )
    rows = [
        {
            "work_id": w.pk,
            "title": w.canonical_title,
            "copies": w.copies,
            "loans": w.loans,
            "turnover": round(w.loans / w.copies, 2) if w.copies else 0.0,
        }
        for w in works
    ]
    rows.sort(key=lambda r: r["turnover"], reverse=True)
    return rows[:limit]


def purchase_suggestions(organization, *, ratio_threshold: float = 2.0, limit: int = 50) -> list[dict]:
    """Titles under demand pressure: (active holds) ÷ (available copies) ≥ threshold.

    A classic collection-development signal — high holds-to-copies means buy more.
    """
    works = (
        Work.objects.filter(holds__organization=organization)
        .distinct()
        .annotate(
            active_holds=Count(
                "holds",
                filter=Q(holds__organization=organization)
                & Q(holds__status__in=[HoldStatus.WAITING, HoldStatus.READY]),
                distinct=True,
            ),
            owned_copies=Count(
                "editions__copies",
                filter=Q(editions__copies__organization=organization)
                & ~Q(editions__copies__status=CopyStatus.RETIRED),
                distinct=True,
            ),
        )
        .filter(active_holds__gt=0)
    )
    rows = []
    for w in works:
        copies = w.owned_copies or 0
        # Divide by copies+? — treat 0 copies as infinite pressure (ratio = holds).
        ratio = w.active_holds / copies if copies else float(w.active_holds)
        if ratio >= ratio_threshold:
            rows.append({
                "work_id": w.pk,
                "title": w.canonical_title,
                "active_holds": w.active_holds,
                "owned_copies": copies,
                "ratio": round(ratio, 2),
                "suggested_copies": max(1, round(w.active_holds / ratio_threshold) - copies),
            })
    rows.sort(key=lambda r: r["ratio"], reverse=True)
    return rows[:limit]


def circulation_timeseries(organization, *, days: int = 30) -> list[dict]:
    """Daily checkout counts over the window (for dashboard charts)."""
    since = timezone.now() - timedelta(days=days)
    rows = (
        Loan.objects.filter(organization=organization, borrowed_at__gte=since)
        .annotate(day=TruncDate("borrowed_at"))
        .values("day")
        .annotate(count=Count("id"))
        .order_by("day")
    )
    return [{"date": r["day"].isoformat(), "checkouts": r["count"]} for r in rows]


def bi_export(organization) -> dict:
    """Compact snapshot of headline metrics for a BI/warehouse pull."""
    copies = _copies_for_work(organization)
    return {
        "generated_at": timezone.now().isoformat(),
        "titles": Work.objects.filter(
            editions__copies__organization=organization, public_status=PublicStatus.PUBLISHED
        ).distinct().count(),
        "copies": copies.count(),
        "active_loans": Loan.objects.filter(
            organization=organization, status__in=[LoanStatus.ACTIVE, LoanStatus.OVERDUE]
        ).count(),
        "overdue_loans": Loan.objects.filter(
            organization=organization, status=LoanStatus.OVERDUE
        ).count(),
        "open_holds": Hold.objects.filter(
            organization=organization, status__in=[HoldStatus.WAITING, HoldStatus.READY]
        ).count(),
        "available_copies": copies.filter(status=CopyStatus.AVAILABLE).count(),
    }
