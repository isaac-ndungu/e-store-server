"""Selectors for the collections app.

Read-only query helpers for listing collections and reading a collection's
membership. Views delegate here so query patterns (filters, prefetching,
ordering, the membership cache read) live in one place and stay consistent.
"""

from django.db.models import Prefetch, Q
from django.utils import timezone

from apps.catalog.models import Product, ProductImage
from apps.collections import cache
from apps.collections.models import Collection


def _in_window_filter():
    """Return the query condition for collections active right now.

    A collection is live when it has no configured ``starts_at`` /
    ``ends_at`` or when ``now`` falls inside the window. Expressed as two
    nullable comparisons so a missing bound never excludes the collection.

    Returns:
        Q: the filter object.
    """
    now = timezone.now()
    return (Q(starts_at__isnull=True) | Q(starts_at__lte=now)) & (
        Q(ends_at__isnull=True) | Q(ends_at__gte=now)
    )


def list_collections(active_only=False, collection_type=None, display_location=None):
    """Return collections for public or admin listing.

    Args:
        active_only (bool): when True, only collections offered to the
            storefront are returned (active and inside their window).
        collection_type (str | None): optional ``manual`` / ``smart`` filter.
        display_location (str | None): optional display-location filter.

    Returns:
        QuerySet: collections in display order.
    """
    queryset = Collection.objects.all()
    if active_only:
        queryset = queryset.filter(is_active=True).filter(_in_window_filter())
    if collection_type is not None:
        queryset = queryset.filter(collection_type=collection_type)
    if display_location is not None:
        queryset = queryset.filter(display_location=display_location)
    return queryset


def get_collection_by_slug(slug, active_only=False):
    """Return a single collection by slug, or None.

    Args:
        slug (str): the collection slug.
        active_only (bool): when True, inactive or out-of-window collections
            are not returned.

    Returns:
        Collection | None: the collection, or None if not found (or hidden
            when ``active_only`` is True).
    """
    queryset = Collection.objects.filter(slug=slug)
    if active_only:
        queryset = queryset.filter(is_active=True).filter(_in_window_filter())
    return queryset.first()


def get_collection_product_pks(collection):
    """Return a collection's product ids, from cache when warm.

    Reads the per-slug cached list and falls back to the current membership
    rows in display order when the cache is cold.

    Args:
        collection (Collection): the collection.

    Returns:
        list[int]: the product primary keys in display order.
    """
    cached = cache.get_cached_product_pks(collection.slug)
    if cached is not None:
        return cached
    return list(
        collection.memberships.order_by("sort_order", "pk").values_list(
            "product_id", flat=True
        )
    )


def get_collection_products(collection):
    """Return the products in a collection, in display order.

    Matches the catalog's slim product queryset shape (``select_related`` /
    ``prefetch_related`` / ``only``) so the detail payload stays small and
    no per-row queries fire while serializing.

    Args:
        collection (Collection): the collection.

    Returns:
        list[Product]: the collection's active products, in display order.
    """
    pks = get_collection_product_pks(collection)
    if not pks:
        return []
    order_map = {pk: index for index, pk in enumerate(pks)}
    products = (
        Product.objects.filter(pk__in=pks, is_active=True)
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
    return sorted(products, key=lambda product: order_map[product.pk])
