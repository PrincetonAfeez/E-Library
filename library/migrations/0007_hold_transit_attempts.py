"""Add transit_attempts field to the Hold model."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("library", "0006_delivery_idempotency_any_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="hold",
            name="transit_attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
    ]
