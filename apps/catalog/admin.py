from django.contrib import admin

from apps.catalog.models import (
    Brand,
    Category,
    FacetDefinition,
    PricingTier,
    Product,
    ProductImage,
    ProductVariant,
    RelatedProduct,
)


class ProductVariantInline(admin.TabularInline):
    """Inline editor for a product's variants within the product page."""

    model = ProductVariant
    extra = 1
    ordering = ["sku"]


class ProductImageInline(admin.TabularInline):
    """Inline editor for a product's images within the product page."""

    model = ProductImage
    extra = 1
    ordering = ["sort_order"]


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    """Admin page for product categories."""

    list_display = ("name", "slug", "parent", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    """Admin page for product brands."""

    list_display = ("name", "slug", "is_authorized_dealer")
    list_filter = ("is_authorized_dealer",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    """Admin page for products with inline variants and images."""

    list_display = (
        "name",
        "slug",
        "sku",
        "category",
        "brand",
        "is_active",
        "is_featured",
        "created_at",
    )
    list_filter = ("is_active", "is_featured", "product_type", "condition", "brand")
    search_fields = ("name", "slug", "sku", "manufacturer_model_number")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [ProductVariantInline, ProductImageInline]
    readonly_fields = ("created_at", "updated_at")


@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    """Admin page for product variants."""

    list_display = ("sku", "product", "price", "is_active")
    list_filter = ("is_active",)
    search_fields = ("sku", "product__name")


@admin.register(ProductImage)
class ProductImageAdmin(admin.ModelAdmin):
    """Admin page for product images."""

    list_display = ("product", "is_primary", "sort_order")
    list_filter = ("is_primary",)


@admin.register(RelatedProduct)
class RelatedProductAdmin(admin.ModelAdmin):
    """Admin page for related-product cross-links."""

    list_display = ("product", "related_product", "relation_type", "sort_order")
    list_filter = ("relation_type",)


@admin.register(PricingTier)
class PricingTierAdmin(admin.ModelAdmin):
    """Admin page for volume-based pricing tiers."""

    list_display = ("variant", "min_quantity", "unit_price")
    list_filter = ("variant__product",)


@admin.register(FacetDefinition)
class FacetDefinitionAdmin(admin.ModelAdmin):
    """Admin page for facet definitions."""

    list_display = (
        "name",
        "source_field",
        "key",
        "field_name",
        "facet_type",
        "is_active",
    )
    list_filter = ("source_field", "facet_type", "is_active")
    search_fields = ("name", "key", "field_name")
