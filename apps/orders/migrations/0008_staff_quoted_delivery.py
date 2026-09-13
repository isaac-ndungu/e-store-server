# Generated to switch order intake to staff-quoted delivery: the computed
# shipping total becomes the staff-entered delivery fee (values preserved),
# the zone link follows the zone-to-area rename, cancellation/return refunds
# gain their record fields, and the per-line fulfilment warehouse goes away
# with warehouse tracking.

from django.db import migrations, models
from django.db.models import Q
from django.db.models.constraints import CheckConstraint


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0007_alter_order_payment_method"),
        ("shipping", "0004_deliveryarea"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="order",
            name="order_money_fields_nonnegative",
        ),
        migrations.RenameField(
            model_name="order",
            old_name="shipping_total",
            new_name="delivery_fee",
        ),
        migrations.RenameField(
            model_name="order",
            old_name="delivery_zone",
            new_name="delivery_area",
        ),
        migrations.AlterField(
            model_name="order",
            name="delivery_area",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name="orders",
                to="shipping.deliveryarea",
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="refund_amount",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name="order",
            name="refund_note",
            field=models.TextField(blank=True),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=CheckConstraint(
                condition=Q(subtotal__gte=0)
                & Q(delivery_fee__gte=0)
                & Q(tax_total__gte=0)
                & Q(grand_total__gte=0)
                & Q(refund_amount__gte=0),
                name="order_money_fields_nonnegative",
            ),
        ),
    ]
