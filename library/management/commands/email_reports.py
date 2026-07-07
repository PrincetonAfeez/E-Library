from django.core.mail import send_mail
from django.core.management.base import BaseCommand

from library.models import Organization, StaffMembership
from library.reporting import (
    circulation_summary,
    default_window,
    fines_summary,
    holds_stats,
    popular_titles,
)


class Command(BaseCommand):
    help = "Email a periodic circulation digest to each organization's admins."

    def add_arguments(self, parser):
        parser.add_argument("--org", help="Limit to one organization slug.")
        parser.add_argument("--days", type=int, default=7)

    def _recipients(self, org):
        emails = (
            StaffMembership.objects.filter(
                organization=org, active=True, role__in=["admin", "branch_manager"]
            )
            .exclude(user__email="")
            .values_list("user__email", flat=True)
            .distinct()
        )
        return list(emails)

    def handle(self, *args, **options):
        orgs = Organization.objects.filter(active=True)
        if options.get("org"):
            orgs = orgs.filter(slug=options["org"])
        start, end = default_window(options["days"])
        sent = 0
        for org in orgs:
            recipients = self._recipients(org)
            if not recipients:
                continue
            circ = circulation_summary(org, start, end)
            holds = holds_stats(org, start, end)
            fines = fines_summary(org, start, end)
            top = popular_titles(org, start, end, limit=5)
            top_lines = "\n".join(f"  - {t['title']} ({t['loans']})" for t in top) or "  (none)"
            body = (
                f"{org.name} — last {options['days']} days\n\n"
                f"Borrowed: {circ['borrowed']}   Returned: {circ['returned']}   "
                f"Renewals: {circ['renewals']}\n"
                f"Active now: {circ['active_now']}   Overdue now: {circ['overdue_now']}\n"
                f"Holds placed: {holds['placed']}   Fulfilled: {holds['fulfilled']}\n"
                f"Fines assessed: {fines['assessed_cents']}c   "
                f"Collected: {fines['collected_cents']}c   "
                f"Outstanding: {fines['outstanding_cents']}c\n\n"
                f"Most borrowed:\n{top_lines}\n"
            )
            send_mail(
                f"[{org.name}] Weekly library report",
                body,
                None,
                recipients,
                fail_silently=True,
            )
            sent += 1
        self.stdout.write(self.style.SUCCESS(f"Sent {sent} report email(s)."))
