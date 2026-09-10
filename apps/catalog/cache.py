"""Redis-backed caching of the storefront category tree.

The active category list with live product counts is read into every
storefront navigation, yet it changes only when a category is created,
edited, or deleted, or when a product's category membership changes — all
rare operations in steady state. Caching the serialized rows and the
per-slug detail payload avoids re-annotating the count query on every
page load.

Changed data can affect many rows at once (a product moving category
increments one count and decrements another), so invalidation is
generation-driven: any relevant change bumps a counter, and a read is
served only if it was cached under the current generation. Keys are
namespaced with a ``catalog:category:`` prefix matching the project's
colon-separated cache-key convention. Reads degrade gracefully to a fresh
computation when the cache is cold; the TTL is a safety net, not the
primary freshness mechanism.
"""

from django.core.cache import cache

_CATEGORY_TTL_SECONDS = 30 * 60

_CATEGORY_GENERATION_KEY = "catalog:category_generation"


def _list_key(generation):
    """Return the storage key for the category list under a generation.

    Args:
        generation (int): the generation the rows were cached under.

    Returns:
        str: the cache key.
    """
    return f"catalog:category:list:{generation}"


def _detail_key(slug, generation):
    """Return the storage key for a category detail under a generation.

    Args:
        slug (str): the category slug.
        generation (int): the generation the payload was cached under.

    Returns:
        str: the cache key.
    """
    return f"catalog:category:detail:{slug}:{generation}"


def get_category_generation():
    """Return the current category generation counter.

    The counter is incremented whenever a category or a product's category
    membership changes, invalidating every cached category read at once.

    Returns:
        int: the current generation.
    """
    return cache.get(_CATEGORY_GENERATION_KEY, 0)


def bump_category_generation():
    """Increment the category generation, invalidating cached category reads.

    Called from the catalog signals when a category is saved or deleted, or
    when a product is saved or deleted (either can change a category's
    product count). A single bump invalidates the whole category cache
    rather than tracking which rows are affected.
    """
    try:
        cache.incr(_CATEGORY_GENERATION_KEY)
    except ValueError:
        cache.set(_CATEGORY_GENERATION_KEY, 1)


def get_cached_category_rows(generation):
    """Return the cached category list rows, if current.

    Args:
        generation (int): the generation the rows must have been cached under.

    Returns:
        list | None: the serialized active category rows, or ``None`` when
            cold or stale.
    """
    return cache.get(_list_key(generation))


def cache_category_rows(generation, rows):
    """Store the serialized active category list.

    Args:
        generation (int): the generation the rows were computed under.
        rows (list): the serialized category rows.
    """
    cache.set(_list_key(generation), rows, _CATEGORY_TTL_SECONDS)


def get_cached_category_detail(slug, generation):
    """Return a cached category detail payload, if current.

    Args:
        slug (str): the category slug.
        generation (int): the generation the payload must have been cached
            under.

    Returns:
        dict | None: the serialized category detail, or ``None`` when cold
            or stale.
    """
    return cache.get(_detail_key(slug, generation))


def cache_category_detail(slug, generation, data):
    """Store a serialized category detail payload.

    Args:
        slug (str): the category slug.
        generation (int): the generation the payload was computed under.
        data (dict): the serialized category detail.
    """
    cache.set(_detail_key(slug, generation), data, _CATEGORY_TTL_SECONDS)
