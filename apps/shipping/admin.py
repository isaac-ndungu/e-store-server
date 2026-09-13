"""Django admin registration for delivery areas."""

from django.contrib import admin

from apps.shipping.models import DeliveryArea


@admin.register(DeliveryArea)
class DeliveryAreaAdmin(admin.ModelAdmin):
    """Admin page for the service-area list."""

    list_display = ("county", "area_name", "is_active")
    list_filter = ("is_active", "county")
    search_fields = ("county", "area_name")
