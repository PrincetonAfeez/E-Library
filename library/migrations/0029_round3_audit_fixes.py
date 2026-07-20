from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("library", "0028_round2_plan_price_sip2_secret_len"),
    ]

    operations = [
        migrations.AddField(
            model_name="checkoutsession",
            name="external_session_id",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="checkoutsession",
            name="hosted_url",
            field=models.URLField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="payment",
            name="gateway_charge_id",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="payment",
            name="gateway_refund_id",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.CreateModel(
            name="GatewayEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_id", models.CharField(max_length=120, unique=True)),
                ("event_type", models.CharField(max_length=120)),
                ("processed_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.AddField(
            model_name="digitalasset",
            name="organization",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.CASCADE,
                related_name="digital_assets",
                to="library.organization",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="digitalasset",
            name="uniq_asset_edition_fmt",
        ),
        migrations.AddConstraint(
            model_name="digitalasset",
            constraint=models.UniqueConstraint(
                fields=("organization", "edition", "fmt"),
                name="uniq_asset_org_edition_fmt",
            ),
        ),
        migrations.AddField(
            model_name="webhookdelivery",
            name="outbox_event_id",
            field=models.PositiveIntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AlterField(
            model_name="webhookdelivery",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("sending", "Sending"),
                    ("delivered", "Delivered"),
                    ("failed", "Failed"),
                ],
                default="pending",
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="webhookdelivery",
            constraint=models.UniqueConstraint(
                condition=Q(outbox_event_id__isnull=False),
                fields=("endpoint", "outbox_event_id"),
                name="uniq_webhook_delivery_outbox",
            ),
        ),
    ]
