"""Tests for collection-development analytics (Increment 16)."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from library import analytics
from library.models import (
    Branch,
    Copy,
    CopyStatus,
    Edition,
    Hold,
    HoldStatus,
    Loan,
    LoanStatus,
    Organization,
    PatronProfile,
    Work,
)

pytestmark = pytest.mark.django_db(transaction=True)


def make_env():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    user = get_user_model().objects.create_user(username="p", email="p@x.test")
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number="C1", home_branch=branch
    )
    return org, branch, patron


def add_work(org, branch, title, slug, isbn):
    work = Work.objects.create(canonical_title=title, slug=slug)
    edition = Edition.objects.create(work=work, isbn_13=isbn)
    copy = Copy.objects.create(organization=org, edition=edition, branch=branch, barcode=f"B{slug}")
    return work, edition, copy


def test_collection_turnover_ranks_hot_titles():
    org, branch, patron = make_env()
    hot, hot_ed, hot_copy = add_work(org, branch, "Hot", "hot", "9780000000001")
    cold, cold_ed, cold_copy = add_work(org, branch, "Cold", "cold", "9780000000002")
    # Three loans for hot, none for cold.
    for _ in range(3):
        Loan.objects.create(
            organization=org, copy=hot_copy, patron=patron,
            due_at=timezone.now() + timedelta(days=7),
            borrowed_at=timezone.now() - timedelta(days=1), status=LoanStatus.RETURNED,
            returned_at=timezone.now(),
        )
    rows = analytics.collection_turnover(org)
    assert rows[0]["title"] == "Hot" and rows[0]["turnover"] == 3.0


def test_purchase_suggestions_flags_high_demand():
    org, branch, patron = make_env()
    work, edition, copy = add_work(org, branch, "Demanded", "dem", "9780000000003")
    # 3 active holds on a single copy -> ratio 3 >= 2.
    for n in range(3):
        u = get_user_model().objects.create_user(username=f"h{n}")
        p = PatronProfile.objects.create(
            user=u, organization=org, library_card_number=f"H{n}", home_branch=branch
        )
        Hold.objects.create(
            organization=org, work=work, patron=p, preferred_branch=branch,
            status=HoldStatus.WAITING,
        )
    rows = analytics.purchase_suggestions(org, ratio_threshold=2.0)
    assert rows and rows[0]["title"] == "Demanded"
    assert rows[0]["active_holds"] == 3 and rows[0]["ratio"] == 3.0


def test_circulation_timeseries_and_bi_export():
    org, branch, patron = make_env()
    work, edition, copy = add_work(org, branch, "T", "t", "9780000000004")
    Loan.objects.create(
        organization=org, copy=copy, patron=patron,
        due_at=timezone.now() + timedelta(days=7), status=LoanStatus.ACTIVE,
    )
    series = analytics.circulation_timeseries(org, days=7)
    assert sum(r["checkouts"] for r in series) == 1

    Copy.objects.filter(pk=copy.pk).update(status=CopyStatus.LOANED)
    bi = analytics.bi_export(org)
    assert bi["copies"] == 1 and bi["active_loans"] == 1 and "generated_at" in bi
