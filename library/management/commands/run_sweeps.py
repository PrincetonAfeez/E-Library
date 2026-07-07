from django.core.management.base import BaseCommand

from library.billing import run_billing_cycle
from library.cadence import send_hold_expiry_reminders, send_overdue_reminders
from library.digital import expire_digital_loans, expire_digital_ready_holds
from library.ops import dead_letter_backlog, scheduler_lock
from library.services import (
    assess_overdue_fines,
    expire_ready_holds,
    expire_stale_transits,
    flag_overdue_loans,
    reconcile_holds,
    send_due_soon_notifications,
)
from library.webhooks import deliver_webhooks


class Command(BaseCommand):
    help = "Run idempotent scheduled sweeps."

    def add_arguments(self, parser):
        parser.add_argument(
            "--full",
            action="store_true",
            help=(
                "Also run the heavier reconciliation sweeps (stale-transit recovery "
                "and hold reconciliation). Intended for a slower cadence (e.g. hourly)."
            ),
        )

    def handle(self, *args, **options):
        # Serialize across instances so two schedulers can't double-run a sweep.
        with scheduler_lock() as acquired:
            if not acquired:
                self.stdout.write(self.style.WARNING("sweep skipped: another runner holds the lock"))
                return
            self._run(options)

    def _run(self, options):
        overdue = flag_overdue_loans()
        expired = expire_ready_holds()
        digital_expired = expire_digital_loans()
        digital_holds_expired = expire_digital_ready_holds()
        due_soon = send_due_soon_notifications()
        overdue_reminders = send_overdue_reminders()
        hold_expiry_reminders = send_hold_expiry_reminders()
        webhooks_sent = deliver_webhooks()
        backlog = dead_letter_backlog()
        parts = [
            f"overdue_flagged={overdue}",
            f"ready_holds_expired={expired}",
            f"digital_loans_expired={digital_expired}",
            f"digital_holds_expired={digital_holds_expired}",
            f"due_soon_sent={due_soon}",
            f"overdue_reminders_sent={overdue_reminders}",
            f"hold_expiry_reminders_sent={hold_expiry_reminders}",
            f"webhooks_delivered={webhooks_sent}",
            f"dlq_outbox_failed={backlog['outbox_failed']}",
            f"dlq_webhook_failed={backlog['webhook_failed']}",
        ]
        if options["full"]:
            from library.delivery import prune_orphan_blobs

            transits = expire_stale_transits()
            reconciled = reconcile_holds()
            fines = assess_overdue_fines()
            billing = run_billing_cycle()
            orphan_blobs = prune_orphan_blobs()
            parts += [
                f"transits_recovered={transits}",
                f"holds_reconciled={reconciled}",
                f"overdue_fines_assessed={fines}",
                f"orphan_blobs_pruned={orphan_blobs}",
                f"subs_renewed={billing['renewed']}",
                f"subs_dunning={billing['dunning']}",
                f"subs_canceled={billing['canceled']}",
            ]
        self.stdout.write(self.style.SUCCESS(" ".join(parts)))
