"""URL routes for the catalog app.

Mounted at ``/api/v1/`` from ``config/api_urls.py``. Public browse endpoints
are at the top level; admin CRUD lives under ``catalog/admin/``.
"""

from django.urls import path

from apps.catalog.views import (
    AdminBrandDetailView,
    AdminBrandListCreateView,
    AdminCategoryDetailView,
    AdminCategoryListCreateView,
    AdminFacetDefinitionDetailView,
    AdminFacetDefinitionListCreateView,
    AdminPricingTierDeleteView,
    AdminPricingTierDetailView,
    AdminPricingTierListCreateView,
    AdminProductDetailView,
    AdminProductImageDeleteView,
    AdminProductImageDetailView,
    AdminProductImageListCreateView,
    AdminProductListCreateView,
    AdminProductVariantDetailView,
    AdminProductVariantListCreateView,
    AdminRelatedProductDetailView,
    AdminRelatedProductListCreateView,
    BrandDetailView,
    BrandListView,
    CategoryDetailView,
    CategoryListView,
    ProductDetailView,
    ProductListView,
    ProductPriceView,
)

urlpatterns = [
    # Public browse
    path("categories/", CategoryListView.as_view(), name="category-list"),
    path(
        "categories/<slug:slug>/", CategoryDetailView.as_view(), name="category-detail"
    ),
    path("brands/", BrandListView.as_view(), name="brand-list"),
    path("brands/<slug:slug>/", BrandDetailView.as_view(), name="brand-detail"),
    path("products/", ProductListView.as_view(), name="product-list"),
    path("products/<slug:slug>/", ProductDetailView.as_view(), name="product-detail"),
    path(
        "products/<slug:slug>/price/",
        ProductPriceView.as_view(),
        name="product-price",
    ),
    # Admin CRUD
    path(
        "catalog/admin/categories/",
        AdminCategoryListCreateView.as_view(),
        name="admin-category-list-create",
    ),
    path(
        "catalog/admin/categories/<int:pk>/",
        AdminCategoryDetailView.as_view(),
        name="admin-category-detail",
    ),
    path(
        "catalog/admin/brands/",
        AdminBrandListCreateView.as_view(),
        name="admin-brand-list-create",
    ),
    path(
        "catalog/admin/brands/<int:pk>/",
        AdminBrandDetailView.as_view(),
        name="admin-brand-detail",
    ),
    path(
        "catalog/admin/products/",
        AdminProductListCreateView.as_view(),
        name="admin-product-list-create",
    ),
    path(
        "catalog/admin/products/<int:pk>/",
        AdminProductDetailView.as_view(),
        name="admin-product-detail",
    ),
    path(
        "catalog/admin/products/<int:product_pk>/variants/",
        AdminProductVariantListCreateView.as_view(),
        name="admin-product-variant-list-create",
    ),
    path(
        "catalog/admin/products/<int:product_pk>/variants/<int:pk>/",
        AdminProductVariantDetailView.as_view(),
        name="admin-product-variant-detail",
    ),
    path(
        "catalog/admin/products/<int:product_pk>/images/",
        AdminProductImageListCreateView.as_view(),
        name="admin-product-image-list-create",
    ),
    path(
        "catalog/admin/products/<int:product_pk>/images/<int:pk>/",
        AdminProductImageDetailView.as_view(),
        name="admin-product-image-detail",
    ),
    path(
        "catalog/admin/images/<int:pk>/",
        AdminProductImageDeleteView.as_view(),
        name="admin-product-image-delete",
    ),
    path(
        "catalog/admin/products/<int:product_pk>/pricing-tiers/",
        AdminPricingTierListCreateView.as_view(),
        name="admin-pricing-tier-list-create",
    ),
    path(
        "catalog/admin/products/<int:product_pk>/pricing-tiers/<int:pk>/",
        AdminPricingTierDetailView.as_view(),
        name="admin-pricing-tier-detail",
    ),
    path(
        "catalog/admin/pricing-tiers/<int:pk>/",
        AdminPricingTierDeleteView.as_view(),
        name="admin-pricing-tier-delete",
    ),
    path(
        "catalog/admin/products/<int:product_pk>/related-products/",
        AdminRelatedProductListCreateView.as_view(),
        name="admin-related-product-list-create",
    ),
    path(
        "catalog/admin/products/<int:product_pk>/related-products/<int:pk>/",
        AdminRelatedProductDetailView.as_view(),
        name="admin-related-product-detail",
    ),
    path(
        "catalog/admin/facets/",
        AdminFacetDefinitionListCreateView.as_view(),
        name="admin-facet-list-create",
    ),
    path(
        "catalog/admin/facets/<int:pk>/",
        AdminFacetDefinitionDetailView.as_view(),
        name="admin-facet-detail",
    ),
]
