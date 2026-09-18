"""API serializers for the orders app.

Writable serializers whitelist exactly the staff-supplied fields accepted by
the order-intake endpoint. Read serializers shape the order response:
server-computed money as decimal strings, per-line item detail, and the
status audit trail.

No client-supplied price, total, or discount amount is ever read — the order
money fields are computed by the service layer from live catalogue prices.
The one exception is ``delivery_fee``: no system source exists for it, so
staff type in the quoted amount and it is validated non-negative here.
"""

from decimal import Decimal

from rest_framework import serializers

from apps.orders.models import (
    Order,
    OrderItem,
    OrderStatusHistory,
)


class StaffOrderIntakeItemSerializer(serializers.Serializer):
    """One staff-entered intake line."""

    variant_id = serializers.IntegerField(min_value=1)
    quantity = serializers.IntegerField(min_value=1, max_value=999)


class StaffOrderIntakeSerializer(serializers.Serializer):
    """Input for the staff order-intake endpoint.

    Prices are never accepted here — every line is repriced server-side from
    the current catalogue/promotion state. The exception is ``delivery_fee``:
    no system source exists for it, so staff type in the amount they quoted
    the customer. ``inquiry_id`` optionally links the created order back to
    the originating hand-off capture.
    """

    phone = serializers.CharField(max_length=15)
    email = serializers.EmailField(required=False, allow_blank=True, default="")
    notes = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    order_source = serializers.ChoiceField(choices=Order.ORDER_SOURCE_CHOICES)
    payment_method = serializers.ChoiceField(choices=Order.PAYMENT_METHOD_CHOICES)
    payment_reference = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default=""
    )
    delivery_area_id = serializers.IntegerField(required=False, allow_null=True)
    delivery_fee = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, default=Decimal("0.00")
    )
    shipping_address_id = serializers.IntegerField(required=False, allow_null=True)
    inquiry_id = serializers.IntegerField(required=False, allow_null=True)
    items = StaffOrderIntakeItemSerializer(many=True, min_length=1, max_length=100)

    def validate_delivery_fee(self, value):
        """Reject a negative staff-quoted delivery fee.

        Args:
            value (Decimal): the quoted fee.

        Returns:
            Decimal: the fee unchanged.

        Raises:
            serializers.ValidationError: if the fee is negative.
        """
        if value < 0:
            raise serializers.ValidationError("delivery_fee must not be negative.")
        return value


class OrderItemSerializer(serializers.ModelSerializer):
    """Read-only detail for one order line."""

    class Meta:
        model = OrderItem
        fields = [
            "id",
            "product",
            "bundle",
            "bundle_group_id",
            "variant_sku",
            "product_name",
            "variant_attributes",
            "unit_price",
            "quantity",
            "total_price",
            "applied_discount",
            "tax_rate",
            "tax",
        ]
        read_only_fields = fields


class OrderStatusHistorySerializer(serializers.ModelSerializer):
    """Read-only detail for one status transition."""

    class Meta:
        model = OrderStatusHistory
        fields = [
            "from_status",
            "to_status",
            "changed_by",
            "note",
            "changed_at",
        ]
        read_only_fields = fields


class OrderListSerializer(serializers.ModelSerializer):
    """List-view shape exposing a compact order summary."""

    item_count = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            "id",
            "status",
            "payment_method",
            "order_source",
            "payment_reference",
            "subtotal",
            "delivery_fee",
            "tax_total",
            "discount_total",
            "grand_total",
            "placed_at",
            "item_count",
        ]
        read_only_fields = fields

    def get_item_count(self, obj):
        """Return the number of physical line items on the order.

        Prefers the ``item_count`` annotation from the staff list selector so
        the list never issues a query per row; falls back to a count query.

        Args:
            obj (Order): the order.

        Returns:
            int: the count of order items.
        """
        annotated = obj.__dict__.get("item_count")
        if isinstance(annotated, int):
            return annotated
        return obj.items.count()


class OrderDetailSerializer(serializers.ModelSerializer):
    """Detail-view shape with items and the status trail."""

    items = OrderItemSerializer(many=True, read_only=True)
    status_history = OrderStatusHistorySerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "status",
            "payment_method",
            "order_source",
            "staff_created_by",
            "payment_reference",
            "currency",
            "subtotal",
            "delivery_fee",
            "shipping_tax_rate",
            "shipping_tax_amount",
            "tax_total",
            "discount_total",
            "grand_total",
            "phone",
            "email",
            "shipping_address",
            "delivery_area",
            "refund_note",
            "refund_amount",
            "placed_at",
            "updated_at",
            "items",
            "status_history",
        ]
        read_only_fields = fields


class OrderStatusUpdateSerializer(serializers.Serializer):
    """Input for a staff user advancing an order's fulfilment status.

    ``to_status`` is constrained to the order status choices; the legality of
    the specific transition (against the current status) is enforced in the
    service layer and surfaced as a 400. ``note`` is optional and goes into the
    status-history audit trail.
    """

    to_status = serializers.ChoiceField(choices=Order.STATUS_CHOICES)
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)
