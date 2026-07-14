"""Run a minimal SIP2 self-check TCP server."""
import socketserver

from django.core.management.base import BaseCommand, CommandError

from library.models import Organization
from library.sip2 import handle_message


class Command(BaseCommand):
    help = "Run a minimal SIP2 self-check TCP server for one organization."

    def add_arguments(self, parser):
        parser.add_argument("--org", required=True)
        # Default to loopback; SIP2 is unauthenticated line-protocol, so binding
        # to all interfaces must be an explicit, deliberate choice (--host 0.0.0.0
        # behind a firewall / on a private network only).
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=6001)

    def handle(self, *args, **options):
        org = Organization.objects.filter(slug=options["org"]).first()
        if org is None:
            raise CommandError(f"Organization '{options['org']}' not found.")

        organization = org

        class SIP2Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    text = line.decode("utf-8", "replace").strip()
                    if not text:
                        continue
                    response = handle_message(text, organization=organization)
                    self.wfile.write((response + "\r").encode("utf-8"))

        server = socketserver.ThreadingTCPServer((options["host"], options["port"]), SIP2Handler)
        self.stdout.write(
            self.style.SUCCESS(f"SIP2 server for {org.slug} on {options['host']}:{options['port']}")
        )
        server.serve_forever()
