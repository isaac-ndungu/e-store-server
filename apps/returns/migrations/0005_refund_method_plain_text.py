# Generated to simplify the refund method to a plain staff description:
# refunds are arranged by hand, so the field records how the money went
# back instead of selecting a payout integration.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("returns", "0004_alter_returnrequest_refund_method"),
    ]

    operations = [
        migrations.AlterField(
            model_name="returnrequest",
            name="refund_method",
            field=models.CharField(
                blank=True,
                help_text="How the refund was sent, in staff words — e.g. "
                "'M-Pesa - sent manually'. Recorded, never executed.",
                max_length=100,
            ),
        ),
    ]
