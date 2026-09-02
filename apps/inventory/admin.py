from django.contrib import admin

from apps.inventory.models import (
    Inventory,
    SerialUnit,
    StockMovementLog,
    StockReservation,
    Warehouse,
)


@admin.register(Warehouse)
class WarehouseAdmin(admin.ModelAdmin):
    """Admin page for warehouse locations."""

    list_display = ("name", "address", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "address")


@admin.register(Inventory)
class InventoryAdmin(admin.ModelAdmin):
    """Admin page for per-warehouse inventory rows."""

    list_display = (
        "variant",
        "warehouse",
        "quantity",
        "reserved",
        "available",
        "is_low_stock",
    )
    list_filter = ("warehouse",)
    search_fields = ("variant__sku", "variant__product__name", "warehouse__name")
    readonly_fields = ("updated_at",)

    def available(self, obj):
        """Return the un-reserved quantity for a row."""
        return obj.available

    available.short_description = "Available"


@admin.register(SerialUnit)
class SerialUnitAdmin(admin.ModelAdmin):
    """Admin page for individually tracked serial units."""

    list_display = (
        "serial_number",
        "variant",
        "warehouse",
        "status",
        "reservation",
        "received_at",
    )
    list_filter = ("status", "warehouse")
    search_fields = ("serial_number", "variant__sku", "variant__product__name")


@admin.register(StockReservation)
class StockReservationAdmin(admin.ModelAdmin):
    """Admin page for stock reservations."""

    list_display = ("inventory", "quantity", "status", "reserved_at", "expires_at")
    list_filter = ("status",)
    readonly_fields = (
        "inventory",
        "quantity",
        "reserved_at",
        "expires_at",
        "released_at",
    )


@admin.register(StockMovementLog)
class StockMovementLogAdmin(admin.ModelAdmin):
    """Read-only audit page for stock-affecting operations."""

    list_display = (
        "created_at",
        "user",
        "action",
        "variant",
        "warehouse",
        "quantity_change",
        "reserved_change",
        "serial_number",
    )
    list_filter = ("action",)
    search_fields = ("serial_number", "variant__sku", "user__email")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        """Movement logs are append-only; no manual creation."""
        return False
