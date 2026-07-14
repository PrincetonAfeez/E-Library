"""Enforce append-only audit logs at the database level."""
from django.db import migrations

# Enforce append-only audit logs at the database level: reject any UPDATE on
# library_auditlog. DELETE remains permitted so the retention job (prune_logs)
# can age out old rows, but existing rows can never be altered/tampered with.
FORWARD = [
    """
    CREATE OR REPLACE FUNCTION library_auditlog_no_update()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'library_auditlog is append-only; UPDATE is not permitted';
    END;
    $$;
    """,
    """
    CREATE TRIGGER library_auditlog_no_update
    BEFORE UPDATE ON library_auditlog
    FOR EACH ROW EXECUTE FUNCTION library_auditlog_no_update();
    """,
]

REVERSE = [
    "DROP TRIGGER IF EXISTS library_auditlog_no_update ON library_auditlog;",
    "DROP FUNCTION IF EXISTS library_auditlog_no_update();",
]


class Migration(migrations.Migration):
    dependencies = [
        ("library", "0025_featureflag"),
    ]

    operations = [
        migrations.RunSQL(sql=FORWARD, reverse_sql=REVERSE),
    ]
