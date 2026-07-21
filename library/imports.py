"""Staged CSV catalog import: stage -> validate -> commit / rollback

The pipeline mirrors the models already present (``CatalogImportBatch`` /
``CatalogImportRow``) and the E-Library spec §8.1:

* **stage**   — persist raw rows against a new batch (status ``staged``).
* **validate** — parse and check every row, record per-row errors and matches
  against existing catalog records, without mutating the catalog.
* **commit**   — transactionally upsert Works/Editions/Authors/Subjects/Copies
  for error-free rows, reindex affected works, and audit the batch.
* **rollback** — cancel a not-yet-committed batch, or reverse a committed batch
  by deleting only the records it created that are still safe to remove.

Rows are plain dicts so the services are unit-testable without file handling;
:func:`parse_rows_from_csv` adapts an uploaded CSV into that shape.
"""

from __future__ import annotations

import csv
import datetime
import io
import logging

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.text import slugify

from . import entitlements
from .models import (
    Author,
    Branch,
    CatalogImportBatch,
    CatalogImportRow,
    CatalogImportStatus,
    Copy,
    CopyCondition,
    Edition,
    EditionFormat,
    Hold,
    Loan,
    ShelfLocation,
    Subject,
    Work,
    normalize_text,
)
from .services import (
    DomainError,
    audit_action,
    emit_domain_event,
    rebuild_work_search_document,
)
from .signals import suspend_search_reindex

logger = logging.getLogger("library")

KNOWN_FORMATS = {choice.value for choice in EditionFormat}
KNOWN_CONDITIONS = {choice.value for choice in CopyCondition}

# Columns recognized in an import row. Everything is optional except ``title``.
KNOWN_COLUMNS = {
    "title",
    "subtitle",
    "slug",
    "summary",
    "authors",
    "subjects",
    "isbn_13",
    "isbn_10",
    "publisher",
    "publication_year",
    "language",
    "format",
    "edition_statement",
    "description",
    "cover_image",
    "branch",
    "barcode",
    "shelf_code",
    "condition",
    "acquisition_date",
    "public_visible",
}


def parse_rows_from_csv(content: str | bytes) -> list[dict]:
    if isinstance(content, bytes):
        # Excel exports are frequently Latin-1/UTF-16 rather than UTF-8; fall
        # back to latin-1 (which never raises) instead of 500-ing on decode.
        try:
            content = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            content = content.decode("latin-1")
    reader = csv.DictReader(io.StringIO(content))
    if reader.fieldnames:
        normalized = [(f or "").strip().lower() for f in reader.fieldnames]
        dupes = {n for n in normalized if normalized.count(n) > 1}
        if dupes:
            logger.warning("CSV import has duplicate columns (last wins): %s", sorted(dupes))
    rows: list[dict] = []
    for raw in reader:
        rows.append({(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k})
    return rows


def _coerce_value(value) -> str:
    """Render an arbitrary JSON cell value as the string the validators expect."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ";".join(_coerce_value(item) for item in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _normalize_row(row: dict) -> dict:
    """Lower-case keys and stringify values so JSON and CSV rows behave alike."""
    return {str(k).strip().lower(): _coerce_value(v) for k, v in row.items() if k}


def _split_multi(value: str) -> list[str]:
    normalized = (value or "").replace("|", ";")
    return [part.strip() for part in normalized.split(";") if part.strip()]


def _parse_bool(value: str, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "t"}


def stage_import(*, organization, rows: list[dict], uploaded_by=None, source_file: str = "") -> CatalogImportBatch:
    """Persist raw rows as a new staged batch."""
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise DomainError("Rows must be a list of objects (mappings of column -> value).")
    normalized = [_normalize_row(row) for row in rows]
    with transaction.atomic():
        batch = CatalogImportBatch.objects.create(
            organization=organization,
            uploaded_by=uploaded_by,
            source_file=source_file or "",
            status=CatalogImportStatus.STAGED,
            row_count=len(normalized),
        )
        CatalogImportRow.objects.bulk_create(
            [
                CatalogImportRow(batch=batch, row_number=index, row_payload=row)
                for index, row in enumerate(normalized, start=1)
            ]
        )
    audit_action(action="import.stage", entity=batch, actor=uploaded_by, source="import")
    return batch


def import_marc(*, organization, content, uploaded_by=None, source_file: str = ""):
    """Parse MARC (binary or MARCXML) into rows and stage+validate a batch."""
    from .marc import marc_rows_from_content

    rows = marc_rows_from_content(content)
    if not rows:
        raise DomainError("No MARC records found in the upload.")
    batch = stage_import(
        organization=organization, rows=rows, uploaded_by=uploaded_by, source_file=source_file
    )
    validate_import(batch=batch)
    return batch


def _validate_row(organization, payload: dict) -> tuple[dict, list[str], dict]:
    """Return ``(parsed_fields, errors, matched_existing)`` for one row."""
    errors: list[str] = []
    parsed: dict = {}
    matched: dict = {}

    title = (payload.get("title") or "").strip()
    if not title:
        errors.append("title is required")
    parsed["title"] = title
    parsed["subtitle"] = (payload.get("subtitle") or "").strip()
    parsed["summary"] = (payload.get("summary") or "").strip()
    parsed["authors"] = _split_multi(payload.get("authors", ""))
    parsed["subjects"] = _split_multi(payload.get("subjects", ""))

    slug = (payload.get("slug") or "").strip() or (slugify(title) if title else "")
    parsed["slug"] = slug
    if title and not slug:
        # e.g. a punctuation-only or non-Latin title slugifies to "" — an empty
        # slug would collide (unique) and break get_absolute_url().
        errors.append("title does not produce a valid slug; provide an explicit 'slug' column")

    year_raw = (payload.get("publication_year") or "").strip()
    if year_raw:
        try:
            parsed["publication_year"] = int(year_raw)
        except ValueError:
            errors.append(f"publication_year '{year_raw}' is not a valid year")
    else:
        parsed["publication_year"] = None

    fmt = (payload.get("format") or "").strip().lower()
    if fmt and fmt not in KNOWN_FORMATS:
        errors.append(f"format '{fmt}' is not recognized")
    parsed["format"] = fmt or EditionFormat.HARDCOVER

    for key in ("isbn_13", "isbn_10"):
        value = (payload.get(key) or "").strip().replace("-", "")
        parsed[key] = value
    if parsed["isbn_13"] and (len(parsed["isbn_13"]) != 13 or not parsed["isbn_13"].isdigit()):
        errors.append("isbn_13 must be 13 digits")
    if parsed["isbn_10"] and len(parsed["isbn_10"]) != 10:
        errors.append("isbn_10 must be 10 characters")

    for key in ("publisher", "language", "edition_statement", "description", "cover_image"):
        parsed[key] = (payload.get(key) or "").strip()
    parsed["language"] = parsed["language"] or "en"

    branch_slug = (payload.get("branch") or "").strip()
    barcode = (payload.get("barcode") or "").strip()
    parsed["branch"] = branch_slug
    parsed["barcode"] = barcode
    parsed["shelf_code"] = (payload.get("shelf_code") or "").strip()
    parsed["public_visible"] = _parse_bool(payload.get("public_visible", ""))

    condition = (payload.get("condition") or "").strip().lower()
    if condition and condition not in KNOWN_CONDITIONS:
        errors.append(f"condition '{condition}' is not recognized")
    parsed["condition"] = condition or CopyCondition.GOOD

    acquisition_raw = (payload.get("acquisition_date") or "").strip()
    parsed["acquisition_date"] = None
    if acquisition_raw:
        try:
            parsed["acquisition_date"] = datetime.date.fromisoformat(acquisition_raw)
        except ValueError:
            errors.append(f"acquisition_date '{acquisition_raw}' must be YYYY-MM-DD")

    if branch_slug or barcode:
        if not branch_slug:
            errors.append("branch is required when a barcode is provided")
        if not barcode:
            errors.append("barcode is required when a branch is provided")
        branch = None
        if branch_slug:
            branch = Branch.objects.filter(organization=organization, slug=branch_slug).first()
            if branch is None:
                errors.append(f"branch '{branch_slug}' does not exist in this organization")
        if branch is not None and parsed["shelf_code"]:
            if not ShelfLocation.objects.filter(branch=branch, code=parsed["shelf_code"]).exists():
                errors.append(
                    f"shelf_code '{parsed['shelf_code']}' does not exist at branch '{branch_slug}'"
                )

    # Match against existing records for the validation preview.
    work_match = None
    if slug:
        work_match = Work.objects.filter(slug=slug).first()
    if work_match is None and title:
        work_match = Work.objects.filter(normalized_title=normalize_text(title)).first()
    if work_match is not None:
        matched["work_id"] = work_match.pk
    if parsed["isbn_13"]:
        edition = Edition.objects.filter(isbn_13=parsed["isbn_13"]).first()
        if edition is not None:
            matched["edition_id"] = edition.pk
    if barcode:
        copy = Copy.objects.filter(organization=organization, barcode=barcode).first()
        if copy is not None:
            matched["copy_id"] = copy.pk

    return parsed, errors, matched


def validate_import(*, batch: CatalogImportBatch) -> CatalogImportBatch:
    if batch.status not in {CatalogImportStatus.STAGED, CatalogImportStatus.VALIDATED}:
        raise DomainError("Only staged batches can be validated.")
    organization = batch.organization
    error_rows = 0
    total_errors = 0
    seen_barcodes: dict[str, int] = {}
    unknown_columns: set[str] = set()
    with transaction.atomic():
        for row in batch.rows.all():
            parsed, errors, matched = _validate_row(organization, row.row_payload)
            unknown_columns |= set(row.row_payload.keys()) - KNOWN_COLUMNS
            # A barcode identifies one physical copy; it cannot repeat within the
            # batch (the DB would merge/collide the second one silently).
            barcode = parsed.get("barcode")
            if barcode:
                if barcode in seen_barcodes:
                    errors.append(
                        f"barcode '{barcode}' duplicates row {seen_barcodes[barcode]} in this batch"
                    )
                else:
                    seen_barcodes[barcode] = row.row_number
            row.parsed_fields = parsed
            row.validation_errors = errors
            row.matched_existing = matched
            row.save(update_fields=["parsed_fields", "validation_errors", "matched_existing", "updated_at"])
            if errors:
                error_rows += 1
                total_errors += len(errors)
        batch.error_count = error_rows
        batch.status = CatalogImportStatus.VALIDATED
        batch.validation_summary = {
            "rows": batch.row_count,
            "error_rows": error_rows,
            "valid_rows": batch.row_count - error_rows,
            "total_errors": total_errors,
            "unknown_columns": sorted(unknown_columns),
        }
        batch.save(update_fields=["error_count", "status", "validation_summary", "updated_at"])
    audit_action(action="import.validate", entity=batch, source="import")
    return batch


def _resolve_authors(names: list[str]) -> list[Author]:
    authors = []
    for name in names:
        author = Author.objects.filter(normalized_name=normalize_text(name)).first()
        if author is None:
            author = Author.objects.create(name=name)
        authors.append(author)
    return authors


def _resolve_subjects(names: list[str]) -> list[Subject]:
    subjects = []
    for name in names:
        slug = slugify(name)
        subject, _ = Subject.objects.get_or_create(slug=slug, defaults={"name": name})
        subjects.append(subject)
    return subjects


def _commit_row(organization, parsed: dict) -> dict:
    """Upsert Work/Edition/Copy for a validated row; return a reversal record."""
    result: dict = {}

    # Work: match by slug then normalized title, else create.
    work = Work.objects.filter(slug=parsed["slug"]).first()
    if work is None:
        work = Work.objects.filter(normalized_title=normalize_text(parsed["title"])).first()
    work_created = work is None
    if work_created:
        work = Work.objects.create(
            canonical_title=parsed["title"],
            subtitle=parsed["subtitle"],
            slug=parsed["slug"],
            summary=parsed["summary"],
        )
    result["work"] = {"id": work.pk, "created": work_created}

    # Track which m2m links this row newly added to a *pre-existing* work, so a
    # rollback can detach exactly those (created works are deleted wholesale).
    existing_author_ids = set() if work_created else set(work.authors.values_list("id", flat=True))
    existing_subject_ids = (
        set() if work_created else set(work.subjects.values_list("id", flat=True))
    )
    authors = _resolve_authors(parsed["authors"])
    if authors:
        work.authors.add(*authors)
    subjects = _resolve_subjects(parsed["subjects"])
    if subjects:
        work.subjects.add(*subjects)
    if not work_created:
        result["added_author_ids"] = [a.pk for a in authors if a.pk not in existing_author_ids]
        result["added_subject_ids"] = [s.pk for s in subjects if s.pk not in existing_subject_ids]

    # Edition: match by ISBN-13, then ISBN-10, else by (work, format, year).
    edition = None
    if parsed["isbn_13"]:
        edition = Edition.objects.filter(isbn_13=parsed["isbn_13"]).first()
    if edition is None and parsed["isbn_10"]:
        edition = Edition.objects.filter(isbn_10=parsed["isbn_10"]).first()
    if edition is None:
        edition = Edition.objects.filter(
            work=work, format=parsed["format"], publication_year=parsed["publication_year"]
        ).first()
    edition_created = edition is None
    if edition_created:
        edition = Edition.objects.create(
            work=work,
            isbn_13=parsed["isbn_13"] or None,
            isbn_10=parsed["isbn_10"] or None,
            publisher=parsed["publisher"],
            publication_year=parsed["publication_year"],
            language=parsed["language"],
            format=parsed["format"],
            edition_statement=parsed["edition_statement"],
            description=parsed["description"],
            cover_image=parsed["cover_image"],
        )
    result["edition"] = {"id": edition.pk, "created": edition_created}

    # Copy (optional): only when branch + barcode were supplied.
    if parsed["branch"] and parsed["barcode"]:
        copy = Copy.objects.filter(organization=organization, barcode=parsed["barcode"]).first()
        copy_created = copy is None
        if copy_created:
            entitlements.assert_within_limit(organization, "copies")
            branch = Branch.objects.get(organization=organization, slug=parsed["branch"])
            shelf = None
            if parsed["shelf_code"]:
                shelf = ShelfLocation.objects.filter(branch=branch, code=parsed["shelf_code"]).first()
            copy = Copy.objects.create(
                organization=organization,
                edition=edition,
                branch=branch,
                shelf_location=shelf,
                barcode=parsed["barcode"],
                condition=parsed["condition"],
                acquisition_date=parsed["acquisition_date"],
                public_visible=parsed["public_visible"],
            )
        result["copy"] = {"id": copy.pk, "created": copy_created}

    result["work_id"] = work.pk
    return result


def commit_import(*, batch: CatalogImportBatch, actor=None) -> CatalogImportBatch:
    if batch.status != CatalogImportStatus.VALIDATED:
        raise DomainError("Only validated batches can be committed.")
    if batch.error_count:
        raise DomainError("Resolve validation errors before committing.")

    affected_work_ids: set[int] = set()
    commit_errors = 0
    with transaction.atomic():
        locked = CatalogImportBatch.objects.select_for_update().get(pk=batch.pk)
        if locked.status != CatalogImportStatus.VALIDATED:
            raise DomainError("Only validated batches can be committed.")
        # Suppress per-object search-reindex signals during the write burst; we
        # rebuild each affected work exactly once below instead of ~4x per row.
        with suspend_search_reindex():
            for row in batch.rows.all():
                if row.validation_errors:
                    continue
                try:
                    # Per-row savepoint: a global uniqueness clash (e.g. a slug or
                    # ISBN created by a concurrent batch) or a plan-limit breach
                    # fails just this row instead of aborting the whole commit.
                    with transaction.atomic():
                        result = _commit_row(batch.organization, row.parsed_fields)
                except (IntegrityError, entitlements.EntitlementError) as exc:
                    row.commit_result = {"error": str(exc)}
                    row.save(update_fields=["commit_result", "updated_at"])
                    commit_errors += 1
                    continue
                row.commit_result = result
                row.save(update_fields=["commit_result", "updated_at"])
                affected_work_ids.add(result["work_id"])

        for work_id in affected_work_ids:
            rebuild_work_search_document(work_id)

        batch.status = CatalogImportStatus.COMMITTED
        batch.committed_at = timezone.now()
        summary = dict(batch.validation_summary or {})
        summary["commit_errors"] = commit_errors
        summary["committed_rows"] = len(affected_work_ids)
        batch.validation_summary = summary
        batch.save(update_fields=["status", "committed_at", "validation_summary", "updated_at"])

    audit_action(
        action="import.commit",
        entity=batch,
        actor=actor,
        after={"works_affected": len(affected_work_ids), "commit_errors": commit_errors},
        source="import",
    )
    emit_domain_event(
        event_type="import.committed",
        aggregate=batch,
        payload={"batch_id": batch.pk, "works_affected": len(affected_work_ids)},
        actor=actor,
        source="import",
    )
    return batch


def rollback_import(*, batch: CatalogImportBatch, actor=None, reason: str = "") -> CatalogImportBatch:
    """Cancel a staged/validated batch, or reverse a committed one.

    Reversal deletes only the records the batch *created* and only when they
    have no dependents (a copy with loans/holds, an edition still holding other
    copies, or a work with other editions is left in place and reported).
    """
    if batch.status == CatalogImportStatus.ROLLED_BACK:
        raise DomainError("This batch is already rolled back.")
    if batch.status in {CatalogImportStatus.STAGED, CatalogImportStatus.VALIDATED}:
        batch.status = CatalogImportStatus.ROLLED_BACK
        batch.rolled_back_at = timezone.now()
        batch.save(update_fields=["status", "rolled_back_at", "updated_at"])
        audit_action(action="import.rollback", entity=batch, actor=actor, reason=reason, source="import")
        return batch
    if batch.status != CatalogImportStatus.COMMITTED:
        raise DomainError("Only staged, validated, or committed batches can be rolled back.")

    reverted = {"copies": 0, "editions": 0, "works": 0, "skipped": 0}
    affected_work_ids: set[int] = set()
    with transaction.atomic():
        rows = list(batch.rows.select_for_update().all())
        # Delete copies first, then editions, then works (respecting PROTECT FKs).
        for row in rows:
            copy_info = row.commit_result.get("copy")
            if not (copy_info and copy_info.get("created")):
                continue
            copy = Copy.objects.filter(pk=copy_info["id"]).first()
            if copy is None:
                continue
            if Loan.objects.filter(copy=copy).exists() or Hold.objects.filter(assigned_copy=copy).exists():
                reverted["skipped"] += 1
                continue
            affected_work_ids.add(copy.edition.work_id)
            copy.delete()
            reverted["copies"] += 1

        for row in rows:
            edition_info = row.commit_result.get("edition")
            if not (edition_info and edition_info.get("created")):
                continue
            edition = Edition.objects.filter(pk=edition_info["id"]).first()
            if edition is None:
                continue
            if edition.copies.exists():
                reverted["skipped"] += 1
                continue
            affected_work_ids.add(edition.work_id)
            edition.delete()
            reverted["editions"] += 1

        for row in rows:
            work_info = row.commit_result.get("work")
            if not (work_info and work_info.get("created")):
                continue
            work = Work.objects.filter(pk=work_info["id"]).first()
            if work is None:
                continue
            if work.editions.exists():
                reverted["skipped"] += 1
                continue
            work.delete()
            reverted["works"] += 1

        # Detach author/subject links this batch added to pre-existing works that
        # survive the rollback (created works were deleted above with their m2m).
        for row in rows:
            work_info = row.commit_result.get("work") or {}
            if work_info.get("created"):
                continue
            work = Work.objects.filter(pk=work_info.get("id")).first()
            if work is None:
                continue
            author_ids = row.commit_result.get("added_author_ids") or []
            subject_ids = row.commit_result.get("added_subject_ids") or []
            if author_ids:
                work.authors.remove(*author_ids)
            if subject_ids:
                work.subjects.remove(*subject_ids)
            if author_ids or subject_ids:
                affected_work_ids.add(work.pk)

        for work_id in affected_work_ids:
            if Work.objects.filter(pk=work_id).exists():
                rebuild_work_search_document(work_id)

        batch.status = CatalogImportStatus.ROLLED_BACK
        batch.rolled_back_at = timezone.now()
        summary = dict(batch.validation_summary or {})
        summary["rollback"] = reverted
        batch.validation_summary = summary
        batch.save(update_fields=["status", "rolled_back_at", "validation_summary", "updated_at"])

    audit_action(
        action="import.rollback",
        entity=batch,
        actor=actor,
        reason=reason,
        after=reverted,
        source="import",
    )
    emit_domain_event(
        event_type="import.rolled_back",
        aggregate=batch,
        payload={"batch_id": batch.pk, **reverted},
        actor=actor,
        source="import",
    )
    return batch
