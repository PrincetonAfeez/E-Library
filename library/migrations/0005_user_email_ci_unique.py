from django.conf import settings
from django.db import migrations

INDEX_NAME = "uniq_user_email_ci"


def create_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    table = apps.get_model(settings.AUTH_USER_MODEL)._meta.db_table
    schema_editor.execute(
        f'CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME} '
        f'ON "{table}" (LOWER(email)) WHERE email <> \'\';'
    )


def drop_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(f"DROP INDEX IF EXISTS {INDEX_NAME};")


class Migration(migrations.Migration):
    dependencies = [
        ("library", "0004_notificationdelivery_idempotency"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(create_index, drop_index),
    ]
