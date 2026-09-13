# Generated to replace the priced delivery-zone catalogue with a plain
# service-area list. The zone table is renamed (areas keep their rows) and
# the fee columns drop; the warehouse-routing table goes away entirely.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("shipping", "0003_alter_deliveryzone_unique_together_and_more"),
    ]

    operations = [
        migrations.RenameModel(
            old_name="DeliveryZone",
            new_name="DeliveryArea",
        ),
        migrations.RemoveConstraint(
            model_name="deliveryarea",
            name="dz_base_fee_gte_0",
        ),
        migrations.RemoveConstraint(
            model_name="deliveryarea",
            name="dz_per_kg_rate_gte_0",
        ),
        migrations.RemoveConstraint(
            model_name="deliveryarea",
            name="dz_threshold_null_or_positive",
        ),
        migrations.RemoveField(
            model_name="deliveryarea",
            name="base_fee",
        ),
        migrations.RemoveField(
            model_name="deliveryarea",
            name="per_kg_rate",
        ),
        migrations.RemoveField(
            model_name="deliveryarea",
            name="free_shipping_threshold",
        ),
        migrations.RemoveField(
            model_name="deliveryarea",
            name="estimated_days",
        ),
        migrations.RemoveField(
            model_name="deliveryarea",
            name="courier_partner",
        ),
    ]
