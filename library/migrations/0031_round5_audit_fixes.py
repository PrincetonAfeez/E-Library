from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("library", "0030_round4_audit_fixes"),
    ]

    operations = [
        migrations.AddField(
            model_name="paymentmethod",
            name="purpose",
            field=models.CharField(
                choices=[("saas", "SaaS subscription"), ("fines", "Patron fines")],
                default="saas",
                max_length=16,
            ),
        ),
    ]
