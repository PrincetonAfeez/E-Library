from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from library.models import Organization, ScopedApiToken


class Command(BaseCommand):
    help = "Issue a scoped API token for a user."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--org", required=True)
        parser.add_argument("--name", default="CLI token")
        parser.add_argument("--scope", action="append", default=[])

    def handle(self, *args, **options):
        User = get_user_model()
        user = User.objects.filter(username=options["username"]).first()
        if user is None:
            raise CommandError("User not found.")
        org = Organization.objects.filter(slug=options["org"]).first()
        if org is None:
            raise CommandError("Organization not found.")
        raw_key, _token = ScopedApiToken.issue(
            user=user,
            organization=org,
            name=options["name"],
            scopes=options["scope"] or ["patron:read"],
        )
        self.stdout.write(raw_key)
