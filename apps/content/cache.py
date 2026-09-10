"""Redis-backed caching of storefront banners and content pages.

Banner rotations and published content pages are rendered into every
storefront page load yet change only when staff publish, edit, schedule, or
delete them. Caching the serialized payloads avoids re-querying banners by
placement and pages by slug on every visit.

Both banner rows and pages are invalidated generation-driven, mirroring the
category and promotion caches: any change to a banner or page bumps the
relevant counter, and a read is served only if it was cached under the
current generation. Keys are namespaced with a ``content:`` prefix matching
the project's colon-separated cache-key convention. Reads degrade gracefully
to fresh computation when cold; the TTL is a safety net, not the primary
freshness mechanism.
"""

from django.core.cache import cache

_BANNER_TTL_SECONDS = 10 * 60
_PAGE_TTL_SECONDS = 10 * 60

_BANNER_GENERATION_KEY = "content:banner_generation"
_PAGE_GENERATION_KEY = "content:page_generation"


def _banner_key(placement, generation):
    """Return the storage key for a placement's banner rows.

    Args:
        placement (str): the banner placement key.
        generation (int): the generation the rows were cached under.

    Returns:
        str: the cache key.
    """
    return f"content:banner:list:{placement}:{generation}"


def _page_key(slug, generation):
    """Return the storage key for a content page under a generation.

    Args:
        slug (str): the page slug.
        generation (int): the generation the page was cached under.

    Returns:
        str: the cache key.
    """
    return f"content:page:{slug}:{generation}"


def get_banner_generation():
    """Return the current banner generation counter.

    The counter is incremented whenever a banner is saved or deleted,
    invalidating every cached banner placement at once.

    Returns:
        int: the current generation.
    """
    return cache.get(_BANNER_GENERATION_KEY, 0)


def bump_banner_generation():
    """Increment the banner generation, invalidating cached banner rows.

    Called from the content signals when a banner is created, edited, or
    deleted. A single bump invalidates every placement rather than tracking
    which keys are affected.
    """
    try:
        cache.incr(_BANNER_GENERATION_KEY)
    except ValueError:
        cache.set(_BANNER_GENERATION_KEY, 1)


def get_cached_banner_rows(placement, generation):
    """Return the cached banner rows for a placement, if current.

    Args:
        placement (str): the banner placement key.
        generation (int): the generation the rows must have been cached under.

    Returns:
        list | None: the serialized banner rows, or ``None`` when cold or
            stale.
    """
    return cache.get(_banner_key(placement, generation))


def cache_banner_rows(placement, generation, rows):
    """Store the serialized banner rows for a placement.

    Args:
        placement (str): the banner placement key.
        generation (int): the generation the rows were computed under.
        rows (list): the serialized banner rows.
    """
    cache.set(_banner_key(placement, generation), rows, _BANNER_TTL_SECONDS)


def get_page_generation():
    """Return the current content page generation counter.

    The counter is incremented whenever a page is saved or deleted,
    invalidating every cached page at once.

    Returns:
        int: the current generation.
    """
    return cache.get(_PAGE_GENERATION_KEY, 0)


def bump_page_generation():
    """Increment the content page generation, invalidating cached pages.

    Called from the content signals when a page is created, edited, or
    deleted.
    """
    try:
        cache.incr(_PAGE_GENERATION_KEY)
    except ValueError:
        cache.set(_PAGE_GENERATION_KEY, 1)


def get_cached_page(slug, generation):
    """Return a cached content page payload, if current.

    Args:
        slug (str): the page slug.
        generation (int): the generation the page must have been cached under.

    Returns:
        dict | None: the serialized page, or ``None`` when cold or stale.
    """
    return cache.get(_page_key(slug, generation))


def cache_page(slug, generation, data):
    """Store a serialized content page payload.

    Args:
        slug (str): the page slug.
        generation (int): the generation the page was computed under.
        data (dict): the serialized page.
    """
    cache.set(_page_key(slug, generation), data, _PAGE_TTL_SECONDS)
