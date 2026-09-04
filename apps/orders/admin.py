from django.contrib import admin

from apps.orders.models import (
    Order,
    OrderItem,
    OrderStatusHistory,
    OrderVerification,
)


class OrderItemInline(admin.TabularInline):
    """Inline editor for an order's line items."""

    model = OrderItem
    extra = 0


class OrderStatusHistoryInline(admin.TabularInline):
    """Inline editor for an order's status audit trail."""

    model = OrderStatusHistory
    extra = 0
    readonly_fields = ("from_status", "to_status", "changed_by", "changed_at")


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    """Admin for orders with line items and status history inlined."""

    list_display = (
        "id",
        "phone",
        "status",
        "payment_method",
        "grand_total",
        "placed_at",
    )
    list_filter = ("status", "payment_method", "placed_at")
    search_fields = ("id", "phone", "email")
    inlines = [OrderItemInline, OrderStatusHistoryInline]
    readonly_fields = ("placed_at", "updated_at")


@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    """Admin for order line items."""

    list_display = (
        "id",
        "order",
        "product_name",
        "variant_sku",
        "quantity",
        "total_price",
    )
    list_filter = ("tax_rate",)


@admin.register(OrderStatusHistory)
class OrderStatusHistoryAdmin(admin.ModelAdmin):
    """Admin for the order status audit trail."""

    list_display = ("order", "from_status", "to_status", "changed_by", "changed_at")
    readonly_fields = ("order", "from_status", "to_status", "changed_by", "changed_at")


@admin.register(OrderVerification)
class OrderVerificationAdmin(admin.ModelAdmin):
    """Admin for COD order verification records."""

    list_display = ("order", "phone_number", "status", "attempts", "verified_at")
    readonly_fields = ("otp_code",)
