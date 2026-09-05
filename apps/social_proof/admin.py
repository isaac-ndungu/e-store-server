"""Django admin registration for the social proof app."""

from django.contrib import admin

from apps.social_proof.models import ProductViewEvent


@admin.register(ProductViewEvent)
class ProductViewEventAdmin(admin.ModelAdmin):
    """Admin page for browsing the durable product-view event log."""

    list_display = ("product", "session_key", "created_at")
    list_filter = ("product",)
    search_fields = ("product__name", "product__slug", "product__sku", "session_key")
    date_hierarchy = "created_at"
