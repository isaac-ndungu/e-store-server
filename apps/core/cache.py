"""Redis-backed caching of the SiteConfig singleton.

The storefront fetches site configuration on cold load and several
selectors read feature toggles per request, yet the row changes only when
staff edit business settings. Reads are cached briefly and dropped
explicitly whenever the singleton is saved, rather than relying on the
TTL for freshness.
"""

from django.core.cache import cache

_SITE_CONFIG_KEY = "core:site_config"
_SITE_CONFIG_TTL_SECONDS = 15 * 60


def get_cached_site_config():
    """Return the cached SiteConfig payload, if warm.

    Returns:
        dict | None: cached field values, or None when cold.
    """
    return cache.get(_SITE_CONFIG_KEY)


def cache_site_config(data):
    """Store SiteConfig field values.

    Args:
        data (dict): field values to cache.
    """
    cache.set(_SITE_CONFIG_KEY, data, _SITE_CONFIG_TTL_SECONDS)


def invalidate_site_config():
    """Drop the cached SiteConfig payload."""
    cache.delete(_SITE_CONFIG_KEY)
