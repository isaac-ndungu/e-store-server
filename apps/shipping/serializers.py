"""Serializers for the shipping app.

Read serializers stay flat so list endpoints render without extra queries
beyond the ``select_related``/filtering done in the views. Write serializers
use an explicit field list and never expose more than the models intend for
admin editing. The quote serializer validates the client's quote request
(mixed-weight lines for a delivery zone) and resolves each variant server-side.

Zones additionally validate at write time: the county must be one of Kenya's
47 counties (spelling-tolerantly), money fields must be non-negative or, for
the free-shipping threshold, positive-or-null, and a county + area must be
unique — so bad or ambiguous pricing data never reaches the database.
"""

from rest_framework import serializers

from apps.catalog.models import ProductVariant
from apps.shipping.constants import is_valid_county
from apps.shipping.models import DeliveryZone, WarehouseZonePriority


class DeliveryZoneSerializer(serializers.ModelSerializer):
    """Read/write serializer for delivery zones.

    Read responses expose the zone's fee schedule and estimate for the
    storefront; writes enforce county, money, and uniqueness invariants.
    """

    class Meta:
        model = DeliveryZone
        fields = [
            "id",
            "county",
            "area_name",
            "base_fee",
            "per_kg_rate",
            "free_shipping_threshold",
            "estimated_days",
            "courier_partner",
            "is_active",
        ]
        read_only_fields = ["id"]

    def validate_county(self, value):
        """Reject a county that is not one of the 47 in Kenya.

        Args:
            value (str): the client-supplied county.

        Returns:
            str: the county unchanged.

        Raises:
            ValidationError: if the county does not match a known Kenyan county.
        """
        if not is_valid_county(value):
            raise serializers.ValidationError(
                "county must be one of Kenya's 47 counties."
            )
        return value

    def validate_base_fee(self, value):
        """Reject a negative base fee.

        Args:
            value (Decimal): the base fee.

        Returns:
            Decimal: the base fee unchanged.

        Raises:
            ValidationError: if the fee is negative.
        """
        if value < 0:
            raise serializers.ValidationError("base_fee must not be negative.")
        return value

    def validate_per_kg_rate(self, value):
        """Reject a negative per-kilogram rate.

        Args:
            value (Decimal): the per-kg rate.

        Returns:
            Decimal: the rate unchanged.

        Raises:
            ValidationError: if the rate is negative.
        """
        if value < 0:
            raise serializers.ValidationError("per_kg_rate must not be negative.")
        return value

    def validate_free_shipping_threshold(self, value):
        """Reject a zero or negative free-shipping threshold.

        A threshold of zero would waive the fee on every order, which is
        almost certainly a mis-entry; leave the field null to disable the
        threshold instead.

        Args:
            value (Decimal | None): the threshold.

        Returns:
            Decimal | None: the threshold unchanged.

        Raises:
            ValidationError: if the threshold is zero or negative.
        """
        if value is not None and value <= 0:
            raise serializers.ValidationError(
                "free_shipping_threshold must be positive or left null."
            )
        return value

    def validate(self, attrs):
        """Reject a second zone for the same county + area.

        The model enforces the pair as a uniqueness constraint; checking here
        turns the database error into a clean 400 for the admin caller. On
        updates only the fields actually present are compared, so a PATCH that
        leaves ``area_name`` untouched cannot collide with itself.

        Args:
            attrs (dict): the validated input fields.

        Returns:
            dict: the validated input fields unchanged.

        Raises:
            ValidationError: if the county + area already names a zone.
        """
        instance = self.instance
        county = attrs.get("county", getattr(instance, "county", None))
        area_name = attrs.get("area_name", getattr(instance, "area_name", None))
        if county is not None and area_name is not None:
            duplicate = DeliveryZone.objects.filter(county=county, area_name=area_name)
            if instance is not None:
                duplicate = duplicate.exclude(pk=instance.pk)
            if duplicate.exists():
                raise serializers.ValidationError(
                    {"area_name": "A zone already exists for this county and area."}
                )
        return attrs


class WarehouseZonePrioritySerializer(serializers.ModelSerializer):
    """Read/write serializer for per-zone warehouse routing priorities."""

    warehouse_name = serializers.CharField(
        source="warehouse.name", read_only=True, default=None
    )
    zone_label = serializers.CharField(source="delivery_zone.__str__", read_only=True)

    class Meta:
        model = WarehouseZonePriority
        fields = [
            "id",
            "delivery_zone",
            "warehouse",
            "warehouse_name",
            "priority",
            "zone_label",
        ]
        read_only_fields = ["id"]

    def validate(self, attrs):
        """Reject duplicate warehouse and duplicate rank rows for one zone.

        The model enforces both as unique constraints; checking here turns the
        database error into a clean 400 for the admin caller. On updates only
        the fields actually present in the payload are compared, so a PATCH
        that leaves ``priority`` untouched cannot collide with itself.

        Args:
            attrs (dict): the validated input fields.

        Returns:
            dict: the validated input fields unchanged.

        Raises:
            ValidationError: if the warehouse or the priority already has a
                row for this zone.
        """
        instance = self.instance
        zone = attrs.get("delivery_zone", getattr(instance, "delivery_zone", None))
        warehouse = attrs.get("warehouse", getattr(instance, "warehouse", None))
        priority = attrs.get("priority", getattr(instance, "priority", None))
        errors = {}
        if zone is not None and warehouse is not None:
            duplicate = WarehouseZonePriority.objects.filter(
                delivery_zone=zone, warehouse=warehouse
            )
            if instance is not None:
                duplicate = duplicate.exclude(pk=instance.pk)
            if duplicate.exists():
                errors["warehouse"] = (
                    "This warehouse already has a priority row for this zone."
                )
        if zone is not None and priority is not None:
            duplicate = WarehouseZonePriority.objects.filter(
                delivery_zone=zone, priority=priority
            )
            if instance is not None:
                duplicate = duplicate.exclude(pk=instance.pk)
            if duplicate.exists():
                errors["priority"] = (
                    "Another warehouse already holds this priority for this zone."
                )
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class QuoteLineSerializer(serializers.Serializer):
    """One line of a shipping-quote request: a variant and its quantity.

    The variant field's queryset already restricts to products sold through
    the storefront, so an inactive or discontinued product is rejected by the
    resolver itself with no extra per-line lookup.
    """

    variant = serializers.PrimaryKeyRelatedField(
        queryset=ProductVariant.objects.filter(
            product__is_active=True, product__is_discontinued=False
        )
    )
    quantity = serializers.IntegerField(min_value=1)


class ShippingQuoteSerializer(serializers.Serializer):
    """Validates a full shipping-quote request for a delivery zone."""

    delivery_zone = serializers.PrimaryKeyRelatedField(
        queryset=DeliveryZone.objects.all()
    )
    items = QuoteLineSerializer(many=True, allow_empty=False)

    def validate_delivery_zone(self, value):
        """Reject quoting against a zone not offered to the storefront.

        Args:
            value (DeliveryZone): the resolved zone.

        Returns:
            DeliveryZone: the validated zone.

        Raises:
            ValidationError: if the zone is inactive.
        """
        if not value.is_active:
            raise serializers.ValidationError(
                "This delivery zone is not currently available."
            )
        return value
