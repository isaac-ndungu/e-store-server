"""Selectors for the catalog app.

Optimized read-only query helpers that keep views thin and give a single
reference for how catalog data is fetched. Every queryset uses
``select_related`` / ``prefetch_related`` to prevent N+1 queries.
"""

from django.db.models import Prefetch

from apps.catalog.models import Brand, Category, Product, ProductVariant


def get_active_categories():
    """Return active categories for public browsing.

    Returns:
        QuerySet: active ``Category`` rows with a product count annotation.
    """
    return (
        Category.objects.filter(is_active=True)
        .select_related("parent")
        .order_by("name")
    )


def get_category_by_slug(slug):
    """Return an active category by slug, or ``None``.

    Args:
        slug (str): the category slug.

    Returns:
        Category | None: the matching category, or None.
    """
    return (
        Category.objects.filter(slug=slug, is_active=True)
        .select_related("parent")
        .first()
    )


def get_active_brands():
    """Return all brands for public browsing.

    The ``Brand`` model has no ``is_active`` flag — every brand is shown.

    Returns:
        QuerySet: ``Brand`` rows ordered by name.
    """
    return Brand.objects.all().order_by("name")


def get_brand_by_slug(slug):
    """Return an active brand by slug, or ``None``.

    Args:
        slug (str): the brand slug.

    Returns:
        Brand | None: the matching brand, or None.
    """
    return Brand.objects.filter(slug=slug).first()


def get_product_list_queryset():
    """Return the base queryset for the public product list.

    Pre-fetches category and brand to avoid N+1 on list serialization.
    Only returns active, non-discontinued products.

    Returns:
        QuerySet: optimised product queryset.
    """
    return (
        Product.objects.filter(is_active=True, is_discontinued=False)
        .select_related("category", "brand")
        .only(
            "id",
            "name",
            "slug",
            "sku",
            "short_description",
            "product_type",
            "category_id",
            "brand_id",
            "is_active",
            "is_featured",
            "tax_class",
            "average_rating",
            "review_count",
            "created_at",
        )
    )


def get_product_by_slug(slug, include_inactive=False):
    """Return a product by slug with all detail prefetches, or ``None``.

    For public endpoints, ``include_inactive`` is ``False`` so inactive or
    discontinued products return ``None`` (triggering a 404). Admin
    endpoints pass ``True`` to see all products.

    Args:
        slug (str): the product slug.
        include_inactive (bool): whether to include inactive products.

    Returns:
        Product | None: the matching product with prefetched relations.
    """
    qs = Product.objects.select_related(
        "category",
        "brand",
        "replacement_product",
    ).prefetch_related(
        Prefetch(
            "variants",
            queryset=ProductVariant.objects.filter(is_active=True).prefetch_related(
                "pricing_tiers"
            ),
        ),
        "images",
    )
    if not include_inactive:
        qs = qs.filter(is_active=True, is_discontinued=False)
    return qs.filter(slug=slug).first()


def get_product_by_pk(pk):
    """Return a product by PK for admin endpoints (includes inactive).

    Args:
        pk (int): the product primary key.

    Returns:
        Product | None: the matching product.
    """
    qs = Product.objects.select_related(
        "category",
        "brand",
        "replacement_product",
    ).prefetch_related(
        "variants",
        "images",
    )
    return qs.filter(pk=pk).first()


def get_products_for_category(category_id):
    """Return active products in a category.

    Args:
        category_id (int): the category PK.

    Returns:
        QuerySet: optimised product queryset for the category.
    """
    return get_product_list_queryset().filter(category_id=category_id)


def get_products_for_brand(brand_id):
    """Return active products for a brand.

    Args:
        brand_id (int): the brand PK.

    Returns:
        QuerySet: optimised product queryset for the brand.
    """
    return get_product_list_queryset().filter(brand_id=brand_id)


def get_all_products_admin():
    """Return all products for admin management (includes inactive).

    Returns:
        QuerySet: all products with prefetched relations.
    """
    return Product.objects.select_related("category", "brand").order_by("-created_at")
