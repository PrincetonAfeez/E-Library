import time

from django.core.management.base import BaseCommand

from library.services import drain_outbox


class Command(BaseCommand):
    help = "Drain pending outbox events with SKIP LOCKED claiming on PostgreSQL."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=100)
        parser.add_argument("--sleep", type=float, default=2.0)
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        while True:
            processed = drain_outbox(batch_size=options["batch_size"])
            self.stdout.write(f"processed={processed}")
            if options["once"]:
                return
            time.sleep(options["sleep"])
