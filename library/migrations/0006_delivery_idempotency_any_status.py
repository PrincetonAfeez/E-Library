"""Enforce delivery idempotency across all statuses."""
from django.db import migrations, models


def dedupe_deliveries(apps, schema_editor):
    """Collapse duplicate (organization, provider_reference) delivery rows.

    Before this migration the constraint only covered status='sent', so a
    failed-then-sent retry could leave two rows sharing a provider_reference.
    Keep the 'sent' one (else the newest) and delete the rest so the new
    all-status unique constraint can be added on existing databases.
    """
    NotificationDelivery = apps.get_model("library", "NotificationDelivery")
    seen: dict = {}
    # Order so the preferred row (status='sent', then newest) is seen first.
    for row in (
        NotificationDelivery.objects.exclude(provider_reference="")
        .order_by("organization_id", "provider_reference")
        .values("id", "organization_id", "provider_reference", "status", "created_at")
        .iterator()
    ):
        key = (row["organization_id"], row["provider_reference"])
        keep = seen.get(key)
        if keep is None:
            seen[key] = row
            continue
        # Decide which of the two to keep; delete the loser.
        current_better = (row["status"] == "sent" and keep["status"] != "sent") or (
            row["status"] == keep["status"] and row["created_at"] > keep["created_at"]
        )
        loser_id = keep["id"] if current_better else row["id"]
        if current_better:
            seen[key] = row
        NotificationDelivery.objects.filter(id=loser_id).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("library", "0005_user_email_ci_unique"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="notificationdelivery",
            name="uniq_sent_delivery_per_event",
        ),
        migrations.RunPython(dedupe_deliveries, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="notificationdelivery",
            constraint=models.UniqueConstraint(
                condition=models.Q(("provider_reference", ""), _negated=True),
                fields=("organization", "provider_reference"),
                name="uniq_delivery_per_event",
            ),
        ),
    ]
