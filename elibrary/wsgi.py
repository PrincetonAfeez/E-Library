"""WSGI entry point for the E-Library Django project."""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "elibrary.settings")

application = get_wsgi_application()
