from rest_framework import serializers

from apps.bundles.constants import DISCOUNT_TYPE_VALUES
from apps.bundles.models import Bundle, BundleItem


class BundleItemSerializer(serializers.ModelSerializer):
    """Read/write serializer for a bundle's component items.

    Read responses surface the component's product display data so the
    detail endpoint can show what the bundle contains without extra
    lookups on the storefront.
    """

    product_name = serializers.CharField(source="product.name", read_only=True)
    product_slug = serializers.CharField(source="product.slug", read_only=True)
    product_sku = serializers.CharField(source="product.sku", read_only=True)

    class Meta:
        model = BundleItem
        fields = [
            "id",
            "bundle",
            "product",
            "product_name",
            "product_slug",
            "product_sku",
            "quantity",
            "is_optional",
        ]
        read_only_fields = ["id", "bundle"]

    def validate(self, attrs):
        """Reject a component that cannot be priced.

        The item's product must be active so the price service can resolve
        a billable figure.

        Args:
            attrs (dict): the validated data (partial on PATCH).

        Returns:
            dict: the validated data.

        Raises:
            ValidationError: if the product is missing or inactive.
        """
        instance = self.instance
        product = attrs.get("product", getattr(instance, "product", None))
        if product is not None and not product.is_active:
            raise serializers.ValidationError(
                {"product": "This product is not active and cannot be priced."}
            )
        return attrs


class BundleSerializer(serializers.ModelSerializer):
    """Read/write serializer for bundles.

    Read responses surface the bundle's discount metadata and component items
    so the storefront can render the bundle; list payloads omit the nested
    items via ``BundleListSerializer``. Writes enforce a valid discount type
    and a sane window.
    """

    items = BundleItemSerializer(many=True, read_only=True)

    class Meta:
        model = Bundle
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "discount_type",
            "discount_value",
            "is_active",
            "starts_at",
            "ends_at",
            "items",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "items", "created_at", "updated_at"]
        extra_kwargs = {"slug": {"required": False}}

    def validate(self, attrs):
        """Require a valid discount type and window.

        Confirms the discount type is registered and that, when both window
        bounds are set, ``starts_at`` is not after ``ends_at``.

        Args:
            attrs (dict): the validated data (partial on PATCH).

        Returns:
            dict: the validated data.

        Raises:
            ValidationError: if the discount type or window is invalid.
        """
        instance = self.instance
        discount_type = attrs.get(
            "discount_type", getattr(instance, "discount_type", None)
        )
        if discount_type is not None and discount_type not in DISCOUNT_TYPE_VALUES:
            raise serializers.ValidationError(
                {"discount_type": "discount_type must be 'percent' or 'fixed'."}
            )
        starts_at = attrs.get("starts_at", getattr(instance, "starts_at", None))
        ends_at = attrs.get("ends_at", getattr(instance, "ends_at", None))
        if starts_at is not None and ends_at is not None and ends_at < starts_at:
            raise serializers.ValidationError(
                {"ends_at": "ends_at must be on or after starts_at."}
            )
        return attrs

    def create(self, validated_data):
        """Create the bundle via the service."""
        from apps.bundles.services import create_bundle

        return create_bundle(**validated_data)

    def update(self, instance, validated_data):
        """Update the bundle's fields via the service."""
        from apps.bundles.services import update_bundle

        return update_bundle(instance, **validated_data)


class BundleListSerializer(serializers.ModelSerializer):
    """Slim bundle representation for list endpoints.

    Deliberately omits the nested component items so a bundle list stays small
    for data-conscious shoppers; the detail endpoint supplies them.
    """

    class Meta:
        model = Bundle
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "discount_type",
            "discount_value",
            "is_active",
            "starts_at",
            "ends_at",
        ]
        read_only_fields = fields


class BundlePriceSerializer(serializers.ModelSerializer):
    """Read-only serializer for a bundle's computed price.

    Brings together the bundle's discount metadata and the authoritative price
    fields from the price service. The per-component price breakdown is
    supplied alongside by the view from ``get_bundle_price`` rather than
    re-derived here.
    """

    regular_price = serializers.CharField(read_only=True)
    discount = serializers.CharField(read_only=True)
    price = serializers.CharField(read_only=True)

    class Meta:
        model = Bundle
        fields = [
            "id",
            "slug",
            "name",
            "discount_type",
            "discount_value",
            "regular_price",
            "discount",
            "price",
        ]
        read_only_fields = fields
