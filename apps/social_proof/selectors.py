"""Selectors for the social proof app.

Read helpers for resolving active products, the recent-sales feed, and
batching live-viewer counts. Live-viewer counts are read from the Redis cache
via ``apps.social_proof.cache``; the product lookups here stay on the slugs
and primary keys the storefront passes in and never load the wide product row.
"""

from datetime import timedelta

from django.utils import timezone

from apps.catalog.models import Product
from apps.orders.models import OrderItem
from apps.social_proof.constants import RECENT_SALES_STATUSES, RECENT_SALES_WINDOW_HOURS


def get_active_product_by_slug(slug):
    """Return an active, non-discontinued product by slug, or None.

    Uses ``only("pk")`` because the social-proof endpoints touch nothing but
    the primary key — the wide product row is not loaded.

    Args:
        slug (str): the product slug.

    Returns:
        Product | None: the product, or None when missing or hidden.
    """
    return (
        Product.objects.only("pk")
        .filter(slug=slug, is_active=True, is_discontinued=False)
        .first()
    )


def list_products_by_slugs(slugs):
    """Return the active products matching any of the given slugs.

    Args:
        slugs (iterable of str): the requested product slugs.

    Returns:
        QuerySet: matching active, non-discontinued products, fields limited
            to the slug and primary key.
    """
    return (
        Product.objects.only("pk", "slug")
        .filter(
            slug__in=set(slugs),
            is_active=True,
            is_discontinued=False,
        )
        .order_by("pk")
    )


def list_recent_sales(limit):
    """Return the most recent completed purchases for the social-proof feed.

    Surfaces only the product, quantity, and purchase time — never the
    customer's identity — so the storefront can show "someone just bought X"
    without leaking who. Bundle purchases appear as one row per component,
    matching how they are stored.

    Args:
        limit (int): how many lines to return, newest first.

    Returns:
        QuerySet: recent ``OrderItem`` rows for orders in a completed status,
            products selected for the feed and fetched in one join.
    """
    cutoff = timezone.now() - timedelta(hours=RECENT_SALES_WINDOW_HOURS)
    return (
        OrderItem.objects.filter(
            order__status__in=RECENT_SALES_STATUSES,
            order__placed_at__gte=cutoff,
        )
        .select_related("product")
        .only(
            "product_name",
            "quantity",
            "order__placed_at",
            "product__id",
            "product__slug",
        )[:limit]
    )
