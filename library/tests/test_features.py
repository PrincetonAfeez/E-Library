"""Tests for E1 (catalog import), E2 (overrides/templates), E3 (registration)."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from library.imports import (
    commit_import,
    rollback_import,
    stage_import,
    validate_import,
)
from library.models import (
    Author,
    Branch,
    CatalogImportStatus,
    Copy,
    CopyStatus,
    Edition,
    LibrarianOverride,
    Loan,
    NotificationTemplate,
    Organization,
    PatronProfile,
    ShelfLocation,
    StaffMembership,
    StaffRole,
    Subject,
    Work,
)
from library.notifications import ensure_default_templates
from library.services import DomainError, borrow_work, register_patron

pytestmark = pytest.mark.django_db(transaction=True)


def make_org():
    org = Organization.objects.create(name="Test Library", slug="test")
    branch = Branch.objects.create(organization=org, name="Main", slug="main")
    ShelfLocation.objects.create(branch=branch, code="FIC", name="Fiction")
    return org, branch


# --------------------------------------------------------------------------- #
# E3 - patron self-registration
# --------------------------------------------------------------------------- #
def test_register_patron_service_creates_profile():
    org, branch = make_org()
    user = get_user_model().objects.create_user(username="newbie", email="n@example.test")
    profile = register_patron(user=user, organization=org, home_branch=branch)
    assert profile.organization == org
    assert profile.library_card_number
    assert profile.notification_email == "n@example.test"


def test_register_view_creates_account_and_logs_in(client):
    org, branch = make_org()
    response = client.post(
        "/accounts/register/",
        {
            "username": "webuser",
            "email": "web@example.test",
            "password1": "s3cretPass99X",
            "password2": "s3cretPass99X",
            "home_branch": branch.pk,
        },
        secure=True,  # production settings enforce HTTPS (SECURE_SSL_REDIRECT)
    )
    assert response.status_code == 302
    user = get_user_model().objects.get(username="webuser")
    assert PatronProfile.objects.filter(user=user, organization=org).exists()
    # Session cookie present -> logged in.
    assert "_auth_user_id" in client.session


def test_card_numbers_are_unique_per_org():
    org, _branch = make_org()
    numbers = set()
    for i in range(5):
        user = get_user_model().objects.create_user(username=f"u{i}")
        profile = register_patron(user=user, organization=org)
        numbers.add(profile.library_card_number)
    assert len(numbers) == 5


# --------------------------------------------------------------------------- #
# E2 - librarian override + notification templates
# --------------------------------------------------------------------------- #
def _catalog_with_two_copies(org, branch):
    work = Work.objects.create(canonical_title="Dune", slug="dune")
    edition = Edition.objects.create(work=work, isbn_13="9780000000123")
    Copy.objects.create(organization=org, edition=edition, branch=branch, barcode="C1")
    Copy.objects.create(organization=org, edition=edition, branch=branch, barcode="C2")
    return work


def test_override_allows_duplicate_borrow_and_records_override():
    org, branch = make_org()
    work = _catalog_with_two_copies(org, branch)
    user = get_user_model().objects.create_user(username="reader")
    staff = get_user_model().objects.create_user(username="liv", is_staff=True)
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number="CARD-1", home_branch=branch
    )
    borrow_work(patron=patron, work=work, branch=branch, actor=staff)

    # Without override, a second borrow of the same work is blocked.
    with pytest.raises(DomainError):
        borrow_work(patron=patron, work=work, branch=branch, actor=staff)

    # With an override reason (and staff actor) it succeeds and is recorded.
    loan = borrow_work(
        patron=patron, work=work, branch=branch, actor=staff, override_reason="lost first copy"
    )
    assert loan.status == "active"
    assert LibrarianOverride.objects.filter(
        organization=org, entity_type="Loan", entity_id=str(loan.pk)
    ).exists()


def test_override_requires_staff_actor():
    org, branch = make_org()
    work = _catalog_with_two_copies(org, branch)
    user = get_user_model().objects.create_user(username="reader")
    patron = PatronProfile.objects.create(
        user=user, organization=org, library_card_number="CARD-1", home_branch=branch
    )
    with pytest.raises(DomainError):
        borrow_work(patron=patron, work=work, branch=branch, override_reason="no actor")


def test_ensure_default_templates_is_idempotent():
    org, _branch = make_org()
    first = ensure_default_templates(org)
    second = ensure_default_templates(org)
    assert first > 0
    assert second == 0
    assert NotificationTemplate.objects.filter(organization=org).count() == first


# --------------------------------------------------------------------------- #
# E1 - catalog import pipeline
# --------------------------------------------------------------------------- #
def test_validate_flags_errors_and_valid_rows():
    org, _branch = make_org()
    rows = [
        {"title": "Good Book", "authors": "Jane Doe", "subjects": "Fiction"},
        {"title": "", "authors": "No Title"},  # missing title
        {"title": "Bad Year", "publication_year": "nineteen"},  # bad year
        {"title": "Bad Branch", "branch": "nope", "barcode": "X1"},  # unknown branch
    ]
    batch = stage_import(organization=org, rows=rows)
    validate_import(batch=batch)
    batch.refresh_from_db()
    assert batch.status == CatalogImportStatus.VALIDATED
    assert batch.error_count == 3
    assert batch.validation_summary["valid_rows"] == 1


def test_commit_creates_catalog_records_and_reindexes():
    org, branch = make_org()
    rows = [
        {
            "title": "The Dispossessed",
            "subtitle": "An Ambiguous Utopia",
            "authors": "Ursula K. Le Guin",
            "subjects": "Science Fiction",
            "isbn_13": "9780061054884",
            "publisher": "Harper",
            "publication_year": "1974",
            "format": "paperback",
            "branch": "main",
            "barcode": "ML-0001",
            "shelf_code": "FIC",
        }
    ]
    batch = stage_import(organization=org, rows=rows)
    validate_import(batch=batch)
    commit_import(batch=batch, actor=None)
    batch.refresh_from_db()

    assert batch.status == CatalogImportStatus.COMMITTED
    work = Work.objects.get(slug="the-dispossessed")
    assert Author.objects.filter(name="Ursula K. Le Guin").exists()
    assert Subject.objects.filter(name="Science Fiction").exists()
    edition = Edition.objects.get(isbn_13="9780061054884")
    assert edition.work == work
    copy = Copy.objects.get(organization=org, barcode="ML-0001")
    assert copy.branch == branch
    assert copy.shelf_location is not None
    # Reindex ran -> the work has a search document.
    assert hasattr(work, "search_row")


def test_commit_blocked_when_errors_present():
    org, _branch = make_org()
    batch = stage_import(organization=org, rows=[{"title": ""}])
    validate_import(batch=batch)
    with pytest.raises(DomainError):
        commit_import(batch=batch, actor=None)


def test_commit_matches_existing_records_without_duplicating():
    org, branch = make_org()
    work = Work.objects.create(canonical_title="Existing", slug="existing")
    Edition.objects.create(work=work, isbn_13="9781111111116")
    rows = [{"title": "Existing", "slug": "existing", "isbn_13": "9781111111116"}]
    batch = stage_import(organization=org, rows=rows)
    validate_import(batch=batch)
    commit_import(batch=batch, actor=None)
    assert Work.objects.filter(slug="existing").count() == 1
    assert Edition.objects.filter(isbn_13="9781111111116").count() == 1


def test_rollback_committed_batch_reverses_created_records():
    org, branch = make_org()
    rows = [
        {
            "title": "Ephemeral",
            "isbn_13": "9782222222223",
            "branch": "main",
            "barcode": "ML-ROLL",
        }
    ]
    batch = stage_import(organization=org, rows=rows)
    validate_import(batch=batch)
    commit_import(batch=batch, actor=None)
    assert Copy.objects.filter(barcode="ML-ROLL").exists()

    rollback_import(batch=batch, reason="mistake")
    batch.refresh_from_db()
    assert batch.status == CatalogImportStatus.ROLLED_BACK
    assert not Copy.objects.filter(barcode="ML-ROLL").exists()
    assert not Work.objects.filter(slug="ephemeral").exists()


def test_librarian_import_ui_upload_validate_commit(client):
    org, branch = make_org()
    librarian = get_user_model().objects.create_user(
        username="liv", password="s3cretPass99X", is_staff=True
    )
    # Imports require the branch-manager/admin capability (a plain librarian
    # cannot import — see the RBAC map).
    StaffMembership.objects.create(
        user=librarian, organization=org, branch=branch, role=StaffRole.BRANCH_MANAGER
    )
    client.force_login(librarian)

    # The upload/list page renders.
    listing = client.get("/librarian/imports/", secure=True)
    assert listing.status_code == 200

    csv_bytes = (
        b"title,authors,isbn_13,branch,barcode,shelf_code\n"
        b"UI Imported Book,Jane Doe,9783333333330,main,UI-0001,FIC\n"
    )
    upload = SimpleUploadedFile("catalog.csv", csv_bytes, content_type="text/csv")
    response = client.post("/librarian/imports/", {"csv_file": upload}, secure=True)
    assert response.status_code == 302  # redirect to the batch detail

    batch = org.import_batches.get()
    assert batch.status == CatalogImportStatus.VALIDATED
    assert batch.error_count == 0

    # The batch detail page renders with the commit action.
    detail = client.get(f"/librarian/imports/{batch.pk}/", secure=True)
    assert detail.status_code == 200
    assert b"UI Imported Book" in detail.content

    commit = client.post(
        f"/librarian/imports/{batch.pk}/commit/", {}, secure=True
    )
    assert commit.status_code == 302
    assert Copy.objects.filter(organization=org, barcode="UI-0001").exists()
    assert Work.objects.filter(slug="ui-imported-book").exists()


def test_librarian_import_ui_denies_non_staff(client):
    make_org()
    user = get_user_model().objects.create_user(username="plain", password="s3cretPass99X")
    client.force_login(user)
    response = client.get("/librarian/imports/", secure=True)
    assert response.status_code == 403


def test_rollback_skips_copy_with_active_loan():
    org, branch = make_org()
    rows = [{"title": "Popular", "branch": "main", "barcode": "ML-LOAN"}]
    batch = stage_import(organization=org, rows=rows)
    validate_import(batch=batch)
    commit_import(batch=batch, actor=None)
    copy = Copy.objects.get(barcode="ML-LOAN")
    Loan.objects.create(
        organization=org,
        copy=copy,
        due_at=timezone.now() + timedelta(days=21),
    )
    copy.status = CopyStatus.LOANED
    copy.save(update_fields=["status"])

    rollback_import(batch=batch, reason="cleanup")
    # The loaned copy (and therefore its work) must survive the rollback.
    assert Copy.objects.filter(barcode="ML-LOAN").exists()
    batch.refresh_from_db()
    assert batch.validation_summary["rollback"]["skipped"] >= 1
