"""Django admin registration for the content app.

Content pages and banners are managed through the staff API and through
these admin pages. The admin provides a safety net for quick edits without
requiring the API, but the canonical workflow is the API layer which
enforces sanitisation through the services.
"""

from django.contrib import admin

from apps.content.models import Banner, BlogCategory, BlogPost, BlogTag, ContentPage


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


@admin.register(BlogCategory)
class BlogCategoryAdmin(admin.ModelAdmin):
    """Admin page for browsing and editing blog categories."""

    list_display = ("name", "slug", "sort_order", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(BlogTag)
class BlogTagAdmin(admin.ModelAdmin):
    """Admin page for browsing and editing blog tags."""

    list_display = ("name", "slug")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(BlogPost)
class BlogPostAdmin(admin.ModelAdmin):
    """Admin page for browsing and editing blog posts."""

    list_display = ("title", "slug", "status", "published_at", "updated_at")
    list_filter = ("status", "category", "noindex")
    search_fields = ("title", "slug", "excerpt")
    prepopulated_fields = {"slug": ("title",)}
    filter_horizontal = ("tags", "related_products")
