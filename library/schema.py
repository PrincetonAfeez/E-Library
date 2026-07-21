"""OpenAPI authentication schema extension for scoped API tokens."""

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class ScopedTokenAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "library.auth.ScopedTokenAuthentication"
    name = "ScopedTokenAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "Scoped API token",
        }
