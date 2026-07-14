"""Enable PostgreSQL trigram extension for fuzzy search."""
from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("library", "0001_initial"),
    ]

    operations = [
        TrigramExtension(),
    ]
