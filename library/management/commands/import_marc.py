from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from library.imports import commit_import, import_marc
from library.models import Organization


class Command(BaseCommand):
    help = "Import a MARC file (binary .mrc or MARCXML) into the catalog."

    def add_arguments(self, parser):
        parser.add_argument("marc_path")
        parser.add_argument("--org", required=True)
        parser.add_argument("--uploaded-by")
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **options):
        org = Organization.objects.filter(slug=options["org"]).first()
        if org is None:
            raise CommandError(f"Organization '{options['org']}' not found.")
        uploaded_by = None
        if options.get("uploaded_by"):
            uploaded_by = get_user_model().objects.filter(username=options["uploaded_by"]).first()

        path = Path(options["marc_path"])
        if not path.exists():
            raise CommandError(f"File '{path}' does not exist.")

        batch = import_marc(
            organization=org, content=path.read_bytes(), uploaded_by=uploaded_by, source_file=str(path)
        )
        summary = batch.validation_summary
        self.stdout.write(
            f"batch={batch.pk} records={summary['rows']} "
            f"valid={summary['valid_rows']} errors={summary['error_rows']}"
        )
        if not batch.error_count and options["commit"]:
            commit_import(batch=batch, actor=uploaded_by)
            self.stdout.write(self.style.SUCCESS(f"Committed batch {batch.pk}."))
        elif not options["commit"]:
            self.stdout.write("Validation done. Re-run with --commit to apply.")
