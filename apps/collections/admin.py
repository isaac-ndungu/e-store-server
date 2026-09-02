"""Django admin registration for collections and their membership rows."""

from django.contrib import admin

from apps.collections.models import Collection, CollectionMembership


class CollectionMembershipInline(admin.TabularInline):
    """Inline editor of the products belonging to a collection."""

    model = CollectionMembership
    extra = 0
    autocomplete_fields = ["product"]


@admin.register(Collection)
class CollectionAdmin(admin.ModelAdmin):
    """Admin page for collections and their memberships."""

    list_display = (
        "name",
        "slug",
        "collection_type",
        "smart_rule",
        "rule_window_days",
        "rule_threshold",
        "display_location",
        "sort_order",
        "is_active",
    )
    list_filter = ("is_active", "collection_type", "smart_rule", "display_location")
    search_fields = ("name", "slug", "description")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [CollectionMembershipInline]


@admin.register(CollectionMembership)
class CollectionMembershipAdmin(admin.ModelAdmin):
    """Admin page for individual collection membership rows."""

    list_display = ("collection", "product", "sort_order", "added_at")
    list_filter = ("collection",)
    search_fields = ("collection__name", "product__name", "product__sku")
    autocomplete_fields = ["product"]
