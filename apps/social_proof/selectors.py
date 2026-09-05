"""Selectors for the social proof app.

Read helpers for resolving active products and browsing the durable view-event
history. Live-viewer counts are read from the Redis cache via
``apps.social_proof.cache``; the product lookup here stays on the primary key
only and the event queries below serve analytics and admin review, not the
hot storefront read.
"""

from apps.catalog.models import Product
from apps.social_proof.models import ProductViewEvent


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


def list_recent_view_events(limit=50):
    """Return the most recent view events with their products.

    Args:
        limit (int): how many rows to return, newest first.

    Returns:
        QuerySet: recent ``ProductViewEvent`` rows, products prefetched.
    """
    return ProductViewEvent.objects.select_related("product")[:limit]


def count_views_for_product(product):
    """Return the total durable view-events recorded for a product.

    Args:
        product (Product): the product.

    Returns:
        int: the number of recorded view events.
    """
    return ProductViewEvent.objects.filter(product=product).count()
