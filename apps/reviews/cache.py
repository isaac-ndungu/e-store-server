"""Cached feature flag for the reviews app.

The ``enable_reviews`` toggle is cached so the per-request write gate is one
cache read. Any change to the configuration singleton must drop the cached
value or the flag could serve stale state until the TTL expires; the signal
receiver in ``apps.reviews.signals`` invalidates it on every ``SiteConfig``
save, mirroring the social-proof feature gate.
"""

from django.core.cache import cache

# Cache key for the ``enable_reviews`` site toggle, read on every write path.
FEATURE_FLAG_KEY = "reviews:feature_enabled"
_FEATURE_FLAG_TTL_SECONDS = 60


def is_feature_enabled():
    """Return whether the reviews feature is switched on.

    Reads the cached value, refreshing it from ``SiteConfig`` on a cache miss.
    A cached ``False`` is distinct from a miss because the cache backend
    returns ``None`` only when the key is absent.

    Returns:
        bool: the ``enable_reviews`` setting, defaulting to True.
    """
    enabled = cache.get(FEATURE_FLAG_KEY)
    if enabled is None:
        from apps.core.models import SiteConfig

        enabled = SiteConfig.load().settings.get("enable_reviews", True)
        cache.set(FEATURE_FLAG_KEY, enabled, _FEATURE_FLAG_TTL_SECONDS)
    return bool(enabled)


def invalidate_feature_enabled():
    """Drop the cached feature flag so the next read reflects ``SiteConfig``.

    Called from the ``SiteConfig`` post-save signal so the write gate never
    serves a stale value after staff change it in the admin.
    """
    cache.delete(FEATURE_FLAG_KEY)
