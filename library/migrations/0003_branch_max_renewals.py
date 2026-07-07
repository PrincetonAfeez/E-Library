from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("library", "0002_enable_pg_trgm"),
    ]

    operations = [
        migrations.AddField(
            model_name="branch",
            name="max_renewals",
            field=models.PositiveSmallIntegerField(default=2),
        ),
    ]
