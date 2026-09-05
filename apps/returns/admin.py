from django.contrib import admin

from apps.returns.models import ReturnRequest, ReturnRequestStatusHistory


class ReturnRequestStatusHistoryInline(admin.TabularInline):
    """Inline audit trail for a return request."""

    model = ReturnRequestStatusHistory
    extra = 0
    readonly_fields = ("from_status", "to_status", "changed_by", "note", "changed_at")


@admin.register(ReturnRequest)
class ReturnRequestAdmin(admin.ModelAdmin):
    """Read-only admin view of return requests and their resolution state."""

    list_display = (
        "pk",
        "order",
        "order_item",
        "status",
        "requested_resolution",
        "refund_method",
        "refund_amount",
        "restocking_fee_applied",
        "created_at",
        "resolved_at",
    )
    list_filter = ("status", "requested_resolution", "refund_method")
    search_fields = ("order__phone", "order__id", "order_item__variant_sku")
    readonly_fields = (
        "order",
        "order_item",
        "reason",
        "status",
        "requested_resolution",
        "restocking_fee_applied",
        "refund_amount",
        "refund_method",
        "created_at",
        "resolved_at",
    )
    inlines = [ReturnRequestStatusHistoryInline]

    def has_add_permission(self, request):
        """Return requests are only ever created through the API service."""
        return False

    def has_change_permission(self, request, obj=None):
        """Status and amounts are mutated by the service layer, not the admin."""
        return False

    def has_delete_permission(self, request, obj=None):
        """Return requests are an audit record and must not be deleted."""
        return False
