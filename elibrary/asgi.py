"""ASGI entry point for the E-Library Django project."""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "elibrary.settings")

application = get_asgi_application()
