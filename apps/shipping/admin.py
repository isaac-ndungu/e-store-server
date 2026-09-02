"""Django admin registration for delivery zones and routing priorities."""

from django.contrib import admin

from apps.shipping.models import DeliveryZone, WarehouseZonePriority


class WarehouseZonePriorityInline(admin.TabularInline):
    """Inline editor for the warehouses serving a delivery zone."""

    model = WarehouseZonePriority
    extra = 0


@admin.register(DeliveryZone)
class DeliveryZoneAdmin(admin.ModelAdmin):
    """Admin page for delivery zones and their warehouse routing."""

    list_display = (
        "county",
        "area_name",
        "base_fee",
        "per_kg_rate",
        "free_shipping_threshold",
        "estimated_days",
        "is_active",
    )
    list_filter = ("is_active", "county")
    search_fields = ("county", "area_name", "courier_partner")
    inlines = [WarehouseZonePriorityInline]


@admin.register(WarehouseZonePriority)
class WarehouseZonePriorityAdmin(admin.ModelAdmin):
    """Admin page for per-zone warehouse routing priorities."""

    list_display = ("delivery_zone", "warehouse", "priority")
    list_filter = ("delivery_zone",)
    search_fields = ("delivery_zone__area_name", "warehouse__name")
