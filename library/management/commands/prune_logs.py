from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from library.models import (
    AuditLog,
    DomainEvent,
    NotificationDelivery,
    OutboxEvent,
    OutboxStatus,
    SearchQueryLog,
)


class Command(BaseCommand):
    help = "Delete aged log/event rows to bound table growth. Schedule daily."

    def add_arguments(self, parser):
        parser.add_argument("--search-days", type=int, default=90)
        parser.add_argument("--outbox-days", type=int, default=7)
        parser.add_argument("--failed-outbox-days", type=int, default=30)
        parser.add_argument("--notification-days", type=int, default=90)
        parser.add_argument("--event-days", type=int, default=180)
        parser.add_argument("--audit-days", type=int, default=365)
        parser.add_argument(
            "--dry-run", action="store_true", help="Report counts without deleting."
        )

    def handle(self, *args, **options):
        now = timezone.now()
        dry = options["dry_run"]

        def cutoff(days):
            return now - timedelta(days=days)

        # Processed outbox events are pruned quickly; failed (dead-lettered) ones
        # are kept longer for investigation but still bounded so the recipient
        # emails their payloads may carry (e.g. return receipts) do not linger.
        targets = [
            ("search_logs", SearchQueryLog.objects.filter(created_at__lt=cutoff(options["search_days"]))),
            (
                "outbox_processed",
                OutboxEvent.objects.filter(
                    status=OutboxStatus.PROCESSED, created_at__lt=cutoff(options["outbox_days"])
                ),
            ),
            (
                "outbox_failed",
                OutboxEvent.objects.filter(
                    status=OutboxStatus.FAILED,
                    created_at__lt=cutoff(options["failed_outbox_days"]),
                ),
            ),
            (
                "notifications",
                NotificationDelivery.objects.filter(
                    created_at__lt=cutoff(options["notification_days"])
                ),
            ),
            ("domain_events", DomainEvent.objects.filter(created_at__lt=cutoff(options["event_days"]))),
            ("audit_logs", AuditLog.objects.filter(created_at__lt=cutoff(options["audit_days"]))),
        ]

        for label, qs in targets:
            if dry:
                self.stdout.write(f"{label}: would delete {qs.count()}")
                continue
            deleted, _ = qs.delete()
            self.stdout.write(self.style.SUCCESS(f"{label}: deleted {deleted}"))

        # Scrub recipient PII from failed deliveries that are past their retry
        # window but not yet old enough for full deletion.
        scrub_before = cutoff(options["failed_outbox_days"])
        scrubbed = (
            NotificationDelivery.objects.filter(status="failed", created_at__lt=scrub_before)
            .exclude(recipient="")
            .update(recipient="")
        )
        if not dry:
            self.stdout.write(self.style.SUCCESS(f"failed_delivery_recipients_scrubbed: {scrubbed}"))
