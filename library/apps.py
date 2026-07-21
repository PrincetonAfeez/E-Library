"""Django app configuration for the library application."""

from django.apps import AppConfig


class LibraryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "library"

    def ready(self) -> None:
        from . import (
            schema,  # noqa: F401
            signals,  # noqa: F401
        )
