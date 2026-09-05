"""Django admin registration for the content app.

Content pages and banners are managed through the staff API and through
these admin pages. The admin provides a safety net for quick edits without
requiring the API, but the canonical workflow is the API layer which
enforces sanitisation through the services.
"""

from django.contrib import admin

from apps.content.models import Banner, ContentPage


@admin.register(ContentPage)
class ContentPageAdmin(admin.ModelAdmin):
    """Admin page for browsing and editing content pages."""

    list_display = ("title", "slug", "is_published", "updated_at")
    list_filter = ("is_published",)
    search_fields = ("title", "slug", "body")
    prepopulated_fields = {"slug": ("title",)}


@admin.register(Banner)
class BannerAdmin(admin.ModelAdmin):
    """Admin page for browsing and editing promotional banners."""

    list_display = (
        "title",
        "placement",
        "sort_order",
        "is_active",
        "starts_at",
        "ends_at",
    )
    list_filter = ("is_active", "placement")
    search_fields = ("title", "placement")
