from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("library", "0003_branch_max_renewals"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="notificationdelivery",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "sent"), models.Q(("provider_reference", ""), _negated=True)),
                fields=("organization", "provider_reference"),
                name="uniq_sent_delivery_per_event",
            ),
        ),
    ]
