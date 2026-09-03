from rest_framework import serializers

from apps.catalog.models import Brand, Category, Product, ProductVariant
from apps.promotions.models import Coupon, Discount


class DiscountSerializer(serializers.ModelSerializer):
    """Read/write serializer for a discount.

    Read responses surface the full discount definition. Writes accept the
    scope-specific many-to-many relations and validate that a non-sitewide
    scope supplies at least one matching relation.
    """

    variants = serializers.PrimaryKeyRelatedField(
        many=True, queryset=ProductVariant.objects.all(), required=False
    )
    products = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Product.objects.all(), required=False
    )
    categories = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Category.objects.all(), required=False
    )
    brands = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Brand.objects.all(), required=False
    )

    class Meta:
        model = Discount
        fields = [
            "id",
            "name",
            "badge_text",
            "scope",
            "discount_type",
            "value",
            "variants",
            "products",
            "categories",
            "brands",
            "bundle",
            "starts_at",
            "ends_at",
            "is_active",
            "priority",
            "max_redemptions",
            "applies_within_bundles",
            "redemption_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "redemption_count", "created_at", "updated_at"]

    def validate(self, attrs):
        """Enforce scope-relation pairing and a valid window.

        Args:
            attrs (dict): the validated data (partial on PATCH).

        Returns:
            dict: the validated data.

        Raises:
            ValidationError: if the scope is missing its relation or the
                window is reversed.
        """
        instance = self.instance
        scope = attrs.get("scope", getattr(instance, "scope", None))
        starts_at = attrs.get("starts_at", getattr(instance, "starts_at", None))
        ends_at = attrs.get("ends_at", getattr(instance, "ends_at", None))
        if starts_at is not None and ends_at is not None and ends_at < starts_at:
            raise serializers.ValidationError(
                {"ends_at": "ends_at must be on or after starts_at."}
            )
        if scope == "bundle":
            bundle = attrs.get("bundle", getattr(instance, "bundle", None))
            if bundle is None:
                raise serializers.ValidationError(
                    {"bundle": "Select a bundle for this scope."}
                )
            return attrs
        if scope == "sitewide":
            return attrs
        field_names = {
            "variant": "variants",
            "product": "products",
            "category": "categories",
            "brand": "brands",
        }
        if scope in field_names:
            relation_field = field_names[scope]
            relation = attrs.get(relation_field)
            if relation is None and instance is not None:
                relation = getattr(instance, relation_field).all()
            if not relation:
                raise serializers.ValidationError(
                    {relation_field: f"Select at least one {scope} for this scope."}
                )
        return attrs

    def create(self, validated_data):
        """Create the discount via the service."""
        from apps.promotions.services import create_discount

        return create_discount(**validated_data)

    def update(self, instance, validated_data):
        """Update the discount via the service."""
        from apps.promotions.services import update_discount

        return update_discount(instance, **validated_data)


class CouponSerializer(serializers.ModelSerializer):
    """Read/write serializer for a coupon.

    ``value`` is optional only for ``free_shipping`` coupons; the serializer
    validates that a percent or fixed coupon carries a value.
    """

    applies_to_products = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Product.objects.all(), required=False
    )
    applies_to_categories = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Category.objects.all(), required=False
    )

    class Meta:
        model = Coupon
        fields = [
            "id",
            "code",
            "discount_type",
            "value",
            "min_order_value",
            "applies_to_products",
            "applies_to_categories",
            "starts_at",
            "ends_at",
            "usage_limit_total",
            "usage_limit_per_user",
            "is_active",
            "stackable_with_discounts",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        """Require a value for percent/fixed coupons and a valid window.

        Args:
            attrs (dict): the validated data (partial on PATCH).

        Returns:
            dict: the validated data.

        Raises:
            ValidationError: if the window is reversed or a value is missing.
        """
        instance = self.instance
        discount_type = attrs.get(
            "discount_type", getattr(instance, "discount_type", None)
        )
        value = attrs.get("value", getattr(instance, "value", None))
        if discount_type != "free_shipping" and value is None:
            raise serializers.ValidationError(
                {"value": "value is required for percent and fixed coupons."}
            )
        starts_at = attrs.get("starts_at", getattr(instance, "starts_at", None))
        ends_at = attrs.get("ends_at", getattr(instance, "ends_at", None))
        if starts_at is not None and ends_at is not None and ends_at < starts_at:
            raise serializers.ValidationError(
                {"ends_at": "ends_at must be on or after starts_at."}
            )
        return attrs


class CouponValidationSerializer(serializers.Serializer):
    """Read serializer describing a validated coupon.

    Optionally accepts a ``subtotal`` so the endpoint can enforce
    ``min_order_value`` when the caller knows the cart total.
    """

    code = serializers.CharField(max_length=50)
    subtotal = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )


class CouponValidationResultSerializer(serializers.Serializer):
    """Serialized result of a coupon validation check."""

    valid = serializers.BooleanField()
    code = serializers.CharField()
    discount_type = serializers.CharField(allow_null=True)
    value = serializers.CharField(allow_null=True)
    min_order_value = serializers.CharField(allow_null=True)
    reason = serializers.CharField(allow_null=True)


class EffectivePriceResultSerializer(serializers.Serializer):
    """Read serializer describing the effective price of a variant.

    All money fields are returned as strings to travel exactly as computed.
    """

    variant = serializers.IntegerField()
    base_price = serializers.CharField()
    price = serializers.CharField()
    discount = serializers.CharField()
    discount_type = serializers.CharField(allow_null=True)
    discount_name = serializers.CharField(allow_null=True)
    badge_text = serializers.CharField(allow_null=True)
    coupon_discount = serializers.CharField()
    within_bundle = serializers.BooleanField()
