from django.core.management.base import BaseCommand, CommandError

from library.enrichment import enrich_edition
from library.models import Edition, Organization


class Command(BaseCommand):
    help = "Enrich editions with external ISBN metadata (OpenLibrary)."

    def add_arguments(self, parser):
        parser.add_argument("--org", required=True)
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument(
            "--only-missing",
            action="store_true",
            help="Only editions missing a publisher or cover image.",
        )

    def handle(self, *args, **options):
        org = Organization.objects.filter(slug=options["org"]).first()
        if org is None:
            raise CommandError(f"Organization '{options['org']}' not found.")
        qs = Edition.objects.filter(
            work__editions__copies__organization=org, isbn_13__isnull=False
        ).distinct()
        if options["only_missing"]:
            qs = qs.filter(publisher="") | qs.filter(cover_image="")
        enriched = 0
        for edition in qs.distinct()[: options["limit"]]:
            if enrich_edition(edition=edition):
                enriched += 1
        self.stdout.write(self.style.SUCCESS(f"Enriched {enriched} edition(s)."))
