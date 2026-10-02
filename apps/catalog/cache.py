"""Redis-backed caching of the storefront category tree and product list.

The active category list with live product counts is read into every
storefront navigation, yet it changes only when a category is created,
edited, or deleted, or when a product's category membership changes  -  all
rare operations in steady state. Caching the serialized rows and the
per-slug detail payload avoids re-annotating the count query on every
page load.

The public product list is the hottest read on the storefront and its
payload (paginated rows plus facet aggregates) is identical for every
anonymous caller asking the same filters, so the whole serialized page is
cached per filter combination. Freshness is generation-driven, exactly
like the category cache: any product, category, image, or facet change
bumps the generation and invalidates every cached page at once. A short
TTL is a safety net for writes that bypass signals (review aggregates
update the product row in place without firing ``post_save``), so ratings
converge within the TTL rather than waiting for the next product edit.

Changed data can affect many rows at once (a product moving category
increments one count and decrements another), so invalidation is
generation-driven: any relevant change bumps a counter, and a read is
served only if it was cached under the current generation. Keys are
namespaced with a ``catalog:`` prefix matching the project's
colon-separated cache-key convention. Reads degrade gracefully to a fresh
computation when the cache is cold; the TTL is a safety net, not the
primary freshness mechanism.
"""

from django.core.cache import cache

_CATEGORY_TTL_SECONDS = 30 * 60

_CATEGORY_GENERATION_KEY = "catalog:category_generation"

_FACET_GENERATION_KEY = "catalog:facet_generation"
_FACET_DEFS_TTL_SECONDS = 15 * 60
_FACET_COUNTS_TTL_SECONDS = 5 * 60

_PRODUCT_LIST_GENERATION_KEY = "catalog:product_list_generation"
_PRODUCT_LIST_TTL_SECONDS = 60


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


def get_facet_generation():
    """Return the current facet generation counter.

    Returns:
        int: the current generation.
    """
    return cache.get(_FACET_GENERATION_KEY, 0)


def bump_facet_generation():
    """Increment the facet generation, invalidating cached facet reads."""
    try:
        cache.incr(_FACET_GENERATION_KEY)
    except ValueError:
        cache.set(_FACET_GENERATION_KEY, 1)


def _facet_defs_key(generation):
    """Return the storage key for cached facet definitions.

    Args:
        generation (int): the generation the rows were cached under.

    Returns:
        str: the cache key.
    """
    return f"catalog:facets:defs:{generation}"


def get_cached_facet_defs(generation):
    """Return cached facet definition payloads, if current.

    Args:
        generation (int): the generation the rows must have been cached under.

    Returns:
        list | None: serialized facet rows, or None when cold.
    """
    return cache.get(_facet_defs_key(generation))


def cache_facet_defs(generation, rows):
    """Store serialized facet definition rows.

    Args:
        generation (int): the generation the rows were computed under.
        rows (list): serialized facet rows.
    """
    cache.set(_facet_defs_key(generation), rows, _FACET_DEFS_TTL_SECONDS)


def _facet_counts_key(generation, query_hash):
    """Return the storage key for cached facet counts.

    Args:
        generation (int): the facet generation.
        query_hash (str): hash of the filtered queryset SQL.

    Returns:
        str: the cache key.
    """
    return f"catalog:facets:counts:{generation}:{query_hash}"


def get_cached_facet_counts(generation, query_hash):
    """Return cached facet counts for a filter combination, if current.

    Args:
        generation (int): the facet generation.
        query_hash (str): hash of the filtered queryset SQL.

    Returns:
        dict | None: the cached counts, or None when cold.
    """
    return cache.get(_facet_counts_key(generation, query_hash))


def cache_facet_counts(generation, query_hash, counts):
    """Store facet counts for a filter combination.

    Args:
        generation (int): the facet generation.
        query_hash (str): hash of the filtered queryset SQL.
        counts (dict): the computed facet counts.
    """
    cache.set(
        _facet_counts_key(generation, query_hash), counts, _FACET_COUNTS_TTL_SECONDS
    )


def get_product_list_generation():
    """Return the current product-list generation counter.

    The counter is incremented whenever a product, category, product image,
    or facet definition is saved or deleted, invalidating every cached
    product-list page at once.

    Returns:
        int: the current generation.
    """
    return cache.get(_PRODUCT_LIST_GENERATION_KEY, 0)


def bump_product_list_generation():
    """Increment the product-list generation, invalidating cached pages."""
    try:
        cache.incr(_PRODUCT_LIST_GENERATION_KEY)
    except ValueError:
        cache.set(_PRODUCT_LIST_GENERATION_KEY, 1)


def _product_list_key(generation, query_hash):
    """Return the storage key for a cached product-list page.

    Args:
        generation (int): the generation the page was cached under.
        query_hash (str): hash of the normalized request query params.

    Returns:
        str: the cache key.
    """
    return f"catalog:product_list:page:{generation}:{query_hash}"


def get_cached_product_list_page(generation, query_hash):
    """Return a cached product-list page payload, if current.

    Args:
        generation (int): the generation the page must have been cached under.
        query_hash (str): hash of the normalized request query params.

    Returns:
        dict | None: the serialized page payload, or None when cold.
    """
    return cache.get(_product_list_key(generation, query_hash))


def cache_product_list_page(generation, query_hash, payload):
    """Store a serialized product-list page payload.

    Args:
        generation (int): the generation the page was computed under.
        query_hash (str): hash of the normalized request query params.
        payload (dict): the serialized page (results, facets, pagination).
    """
    cache.set(
        _product_list_key(generation, query_hash), payload, _PRODUCT_LIST_TTL_SECONDS
    )
