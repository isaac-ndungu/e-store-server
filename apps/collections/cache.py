"""Redis-backed caching of collection membership product lists.

The public collection detail endpoint renders a collection's products. For
lists that change rarely — curated manual collections, or smart collections
between their periodic refreshes — recomputing memberships and hitting the
database on every request is wasteful on slow, data-costly connections. The
product id list for a collection is cached per slug and invalidated
explicitly (via ``apps.collections.signals``) whenever the collection or its
membership changes, rather than relying on a short TTL that could serve a
stale list mid-window.

Keys are namespaced with a ``collection:memberships:`` prefix and the
collection's slug, matching the project's cache-key convention of a colon
separated prefix. TTLs are named constants; all reads degrade gracefully to
the database when the cache is cold.
"""

from django.core.cache import cache

# How long a cached product list is kept. The smart-refresh job repopulates
# entries on its own schedule, so this is a safety net, not the primary
# freshness mechanism.
_COLLECTION_TTL_SECONDS = 30 * 60


def _cache_key(slug):
    """Return the storage key for a collection's cached product list.

    Args:
        slug (str): the collection slug.

    Returns:
        str: the cache key.
    """
    return f"collection:memberships:{slug}"


def get_cached_product_pks(slug):
    """Return the cached product id list for a collection, if any.

    Args:
        slug (str): the collection slug.

    Returns:
        list[int] | None: the cached product primary keys, or ``None`` when
            the cache is cold.
    """
    return cache.get(_cache_key(slug))


def cache_product_pks(slug, product_pks, ttl_seconds=_COLLECTION_TTL_SECONDS):
    """Store a collection's product id list.

    Args:
        slug (str): the collection slug.
        product_pks (list[int]): the product primary keys in order.
        ttl_seconds (int): how long to keep the value.
    """
    cache.set(_cache_key(slug), product_pks, ttl_seconds)


def invalidate_collection(slug):
    """Drop a collection's cached product list.

    Called from the collection and membership signals whenever the collection
    or its membership changes, so a stale list is never served.

    Args:
        slug (str): the collection slug.
    """
    cache.delete(_cache_key(slug))
