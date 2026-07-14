"""Seed a demo library with catalog, copies, users, and staff."""
from random import Random

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils.text import slugify

from library.models import (
    Author,
    Branch,
    Collection,
    Copy,
    CopyStatus,
    Edition,
    EditionFormat,
    Organization,
    PatronProfile,
    ShelfLocation,
    StaffMembership,
    StaffRole,
    Subject,
    Work,
)
from library.notifications import ensure_default_templates
from library.services import rebuild_work_search_document

AUTHORS = [
    "Octavia Butler",
    "Ursula K. Le Guin",
    "James Baldwin",
    "Toni Morrison",
    "N. K. Jemisin",
    "Rebecca Solnit",
    "Mary Oliver",
    "Kazuo Ishiguro",
    "Isabel Allende",
    "Ted Chiang",
    "Jhumpa Lahiri",
    "Colson Whitehead",
]

SUBJECTS = [
    "Science Fiction",
    "Literary Fiction",
    "History",
    "Poetry",
    "Technology",
    "Mystery",
    "Memoir",
    "Young Adult",
    "Design",
    "Philosophy",
]

TITLE_NOUNS = [
    "Archive",
    "Harbor",
    "Garden",
    "Signal",
    "Library",
    "Compass",
    "Atlas",
    "Weather",
    "Memory",
    "River",
    "Machine",
    "Orchard",
]

TITLE_MODIFIERS = [
    "Hidden",
    "Luminous",
    "Borrowed",
    "Quiet",
    "Northern",
    "Electric",
    "Patient",
    "Paper",
    "Last",
    "Wild",
    "Glass",
    "Brave",
]


class Command(BaseCommand):
    help = "Seed a realistic demo library with catalog, copies, users, and staff."

    def add_arguments(self, parser):
        parser.add_argument("--works", type=int, default=96)

    def handle(self, *args, **options):
        rng = Random(75)
        User = get_user_model()
        org, _ = Organization.objects.get_or_create(
            slug="metro-library",
            defaults={"name": "Metro Library", "default_timezone": "America/Los_Angeles"},
        )
        downtown, _ = Branch.objects.get_or_create(
            organization=org,
            slug="downtown",
            defaults={
                "name": "Downtown",
                "address": "100 Main Street",
                "timezone": "America/Los_Angeles",
            },
        )
        westside, _ = Branch.objects.get_or_create(
            organization=org,
            slug="westside",
            defaults={
                "name": "Westside",
                "address": "42 Ocean Avenue",
                "timezone": "America/Los_Angeles",
            },
        )
        shelves = []
        for branch in [downtown, westside]:
            for code, name in [("FIC", "Fiction"), ("NF", "Nonfiction"), ("YA", "Young Adult")]:
                shelf, _ = ShelfLocation.objects.get_or_create(
                    branch=branch,
                    code=code,
                    defaults={"name": name, "public_label": f"{branch.name} {name}"},
                )
                shelves.append(shelf)

        author_objs = []
        for name in AUTHORS:
            author, _ = Author.objects.get_or_create(name=name)
            author_objs.append(author)

        subject_objs = []
        for name in SUBJECTS:
            subject, _ = Subject.objects.get_or_create(slug=slugify(name), defaults={"name": name})
            subject_objs.append(subject)

        admin_user, _ = User.objects.get_or_create(
            username="admin",
            defaults={"email": "admin@example.test", "is_staff": True, "is_superuser": True},
        )
        admin_user.set_password("demo12345")
        admin_user.save()

        patron_user, _ = User.objects.get_or_create(
            username="patron",
            defaults={"email": "patron@example.test", "first_name": "Pat", "last_name": "Reader"},
        )
        patron_user.set_password("demo12345")
        patron_user.save()
        PatronProfile.objects.get_or_create(
            user=patron_user,
            organization=org,
            defaults={
                "library_card_number": "CARD-1001",
                "home_branch": downtown,
                "notification_email": "patron@example.test",
            },
        )

        librarian_user, _ = User.objects.get_or_create(
            username="librarian",
            defaults={
                "email": "librarian@example.test",
                "first_name": "Liv",
                "last_name": "Stacks",
                "is_staff": True,
            },
        )
        librarian_user.set_password("demo12345")
        librarian_user.save()
        StaffMembership.objects.get_or_create(
            user=librarian_user,
            organization=org,
            branch=downtown,
            role=StaffRole.LIBRARIAN,
        )

        created = 0
        for index in range(options["works"]):
            title = f"The {rng.choice(TITLE_MODIFIERS)} {rng.choice(TITLE_NOUNS)}"
            if Work.objects.filter(slug=f"{slugify(title)}-{index}").exists():
                continue
            work = Work.objects.create(
                canonical_title=f"{title} {index + 1}",
                slug=f"{slugify(title)}-{index}",
                subtitle=rng.choice(["", "A Chronicle", "Collected Notes", "A Field Guide"]),
                summary=(
                    "A carefully cataloged demo title with enough descriptive text to make "
                    "search, facets, and pagination feel like a working library."
                ),
            )
            work.authors.add(*rng.sample(author_objs, rng.randint(1, 2)))
            work.subjects.add(*rng.sample(subject_objs, rng.randint(1, 3)))
            edition = Edition.objects.create(
                work=work,
                isbn_13=f"978{index + 1000000000:010d}"[-13:],
                publisher=rng.choice(["Northstar Press", "Civic House", "Brightline Books"]),
                publication_year=rng.randint(1990, 2026),
                format=rng.choice(
                    [EditionFormat.HARDCOVER, EditionFormat.PAPERBACK, EditionFormat.EBOOK]
                ),
                cover_image=f"https://picsum.photos/seed/elibrary-{index}/320/480",
                description=work.summary,
            )
            for copy_number in range(rng.randint(1, 4)):
                branch = rng.choice([downtown, westside])
                shelf = rng.choice([s for s in shelves if s.branch_id == branch.id])
                Copy.objects.create(
                    organization=org,
                    edition=edition,
                    branch=branch,
                    shelf_location=shelf,
                    barcode=f"ML-{index + 1:04d}-{copy_number + 1}",
                    status=CopyStatus.AVAILABLE,
                )
            rebuild_work_search_document(work.pk)
            created += 1

        collection, _ = Collection.objects.get_or_create(
            organization=org,
            slug="staff-picks",
            defaults={
                "name": "Staff Picks",
                "description": "A rotating set of high-signal demo works.",
            },
        )
        collection.works.set(Work.objects.order_by("id")[:12])
        ensure_default_templates(org)

        from django.core.management import call_command

        from library.models import FeePolicy, Plan, Subscription, SubscriptionStatus

        call_command("seed_plans")
        professional = Plan.objects.filter(slug="professional").first()
        if professional:
            Subscription.objects.get_or_create(
                organization=org,
                defaults={"plan": professional, "status": SubscriptionStatus.ACTIVE},
            )
        FeePolicy.objects.get_or_create(organization=org)
        self.stdout.write(self.style.SUCCESS(f"Seeded {created} works."))
        self.stdout.write("Users: admin/demo12345, librarian/demo12345, patron/demo12345")
