from rest_framework import authentication, exceptions

from .models import ScopedApiToken


class ScopedTokenAuthentication(authentication.BaseAuthentication):
    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).decode("utf-8")
        if not header:
            return None
        parts = header.split()
        if len(parts) != 2 or parts[0] not in {self.keyword, "Token"}:
            return None
        raw_key = parts[1]
        # Verify against every non-revoked token sharing the prefix: two tokens
        # can collide on the 12-char prefix, and picking just one would reject an
        # otherwise-valid key.
        candidates = ScopedApiToken.objects.filter(
            prefix=raw_key[:12], revoked_at__isnull=True
        )
        token = next((candidate for candidate in candidates if candidate.verify(raw_key)), None)
        if token is None:
            raise exceptions.AuthenticationFailed("Invalid API token.")
        token.mark_used()
        request.organization = token.organization
        request.auth_scopes = token.scopes
        return (token.user, token)
