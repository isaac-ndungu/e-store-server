"""Redis-backed caching of effective prices.

``get_effective_price`` is read on every storefront product page and at
checkout and depends on the variant's base price and the currently applicable
discounts. Both change rarely, but a discount change can affect many variants
at once — a sitewide or category sale touches every product in its scope — so
the cache cannot be invalidated per-key when a discount is saved. Instead a
generation counter is bumped on any relevant change: the computed price is
stored alongside the generation it was computed under, and a read recomputes
whenever the stored generation no longer matches the current one. This keeps
reads O(1) when nothing changed and pays a recompute only when a discount or
a variant price actually did.

Keys are namespaced with a ``promotions:effective:`` prefix and the variant
id, matching the project's colon-separated cache-key convention. Reads
degrade gracefully to a fresh computation when the cache is cold.
"""

from django.core.cache import cache

# How long an effective price is kept. Invalidation is generation-driven, so
# this TTL is a safety net rather than the primary freshness mechanism.
_EFFECTIVE_TTL_SECONDS = 30 * 60

# The Redis flag name for the current discount generation.
_GENERATION_KEY = "promotions:discount_generation"


def _effective_key(variant_id, within_bundle):
    """Return the storage key for a variant's effective price.

    Args:
        variant_id (int): the product variant id.
        within_bundle (bool): whether the price is inside a bundle.

    Returns:
        str: the cache key.
    """
    return f"promotions:effective:{variant_id}:{int(within_bundle)}"


def get_discount_generation():
    """Return the current discount generation counter.

    The counter is incremented whenever a discount or a variant price changes,
    invalidating every cached effective price at once.

    Returns:
        int: the current generation.
    """
    return cache.get(_GENERATION_KEY, 0)


def bump_discount_generation():
    """Increment the discount generation, invalidating cached effective prices.

    Called from the promotions signals when a discount or a variant price
    changes. Because any such change can affect many variants at once, a
    single bump invalidates the whole effective-price cache rather than
    tracking which keys are affected.
    """
    try:
        cache.incr(_GENERATION_KEY)
    except ValueError:
        cache.set(_GENERATION_KEY, 1)


def get_cached_effective_price(variant_id, within_bundle, generation):
    """Return the cached effective price if it was computed under ``generation``.

    Args:
        variant_id (int): the product variant id.
        within_bundle (bool): whether the price is inside a bundle.
        generation (int): the generation the value must have been computed under.

    Returns:
        dict | None: the cached price data, or ``None`` when cold or stale.
    """
    entry = cache.get(_effective_key(variant_id, within_bundle))
    if entry is None:
        return None
    stored_generation, price_data = entry
    if stored_generation != generation:
        return None
    return price_data


def cache_effective_price(variant_id, within_bundle, generation, price_data):
    """Store an effective price under the generation it was computed with.

    Args:
        variant_id (int): the product variant id.
        within_bundle (bool): whether the price is inside a bundle.
        generation (int): the generation the price was computed under.
        price_data (dict): the price data to cache.
    """
    cache.set(
        _effective_key(variant_id, within_bundle),
        (generation, price_data),
        _EFFECTIVE_TTL_SECONDS,
    )


def invalidate_effective_price(variant_id, within_bundle):
    """Drop the effective price for a single variant.

    Used when a variant's own price changes, so the next read recomputes
    without waiting for a generation bump.

    Args:
        variant_id (int): the product variant id.
        within_bundle (bool): whether the price is inside a bundle.
    """
    cache.delete(_effective_key(variant_id, within_bundle))
