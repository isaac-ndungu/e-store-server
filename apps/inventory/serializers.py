"""Serializers for the inventory app.

Read serializers stay flat (variant SKU, product name, warehouse name inline)
so list endpoints need no extra queries beyond the ``select_related`` done in
the views. Write serializers enforce the stock invariants: quantity can only
be increased (through the service layer), and serial-unit statuses that the
reservation lifecycle owns are not directly settable.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from apps.inventory.models import Inventory, SerialUnit, StockReservation, Warehouse
from apps.inventory.services import (
    receive_serial_units,
    receive_stock,
    update_serial_unit_status,
)

# Statuses an admin may set directly; the reservation lifecycle owns the rest.
MANUAL_SERIAL_STATUSES = {"in_stock", "returned", "defective"}


def _request_user(serializer):
    """Return the authenticated user from a serializer's request context.

    Args:
        serializer (Serializer): the serializer whose context to inspect.

    Returns:
        User | None: the requesting user, or None when absent.
    """
    request = serializer.context.get("request")
    if request is None or request.user is None:
        return None
    return request.user if request.user.is_authenticated else None


class WarehouseSerializer(serializers.ModelSerializer):
    """Read/write serializer for warehouses."""

    class Meta:
        model = Warehouse
        fields = ["id", "name", "address", "is_active"]
        read_only_fields = ["id"]


class InventorySerializer(serializers.ModelSerializer):
    """Read serializer for per-warehouse inventory rows."""

    variant_sku = serializers.CharField(source="variant.sku", read_only=True)
    product_name = serializers.CharField(
        source="variant.product.name", read_only=True, default=None
    )
    warehouse_name = serializers.CharField(
        source="warehouse.name", read_only=True, default=None
    )

    class Meta:
        model = Inventory
        fields = [
            "id",
            "variant",
            "variant_sku",
            "product_name",
            "warehouse",
            "warehouse_name",
            "quantity",
            "reserved",
            "available",
            "is_low_stock",
            "low_stock_threshold",
            "updated_at",
        ]
        read_only_fields = ["id", "available", "is_low_stock", "updated_at"]


class InventoryWriteSerializer(serializers.ModelSerializer):
    """Writable serializer for inventory rows (admin only).

    ``variant`` and ``warehouse`` are set on create and immutable afterwards.
    ``quantity`` on update may only grow — otherwise the row is rejected with
    a pointer to the reservation lifecycle.
    """

    class Meta:
        model = Inventory
        fields = [
            "id",
            "variant",
            "warehouse",
            "quantity",
            "low_stock_threshold",
        ]
        read_only_fields = ["id"]

    def validate_quantity(self, value):
        """Reject a non-positive quantity.

        Args:
            value (int): the requested quantity.

        Returns:
            int: the validated quantity.

        Raises:
            ValidationError: if the quantity is not positive.
        """
        if value <= 0:
            raise serializers.ValidationError(
                "Received quantity must be a positive integer."
            )
        return value

    def create(self, validated_data):
        """Create the inventory row via the stock-receiving service.

        Args:
            validated_data (dict): validated creation data.

        Returns:
            Inventory: the created inventory row.

        Raises:
            ValidationError: if receiving is rejected (e.g. the variant is
                serialized and must be stocked via serial units).
        """
        try:
            return receive_stock(user=_request_user(self), **validated_data)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc

    def update(self, instance, validated_data):
        """Increase stock via the service, or adjust the low-stock threshold.

        A PATCH that leaves ``quantity`` unchanged (or only adjusts the
        threshold) is a no-op that still succeeds. Lowering stock is rejected
        with a pointer to the reservation lifecycle.

        Args:
            instance (Inventory): the inventory row being updated.
            validated_data (dict): validated update data (partial on PATCH).

        Returns:
            Inventory: the updated inventory row.

        Raises:
            ValidationError: if a quantity change would decrease stock.
        """
        quantity = validated_data.pop("quantity", None)
        threshold = validated_data.pop("low_stock_threshold", None)

        if quantity is not None and quantity < instance.quantity:
            raise serializers.ValidationError(
                {
                    "quantity": (
                        "Stock can only be increased here. Reserved units "
                        "are returned via reservation release, and "
                        "fulfilled stock is reduced through returns and "
                        "cancellations."
                    )
                }
            )
        if quantity is not None and quantity > instance.quantity:
            try:
                receive_stock(
                    variant=instance.variant,
                    warehouse=instance.warehouse,
                    quantity=quantity - instance.quantity,
                    low_stock_threshold=threshold,
                    user=_request_user(self),
                )
            except DjangoValidationError as exc:
                raise serializers.ValidationError(exc.messages) from exc
            instance.refresh_from_db()
        elif threshold is not None and threshold != instance.low_stock_threshold:
            instance.low_stock_threshold = threshold
            instance.save(update_fields=["low_stock_threshold"])
        return instance


class SerialUnitSerializer(serializers.ModelSerializer):
    """Read serializer for serial units with inline variant and warehouse."""

    variant_sku = serializers.CharField(source="variant.sku", read_only=True)
    product_name = serializers.CharField(
        source="variant.product.name", read_only=True, default=None
    )
    warehouse_name = serializers.CharField(
        source="warehouse.name", read_only=True, default=None
    )

    class Meta:
        model = SerialUnit
        fields = [
            "id",
            "variant",
            "variant_sku",
            "product_name",
            "warehouse",
            "warehouse_name",
            "serial_number",
            "status",
            "reservation",
            "received_at",
        ]
        read_only_fields = ["id", "received_at"]


class SerialUnitWriteSerializer(serializers.ModelSerializer):
    """Writable serializer for serial units (admin only).

    Reception always creates an ``in_stock`` unit in a known warehouse, and
    feeds the count ledger from the registered unit. On update, only
    ``status`` is writable, and only manual transitions are allowed
    (``in_stock`` -> ``returned``/``defective``, ``returned`` ->
    ``in_stock``/``defective``); ``reserved`` and ``sold`` are set
    exclusively by the reservation lifecycle so the serial-unit status and
    the sibling inventory counts never drift.
    """

    STATUS_CHOICES = tuple(
        (value, label)
        for value, label in SerialUnit.STATUS_CHOICES
        if value in MANUAL_SERIAL_STATUSES
    )

    status = serializers.ChoiceField(choices=STATUS_CHOICES, required=False)

    class Meta:
        model = SerialUnit
        fields = [
            "id",
            "variant",
            "warehouse",
            "serial_number",
            "status",
        ]
        read_only_fields = ["id"]

    def validate(self, attrs):
        """Reject serial registration for non-serialized products.

        Args:
            attrs (dict): the validated data (partial on PATCH).

        Returns:
            dict: the validated data.

        Raises:
            ValidationError: when creating a unit for a product that does not
                track serial numbers.
        """
        variant = attrs.get("variant", getattr(self.instance, "variant", None))
        if variant is not None and not variant.product.tracks_serial_numbers:
            raise serializers.ValidationError(
                {"variant": "This product does not track serial numbers."}
            )
        if self.instance is None and "warehouse" not in attrs:
            raise serializers.ValidationError(
                {"warehouse": "A warehouse is required when receiving a serial unit."}
            )
        return attrs

    def create(self, validated_data):
        """Register the serial unit as in-stock via the receiving service.

        Args:
            validated_data (dict): validated creation data.

        Returns:
            SerialUnit: the created in-stock serial unit.

        Raises:
            ValidationError: if the product does not track serial numbers or
                the serial number is a duplicate.
        """
        try:
            created = receive_serial_units(
                variant=validated_data.pop("variant"),
                warehouse=validated_data.pop("warehouse"),
                serial_numbers=[validated_data.pop("serial_number")],
                user=_request_user(self),
            )
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return created[0]

    def update(self, instance, validated_data):
        """Apply a manual status transition via the service layer.

        Args:
            instance (SerialUnit): the unit being updated.
            validated_data (dict): validated update data.

        Returns:
            SerialUnit: the updated unit.

        Raises:
            ValidationError: if a non-status field is modified or the
                transition is not allowed for the unit's current state.
        """
        new_status = validated_data.pop("status", None)
        if validated_data:
            raise serializers.ValidationError(
                {
                    field: "This field cannot be changed on an existing serial "
                    "unit; only status is writable here."
                    for field in validated_data
                }
            )
        if new_status is None or new_status == instance.status:
            return instance
        try:
            update_serial_unit_status(instance, new_status, user=_request_user(self))
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        instance.refresh_from_db()
        return instance


class StockReservationSerializer(serializers.ModelSerializer):
    """Read serializer for stock reservations."""

    variant_sku = serializers.CharField(source="inventory.variant.sku", read_only=True)
    product_name = serializers.CharField(
        source="inventory.variant.product.name", read_only=True, default=None
    )
    warehouse_name = serializers.CharField(
        source="inventory.warehouse.name", read_only=True, default=None
    )

    class Meta:
        model = StockReservation
        fields = [
            "id",
            "inventory",
            "variant_sku",
            "product_name",
            "warehouse_name",
            "quantity",
            "reserved_at",
            "expires_at",
            "released_at",
            "status",
        ]
        read_only_fields = fields
