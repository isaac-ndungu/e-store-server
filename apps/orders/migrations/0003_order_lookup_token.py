import uuid

from django.db import migrations, models


def backfill_lookup_tokens(apps, schema_editor):
    """Assign a unique lookup token to every existing order.

    Returns:
        None
    """
    Order = apps.get_model("orders", "Order")
    for order in Order.objects.all().iterator():
        order.lookup_token = uuid.uuid4()
        order.save(update_fields=["lookup_token"])


class Migration(migrations.Migration):
    """Add an unguessable lookup token for guest order access."""

    dependencies = [
        ("orders", "0002_orderverification_resend_count"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="lookup_token",
            field=models.UUIDField(default=uuid.uuid4, unique=True, null=True),
        ),
        migrations.RunPython(backfill_lookup_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="order",
            name="lookup_token",
            field=models.UUIDField(default=uuid.uuid4, unique=True, editable=False),
        ),
    ]
