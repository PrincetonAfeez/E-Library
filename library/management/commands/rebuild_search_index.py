"""Rebuild denormalized work-level search documents."""
from django.core.management.base import BaseCommand

from library.models import Work
from library.services import rebuild_work_search_document


class Command(BaseCommand):
    help = "Rebuild denormalized Work-level search documents."

    def add_arguments(self, parser):
        parser.add_argument("--work-id", type=int)

    def handle(self, *args, **options):
        qs = Work.objects.all()
        if options["work_id"]:
            qs = qs.filter(pk=options["work_id"])
        count = 0
        for work_id in qs.values_list("id", flat=True).iterator():
            rebuild_work_search_document(work_id)
            count += 1
        self.stdout.write(self.style.SUCCESS(f"Rebuilt {count} search document(s)."))
