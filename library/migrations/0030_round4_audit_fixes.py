from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("library", "0029_round3_audit_fixes"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="digitalasset",
            name="uniq_asset_org_edition_fmt",
        ),
        migrations.AddConstraint(
            model_name="digitalasset",
            constraint=models.UniqueConstraint(
                condition=Q(organization__isnull=True),
                fields=("edition", "fmt"),
                name="uniq_asset_edition_fmt_global",
            ),
        ),
        migrations.AddConstraint(
            model_name="digitalasset",
            constraint=models.UniqueConstraint(
                condition=Q(organization__isnull=False),
                fields=("organization", "edition", "fmt"),
                name="uniq_asset_org_edition_fmt",
            ),
        ),
    ]
