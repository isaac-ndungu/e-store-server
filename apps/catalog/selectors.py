"""Selectors for the catalog app.

Optimized read-only query helpers that keep views thin and give a single
reference for how catalog data is fetched. Every queryset uses
``select_related`` / ``prefetch_related`` / ``annotate`` to prevent N+1
queries.
"""

from datetime import timedelta

from django.db.models import Count, Prefetch, Q
from django.utils import timezone

from apps.catalog.models import Brand, Category, Product, ProductImage, RelatedProduct

# Popularity is measured over a rolling window so a product that was viewed
# heavily once does not stay pinned to the top forever.
POPULARITY_WINDOW_DAYS = 30


def get_active_categories():
    """Return active categories for public browsing.

    Annotates each category with its product count so list serialization
    does not fire a count query per row.

    Returns:
        QuerySet: active ``Category`` rows with a ``product_count``
            annotation.
    """
    return (
        Category.objects.filter(is_active=True)
        .select_related("parent")
        .annotate(product_count=Count("products"))
        .order_by("sort_order", "name")
    )


def get_active_brands():
    """Return all brands for browsing and management.

    Annotates each brand with its product count to avoid a count query per
    row during list serialization.

    Returns:
        QuerySet: ``Brand`` rows with a ``product_count`` annotation,
            ordered by name.
    """
    return (
        Brand.objects.all().annotate(product_count=Count("products")).order_by("name")
    )


def get_product_list_queryset():
    """Return the base queryset for the public product list.

    Pre-fetches category, brand, and the primary image to avoid N+1 on list
    serialization. Only returns active, non-discontinued products.

    Returns:
        QuerySet: optimised product queryset.
    """
    return (
        Product.objects.filter(is_active=True, is_discontinued=False)
        .select_related("category", "brand")
        .prefetch_related(
            Prefetch(
                "images",
                queryset=ProductImage.objects.filter(is_primary=True),
                to_attr="primary_images",
            )
        )
        .only(
            "id",
            "name",
            "slug",
            "sku",
            "price",
            "compare_at_price",
            "stock_status",
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


def get_product_by_slug(
    slug,
    include_inactive=False,
    include_faqs=True,
    include_related=True,
):
    """Return a product by slug with all detail prefetches, or ``None``.

    For public endpoints, ``include_inactive`` is ``False`` so inactive or
    discontinued products return ``None`` (triggering a 404). Admin
    endpoints pass ``True`` to see all products.

    Args:
        slug (str): the product slug.
        include_inactive (bool): whether to include inactive products.
        include_faqs (bool): whether to prefetch the product's FAQs. Callers
            that do not serialize FAQs skip that query.
        include_related (bool): whether to prefetch related products. Callers
            that do not serialize related products skip that query.

    Returns:
        Product | None: the matching product with prefetched relations.
    """
    qs = Product.objects.select_related(
        "category",
        "brand",
        "replacement_product",
    ).prefetch_related(
        "pricing_tiers",
        "images",
    )
    if include_related:
        qs = qs.prefetch_related(
            Prefetch(
                "related_from",
                queryset=RelatedProduct.objects.select_related(
                    "related_product__category",
                    "related_product__brand",
                )
                .prefetch_related(
                    Prefetch(
                        "related_product__images",
                        queryset=ProductImage.objects.filter(is_primary=True),
                        to_attr="primary_images",
                    )
                )
                .order_by("sort_order"),
            )
        )
    if include_faqs:
        qs = qs.prefetch_related("faqs")
    if not include_inactive:
        qs = qs.filter(is_active=True, is_discontinued=False)
    return qs.filter(slug=slug).first()


def get_all_products_admin():
    """Return all products for admin management (includes inactive).

    Returns:
        QuerySet: all products with prefetched relations.
    """
    return (
        Product.objects.select_related("category", "brand")
        .prefetch_related(
            Prefetch(
                "images",
                queryset=ProductImage.objects.filter(is_primary=True),
                to_attr="primary_images",
            )
        )
        .order_by("-created_at")
    )


def order_by_popularity(queryset):
    """Order products by real view counts from the last 30 days.

    Uses the durable ``ProductViewEvent`` history (exposed through the
    ``view_events`` reverse relation) rather than inventing a popularity
    score. Ties fall back to review volume and recency so a catalog with no
    view history still has a stable order.

    Args:
        queryset (QuerySet): the already-filtered product queryset.

    Returns:
        QuerySet: the queryset annotated with ``view_count`` and ordered most
            viewed first.
    """
    since = timezone.now() - timedelta(days=POPULARITY_WINDOW_DAYS)
    return queryset.annotate(
        view_count=Count(
            "view_events",
            filter=Q(view_events__created_at__gte=since),
        )
    ).order_by("-view_count", "-review_count", "-created_at")
