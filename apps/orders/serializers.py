"""API serializers for the orders app.

Writable serializers whitelist exactly the client-supplied fields accepted
when creating an order. Read serializers shape the detailed order response for
the storefront: server-computed money as decimal strings, per-line item detail,
the status audit trail, and (for COD orders) the caller's verification state.

No client-supplied price, total, or discount amount is ever read — the order
money fields are computed by the service layer from live catalogue prices and
the order snapshot.
"""

from rest_framework import serializers

from apps.orders.models import (
    Order,
    OrderItem,
    OrderStatusHistory,
    OrderVerification,
)


class OrderCreateSerializer(serializers.Serializer):
    """Input accepted when placing an order.

    ``phone`` is the order-level contact number (may differ from the account's
    own). ``delivery_zone_id`` selects the zone that prices shipping and
    routes warehouse selection. ``shipping_address_id`` optionally references
    a stored address; the address is validated for ownership at the view layer.
    """

    phone = serializers.CharField(max_length=15)
    shipping_address_id = serializers.IntegerField(required=False, allow_null=True)
    delivery_zone_id = serializers.IntegerField(required=False, allow_null=True)
    payment_method = serializers.ChoiceField(choices=Order.PAYMENT_METHOD_CHOICES)
    email = serializers.EmailField(required=False, allow_blank=True, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True)


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
            "fulfillment_warehouse",
        ]


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


class OrderVerificationSerializer(serializers.ModelSerializer):
    """Read-only detail of a COD order's verification state.

    The OTP code itself is never exposed over the API — only status, attempt
    count, and timestamps. Callers see only whether verification is pending,
    verified, expired, or failed.
    """

    class Meta:
        model = OrderVerification
        fields = [
            "phone_number",
            "status",
            "attempts",
            "sent_at",
            "verified_at",
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
            "subtotal",
            "shipping_total",
            "tax_total",
            "discount_total",
            "grand_total",
            "placed_at",
            "item_count",
        ]

    def get_item_count(self, obj):
        """Return the number of physical line items on the order.

        Args:
            obj (Order): the order.

        Returns:
            int: the count of order items.
        """
        return obj.items.count()


class OrderDetailSerializer(serializers.ModelSerializer):
    """Detail-view shape with items, status trail, and verification."""

    items = OrderItemSerializer(many=True, read_only=True)
    status_history = OrderStatusHistorySerializer(many=True, read_only=True)
    verification = OrderVerificationSerializer(read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "lookup_token",
            "status",
            "payment_method",
            "currency",
            "subtotal",
            "shipping_total",
            "shipping_tax_rate",
            "shipping_tax_amount",
            "tax_total",
            "discount_total",
            "grand_total",
            "phone",
            "email",
            "shipping_address",
            "delivery_zone",
            "placed_at",
            "updated_at",
            "items",
            "status_history",
            "verification",
        ]


class OTPVerifySerializer(serializers.Serializer):
    """Input for verifying a COD order code."""

    otp_code = serializers.CharField(max_length=6, min_length=6)


class CancelOrderSerializer(serializers.Serializer):
    """Input for cancelling an order.

    ``note`` is optional and is recorded in the status-history audit trail.
    """

    note = serializers.CharField(required=False, allow_blank=True)


class OrderStatusUpdateSerializer(serializers.Serializer):
    """Input for a staff user advancing an order's fulfilment status.

    ``to_status`` is constrained to the order status choices; the legality of
    the specific transition (against the current status) is enforced in the
    service layer and surfaced as a 400. ``note`` is optional and goes into the
    status-history audit trail.
    """

    to_status = serializers.ChoiceField(choices=Order.STATUS_CHOICES)
    note = serializers.CharField(required=False, allow_blank=True)
