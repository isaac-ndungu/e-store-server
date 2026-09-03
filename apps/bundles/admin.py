from django.contrib import admin

from apps.bundles.models import Bundle, BundleItem


class BundleItemInline(admin.TabularInline):
    """Inline editor of the components belonging to a bundle."""

    model = BundleItem
    extra = 0
    autocomplete_fields = ["product", "variant"]


@admin.register(Bundle)
class BundleAdmin(admin.ModelAdmin):
    """Admin page for bundles and their components."""

    list_display = (
        "name",
        "slug",
        "discount_type",
        "discount_value",
        "is_active",
        "starts_at",
        "ends_at",
    )
    list_filter = ("is_active", "discount_type")
    search_fields = ("name", "slug", "description")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [BundleItemInline]


@admin.register(BundleItem)
class BundleItemAdmin(admin.ModelAdmin):
    """Admin page for individual bundle components."""

    list_display = ("bundle", "product", "variant", "quantity", "is_optional")
    list_filter = ("bundle", "is_optional")
    search_fields = ("bundle__name", "product__name", "product__sku")
    autocomplete_fields = ["product", "variant"]
