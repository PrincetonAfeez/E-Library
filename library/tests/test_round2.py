"""Regression tests for the Round 2 fixes (A1-A5, B6-B10, C13-C18)."""


import pytest
from django.contrib.auth import get_user_model
from django.core import mail

from library.imports import commit_import, rollback_import, stage_import, validate_import
from library.models import (
    Branch,
    Copy,
    Edition,
    Organization,
    PatronProfile,
    SearchQueryLog,
    ShelfLocation,
    Work,
)
from library.selectors import search_catalog
from library.services import (
    DomainError,
    borrow_work,
    drain_outbox,
    renew_loan,
    return_loan,
)

pytestmark = pytest.mark.django_db(transaction=True)


def make_catalog(retain_history=False, max_renewals=2):
    org = Organization.objects.create(name="Lib", slug="lib")
    branch = Branch.objects.create(organization=org, name="Main", slug="main", max_renewals=max_renewals)
    ShelfLocation.objects.create(branch=branch, code="FIC", name="Fiction")
    work = Work.objects.create(canonical_title="Cats & Dogs", slug="cats-dogs")
    edition = Edition.objects.create(work=work, isbn_13="9780000000001")
    Copy.objects.create(organization=org, edition=edition, branch=branch, barcode="C1")
    user = get_user_model().objects.create_user(
        username="reader", email="reader@example.test", password="s3cretPass99X"
    )
    patron = PatronProfile.objects.create(
        user=user,
        organization=org,
        library_card_number="CARD-1",
        home_branch=branch,
        retain_loan_history=retain_history,
    )
    return org, branch, work, patron


# --------------------------------------------------------------------------- #
# A1 - non-patron users must not 500 on circulation views
# --------------------------------------------------------------------------- #
def test_non_patron_user_redirected_not_500(client):
    Organization.objects.create(name="Lib", slug="lib")
    staff = get_user_model().objects.create_user(
        username="admin", password="s3cretPass99X", is_staff=True
    )
    client.force_login(staff)
    resp = client.get("/account/", secure=True)
    assert resp.status_code == 302  # redirected to catalog, not a 500
    resp2 = client.get("/works/anything/borrow/", secure=True)  # GET on POST view
    assert resp2.status_code in (302, 405)


# --------------------------------------------------------------------------- #
# A2 - notification emails are not HTML-escaped
# --------------------------------------------------------------------------- #
def test_notification_email_not_html_escaped():
    _org, branch, work, patron = make_catalog()
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    drain_outbox()
    assert len(mail.outbox) == 1
    assert "Cats & Dogs" in mail.outbox[0].subject
    assert "&amp;" not in mail.outbox[0].body


# --------------------------------------------------------------------------- #
# A4 - repeatable events (2nd renewal) still notify
# --------------------------------------------------------------------------- #
def test_second_renewal_still_notifies():
    _org, branch, work, patron = make_catalog(max_renewals=3)
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    loan = patron.loans.get()
    renew_loan(loan=loan, actor=patron.user)
    renew_loan(loan=loan, actor=patron.user)
    drain_outbox()
    renewed = [m for m in mail.outbox if m.subject.startswith("Renewed:")]
    assert len(renewed) == 2  # both renewals notified, not suppressed


# --------------------------------------------------------------------------- #
# A5 - malformed rows payload is a domain error, not a 500
# --------------------------------------------------------------------------- #
def test_stage_import_rejects_non_dict_rows():
    org = Organization.objects.create(name="Lib", slug="lib")
    with pytest.raises(DomainError):
        stage_import(organization=org, rows=["not-a-dict", 1])


# --------------------------------------------------------------------------- #
# A3 - empty-slug titles are flagged
# --------------------------------------------------------------------------- #
def test_import_flags_empty_slug_title():
    org = Organization.objects.create(name="Lib", slug="lib")
    batch = stage_import(organization=org, rows=[{"title": "!!!"}])
    validate_import(batch=batch)
    row = batch.rows.get()
    assert any("slug" in e for e in row.validation_errors)


# --------------------------------------------------------------------------- #
# B6 - non-UTF-8 CSV does not crash
# --------------------------------------------------------------------------- #
def test_parse_rows_handles_latin1_bytes():
    from library.imports import parse_rows_from_csv

    content = "title\nCaf\xe9 Life\n".encode("latin-1")
    rows = parse_rows_from_csv(content)
    assert rows[0]["title"] == "Café Life"


# --------------------------------------------------------------------------- #
# B8 - return receipt is delivered even after patron scrub
# --------------------------------------------------------------------------- #
def test_return_receipt_sent_after_patron_scrub():
    _org, branch, work, patron = make_catalog(retain_history=False)
    borrow_work(patron=patron, work=work, branch=branch, actor=patron.user)
    loan = patron.loans.get()
    return_loan(loan=loan, actor=patron.user)
    loan.refresh_from_db()
    assert loan.patron is None  # scrubbed
    drain_outbox()
    returned = [m for m in mail.outbox if m.subject.startswith("Returned:")]
    assert len(returned) == 1
    assert returned[0].to == ["reader@example.test"]


# --------------------------------------------------------------------------- #
# B10 - intra-batch duplicate barcode is flagged
# --------------------------------------------------------------------------- #
def test_duplicate_barcode_within_batch_flagged():
    org = Organization.objects.create(name="Lib", slug="lib")
    Branch.objects.create(organization=org, name="Main", slug="main")
    rows = [
        {"title": "Book A", "branch": "main", "barcode": "DUP-1"},
        {"title": "Book B", "branch": "main", "barcode": "DUP-1"},
    ]
    batch = stage_import(organization=org, rows=rows)
    validate_import(batch=batch)
    batch.refresh_from_db()
    assert batch.error_count == 1  # the second row is flagged


# --------------------------------------------------------------------------- #
# C12 - unknown columns are reported
# --------------------------------------------------------------------------- #
def test_unknown_columns_reported():
    org = Organization.objects.create(name="Lib", slug="lib")
    batch = stage_import(organization=org, rows=[{"title": "X", "weird_col": "y"}])
    validate_import(batch=batch)
    batch.refresh_from_db()
    assert "weird_col" in batch.validation_summary["unknown_columns"]


# --------------------------------------------------------------------------- #
# C13 - rollback detaches authors added to a pre-existing work
# --------------------------------------------------------------------------- #
def test_rollback_detaches_added_authors_from_existing_work():
    org = Organization.objects.create(name="Lib", slug="lib")
    work = Work.objects.create(canonical_title="Existing", slug="existing")
    rows = [{"title": "Existing", "slug": "existing", "authors": "Brand New Author"}]
    batch = stage_import(organization=org, rows=rows)
    validate_import(batch=batch)
    commit_import(batch=batch)
    assert work.authors.filter(name="Brand New Author").exists()

    rollback_import(batch=batch, reason="undo")
    assert not work.authors.filter(name="Brand New Author").exists()
    # The pre-existing work itself must survive.
    assert Work.objects.filter(slug="existing").exists()


# --------------------------------------------------------------------------- #
# C17 - search log captures a requester hash
# --------------------------------------------------------------------------- #
def test_search_log_records_requester_hash():
    org, branch, work, _patron = make_catalog()
    # A non-empty query is logged (empty/keystroke searches are not — see C8).
    search_catalog(organization=org, query="cats", requester_hash="abc123")
    log = SearchQueryLog.objects.latest("created_at")
    assert log.user_or_session_hash == "abc123"


# --------------------------------------------------------------------------- #
# B7 - multi-tenant registration requires an org choice
# --------------------------------------------------------------------------- #
def test_registration_requires_org_when_multiple_tenants(client):
    Organization.objects.create(name="Alpha", slug="alpha")
    Organization.objects.create(name="Beta", slug="beta")
    # No ?org -> the form must require an organization choice; omitting it fails.
    resp = client.post(
        "/accounts/register/",
        {
            "username": "multi",
            "email": "multi@example.test",
            "password1": "s3cretPass99X",
            "password2": "s3cretPass99X",
        },
        secure=True,
    )
    assert resp.status_code == 200  # re-rendered with errors, not created
    assert not get_user_model().objects.filter(username="multi").exists()


def test_registration_binds_to_chosen_org(client):
    alpha = Organization.objects.create(name="Alpha", slug="alpha")
    Organization.objects.create(name="Beta", slug="beta")
    resp = client.post(
        "/accounts/register/",
        {
            "username": "multi",
            "email": "multi@example.test",
            "password1": "s3cretPass99X",
            "password2": "s3cretPass99X",
            "organization": alpha.pk,
        },
        secure=True,
    )
    assert resp.status_code == 302
    profile = PatronProfile.objects.get(user__username="multi")
    assert profile.organization == alpha
