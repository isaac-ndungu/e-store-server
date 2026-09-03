"""Serializers for the cart app.

Read serializers surface computed totals and line-item pricing alongside the
stored model fields.  Write serializers accept only the fields a client
may submit — ``variant_id``, ``bundle_id``, and ``quantity`` — and never
trust a client-supplied price, total, or discount amount.
"""

from rest_framework import serializers

from apps.bundles.models import Bundle
from apps.cart.models import CartItem, WishlistItem
from apps.catalog.models import ProductVariant


class CartItemSerializer(serializers.ModelSerializer):
    """Read/write serializer for cart line items.

    On write, accepts ``variant_id`` or ``bundle_id`` and ``quantity``.
    On read, surfaces product name, SKU, attributes, and unit price.
    """

    variant_id = serializers.PrimaryKeyRelatedField(
        queryset=ProductVariant.objects.all(),
        source="variant",
        required=False,
        allow_null=True,
    )
    bundle_id = serializers.PrimaryKeyRelatedField(
        queryset=Bundle.objects.none(),
        source="bundle",
        required=False,
        allow_null=True,
    )
    product_name = serializers.CharField(
        source="variant.product.name", read_only=True, default=None
    )
    bundle_name = serializers.CharField(
        source="bundle.name", read_only=True, default=None
    )
    sku = serializers.CharField(source="variant.sku", read_only=True, default=None)
    attributes = serializers.JSONField(
        source="variant.attributes", read_only=True, default=dict
    )

    class Meta:
        model = CartItem
        fields = [
            "id",
            "variant_id",
            "bundle_id",
            "quantity",
            "product_name",
            "bundle_name",
            "sku",
            "attributes",
            "added_at",
        ]
        read_only_fields = ["id", "added_at"]

    def __init__(self, *args, **kwargs):
        """Set the bundle queryset to only active bundles."""
        super().__init__(*args, **kwargs)
        self.fields["bundle_id"].child_relation.queryset = Bundle.objects.filter(
            is_active=True
        )

    def validate(self, attrs):
        """Require exactly one of variant_id or bundle_id.

        Args:
            attrs (dict): the validated data.

        Returns:
            dict: the validated data.

        Raises:
            ValidationError: if both or neither target is set.
        """
        variant = attrs.get("variant")
        bundle = attrs.get("bundle")
        if variant and bundle:
            raise serializers.ValidationError(
                "Provide either variant_id or bundle_id, not both."
            )
        if not variant and not bundle:
            instance = self.instance
            if instance is None or (
                not getattr(instance, "variant_id", None)
                and not getattr(instance, "bundle_id", None)
            ):
                raise serializers.ValidationError(
                    "Either variant_id or bundle_id is required."
                )
        return attrs


class CartItemWriteSerializer(serializers.Serializer):
    """Input serializer for adding/updating a cart item.

    Accepts ``variant_id`` or ``bundle_id`` (mutually exclusive) and
    ``quantity``.  Price is never accepted from the client.
    """

    variant_id = serializers.IntegerField(required=False, allow_null=True)
    bundle_id = serializers.IntegerField(required=False, allow_null=True)
    quantity = serializers.IntegerField(min_value=1, default=1)

    def validate(self, attrs):
        """Require exactly one of variant_id or bundle_id.

        Args:
            attrs (dict): the validated data.

        Returns:
            dict: the validated data.

        Raises:
            ValidationError: if both or neither target is set.
        """
        variant_id = attrs.get("variant_id")
        bundle_id = attrs.get("bundle_id")
        if variant_id and bundle_id:
            raise serializers.ValidationError(
                "Provide either variant_id or bundle_id, not both."
            )
        if not variant_id and not bundle_id:
            raise serializers.ValidationError(
                "Either variant_id or bundle_id is required."
            )
        return attrs


class CartItemQuantitySerializer(serializers.Serializer):
    """Input serializer for updating a cart item's quantity."""

    quantity = serializers.IntegerField(min_value=1)


class CouponApplySerializer(serializers.Serializer):
    """Input serializer for applying a coupon code to the cart."""

    code = serializers.CharField(max_length=50)


class CouponResultSerializer(serializers.Serializer):
    """Output serializer for a coupon validation result."""

    valid = serializers.BooleanField()
    code = serializers.CharField()
    discount_type = serializers.CharField(allow_null=True)
    value = serializers.CharField(allow_null=True)
    min_order_value = serializers.CharField(allow_null=True)
    reason = serializers.CharField(allow_null=True)


class CartLineItemSerializer(serializers.Serializer):
    """Read serializer for a priced cart line item."""

    item_id = serializers.IntegerField()
    type = serializers.ChoiceField(choices=["variant", "bundle"])
    variant_id = serializers.IntegerField(allow_null=True)
    bundle_id = serializers.IntegerField(allow_null=True)
    product_name = serializers.CharField(allow_null=True)
    bundle_name = serializers.CharField(allow_null=True)
    variant_attributes = serializers.JSONField()
    sku = serializers.CharField(allow_null=True)
    unit_price = serializers.CharField()
    base_price = serializers.CharField()
    quantity = serializers.IntegerField()
    line_subtotal = serializers.CharField()
    line_discount = serializers.CharField()
    stock_available = serializers.IntegerField(allow_null=True)
    in_stock = serializers.BooleanField()
    tax_class = serializers.CharField()
    tax_rate = serializers.CharField()
    tax = serializers.CharField()


class CartSummarySerializer(serializers.Serializer):
    """Read serializer for the full cart response including computed totals."""

    id = serializers.IntegerField()
    user = serializers.IntegerField(allow_null=True)
    session_key = serializers.CharField()
    coupon_code = serializers.CharField(allow_null=True)
    items = CartLineItemSerializer(many=True)
    item_count = serializers.IntegerField()
    subtotal = serializers.CharField()
    discount_total = serializers.CharField()
    coupon_discount = serializers.CharField()
    vat_breakdown = serializers.JSONField()
    vat_total = serializers.CharField()
    total = serializers.CharField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class WishlistItemSerializer(serializers.ModelSerializer):
    """Read serializer for wishlist entries."""

    product_name = serializers.CharField(source="product.name", read_only=True)
    product_slug = serializers.CharField(source="product.slug", read_only=True)

    class Meta:
        model = WishlistItem
        fields = [
            "id",
            "product",
            "product_name",
            "product_slug",
            "added_at",
        ]
        read_only_fields = ["id", "added_at"]


class WishlistAddSerializer(serializers.Serializer):
    """Input serializer for adding a product to the wishlist."""

    product_id = serializers.IntegerField()
