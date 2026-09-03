"""Redis-backed caching of bundle price computations.

Bundle prices are derived from live component-variant prices and a bundle-level
discount. Pricing is read on every storefront bundle page and at checkout, and
depends only on data that changes rarely — component variants and the bundle's
own discount fields. Caching the computed price per slug and invalidating it
explicitly (via ``apps.bundles.signals``) whenever the bundle or its items
change avoids recomputing on every request without serving a stale price
mid-window.

Keys are namespaced with a ``bundle:price:`` prefix and the bundle's slug,
matching the project's colon-separated cache-key convention. All reads degrade
gracefully to a fresh computation when the cache is cold.
"""

from decimal import Decimal

from django.core.cache import cache

# How long a cached bundle price is kept. Invalidation is signal-driven, so
# this TTL is a safety net rather than the primary freshness mechanism.
_BUNDLE_PRICE_TTL_SECONDS = 30 * 60


def _cache_key(slug):
    """Return the storage key for a bundle's cached price.

    Args:
        slug (str): the bundle slug.

    Returns:
        str: the cache key.
    """
    return f"bundle:price:{slug}"


def get_cached_bundle_price(slug):
    """Return the cached price dict for a bundle, if any.

    Args:
        slug (str): the bundle slug.

    Returns:
        dict | None: the cached price breakdown, or ``None`` when cold.
    """
    return cache.get(_cache_key(slug))


def cache_bundle_price(slug, price_data, ttl_seconds=_BUNDLE_PRICE_TTL_SECONDS):
    """Store a bundle's computed price breakdown.

    Args:
        slug (str): the bundle slug.
        price_data (dict): the price breakdown to cache.
        ttl_seconds (int): how long to keep the value.
    """
    cache.set(_cache_key(slug), price_data, ttl_seconds)


def invalidate_bundle_price(slug):
    """Drop a bundle's cached price.

    Called from the bundle signals whenever the bundle or one of its items
    changes, so a stale price is never served.

    Args:
        slug (str): the bundle slug.
    """
    cache.delete(_cache_key(slug))


def invalidate_all_bundle_prices():
    """Drop every cached bundle price.

    Called when a promotion changes in a way that could affect any bundle —
    a sitewide or category sale can change the discount-aware component price
    of many bundles at once, so it is cheaper and simpler to clear all bundle
    prices than to track which bundles are affected.
    """
    from apps.bundles.models import Bundle

    for slug in Bundle.objects.values_list("slug", flat=True):
        cache.delete(_cache_key(slug))


def _money(value):
    """Return ``value`` as an exact ``Decimal``.

    ``DecimalField`` values are ``Decimal`` under PostgreSQL but plain strings
    under the in-memory SQLite used by tests, so any arithmetic on a money
    field must pass through here first.

    Args:
        value: a ``DecimalField`` value in its backend-native form.

    Returns:
        Decimal: the exact decimal value.
    """
    return value if isinstance(value, Decimal) else Decimal(value)
