"""Stage, validate, and commit CSV catalog imports."""
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from library.imports import (
    commit_import,
    parse_rows_from_csv,
    rollback_import,
    stage_import,
    validate_import,
)
from library.models import Organization


class Command(BaseCommand):
    help = "Stage, validate, and optionally commit a CSV catalog import."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--org", required=True, help="Organization slug")
        parser.add_argument("--uploaded-by", help="Username to attribute the batch to")
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Commit the batch when validation reports no errors.",
        )
        parser.add_argument(
            "--rollback-on-error",
            action="store_true",
            help="Roll the batch back if validation finds errors (instead of leaving it staged).",
        )

    def handle(self, *args, **options):
        org = Organization.objects.filter(slug=options["org"]).first()
        if org is None:
            raise CommandError(f"Organization '{options['org']}' not found.")

        uploaded_by = None
        if options.get("uploaded_by"):
            uploaded_by = get_user_model().objects.filter(username=options["uploaded_by"]).first()
            if uploaded_by is None:
                raise CommandError(f"User '{options['uploaded_by']}' not found.")

        path = Path(options["csv_path"])
        if not path.exists():
            raise CommandError(f"File '{path}' does not exist.")
        rows = parse_rows_from_csv(path.read_bytes())
        if not rows:
            raise CommandError("No rows found in CSV.")

        batch = stage_import(
            organization=org, rows=rows, uploaded_by=uploaded_by, source_file=str(path)
        )
        validate_import(batch=batch)
        batch.refresh_from_db()
        summary = batch.validation_summary
        self.stdout.write(
            f"batch={batch.pk} rows={summary['rows']} "
            f"valid={summary['valid_rows']} errors={summary['error_rows']}"
        )

        if batch.error_count:
            for row in batch.rows.exclude(validation_errors=[]).order_by("row_number"):
                self.stdout.write(
                    self.style.WARNING(f"  row {row.row_number}: {'; '.join(row.validation_errors)}")
                )
            if options["rollback_on_error"]:
                rollback_import(batch=batch, actor=uploaded_by, reason="validation errors")
                self.stdout.write(self.style.ERROR("Rolled back due to validation errors."))
            return

        if options["commit"]:
            commit_import(batch=batch, actor=uploaded_by)
            self.stdout.write(self.style.SUCCESS(f"Committed batch {batch.pk}."))
        else:
            self.stdout.write("Validation passed. Re-run with --commit to apply.")
