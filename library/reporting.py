"""Reporting & analytics selectors.

Pure, organization-scoped query functions over the operational data already
captured (loans, holds, copies, fees, payments, search logs). Each returns
JSON-serializable primitives so it can back both the HTML dashboard, the REST
API, and emailed digests.
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Avg, Count, Sum
from django.utils import timezone

from .models import (
    Copy,
    Fee,
    FeeStatus,
    Hold,
    HoldStatus,
    Loan,
    LoanStatus,
    Payment,
    Renewal,
    SearchQueryLog,
)


def default_window(days: int = 30):
    end = timezone.now()
    return end - timedelta(days=days), end


def circulation_summary(organization, start, end) -> dict:
    loans = Loan.objects.filter(organization=organization)
    return {
        "borrowed": loans.filter(borrowed_at__range=(start, end)).count(),
        "returned": loans.filter(returned_at__range=(start, end)).count(),
        "renewals": Renewal.objects.filter(
            loan__organization=organization, created_at__range=(start, end)
        ).count(),
        "active_now": loans.filter(status__in=[LoanStatus.ACTIVE, LoanStatus.OVERDUE]).count(),
        "overdue_now": loans.filter(status=LoanStatus.OVERDUE).count(),
        "unique_borrowers": loans.filter(borrowed_at__range=(start, end))
        .values("patron")
        .distinct()
        .count(),
    }


def popular_titles(organization, start, end, limit: int = 10) -> list[dict]:
    rows = (
        Loan.objects.filter(organization=organization, borrowed_at__range=(start, end))
        .values("copy__edition__work", "copy__edition__work__canonical_title")
        .annotate(count=Count("id"))
        .order_by("-count")[:limit]
    )
    return [
        {"work_id": r["copy__edition__work"], "title": r["copy__edition__work__canonical_title"], "loans": r["count"]}
        for r in rows
    ]


def collection_stats(organization) -> dict:
    copies = Copy.objects.filter(organization=organization)
    by_status = {r["status"]: r["count"] for r in copies.values("status").annotate(count=Count("id"))}
    by_branch = [
        {"branch": r["branch__name"], "count": r["count"]}
        for r in copies.values("branch__name").annotate(count=Count("id")).order_by("-count")
    ]
    by_format = [
        {"format": r["edition__format"], "count": r["count"]}
        for r in copies.values("edition__format").annotate(count=Count("id")).order_by("-count")
    ]
    return {
        "total_copies": copies.count(),
        "total_works": copies.values("edition__work").distinct().count(),
        "total_editions": copies.values("edition").distinct().count(),
        "by_status": by_status,
        "by_branch": by_branch,
        "by_format": by_format,
    }


def overdue_aging(organization, now=None) -> dict:
    now = now or timezone.now()
    buckets = {"1-7": 0, "8-30": 0, "31-90": 0, "90+": 0}
    for due_at in Loan.objects.filter(
        organization=organization, status=LoanStatus.OVERDUE
    ).values_list("due_at", flat=True):
        days = (now.date() - due_at.date()).days
        if days <= 7:
            buckets["1-7"] += 1
        elif days <= 30:
            buckets["8-30"] += 1
        elif days <= 90:
            buckets["31-90"] += 1
        else:
            buckets["90+"] += 1
    return buckets


def holds_stats(organization, start, end) -> dict:
    holds = Hold.objects.filter(organization=organization)
    placed = holds.filter(created_at__range=(start, end)).count()
    fulfilled = holds.filter(status=HoldStatus.FULFILLED, updated_at__range=(start, end)).count()
    expired = holds.filter(status=HoldStatus.EXPIRED, updated_at__range=(start, end)).count()
    return {
        "waiting_now": holds.filter(status=HoldStatus.WAITING).count(),
        "ready_now": holds.filter(status=HoldStatus.READY).count(),
        "placed": placed,
        "fulfilled": fulfilled,
        "expired": expired,
        "fill_rate": round(fulfilled / (fulfilled + expired), 3) if (fulfilled + expired) else None,
    }


def fines_summary(organization, start, end) -> dict:
    assessed = (
        Fee.objects.filter(organization=organization, created_at__range=(start, end)).aggregate(
            total=Sum("amount_cents")
        )["total"]
        or 0
    )
    collected = (
        Payment.objects.filter(organization=organization, created_at__range=(start, end)).aggregate(
            total=Sum("amount_cents")
        )["total"]
        or 0
    )
    outstanding = 0
    for fee in Fee.objects.filter(organization=organization).exclude(status=FeeStatus.WAIVED):
        outstanding += max(0, fee.amount_cents - fee.paid_cents)
    return {
        "assessed_cents": assessed,
        "collected_cents": collected,
        "outstanding_cents": outstanding,
    }


def branch_activity(organization, start, end) -> list[dict]:
    rows = (
        Loan.objects.filter(organization=organization, borrowed_at__range=(start, end))
        .values("copy__branch__name")
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    return [{"branch": r["copy__branch__name"], "loans": r["count"]} for r in rows]


def search_analytics(organization, start, end, limit: int = 10) -> dict:
    logs = SearchQueryLog.objects.filter(organization=organization, created_at__range=(start, end))
    top = (
        logs.exclude(query="")
        .values("query")
        .annotate(count=Count("id"))
        .order_by("-count")[:limit]
    )
    return {
        "volume": logs.count(),
        "zero_result": logs.filter(result_count=0).count(),
        "avg_latency_ms": round(logs.aggregate(a=Avg("latency_ms"))["a"] or 0, 1),
        "top_queries": [{"query": r["query"], "count": r["count"]} for r in top],
    }


REPORTS = {
    "circulation": circulation_summary,
    "popular": popular_titles,
    "collection": lambda org, start, end: collection_stats(org),
    "overdue": lambda org, start, end: overdue_aging(org),
    "holds": holds_stats,
    "fines": fines_summary,
    "branches": branch_activity,
    "search": search_analytics,
}


def build_report(report_type: str, organization, start, end):
    fn = REPORTS.get(report_type)
    if fn is None:
        return None
    return fn(organization, start, end)


def dashboard_report(organization, days: int = 30) -> dict:
    start, end = default_window(days)
    return {
        "window_days": days,
        "circulation": circulation_summary(organization, start, end),
        "collection": collection_stats(organization),
        "popular": popular_titles(organization, start, end, limit=10),
        "overdue_aging": overdue_aging(organization),
        "holds": holds_stats(organization, start, end),
        "fines": fines_summary(organization, start, end),
        "branches": branch_activity(organization, start, end),
        "search": search_analytics(organization, start, end),
    }
