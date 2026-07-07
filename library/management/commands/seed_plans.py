from django.core.management.base import BaseCommand

from library.models import Plan

PLANS = [
    {
        "slug": "trial",
        "name": "Free Trial",
        "price_cents": 0,
        "max_branches": 1,
        "max_patrons": 500,
        "max_copies": 2000,
        "features": ["catalog", "circulation", "holds"],
    },
    {
        "slug": "community",
        "name": "Community",
        "price_cents": 9900,
        "max_branches": 3,
        "max_patrons": 10000,
        "max_copies": 50000,
        "features": ["catalog", "circulation", "holds", "imports", "reports", "fines"],
    },
    {
        "slug": "professional",
        "name": "Professional",
        "price_cents": 29900,
        "max_branches": 15,
        "max_patrons": 100000,
        "max_copies": 500000,
        "features": ["*"],
    },
    {
        "slug": "enterprise",
        "name": "Enterprise",
        "price_cents": 99900,
        "max_branches": None,
        "max_patrons": None,
        "max_copies": None,
        "features": ["*"],
    },
]


class Command(BaseCommand):
    help = "Create/refresh the standard subscription plan tiers."

    def handle(self, *args, **options):
        for spec in PLANS:
            Plan.objects.update_or_create(slug=spec["slug"], defaults=spec)
        self.stdout.write(self.style.SUCCESS(f"Seeded {len(PLANS)} plans."))
