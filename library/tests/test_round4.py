"""Regression tests for the Round 4 fixes (A1-A4, B5-B6, C7-C9, D10-D11)."""

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone

from library.imports import commit_import, stage_import, validate_import
from library.models import (
    Branch,
    Copy,
    DomainEvent,
    Edition,
    Organization,
    OutboxEvent,
    OutboxStatus,
    PatronProfile,
    SearchQueryLog,
    Work,
)
from library.selectors import availability_map_for_works, search_catalog
from library.services import borrow_work, drain_outbox, register_patron, return_loan

pytestmark = pytest.mark.django_db(transaction=True)


def make_catalog():
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    work = Work.objects.create(canonical_title="Dune", slug="dune")
    edition = Edition.objects.create(work=work, isbn_13="9780000000001")
    Copy.objects.create(organization=org, edition=edition, branch=branch, barcode="C1")
    user = get_user_model().objects.create_user(
        username="reader", email="reader@example.test", password="s3cretPass99X"
    )
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number="CARD-1", home_branch=branch
    )
    return org, branch, work, patron


# --------------------------------------------------------------------------- #
# A1 - JSON rows with non-string values do not crash
# --------------------------------------------------------------------------- #
def test_import_coerces_non_string_values():
    org = Organization.objects.create(name="Lib", slug="lib")
    rows = [{"title": 12345, "publication_year": 1969, "authors": ["Jane", "Joe"]}]
    batch = stage_import(organization=org, rows=rows)
    validate_import(batch=batch)
    row = batch.rows.get()
    assert row.parsed_fields["title"] == "12345"
    assert row.parsed_fields["authors"] == ["Jane", "Joe"]
    assert not row.validation_errors


# --------------------------------------------------------------------------- #
# A2 - return receipt recipient must not persist in the durable DomainEvent
# --------------------------------------------------------------------------- #
def test_return_recipient_not_in_domain_event():
    _org, branch, work, patron = make_catalog()
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    loan = patron.loans.get()
    return_loan(loan=loan, actor=patron.user)

    event = DomainEvent.objects.get(event_type="loan.returned")
    assert "recipient" not in event.payload  # scrubbed out of the permanent record
    outbox = OutboxEvent.objects.get(event_type="loan.returned")
    assert outbox.payload.get("recipient") == "reader@example.test"  # available for delivery
    drain_outbox()
    from django.core import mail

    assert any(m.subject.startswith("Returned:") for m in mail.outbox)


# --------------------------------------------------------------------------- #
# A3 - a duplicate-slug clash fails one row, not the whole commit
# --------------------------------------------------------------------------- #
def test_commit_isolates_integrity_error_per_row(monkeypatch):
    from django.db import IntegrityError

    import library.imports as imports_mod
    from library.models import CatalogImportStatus

    org = Organization.objects.create(name="Lib", slug="lib")
    rows = [{"title": "Good One", "slug": "good-one"}, {"title": "Bad One", "slug": "bad-one"}]
    batch = stage_import(organization=org, rows=rows)
    validate_import(batch=batch)

    # Simulate a global-uniqueness race that only surfaces at commit for one row.
    real_commit_row = imports_mod._commit_row

    def fake_commit_row(organization, parsed):
        if parsed["slug"] == "bad-one":
            raise IntegrityError("simulated slug clash")
        return real_commit_row(organization, parsed)

    monkeypatch.setattr(imports_mod, "_commit_row", fake_commit_row)
    commit_import(batch=batch)
    batch.refresh_from_db()

    assert batch.status == CatalogImportStatus.COMMITTED
    assert batch.validation_summary["commit_errors"] == 1
    assert Work.objects.filter(slug="good-one").exists()  # the good row survived
    assert not Work.objects.filter(slug="bad-one").exists()  # the bad row rolled back


# --------------------------------------------------------------------------- #
# A4 - a non-card IntegrityError is not misreported as a card collision
# --------------------------------------------------------------------------- #
def test_register_patron_reraises_duplicate_profile():
    org, branch, _work, patron = make_catalog()
    # patron.user already has a profile; registering again must raise the real
    # OneToOne error, not loop into "could not allocate card number".
    with pytest.raises(Exception) as exc:
        register_patron(user=patron.user, organization=org, home_branch=branch)
    assert "card number" not in str(exc.value).lower()


# --------------------------------------------------------------------------- #
# B5 - registration is rate limited
# --------------------------------------------------------------------------- #
@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
def test_registration_is_rate_limited(client):
    Organization.objects.create(name="Solo", slug="solo")
    statuses = []
    for i in range(12):
        resp = client.post(
            "/accounts/register/",
            {"username": f"u{i}", "password1": "x", "password2": "y"},
            secure=True,
        )
        statuses.append(resp.status_code)
    assert 429 in statuses  # limiter kicks in within the window


# --------------------------------------------------------------------------- #
# C7 - batched availability map matches per-work availability
# --------------------------------------------------------------------------- #
def test_availability_map_for_works():
    org, branch, work, _patron = make_catalog()
    amap = availability_map_for_works(org, [work.id])
    assert amap[work.id]["available"] == 1
    assert amap[work.id]["total"] == 1
    # Unknown ids get a zeroed entry.
    assert availability_map_for_works(org, [99999])[99999]["total"] == 0


# --------------------------------------------------------------------------- #
# C8 - empty and cursor searches are not logged
# --------------------------------------------------------------------------- #
def test_empty_and_paged_searches_not_logged():
    org, branch, work, _patron = make_catalog()
    search_catalog(organization=org, query="")
    assert SearchQueryLog.objects.count() == 0
    search_catalog(organization=org, query="dune")
    assert SearchQueryLog.objects.count() == 1


# --------------------------------------------------------------------------- #
# C9 - prune_logs deletes aged rows
# --------------------------------------------------------------------------- #
def test_prune_logs_removes_aged_rows():
    from datetime import timedelta

    from django.core.management import call_command

    org = Organization.objects.create(name="Lib", slug="lib")
    old = SearchQueryLog.objects.create(organization=org, query="old")
    SearchQueryLog.objects.filter(pk=old.pk).update(
        created_at=timezone.now() - timedelta(days=200)
    )
    recent = SearchQueryLog.objects.create(organization=org, query="recent")
    call_command("prune_logs", "--search-days", "90")
    assert not SearchQueryLog.objects.filter(pk=old.pk).exists()
    assert SearchQueryLog.objects.filter(pk=recent.pk).exists()


# --------------------------------------------------------------------------- #
# D10 - readiness probe returns 200 when DB + cache are up
# --------------------------------------------------------------------------- #
def test_readyz_ok(client):
    resp = client.get("/readyz/", secure=True)
    assert resp.status_code == 200
    assert b"ready" in resp.content


# --------------------------------------------------------------------------- #
# D11 - failed outbox events surface on the dashboard selector
# --------------------------------------------------------------------------- #
def test_dashboard_reports_failed_outbox():
    from library.selectors import get_librarian_dashboard

    org, branch, work, _patron = make_catalog()
    OutboxEvent.objects.create(organization=org, event_type="x", status=OutboxStatus.FAILED)
    dashboard = get_librarian_dashboard(org)
    assert dashboard["failed_outbox_events"] == 1
