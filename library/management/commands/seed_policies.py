"""Seed default patron types, material types, and circulation policies."""
from django.core.management.base import BaseCommand, CommandError

from library.models import CirculationPolicy, MaterialType, Organization, PatronType


class Command(BaseCommand):
    help = "Seed default patron types, material types, and a circulation matrix for an org."

    def add_arguments(self, parser):
        parser.add_argument("--org", required=True)

    def handle(self, *args, **options):
        org = Organization.objects.filter(slug=options["org"]).first()
        if org is None:
            raise CommandError(f"Organization '{options['org']}' not found.")

        patron_types = {}
        for code, name, loans, holds in [
            ("adult", "Adult", 20, 10),
            ("child", "Child", 10, 5),
            ("staff", "Staff", 50, 25),
        ]:
            patron_types[code], _ = PatronType.objects.update_or_create(
                organization=org, code=code, defaults={"name": name, "max_loans": loans, "max_holds": holds}
            )

        material_types = {}
        for code, name in [("book", "Book"), ("dvd", "DVD"), ("reference", "Reference")]:
            material_types[code], _ = MaterialType.objects.update_or_create(
                organization=org, code=code, defaults={"name": name}
            )

        # Global default cell.
        CirculationPolicy.objects.update_or_create(
            organization=org,
            patron_type=None,
            material_type=None,
            defaults={"loan_days": 21, "max_renewals": 2, "hold_pickup_days": 7, "holdable": True},
        )
        # Reference material: short loan, not holdable, for any patron.
        CirculationPolicy.objects.update_or_create(
            organization=org,
            patron_type=None,
            material_type=material_types["reference"],
            defaults={"loan_days": 3, "max_renewals": 0, "holdable": False},
        )
        # DVDs: shorter loan for children.
        CirculationPolicy.objects.update_or_create(
            organization=org,
            patron_type=patron_types["child"],
            material_type=material_types["dvd"],
            defaults={"loan_days": 7, "max_renewals": 1, "holdable": True},
        )
        self.stdout.write(self.style.SUCCESS("Seeded patron/material types and circulation matrix."))
