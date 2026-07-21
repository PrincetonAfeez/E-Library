"""Discovery/social features: reviews, ratings, reading lists, recommendations."""
 
from __future__ import annotations

from django.db.models import Avg, Count

from .models import Loan, PublicStatus, ReadingList, Review, Work
from .services import DomainError, audit_action


def submit_review(*, patron, work, rating: int, body: str = "", actor=None) -> Review:
    if not 1 <= int(rating) <= 5:
        raise DomainError("Rating must be between 1 and 5.")
    review, _ = Review.objects.update_or_create(
        patron=patron,
        work=work,
        defaults={"organization": patron.organization, "rating": rating, "body": body},
    )
    audit_action(action="review.submit", entity=review, actor=actor or patron.user, source="web")
    return review


def delete_review(*, review, actor=None) -> None:
    review.delete()


def work_rating(work) -> dict:
    agg = Review.objects.filter(work=work, public=True).aggregate(
        average=Avg("rating"), count=Count("id")
    )
    return {
        "average": round(agg["average"], 2) if agg["average"] is not None else None,
        "count": agg["count"],
    }


def work_reviews(work, limit: int = 20):
    return (
        Review.objects.filter(work=work, public=True)
        .select_related("patron__user")
        .order_by("-created_at")[:limit]
    )


def recommendations_for_work(organization, work, limit: int = 6) -> list[dict]:
    """'Readers also borrowed': works most co-borrowed by patrons of this work."""
    borrower_ids = (
        Loan.objects.filter(organization=organization, copy__edition__work=work)
        .exclude(patron__isnull=True)
        .values("patron")
    )
    rows = (
        Loan.objects.filter(organization=organization, patron__in=borrower_ids)
        .exclude(copy__edition__work=work)
        .filter(copy__edition__work__public_status=PublicStatus.PUBLISHED)
        .values(
            "copy__edition__work",
            "copy__edition__work__canonical_title",
            "copy__edition__work__slug",
        )
        .annotate(count=Count("id"))
        .order_by("-count")[:limit]
    )
    return [
        {
            "work_id": r["copy__edition__work"],
            "title": r["copy__edition__work__canonical_title"],
            "slug": r["copy__edition__work__slug"],
            "co_borrows": r["count"],
        }
        for r in rows
    ]


def create_reading_list(*, patron, name: str, public: bool = False) -> ReadingList:
    if not name.strip():
        raise DomainError("A list needs a name.")
    return ReadingList.objects.create(
        organization=patron.organization, patron=patron, name=name.strip(), public=public
    )


def add_to_list(*, reading_list, work: Work) -> ReadingList:
    reading_list.works.add(work)
    return reading_list


def remove_from_list(*, reading_list, work: Work) -> ReadingList:
    reading_list.works.remove(work)
    return reading_list
