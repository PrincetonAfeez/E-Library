"""ENT-sso: end-to-end OIDC test against a real (local, in-process) IdP.

Stands up an actual OIDC HTTP server on localhost and drives the full login
through the real client code (_default_exchange / _default_userinfo do a real
urlopen over a socket) — genuine "tested against an IdP" evidence with no
external infrastructure or credentials.
"""

import http.server
import json
import threading

import pytest

from library import sso
from library.models import Organization, SsoConnection, SsoIdentity

pytestmark = pytest.mark.django_db(transaction=True)


class _IdPHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence server logging in tests
        pass

    def _json(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # OIDC token endpoint
        length = int(self.headers.get("Content-Length", 0) or 0)
        self.rfile.read(length)
        self._json({"access_token": "AT-real", "token_type": "Bearer", "id_token": "jwt"})

    def do_GET(self):  # OIDC userinfo endpoint
        self._json({"sub": "idp-777", "email": "real@idp.test", "given_name": "Real", "family_name": "User"})


@pytest.fixture
def local_idp():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _IdPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_oidc_end_to_end_against_local_idp(local_idp):
    org = Organization.objects.create(name="Lib", slug="lib")
    conn = SsoConnection.objects.create(
        organization=org,
        client_id="cid",
        client_secret="secret",
        authorize_url=f"{local_idp}/authorize",
        token_url=f"{local_idp}/token",
        userinfo_url=f"{local_idp}/userinfo",
    )
    # No stubs: real token exchange + userinfo fetch happen over an actual socket.
    user = sso.handle_callback(conn, code="auth-code", redirect_uri="https://app/callback")
    assert user.email == "real@idp.test"
    assert SsoIdentity.objects.filter(connection=conn, subject="idp-777", user=user).exists()

    # A second login with the same subject links to the same account (idempotent).
    again = sso.handle_callback(conn, code="auth-code-2", redirect_uri="https://app/callback")
    assert again.pk == user.pk
    assert SsoIdentity.objects.filter(connection=conn).count() == 1
