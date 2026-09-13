"""Serializers for the cart app.

Write serializers whitelist exactly what a visitor may send (variant,
quantity, optional bundle); prices are never an input. Read serializers
carry the server-computed unit and line totals alongside the line identity.
"""

from rest_framework import serializers

from apps.cart.selectors import MAX_LINE_QUANTITY


class CartItemAddSerializer(serializers.Serializer):
    """Input for adding a line to the visitor's cart."""

    variant_id = serializers.IntegerField(min_value=1)
    quantity = serializers.IntegerField(
        min_value=1, max_value=MAX_LINE_QUANTITY, default=1
    )
    bundle_id = serializers.IntegerField(
        min_value=1, required=False, allow_null=True, default=None
    )


class CartItemUpdateSerializer(serializers.Serializer):
    """Input for changing a cart line's quantity."""

    quantity = serializers.IntegerField(min_value=1, max_value=MAX_LINE_QUANTITY)


class CartLineSerializer(serializers.Serializer):
    """Read shape for one priced cart line."""

    id = serializers.IntegerField()
    variant_id = serializers.IntegerField(source="variant.id")
    sku = serializers.CharField(source="variant.sku")
    product_name = serializers.SerializerMethodField()
    attributes = serializers.DictField(source="variant.attributes")
    bundle_id = serializers.IntegerField(source="bundle.id", allow_null=True)
    bundle_name = serializers.SerializerMethodField()
    quantity = serializers.IntegerField()
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2)
    line_total = serializers.DecimalField(max_digits=12, decimal_places=2)
    added_at = serializers.DateTimeField(source="item.added_at")

    def get_product_name(self, obj):
        """Return the line's product name, falling back to the sku.

        Args:
            obj (dict): the priced line entry.

        Returns:
            str: the product name or variant sku.
        """
        product = getattr(obj["variant"], "product", None)
        if product is not None:
            return product.name
        return obj["variant"].sku

    def get_bundle_name(self, obj):
        """Return the bundle name, or None for a standalone line.

        Args:
            obj (dict): the priced line entry.

        Returns:
            str | None: the bundle name.
        """
        bundle = obj.get("bundle")
        return bundle.name if bundle is not None else None


class CartSerializer(serializers.Serializer):
    """Read shape for the visitor's cart."""

    id = serializers.IntegerField()
    item_count = serializers.IntegerField()
    subtotal = serializers.DecimalField(max_digits=12, decimal_places=2)
    updated_at = serializers.DateTimeField()
    items = CartLineSerializer(many=True)
